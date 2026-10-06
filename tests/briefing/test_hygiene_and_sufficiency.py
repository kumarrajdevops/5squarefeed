"""Scenarios 1, 3, 4, 5, 7, 14: what counts as usable source text."""
from app.content.briefing.hygiene import clean_source, is_boilerplate
from app.content.briefing.sufficiency import INSUFFICIENT, SUFFICIENT, THIN, assess
from tests.briefing import samples as S


def test_scenario_01_rss_only_shallow_teaser_is_insufficient():
    a = assess(S.HEADLINE, S.RSS_TEASER, None)
    assert a.status == INSUFFICIENT
    assert a.source_kind == "rss"


def test_scenario_03_boilerplate_only_page_is_insufficient_and_stripped():
    cleaned = clean_source(S.BOILERPLATE_ARTICLE, S.HEADLINE)
    assert cleaned.sentences == []
    assert assess(S.HEADLINE, "", S.BOILERPLATE_ARTICLE).status == INSUFFICIENT


def test_scenario_04_duplicated_headline_adds_no_information():
    cleaned = clean_source(S.DUPLICATED_HEADLINE_ARTICLE, S.HEADLINE)
    assert cleaned.sentences == []
    assert assess(S.HEADLINE, "", S.DUPLICATED_HEADLINE_ARTICLE).status == INSUFFICIENT


def test_scenario_05_newsletter_signup_lines_are_dropped_but_the_news_is_kept():
    cleaned = clean_source(S.NEWSLETTER_TAIL, S.HEADLINE)
    joined = " ".join(cleaned.sentences).lower()
    assert "newsletter" not in joined and "sign up" not in joined
    assert "256,000" in joined and "apache 2.0" in joined
    assert is_boilerplate("Subscribe to our newsletter to get stories like this in your inbox.")


def test_scenario_07_insufficient_content_hacker_news_stub():
    a = assess("Show HN: a tool", S.HN_TEASER, None)
    assert a.status == INSUFFICIENT
    assert a.novel_sentences == 0


def test_scenario_14_truncated_rss_is_flagged_and_not_trusted():
    a = assess(S.HEADLINE, S.TRUNCATED_RSS, None)
    assert a.truncated is True
    assert a.status == INSUFFICIENT
    assert "[...]" not in " ".join(a.cleaned.sentences)


def test_full_article_is_sufficient():
    a = assess(S.HEADLINE, "", S.STRONG_ARTICLE)
    assert a.status == SUFFICIENT
    assert a.source_kind == "article"


def test_an_article_beats_a_worse_rss_summary():
    a = assess(S.HEADLINE, S.RSS_TEASER, S.STRONG_ARTICLE)
    assert a.source_kind == "article"


def test_short_but_dense_source_is_not_insufficient():
    assert assess(S.HEADLINE, "", S.SHORT_DENSE_ARTICLE).status in (SUFFICIENT, THIN)


def test_html_and_entities_are_cleaned_before_assessing():
    html = "<p>" + S.STRONG_ARTICLE.replace("Tuesday", "Tuesday&nbsp;") + "</p>"
    cleaned = clean_source(html, S.HEADLINE)
    assert "<p>" not in " ".join(cleaned.sentences)
    assert "&nbsp;" not in " ".join(cleaned.sentences)
