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
transition within a story, a 0.6s left-to-right sweep transition (+ SFX tick) at every boundary
(intro, between stories, outro), one light-surface intro, one final episode outro, and a processed voice +
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
import hashlib
import html
import json
import os
import subprocess
import sys
import time
from datetime import timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
os.chdir(APP_ROOT)  # every reused function's relative MEDIA_ROOT = Path("media") depends on this

from PIL import Image, ImageDraw, ImageFont

from app.content import brand_assets, support_facts, visual_assets
from app.content import scene_renderer as sr
from app.content import storyboard_composer as sc
from app.content.storyboard_service import ensure_storyboard, mark_storyboard_current
from app.dates import today_ist
from app.content.video_composer import get_audio_duration_seconds
from app.content.voice_generator import VOICE_NAME, synthesize_voice
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem
from app.tasks.content import get_or_create_content

MEDIA = APP_ROOT / "media"
OUT_DIR = MEDIA / "pillow_enhanced"

FPS = 25
OUTPUT_W, OUTPUT_H = 1920, 1080

SCENE_T = 8 / FPS      # 0.32s -- within-story scene-to-scene transition
STORY_GAP = 0.6        # between-story sweep transition slot (+ SFX tick), within spec's 0.5-1.5s
# Every boundary (intro->story 1, story->story, last story->outro) is one
# TRANSITION_DUR sweep clip in its own slot between the neighbouring clips.
STORY_TRANSITION = "smoothright"   # xfade style: soft left-to-right sweep ("radial" = rotating wipe)
# Spoken, captioned bumpers. INTRO_DUR/OUTRO_DUR are fixed so the dashboard's
# jump-to-story offset (INTRO_LEAD_IN_SECONDS in app.js = INTRO_DUR + GAP_DUR)
# never needs a per-episode probe; ensure_bumper_voice() refuses a voice that
# would not fit.
INTRO_DUR, GAP_DUR, OUTRO_DUR = 7.5, 0.6, 6.5
OUTRO_GAP = 0.6        # last story -> outro transition slot
BUMPER_TAIL = 0.3      # minimum silence left after the last word
BUMPERS = {
    "intro": {
        "spoken": "Good morning! Welcome to Five square Feed. Today's Top Tech Headlines.",
        "captions": ["Good morning!", "Welcome to 5squareFeed.", "Today's Top Tech Headlines."],
        "lead": 0.5, "duration": INTRO_DUR,
    },
    "outro": {
        "spoken": "Thanks for watching. See you tomorrow on Five square Feed.",
        "captions": ["Thanks for watching.", "See you tomorrow on 5squareFeed."],
        "lead": 1.0, "duration": OUTRO_DUR,
    },
}
BUMPER_DIR = MEDIA / "audio" / "bumpers"

# Template output geometry of the visual window (sr.WINDOW scaled 2560x1440 -> 1920x1080).
_K = OUTPUT_W / sr.SCENE_WIDTH
WIN_X, WIN_Y = round(sr.WINDOW[0] * _K), round(sr.WINDOW[1] * _K)
WIN_W, WIN_H = round(sr.WINDOW[2] * _K), round(sr.WINDOW[3] * _K)

# Per-story narration processing chain -- shared UNCHANGED by both
# render_episode()'s master mix (one voice file per story, each
# processed independently before being placed at its own offset) and
# render_story_standalone()'s single-story mux, via process_story_voice()
# below. Never duplicated as a second copy of this filter string.
VOICE_CHAIN = ("highpass=f=80,equalizer=f=3000:t=q:w=1:g=2,"
               "acompressor=threshold=-18dB:ratio=2.5:attack=8:release=180:makeup=2,"
               "loudnorm=I=-16:TP=-1.5:LRA=7")


def process_voice_file(raw: Path, out_path: Path) -> None:
    run(["ffmpeg", "-y", "-i", str(raw), "-af", VOICE_CHAIN, "-ar", "44100", "-ac", "2", str(out_path)])


def process_story_voice(story_id: int, out_path: Path) -> None:
    """Apply the one shared narration processing chain to a single
    story's raw edge-tts mp3. Same ffmpeg call used per-story inside
    render_episode()'s master mix and by render_story_standalone()."""
    process_voice_file(MEDIA / "audio" / f"{story_id}.mp3", out_path)


def ensure_bumper_voice(name: str) -> tuple[Path, list[dict]]:
    """(raw mp3, caption cues in clip time) for the intro/outro greeting.
    Synthesised once per distinct text+voice and cached, so every later render
    is identical and offline. Cue times include the bumper's lead-in."""
    spec = BUMPERS[name]
    key = hashlib.sha1(f"{VOICE_NAME}|{spec['spoken']}".encode()).hexdigest()[:10]
    mp3, meta = BUMPER_DIR / f"{name}_{key}.mp3", BUMPER_DIR / f"{name}_{key}.json"
    if not (mp3.exists() and meta.exists()):
        BUMPER_DIR.mkdir(parents=True, exist_ok=True)
        segments = synthesize_voice(spec["spoken"], mp3)
        meta.write_text(json.dumps({"segments": segments, "duration": get_audio_duration_seconds(mp3)}))
    data = json.loads(meta.read_text())
    if spec["lead"] + data["duration"] + BUMPER_TAIL > spec["duration"]:
        raise ValueError(f"{name} greeting ({data['duration']:.2f}s) does not fit its {spec['duration']}s card")
    segments = data["segments"]
    texts = spec["captions"] if len(segments) == len(spec["captions"]) else [s["text"] for s in segments]
    cues = [{"text": t, "start": spec["lead"] + s["start"], "end": spec["lead"] + s["end"]}
            for t, s in zip(texts, segments)]
    return mp3, cues


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
    # The status write above bumped updated_at; keep the (still valid)
    # storyboard from looking stale on the next Produce.
    mark_storyboard_current(story_id)


def _render_and_mark_story(db, story_id: int, work: Path, position: tuple[int, int] | None = None) -> tuple[Path, float]:
    """One iteration of render_episode()'s story loop below, pulled out
    so the "render, THEN mark video_ready -- never the reverse, never
    on a raised exception" ordering is directly unit-testable without
    exercising the rest of render_episode() (intro/outro/audio mix/final
    mux)."""
    clip, dur, _ = render_story_enhanced(story_id, tail_pad=0.0, work=work, position=position)
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
    order, never re-ranked or filtered further here.

    The returned date string is the IST day the episode was made
    (Episode.created_at), not the coverage day: Episode.episode_date stays
    the coverage key that selection and its uniqueness constraint use."""
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
        made_at = episode.created_at
        if made_at.tzinfo is None:
            made_at = made_at.replace(tzinfo=timezone.utc)
        episode_date = today_ist(made_at).strftime("%B %d, %Y")
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
INTRO_DATE_FONT = 44
INTRO_DATE_GAP = 26


def _render_lockup_card(output_path: Path, date_text: str = "") -> None:
    """Intro/outro art: the finalized primary lockup, undistorted (uniform
    scale only), on the vivid soft ground, raised and slightly reduced so the
    bottom caption box never covers the tagline. `date_text`, when given, is
    drawn bold just below the tagline (intro only)."""
    image = sr.soft_background(vivid=True).convert("RGBA")
    logo = Image.open(brand_assets.get_lockup_path()).convert("RGBA")
    target_h = int(sr.SCENE_HEIGHT * 0.74)
    logo = logo.resize((round(logo.width * target_h / logo.height), target_h), Image.LANCZOS)
    top = int(sr.SCENE_HEIGHT * 0.03)
    image.alpha_composite(logo, ((sr.SCENE_WIDTH - logo.width) // 2, top))
    image = image.convert("RGB")
    if date_text:
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(sr.FONT_BOLD, INTRO_DATE_FONT)
        text = date_text.upper()
        box = draw.textbbox((0, 0), text, font=font)
        x = (sr.SCENE_WIDTH - (box[2] - box[0])) // 2 - box[0]
        y = top + logo.height + INTRO_DATE_GAP - box[1]
        draw.text((x, y), text, font=font, fill=sr.NAVY)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG")


def render_intro(output_path: Path, episode_date: str = "") -> None:
    _render_lockup_card(output_path, episode_date)


def render_outro(output_path: Path) -> None:
    _render_lockup_card(output_path)


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


# Caption typography -- one fixed system for every caption in every story.
# Sizes are libass px at 1920x1080; libass renders DejaVu ~0.86x the PIL pixel
# size for the same number, so wrapping measures at the calibrated PIL size.
CAPTION_FONT = 36
CAPTION_SUB_FONT = 30          # statistic scenes: the big number is primary
CAPTION_MAX_LINES = 2
CAPTION_MAX_WIDTH = 1400       # PIL px per line at the calibrated size
CAPTION_MARGIN_V = 110
_LIBASS_TO_PIL = 0.86
_CAPTION_BOX = "{:02X}{:02X}{:02X}".format(*reversed(sr.NAVY))     # BGR
_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _ass_box(alpha: int) -> str:
    return f"&H{alpha:02X}{_CAPTION_BOX}&"


def _ass_header() -> str:
    text_main = ass_color((246, 248, 252), alpha=0x14)
    text_sub = ass_color((246, 248, 252), alpha=0x38)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUTPUT_W}
PlayResY: {OUTPUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{CAPTION_FONT},{text_main},{text_main},&HFF000000&,&HFF000000&,0,0,0,0,100,100,0,0,1,0,0,2,240,240,{CAPTION_MARGIN_V},1
Style: Subordinate,DejaVu Sans,{CAPTION_SUB_FONT},{text_sub},{text_sub},&HFF000000&,&HFF000000&,0,0,0,0,100,100,0,0,1,0,0,2,240,240,{CAPTION_MARGIN_V},1
Style: BoxDefault,DejaVu Sans,20,{_ass_box(0x52)},{_ass_box(0x52)},&HFF000000&,&HFF000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: BoxSubordinate,DejaVu Sans,20,{_ass_box(0x78)},{_ass_box(0x78)},&HFF000000&,&HFF000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def caption_lines(text: str, style: str) -> list[str]:
    size = CAPTION_SUB_FONT if style == "Subordinate" else CAPTION_FONT
    font = ImageFont.truetype(sr.FONT_REGULAR, round(size * _LIBASS_TO_PIL))
    return sr._wrap_text(_MEASURE, text, font, CAPTION_MAX_WIDTH)


def split_caption_cue(cue: dict, style: str) -> list[dict]:
    """One narration cue -> display cues of at most CAPTION_MAX_LINES lines.
    Every word is kept in order; a cue that needs several pages shares its own
    real time span between them in proportion to their length."""
    text = html.unescape(cue["text"])
    lines = caption_lines(text, style)
    pages = [lines[i:i + CAPTION_MAX_LINES] for i in range(0, len(lines), CAPTION_MAX_LINES)] or [[text]]
    weights = [sum(len(line) for line in page) for page in pages]
    span, cursor, out = cue["end"] - cue["start"], cue["start"], []
    for page, weight in zip(pages, weights):
        end = cursor + span * weight / sum(weights)
        out.append({"lines": page, "start": cursor, "end": end, "style": style})
        cursor = end
    out[-1]["end"] = cue["end"]
    return out


CAPTION_PAD_X, CAPTION_PAD_Y, CAPTION_RADIUS = 24, 9, 12


def _caption_box_path(lines: list[str], style: str) -> tuple[float, float, str]:
    """(x, y, ASS drawing) of ONE rounded box behind the whole cue, sized from
    the same PIL measurement that decided the line breaks (libass line height
    equals the font size)."""
    size = CAPTION_SUB_FONT if style == "Subordinate" else CAPTION_FONT
    font = ImageFont.truetype(sr.FONT_REGULAR, size * _LIBASS_TO_PIL)
    width = max(_MEASURE.textlength(line, font=font) for line in lines) * 1.01 + 2 * CAPTION_PAD_X
    height = size * len(lines) + 2 * CAPTION_PAD_Y
    x, y = (OUTPUT_W - width) / 2, OUTPUT_H - CAPTION_MARGIN_V - size * len(lines) - CAPTION_PAD_Y
    w, h, r = round(width), round(height), CAPTION_RADIUS
    path = (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")
    return x, y, path


CAPTION_JOIN_GAP = 0.15


def caption_dialogue_line(lines: list[str], start: float, end: float, style: str,
                          fade_in: bool = True, fade_out: bool = True) -> str:
    """Two ASS events per display cue: one translucent rounded box behind the
    whole cue (a per-line libass box double-darkens where lines overlap) and
    the plain single-weight text above it. Both fade only at the outer edges
    of a run of adjacent cues, so a cue-to-cue change is a clean swap."""
    x, y, path = _caption_box_path(lines, style)
    stamp = f"{ass_time(start)},{ass_time(end)}"
    fad = f"\\fad({80 if fade_in else 0},{80 if fade_out else 0})"
    box = f"Dialogue: 0,{stamp},Box{style},,0,0,0,,{{{fad}\\pos({x:.1f},{y:.1f})\\p1}}{path}{{\\p0}}\n"
    text = "\\N".join(lines)
    return box + f"Dialogue: 1,{stamp},{style},,0,0,0,,{{{fad}}}{text}\n"


def caption_events(cues: list[dict]) -> str:
    """ASS events for time-ordered display cues: no two cues overlap and a cue
    that follows another within CAPTION_JOIN_GAP swaps in without fading."""
    out, prev_end = "", None
    for i, c in enumerate(cues):
        start = c["start"] if prev_end is None else max(c["start"], prev_end)
        if start >= c["end"]:
            continue
        joined_in = prev_end is not None and start - prev_end < CAPTION_JOIN_GAP
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        joined_out = nxt is not None and nxt - c["end"] < CAPTION_JOIN_GAP
        out += caption_dialogue_line(c["lines"], start, c["end"], c["style"], not joined_in, not joined_out)
        prev_end = c["end"]
    return out


# ---------------------------------------------------------------------
# Per-scene render + encode (reuses real production renderers/motion)
# ---------------------------------------------------------------------
def build_segments_for_scene(scene: dict, storyboard: dict, work_dir: Path) -> list[dict]:
    """Visual-window frames (2400x752 PNGs, window_only) for a scene whose
    window content is animated data (statistic count-up, comparison, stage
    pills). The card chrome and text are composited later, in one pass."""
    work_dir.mkdir(parents=True, exist_ok=True)
    with sr.window_only():
        if scene["scene_type"] in sr._COUNTUP_CAPABLE_TYPES:
            return sr.render_scene_frame_sequence(scene, storyboard, work_dir, fps=FPS)
        png = work_dir / "frame.png"
        sr.render_scene_image(scene, storyboard, png)
    return [{"duration": scene["duration"], "frame_paths": [png], "is_ramp": False}]


def encode_segments(segments: list[dict], out_path: Path, pad: float = 0.0, size: tuple = (WIN_W, WIN_H)) -> None:
    """Window-sized intermediate clip for a multi-frame scene (count-up ramps).
    `pad` extends the encoded duration beyond the scene's real duration --
    required so the outer crossfade's offset math (which assumes the padded
    length) matches what actually got encoded. Explicit -t per CLAUDE.md #4."""
    segments = [dict(s) for s in segments]
    if pad > 0.001 and segments:
        last = segments[-1]
        if last["is_ramp"] and len(last["frame_paths"]) > 1:
            segments.append({"duration": pad, "frame_paths": [last["frame_paths"][-1]], "is_ramp": False})
        else:
            segments[-1] = dict(last)
            segments[-1]["duration"] = last["duration"] + pad
    inputs, filters, idx, total = [], [], 0, 0.0
    for seg in segments:
        dur = seg["duration"]
        if dur <= 0.02:
            continue
        if seg["is_ramp"] and len(seg["frame_paths"]) > 1:
            seq_dir = seg["frame_paths"][0].parent
            inputs += ["-framerate", str(FPS), "-i", str(seq_dir / "frame_%05d.png")]
        else:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(dur), "-i", str(seg["frame_paths"][0])]
        filters.append(f"[{idx}:v]scale={size[0]}:{size[1]}:flags=lanczos,fps={FPS},setsar=1[v{idx}]")
        total += dur
        idx += 1
    concat_in = "".join(f"[v{i}]" for i in range(idx))
    filter_complex = ";".join(filters) + f";{concat_in}concat=n={idx}:v=1:a=0[vout]"
    run(["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_complex, "-map", "[vout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", "-t", f"{total:.3f}", str(out_path)])


# Headline/support text is composited above the crossfaded imagery. When a
# scene change swaps the text, the outgoing text fades out and the incoming
# text fades in one after the other (never overlapping), inside the 0.32s
# crossfade -- so no headline ever ghosts through another.
TEXT_FADE = 0.12
TEXT_LEAD = 0.04


def text_runs(keys: list, starts: list[float], ends: list[float]) -> list[dict]:
    """Consecutive scenes with identical text share one run (no flicker)."""
    runs: list[dict] = []
    for i, key in enumerate(keys):
        if runs and runs[-1]["key"] == key:
            runs[-1]["end"] = ends[i]
        else:
            runs.append({"key": key, "first": i, "start": starts[i], "end": ends[i]})
    return runs


def still(duration: float) -> str:
    """Filter-graph replacement for `-loop 1 -t D -i image`. A looped image input
    gets its own decode thread that races ahead of the encoder and buffers
    frames without bound (a 140s story exhausted 8 GB of RAM); a one-frame input
    repeated by `loop` is pulled on demand, so memory stays flat at any length."""
    return f"loop=loop=-1:size=1,setpts=N/({FPS}*TB),trim=duration={duration:.3f},setpts=PTS-STARTPTS"


def text_layer_filter(src: int, label: str, run: dict, is_first: bool, is_last: bool,
                      hold: float | None = None) -> tuple[str, str]:
    """(filter for the text input, overlay enable expression). `hold` = how long
    the single-frame text image is held (the story length)."""
    chain = [still(hold)] if hold is not None else []
    chain.append("format=rgba")
    if not is_first:
        chain.append(f"fade=t=in:st={run['start'] + TEXT_LEAD + TEXT_FADE:.3f}:d={TEXT_FADE}:alpha=1")
    if not is_last:
        chain.append(f"fade=t=out:st={run['end'] + TEXT_LEAD:.3f}:d={TEXT_FADE}:alpha=1")
    lo = 0.0 if is_first else run["start"] + TEXT_LEAD + TEXT_FADE - 0.02
    hi = 1e6 if is_last else run["end"] + TEXT_LEAD + TEXT_FADE + 0.02
    return f"[{src}:v]{','.join(chain)}[{label}]", f"between(t,{lo:.3f},{hi:.3f})"


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
def render_story_enhanced(story_id: int, tail_pad: float, work: Path,
                          position: tuple[int, int] | None = None) -> tuple[Path, float, list[dict]]:
    """`position` = (rank, total) inside an episode, drawn as the card's
    "NN / TT" counter; None (standalone render) omits the counter rather
    than showing a fabricated one.

    Per story: one card (chrome + headline/support plates) held constant, a
    crossfaded visual-window stream underneath it, headline/support text and
    ASS captions above it -- all composited and encoded in a single ffmpeg pass."""
    story_dir = work / f"story_{story_id}"
    story_dir.mkdir(parents=True, exist_ok=True)
    storyboard = json.loads((MEDIA / "storyboard" / str(story_id) / "storyboard.json").read_text())
    storyboard["_episode_position"] = position
    storyboard["_support"] = support_facts.load_support_info(story_id)
    visual_assets.ensure_story_visual(story_id)  # cache hit after the episode's asset pre-pass
    scenes = [s for s in storyboard["scenes"] if s["scene_type"] != "source_card"]

    inputs: list[str] = []
    n_inputs = 0

    def add_input(*args: str) -> int:
        nonlocal n_inputs
        inputs.extend(args)
        n_inputs += 1
        return n_inputs - 1

    total = sum(s["duration"] for s in scenes) + tail_pad
    filters: list[str] = []
    cue_info, scene_meta, cursors, ends, head_keys, supp_keys, parts_by_scene = [], [], [], [], [], [], []
    cursor, window_png, window_kind = 0.0, None, None
    base_path = story_dir / "base.png"
    for i, scene in enumerate(scenes):
        pad = tail_pad if i == len(scenes) - 1 else SCENE_T
        d = scene["duration"] + pad
        scene_dir = story_dir / f"scene_{i}"
        scene_dir.mkdir(parents=True, exist_ok=True)

        if sr.uses_story_visual(scene):
            if window_png is None:
                window, window_kind = sr.story_visual_window(storyboard)
                window_png = story_dir / "window.png"
                window.save(window_png, "PNG")
            src = add_input("-i", str(window_png))
            filters.append(f"[{src}:v]{still(d)},scale={WIN_W}:{WIN_H}:flags=lanczos,fps={FPS},setsar=1,format=yuv420p[w{i}]")
            motion = "static"
            visual = window_kind
        else:
            segs = build_segments_for_scene(scene, storyboard, scene_dir)
            if len(segs) == 1 and not segs[0]["is_ramp"]:
                src = add_input("-i", str(segs[0]["frame_paths"][0]))
                filters.append(f"[{src}:v]{still(d)},scale={WIN_W}:{WIN_H}:flags=lanczos,fps={FPS},setsar=1,format=yuv420p[w{i}]")
                motion = "static"
            else:
                clip = scene_dir / "window.mp4"
                encode_segments(segs, clip, pad=pad)
                src = add_input("-i", str(clip))
                filters.append(f"[{src}:v]fps={FPS},setsar=1,format=yuv420p[w{i}]")
                motion = "countup"
            visual = "data"
        scene_meta.append({"index": i, "scene_type": scene["scene_type"], "visual": visual, "motion": motion, "duration": scene["duration"]})

        parts = sr.render_card_parts(scene, storyboard, scene_dir, _K, base_path)
        parts_by_scene.append(parts)
        head_keys.append(parts["key"][0])
        supp_keys.append(parts["key"][1:])

        style = "Subordinate" if scene["scene_type"] == "statistic" else "Default"
        for cue in sc._caption_cues(scene):
            for shown in split_caption_cue({"text": cue["text"], "start": cursor + cue["start"], "end": cursor + cue["end"]}, style):
                cue_info.append(shown)
        cursors.append(cursor)
        cursor += scene["duration"]
        ends.append(cursor)
    ends[-1] = total

    # imagery: crossfaded window stream
    prev = "w0"
    for i in range(1, len(scenes)):
        filters.append(f"[{prev}][w{i}]xfade=transition=fade:duration={SCENE_T}:offset={cursors[i]:.3f}[x{i}]")
        prev = f"x{i}"
    filters.append(f"[{prev}]pad={OUTPUT_W}:{OUTPUT_H}:{WIN_X}:{WIN_Y}:color=white,format=yuv420p[stream]")

    # constant card (chrome + plates) above the imagery
    base_in = add_input("-i", str(base_path))
    filters.append(f"[{base_in}:v]{still(total)},format=rgba[base]")
    filters.append("[stream][base]overlay=0:0:format=auto[card]")

    # headline + support text, faded so consecutive texts never overlap
    current = "card"
    for layer, keys in (("headline", head_keys), ("support", supp_keys)):
        runs = text_runs(keys, cursors, ends)
        for r, run_ in enumerate(runs):
            png = parts_by_scene[run_["first"]][layer]
            x, y = parts_by_scene[run_["first"]][f"{layer}_xy"]
            src = add_input("-i", str(png))
            label = f"t_{layer}{r}"
            text_filter, enable = text_layer_filter(src, label, run_, r == 0, r == len(runs) - 1, hold=total)
            filters.append(text_filter)
            filters.append(f"[{current}][{label}]overlay={x}:{y}:enable='{enable}':format=auto[o_{layer}{r}]")
            current = f"o_{layer}{r}"

    ass_text = _ass_header()
    ass_text += caption_events(cue_info)
    ass_path = story_dir / "story.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    filters.append(f"[{current}]ass={ass_path},format=yuv420p[vout]")

    captioned_path = story_dir / "captioned.mp4"
    run(["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(filters), "-map", "[vout]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{total:.3f}", str(captioned_path)])

    (story_dir / "render_meta.json").write_text(json.dumps({
        "story_id": story_id, "total": total, "scenes": scene_meta,
        "caption_max_lines": max((len(c["lines"]) for c in cue_info), default=0),
        "headline_runs": len(text_runs(head_keys, cursors, ends)),
        "headlines": list(head_keys),
    }, indent=1))
    return captioned_path, cursor, cue_info


def make_transition_clip(prev_clip: Path, next_clip: Path, out_path: Path, duration: float) -> None:
    """Sweep from the outgoing clip's last frame to the incoming clip's first
    frame, inside its own `duration` slot (so neighbours and the timeline are
    untouched). Stills are held in the filter graph, never looped inputs (rule 12)."""
    last_png, first_png = out_path.with_name(out_path.stem + "_a.png"), out_path.with_name(out_path.stem + "_b.png")
    run(["ffmpeg", "-y", "-sseof", "-0.2", "-i", str(prev_clip), "-update", "1", "-q:v", "1", str(last_png)])
    run(["ffmpeg", "-y", "-i", str(next_clip), "-frames:v", "1", str(first_png)])
    fmt = f"scale={OUTPUT_W}:{OUTPUT_H}:flags=lanczos,fps={FPS},setsar=1,format=yuv420p"
    run(["ffmpeg", "-y", "-i", str(last_png), "-i", str(first_png), "-filter_complex",
         f"[0:v]{still(duration)},{fmt}[a];[1:v]{still(duration)},{fmt}[b];"
         f"[a][b]xfade=transition={STORY_TRANSITION}:duration={duration}:offset=0,format=yuv420p[v]",
         "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-frames:v", str(round(duration * FPS)), str(out_path)])


def make_bumper_clip(png_path: Path, duration: float, cues: list[dict], out_path: Path) -> None:
    """Intro/outro card with burned captions (same ASS look as story captions).
    The still is held in the filter graph, never a looped input (rule 12)."""
    pages = [p for cue in cues for p in split_caption_cue(cue, "Default")]
    ass_path = out_path.with_suffix(".ass")
    ass_path.write_text(_ass_header() + caption_events(pages), encoding="utf-8")
    run(["ffmpeg", "-y", "-i", str(png_path), "-filter_complex",
         f"[0:v]{still(duration)},scale={OUTPUT_W}:{OUTPUT_H}:flags=lanczos,fps={FPS},ass={ass_path},format=yuv420p[v]",
         "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{duration:.3f}",
         str(out_path)])


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

    log("sourcing real visuals for all stories (cached results reused, never refetched)")
    stage_start = time.time()
    visual_summary = []
    for story_id in story_ids:
        prov = visual_assets.ensure_story_visual(story_id)
        visual_summary.append({"story_id": story_id, "kind": prov.get("kind"), "cached": bool(prov.get("cached"))})
    timings["asset_retrieval_s"] = round(time.time() - stage_start, 1)
    log(f"visuals ready: { {k: sum(1 for v in visual_summary if v['kind'] == k) for k in ('photo', 'logo', 'fallback')} } "
        f"({timings['asset_retrieval_s']:.1f}s)")

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
            clip, dur = _render_and_mark_story(db, story_id, work, position=(idx + 1, len(story_ids)))
            story_clips.append(clip)
            story_durations.append(dur)
    timings["story_render_s"] = round(time.time() - stage_start, 1)
    log(f"all {len(story_ids)} stories rendered: {timings['story_render_s']:.1f}s")

    stage_start = time.time()
    intro_png = work / "intro.png"
    render_intro(intro_png, episode_date)
    intro_clip = work / "s_intro.mp4"
    intro_mp3, intro_cues = ensure_bumper_voice("intro")
    make_bumper_clip(intro_png, INTRO_DUR, intro_cues, intro_clip)

    outro_png = work / "outro.png"
    render_outro(outro_png)
    outro_clip = work / "s_outro.mp4"
    outro_mp3, outro_cues = ensure_bumper_voice("outro")
    make_bumper_clip(outro_png, OUTRO_DUR, outro_cues, outro_clip)

    boundaries = [intro_clip, *story_clips, outro_clip]
    durations = [GAP_DUR, *([STORY_GAP] * (len(story_clips) - 1)), OUTRO_GAP]
    sequence = [intro_clip]
    for i, dur in enumerate(durations):
        transition = work / f"s_transition_{i}.mp4"
        make_transition_clip(boundaries[i], boundaries[i + 1], transition, dur)
        sequence += [transition, boundaries[i + 1]]

    silent_final = OUT_DIR / f"episode_{episode_id}_pillow_enhanced_silent.mp4"
    hard_cut_concat(sequence, silent_final, work)
    timings["episode_assembly_s"] = round(time.time() - stage_start, 1)
    log(f"video assembly done: {timings['episode_assembly_s']:.1f}s")

    # --- audio: per-story processed voice tracks placed at their real
    #     absolute offsets, one continuous ducked music bed, SFX at each
    #     story boundary + episode intro. ---
    stage_start = time.time()
    log("audio mix")

    intro_voice = work / "voice_intro.wav"
    process_voice_file(intro_mp3, intro_voice)
    voice_offsets_ms = [round(BUMPERS["intro"]["lead"] * 1000)]
    cursor = INTRO_DUR + GAP_DUR
    processed_voice_paths = [intro_voice]
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

    outro_voice = work / "voice_outro.wav"
    process_voice_file(outro_mp3, outro_voice)
    processed_voice_paths.append(outro_voice)
    voice_offsets_ms.append(round((total_duration - OUTRO_DUR + BUMPERS["outro"]["lead"]) * 1000))

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
    # Soft "glass ping" at every sweep boundary (user pick "04" from the sample page;
    # the earlier 660 Hz sine tick was `tone(660.0, 0.18, amp=0.12, a=0.01, r=0.10)`).
    ping_t = np.arange(int(0.8 * SR)) / SR
    ping = (np.sin(2 * np.pi * 1318.5 * ping_t) + 0.3 * np.sin(2 * np.pi * 2637.0 * ping_t)
            + 0.12 * np.sin(2 * np.pi * 3951.0 * ping_t)) * np.exp(-ping_t / 0.16)
    ping[: int(0.004 * SR)] *= np.linspace(0, 1, int(0.004 * SR))
    write_wav(work / "sfx_story_gap.wav", ping / np.max(np.abs(ping)) * 0.15)

    run(["ffmpeg", "-y", "-i", str(work / "music_bed.wav"), "-af", f"apad=whole_dur={total_duration}",
         "-t", str(total_duration), "-ar", "44100", "-ac", "2", str(work / "music_full.wav")])

    music_input_idx = n_voices
    filt.append(
        f"[{music_input_idx}:a][voices_duck]sidechaincompress=threshold=0.02:ratio=8:attack=25:release=400:makeup=1[ducked]"
    )

    gap_starts_ms = [round(INTRO_DUR * 1000)]
    c = INTRO_DUR + GAP_DUR
    for i in range(len(story_ids) - 1):
        c += story_durations[i]
        gap_starts_ms.append(round(c * 1000))
        c += STORY_GAP
    gap_starts_ms.append(round((total_duration - OUTRO_DUR - OUTRO_GAP) * 1000))

    sfx_inputs = []
    sfx_labels = []
    for j, ms in enumerate(gap_starts_ms):
        sfx_inputs += ["-i", str(work / "sfx_story_gap.wav")]
        filt.append(f"[{n_voices + 1 + j}:a]adelay={ms}:all=1[sfxg{j}]")
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
        "visuals": visual_summary,
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
