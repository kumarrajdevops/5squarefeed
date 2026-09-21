from datetime import date, datetime, timezone

from app.dates import target_collection_date
from app.db import SessionLocal
from app.models import CollectionRun
from app.tasks.ingestion import ingest_news
from app.tasks.ingestion_hackernews import ingest_hackernews_stories
from app.worker.celery_app import celery_app


@celery_app.task
def run_collection(target_date_iso: str | None = None, trigger_type: str = "manual") -> dict:
    """
    The single entry point for "Collect News" -- runs RSS and Hacker
    News collection together, as one complete operation, and records
    exactly one raw.collection_runs row for it (not one per source).
    Writes ONLY to raw.news_items/raw.collection_runs -- no
    ai_relevance, no dedup, no ranking, nothing editorial. See
    app/tasks/scheduled.py's run_daily_processing() for the separate,
    explicit operation that reads raw.news_items and produces an
    editorial.episodes row.

    target_date_iso: optional "YYYY-MM-DD" override for dev/testing.
    Defaults to target_collection_date() (today IST - 1 day) -- normal
    callers (the API, the scheduler) never need to pass this; it exists
    so a DEV-only debug path can target an explicit day.
    """

    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()

    run = CollectionRun(
        collection_date=target_date,
        trigger_type=trigger_type,
        status="running",
    )

    with SessionLocal() as db:
        db.add(run)
        db.commit()
        run_id = run.id

        error_details: list[str] = []

        try:
            rss_result = ingest_news(db, target_date)
        except Exception as exc:
            rss_result = {"error": str(exc)}
            error_details.append(f"RSS: {exc}")
            print(f"[collection] RSS collection failed: {exc}")

        try:
            hn_result = ingest_hackernews_stories(db, target_date)
        except Exception as exc:
            hn_result = {"error": str(exc)}
            error_details.append(f"Hacker News: {exc}")
            print(f"[collection] Hacker News collection failed: {exc}")

        run = db.get(CollectionRun, run_id)
        run.completed_at = datetime.now(timezone.utc)
        run.status = "failed" if error_details else "success"
        run.rss_items_seen = rss_result.get("items_seen", 0)
        run.rss_items_inserted = rss_result.get("items_inserted", 0)
        run.rss_items_updated = rss_result.get("items_updated", 0)
        run.hn_items_seen = hn_result.get("seen", 0)
        run.hn_items_inserted = hn_result.get("inserted", 0)
        run.hn_items_updated = hn_result.get("updated", 0)
        run.error_count = len(error_details)
        run.error_details = "; ".join(error_details) if error_details else None
        db.commit()

        result = {
            "collection_run_id": run_id,
            "collection_date": target_date.isoformat(),
            "status": run.status,
            "rss": rss_result,
            "hackernews": hn_result,
        }

        print(f"[collection] Completed: {result}")

        return result
