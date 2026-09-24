import json
import types

from app.content.storyboard_generator import (
    PROJECTED_DEPLOYMENT_HEADER,
    _build_comparison_motion_states,
    _comparison_header,
    _derive_comparison_header,
    _extract_progression_stages,
    _infer_icon,
    _split_hold_states,
    generate_storyboard,
)

STORY_51_TITLE = "Why Deploying Physical AI at Scale Demands Safety at Every Layer."


def _fake_story(source_name="NVIDIA Blog", taxonomy_category="research",
                 caption_segments=None, extracted_facts=None,
                 audio_duration_seconds=None, story_id=51, title=STORY_51_TITLE):
    item = types.SimpleNamespace(id=story_id, source_name=source_name, title=title)
    state = types.SimpleNamespace(
        taxonomy_category=taxonomy_category,
        extracted_facts=json.dumps(extracted_facts if extracted_facts is not None else {}),
    )
    content = types.SimpleNamespace(
        caption_segments=json.dumps(caption_segments or []),
        audio_duration_seconds=audio_duration_seconds,
    )
    return item, state, content


# Real story 51 data, queried live from the dev DB this session.
STORY_51_SEGMENTS = [
    {"text": "Why Deploying Physical AI at Scale Demands Safety at Every Layer.", "start": 0.1, "end": 4.5625},
    {"text": "Physical AI is moving rapidly from research to large-scale deployment.", "start": 4.5125, "end": 9.1875},
    {
        "text": (
            "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
            "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
            "industrial robots will be deployed between 2026 and 2035."
        ),
        "start": 9.1875, "end": 23.4,
    },
]
STORY_51_FACTS = {"companies": [], "products": [], "events": ["research", "safety_policy"], "dates": [], "claims": []}


def test_story_51_real_content_produces_expected_scenes():
    """
    Real content, not a synthetic fixture -- confirms the engine
    genuinely detects structured content in an actual production
    story rather than a hand-built example. Segment 1 legitimately
    matches the reused extract_events() "research" category (a real
    signal, not a fabrication), producing 4 scenes rather than a
    hardcoded 3 -- exactly the "exercise whichever branches naturally
    apply" behavior this engine is meant to have.
    """
    item, state, content = _fake_story(
        caption_segments=STORY_51_SEGMENTS, extracted_facts=STORY_51_FACTS,
        audio_duration_seconds=23.448,
    )

    storyboard = generate_storyboard(item, state, content)

    scene_types = [s["scene_type"] for s in storyboard["scenes"]]
    assert scene_types == ["hero", "key_fact", "comparison", "source_card"]

    # Contiguous, non-overlapping timing, starting at 0.0.
    assert storyboard["scenes"][0]["start"] == 0.0
    for previous, current in zip(storyboard["scenes"], storyboard["scenes"][1:]):
        assert previous["end"] == current["start"]
    assert storyboard["total_duration_seconds"] == 23.448 + 3.0

    comparison = storyboard["scenes"][2]
    assert comparison["left"]["stat"] == "49"
    assert comparison["left"]["unit"] == "M"
    assert comparison["left"]["entity"] == "Autonomous vehicles"
    assert comparison["left"]["tier"] == "L3–L5"
    assert comparison["left"]["date"] == "By 2035"
    assert comparison["left"]["source"] == "ABI Research"
    assert comparison["right"]["stat"] == "60"
    assert comparison["right"]["unit"] == "M"
    assert comparison["right"]["entity"] == "Industrial robots"
    assert comparison["right"]["tier"] is None
    assert comparison["right"]["date"] == "2026–2035"
    assert comparison["right"]["source"] == "Omdia"

    # Never the full narration sentence as a rendered field.
    assert comparison["left"]["entity"] != comparison["narration_text"]

    # New visual-quality fields (this iteration): neutral structural
    # header (both sides carry a real date/timeframe field and the
    # narration itself uses "deploy" -- see _comparison_header), never
    # a truncated title fragment that could misread as "the stats
    # prove this claim"; generic category icons per side; no "VS".
    assert comparison["header"] == "PROJECTED DEPLOYMENT SCALE"
    assert "VS" not in comparison["header"].upper()
    assert comparison["visual_mode"] == "two_scale_signals"
    assert comparison["left"]["icon"] == "vehicle"
    assert comparison["right"]["icon"] == "robot"
    assert comparison["source_segment_indices"] == [2]

    # Motion states always sum exactly to the scene's own duration --
    # the invariant that makes the "long static hold" regression
    # impossible regardless of how many states are produced.
    states = comparison["motion"]["states"]
    assert abs(sum(s["duration"] for s in states) - comparison["duration"]) < 1e-9
    # Substantially equivalent treatment: both sides' reveal states
    # get the identical countup duration.
    reveal_states = {s["name"]: s for s in states if s["name"] in ("reveal_left", "reveal_right")}
    assert reveal_states["reveal_left"]["duration"] == reveal_states["reveal_right"]["duration"]

    hero = storyboard["scenes"][0]
    assert hero["kicker"] == "Deploying Physical AI at Scale…"
    assert hero["source_segment_indices"] == [0]

    key_fact = storyboard["scenes"][1]
    assert key_fact["stages"] == ["RESEARCH", "LARGE-SCALE DEPLOYMENT"]
    assert key_fact["visual_mode"] == "progression"
    assert key_fact["source_segment_indices"] == [1]

    source_card = storyboard["scenes"][-1]
    assert source_card["scene_type"] == "source_card"
    assert source_card["closing_line"] == "Full story: NVIDIA Blog"
    assert source_card["silent"] is True
    assert source_card["source_segment_indices"] == []


def test_generator_does_not_special_case_any_story_id():
    """
    Same shape of input, a completely different story_id/source --
    must produce the analogous result. Proves nothing in the
    generator is hardcoded to story 51 specifically.
    """
    item, state, content = _fake_story(
        story_id=999999, source_name="Some Other Blog",
        caption_segments=STORY_51_SEGMENTS, extracted_facts=STORY_51_FACTS,
        audio_duration_seconds=23.448,
    )

    storyboard = generate_storyboard(item, state, content)

    assert storyboard["story_id"] == 999999
    assert [s["scene_type"] for s in storyboard["scenes"]] == ["hero", "key_fact", "comparison", "source_card"]
    assert storyboard["scenes"][-1]["closing_line"] == "Full story: Some Other Blog"


def test_degenerate_all_non_numeric_narration_is_hero_plus_source_card():
    segments = [
        {"text": "A quiet day in AI news.", "start": 0.0, "end": 2.0},
        {"text": "Nothing major happened today.", "start": 2.0, "end": 4.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=4.0)

    storyboard = generate_storyboard(item, state, content)

    assert [s["scene_type"] for s in storyboard["scenes"]] == ["hero", "source_card"]
    assert storyboard["scenes"][0]["end"] == 4.0


def test_statistic_branch_fires_for_a_lone_numeric_segment():
    segments = [
        {"text": "Some intro sentence with no numbers here at all.", "start": 0.0, "end": 3.0},
        {"text": "The company raised 10 million dollars in new funding this week.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    assert "statistic" in [s["scene_type"] for s in storyboard["scenes"]]


def test_quote_branch_fires_for_a_quoted_segment():
    segments = [
        {"text": "An executive commented on the news today.", "start": 0.0, "end": 3.0},
        {"text": 'The CEO said, "This changes everything." according to reports', "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    assert "quote" in [s["scene_type"] for s in storyboard["scenes"]]


def test_company_branch_fires_when_a_known_company_is_mentioned():
    segments = [
        {"text": "Something happened in the industry today.", "start": 0.0, "end": 3.0},
        {"text": "OpenAI announced a new update to its platform.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    company_scenes = [s for s in storyboard["scenes"] if s["scene_type"] == "company"]
    assert len(company_scenes) == 1
    assert company_scenes[0]["company"] == "openai"


def test_product_branch_fires_when_a_known_product_is_mentioned():
    segments = [
        {"text": "Something happened in the industry today.", "start": 0.0, "end": 3.0},
        {"text": "The newest version of ChatGPT rolled out this week.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    assert any(s["scene_type"] == "product" for s in storyboard["scenes"])


def test_concept_branch_fires_for_technical_jargon():
    segments = [
        {"text": "Something happened in the industry today.", "start": 0.0, "end": 3.0},
        {"text": "The team redesigned the underlying neural network architecture.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    assert "concept" in [s["scene_type"] for s in storyboard["scenes"]]


def test_genuine_takeaway_is_detected_from_real_conclusion_wording():
    segments = [
        {"text": "A new AI tool launched today.", "start": 0.0, "end": 3.0},
        {"text": "Overall, this signals a shift in how developers build products.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    assert storyboard["scenes"][-1]["scene_type"] == "takeaway"


def test_no_genuine_conclusion_produces_a_plain_source_card_not_a_fabricated_takeaway():
    segments = [
        {"text": "A new AI tool launched today.", "start": 0.0, "end": 3.0},
        {"text": "It supports several new integrations.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(
        caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0, source_name="Example Source",
    )

    storyboard = generate_storyboard(item, state, content)

    last = storyboard["scenes"][-1]
    assert last["scene_type"] == "source_card"
    assert last["closing_line"] == "Full story: Example Source"
    assert last["silent"] is True


def test_extract_progression_stages_matches_a_real_from_to_sentence():
    stages = _extract_progression_stages("Physical AI is moving rapidly from research to large-scale deployment.")
    assert stages == ["RESEARCH", "LARGE-SCALE DEPLOYMENT"]


def test_extract_progression_stages_returns_none_without_a_real_from_to_pattern():
    """
    No fabricated middle stage: a sentence that doesn't state a real
    "from X to Y" relationship must never produce a progression --
    explicit regression for the "no safety-layers diagram invented
    from a title fragment" constraint.
    """
    assert _extract_progression_stages("Safety must be considered at every layer of the stack.") is None
    assert _extract_progression_stages("") is None


def test_infer_icon_matches_vehicle_and_robot_keywords_and_falls_back_to_generic():
    assert _infer_icon("Autonomous vehicles") == "vehicle"
    assert _infer_icon("Self-driving cars") == "vehicle"
    assert _infer_icon("Industrial robots") == "robot"
    assert _infer_icon("Humanoid robots") == "robot"
    assert _infer_icon("Cloud infrastructure spend") == "generic"
    assert _infer_icon(None) == "generic"


def test_build_comparison_motion_states_sums_to_duration_and_splits_long_holds():
    """
    A long comparison scene (14.26s, matching real story 51) must not
    produce a single unbroken hold beyond the QA duration cap -- the
    direct regression check for the original "long static hold after
    count-up" complaint.
    """
    states = _build_comparison_motion_states(duration=14.2605, countup_seconds=1.1)
    assert abs(sum(s["duration"] for s in states) - 14.2605) < 1e-9
    assert [s["name"] for s in states] == ["intro", "reveal_left", "reveal_right", "both_context", "hold_1", "hold_2"]
    for state in states:
        if state["name"].startswith("hold"):
            assert state["duration"] <= 5.0


def test_build_comparison_motion_states_falls_back_gracefully_for_a_short_scene():
    """
    A scene too short for the full 5-state shape degrades first by
    dropping both_context, then to the original 2-state ramp+hold
    shape -- never a negative-duration state.
    """
    states = _build_comparison_motion_states(duration=4.0, countup_seconds=1.0)
    assert all(s["duration"] >= 0 for s in states)
    assert abs(sum(s["duration"] for s in states) - 4.0) < 1e-9

    very_short_states = _build_comparison_motion_states(duration=1.5, countup_seconds=1.0)
    assert [s["name"] for s in very_short_states] == ["reveal_both", "hold"]
    assert abs(sum(s["duration"] for s in very_short_states) - 1.5) < 1e-9


def test_split_hold_states_stays_single_when_under_the_cap():
    states = _split_hold_states(3.0)
    assert states == [{"name": "hold", "duration": 3.0, "countup_seconds": None, "countup_side": None}]


def test_split_hold_states_splits_evenly_and_sums_exactly_when_over_the_cap():
    states = _split_hold_states(12.0)
    assert [s["name"] for s in states] == ["hold_1", "hold_2", "hold_3"]
    assert all(s["duration"] <= 5.0 for s in states)
    assert abs(sum(s["duration"] for s in states) - 12.0) < 1e-9


def test_comparison_header_uses_the_neutral_structural_label_for_a_real_deployment_projection():
    """
    Direct regression for the Story #51 header fix: both sides carry a
    real date/timeframe field and the real narration uses "deploy" --
    a title-truncated header would otherwise end mid-clause on a verb
    ("... Demands...") that can misread as "the stats below prove this
    claim". This rule is content-driven (keyed on the comparison's own
    real data shape), not on any specific story_id -- it fires for any
    future story with this same real shape too.
    """
    narration = (
        "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
        "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
        "industrial robots will be deployed between 2026 and 2035."
    )
    left = {"date": "By 2035"}
    right = {"date": "2026–2035"}

    header = _comparison_header("Why Deploying Physical AI at Scale Demands Safety at Every Layer.", narration, left, right)

    assert header == PROJECTED_DEPLOYMENT_HEADER
    assert "VS" not in header.upper()


def test_comparison_header_falls_back_to_title_truncation_without_the_real_deployment_shape():
    """
    No fabrication: when the comparison's own real data doesn't have
    both a date on each side AND a real "deploy" mention in the
    narration, the header stays the ordinary title-derived phrase --
    proves this isn't a per-story_id special case but a genuinely
    conditional, content-driven rule.
    """
    title = "Why Cloud Spending Keeps Rising Across the Industry."
    narration = "Company A spent 10 million dollars while Company B spent 20 million dollars."

    # Neither side has a date at all.
    assert _comparison_header(title, narration, {"date": None}, {"date": None}) == _derive_comparison_header(title)

    # Both sides have a date, but the narration never says "deploy".
    assert _comparison_header(title, narration, {"date": "2024"}, {"date": "2025"}) == _derive_comparison_header(title)
