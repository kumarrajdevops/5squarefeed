"""Lightweight deterministic checks for a rendered template episode: they read
the per-story render_meta.json files the renderer leaves in its work dir plus
the final mp4 (ffprobe/ffmpeg only, no OCR, no numpy). Run:

    python -m app.qa.polish_qa <episode_id> [video.mp4]
"""
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from app.content import episode_renderer as er

BRAND_ASSETS = (
    "app/dashboard/branding/icons/5squarefeed-icon-light.png",
    "app/dashboard/branding/logo/5squarefeed-logo-primary.png",
)
MAX_STATIC = 1.0    # codec noise on a static scene stays well under this; real photo motion here is >3


def _probe(path: Path, stream: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return float(out[0]) if out else 0.0


def _window_frame(video: Path, t: float) -> Image.Image:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf",
         f"crop={er.WIN_W}:{er.WIN_H - 220}:{er.WIN_X}:{er.WIN_Y},format=gray", "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True, check=True,
    ).stdout
    return Image.open(BytesIO(raw)).convert("L")


def _motion(video: Path, a: float, b: float) -> float:
    return ImageStat.Stat(ImageChops.difference(_window_frame(video, a), _window_frame(video, b))).mean[0]


def load_metas(episode_id: int, story_ids: list[int]) -> list[dict]:
    work = er.OUT_DIR / f"ep{episode_id}_work"
    return [json.loads((work / f"story_{sid}" / "render_meta.json").read_text()) for sid in story_ids]


def check_episode(episode_id: int, video: Path) -> dict:
    story_ids, _date = er.load_episode_stories(episode_id)
    metas = load_metas(episode_id, story_ids)
    checks: dict = {}

    checks["story_count"] = len(metas) == 25 and [m["story_id"] for m in metas] == story_ids
    checks["caption_max_lines<=2"] = all(m["caption_max_lines"] <= er.CAPTION_MAX_LINES for m in metas)
    checks["no_truncated_headlines"] = all(not h.endswith("…") for m in metas for h in m.get("headlines", [])) and all("headlines" in m for m in metas)
    checks["brand_assets_present"] = all(Path(p).exists() for p in BRAND_ASSETS)

    v, a = _probe(video, "v:0"), _probe(video, "a:0")
    checks["av_duration_match"] = abs(v - a) < 0.15
    expected = er.INTRO_DUR + er.GAP_DUR + sum(m["total"] for m in metas) + er.STORY_GAP * (len(metas) - 1) + er.OUTRO_GAP + er.OUTRO_DUR
    checks["timeline_matches_layout"] = abs(v - expected) < 0.3

    cursor, static_motion = er.INTRO_DUR + er.GAP_DUR, []
    for m in metas:
        offset = cursor
        for scene in m["scenes"]:
            dur = scene["duration"]
            if dur >= 2.5 and scene["motion"] == "static" and scene["visual"] in ("photo", "logo", "fallback"):
                static_motion.append((m["story_id"], round(_motion(video, offset + 0.5, offset + dur - 0.5), 3)))
            offset += dur
        cursor += m["total"] + er.STORY_GAP
    checks["static_scenes_static"] = all(d <= MAX_STATIC for _s, d in static_motion)
    checks["_static_motion"] = static_motion
    return checks


if __name__ == "__main__":
    episode = int(sys.argv[1])
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else er.OUT_DIR / f"episode_{episode}_pillow_enhanced.mp4"
    result = check_episode(episode, path)
    for key, value in result.items():
        print(f"{key}: {value}")
    sys.exit(0 if all(v for k, v in result.items() if not k.startswith("_")) else 1)
