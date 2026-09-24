import json
import types

from app.content.storyboard_generator import (
    HEADER_FILLER_PREFIX_RE,
    PROJECTED_DEPLOYMENT_HEADER,
    SCENE_VISUAL_DURATION_CAPS,
    _build_comparison_motion_states,
    _build_static_states,
    _comparison_header,
    _derive_comparison_header,
    _derive_hero_kicker,
    _extract_entity_field,
    _extract_progression_stages,
    _extract_stat_fields,
    _infer_icon,
    _shorten,
    _split_hold_states,
    _split_static_hold,
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

    # The real story title is now persisted at the top level -- lets
    # QA independently re-derive hero.kicker/comparison.header without
    # re-querying the database (Master Storyboard Specification §6/§16).
    assert storyboard["title"] == STORY_51_TITLE

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

    statistic_scenes = [s for s in storyboard["scenes"] if s["scene_type"] == "statistic"]
    assert len(statistic_scenes) == 1
    # A real extractable value -> the count-up visual, not the plain
    # headline fallback.
    assert statistic_scenes[0]["visual_mode"] == "count_up"
    assert statistic_scenes[0]["motion"]["type"] == "count_up"


def test_statistic_scene_falls_back_to_headline_visual_mode_without_an_extractable_value():
    """
    A segment can carry a numeric SIGNAL (a bare year) strong enough to
    classify as `statistic` without actually stating an extractable
    VALUE (no million/billion/%/x/comma-grouped shape) -- this must
    fall back to the plain headline visual_mode/motion, never a
    count-up animation with nothing real to count up to.
    """
    segments = [
        {"text": "Some intro sentence with no numbers here at all.", "start": 0.0, "end": 3.0},
        {"text": "In 2024 the company shipped a completely redesigned interface.", "start": 3.0, "end": 6.0},
    ]
    item, state, content = _fake_story(caption_segments=segments, extracted_facts={}, audio_duration_seconds=6.0)

    storyboard = generate_storyboard(item, state, content)

    statistic_scenes = [s for s in storyboard["scenes"] if s["scene_type"] == "statistic"]
    assert len(statistic_scenes) == 1
    assert statistic_scenes[0]["stat"] is None
    assert statistic_scenes[0]["visual_mode"] == "headline"
    assert statistic_scenes[0]["motion"] == {"type": "headline_reveal"}


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


def test_extract_stat_fields_recognizes_a_dollar_amount():
    fields = _extract_stat_fields("The startup raised $50 million in its latest funding round.")
    assert fields["stat"] == "$50"
    assert fields["unit"] == "M"
    assert fields["entity"] == "In its latest funding round."


def test_extract_stat_fields_recognizes_a_percentage():
    fields = _extract_stat_fields("Adoption grew 40% year over year among enterprise customers.")
    assert fields["stat"] == "40"
    assert fields["unit"] == "%"


def test_extract_stat_fields_recognizes_a_multiplier():
    fields = _extract_stat_fields("The new chip is 10x faster than its predecessor.")
    assert fields["stat"] == "10"
    assert fields["unit"] == "x"


def test_extract_stat_fields_recognizes_an_already_comma_grouped_count():
    fields = _extract_stat_fields("The platform now serves 1,234 enterprise customers worldwide.")
    assert fields["stat"] == "1234"
    assert fields["unit"] == ""


def test_extract_stat_fields_never_matches_a_bare_year_as_a_value():
    """
    A bare 4-digit year (no comma grouping, no scale word, no $/%/x)
    must never be mistaken for a statistic value -- it's a date, not a
    count. Confirms the widened extraction doesn't introduce a false
    positive the original million/billion/thousand-only match never had.
    """
    fields = _extract_stat_fields("In 2024 the company shipped a completely redesigned interface.")
    assert fields["stat"] is None
    assert fields["date"] == "2024"


def test_extract_stat_fields_story_51_numeric_output_is_unchanged_by_the_widening():
    """
    Regression pin: Story #51's own real comparison clauses (plain
    "<number> million" phrasing, no $/%/x/comma-grouping) must produce
    byte-identical stat/unit output after widening _extract_stat_fields
    -- the widening only ADDS new recognized shapes, it never changes
    how the original shape is parsed.
    """
    left = _extract_stat_fields(
        "By 2035, ABI Research projects an installed base of 49 million level 3-5 autonomous vehicles (AVs)"
    )
    right = _extract_stat_fields(
        " Omdia estimates that roughly 60 million industrial robots will be deployed between 2026 and 2035."
    )
    assert (left["stat"], left["unit"]) == ("49", "M")
    assert (right["stat"], right["unit"]) == ("60", "M")


def test_hero_kicker_negation_guard_widens_the_cutoff_to_preserve_meaning():
    """
    Direct regression for the hero negation-truncation risk: a title
    whose tight 5-word cutoff would silently drop a real negation word
    ("Not") widens to the general 9-word cap instead -- still a strict
    PREFIX of the same real words, never reordered or added.
    """
    title = "Company Announces Product That Does Not Work As Advertised"

    kicker = _derive_hero_kicker(title)

    assert "Not" in kicker
    assert kicker.split() == title.split()  # no truncation needed once widened to 9 (title is exactly 9 words)


def test_hero_kicker_negation_guard_does_not_widen_when_negation_is_already_within_the_tight_cutoff():
    """
    The guard only widens when the negation word would otherwise be
    DROPPED -- if it already falls within the tight 5-word prefix,
    the tight cutoff stays, matching the ordinary _derive_hero_kicker
    behavior with no widening needed.
    """
    title = "Study Finds No Evidence Of Any Real Risk Here"

    kicker = _derive_hero_kicker(title)

    assert kicker == "Study Finds No Evidence Of…"


def test_hero_kicker_negation_guard_never_reorders_or_adds_words():
    """
    No negation-guard case ever produces a word that isn't a real,
    in-order prefix of the (filler-prefix-stripped) title -- proves
    the guard only ever widens the cutoff, never reorders or invents.
    """
    for title in [
        "Company Announces Product That Does Not Work As Advertised",
        "Why Deploying Physical AI at Scale Demands Safety at Every Layer.",
        "A Completely Ordinary Headline With No Negation At All In It",
    ]:
        kicker = _derive_hero_kicker(title)
        kicker_words = kicker.rstrip("…").split()
        source_words = HEADER_FILLER_PREFIX_RE.sub("", title.strip()).split()
        assert kicker_words == source_words[:len(kicker_words)]


# ---------------------------------------------------------------------
# Phase 3A regression tests (Master Storyboard Specification, approved
# after the 5-story validation surfaced these as real, demonstrated
# findings -- not speculative additions).
# ---------------------------------------------------------------------

def test_shorten_abandons_a_too_short_comma_fragment_for_story_72s_real_title_shape():
    """
    Direct regression for the Phase 2 finding: Story #72's real title
    ("In September, AI generated code has made up 17.25% of all Linux
    Kernel patches") produced a near-content-free 2-word kicker ("In
    September") because _shorten split on the first comma before
    applying the word cap. The pre-comma fragment is too short (<=3
    words) to be meaningful, so the cap now applies to the full text.
    """
    title = "In September, AI generated code has made up 17.25% of all Linux Kernel patches"

    kicker = _derive_hero_kicker(title)  # max_words=5 by default

    assert kicker != "In September"
    assert "AI generated code" in kicker
    # Still only ever a real, in-order PREFIX -- never reordered/added.
    assert kicker.rstrip("…").split() == title.split()[:len(kicker.rstrip("…").split())]


def test_shorten_still_uses_a_meaningful_comma_fragment_when_long_enough():
    """
    The guard only fires for a TOO-SHORT fragment -- a comma-separated
    prefix with enough real words to stand on its own is kept exactly
    as before (no regression to the original, working comma-split
    behavior for a normal case).
    """
    text = "The company announced a major restructuring plan, cutting costs across every division."

    shortened = _shorten(text, max_words=9)

    assert shortened == "The company announced a major restructuring plan"


def test_shorten_comma_guard_never_fires_when_there_is_no_comma_at_all():
    text = "A completely ordinary sentence with no punctuation splitting it whatsoever today."
    assert _shorten(text, max_words=9) == "A completely ordinary sentence with no punctuation splitting it…"


def test_entity_stop_re_widening_fixes_story_72s_real_dangling_which():
    """
    Direct regression for the Phase 2 finding: Story #72's real
    statistic clause produced entity="Code submissions to the Linux
    Kernel which" (a dangling relative pronoun) because the original
    stop set only had "were", positioned after "which" in the real
    sentence. "which" now stops the entity extraction earlier.
    """
    entity = _extract_entity_field(" code submissions to the Linux Kernel which were written by AI.")
    assert entity == "Code submissions to the Linux Kernel"
    assert "which" not in entity.lower()


def test_entity_stop_re_widening_fixes_story_41s_real_run_on_entity():
    """
    Direct regression for the Phase 2 finding: Story #41's real
    statistic clause produced a long run-on entity ("To settle claims
    that it failed to deliver an AI-upgraded Siri - and now") because
    neither "that" nor a real dash-clause boundary stopped it early.
    "that" now stops it immediately; the hyphen inside "AI-upgraded"
    (no surrounding spaces) must NOT be mistaken for the same boundary.
    """
    entity = _extract_entity_field(
        " to settle claims that it failed to deliver an AI-upgraded Siri - and now, "
        "eligible iPhone owners can submit a claim for a payout."
    )
    assert entity == "To settle claims"
    assert "that" not in entity.lower()


def test_entity_stop_re_does_not_treat_a_hyphenated_compound_word_as_a_stop_boundary():
    """A real hyphenated compound word (no surrounding spaces) is real source text and must survive intact."""
    entity = _extract_entity_field(" an AI-upgraded assistant shipped today.")
    assert "AI-upgraded" in entity


def test_split_static_hold_stays_single_when_under_the_cap():
    states = _split_static_hold(4.0, cap=6.0, name_prefix="segment")
    assert states == [{"name": "segment_1", "duration": 4.0, "is_ramp": False, "zoom": 1.0}]


def test_split_static_hold_splits_and_sums_exactly_with_distinct_zoom_per_segment():
    states = _split_static_hold(12.39, cap=8.0, name_prefix="segment")
    assert [s["name"] for s in states] == ["segment_1", "segment_2"]
    assert all(s["duration"] <= 8.0 for s in states)
    assert abs(sum(s["duration"] for s in states) - 12.39) < 1e-9
    # Consecutive segments are never pixel-identical -- distinct zoom.
    zooms = [s["zoom"] for s in states]
    assert len(set(zooms)) == len(zooms)
    assert all(1.0 <= z <= 1.12 for z in zooms)


def test_build_static_states_preserves_the_real_countup_ramp_then_splits_the_remaining_hold():
    """
    A count-up-capable scene (e.g. statistic) whose duration exceeds
    its cap keeps its existing real ramp UNCHANGED as the first state
    -- only the static remainder is split. Direct regression for
    Story #41's statistic_1 (9.84s, 8.0s cap, 1.2s real countup).
    """
    states = _build_static_states(duration=9.84, cap=8.0, countup_seconds=1.2)

    assert states[0] == {"name": "reveal", "duration": 1.2, "is_ramp": True, "zoom": 1.0}
    assert all(not s["is_ramp"] for s in states[1:])
    assert abs(sum(s["duration"] for s in states) - 9.84) < 1e-9
    assert all(s["duration"] <= 8.0 for s in states)


def test_build_static_states_is_a_no_op_shape_under_the_cap():
    """A scene already under its cap gets exactly one state at the base zoom -- confirms zero effect on passing scenes."""
    states = _build_static_states(duration=4.02, cap=6.0, countup_seconds=None)
    assert states == [{"name": "segment_1", "duration": 4.02, "is_ramp": False, "zoom": 1.0}]


def test_scene_visual_duration_caps_cover_every_non_comparison_scene_type():
    for scene_type in ("hero", "takeaway", "source_card", "quote", "concept", "company", "product", "key_fact", "statistic"):
        assert scene_type in SCENE_VISUAL_DURATION_CAPS
    assert "comparison" not in SCENE_VISUAL_DURATION_CAPS
