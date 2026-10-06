import dataclasses

import pytest

from app.filters.classification_rules import (
    AI_ADJACENT,
    AI_CORE,
    AI_NONE,
    CANDIDATE,
    REJECT,
    REVIEW,
    RULES_VERSION,
    classify,
)


# ---------------------------------------------------------------- candidate
@pytest.mark.parametrize("title", [
    "OpenAI launches a new reasoning model",
    "Anthropic releases Claude update",
    "Google DeepMind unveils new robotics model",
    "Meta adds AI summaries to Instagram",
    "EU bans AI-generated political ads",
])
def test_ai_title_with_development_verb_is_candidate(title):
    r = classify(title, "Some summary text.")
    assert r.disposition == CANDIDATE
    assert r.ai_relatedness == AI_CORE
    assert r.content_flag is None


def test_candidate_reason_is_explainable():
    r = classify("OpenAI launches GPT update", "")
    assert "title" in r.reason and "development verb" in r.reason


# ---------------------------------------------------------------- review
def test_ai_title_without_development_verb_is_review():
    r = classify("OpenAI and the future of chatbots", "An overview.")
    assert r.disposition == REVIEW
    assert r.ai_relatedness == AI_CORE


def test_ai_term_only_in_summary_is_review():
    r = classify("Startup closes funding round", "The company builds LLM tooling for developers.")
    assert r.disposition == REVIEW
    assert r.ai_relatedness == AI_CORE
    assert "summary" in r.reason


def test_adjacent_term_only_is_review():
    r = classify("Humanoid robots enter factories", "Warehouses are testing them.")
    assert r.disposition == REVIEW
    assert r.ai_relatedness == AI_ADJACENT


def test_opinion_with_ai_is_review_not_reject():
    r = classify("Why we need to slow down AI", "")
    assert r.disposition == REVIEW
    assert r.content_flag == "opin"


def test_roundup_with_ai_is_review():
    r = classify("This week in AI: new models and funding", "")
    assert r.disposition == REVIEW
    assert r.content_flag == "round"


def test_personal_voice_with_ai_is_review():
    r = classify("I built a chatbot over the weekend", "")
    assert r.disposition == REVIEW
    assert r.content_flag == "opin"


# ---------------------------------------------------------------- reject
def test_no_ai_terms_is_rejected():
    r = classify("City council approves new bike lanes", "Construction starts in spring.")
    assert r.disposition == REJECT
    assert r.ai_relatedness == AI_NONE
    assert r.content_flag is None


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


def test_opinion_without_ai_is_rejected():
    r = classify("Why we need to rethink city parking", "")
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
    # "tokens" is a term; "tokenize" and "agentless" are not whole-word hits.
    assert classify("We tokenize everything", "").ai_relatedness == AI_NONE
    assert classify("Agentless deployment tool arrives", "").ai_relatedness == AI_NONE


def test_keyword_set_from_existing_classifier_is_used():
    assert classify("Mistral releases a model", "").ai_relatedness == AI_CORE
    assert classify("DeepSeek launches update", "").disposition == CANDIDATE


# ---------------------------------------------------------------- result shape / robustness
def test_result_carries_version_and_is_immutable():
    r = classify("OpenAI launches a model", "")
    assert r.version == RULES_VERSION == "rules-v1"
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
