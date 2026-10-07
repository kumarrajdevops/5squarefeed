"""Briefing pipeline wired into extraction, content-dedup, ranking, the script
stage, QA and the story-edit endpoint."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import requests

from app import main
from app.content import article_extractor
from app.content.briefing import briefing_fingerprint
from app.content.briefing.loader import load_corroborating
from app.models import EpisodeStory, NewsItem, StoryContent, StoryState
from app.qa.video_qa import run_qa_checks
from app.tasks import content as content_task
from app.tasks import content_dedup
from app.tasks.ranking import _run_ranking_selection
from tests.briefing import samples as S

TARGET = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 12, 0)


# ---------------------------------------------------------------- extractor

def _response(html="<html><body>x</body></html>", status=200, content_type="text/html; charset=utf-8"):
    body = html.encode("utf-8")

    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(str(status))

    return SimpleNamespace(
        status_code=status, headers={"content-type": content_type}, content=body,
        text=html, apparent_encoding="utf-8", raise_for_status=raise_for_status,
    )


def test_extractor_reports_the_method_used(monkeypatch):
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response())
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: S.STRONG_ARTICLE)
    result = article_extractor.fetch_full_article_text("https://example.test/a")
    assert result.status == "success"
    assert result.method == "trafilatura_precision"
    assert result.text.startswith("Nova Labs")


def test_extractor_falls_back_to_recall_then_json_ld(monkeypatch):
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response())

    calls = []

    def fake_extract(html, **kw):
        calls.append(kw)
        return S.STRONG_ARTICLE if kw.get("favor_recall") else None

    monkeypatch.setattr(article_extractor.trafilatura, "extract", fake_extract)
    result = article_extractor.fetch_full_article_text("https://example.test/a")
    assert result.method == "trafilatura_recall" and len(calls) == 2

    ld = json.dumps({"@type": "NewsArticle", "articleBody": S.STRONG_ARTICLE})
    html = f'<html><script type="application/ld+json">{ld}</script></html>'
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response(html))
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: None)
    result = article_extractor.fetch_full_article_text("https://example.test/a")
    assert result.method == "json_ld" and result.status == "success"


def test_extractor_retries_once_on_timeout(monkeypatch):
    attempts = []

    def flaky(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.Timeout()
        return _response()

    monkeypatch.setattr(article_extractor.requests, "get", flaky)
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: S.STRONG_ARTICLE)
    assert article_extractor.fetch_full_article_text("https://example.test/a").status == "success"
    assert len(attempts) == 2


def test_extractor_never_raises_and_gives_up_after_one_retry(monkeypatch):
    attempts = []

    def always_down(*a, **k):
        attempts.append(1)
        raise requests.ConnectionError()

    monkeypatch.setattr(article_extractor.requests, "get", always_down)
    result = article_extractor.fetch_full_article_text("https://example.test/a")
    assert result.text is None and result.status == "fetch_error" and result.method is None
    assert len(attempts) == 2


def test_extractor_labels_a_paywalled_stub_partial(monkeypatch):
    stub = ("Nova Labs released Atlas-2 on Tuesday with 70 billion parameters and a long context window. " * 3
            + "Subscribe to continue reading this article.")
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response())
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: stub)
    result = article_extractor.fetch_full_article_text("https://example.test/a")
    assert result.status == "partial" and result.text


def test_extractor_non_html_and_empty_extraction(monkeypatch):
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response(content_type="application/pdf"))
    assert article_extractor.fetch_full_article_text("https://example.test/a").status == "non_html"
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response())
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: None)
    assert article_extractor.fetch_full_article_text("https://example.test/a").status == "empty_extraction"


def test_extractor_caps_stored_text(monkeypatch):
    monkeypatch.setattr(article_extractor.requests, "get", lambda *a, **k: _response())
    monkeypatch.setattr(article_extractor.trafilatura, "extract", lambda html, **kw: "word " * 20_000)
    assert len(article_extractor.fetch_full_article_text("https://example.test/a").text) <= article_extractor.MAX_TEXT_CHARS


# ------------------------------------------------- content-dedup + ranking

def _story(db, title, summary, raw_content=None, source="Example Source", url=None, collection_date=TARGET):
    item = NewsItem(
        title=title, canonical_url=url or f"https://example.test/{title.replace(' ', '-')}",
        source_name=source, source_type="rss", published_at=NOW, collected_at=NOW,
        collection_date=collection_date, status="collected", raw_summary=summary, raw_content=raw_content,
    )
    db.add(item)
    db.flush()
    state = StoryState(id=item.id, ai_relevance="ai_candidate", ai_relevance_score=0.8)
    db.add(state)
    db.commit()
    return item, state


def test_assess_source_sufficiency_stores_status_and_detail(db_session):
    good, good_state = _story(db_session, S.HEADLINE, "", S.STRONG_ARTICLE)
    bad, bad_state = _story(db_session, "Some tool", S.HN_TEASER)
    assert content_dedup.assess_source_sufficiency(db_session, good, good_state) == "sufficient"
    assert content_dedup.assess_source_sufficiency(db_session, bad, bad_state) == "insufficient"
    detail = json.loads(bad_state.sufficiency_detail)
    assert detail["status"] == "insufficient" and detail["reason"]
    assert bad_state.source_word_count == len(S.HN_TEASER.split())


def test_a_verdict_is_stale_when_never_assessed_or_assessed_under_other_briefing_code(db_session, monkeypatch):
    item, state = _story(db_session, S.HEADLINE, "", S.STRONG_ARTICLE)
    assert content_dedup.sufficiency_is_stale(state)
    content_dedup.assess_source_sufficiency(db_session, item, state)
    assert json.loads(state.sufficiency_detail)["briefing_fingerprint"] == briefing_fingerprint()
    assert not content_dedup.sufficiency_is_stale(state)

    monkeypatch.setattr(content_dedup, "briefing_fingerprint", lambda: "changed-composer")
    assert content_dedup.sufficiency_is_stale(state)

    state.sufficiency_detail = None
    state.source_sufficiency = "sufficient"
    monkeypatch.undo()
    assert content_dedup.sufficiency_is_stale(state)


def test_the_fingerprint_tracks_the_composer_sources():
    assert briefing_fingerprint() == briefing_fingerprint() and len(briefing_fingerprint()) == 12


def test_a_same_day_sibling_can_lift_a_thin_story_to_briefable(db_session):
    main_item, main_state = _story(db_session, S.HEADLINE, S.THIN_RSS, source="Outlet A")
    sib, sib_state = _story(db_session, "Atlas-2 arrives", "", S.SIBLING_AGREE, source="Outlet B")
    sib_state.canonical_story_id = main_item.id
    db_session.commit()
    assert load_corroborating(db_session, main_item)[0].source_name == "Outlet B"
    assert content_dedup.assess_source_sufficiency(db_session, main_item, main_state) != "insufficient"


def test_ranking_excludes_insufficient_stories_but_keeps_unassessed_ones(db_session):
    ok, ok_state = _story(db_session, "Good story", "", S.STRONG_ARTICLE, url="https://example.test/ok")
    ok_state.source_sufficiency = "sufficient"
    thin, thin_state = _story(db_session, "Thin story", "x", url="https://example.test/thin")
    thin_state.source_sufficiency = "insufficient"
    old, _ = _story(db_session, "Old story with no assessment", "x", url="https://example.test/old")
    db_session.commit()

    result = _run_ranking_selection(db_session, TARGET, NOW)
    selected = {r[0] for r in db_session.query(EpisodeStory.story_id).filter(EpisodeStory.episode_id == result["episode_id"])}
    assert ok.id in selected and old.id in selected
    assert thin.id not in selected


# ------------------------------------------------------------ script stage

def _content(db, story, **kw):
    content = StoryContent(story_id=story.id, **{"status": "pending", **kw})
    db.add(content)
    db.commit()
    return content


def test_insufficient_story_fails_the_script_stage_without_voicing_it(db_session, monkeypatch):
    story, _ = _story(db_session, "Some tool", S.HN_TEASER)
    content = _content(db_session, story)
    voiced = []
    monkeypatch.setattr(content_task, "synthesize_voice", lambda text, path: voiced.append(text) or [])

    assert content_task.ensure_script_and_voice(db_session, story, content) is False
    assert voiced == []
    assert content.status == "failed"
    assert "insufficient for briefing" in content.error_message
    assert not content.script_text
    assert content.script_quality_status == "fail"


def test_generated_script_persists_quality_metadata(db_session, monkeypatch):
    story, _ = _story(db_session, S.HEADLINE, "", S.IDEAL_ARTICLE)
    content = _content(db_session, story)
    monkeypatch.setattr(content_task, "synthesize_voice", lambda text, path: [])
    monkeypatch.setattr(content_task, "get_audio_duration_seconds", lambda p: 20.0)

    assert content_task.ensure_script_and_voice(db_session, story, content) is True
    assert content.script_quality_status == "pass"
    assert content.script_word_count == len(content.script_text.split())
    assert content.script_sentence_count == 2
    assert json.loads(content.script_meta)["quality"]["status"] == "pass"
    assert content.script_text.startswith("Nova Labs releases Atlas-2 open model.")


# --------------------------------------------------------------------- QA

def _qa_row(db, story, state, content):
    return (SimpleNamespace(), story, state, content)


def test_qa_script_quality_check_flags_insufficient_and_failed_stories(db_session):
    s1, st1 = _story(db_session, "One", "x", url="https://example.test/1")
    s2, st2 = _story(db_session, "Two", "x", url="https://example.test/2")
    s3, st3 = _story(db_session, "Three", "x", url="https://example.test/3")
    st1.source_sufficiency = "sufficient"
    st2.source_sufficiency = "insufficient"
    c1 = SimpleNamespace(script_quality_status="pass", audio_path=None, caption_segments=None, captions_path=None)
    c2 = SimpleNamespace(script_quality_status="pass", audio_path=None, caption_segments=None, captions_path=None)
    c3 = SimpleNamespace(script_quality_status="review", audio_path=None, caption_segments=None, captions_path=None)
    checks = {c["check"]: c for c in run_qa_checks(None, [(0, s1, st1, c1), (0, s2, st2, c2), (0, s3, st3, c3)], None)}
    assert checks["script_quality"]["passed"] is False
    assert str(s2.id) in checks["script_quality"]["detail"]
    assert "review" in checks["script_quality"]["detail"]

    ok = {c["check"]: c for c in run_qa_checks(None, [(0, s1, st1, c1), (0, s3, st3, c3)], None)}
    assert ok["script_quality"]["passed"] is True  # "review" is informational, never a failure


# ---------------------------------------------------------- edit endpoint

def test_human_edit_replaces_the_generated_quality_grade(db_session, monkeypatch):
    story, _ = _story(db_session, S.HEADLINE, "", S.IDEAL_ARTICLE)
    _content(db_session, story, script_text="Old generated text.", script_quality_status="pass",
             script_word_count=3, script_sentence_count=1, status="voice_ready")
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)

    main.update_story_content(story.id, main.StoryContentUpdate(script_text="Editor wrote this. It has two sentences."))

    content = db_session.query(StoryContent).filter_by(story_id=story.id).one()
    assert content.script_quality_status == "edited"
    assert content.script_word_count == 7
    assert content.script_sentence_count == 2
    assert content.script_text == "Editor wrote this. It has two sentences."
