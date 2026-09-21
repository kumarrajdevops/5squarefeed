from datetime import datetime, timezone  # Date/time handling

import feedparser  # Read and parse RSS feeds
from dateutil import parser as date_parser  # Robust date parsing (RFC 2822 + ISO 8601 + more)
from sqlalchemy.exc import IntegrityError  # Raised on a raw.news_items identity collision

from app.dates import coverage_window  # Single authoritative date/window module
from app.models import NewsItem  # raw.news_items model
from app.sources.article_fetcher import fetch_article_summary  # Fallback summary for empty RSS excerpts
from app.sources.registry import NEWS_SOURCES  # Configured news sources


# Many sites (VentureBeat's Vercel bot-challenge is a known example) block
# or throttle requests that don't look like they come from a browser.
# feedparser does not send one by default, so we set one explicitly.
FEED_USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsPlatformBot/1.0; "
    "+https://github.com/5min-ai-news)"
)


def parse_published(value: str | None) -> datetime | None:
    """
    Convert RSS publication date into a timezone-aware UTC datetime.

    Feeds are inconsistent about date format: some use RFC 2822
    ("Wed, 09 Sep 2026 17:42:19 -0400"), others use ISO 8601
    ("2026-09-09T17:42:19-04:00"). dateutil.parser.parse() handles
    both (and most other common variants), unlike email.utils'
    parsedate_to_datetime which only understands RFC 2822.
    """

    if not value:  # RSS entry has no publication date
        return None

    try:
        dt = date_parser.parse(value)  # Handles RFC 2822, ISO 8601, and more

        if dt.tzinfo is None:  # If RSS date has no timezone information
            dt = dt.replace(tzinfo=timezone.utc)  # Treat it as UTC

        return dt.astimezone(timezone.utc)  # Normalize everything to UTC

    except (TypeError, ValueError, OverflowError):  # Invalid/unparseable date
        return None


def _find_existing_news_item(db, source_name: str, external_id: str | None, canonical_url: str) -> NewsItem | None:
    """
    Identity per app/models.py's NewsItem: prefer (source_name,
    external_id) when the source gave us a reliable id, else fall back
    to (source_name, canonical_url). Same two-index scheme the DB
    itself enforces (uq_news_items_source_external_id,
    uq_news_items_source_url) -- this is the read-side half of it.
    """
    if external_id:
        existing = (
            db.query(NewsItem)
            .filter(NewsItem.source_name == source_name, NewsItem.external_id == external_id)
            .first()
        )
        if existing is not None:
            return existing

    return (
        db.query(NewsItem)
        .filter(NewsItem.source_name == source_name, NewsItem.canonical_url == canonical_url)
        .first()
    )


def ingest_news(db, target_date) -> dict:
    """
    Fetch enabled RSS sources and store raw source items for
    target_date (app/dates.py's target_collection_date() -- yesterday
    in IST, unless a dev-only explicit date is given). Collection only:
    no ai_relevance, no dedup, no ranking, no editorial fields of any
    kind are written here (see app/models.py's NewsItem vs StoryState).

    RSS has no historical query capability -- each source's feed is
    fetched exactly once, as-is (same feedparser call as always), and
    whatever entries currently happen to be live are kept if their own
    published_at falls inside target_date's IST calendar day,
    discarded otherwise. A story published on target_date that has
    already scrolled out of a feed's live window by the time this
    runs is NOT recovered -- deliberately deferred, not solved here
    (see TODO.md/the plan this was built from).

    Repeated collection for the same target_date is safe: an item
    already in raw.news_items (matched by identity, see
    _find_existing_news_item) has its collected_at refreshed rather
    than being inserted again.

    Takes `db` explicitly (same split as app/tasks/episode_video.py's
    _produce_story_content) so app/tasks/collection.py's orchestrator
    can call this and app/tasks/ingestion_hackernews.py's HN
    equivalent within one shared session/CollectionRun row.
    """

    coverage_start, coverage_end = coverage_window(target_date)

    sources_processed = 0
    items_seen = 0
    items_inserted = 0
    items_updated = 0
    invalid = 0
    outside_window = 0
    failed_sources = 0

    per_source_stats: dict[str, dict[str, int]] = {}

    for source in NEWS_SOURCES:

        if not source["enabled"]:
            continue

        sources_processed += 1

        source_seen = 0
        source_inserted = 0
        source_updated = 0
        source_invalid = 0
        source_outside_window = 0

        try:
            feed = feedparser.parse(source["url"], agent=FEED_USER_AGENT)

            http_status = getattr(feed, "status", None)

            if http_status is not None and http_status >= 400:
                print(
                    f"[{source['name']}] HTTP {http_status} fetching feed "
                    f"— 0 entries will be available even though this "
                    f"is not counted as a failed_source."
                )

            if getattr(feed, "bozo", 0):
                bozo_exc = getattr(feed, "bozo_exception", None)
                print(f"[{source['name']}] Feed parsed with warnings (bozo=1): {bozo_exc}")

            print(f"[{source['name']}] HTTP status={http_status}, entries found={len(feed.entries)}")

            for entry in feed.entries:

                items_seen += 1
                source_seen += 1

                title = getattr(entry, "title", None)
                url = getattr(entry, "link", None)

                if not title or not url:
                    invalid += 1
                    source_invalid += 1
                    print(f"[{source['name']}] REJECTED (invalid): missing title or url. title={title!r} url={url!r}")
                    continue

                published_value = getattr(entry, "published", None) or getattr(entry, "updated", None)
                published_at = parse_published(published_value)

                if published_at is None:
                    invalid += 1
                    source_invalid += 1
                    print(
                        f"[{source['name']}] REJECTED (invalid): unparseable publish date. "
                        f"raw_value={published_value!r} title={title!r}"
                    )
                    continue

                if not (coverage_start <= published_at <= coverage_end):
                    outside_window += 1
                    source_outside_window += 1
                    continue

                canonical_url = url.strip()
                external_id = getattr(entry, "id", None)

                existing = _find_existing_news_item(db, source["name"], external_id, canonical_url)

                if existing is not None:
                    existing.collected_at = datetime.now(timezone.utc)
                    db.commit()
                    items_updated += 1
                    source_updated += 1
                    continue

                author = getattr(entry, "author", None)
                summary = getattr(entry, "summary", None)

                # A minority of RSS entries carry no description at
                # all (confirmed: NVIDIA Blog's "Heart of the Matter"
                # story) -- fetch the linked page's own description
                # rather than leaving the item with nothing.
                if not summary:
                    summary = fetch_article_summary(url)

                item = NewsItem(
                    title=title.strip(),
                    canonical_url=canonical_url,
                    source_name=source["name"],
                    source_type=source["source_type"],
                    published_at=published_at,
                    author=author,
                    external_id=external_id,
                    raw_summary=summary,
                    collected_at=datetime.now(timezone.utc),
                    collection_date=target_date,
                    status="collected",
                )

                # Commit each item individually, not the whole source's
                # batch at once -- a concurrent collection run touching
                # the same item would otherwise crash this bare commit
                # and lose every item queued so far in this source, not
                # just the colliding one.
                db.add(item)

                try:
                    db.commit()
                    items_inserted += 1
                    source_inserted += 1
                except IntegrityError:
                    db.rollback()
                    items_updated += 1
                    source_updated += 1
                    print(f"[{source['name']}] Inserted concurrently by another run, treated as update: {url}")

            per_source_stats[source["name"]] = {
                "seen": source_seen,
                "inserted": source_inserted,
                "updated": source_updated,
                "invalid": source_invalid,
                "outside_window": source_outside_window,
            }

        except Exception as exc:
            db.rollback()
            failed_sources += 1
            per_source_stats[source["name"]] = {"error": str(exc)}
            print(f"Failed to process {source['name']}: {exc}")

    print("---- Per-source RSS collection breakdown ----")
    for name, stats in per_source_stats.items():
        print(f"  {name}: {stats}")
    print("-----------------------------------------")

    return {
        "sources_processed": sources_processed,
        "items_seen": items_seen,
        "items_inserted": items_inserted,
        "items_updated": items_updated,
        "outside_window": outside_window,
        "invalid": invalid,
        "failed_sources": failed_sources,
        "per_source": per_source_stats,
        "collection_date": target_date.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
    }
