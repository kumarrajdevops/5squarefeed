import types
from datetime import date, timedelta

from app.dates import coverage_window
from app.models import NewsItem, StoryState
from app.tasks import ingestion


TARGET_DATE = date(2026, 9, 20)


def _fake_entry(title, link, published, entry_id=None, summary="A real summary."):
    return types.SimpleNamespace(
        title=title, link=link, published=published, id=entry_id,
        summary=summary, author=None,
    )


def _patch_single_source(monkeypatch, entries):
    fake_feed = types.SimpleNamespace(entries=entries, status=200, bozo=0)
    monkeypatch.setattr(
        ingestion, "feedparser",
        types.SimpleNamespace(parse=lambda url, agent=None: fake_feed),
    )
    monkeypatch.setattr(ingestion, "NEWS_SOURCES", [
        {"name": "Test Source", "url": "https://example.test/feed", "source_type": "rss", "enabled": True},
    ])


def test_ingest_news_keeps_entry_inside_window_discards_outside(db_session, monkeypatch):
    coverage_start, coverage_end = coverage_window(TARGET_DATE)
    inside = coverage_start + timedelta(hours=1)
    outside = coverage_start - timedelta(hours=1)

    entries = [
        _fake_entry("Inside story", "https://example.test/a", inside.isoformat()),
        _fake_entry("Outside story", "https://example.test/b", outside.isoformat()),
    ]
    _patch_single_source(monkeypatch, entries)

    result = ingestion.ingest_news(db_session, TARGET_DATE)

    assert result["items_inserted"] == 1
    assert result["outside_window"] == 1

    items = db_session.query(NewsItem).all()
    assert len(items) == 1
    assert items[0].title == "Inside story"
    assert items[0].collection_date == TARGET_DATE


def test_ingest_news_is_repeatable_without_duplicating(db_session, monkeypatch):
    coverage_start, _ = coverage_window(TARGET_DATE)
    inside = coverage_start + timedelta(hours=1)
    entries = [_fake_entry("Repeatable story", "https://example.test/a", inside.isoformat())]
    _patch_single_source(monkeypatch, entries)

    first = ingestion.ingest_news(db_session, TARGET_DATE)
    second = ingestion.ingest_news(db_session, TARGET_DATE)

    assert first["items_inserted"] == 1
    assert second["items_inserted"] == 0
    assert second["items_updated"] == 1
    assert db_session.query(NewsItem).count() == 1


def test_ingest_news_never_creates_editorial_rows(db_session, monkeypatch):
    coverage_start, _ = coverage_window(TARGET_DATE)
    inside = coverage_start + timedelta(hours=1)
    entries = [_fake_entry("Some story", "https://example.test/a", inside.isoformat())]
    _patch_single_source(monkeypatch, entries)

    ingestion.ingest_news(db_session, TARGET_DATE)

    assert db_session.query(NewsItem).count() == 1
    assert db_session.query(StoryState).count() == 0
