"""Short-format rules: one event sentence, no minimum length, event over explanation,
no invented motive, no truncation, headline-only only as a last resort."""
from app.content.briefing.composer import MAX_BODY_WORDS, _compress
from app.content.briefing.events import EVENT, classify, headline_is_event
from app.content.briefing.pipeline import compose_briefing
from app.content.briefing.quality import FAIL, PASS, REVIEW, evaluate
from app.content.briefing.textutil import word_count

AGENTS_HEADLINE = "Report examines connecting AI agents to enterprise knowledge"
AGENTS_SOURCE = (
    "AI agents need this understanding to reason about situations, make decisions, and ultimately take actions. "
    "A report based on a survey of 300 technology executives examines connecting AI agents to enterprise knowledge. "
    "Knowledge graphs are a way of representing information as connected entities."
)

ADS_HEADLINE = "OpenAI to show more ads in ChatGPT"
ADS_SOURCE = (
    "OpenAI wants to increase advertising to monetize ChatGPT. "
    "OpenAI is adding more ads to ChatGPT, the company confirmed on Monday. "
    "Advertising could reshape how people use chatbots."
)


def test_explanation_is_not_chosen_over_the_report_event():
    r = compose_briefing(AGENTS_HEADLINE, "", AGENTS_SOURCE)
    assert "survey of 300" in r.script_text
    assert "need this understanding" not in r.script_text
    assert classify("AI agents need this understanding to reason about situations, make decisions, and ultimately take actions.") != EVENT


def test_event_is_chosen_and_motive_is_never_generated():
    r = compose_briefing(ADS_HEADLINE, "", ADS_SOURCE)
    assert "adding more ads" in r.script_text
    assert "monetize" not in r.script_text and "wants to" not in r.script_text
    assert "reshape" not in r.script_text


def test_background_and_significance_sentences_are_not_events():
    assert classify("Previously, the company had focused on enterprise customers for many years.") != EVENT
    assert classify("This means developers could ship faster and reshape the market.") != EVENT


def test_short_event_stays_short_and_passes():
    headline = "Acme opens Berlin office"
    source = "Acme hired 40 engineers for the new Berlin office on Monday. It has 40 staff."
    r = compose_briefing(headline, "", source)
    assert r.quality.body_words < 15
    assert r.quality.status in (PASS, REVIEW)
    assert r.script_text == "Acme opens Berlin office. Acme hired 40 engineers for the new Berlin office on Monday."


def test_never_more_than_two_body_sentences_and_never_over_35_words():
    for text in (
        "Nova Labs released Atlas-2 on Tuesday. The weights are available under an Apache 2.0 license from today. "
        "Azure will host the model starting in November. Google Cloud will also host the model in December.",
    ):
        r = compose_briefing("Nova Labs releases Atlas-2 open model", "", text)
        assert r.quality.body_words <= MAX_BODY_WORDS
        assert r.quality.sentences <= 3


def test_over_long_sentence_is_compressed_or_dropped_never_truncated():
    long = (
        "Nova Labs said on Tuesday that it released Atlas-2 with 70 billion parameters, an open-weight model "
        "trained on 15 trillion tokens of filtered web text, code and licensed books over nine weeks, "
        "which the company financed from internal funds."
    )
    out = _compress(long)
    assert out == long or (word_count(out) <= MAX_BODY_WORDS and out.endswith("."))
    r = compose_briefing("Nova Labs releases Atlas-2", "", long)
    assert r.quality.body_words <= MAX_BODY_WORDS
    assert r.quality.status != FAIL or r.script_text == ""


def test_compression_keeps_attribution_and_quotes():
    text = (
        "Chief executive Dana Reyes said the release \"puts frontier-level reasoning in the hands of every developer\" "
        "(a claim the company repeated at its annual developer conference in San Francisco on Tuesday afternoon), "
        "which analysts have not verified."
    )
    out = _compress(text, limit=25)
    assert "said" in out and "puts frontier-level reasoning in the hands of every developer" in out


def test_headline_repeat_is_omitted():
    script = "Nova Labs releases Atlas-2 open model. Nova Labs releases the Atlas-2 open model."
    report = evaluate("Nova Labs releases Atlas-2 open model", script, [script], "sufficient")
    assert report.status == FAIL


def test_headline_only_when_the_source_has_no_event_fact():
    headline = "Nova Labs releases Atlas-2 open model"
    source = (
        "Open models are language models whose weights anyone can download and run on their own hardware. "
        "Such models need large amounts of memory to reason about long documents and answer questions about them. "
        "Atlas-2 and open model releases like it matter because they could reshape the market for developers."
    )
    r = compose_briefing(headline, "", source)
    if r.headline_only:
        assert r.script_text == "Nova Labs releases Atlas-2 open model."
        assert r.quality.status != PASS
    else:
        assert r.script_text == ""


def test_headline_only_is_not_used_just_because_the_headline_is_an_event():
    r = compose_briefing("Nova Labs releases Atlas-2 open model", "", "Nova Labs released Atlas-2 on Tuesday with 70 billion parameters.")
    assert r.headline_only is False
    assert "70 billion" in r.script_text


def test_headline_only_needs_a_relevant_source_and_an_event_headline():
    assert headline_is_event("Nova Labs releases Atlas-2 open model")
    assert not headline_is_event("Why open models matter")
    r = compose_briefing("Why open models matter", "", "Open models are models whose weights anyone can download. They need memory to run.")
    assert r.headline_only is False and r.script_text == ""


def test_edited_in_explanation_cannot_pass_the_grader():
    body = "AI agents need this understanding to reason about situations, make decisions, and ultimately take actions."
    report = evaluate(AGENTS_HEADLINE, f"{AGENTS_HEADLINE}. {body}", [AGENTS_SOURCE], "sufficient")
    assert report.status == FAIL
    assert "body is not a news event" in report.gates
