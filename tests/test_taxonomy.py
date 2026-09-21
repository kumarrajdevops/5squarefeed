from app.extraction.taxonomy import (
    BUSINESS,
    DEVELOPER_TOOLS,
    MAJOR_NEWS,
    RESEARCH,
    SECURITY_POLICY,
    classify_category,
)


def test_research_event_wins():
    category = classify_category(
        title="New paper benchmarks reasoning models",
        events=["research"],
        source_type="rss",
    )
    assert category == RESEARCH


def test_lawsuit_regulatory_event_maps_to_security_policy():
    category = classify_category(
        title="Regulator sues AI company over data practices",
        events=["lawsuit_regulatory"],
        source_type="rss",
    )
    assert category == SECURITY_POLICY


def test_safety_policy_event_maps_to_security_policy():
    category = classify_category(
        title="A warning about model safety",
        events=["safety_policy"],
        source_type="rss",
    )
    assert category == SECURITY_POLICY


def test_funding_event_maps_to_business():
    category = classify_category(
        title="Startup raises $40 million",
        events=["funding"],
        source_type="rss",
    )
    assert category == BUSINESS


def test_research_takes_priority_over_business_when_both_match():
    # A story can match more than one event category -- research wins
    # per the documented priority order in app/extraction/taxonomy.py.
    category = classify_category(
        title="Research lab raises funding to build a new model",
        events=["research", "funding"],
        source_type="rss",
    )
    assert category == RESEARCH


def test_show_hn_prefix_is_developer_tools_regardless_of_events():
    category = classify_category(
        title="Show HN: A CLI tool for local LLM inference",
        events=[],
        source_type="hackernews",
    )
    assert category == DEVELOPER_TOOLS


def test_show_hn_prefix_beats_a_coincidental_research_event_match():
    # Real bug, caught live: a real "Show HN:" post about a quantized
    # model's speed/accuracy numbers tripped fact_extractor.py's
    # "research" event keyword ("benchmark") in its body text, and got
    # misclassified as Research instead of Developer/Tools. The title
    # convention must win over a coincidental body-text keyword hit.
    category = classify_category(
        title="Show HN: Swift-Qwen3.8-27B, -58.3% thinking, x1.95 speed, accuracy of xhigh",
        events=["research"],
        source_type="hackernews",
    )
    assert category == DEVELOPER_TOOLS


def test_hackernews_source_without_stronger_signal_falls_back_to_developer_tools():
    category = classify_category(
        title="An essay about AI and society",
        events=[],
        source_type="hackernews",
    )
    assert category == DEVELOPER_TOOLS


def test_hackernews_submission_of_real_news_still_classifies_on_content():
    # A generic HN submission linking to mainstream coverage should be
    # classified on its actual content, not just because it came via
    # HN -- source_type is only a fallback, not an override.
    category = classify_category(
        title="Regulator sues AI company over data practices",
        events=["lawsuit_regulatory"],
        source_type="hackernews",
    )
    assert category == SECURITY_POLICY


def test_default_is_major_news():
    category = classify_category(
        title="OpenAI details new agent incidents",
        events=[],
        source_type="rss",
    )
    assert category == MAJOR_NEWS
