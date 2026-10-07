import dataclasses

import pytest

from app.filters.classification_rules import (
    AI_ADJACENT,
    AI_CORE,
    AI_NONE,
    CANDIDATE,
    REJECT,
    RULES_VERSION,
    classify,
)


# ---------------------------------------------------------------- candidate: genuine AI development
@pytest.mark.parametrize("title", [
    "OpenAI launches a new reasoning model",
    "Anthropic releases Claude update",
    "Google DeepMind unveils new robotics model",
    "Meta adds AI summaries to Instagram",
    "EU bans AI-generated political ads",
    "Reflection debuts Beam, an open-weight AI model",
    "Google is about to remove free access to Gemini Flash",
    "California subpoenas OpenAI over rogue AI agents",
    "DeepSeek launches update",
    "Mistral releases a model",
])
def test_ai_title_with_development_is_candidate(title):
    r = classify(title, "Some summary text.")
    assert r.disposition == CANDIDATE
    assert r.ai_relatedness == AI_CORE
    assert r.content_flag is None


def test_candidate_reason_is_explainable():
    r = classify("OpenAI launches GPT update", "")
    assert "title" in r.reason and "development" in r.reason


# ---------------------------------------------------------------- candidate: real 2026-10-06 patterns
@pytest.mark.parametrize("title", [
    # "giving", "aims", "want to": development verbs the old regex did not inflect
    "Anthropic is giving startups a free year of Claude Team",
    "NetApp aims to make legacy data AI-ready",
    "Meta and Microsoft want to stop their employees using Claude",
    # restriction / policy language
    "Norway proposes ban on AI chatbots for children",
    "EU restricts AI use in hiring",
    # reported statements about an AI company
    "Amazon warns local communities about its AI data centers",
])
def test_real_1006_patterns_are_candidates(title):
    r = classify(title, "")
    assert r.disposition == CANDIDATE
    assert r.ai_relatedness == AI_CORE


def test_ai_term_only_in_summary_with_a_development_verb_in_the_title_is_candidate():
    # Licence / pay / partner news where the AI context sits in the summary.
    r = classify(
        "Qualcomm will pay Huawei to license its patents",
        "The deal covers chips for AI phones and LLM inference.",
    )
    assert r.disposition == CANDIDATE
    assert "summary" in r.reason


def test_html_in_the_summary_is_ignored():
    r = classify(
        "Qualcomm will pay Huawei to license its patents",
        '<p>Chips for <a href="https://example.test/openai-llm">phones</a>.</p>',
    )
    assert r.disposition == REJECT


def test_summary_term_without_a_development_in_the_title_is_rejected():
    r = classify("Startup profile and company overview", "The company builds LLM tooling.")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_CORE


# ---------------------------------------------------------------- reject: AI term without development
@pytest.mark.parametrize("title", [
    "OpenAI and the future of chatbots",
    "The AI bubble: an explainer",
    "OpenAI",
])
def test_ai_term_without_development_is_rejected(title):
    r = classify(title, "An overview.")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_CORE
    assert "no concrete development" in r.reason


# ---------------------------------------------------------------- reject: adjacent tech
def test_adjacent_term_only_is_rejected():
    r = classify("Humanoid robots enter factories", "Warehouses are testing them.")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_ADJACENT


def test_chip_smuggling_without_an_ai_development_is_rejected():
    r = classify("Nvidia chip smuggling arrests", "")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_ADJACENT


# ---------------------------------------------------------------- reject: no AI
def test_no_ai_terms_is_rejected():
    r = classify("City council approves new bike lanes", "Construction starts in spring.")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_NONE
    assert r.content_flag is None


# ---------------------------------------------------------------- reject: non-news form
@pytest.mark.parametrize("title,flag", [
    ("Best AI laptop deals this week", "deal"),
    ("How to watch the OpenAI keynote live stream", "deal"),
    ("Register now for the AI Summit", "event"),
    ("How to build an agent with Claude", "tut"),
    ("Claude vs ChatGPT: which is better", "rev"),
])
def test_non_news_pattern_is_rejected_even_with_ai_terms(title, flag):
    r = classify(title, "")
    assert r.disposition == REJECT
    assert r.content_flag == flag
    assert r.ai_relatedness == AI_CORE


def test_event_pattern_in_summary_rejects():
    r = classify("OpenAI launches a new model", "Tickets are on sale for the summit.")
    assert r.disposition == REJECT
    assert r.content_flag == "event"


@pytest.mark.parametrize("title,flag", [
    ("Why we need to slow down AI", "opin"),
    ("I built a chatbot over the weekend", "opin"),
    ("This week in AI: new models and funding", "round"),
    ("How I Use AI to Learn a new language", "ask"),
])
def test_opinion_personal_voice_roundup_and_tutorial_shapes_are_rejected(title, flag):
    r = classify(title, "")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_CORE
    assert r.content_flag == flag


def test_opinion_without_ai_is_rejected():
    r = classify("Why we need to rethink city parking", "")
    assert r.disposition == REJECT
    assert r.content_flag == "opin"


def test_pundit_says_shape_is_not_a_reported_development():
    # "X says AI is ..." is a person's view, not a development.
    r = classify("Sam Altman says AI is totally worth it", "")
    assert r.disposition == REJECT
    assert r.content_flag == "opin"


# ---------------------------------------------------------------- precedence / matching
def test_non_news_pattern_beats_opinion_pattern():
    r = classify("Best AI laptop deals: we need to talk", "")
    assert r.content_flag == "deal"
    assert r.disposition == REJECT


def test_bare_ai_is_case_sensitive():
    assert classify("Said the mayor to ail neighbours", "").ai_relatedness == AI_NONE
    assert classify("lowercase ai chatter", "").ai_relatedness == AI_NONE
    assert classify("A.I. startup raises money", "").ai_relatedness == AI_CORE


def test_whole_word_matching():
    assert classify("We tokenize everything", "").ai_relatedness == AI_NONE
    assert classify("Agentless deployment tool arrives", "").ai_relatedness == AI_NONE


# ---------------------------------------------------------------- binary result / robustness
@pytest.mark.parametrize("title,summary", [
    ("OpenAI and the future of chatbots", "An overview."),
    ("Humanoid robots enter factories", "Warehouses are testing them."),
    ("Startup closes funding round", "The company builds LLM tooling."),
    ("Why we need to slow down AI", ""),
    ("This week in AI: new models and funding", ""),
    ("OpenAI launches a model", ""),
    ("", ""),
])
def test_disposition_is_always_binary(title, summary):
    assert classify(title, summary).disposition in {CANDIDATE, REJECT}


def test_no_review_disposition_exists():
    import app.filters.classification_rules as rules

    assert not hasattr(rules, "REVIEW")


def test_result_carries_version_and_is_immutable():
    r = classify("OpenAI launches a model", "")
    assert r.version == RULES_VERSION == "rules-v3"
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.disposition = REJECT


def test_handles_missing_summary_and_empty_title():
    assert classify("OpenAI launches a model", None).disposition == CANDIDATE
    assert classify("", None).disposition == REJECT
    assert classify(None, None).disposition == REJECT


def test_deterministic():
    a = classify("Anthropic raises funds", "Summary")
    b = classify("Anthropic raises funds", "Summary")
    assert a == b
