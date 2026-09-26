"""
Focused tests for app.content.episode_renderer.render_story_standalone --
the DEV single-story endpoint's entire implementation (see
app/tasks/storyboard_prototype.py) -- and its shared use of
render_story_enhanced(), the canonical enhanced per-story renderer also
used by render_episode(). Two tiers:

- Orchestration (no ffmpeg): confirms a failed storyboard prerequisite
  short-circuits before any rendering is attempted.
- Real end-to-end (ffmpeg-gated, same pattern as
  test_storyboard_compose_integration.py): confirms the actual enhanced
  video+caption renderer runs, this story's own narration gets muxed in
  (the standalone output must never be video-only), and the final file
  lands at the documented media/pillow_enhanced/{story_id}_pillow_enhanced.mp4
  path rather than the base {story_id}_storyboard.mp4 intermediate.
"""
import json
import shutil
import subprocess

import pytest

from app.content import episode_renderer


ffmpeg_required = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def test_render_story_standalone_short_circuits_on_failed_prerequisite(monkeypatch):
    """A genuine storyboard/content failure must be returned as-is,
    without ever attempting to render (render_story_enhanced must not
    be called at all)."""
    failed = {"story_id": 51, "status": "failed", "stage": "content", "error": "boom"}
    monkeypatch.setattr(episode_renderer, "ensure_storyboard", lambda db, story_id: failed)
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: _FakeSessionCtx())

    render_calls = []
    monkeypatch.setattr(
        episode_renderer, "render_story_enhanced",
        lambda story_id, tail_pad, work: render_calls.append(story_id) or (None, 0.0, []),
    )

    result = episode_renderer.render_story_standalone(51)

    assert result == failed
    assert render_calls == []


@ffmpeg_required
def test_render_story_standalone_produces_a_muxed_video_with_real_narration(tmp_path, monkeypatch):
    """
    Real ffmpeg + real Pillow rendering (no mocking of render_story_enhanced
    itself) -- exercises the actual canonical enhanced-renderer code path
    render_episode() also uses, then confirms the standalone-only step
    (muxing this story's own processed narration onto it) leaves a final
    file with BOTH a video and an audio stream, at the documented
    pillow_enhanced output path, with the base *_storyboard.mp4 convention
    untouched by this function (produce_storyboard_prototype writes that
    one; this test only exercises the enhanced renderer +standalone mux).
    """
    story_id = 999001
    media_root = tmp_path / "media"
    out_dir = media_root / "pillow_enhanced"
    monkeypatch.setattr(episode_renderer, "MEDIA", media_root)
    monkeypatch.setattr(episode_renderer, "OUT_DIR", out_dir)
    # video_path/storyboard_path are stored relative to APP_ROOT in real
    # use (MEDIA is always a subdirectory of the real APP_ROOT) -- redirect
    # APP_ROOT to tmp_path too so relative_to() succeeds for this test's
    # own isolated MEDIA location, without touching the module's other
    # already-import-time-bound absolute asset paths (LIGHT_ICON etc.).
    monkeypatch.setattr(episode_renderer, "APP_ROOT", tmp_path)

    storyboard = {
        "version": 1, "story_id": story_id, "taxonomy_category": "research",
        "source_name": "Example Source", "accent_color": [64, 156, 255],
        "total_duration_seconds": 3.0,
        "scenes": [
            {
                "scene_id": "hero_0", "scene_type": "hero", "order": 0,
                "narration_text": "A short test headline for this standalone story.",
                "kicker": "A Short Test Headline",
                "start": 0.0, "end": 3.0, "duration": 3.0, "silent": False,
                "motion": {"type": "zoom_in", "max_zoom": 1.1},
                "source_segment_indices": [0],
            },
        ],
    }
    storyboard_dir = media_root / "storyboard" / str(story_id)
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    (storyboard_dir / "storyboard.json").write_text(json.dumps(storyboard))

    audio_dir = media_root / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3.0",
         "-c:a", "libmp3lame", str(audio_dir / f"{story_id}.mp3")],
        check=True, capture_output=True, text=True,
    )

    monkeypatch.setattr(
        episode_renderer, "ensure_storyboard",
        lambda db, sid: {"story_id": sid, "status": "ready", "reused": False, "qa": [{"check": "fake", "passed": True}]},
    )
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: _FakeSessionCtx())

    result = episode_renderer.render_story_standalone(story_id)

    assert result["status"] == "ready"
    assert result["scene_count"] == 1
    assert result["scene_types"] == ["hero"]

    final_path = media_root / "pillow_enhanced" / f"{story_id}_pillow_enhanced.mp4"
    assert final_path.exists()
    assert result["video_path"].endswith(f"{story_id}_pillow_enhanced.mp4")
    assert "storyboard.mp4" not in result["video_path"]

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(final_path)],
        check=True, capture_output=True, text=True,
    )
    stream_types = {line.strip() for line in probe.stdout.splitlines() if line.strip()}
    assert stream_types == {"video", "audio"}, f"expected both a video and audio stream, got: {stream_types}"


class _FakeSessionCtx:
    """Same minimal stand-in used in test_episode_renderer_lookup.py --
    render_story_standalone's own body only ever passes `db` through to
    the monkeypatched ensure_storyboard, which ignores it here."""
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
