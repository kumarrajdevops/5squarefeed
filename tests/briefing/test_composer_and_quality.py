"""Scenarios 2, 6, 8, 9, 10, 11, 13, 15, 16, 17: composition and the quality gate."""
from app.content.briefing.composer import MAX_BODY_WORDS, _compress
from app.content.briefing.pipeline import compose_briefing
from app.content.briefing.quality import FAIL, PASS, REVIEW, evaluate
from app.content.briefing.textutil import word_count
from tests.briefing import samples as S


def test_scenario_02_full_article_script_is_grounded_and_headline_first():
    r = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    assert r.script_text.startswith("Nova Labs releases Atlas-2 open model. ")
    assert r.quality.status in (PASS, REVIEW)
    assert r.quality.gates == []
    by_name = {c["check"]: c for c in r.quality.checks}
    assert by_name["unsupported_claims"]["passed"] and by_name["boilerplate_detected"]["passed"]


def test_scenario_06_long_sentence_is_compressed_without_losing_numbers():
    out = _compress(S.COMPRESSIBLE_SENTENCE)
    assert word_count(out) < word_count(S.COMPRESSIBLE_SENTENCE)
    assert "15 trillion" in out and out.endswith(".")


def test_scenario_08_short_meaningful_script_is_not_failed_for_length():
    r = compose_briefing(S.HEADLINE, "", S.SHORT_DENSE_ARTICLE)
    assert r.script_text
    assert r.quality.body_words < 15
    assert r.quality.status == PASS


def test_scenario_09_one_sentence_body_within_the_ideal_range_passes():
    r = compose_briefing(S.HEADLINE, "", S.IDEAL_ARTICLE)
    assert 15 <= r.quality.body_words <= 25
    assert r.quality.status == PASS
    assert r.quality.sentences == 2


def test_scenario_10_body_over_35_words_is_never_pass_and_composer_never_emits_it():
    long_body = " ".join(["Nova Labs released Atlas-2 with 70 billion parameters on Tuesday, and Azure will host it."] * 4)
    report = evaluate(S.HEADLINE, f"{S.HEADLINE}. {long_body}", [S.STRONG_ARTICLE], "sufficient")
    assert report.body_words > MAX_BODY_WORDS
    assert report.status == FAIL
    assert "body exceeds 35 words" in report.gates
    r = compose_briefing(S.HEADLINE, "", S.IDEAL_ARTICLE + " " + S.STRONG_ARTICLE)
    assert r.quality.body_words <= MAX_BODY_WORDS


def test_scenario_11_why_it_matters_is_never_selected():
    r = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    assert "could" not in r.script_text.lower() and "analysts" not in r.script_text.lower()
    assert "WHY_IT_MATTERS" not in r.roles


def test_scenario_13_headline_repetition_fails_the_gate():
    script = f"{S.HEADLINE}. Nova Labs releases the Atlas-2 open model. The Atlas-2 open model is released by Nova Labs."
    report = evaluate(S.HEADLINE, script, [script], "sufficient")
    assert report.status == FAIL
    assert "body repeats the headline" in report.gates


def test_scenario_15_unsupported_claim_fails_the_gate():
    script = (
        f"{S.HEADLINE}. Nova Labs said Atlas-2 beat GPT-9 by 300 percent on every benchmark. "
        "The model supports a context window of 256,000 tokens."
    )
    report = evaluate(S.HEADLINE, script, [S.STRONG_ARTICLE], "sufficient")
    assert report.status == FAIL
    assert "unsupported claim" in report.gates
    assert any(c["check"] == "unsupported_claims" and not c["passed"] for c in report.checks)


def test_scenario_16_story_with_no_event_beyond_the_headline_is_never_narrated():
    r = compose_briefing(S.HEADLINE, S.RSS_TEASER, None)
    assert r.script_text == ""
    assert r.quality.status == FAIL
    assert r.generation_reason().startswith("insufficient for briefing")


def test_scenario_17_short_event_keeps_its_facts_and_stays_short():
    r = compose_briefing(S.HEADLINE, "", S.SHORT_DENSE_ARTICLE)
    assert "70 billion" in r.script_text and "Tuesday" in r.script_text
    assert r.quality.distinct_facts >= 2
    assert r.quality.body_words == 10


def test_truncated_script_is_failed_by_the_gate():
    script = f"{S.HEADLINE}. Nova Labs released Atlas-2 with 70 billion parameters and the"
    assert evaluate(S.HEADLINE, script, [S.STRONG_ARTICLE], "sufficient").status == FAIL


def test_sentence_count_includes_headline_and_stays_in_range():
    r = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    assert r.quality.sentences in (2, 3)


def test_insufficient_script_reports_a_clean_reason():
    r = compose_briefing(S.HEADLINE, S.RSS_TEASER, None)
    assert r.script_text == ""
    assert r.generation_reason().startswith("insufficient for briefing")
    assert "body repeats the headline" not in r.quality.gates
