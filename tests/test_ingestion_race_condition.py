from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.models import NewsItem


COLLECTION_DATE = date(2026, 9, 22)


def _insert_item(db, url, title="Some story", source_name="Example Source"):
    item = NewsItem(
        title=title,
        canonical_url=url,
        source_name=source_name,
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=COLLECTION_DATE,
        status="collected",
    )
    db.add(item)
    db.commit()
    return item


def test_duplicate_url_raises_integrity_error_not_silent_corruption(db_session):
    """
    Sanity check that the underlying constraint this fix relies on
    actually fires under the test database too (SQLite, like Postgres,
    raises IntegrityError on a uq_news_items_source_url violation via
    SQLAlchemy's backend-agnostic exception).
    """
    _insert_item(db_session, url="https://example.com/story")

    duplicate = NewsItem(
        title="Different title, same url",
        canonical_url="https://example.com/story",
        source_name="Example Source",  # same source_name -- same identity
        source_type="hackernews",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=COLLECTION_DATE,
        status="collected",
    )
    db_session.add(duplicate)

    try:
        db_session.commit()
        assert False, "expected an IntegrityError on the duplicate (source_name, canonical_url)"
    except IntegrityError:
        db_session.rollback()


def test_per_row_commit_pattern_isolates_a_collision_from_other_inserts(db_session):
    """
    Direct regression for this session's ingestion race-condition fix
    (app/tasks/ingestion.py, app/tasks/ingestion_hackernews.py): a
    uq_news_items_source_url collision on one entry must not roll back
    other valid inserts already committed in the same run -- reproduces
    the exact per-row commit + narrow except IntegrityError pattern
    those tasks now use, rather than a single commit-at-the-end-of-the-
    batch approach that would have lost everything.
    """
    # An item that already exists (simulating another run having just
    # inserted it moments before this one's existing-item check ran).
    _insert_item(db_session, url="https://example.com/already-inserted")

    incoming_urls = [
        "https://example.com/new-story-1",
        "https://example.com/already-inserted",  # collides
        "https://example.com/new-story-2",
    ]

    inserted = 0
    duplicates = 0

    for url in incoming_urls:
        item = NewsItem(
            title=f"Story for {url}",
            canonical_url=url,
            source_name="Example Source",
            source_type="rss",
            published_at=datetime.now(timezone.utc),
            collected_at=datetime.now(timezone.utc),
            collection_date=COLLECTION_DATE,
            status="collected",
        )
        db_session.add(item)

        try:
            db_session.commit()
            inserted += 1
        except IntegrityError:
            db_session.rollback()
            duplicates += 1

    assert inserted == 2
    assert duplicates == 1

    all_urls = {i.canonical_url for i in db_session.query(NewsItem).all()}
    assert all_urls == {
        "https://example.com/already-inserted",
        "https://example.com/new-story-1",
        "https://example.com/new-story-2",
    }
