"""
Canonical episode video renderer -- the SAME implementation used by the
CLI entry point (build_episode.py), the production Celery task
(app.tasks.episode_video.produce_episode_video), AND the DEV single-story
endpoint (app.tasks.storyboard_prototype, via render_story_standalone
below). There is deliberately only one enhanced-story-rendering
implementation; no caller duplicates any of this logic.

render_story_enhanced() is the canonical enhanced per-story renderer
(storyboard-driven scenes, light-icon watermark, ASS captions with
deterministic keyword emphasis + statistic-scene subordinate style, the
0.32s dark scene-to-scene xfade, no scene/story outro). It requires only
a story's own already-valid storyboard.json + rendered content -- no
episode needs to exist. Two callers, zero duplication:
- render_episode() below calls it once per story, then assembles N of
  its outputs into one episode (intro/gaps/outro, master audio mix).
- render_story_standalone() below calls it exactly once for a single
  story, then muxes that ONE story's own processed narration onto it --
  this is the entire implementation behind the DEV endpoint.

Validated on Story #51 and Episode 3 (25 stories): light-icon watermark,
real ASS captions (deterministic keyword emphasis, subordinate style
during statistic scenes), a short (0.32s) dark xfade scene-to-scene
transition within a story, a 0.6s dark gap + SFX between stories, one
light-surface intro, one final episode outro, and a processed voice +
ducked music + restrained SFX master mix.

Reuses real, already-validated production code UNCHANGED via import:
- app.content.scene_renderer: per-scene-type renderers, render_scene_image,
  render_scene_frame_sequence (statistic count-up), all layout constants.
- app.content.storyboard_composer: _caption_cues / _wrap_caption_text (the
  real, tested Tier-1/Tier-2 clause-boundary caption splitter), _zoompan_expr
  (each scene's own already-tuned motion).
- app.content.storyboard_service.ensure_storyboard: the shared storyboard
  prerequisite (content -> storyboard -> render -> QA), also used by the
  DEV endpoint -- no separate implementation here.

Working-directory independent: every path used by this module (and by
the reused functions above, whose own MEDIA_ROOT = Path("media") is
relative) is resolved against APP_ROOT, and APP_ROOT is made the process
CWD once at import time -- `python build_episode.py --episode-id N` and
the Celery task behave identically regardless of the shell's starting
directory.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
os.chdir(APP_ROOT)  # every reused function's relative MEDIA_ROOT = Path("media") depends on this

from PIL import Image, ImageDraw, ImageFont

from app.content import scene_renderer as sr
from app.content import storyboard_composer as sc
from app.content.storyboard_service import ensure_storyboard
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem
from app.tasks.content import get_or_create_content

MEDIA = APP_ROOT / "media"
OUT_DIR = MEDIA / "pillow_enhanced"

FPS = 25
OUTPUT_W, OUTPUT_H = 1920, 1080
SCENE_CANVAS = f"{sr.SCENE_WIDTH}x{sr.SCENE_HEIGHT}"

SCENE_T = 8 / FPS      # 0.32s -- within-story scene-to-scene transition
STORY_GAP = 0.6        # between-story interlude (hard-cut gap card + SFX/music swell), within spec's 0.5-1.5s
INTRO_DUR, GAP_DUR = 1.8, 0.2

LIGHT_ICON = APP_ROOT / "app/dashboard/branding/icons/5squarefeed-icon-light.png"
LOGO_HORIZONTAL = APP_ROOT / "app/dashboard/branding/logo/5squarefeed-logo-horizontal.png"

# Per-story narration processing chain -- shared UNCHANGED by both
# render_episode()'s master mix (one voice file per story, each
# processed independently before being placed at its own offset) and
# render_story_standalone()'s single-story mux, via process_story_voice()
# below. Never duplicated as a second copy of this filter string.
VOICE_CHAIN = ("highpass=f=80,equalizer=f=3000:t=q:w=1:g=2,"
               "acompressor=threshold=-18dB:ratio=2.5:attack=8:release=180:makeup=2,"
               "loudnorm=I=-16:TP=-1.5:LRA=7")


def process_story_voice(story_id: int, out_path: Path) -> None:
    """Apply the one shared narration processing chain to a single
    story's raw edge-tts mp3. Same ffmpeg call used per-story inside
    render_episode()'s master mix and by render_story_standalone()."""
    raw = MEDIA / "audio" / f"{story_id}.mp3"
    run(["ffmpeg", "-y", "-i", str(raw), "-af", VOICE_CHAIN, "-ar", "44100", "-ac", "2", str(out_path)])


def _mark_story_video_ready(db, story_id: int) -> None:
    """
    Called only after render_story_enhanced() has returned successfully
    for this story inside render_episode()'s loop below -- preserves
    "video_ready" as meaning "this story's enhanced render actually
    completed", which app/tasks/episode_qa.py's StoryContent.status ==
    "video_ready" filter relies on to decide which stories count as
    "really in the produced video" for per-story QA checks. Before this,
    a story produced only through ensure_script_and_voice topped out at
    "voice_ready" and was silently excluded from that filter even though
    its enhanced segment rendered fine and is in the episode.

    video_path points at this story's own {story_id}_storyboard.mp4 --
    not a new artifact: it's the same base storyboard video
    ensure_storyboard/ensure_all_storyboards already guarantees exists
    (with real narration audio, per storyboard_composer.render_scene_clip)
    as a hard prerequisite before this loop's render_story_enhanced()
    call ever runs. Reusing it here adds no new rendering step -- the
    per-story silent clip render_story_enhanced() itself produces has no
    audio and lives in a transient per-episode work directory, so it is
    deliberately NOT what gets stored as this story's video_path.
    """
    content = get_or_create_content(db, story_id)
    content.video_path = (MEDIA / "videos" / f"{story_id}_storyboard.mp4").relative_to(APP_ROOT).as_posix()
    content.status = "video_ready"
    content.error_message = None
    db.commit()


def _render_and_mark_story(db, story_id: int, work: Path) -> tuple[Path, float]:
    """One iteration of render_episode()'s story loop below, pulled out
    so the "render, THEN mark video_ready -- never the reverse, never
    on a raised exception" ordering is directly unit-testable without
    exercising the rest of render_episode() (intro/outro/audio mix/final
    mux)."""
    clip, dur, _ = render_story_enhanced(story_id, tail_pad=0.0, work=work)
    _mark_story_video_ready(db, story_id)
    return clip, dur


def log(msg: str) -> None:
    print(f"[episode-renderer] {msg}", flush=True)


def run(cmd: list) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd[:4])}...\n{r.stderr[-4000:]}")


# ---------------------------------------------------------------------
# Episode / story lookup -- never re-ranked, never hardcoded.
# ---------------------------------------------------------------------
def load_episode_stories(episode_id: int) -> tuple[list[int], str]:
    """The exact same query production's own render path uses:
    EpisodeStory joined to NewsItem, filtered to selection_status ==
    "primary", ordered by rank_position ascending -- the DB's own stored
    order, never re-ranked or filtered further here."""
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)
        if episode is None:
            raise ValueError(f"Episode {episode_id} not found in the database.")
        rows = (
            db.query(EpisodeStory, NewsItem)
            .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.selection_status == "primary",
            )
            .order_by(EpisodeStory.rank_position.asc())
            .all()
        )
        if not rows:
            raise ValueError(f"Episode {episode_id} has no primary stories selected.")
        story_ids = [es.story_id for es, _ in rows]
        episode_date = episode.episode_date.strftime("%B %d, %Y")
    return story_ids, episode_date


def ensure_all_storyboards(story_ids: list[int]) -> dict:
    """Pre-flight pass: every selected story must have ready production
    content and a valid storyboard before any rendering/assembly begins
    (reusing storyboard_service.ensure_storyboard -- content ensure,
    storyboard generate/compose/QA, all unchanged there). Only a genuine
    "failed" status stops the run; "qa_failed" still leaves a usable
    storyboard+video (a documented, already-accepted QA warning), so it's
    treated as ready-to-use, not fatal -- episode assembly must not start
    on missing artifacts, but it was never gated on every QA check
    passing."""
    reused, regenerated, failures = [], [], []
    with SessionLocal() as db:
        for story_id in story_ids:
            result = ensure_storyboard(db, story_id)
            if result.get("status") == "failed":
                failures.append(result)
                continue
            (reused if result.get("reused") else regenerated).append(story_id)
    if failures:
        details = "; ".join(f"story {f['story_id']} ({f.get('stage')}): {f.get('error')}" for f in failures)
        raise RuntimeError(
            f"Storyboard prerequisite failed for {len(failures)} stor{'y' if len(failures)==1 else 'ies'} "
            f"-- {details}. Stopping before any rendering: episode assembly requires every story ready."
        )
    return {"reused": reused, "regenerated": regenerated}


# ---------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------
def repaint_watermark(png_path: Path) -> None:
    img = Image.open(png_path).convert("RGBA")
    icon = Image.open(LIGHT_ICON).convert("RGBA").resize((sr.LOGO_BADGE_SIZE, sr.LOGO_BADGE_SIZE))
    pos = (sr.CONTENT_MARGIN_X, 90)
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [pos[0] - 6, pos[1] - 6, pos[0] + sr.LOGO_BADGE_SIZE + 6, pos[1] + sr.LOGO_BADGE_SIZE + 6],
        fill=sr.BACKGROUND_COLOR,
    )
    img.paste(icon, pos, icon)
    img.convert("RGB").save(png_path, "PNG")


def render_intro(output_path: Path, episode_date: str, story_count: int) -> None:
    image = Image.new("RGB", (sr.SCENE_WIDTH, sr.SCENE_HEIGHT), (245, 246, 248))
    logo = Image.open(LOGO_HORIZONTAL).convert("RGBA")
    target_w = int(sr.SCENE_WIDTH * 0.6)
    ratio = target_w / logo.width
    logo = logo.resize((target_w, int(logo.height * ratio)))
    x = (sr.SCENE_WIDTH - logo.width) // 2
    y = int(sr.SCENE_HEIGHT * 0.34)
    image.paste(logo, (x, y), logo)
    draw = ImageDraw.Draw(image)
    sub_text = f"{episode_date} — {story_count} Stories A Day"
    sub_font = ImageFont.truetype(sr.FONT_REGULAR, 46)
    w = draw.textlength(sub_text, font=sub_font)
    draw.text(((sr.SCENE_WIDTH - w) // 2, y + logo.height + 44), sub_text, font=sub_font, fill=(90, 98, 115))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG")


def render_outro(output_path: Path) -> None:
    image, draw = sr._new_canvas()
    accent = (64, 156, 255)
    draw.rectangle([(0, 0), (sr.SCENE_WIDTH, 20)], fill=accent)
    sr._draw_logo_badge(image, draw)
    main_font = ImageFont.truetype(sr.FONT_BOLD, 76)
    lines = sr._wrap_text(draw, "That's all for today's 5squareFeed.", main_font, sr.SCENE_WIDTH - sr.CONTENT_MARGIN_X * 2)
    y = 560
    for line in lines:
        draw.text((sr.CONTENT_MARGIN_X, y), line, font=main_font, fill=sr.TEXT_COLOR)
        y += 96
    sub_font = ImageFont.truetype(sr.FONT_REGULAR, 44)
    draw.text((sr.CONTENT_MARGIN_X, y + 20), "See you tomorrow.", font=sub_font, fill=sr.SOURCE_COLOR)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG")


# ---------------------------------------------------------------------
# Deterministic caption emphasis + ASS
# ---------------------------------------------------------------------
def ass_color(rgb: tuple, alpha: int = 0x00) -> str:
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}&"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_header() -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUTPUT_W}
PlayResY: {OUTPUT_H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,40,{ass_color(sr.TEXT_COLOR)},{ass_color(sr.TEXT_COLOR)},&H000000&,&H80000000&,-1,0,0,0,100,100,0,0,1,2,0,2,120,120,60,1
Style: Subordinate,DejaVu Sans,30,{ass_color(sr.TEXT_COLOR, alpha=0x60)},{ass_color(sr.TEXT_COLOR, alpha=0x60)},&H000000&,&H80000000&,-1,0,0,0,100,100,0,0,1,2,0,2,120,120,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_emphasis_set(scene: dict, storyboard: dict) -> set:
    tokens = set()
    for key in ("stat", "tier", "date", "source", "entity", "company", "product"):
        v = scene.get(key)
        if v:
            tokens.add(str(v).lower())
    if storyboard.get("source_name"):
        tokens.add(storyboard["source_name"].lower())
    return tokens


def is_emphasized(word: str, emphasis_set: set) -> bool:
    clean = word.strip(".,;:()\"'—").strip()
    if not clean:
        return False
    if any(ch.isdigit() for ch in clean):
        return True
    return clean.lower() in emphasis_set


def caption_dialogue_line(cue_text: str, start: float, end: float, style: str, emphasis_set: set) -> str:
    """One ASS Dialogue line per real caption cue (from storyboard_composer's
    own tested clause-boundary splitter). No per-word timing available at
    episode scale (would need a fresh edge-tts WordBoundary fetch per
    story) -- static deterministic keyword-colour emphasis + a plain
    fade-in/out instead of per-word karaoke, which is explicitly allowed
    ("kinetic captioning kept light... word highlight/opacity/weight/color")."""
    wrapped = sc._wrap_caption_text(cue_text)
    lines_out = []
    for line in wrapped.split("\n"):
        words_out = []
        for w in line.split(" "):
            if is_emphasized(w, emphasis_set):
                words_out.append(f"{{\\c{ass_color((5, 216, 252))}}}{w}{{\\c{ass_color(sr.TEXT_COLOR)}}}")
            else:
                words_out.append(w)
        lines_out.append(" ".join(words_out))
    text = "\\N".join(lines_out)
    return f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{{\\fad(80,80)}}{text}\n"


# ---------------------------------------------------------------------
# Per-scene render + encode (reuses real production renderers/motion)
# ---------------------------------------------------------------------
def build_segments_for_scene(scene: dict, storyboard: dict, work_dir: Path) -> list[dict]:
    work_dir.mkdir(parents=True, exist_ok=True)
    if scene["scene_type"] in sr._COUNTUP_CAPABLE_TYPES:
        segments = sr.render_scene_frame_sequence(scene, storyboard, work_dir, fps=FPS)
    else:
        png = work_dir / "frame.png"
        sr.render_scene_image(scene, storyboard, png)
        segments = [{"duration": scene["duration"], "frame_paths": [png], "is_ramp": False}]
    for seg in segments:
        for p in seg["frame_paths"]:
            repaint_watermark(p)
    return segments


def encode_segments(segments: list[dict], motion: dict, out_path: Path, pad: float = 0.0) -> None:
    """`pad` extends this clip's OWN encoded duration beyond the scene's
    real duration -- required so the outer xfade_chain's offset math (which
    assumes this padded length) matches what actually got encoded; without
    this the crossfade's offset overruns the real content and truncates it
    (found live: story 122's hero scene was being cut down from 3.6s to
    effectively nothing because only the un-padded duration was encoded)."""
    zoom_expr, x_expr, y_expr = sc._zoompan_expr(motion or {})
    segments = [dict(s) for s in segments]
    if pad > 0.001 and segments:
        last = segments[-1]
        if last["is_ramp"] and len(last["frame_paths"]) > 1:
            segments.append({"duration": pad, "frame_paths": [last["frame_paths"][-1]], "is_ramp": False})
        else:
            segments[-1] = dict(last)
            segments[-1]["duration"] = last["duration"] + pad
    inputs, filters, idx = [], [], 0
    for seg in segments:
        dur = seg["duration"]
        if dur <= 0.02:
            continue
        if seg["is_ramp"] and len(seg["frame_paths"]) > 1:
            seq_dir = seg["frame_paths"][0].parent
            inputs += ["-framerate", str(FPS), "-i", str(seq_dir / "frame_%05d.png")]
        else:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(dur), "-i", str(seg["frame_paths"][0])]
        filters.append(
            f"[{idx}:v]scale={SCENE_CANVAS},"
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d=1:s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},setsar=1[v{idx}]"
        )
        idx += 1
    concat_in = "".join(f"[v{i}]" for i in range(idx))
    filter_complex = ";".join(filters) + f";{concat_in}concat=n={idx}:v=1:a=0[vout]"
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_complex, "-map", "[vout]",
           "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", str(out_path)]
    run(cmd)


def xfade_chain(clips: list[Path], durations: list[float], out_path: Path, transition_s: float) -> None:
    n = len(clips)
    if n == 1:
        run(["ffmpeg", "-y", "-i", str(clips[0]), "-c", "copy", str(out_path)])
        return
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    filters = []
    prev = "0"
    cumulative = durations[0]
    for i in range(1, n):
        offset = cumulative - transition_s
        outlbl = f"x{i}"
        filters.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={transition_s}:offset={offset}[{outlbl}]")
        prev = outlbl
        cumulative += durations[i] - transition_s
    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{prev}]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)]
    run(cmd)


def hard_cut_concat(clips: list[Path], out_path: Path, work_dir: Path) -> None:
    list_path = work_dir / "_concat_list.txt"
    list_path.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_path)])
    list_path.unlink()


# ---------------------------------------------------------------------
# CANONICAL ENHANCED STORY RENDERER -- the one implementation used by
# both render_episode() (below, x N stories) and render_story_standalone()
# (below, x 1 story). Scenes -> xfaded silent clip (net duration == sum
# of real scene durations, tail padded by SCENE_T per internal boundary
# and by the caller's requested `tail_pad` for an upcoming inter-story
# cut, 0.0 when there is none) + captions burned onto that same 0-based
# timeline. Deliberately silent (no audio) -- each caller mixes in this
# story's own narration differently (render_episode places many voice
# files at absolute offsets against one master mix; render_story_standalone
# muxes exactly one), so audio is intentionally left to the caller rather
# than baked in here, which would force a duplicate mixing implementation.
# ---------------------------------------------------------------------
def render_story_enhanced(story_id: int, tail_pad: float, work: Path) -> tuple[Path, float, list[dict]]:
    story_dir = work / f"story_{story_id}"
    story_dir.mkdir(parents=True, exist_ok=True)
    storyboard = json.loads((MEDIA / "storyboard" / str(story_id) / "storyboard.json").read_text())
    scenes = [s for s in storyboard["scenes"] if s["scene_type"] != "source_card"]

    clip_paths, clip_durations, cue_info = [], [], []
    cursor = 0.0
    for i, scene in enumerate(scenes):
        segs = build_segments_for_scene(scene, storyboard, story_dir / f"scene_{i}")
        clip_path = story_dir / f"scene_{i}.mp4"
        is_last = i == len(scenes) - 1
        pad = tail_pad if is_last else SCENE_T
        encode_segments(segs, scene.get("motion"), clip_path, pad=pad)
        dur = scene["duration"] + pad
        clip_paths.append(clip_path)
        clip_durations.append(dur)

        cues = sc._caption_cues(scene)
        style = "Subordinate" if scene["scene_type"] == "statistic" else "Default"
        emphasis = build_emphasis_set(scene, storyboard)
        for cue in cues:
            cue_info.append({
                "text": cue["text"], "start": cursor + cue["start"], "end": cursor + cue["end"],
                "style": style, "emphasis": emphasis,
            })
        cursor += scene["duration"]

    silent_path = story_dir / "silent.mp4"
    xfade_chain(clip_paths, clip_durations, silent_path, transition_s=SCENE_T)

    ass_text = _ass_header()
    for c in cue_info:
        ass_text += caption_dialogue_line(c["text"], c["start"], c["end"], c["style"], c["emphasis"])
    ass_path = story_dir / "story.ass"
    ass_path.write_text(ass_text)

    captioned_path = story_dir / "captioned.mp4"
    run(["ffmpeg", "-y", "-i", str(silent_path), "-vf", f"ass={ass_path}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(captioned_path)])

    net_duration = cursor  # sum of real scene durations, tail_pad cancels via the outer chain's own eat
    return captioned_path, net_duration, cue_info


def make_gap_clip(out_path: Path, duration: float) -> None:
    bg_hex = "0x{:02X}{:02X}{:02X}".format(*sr.BACKGROUND_COLOR)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={bg_hex}:s={OUTPUT_W}x{OUTPUT_H}:r={FPS}:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)])


def make_card_clip(png_path: Path, duration: float, out_path: Path) -> None:
    run(["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-t", str(duration), "-i", str(png_path),
         "-vf", f"scale={OUTPUT_W}:{OUTPUT_H},format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)])


# ---------------------------------------------------------------------
# Canonical entry point -- called identically by build_episode.py (CLI)
# and app.tasks.episode_video.produce_episode_video (Celery/production).
# ---------------------------------------------------------------------
def render_episode(episode_id: int) -> dict:
    t_start = time.time()
    timings: dict[str, float] = {}

    story_ids, episode_date = load_episode_stories(episode_id)
    log(f"episode {episode_id}: {len(story_ids)} primary stories, date {episode_date}")
    work = OUT_DIR / f"ep{episode_id}_work"
    work.mkdir(parents=True, exist_ok=True)

    log("ensuring storyboards for all selected stories (prerequisite pass)")
    stage_start = time.time()
    preflight = ensure_all_storyboards(story_ids)
    timings["storyboard_prerequisite_s"] = round(time.time() - stage_start, 1)
    log(f"storyboards ready: {len(preflight['reused'])} reused, "
        f"{len(preflight['regenerated'])} regenerated ({timings['storyboard_prerequisite_s']:.1f}s)")
    if preflight["regenerated"]:
        log(f"regenerated story IDs: {preflight['regenerated']}")

    stage_start = time.time()
    story_durations, story_clips = [], []
    with SessionLocal() as db:
        for idx, story_id in enumerate(story_ids):
            log(f"story {story_id} ({idx + 1}/{len(story_ids)})")
            # A failure inside here propagates up unchanged (existing
            # all-or-nothing episode-render behavior) -- this story
            # (and every story after it) is simply never reached, so
            # its StoryContent.status stays exactly as ensure_storyboard
            # left it, never marked video_ready. Every story handled in
            # an earlier iteration already had its own commit, so it
            # stays marked regardless of a later story's failure.
            clip, dur = _render_and_mark_story(db, story_id, work)
            story_clips.append(clip)
            story_durations.append(dur)
    timings["story_render_s"] = round(time.time() - stage_start, 1)
    log(f"all {len(story_ids)} stories rendered: {timings['story_render_s']:.1f}s")

    stage_start = time.time()
    intro_png = work / "intro.png"
    render_intro(intro_png, episode_date, len(story_ids))
    intro_clip = work / "s_intro.mp4"
    make_card_clip(intro_png, INTRO_DUR, intro_clip)

    gap_clip = work / "s_gap.mp4"
    make_gap_clip(gap_clip, GAP_DUR)

    story_gap_clip = work / "s_story_gap.mp4"
    make_gap_clip(story_gap_clip, STORY_GAP)

    outro_png = work / "outro.png"
    render_outro(outro_png)
    outro_clip = work / "s_outro.mp4"
    make_card_clip(outro_png, 3.0, outro_clip)

    sequence = [intro_clip, gap_clip]
    for i, clip in enumerate(story_clips):
        sequence.append(clip)
        if i != len(story_clips) - 1:
            sequence.append(story_gap_clip)
    sequence.append(outro_clip)

    silent_final = OUT_DIR / f"episode_{episode_id}_pillow_enhanced_silent.mp4"
    hard_cut_concat(sequence, silent_final, work)
    timings["episode_assembly_s"] = round(time.time() - stage_start, 1)
    log(f"video assembly done: {timings['episode_assembly_s']:.1f}s")

    # --- audio: per-story processed voice tracks placed at their real
    #     absolute offsets, one continuous ducked music bed, SFX at each
    #     story boundary + episode intro. ---
    stage_start = time.time()
    log("audio mix")

    voice_offsets_ms = []
    cursor = INTRO_DUR + GAP_DUR
    processed_voice_paths = []
    for i, story_id in enumerate(story_ids):
        processed = work / f"voice_{story_id}.wav"
        process_story_voice(story_id, processed)
        processed_voice_paths.append(processed)
        voice_offsets_ms.append(round(cursor * 1000))
        cursor += story_durations[i]
        if i != len(story_ids) - 1:
            cursor += STORY_GAP

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(silent_final)],
        check=True, capture_output=True, text=True,
    )
    total_duration = float(probe.stdout.strip())

    filt, voice_labels = [], []
    for i, p in enumerate(processed_voice_paths):
        filt.append(f"[{i}:a]adelay={voice_offsets_ms[i]}:all=1,apad=whole_dur={total_duration}[v{i}]")
        voice_labels.append(f"[v{i}]")
    n_voices = len(processed_voice_paths)
    filt.append("".join(voice_labels) + f"amix=inputs={n_voices}:duration=longest:normalize=0[voices]")
    # [voices] is consumed by TWO downstream filters (the sidechain trigger
    # and the final mix) -- an explicit asplit is required to fan the label
    # out safely; referencing it twice directly caused the second consumer
    # to silently degrade over a long stream (root cause of a real
    # missing-narration bug found and fixed on Episode 3).
    filt.append("[voices]asplit=2[voices_duck][voices_mix]")

    import numpy as np
    SR = 44100

    def envelope(n, a, r):
        env = np.ones(n)
        na, nr = int(a * SR), int(r * SR)
        if na:
            env[:na] = np.linspace(0, 1, na)
        if nr:
            env[-nr:] = np.linspace(1, 0, nr)
        return env

    def tone(freq, dur, amp=0.15, a=0.01, r=0.08):
        n = int(dur * SR)
        t = np.arange(n) / SR
        return np.sin(2 * np.pi * freq * t) * envelope(n, a, r) * amp

    def pad(dur, notes, amp=0.045):
        n = int(dur * SR)
        t = np.arange(n) / SR
        out = np.zeros(n)
        for f in notes:
            out += np.sin(2 * np.pi * f * t) * 0.5 + np.sin(2 * np.pi * f * 1.003 * t) * 0.5
        out /= len(notes)
        lfo = 0.75 + 0.25 * np.sin(2 * np.pi * 0.08 * t)
        return out * lfo * envelope(n, 2.0, 2.0) * amp

    def write_wav(path, mono):
        stereo = np.stack([mono, mono], axis=1)
        pcm = (np.clip(stereo, -1, 1) * 32767).astype(np.int16)
        import wave
        with wave.open(str(path), "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(SR)
            f.writeframes(pcm.tobytes())

    write_wav(work / "music_bed.wav", pad(total_duration, [110.0, 164.81, 220.0, 277.18, 329.63]))
    write_wav(work / "sfx_intro.wav", np.concatenate([tone(523.25, 0.22), np.zeros(int(0.05 * SR)), tone(783.99, 0.30)]))
    write_wav(work / "sfx_story_gap.wav", tone(660.0, 0.18, amp=0.12, a=0.01, r=0.10))

    run(["ffmpeg", "-y", "-i", str(work / "music_bed.wav"), "-af", f"apad=whole_dur={total_duration}",
         "-t", str(total_duration), "-ar", "44100", "-ac", "2", str(work / "music_full.wav")])

    music_input_idx = n_voices
    filt.append(
        f"[{music_input_idx}:a][voices_duck]sidechaincompress=threshold=0.02:ratio=8:attack=25:release=400:makeup=1[ducked]"
    )

    gap_starts_ms = []
    c = INTRO_DUR + GAP_DUR
    for i in range(len(story_ids) - 1):
        c += story_durations[i]
        gap_starts_ms.append(round(c * 1000))
        c += STORY_GAP

    sfx_inputs = ["-i", str(work / "sfx_intro.wav")]
    sfx_labels = []
    filt.append(f"[{n_voices + 1}:a]adelay=0:all=1[sfx0]")
    sfx_labels.append("[sfx0]")
    for j, ms in enumerate(gap_starts_ms):
        sfx_inputs += ["-i", str(work / "sfx_story_gap.wav")]
        filt.append(f"[{n_voices + 2 + j}:a]adelay={ms}:all=1[sfxg{j}]")
        sfx_labels.append(f"[sfxg{j}]")

    filt.append("".join(sfx_labels) + f"amix=inputs={len(sfx_labels)}:duration=longest:normalize=0[sfxmix]")
    filt.append("[ducked][sfxmix][voices_mix]amix=inputs=3:duration=longest:normalize=0[m]")
    filt.append("[m]alimiter=limit=0.97:attack=5:release=50,loudnorm=I=-14:TP=-1.0:LRA=8,"
                f"apad=whole_dur={total_duration}[final]")

    cmd = ["ffmpeg", "-y"]
    for p in processed_voice_paths:
        cmd += ["-i", str(p)]
    cmd += ["-i", str(work / "music_full.wav")]
    cmd += sfx_inputs
    cmd += ["-filter_complex", ";".join(filt), "-map", "[final]", "-t", str(total_duration),
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(work / "final_mix.wav")]
    run(cmd)
    timings["audio_mix_s"] = round(time.time() - stage_start, 1)
    log(f"audio mix time: {timings['audio_mix_s']:.1f}s")

    stage_start = time.time()
    log("final mux")
    final_out = OUT_DIR / f"episode_{episode_id}_pillow_enhanced.mp4"
    # Re-encode the video here rather than "-c:v copy": `silent_final` is
    # a patchwork of many separately-encoded clips stream-copy-concatenated
    # by hard_cut_concat (intro/gap/story/gap/.../outro), and copying THAT
    # straight through leaves subtle timestamp irregularities that ffprobe,
    # curl and VLC don't mind but Chrome's stricter demuxer does -- found
    # live on Episode 3: every backend/ffprobe/volumedetect check passed,
    # the file even played fine via curl range-requests, but the <video>
    # element (and a raw Chrome tab navigation to the same URL) stalled at
    # readyState 0 forever. A single clean re-encode produces consistent
    # timestamps end to end and resolved it completely, with no change to
    # picture/audio content.
    run(["ffmpeg", "-y", "-fflags", "+genpts", "-i", str(silent_final), "-i", str(work / "final_mix.wav"),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart",
         "-t", str(total_duration), "-shortest", str(final_out)])
    timings["final_mux_s"] = round(time.time() - stage_start, 1)

    timings["total_s"] = round(time.time() - t_start, 1)
    log(f"TOTAL BUILD TIME: {timings['total_s']:.1f}s")

    return {
        "episode_id": episode_id,
        "status": "ready",
        # Relative to APP_ROOT (e.g. "media/pillow_enhanced/episode_3_
        # pillow_enhanced.mp4"), matching every other stored video_path
        # in this codebase (app/main.py's media_url()/video_url building
        # a URL as f"/{path}" against the /media StaticFiles mount) --
        # an absolute path here silently breaks the dashboard's video
        # player (found live: episode 3 "produced" successfully by every
        # backend measure, but showed no video in the browser, because
        # the stored absolute path turned into an invalid "//app/media/
        # ..." URL).
        "video_path": final_out.relative_to(APP_ROOT).as_posix(),
        "stories_total": len(story_ids),
        "stories_reused": len(preflight["reused"]),
        "stories_regenerated": len(preflight["regenerated"]),
        "regenerated_story_ids": preflight["regenerated"],
        "duration_seconds": total_duration,
        "timings": timings,
    }


# ---------------------------------------------------------------------
# Standalone single-story entry point -- called identically by
# app.tasks.storyboard_prototype's DEV Celery task (the sole caller of
# POST /api/v1/dev/storyboard-prototype/{story_id}). Same
# render_story_enhanced() core as render_episode() above; the only
# difference is muxing exactly one story's own processed narration
# instead of placing many voice files into a master episode mix, since
# there is no episode here for a shared mix to belong to.
# ---------------------------------------------------------------------
def render_story_standalone(story_id: int) -> dict:
    """
    ensure content/audio/captions -> ensure storyboard -> the SAME
    enhanced Pillow story renderer episodes use -> mux this story's own
    processed narration on top -> one standalone enhanced story MP4.

    Delegates the entire content+storyboard prerequisite to
    storyboard_service.ensure_storyboard (reuse if valid/current,
    regenerate + QA if missing/stale) -- identical to what
    ensure_all_storyboards() does per-story for episode production, just
    for a single story with no episode involved. A failure there is
    returned as-is (status: "failed", stage, error) without attempting
    any rendering; "qa_failed" still means a usable storyboard exists
    (same "not fatal" rule ensure_all_storyboards applies), so rendering
    proceeds.

    The base {story_id}_storyboard.mp4 produced by ensure_storyboard
    remains on disk as the storyboard-QA pipeline's own intermediate
    artifact -- it is never the value returned as "video_path" here.
    """
    t_start = time.time()

    with SessionLocal() as db:
        storyboard_result = ensure_storyboard(db, story_id)

    if storyboard_result.get("status") == "failed":
        return storyboard_result

    storyboard_path = MEDIA / "storyboard" / str(story_id) / "storyboard.json"
    storyboard = json.loads(storyboard_path.read_text())

    work = OUT_DIR / f"story_{story_id}_standalone_work"
    work.mkdir(parents=True, exist_ok=True)

    log(f"story {story_id}: rendering standalone enhanced video")
    captioned_path, _net_duration, _cue_info = render_story_enhanced(story_id, tail_pad=0.0, work=work)

    processed_voice = work / "voice.wav"
    process_story_voice(story_id, processed_voice)

    # Same "measure the real encoded video duration, then pad/trim audio
    # and the final mux to that exact figure" pattern render_episode uses
    # for its own master mix -- `-shortest` alone is never trusted (CLAUDE.md
    # hard rule #4: ffmpeg's `-shortest` overran by ~2s per clip here).
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(captioned_path)],
        check=True, capture_output=True, text=True,
    )
    video_duration = float(probe.stdout.strip())

    padded_voice = work / "voice_padded.wav"
    run(["ffmpeg", "-y", "-i", str(processed_voice), "-af", f"apad=whole_dur={video_duration}",
         "-t", str(video_duration), "-ar", "44100", "-ac", "2", str(padded_voice)])

    final_out = MEDIA / "pillow_enhanced" / f"{story_id}_pillow_enhanced.mp4"
    final_out.parent.mkdir(parents=True, exist_ok=True)
    # Re-encode rather than "-c:v copy", same rationale as render_episode's
    # own final mux (see its comment above): this file is muxed straight
    # from the standalone renderer's own timestamps, but re-encoding here
    # keeps the two code paths' final-mux behavior identical rather than
    # one being copy-based and the other not for no real reason.
    run(["ffmpeg", "-y", "-fflags", "+genpts", "-i", str(captioned_path), "-i", str(padded_voice),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart",
         "-t", str(video_duration), "-shortest", str(final_out)])

    render_time_s = round(time.time() - t_start, 1)
    log(f"story {story_id}: standalone enhanced render done in {render_time_s:.1f}s")

    result = {
        "story_id": story_id,
        "status": storyboard_result.get("status", "ready"),
        "reused": storyboard_result.get("reused"),
        "video_path": final_out.relative_to(APP_ROOT).as_posix(),
        "storyboard_path": storyboard_path.relative_to(APP_ROOT).as_posix(),
        "scene_count": len(storyboard["scenes"]),
        "scene_types": [scene["scene_type"] for scene in storyboard["scenes"]],
        "qa": storyboard_result.get("qa", []),
        "duration_seconds": video_duration,
        "render_time_s": render_time_s,
    }
    print(f"[episode-renderer] standalone story result: {result}")
    return result
