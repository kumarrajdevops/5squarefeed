import subprocess
from pathlib import Path

from app.content.scene_renderer import render_scene_frame_sequence
from app.content.video_composer import build_captions, concat_videos


# Pure ffmpeg subprocess calls only -- no moviepy/ffmpeg-python (neither
# installed; app/content/video_composer.py already establishes this
# project's "ffmpeg as an external binary" idiom, reused here rather
# than introducing a new dependency).

FPS = 25
SCENE_CANVAS = "2560:1440"
OUTPUT_W, OUTPUT_H = 1920, 1080
SUBTITLE_STYLE = "FontName=DejaVu Sans,FontSize=20,PrimaryColour=&HFFFFFF&"


def _clip_path_for_scene(scene_dir: Path, scene: dict) -> Path:
    return scene_dir / f"scene_{scene['order']}_{scene['scene_id']}.mp4"


# Same comparison markers app/content/storyboard_generator.py splits
# on -- duplicated here (not imported) since this is only used to
# re-split a caption into two shorter cues, a presentation concern
# distinct from that module's scene-classification decision table.
_COMPARISON_MARKERS = [" while ", " whereas ", " compared to ", " versus ", " vs. "]


def _caption_cues(scene: dict) -> list[dict]:
    """
    A `comparison` scene's real narration is one long sentence
    spanning its whole (often 10+ second) duration -- burning it in as
    a single caption cue would sit a large multi-line wall of text on
    screen for the entire scene, exactly the text-heavy-video problem
    this feature exists to fix. Splitting at the scene's own natural
    clause boundary (the same comparison marker the generator itself
    split on) into two shorter, proportionally-timed cues keeps real,
    unmodified narration text and real derived timing -- not
    fabricated, not re-timed from scratch -- while meaningfully
    shortening how long any single cue sits on screen. Every other
    scene type keeps one cue for its whole duration, matching the
    existing per-sentence caption granularity elsewhere in this
    pipeline (no word-by-word/kinetic captions here).
    """
    text = scene.get("narration_text") or ""
    duration = scene["duration"]

    if scene["scene_type"] == "comparison":
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


def _write_scene_captions(scene: dict, scene_dir: Path) -> Path:
    """
    Built via video_composer.py's build_captions() completely
    unmodified -- the scene's own real narration text/duration (from
    edge-tts's own per-sentence timing, not re-derived), same
    burned-subtitle idiom compose_video() already uses.
    """
    srt_path = scene_dir / f"scene_{scene['order']}_{scene['scene_id']}.srt"
    build_captions(_caption_cues(scene), srt_path)
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
        f"[vcat]format=yuv420p,subtitles={captions_path}:force_style='{SUBTITLE_STYLE}'[vout]"
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
