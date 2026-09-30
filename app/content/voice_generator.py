import asyncio
from pathlib import Path

import edge_tts


# The architecture calls for "One branded AI voice" across every
# episode -- hardcoded rather than configurable so every story sounds
# consistent. en-US-JennyNeural (chosen by the user from the voice
# samples) is a free Microsoft neural voice served by edge-tts over the
# network; no API key required.
VOICE_NAME = "en-US-JennyNeural"

# edge-tts reports SentenceBoundary offset/duration in "ticks" (100-ns
# units, the Azure Speech convention) -- divide by this to get seconds.
TICKS_PER_SECOND = 10_000_000


async def _synthesize(text: str, output_path: Path) -> list[dict]:
    communicate = edge_tts.Communicate(text, VOICE_NAME, boundary="SentenceBoundary")
    segments: list[dict] = []

    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                start = chunk["offset"] / TICKS_PER_SECOND
                end = start + chunk["duration"] / TICKS_PER_SECOND
                segments.append({"text": chunk["text"], "start": start, "end": end})

    return segments


def synthesize_voice(text: str, output_path: Path) -> list[dict]:
    """
    Generate narration audio for `text` and save it as an mp3 at
    `output_path`. Runs edge-tts's async client inside a fresh event
    loop -- safe here because each Celery worker task runs
    synchronously in its own process.

    Returns the real per-sentence timing edge-tts reports while
    synthesizing -- a list of {"text", "start", "end"} (seconds), each
    exactly when that sentence is actually spoken in the generated
    audio. Used by build_captions() (app/content/video_composer.py)
    for frame-accurate captions instead of a proportional
    character-count estimate.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_synthesize(text, output_path))
