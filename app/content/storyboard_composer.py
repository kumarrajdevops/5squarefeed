import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.content.scene_renderer import FONT_REGULAR, _wrap_text, render_scene_frame_sequence
from app.content.video_composer import build_captions, concat_videos


# Pure ffmpeg subprocess calls only -- no moviepy/ffmpeg-python (neither
# installed; app/content/video_composer.py already establishes this
# project's "ffmpeg as an external binary" idiom, reused here rather
# than introducing a new dependency).

FPS = 25
SCENE_CANVAS = "2560:1440"
OUTPUT_W, OUTPUT_H = 1920, 1080
CAPTION_FONT_SIZE = 20
SUBTITLE_STYLE = f"FontName=DejaVu Sans,FontSize={CAPTION_FONT_SIZE},PrimaryColour=&HFFFFFF&"
# `original_size` pins the subtitles filter's internal coordinate space
# to the real final output resolution -- without it, libass infers its
# own internal script resolution for a plain .srt (no ASS header),
# which is why every caption in this pipeline previously rendered far
# larger than "FontSize=20 out of 1080p" would suggest. Confirmed via a
# real, isolated ffmpeg render (not assumed): with this pin in place,
# FontSize=20 renders as real, legible ~20-25px-tall text at 1080p, and
# a literal newline inside a caption's .srt text block is respected as
# a real hard line break by libass -- both empirically verified before
# building the wrap logic below on top of these two facts.
CAPTION_ORIGINAL_SIZE = f"{OUTPUT_W}x{OUTPUT_H}"

# Master Storyboard Specification Phase 3B (caption safety). Empirically
# measured, not assumed: with CAPTION_ORIGINAL_SIZE pinned, libass's
# real rendered pixel width at FontSize=20 is consistently ~3.2x what
# PIL's own ImageFont.truetype(path, 20).textlength() predicts for the
# same text (confirmed via a real ffmpeg render + pixel bounding-box
# measurement against two different real strings, ratios 3.09 and
# 3.20). Requesting PIL's font at this SCALED-UP size makes
# scene_renderer._wrap_text's existing, already-tested wrap algorithm
# usable as-is to predict libass's real wrapped line breaks, rather
# than writing a second wrapping implementation.
CAPTION_LIBASS_SCALE_FACTOR = 3.2
CAPTION_WRAP_FONT_SIZE = round(CAPTION_FONT_SIZE * CAPTION_LIBASS_SCALE_FACTOR)
# Real px width at the real 1920px-wide output, leaving a safe side
# margin comparable to CONTENT_MARGIN_X used for on-card text elsewhere
# in this pipeline (140px each side -> 1920 - 2*140 = 1640).
CAPTION_MAX_WIDTH_PX = 1640
CAPTION_MAX_LINES = 3

# Deterministic per-cue budget (app/qa/storyboard_qa.py re-derives and
# checks against these same values -- see the new caption_cue_* checks).
# MAX_CUE_DURATION_SECONDS reuses the already-established
# SCENE_VISUAL_DURATION_CAPS/_DURATION_CAPS default (8.0) rather than
# inventing a new number; MIN_CUE_DURATION_SECONDS reuses
# storyboard_generator.COMPARISON_STATE_MIN_HOLD_SECONDS's own
# already-established "too short to be meaningful" threshold (1.0).
# MAX_WORDS_PER_CUE is cross-checked against this dataset's observed
# ~2.5-2.8 real words/sec TTS speaking rate x the 8s cap, and
# empirically confirmed (via the same real ffmpeg measurement above) to
# keep a cue's wrapped text at or under CAPTION_MAX_LINES.
MAX_CUE_DURATION_SECONDS = 8.0
MAX_WORDS_PER_CUE = 20
MIN_CUE_DURATION_SECONDS = 1.0

# Generalizes _COMPARISON_MARKERS below to a broader, still-literal set
# of real clause-boundary markers -- tried in this priority order
# (higher-priority / more substantial conjunctions first) so a long
# real sentence with several commas doesn't fragment into many tiny
# cues when a single, more meaningful split would do. Comparison
# scenes never reach this list -- they keep their own unchanged,
# dedicated 2-way split (see _comparison_caption_cues) for a byte-for-
# byte regression guarantee.
CLAUSE_SPLIT_MARKERS = [
    " while ", " whereas ", " compared to ", " versus ", " vs. ",
    " because ", " although ", " but ", " and ",
    "; ", ", ",
    " — ", " - ",
]
# Conjunction-style markers split BEFORE the marker so the conjunction
# word introduces the clause that follows it ("Reports show growth,"
# / "while analysts remain cautious..." rather than orphaning "while"
# onto the end of the first cue) -- reads more naturally. Punctuation-
# style markers (comma/semicolon/dash) keep the mark itself attached
# to the end of the preceding clause, matching normal writing
# convention. Either way, no word is ever dropped -- concatenating a
# split's two sides always recovers the original text exactly.
_CONJUNCTION_MARKERS = {
    " while ", " whereas ", " compared to ", " versus ", " vs. ",
    " because ", " although ", " but ", " and ",
}


def _clip_path_for_scene(scene_dir: Path, scene: dict) -> Path:
    return scene_dir / f"scene_{scene['order']}_{scene['scene_id']}.mp4"


# Same comparison markers app/content/storyboard_generator.py splits
# on -- duplicated here (not imported) since this is only used to
# re-split a caption into two shorter cues, a presentation concern
# distinct from that module's scene-classification decision table.
_COMPARISON_MARKERS = [" while ", " whereas ", " compared to ", " versus ", " vs. "]


def _comparison_caption_cues(scene: dict) -> list[dict]:
    """
    UNCHANGED from before Phase 3B (byte-for-byte -- pinned by a
    regression test) -- a comparison scene's real narration is one
    long sentence; splitting at the scene's own natural clause
    boundary (the same comparison marker the generator itself split
    on) into two proportionally-timed cues keeps real, unmodified
    narration text and real derived timing. Kept as its own dedicated
    function (not routed through the generalized Phase 3B splitter
    below) specifically so this exact, already-tested behavior can
    never silently drift.
    """
    text = scene.get("narration_text") or ""
    duration = scene["duration"]

    lowered = text.lower()
    for marker in _COMPARISON_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            left, right = text[:idx].strip(), text[idx + len(marker):].strip()
            split_at = duration * (len(left) / max(1, len(left) + len(right)))
            return [
                {"text": left, "start": 0.0, "end": split_at},
                {"text": right, "start": split_at, "end": duration},
            ]

    return [{"text": text, "start": 0.0, "end": duration}]


def _find_best_clause_split(text: str, duration: float) -> int | None:
    """
    Finds the real clause-boundary character offset (from
    CLAUSE_SPLIT_MARKERS, checked in priority order) to split `text`
    at: among a marker type's real occurrences, prefers the one whose
    time-proportional split keeps BOTH resulting sides at or above
    MIN_CUE_DURATION_SECONDS and is closest to the text's real
    midpoint. Returns None when no marker produces a valid split --
    the caller then keeps the text as one (longer than ideal) cue
    rather than inventing a break point.
    """
    lowered = text.lower()
    midpoint = len(text) / 2

    for marker in CLAUSE_SPLIT_MARKERS:
        candidates = []
        search_from = 0
        is_conjunction = marker in _CONJUNCTION_MARKERS
        while True:
            idx = lowered.find(marker, search_from)
            if idx == -1:
                break
            split_point = idx if is_conjunction else idx + len(marker)
            left_fraction = split_point / max(1, len(text))
            left_seconds = duration * left_fraction
            right_seconds = duration - left_seconds
            if left_seconds >= MIN_CUE_DURATION_SECONDS and right_seconds >= MIN_CUE_DURATION_SECONDS:
                candidates.append(split_point)
            search_from = idx + 1
        if candidates:
            return min(candidates, key=lambda point: abs(point - midpoint))

    return None


def _split_long_cue(text: str, start: float, end: float) -> list[dict]:
    """
    Recursively splits one real cue's text/time span at real clause
    boundaries whenever it exceeds the deterministic budget
    (MAX_CUE_DURATION_SECONDS / MAX_WORDS_PER_CUE) -- generalizes the
    same proportional-by-character time-split technique already used
    (and already accepted) for comparison scenes to a broader marker
    set and to as many splits as needed. Every returned cue's text is
    an exact, contiguous, real substring of `text` -- never reworded,
    reordered, or invented. Recursion terminates because each split
    strictly shrinks both sides, and _find_best_clause_split returns
    None (stopping the recursion) once no valid marker remains.
    """
    duration = end - start
    if duration <= MAX_CUE_DURATION_SECONDS and len(text.split()) <= MAX_WORDS_PER_CUE:
        return [{"text": text, "start": start, "end": end}]

    split_at = _find_best_clause_split(text, duration)
    if split_at is None:
        return [{"text": text, "start": start, "end": end}]

    left_text, right_text = text[:split_at].strip(), text[split_at:].strip()
    left_fraction = split_at / max(1, len(text))
    mid = start + duration * left_fraction
    return _split_long_cue(left_text, start, mid) + _split_long_cue(right_text, mid, end)


def _general_caption_cues(scene: dict) -> list[dict]:
    """
    Tier 1: one cue per real narration_segments entry (edge-tts's own
    per-sentence timing, see storyboard_generator._build_narration_segments)
    -- zero estimation, exact real timing, matching this pipeline's
    strongest existing principle. A non-merged scene's single real
    sentence passes through here as one entry.
    Tier 2 (_split_long_cue): only when a Tier-1 cue is STILL too
    long/dense on its own (a single real run-on sentence) -- splits it
    at real clause boundaries. Never fabricates, rewrites, or
    paraphrases -- every cue is a real, contiguous substring of the
    real narration.
    """
    narration_segments = scene.get("narration_segments") or [
        {"text": scene.get("narration_text") or "", "start": 0.0, "end": scene["duration"]}
    ]
    cues = []
    for entry in narration_segments:
        if not entry["text"]:
            continue
        cues.extend(_split_long_cue(entry["text"], entry["start"], entry["end"]))
    return cues or [{"text": scene.get("narration_text") or "", "start": 0.0, "end": scene["duration"]}]


def _caption_cues(scene: dict) -> list[dict]:
    """
    `comparison` scenes keep their own unchanged, dedicated 2-way
    split (see _comparison_caption_cues). Every other scene type uses
    the Phase 3B generalized Tier-1/Tier-2 splitter -- a scene short
    enough to already fit under budget still returns exactly one cue
    for its whole duration, a strict no-op for anything not affected
    by the caption-overflow finding.
    """
    if scene["scene_type"] == "comparison":
        return _comparison_caption_cues(scene)
    return _general_caption_cues(scene)


def _wrap_caption_text(text: str) -> str:
    """
    Deterministically wraps one cue's text into real, hard line breaks
    (a literal newline in an .srt block, which libass respects as-is
    -- empirically confirmed via a direct ffmpeg render before this
    function was written) using the SAME word-wrap algorithm already
    proven for on-card Pillow text (scene_renderer._wrap_text), at a
    PIL font size empirically calibrated (CAPTION_WRAP_FONT_SIZE) so
    its measurement matches libass's real rendered width at the real
    output resolution. Never truncates, rewords, or drops a word --
    only decides which real words fall on which line. This is
    explicitly NOT a font-size change to the burned caption itself
    (CAPTION_FONT_SIZE is unchanged); it is a separate, PIL-only
    measurement used purely to choose line-break positions.
    """
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = ImageFont.truetype(FONT_REGULAR, CAPTION_WRAP_FONT_SIZE)
    lines = _wrap_text(draw, text, font, CAPTION_MAX_WIDTH_PX)
    return "\n".join(lines)


def _write_scene_captions(scene: dict, scene_dir: Path) -> Path:
    """
    Built via video_composer.py's build_captions() completely
    unmodified -- the scene's own real narration text/duration (from
    edge-tts's own per-sentence timing, not re-derived), same
    burned-subtitle idiom compose_video() already uses. Each cue's
    text is deterministically pre-wrapped (see _wrap_caption_text)
    before being written, so the resulting line count is exactly
    known rather than left to libass's own unpinned auto-wrap guess.
    """
    srt_path = scene_dir / f"scene_{scene['order']}_{scene['scene_id']}.srt"
    cues = [
        {"text": _wrap_caption_text(cue["text"]), "start": cue["start"], "end": cue["end"]}
        for cue in _caption_cues(scene)
    ]
    build_captions(cues, srt_path)
    return srt_path


def _zoompan_expr(motion: dict) -> tuple[str, str, str]:
    motion_type = motion.get("type", "static_hold")
    if motion_type == "zoom_in":
        max_zoom = motion.get("max_zoom", 1.12)
        return f"min(zoom+0.0018,{max_zoom})", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion_type == "split_reveal":
        # Fixed, subtle zoom -- no pan. The split-screen layout itself
        # is the visual interest; motion stays restrained/editorial
        # per this project's "not a trailer" rule.
        return "1.05", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return "1.0", "0", "0"


def render_scene_clip(scene: dict, storyboard: dict, scene_dir: Path, content_audio_path: Path, output_path: Path) -> None:
    """
    Composes one non-silent scene into a standalone, concat-compatible
    mp4: motion/visual-state sequence + burned captions + the real
    narration audio slice. Ends with an explicit `-t duration` hard
    cap, replicating app/content/video_composer.py's compose_video()'s
    documented, deliberate fix for a GOP/keyframe-overshoot audio/video
    desync bug -- applied here per-scene instead of per-story.

    Loops over whatever render_scene_frame_sequence() returns -- one
    ffmpeg image-sequence/still input per segment, joined via ffmpeg's
    `concat` filter (operates on decoded/filtered frame streams
    directly, so it needs no codec matching, unlike video_composer.py's
    concat_videos(), which stream-copies whole already-encoded files).
    This is a single generalized path for every scene: a plain scene
    (hero/key_fact/etc) is just the N=1 case; the old ramp+hold
    count-up shape is N=2; a multi-state comparison scene is N=5 (or
    fewer, via its own short-duration fallback) -- same mechanism
    throughout, not a special case per scene type.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = scene_dir / f"frames_{scene['order']}_{scene['scene_id']}"
    segments = render_scene_frame_sequence(scene, storyboard, frames_dir, fps=FPS)
    captions_path = _write_scene_captions(scene, scene_dir)

    duration = scene["duration"]
    motion = scene.get("motion") or {}

    default_z, default_x, default_y = _zoompan_expr(motion)

    command = ["ffmpeg", "-y"]
    scale_filters = []

    for index, segment in enumerate(segments):
        if segment["is_ramp"]:
            frame_dir_for_segment = segment["frame_paths"][0].parent
            command += ["-framerate", str(FPS), "-i", str(frame_dir_for_segment / "frame_%05d.png")]
        else:
            command += [
                "-loop", "1", "-framerate", str(FPS), "-t", str(segment["duration"]),
                "-i", str(segment["frame_paths"][0]),
            ]
        # Ken-Burns/motion applied per-segment, targeting the FINAL
        # output resolution directly (not the supersampled canvas) --
        # every segment must land at the same resolution before
        # `concat` can join them cleanly. This also fixes a previously
        # dead code path: a comparison scene's "split_reveal" fixed
        # 1.05x zoom was defined but never actually reached the ramp
        # branch before this change (the ramp path only ever did a
        # flat scale-down); it's real now, applied uniformly across
        # every state since it's a constant (non-animated) zoom level,
        # so there's no discontinuity at a state boundary.
        #
        # A Phase 3A generalized long-scene segment (see
        # scene_renderer.render_scene_frame_sequence /
        # storyboard_generator._build_static_states) carries its own
        # fixed per-segment "zoom" -- used INSTEAD of the scene-level
        # motion here so consecutive segments of the same reused frame
        # are never pixel-identical. Every other segment (including
        # every existing comparison state, which never carries a
        # "zoom" key) falls back to the scene-level motion exactly as
        # before -- zero behavior change for anything but the new case.
        if "zoom" in segment:
            z, x, y = str(segment["zoom"]), "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        else:
            z, x, y = default_z, default_x, default_y
        scale_filters.append(
            f"[{index}:v]scale={SCENE_CANVAS},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},"
            f"setsar=1[s{index}]"
        )

    audio_index = len(segments)
    command += ["-ss", str(scene["start"]), "-to", str(scene["end"]), "-i", str(content_audio_path)]

    concat_inputs = "".join(f"[s{index}]" for index in range(len(segments)))
    filter_complex = (
        ";".join(scale_filters) + ";"
        f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[vcat];"
        f"[vcat]format=yuv420p,subtitles={captions_path}:force_style='{SUBTITLE_STYLE}'"
        f":original_size={CAPTION_ORIGINAL_SIZE}[vout]"
    )
    command += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", f"{audio_index}:a"]

    command += [
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k", "-ar", "24000", "-ac", "1",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        str(output_path),
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)


def render_silent_scene_clip(scene: dict, storyboard: dict, scene_dir: Path, output_path: Path) -> None:
    """
    Same composition recipe as render_scene_clip, but for a silent
    scene (source_card/takeaway with no narration) -- anullsrc silence
    instead of a narration slice, same profile generate_gap_clip()
    already establishes (aac, 24kHz mono), no captions.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = scene_dir / f"frames_{scene['order']}_{scene['scene_id']}"
    # Silent scenes (source_card/takeaway) never have multi-state or
    # count-up motion, so this is always exactly one static-frame
    # segment -- same simple case as before this iteration's contract
    # change.
    segments = render_scene_frame_sequence(scene, storyboard, frames_dir, fps=FPS)
    frame_path = segments[0]["frame_paths"][0]

    duration = scene["duration"]
    z, x, y = _zoompan_expr(scene.get("motion") or {})

    command = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(FPS), "-i", str(frame_path),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
        "-vf", f"scale={SCENE_CANVAS},zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},format=yuv420p",
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k", "-ar", "24000", "-ac", "1",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        str(output_path),
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)


def compose_storyboard_video(storyboard: dict, content, scene_dir: Path, output_path: Path) -> None:
    """
    Renders every scene to its own concat-compatible clip, then joins
    them via app/content/video_composer.py's concat_videos() --
    completely unmodified, no duplicated concat logic.
    """
    clip_paths = []

    for scene in storyboard["scenes"]:
        clip_path = _clip_path_for_scene(scene_dir, scene)
        if scene.get("silent"):
            render_silent_scene_clip(scene, storyboard, scene_dir, clip_path)
        else:
            render_scene_clip(scene, storyboard, scene_dir, Path(content.audio_path), clip_path)
        clip_paths.append(clip_path)

    concat_videos(clip_paths, output_path)
