"""Scenario 12 (conflicting sources) plus same-day corroboration rules."""
from app.content.briefing.pipeline import Corroborating, compose_briefing
from tests.briefing import samples as S


def test_scenario_12_conflicting_numbers_are_dropped_and_recorded():
    primary = "Nova Labs said Atlas-2 scored 86.4 percent on the MMLU benchmark."
    r = compose_briefing(S.HEADLINE, primary, None, [Corroborating("Other Outlet", S.SIBLING_CONFLICT)])
    assert "91.0" not in r.script_text
    assert "86.4" in r.script_text
    assert r.conflicts and r.conflicts[0]["source"] == "Other Outlet"


def test_agreeing_sibling_can_fill_a_primary_with_no_event_sentence():
    r = compose_briefing(S.HEADLINE, S.RSS_TEASER, None, [Corroborating("Reuters", S.SIBLING_AGREE)])
    assert r.corroborated is True
    assert "Nova Labs released Atlas-2" in r.script_text
    assert r.quality.gates == []


def test_sibling_is_not_used_when_the_primary_already_has_an_event_sentence():
    r = compose_briefing(S.HEADLINE, S.THIN_RSS, None, [Corroborating("Reuters", S.SIBLING_AGREE)])
    assert r.corroborated is False


def test_corroboration_is_not_used_when_the_primary_is_already_sufficient():
    r = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE, [Corroborating("Reuters", S.SIBLING_AGREE)])
    assert r.corroborated is False


def test_a_useless_sibling_cannot_rescue_a_story_with_no_primary_text():
    r = compose_briefing(S.HEADLINE, S.RSS_TEASER, None, [Corroborating("Reuters", "Short stub.")])
    assert r.script_text == ""
