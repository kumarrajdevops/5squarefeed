import shutil
import subprocess
from pathlib import Path

import pytest

from app.content.storyboard_generator import _comparison_header, _derive_hero_kicker
from app.qa.storyboard_qa import run_storyboard_qa_checks


ffmpeg_required = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _fake_storyboard():
    # narration_text is intentionally omitted (None) on both scenes --
    # checks 7/9/10 (which all key off narration_text) skip a scene
    # that has none, exactly like a real silent source_card scene.
    # source_segment_indices is set to a real-shaped value (non-empty
    # for hero, empty for source_card, matching how the generator
    # actually populates it) so check 11 passes without being the
    # focus of these structure/timing/asset-focused tests.
    return {
        "total_duration_seconds": 10.0,
        "scenes": [
            {
                "scene_id": "hero", "scene_type": "hero", "order": 0,
                "start": 0.0, "end": 5.0, "duration": 5.0,
                "narration_text": None, "source_segment_indices": [0],
            },
            {
                "scene_id": "source_card", "scene_type": "source_card", "order": 1,
                "start": 5.0, "end": 10.0, "duration": 5.0,
                "narration_text": None, "source_segment_indices": [],
            },
        ],
    }


def _check(checks, name):
    return next(c for c in checks if c["check"] == name)


def test_scene_structure_passes_for_hero_first_source_card_last(tmp_path):
    checks = run_storyboard_qa_checks(_fake_storyboard(), tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_structure")["passed"] is True


def test_scene_structure_fails_when_last_scene_is_not_a_closer(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"][-1]["scene_type"] = "comparison"
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_structure")["passed"] is False


def test_scene_timing_contiguous_detects_a_gap(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"][1]["start"] = 6.0  # gap between 5.0 and 6.0
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_timing_contiguous")["passed"] is False


def test_scene_assets_present_fails_when_clip_files_missing(tmp_path):
    checks = run_storyboard_qa_checks(_fake_storyboard(), tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_assets_present")["passed"] is False


def test_scene_assets_present_passes_when_clip_files_exist(tmp_path):
    storyboard = _fake_storyboard()
    for scene in storyboard["scenes"]:
        (tmp_path / f"scene_{scene['order']}_{scene['scene_id']}.mp4").write_bytes(b"fake video bytes")
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_assets_present")["passed"] is True


def test_missing_video_file_fails_resolution_and_duration_checks(tmp_path):
    checks = run_storyboard_qa_checks(_fake_storyboard(), tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "resolution_1080p")["passed"] is False
    assert _check(checks, "audio_video_duration_match")["passed"] is False


def test_brand_logo_present_reflects_the_real_placeholder_asset(tmp_path):
    # The real placeholder asset genuinely exists in this repo --
    # a real filesystem check, not a mock.
    checks = run_storyboard_qa_checks(_fake_storyboard(), tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "brand_logo_present")["passed"] is True


@ffmpeg_required
def test_resolution_and_duration_checks_pass_for_a_real_1080p_video(tmp_path):
    output_path = tmp_path / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25:d=2",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-t", "2",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )

    storyboard = {"total_duration_seconds": 2.0, "scenes": []}
    checks = run_storyboard_qa_checks(storyboard, tmp_path, output_path)

    assert _check(checks, "resolution_1080p")["passed"] is True
    assert _check(checks, "audio_video_duration_match")["passed"] is True


def _comparison_scene(**overrides):
    scene = {
        "scene_id": "comparison_2", "scene_type": "comparison", "order": 1,
        "start": 0.0, "end": 14.2605, "duration": 14.2605,
        "narration_text": (
            "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
            "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
            "industrial robots will be deployed between 2026 and 2035."
        ),
        "left": {"stat": "49", "unit": "M", "entity": "Autonomous vehicles", "tier": "L3–L5", "date": "By 2035", "source": "ABI Research", "icon": "vehicle"},
        "right": {"stat": "60", "unit": "M", "entity": "Industrial robots", "tier": None, "date": "2026–2035", "source": "Omdia", "icon": "robot"},
        "motion": {"states": [
            {"name": "intro", "duration": 1.6}, {"name": "reveal_left", "duration": 1.1},
            {"name": "reveal_right", "duration": 1.1}, {"name": "both_context", "duration": 2.5},
            {"name": "hold_1", "duration": 3.98}, {"name": "hold_2", "duration": 3.98},
        ]},
        "source_segment_indices": [2],
    }
    scene.update(overrides)
    return scene


def _key_fact_scene(**overrides):
    scene = {
        "scene_id": "key_fact_1", "scene_type": "key_fact", "order": 0,
        "start": 0.0, "end": 4.625, "duration": 4.625,
        "narration_text": "Physical AI is moving rapidly from research to large-scale deployment.",
        "stages": ["RESEARCH", "LARGE-SCALE DEPLOYMENT"],
        "source_segment_indices": [1],
    }
    scene.update(overrides)
    return scene


def test_text_duplication_passes_when_kicker_is_shorter_than_the_full_narration(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"][0].update({
        "narration_text": "Why Deploying Physical AI at Scale Demands Safety at Every Layer.",
        "kicker": "Deploying Physical AI at Scale…",
    })
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "text_duplication")["passed"] is True


def test_text_duplication_fails_when_dominant_text_repeats_the_full_narration_verbatim(tmp_path):
    storyboard = _fake_storyboard()
    narration = "Why Deploying Physical AI at Scale Demands Safety at Every Layer."
    storyboard["scenes"][0].update({"narration_text": narration, "kicker": narration})
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "text_duplication")["passed"] is False


def test_scene_visual_duration_passes_when_every_state_is_under_the_cap(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _comparison_scene())
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_visual_duration")["passed"] is True


def test_scene_visual_duration_fails_when_a_single_hold_state_exceeds_the_cap(tmp_path):
    """Direct regression for the original "long static hold after count-up" bug."""
    storyboard = _fake_storyboard()
    broken_comparison = _comparison_scene()
    broken_comparison["motion"]["states"] = [
        {"name": "intro", "duration": 1.6}, {"name": "reveal_left", "duration": 1.1},
        {"name": "reveal_right", "duration": 1.1}, {"name": "both_context", "duration": 2.5},
        {"name": "hold", "duration": 7.96},  # never split -- exceeds the 5.0s comparison cap
    ]
    storyboard["scenes"].insert(1, broken_comparison)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_visual_duration")["passed"] is False


def test_numeric_integrity_passes_when_stored_fields_match_fresh_derivation(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _comparison_scene())
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "numeric_integrity")["passed"] is True


def test_numeric_integrity_fails_when_a_stored_stat_was_hand_edited(tmp_path):
    storyboard = _fake_storyboard()
    tampered = _comparison_scene()
    tampered["left"]["stat"] = "999"  # doesn't match the real narration_text anymore
    storyboard["scenes"].insert(1, tampered)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "numeric_integrity")["passed"] is False


def test_source_attribution_passes_when_entity_source_pairing_matches_the_real_narration(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _comparison_scene())
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_attribution")["passed"] is True


def test_source_attribution_fails_when_a_source_drifts_onto_the_wrong_side(tmp_path):
    storyboard = _fake_storyboard()
    drifted = _comparison_scene()
    drifted["left"]["source"] = "Omdia"  # ABI Research belongs with the vehicles side, not Omdia
    storyboard["scenes"].insert(1, drifted)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_attribution")["passed"] is False


def test_source_fidelity_passes_for_real_provenance_and_real_progression_stages(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _key_fact_scene())
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is True


def test_source_fidelity_fails_when_source_segment_indices_is_missing(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"][0].pop("source_segment_indices")
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is False


def test_source_fidelity_fails_when_stages_were_hand_edited_beyond_what_the_narration_supports(tmp_path):
    storyboard = _fake_storyboard()
    tampered = _key_fact_scene()
    tampered["stages"] = ["RESEARCH", "A FABRICATED MIDDLE STAGE", "LARGE-SCALE DEPLOYMENT"]
    storyboard["scenes"].insert(1, tampered)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is False


_TITLE = "Why Deploying Physical AI at Scale Demands Safety at Every Layer."


def test_source_fidelity_passes_when_hero_kicker_matches_fresh_re_derivation(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["title"] = _TITLE
    storyboard["scenes"][0]["kicker"] = _derive_hero_kicker(_TITLE)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is True


def test_source_fidelity_fails_when_hero_kicker_was_hand_edited(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["title"] = _TITLE
    storyboard["scenes"][0]["kicker"] = "A Completely Different Made Up Phrase"
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is False


def test_source_fidelity_passes_when_comparison_header_matches_fresh_re_derivation(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["title"] = _TITLE
    scene = _comparison_scene()
    scene["header"] = _comparison_header(_TITLE, scene["narration_text"], scene["left"], scene["right"])
    storyboard["scenes"].insert(1, scene)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is True


def test_source_fidelity_fails_when_comparison_header_was_hand_edited(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["title"] = _TITLE
    scene = _comparison_scene()
    scene["header"] = "AUTONOMOUS VEHICLES VS INDUSTRIAL ROBOTS"
    storyboard["scenes"].insert(1, scene)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is False


def test_source_fidelity_skips_kicker_and_header_re_derivation_when_title_is_absent(tmp_path):
    """
    Conditionally exercised, matching checks 9/10's own philosophy for
    stories without comparison/statistic scenes: a storyboard that
    (unusually) omits the top-level `title` field simply isn't checked
    on this sub-rule -- it must not produce a false failure.
    """
    storyboard = _fake_storyboard()
    storyboard["scenes"][0]["kicker"] = "Whatever Kicker Was Stored"
    scene = _comparison_scene()
    scene["header"] = "Whatever Header Was Stored"
    storyboard["scenes"].insert(1, scene)
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "source_fidelity")["passed"] is True


# ---------------------------------------------------------------------
# Phase 3A: check 8 generalized to any scene type's states list, not
# just comparison.
# ---------------------------------------------------------------------

def test_scene_visual_duration_passes_for_a_generalized_non_comparison_split(tmp_path):
    """Direct regression shape for Story #54's concept_1 (12.39s, 8.0s cap) after the Phase 3A fix."""
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, {
        "scene_id": "concept_1", "scene_type": "concept", "order": 1,
        "start": 4.02, "end": 16.41, "duration": 12.39,
        "narration_text": "Clean energy isn't hard to come by, but the pace of large-scale adoption has historically been slow.",
        "motion": {"type": "headline_reveal", "states": [
            {"name": "segment_1", "duration": 6.195, "is_ramp": False, "zoom": 1.0},
            {"name": "segment_2", "duration": 6.195, "is_ramp": False, "zoom": 1.03},
        ]},
        "source_segment_indices": [1],
    })
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_visual_duration")["passed"] is True


def test_scene_visual_duration_still_fails_a_non_comparison_scene_with_no_states_over_cap(tmp_path):
    """Confirms the check still catches an over-cap scene that (incorrectly) has no states list at all."""
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, {
        "scene_id": "concept_1", "scene_type": "concept", "order": 1,
        "start": 4.02, "end": 16.41, "duration": 12.39,
        "narration_text": "text", "motion": {"type": "headline_reveal"},
        "source_segment_indices": [1],
    })
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "scene_visual_duration")["passed"] is False
    assert "concept_1" in _check(checks, "scene_visual_duration")["detail"]


# ---------------------------------------------------------------------
# Phase 3B: caption safety checks (12-15) -- direct regression checks
# for the Broader Validation's confirmed caption-overflow finding
# (Story #19's 97.4s single cue, Story #52's 19.44s single cue).
# ---------------------------------------------------------------------

def _well_segmented_scene(**overrides):
    scene = {
        "scene_id": "hero_0", "scene_type": "hero", "order": 0,
        "start": 0.0, "end": 10.39, "duration": 10.39, "silent": False,
        "narration_text": (
            "From Enablement to Execution, Egypt's AI Ecosystem Reaches Production Scale. "
            "Today, Egypt's AI builders gathered for a reception."
        ),
        "narration_segments": [
            {"text": "From Enablement to Execution, Egypt's AI Ecosystem Reaches Production Scale.", "start": 0.0, "end": 5.39},
            {"text": "Today, Egypt's AI builders gathered for a reception.", "start": 5.39, "end": 10.39},
        ],
        "source_segment_indices": [0, 1],
    }
    scene.update(overrides)
    return scene


def test_caption_safety_checks_all_pass_for_a_well_segmented_scene(tmp_path):
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _well_segmented_scene())
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "caption_cue_duration")["passed"] is True
    assert _check(checks, "caption_cue_word_count")["passed"] is True
    assert _check(checks, "caption_cue_line_count")["passed"] is True
    assert _check(checks, "caption_segmentation_coverage")["passed"] is True


def test_caption_cue_duration_fails_for_an_unsplittable_long_single_word_cue(tmp_path):
    """A single real word spanning an unusually long real duration, with no clause marker to split at."""
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _well_segmented_scene(
        narration_segments=[{"text": "Hello.", "start": 0.0, "end": 10.0}],
        duration=10.0, end=10.0,
    ))
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "caption_cue_duration")["passed"] is False


def test_caption_cue_word_count_fails_for_a_dense_fast_cue(tmp_path):
    """25 real words, no clause markers to split at, spoken in a short real duration -- duration passes, word count fails."""
    text = (
        "one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo "
        "twentythree twentyfour twentyfive"
    )
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _well_segmented_scene(
        narration_segments=[{"text": text, "start": 0.0, "end": 3.0}],
        duration=3.0, end=3.0,
    ))
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "caption_cue_duration")["passed"] is True
    assert _check(checks, "caption_cue_word_count")["passed"] is False


def test_caption_cue_line_count_fails_for_long_words_that_wrap_past_the_budget(tmp_path):
    """Few real words (well under the word-count budget) that are individually long enough to wrap past 3 lines."""
    text = "Internationalization implementations characteristically miscommunicated disproportionately counterproductively unconventionally"
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _well_segmented_scene(
        narration_segments=[{"text": text, "start": 0.0, "end": 3.0}],
        duration=3.0, end=3.0,
    ))
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "caption_cue_word_count")["passed"] is True
    assert _check(checks, "caption_cue_line_count")["passed"] is False


def test_caption_segmentation_coverage_fails_when_a_long_scene_has_only_one_cue(tmp_path):
    """
    The scene's own real duration exceeds the cap but produced only
    one cue -- the direct regression shape for Story #19/#52 before
    the Phase 3B fix (a long merged scene with no real per-sentence
    segmentation at all).
    """
    storyboard = _fake_storyboard()
    storyboard["scenes"].insert(1, _well_segmented_scene(
        duration=9.0, end=9.0,
        narration_segments=[{"text": "Short real text.", "start": 0.0, "end": 3.0}],
    ))
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    assert _check(checks, "caption_segmentation_coverage")["passed"] is False


def test_caption_safety_checks_skip_silent_scenes(tmp_path):
    """A silent scene (source_card/takeaway, no narration audio) is never caption-checked."""
    storyboard = _fake_storyboard()
    storyboard["scenes"][1]["silent"] = True
    checks = run_storyboard_qa_checks(storyboard, tmp_path, tmp_path / "missing.mp4")
    for check_name in ("caption_cue_duration", "caption_cue_word_count", "caption_cue_line_count", "caption_segmentation_coverage"):
        assert _check(checks, check_name)["passed"] is True
