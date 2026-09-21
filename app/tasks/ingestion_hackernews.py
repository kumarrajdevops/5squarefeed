from datetime import datetime, timezone  # Date/time handling

from dateutil import parser as date_parser  # Robust ISO 8601 date parsing
from sqlalchemy.exc import IntegrityError  # Raised on a raw.news_items identity collision

from app.dates import coverage_window  # Single authoritative date/window module
from app.models import NewsItem  # raw.news_items model
from app.sources.article_fetcher import fetch_article_summary  # Real article summary for link-posts
from app.sources.hackernews_api import fetch_ai_stories_for_range  # Hacker News fetcher
from app.sources.publisher_resolver import resolve_publisher  # Real publisher from URL
from app.tasks.ingestion import _find_existing_news_item  # Shared raw-identity lookup


SOURCE_NAME = "Hacker News"


def parse_published(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        dt = date_parser.parse(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (TypeError, ValueError, OverflowError):
        return None


def _build_url(hit: dict) -> str | None:
    # Link posts have a real external url. Ask/Show/Tell HN self-posts
    # don't -- fall back to the HN discussion permalink so every story
    # still has a usable, unique url.
    url = hit.get("url")
    if url:
        return url

    object_id = hit.get("objectID")
    if object_id:
        return f"https://news.ycombinator.com/item?id={object_id}"

    return None


def _build_summary(hit: dict) -> str:
    story_text = hit.get("story_text")
    if story_text:
        return story_text

    # Link posts: HN's own API has no article content, only submission
    # metadata -- try fetching the actual linked page for a real
    # summary before falling back to points/comments (which carries no
    # information about what the story is actually about). Only
    # attempted for a real external link, not the synthetic HN
    # discussion permalink used as a fallback url for self-posts.
    link = hit.get("url")
    if link:
        fetched = fetch_article_summary(link)
        if fetched:
            return fetched

    points = hit.get("points", 0)
    num_comments = hit.get("num_comments", 0)
    return f"{points} points, {num_comments} comments on Hacker News."


def ingest_hackernews_stories(db, target_date) -> dict:
    """
    Fetch AI-related Hacker News stories for target_date and store raw
    source items. Collection only -- same contract as
    app/tasks/ingestion.py's ingest_news(): no ai_relevance, no dedup,
    no ranking, nothing editorial written here.

    Unlike RSS, this IS a true historical query -- Algolia's search
    API genuinely supports an exact [start, end) window
    (fetch_ai_stories_for_range), so "collection" and what used to be
    a separate "backfill" operation are now the same thing. The old
    rolling fetch_ai_stories(window_hours, now) and the separate
    backfill_hackernews_for_date task are both gone -- this is the
    only Hacker News collection path.

    Takes `db`/`target_date` explicitly so app/tasks/collection.py's
    orchestrator can call this and RSS's ingest_news() within one
    shared session/CollectionRun row.
    """

    coverage_start, coverage_end = coverage_window(target_date)

    seen = 0
    inserted = 0
    updated = 0
    invalid = 0

    try:
        hits = fetch_ai_stories_for_range(coverage_start, coverage_end)
    except Exception as exc:
        print(f"[{SOURCE_NAME}] Failed to fetch: {exc}")
        return {"error": str(exc), "seen": 0, "inserted": 0, "updated": 0, "invalid": 0}

    for hit in hits:
        seen += 1

        title = hit.get("title")
        url = _build_url(hit)

        if not title or not url:
            invalid += 1
            continue

        published_at = parse_published(hit.get("created_at"))

        if published_at is None:
            invalid += 1
            continue

        canonical_url = url.strip()
        external_id = hit.get("objectID")
        source_name = resolve_publisher(url)

        existing = _find_existing_news_item(db, source_name, external_id, canonical_url)

        if existing is not None:
            existing.collected_at = datetime.now(timezone.utc)
            db.commit()
            updated += 1
            continue

        summary = _build_summary(hit)

        item = NewsItem(
            title=title.strip(),
            canonical_url=canonical_url,
            # source_name is the actual publisher (resolved from the
            # URL's domain -- e.g. "The Guardian" for a link post, or
            # "Hacker News" itself for a genuine Ask/Show/Tell HN
            # self-post). source_type stays "hackernews" as the
            # discovery-channel marker.
            source_name=source_name,
            source_type="hackernews",
            published_at=published_at,
            author=hit.get("author"),
            external_id=external_id,
            raw_summary=summary,
            collected_at=datetime.now(timezone.utc),
            collection_date=target_date,
            status="collected",
        )

        db.add(item)

        try:
            db.commit()
            inserted += 1
        except IntegrityError:
            db.rollback()
            updated += 1
            print(f"[{SOURCE_NAME}] Inserted concurrently by another run, treated as update: {url}")

    result = {
        "seen": seen,
        "inserted": inserted,
        "updated": updated,
        "invalid": invalid,
        "collection_date": target_date.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
    }

    print(f"[{SOURCE_NAME}] {result}")

    return result
