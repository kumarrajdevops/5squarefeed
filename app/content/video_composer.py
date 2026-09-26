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


def concat_videos(video_paths: list[Path], output_path: Path) -> None:
    """
    Concatenate multiple already-composed videos into one, in the
    given order, via ffmpeg's concat demuxer with stream copy (no
    re-encoding -- fast and lossless, safe as long as every input
    shares the same codec/resolution/pixel format). Used by
    app/content/storyboard_composer.py to join a scene's frame-ramp
    segments and multi-state comparison clips.
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
