import shutil
import subprocess

import pytest

from app.content.storyboard_composer import compose_storyboard_video
from app.qa.storyboard_qa import run_storyboard_qa_checks


ffmpeg_required = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _make_synthetic_audio(path, duration_seconds: float) -> None:
    # A short synthetic tone via ffmpeg's own generator -- no network,
    # no edge-tts. Real MP3 encoding (libmp3lame), matching production
    # content.audio_path's real file type (edge-tts's own mp3 output,
    # see app/content/voice_generator.py) -- the mp3 muxer refuses a
    # non-MP3 stream ("Exactly one MP3 audio stream is required"), so
    # the generic `-c:a aac` idiom other tests use for a .aac-suffixed
    # file doesn't work for an .mp3-suffixed one.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:a", "libmp3lame",
            str(path),
        ],
        check=True, capture_output=True, text=True,
    )


class _FakeContent:
    def __init__(self, audio_path):
        self.audio_path = str(audio_path)


@ffmpeg_required
def test_compose_storyboard_video_end_to_end_hero_and_source_card(tmp_path):
    """
    Real ffmpeg, real Pillow-rendered frames, real synthetic audio --
    exercises the non-count-up path (hero's zoompan Ken-Burns motion +
    a silent source_card) end to end, then runs the same QA checks the
    live dev endpoint uses.
    """
    audio_path = tmp_path / "narration.mp3"
    _make_synthetic_audio(audio_path, 6.0)

    storyboard = {
        "version": 1, "story_id": 1, "taxonomy_category": "research",
        "source_name": "Example Source", "accent_color": [64, 156, 255],
        "total_duration_seconds": 9.0,
        "scenes": [
            {
                "scene_id": "hero_0", "scene_type": "hero", "order": 0,
                "narration_text": "A short test headline for this story.",
                "kicker": "A Short Test Headline",
                "start": 0.0, "end": 6.0, "duration": 6.0, "silent": False,
                "motion": {"type": "zoom_in", "max_zoom": 1.1},
                "source_segment_indices": [0],
            },
            {
                "scene_id": "source_card", "scene_type": "source_card", "order": 1,
                "narration_text": None,
                "start": 6.0, "end": 9.0, "duration": 3.0, "silent": True,
                "closing_line": "Full story: Example Source",
                "motion": {"type": "static_hold"},
                "source_segment_indices": [],
            },
        ],
    }

    output_path = tmp_path / "story_video.mp4"
    compose_storyboard_video(storyboard, _FakeContent(audio_path), tmp_path, output_path)

    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)
    failed = [c for c in checks if not c["passed"]]
    assert not failed, failed


@ffmpeg_required
def test_compose_storyboard_video_with_countup_comparison_scene(tmp_path):
    """
    Exercises the count-up path specifically (a real Pillow frame-ramp
    sequence joined via ffmpeg's concat filter with a held final
    frame) -- the more complex render path, kept separate from the
    simpler hero/source_card test above.
    """
    audio_path = tmp_path / "narration.mp3"
    _make_synthetic_audio(audio_path, 4.0)

    storyboard = {
        "version": 1, "story_id": 2, "taxonomy_category": "business",
        "source_name": "Example Source", "accent_color": [95, 200, 145],
        "total_duration_seconds": 4.0,
        "scenes": [
            {
                "scene_id": "comparison_0", "scene_type": "comparison", "order": 0,
                # Deliberately re-derivable via the same _split_comparison/
                # _extract_stat_fields the new numeric_integrity/
                # source_attribution QA checks use -- unlike a purely
                # cosmetic placeholder sentence, this narration actually
                # supports the stored left/right fields below.
                "narration_text": "10 million thing ones were confirmed deployed, while 20 million thing twos were confirmed installed.",
                "start": 0.0, "end": 4.0, "duration": 4.0, "silent": False,
                "left": {"stat": "10", "unit": "M", "entity": "Thing ones", "tier": None, "date": None, "source": None, "icon": "generic"},
                "right": {"stat": "20", "unit": "M", "entity": "Thing twos", "tier": None, "date": None, "source": None, "icon": "generic"},
                "motion": {"type": "split_reveal", "countup_seconds": 1.0},
                "source_segment_indices": [0],
            },
        ],
    }

    output_path = tmp_path / "story_video.mp4"
    compose_storyboard_video(storyboard, _FakeContent(audio_path), tmp_path, output_path)

    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)
    # scene_structure legitimately fails here (no hero/source_card in
    # this minimal single-scene fixture) -- everything else must pass.
    failed = [c for c in checks if not c["passed"] and c["check"] != "scene_structure"]
    assert not failed, failed


@ffmpeg_required
def test_compose_storyboard_video_with_multi_state_comparison_scene(tmp_path):
    """
    Exercises the NEW multi-state comparison path (intro -> reveal_left
    -> reveal_right -> both_context -> hold_1 -> hold_2, this
    iteration's replacement for the old flat ramp+hold shape) end to
    end through real ffmpeg concat + zoompan, confirming the real
    ffprobe-measured duration matches the state-duration sum and every
    QA check (including the new duration-cap/provenance ones) passes.
    """
    audio_path = tmp_path / "narration.mp3"
    _make_synthetic_audio(audio_path, 8.28)

    states = [
        {"name": "intro", "duration": 1.6, "countup_seconds": None, "countup_side": None},
        {"name": "reveal_left", "duration": 1.1, "countup_seconds": 1.1, "countup_side": "left"},
        {"name": "reveal_right", "duration": 1.1, "countup_seconds": 1.1, "countup_side": "right"},
        {"name": "both_context", "duration": 2.5, "countup_seconds": None, "countup_side": None},
        {"name": "hold_1", "duration": 0.99, "countup_seconds": None, "countup_side": None},
        {"name": "hold_2", "duration": 0.99, "countup_seconds": None, "countup_side": None},
    ]
    total_duration = sum(s["duration"] for s in states)

    storyboard = {
        "version": 1, "story_id": 3, "taxonomy_category": "research",
        "source_name": "Example Source", "accent_color": [64, 156, 255],
        "total_duration_seconds": total_duration,
        "scenes": [
            {
                "scene_id": "comparison_0", "scene_type": "comparison", "order": 0,
                "narration_text": (
                    "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
                    "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
                    "industrial robots will be deployed between 2026 and 2035."
                ),
                "header": "PROJECTED DEPLOYMENT SCALE",
                "start": 0.0, "end": total_duration, "duration": total_duration, "silent": False,
                "left": {"stat": "49", "unit": "M", "entity": "Autonomous vehicles", "tier": "L3–L5", "date": "By 2035", "source": "ABI Research", "icon": "vehicle"},
                "right": {"stat": "60", "unit": "M", "entity": "Industrial robots", "tier": None, "date": "2026–2035", "source": "Omdia", "icon": "robot"},
                "motion": {"type": "split_reveal", "countup_seconds": 1.1, "states": states},
                "source_segment_indices": [0],
            },
        ],
    }

    output_path = tmp_path / "story_video.mp4"
    compose_storyboard_video(storyboard, _FakeContent(audio_path), tmp_path, output_path)

    from app.content.video_composer import probe_video
    probe = probe_video(output_path)
    assert abs(probe["duration_seconds"] - total_duration) < 0.3

    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)
    failed = [c for c in checks if not c["passed"] and c["check"] != "scene_structure"]
    assert not failed, failed


@ffmpeg_required
def test_compose_storyboard_video_with_a_long_merged_scene_gets_real_multi_cue_captions(tmp_path):
    """
    Phase 3B end-to-end: direct regression shape for Story #52 (a
    19.44s merged hero scene that previously burned ONE unsplit
    caption cue overlapping the source-attribution line). With real
    narration_segments present, composing through the real ffmpeg
    pipeline must produce a real .srt with multiple cues, each within
    the deterministic budget, and every QA check (including the new
    caption-safety checks) must pass.
    """
    audio_path = tmp_path / "narration.mp3"
    _make_synthetic_audio(audio_path, 19.44)

    narration_segments = [
        {"text": "From Enablement to Execution, Egypt's AI Ecosystem Reaches Production Scale.", "start": 0.0, "end": 5.39},
        {"text": "Today, Egypt's AI builders gathered for a reception that highlighted the ecosystem.", "start": 5.39, "end": 12.0},
        {"text": "The event included a keynote from a senior NVIDIA executive.", "start": 12.0, "end": 19.44},
    ]
    narration_text = " ".join(s["text"] for s in narration_segments)

    storyboard = {
        "version": 1, "story_id": 6, "title": "From Enablement to Execution, Egypt's AI Ecosystem Reaches Production Scale",
        "taxonomy_category": "major_news", "source_name": "Example Source", "accent_color": [220, 90, 90],
        "total_duration_seconds": 19.44,
        "scenes": [
            {
                "scene_id": "hero_0", "scene_type": "hero", "order": 0,
                "narration_text": narration_text,
                "narration_segments": narration_segments,
                "kicker": "From Enablement to Execution",
                "start": 0.0, "end": 19.44, "duration": 19.44, "silent": False,
                # A real 19.44s hero also exceeds its own Phase 3A
                # visual-duration cap (6.0s) -- this states list is
                # exactly what _build_static_states would produce,
                # included here so this test exercises the NEW caption
                # logic without tripping the separately-already-tested
                # Phase 3A visual-duration check.
                "motion": {"type": "zoom_in", "max_zoom": 1.12, "states": [
                    {"name": "segment_1", "duration": 4.86, "is_ramp": False, "zoom": 1.0},
                    {"name": "segment_2", "duration": 4.86, "is_ramp": False, "zoom": 1.03},
                    {"name": "segment_3", "duration": 4.86, "is_ramp": False, "zoom": 1.06},
                    {"name": "segment_4", "duration": 4.86, "is_ramp": False, "zoom": 1.09},
                ]},
                "source_segment_indices": [0, 1, 2],
            },
        ],
    }

    output_path = tmp_path / "story_video.mp4"
    compose_storyboard_video(storyboard, _FakeContent(audio_path), tmp_path, output_path)

    from app.content.video_composer import probe_video
    probe = probe_video(output_path)
    assert abs(probe["duration_seconds"] - 19.44) < 0.3

    srt_path = tmp_path / "scene_0_hero_0.srt"
    srt_text = srt_path.read_text(encoding="utf-8")
    cue_count = srt_text.count(" --> ")
    assert cue_count >= 3, f"expected at least 3 real cues, got {cue_count}:\n{srt_text}"

    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)
    failed = [c for c in checks if not c["passed"] and c["check"] != "scene_structure"]
    assert not failed, failed


@ffmpeg_required
def test_compose_storyboard_video_with_a_generalized_long_non_comparison_scene(tmp_path):
    """
    Phase 3A end-to-end: a non-comparison scene (concept) whose real
    duration exceeds its own QA cap, composed through the real ffmpeg
    concat + per-segment zoompan path, confirming the real
    ffprobe-measured duration matches the split-segment sum and every
    QA check (including the now-generalized scene_visual_duration)
    passes.
    """
    audio_path = tmp_path / "narration.mp3"
    _make_synthetic_audio(audio_path, 6.2)

    states = [
        {"name": "segment_1", "duration": 3.1, "is_ramp": False, "zoom": 1.0},
        {"name": "segment_2", "duration": 3.1, "is_ramp": False, "zoom": 1.03},
    ]
    total_duration = sum(s["duration"] for s in states)

    storyboard = {
        "version": 1, "story_id": 4, "title": "A Long Real Sentence About Clean Energy Adoption",
        "taxonomy_category": "research", "source_name": "Example Source", "accent_color": [64, 156, 255],
        "total_duration_seconds": total_duration,
        "scenes": [
            {
                "scene_id": "concept_0", "scene_type": "concept", "order": 0,
                "narration_text": "A long real sentence about clean energy adoption that runs past the usual concept-scene cap.",
                "headline": "A long real sentence about clean energy…",
                "start": 0.0, "end": total_duration, "duration": total_duration, "silent": False,
                "motion": {"type": "headline_reveal", "states": states},
                "source_segment_indices": [0],
            },
        ],
    }

    output_path = tmp_path / "story_video.mp4"
    compose_storyboard_video(storyboard, _FakeContent(audio_path), tmp_path, output_path)

    from app.content.video_composer import probe_video
    probe = probe_video(output_path)
    assert abs(probe["duration_seconds"] - total_duration) < 0.3

    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)
    failed = [c for c in checks if not c["passed"] and c["check"] != "scene_structure"]
    assert not failed, failed
