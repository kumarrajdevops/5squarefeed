import json
import subprocess
from pathlib import Path


def get_audio_duration_seconds(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def probe_video(video_path: Path) -> dict:
    """
    Inspect a video file via ffprobe: total duration, which stream
    types are present, and the video stream's pixel dimensions. Used
    by app/qa/video_qa.py's video-integrity check -- a real,
    independent check of the actual file, not just trusting that
    composition/concatenation reported success. width/height are read
    specifically from the stream where codec_type == "video" (an audio
    stream has no width/height of its own).
    """

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,width,height",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    codec_types = {stream.get("codec_type") for stream in streams}
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)

    return {
        "duration_seconds": float(data.get("format", {}).get("duration", 0.0)),
        "has_video": "video" in codec_types,
        "has_audio": "audio" in codec_types,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
    }


def _format_srt_timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_captions(segments: list[dict], output_path: Path) -> None:
    """
    Write an .srt caption file from `segments` -- real per-sentence
    timing reported by edge-tts during synthesis (see
    app/content/voice_generator.py's synthesize_voice()), not an
    estimate. Each segment's start/end already reflects exactly when
    that sentence is spoken in the audio, so captions no longer drift
    on longer/uneven sentences the way a proportional character-count
    split did.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for index, segment in enumerate(segments, start=1):
            f.write(f"{index}\n")
            f.write(
                f"{_format_srt_timestamp(segment['start'])} --> "
                f"{_format_srt_timestamp(segment['end'])}\n"
            )
            f.write(f"{segment['text']}\n\n")


def generate_gap_clip(duration_seconds: float, output_path: Path) -> None:
    """
    A short silent black clip inserted between consecutive story
    segments in the concatenated episode video (see
    app/tasks/episode_video.py) -- a deliberate beat so one story's
    narration doesn't run straight into the next, giving continuity/
    sync a moment to visually and audibly reset between stories.

    Generated directly via ffmpeg's lavfi color/anullsrc sources (no
    image file needed) at the exact same codec/resolution/frame-rate/
    audio profile compose_video() above produces (h264, 1280x720,
    yuv420p, 25fps; aac, 24kHz mono) so it splices via concat_videos'
    stream-copy concatenation without a mismatch.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=25:d={duration_seconds}",
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate=24000",
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-t", str(duration_seconds),
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def concat_videos(video_paths: list[Path], output_path: Path) -> None:
    """
    Concatenate multiple already-composed story videos into one, in
    the given order, via ffmpeg's concat demuxer with stream copy (no
    re-encoding -- fast and lossless, safe here because every story
    video comes from the same compose_video() call above, so codec/
    resolution/pixel format always match).

    This is a straight concatenation only -- no intro/outro,
    transitions, or episode-level branding. That's a separate,
    not-yet-built piece of the architecture (see TODO.md).
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # The concat demuxer resolves each `file` entry relative to the
    # list file's own directory, so write the list next to the
    # per-story videos and reference them by filename only.
    list_path = video_paths[0].parent / f"_concat_{output_path.stem}.txt"

    with open(list_path, "w", encoding="utf-8") as f:
        for path in video_paths:
            f.write(f"file '{path.name}'\n")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        list_path.unlink(missing_ok=True)


def compose_video(
    image_path: Path,
    audio_path: Path,
    captions_path: Path,
    output_path: Path,
    duration_seconds: float | None = None,
) -> None:
    """
    Compose a static-image + narration-audio + burned-in-captions
    video via ffmpeg. The image loops for the audio's duration.

    `-shortest` alone is not precise here: combined with a looped
    image input and the subtitles filter, the video stream has been
    observed running 1-2+ seconds longer than the audio stream (e.g.
    26.68s video vs. 24.55s audio on one real story) -- GOP/keyframe
    flushing overshoot past where audio actually ends, not a rounding
    error. Left uncorrected, that per-clip overshoot accumulates
    additively once every story is concatenated into one episode video
    (concat_videos() below), producing a large audio/video/caption
    desync by the end of a 25+ story episode. Passing an explicit
    `-t duration_seconds` hard-caps the output to the real audio
    length regardless of keyframe alignment; `-shortest` is kept as a
    secondary safeguard.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    subtitles_filter = (
        f"subtitles={captions_path}:force_style="
        f"'FontName=DejaVu Sans,FontSize=20,PrimaryColour=&HFFFFFF&'"
    )

    command = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-vf", subtitles_filter,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
    ]

    if duration_seconds is not None:
        command += ["-t", str(duration_seconds)]

    command.append(str(output_path))

    subprocess.run(command, check=True, capture_output=True, text=True)
