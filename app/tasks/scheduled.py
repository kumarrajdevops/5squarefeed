from datetime import date, datetime, timezone

from app.dates import target_collection_date
from app.db import SessionLocal
from app.tasks.classify import classify_new_raw_items
from app.tasks.content_dedup import enrich_and_dedup_by_content
from app.tasks.dedup import deduplicate_new_stories
from app.tasks.episode_qa import run_episode_qa
from app.tasks.episode_video import produce_episode_video
from app.tasks.ranking import _run_ranking_selection
from app.tasks.verification import run_fact_extraction_and_verification
from app.worker.celery_app import celery_app


@celery_app.task
def run_daily_processing(target_date_iso: str | None = None) -> dict:
    """
    The "processing" half of the daily cycle (project.md's Daily
    Execution Architecture -- 4 AM IST, after the 10 PM/1 AM/3:30 AM
    IST collection passes have finished populating raw.news_items):
    classify -> title-dedup -> content-dedup/historical-repeat ->
    verification -> rank/select -> produce -> QA, for target_date.

    Collection and processing are deliberately separate operations
    (see app/tasks/collection.py's docstring) -- this is the only
    place a raw.news_items row is ever turned into editorial state.

    Runs every step sequentially in-process (direct function calls,
    not .delay() -- replaces the old auto-chain where each stage
    queued the next one itself) since each stage's output feeds
    the next and this task must genuinely wait for each to finish, same
    rationale as _produce_story_content in app/tasks/episode_video.py.
    All five processing stages (classify/dedup/content_dedup/
    verification/ranking) share one db session so each stage's own
    commit() is the real transaction boundary -- an earlier stage's
    committed work is never rolled back by a later stage failing, and
    re-running this task for a target_date already processed is safe
    (each stage's own idempotency filter -- see each stage's
    docstring -- makes it a no-op the second time).

    target_date_iso: optional "YYYY-MM-DD" override for dev/testing.
    Defaults to target_collection_date() (today IST - 1 day) --
    normal (scheduled) invocation never needs to pass this.
    """

    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        classify_result = classify_new_raw_items(db, target_date)
        dedup_result = deduplicate_new_stories(db, target_date)
        content_dedup_result = enrich_and_dedup_by_content(db, target_date)
        verification_result = run_fact_extraction_and_verification(db, target_date)
        ranking_result = _run_ranking_selection(db, target_date, now)

    result = {
        "target_date": target_date.isoformat(),
        "classify": classify_result,
        "dedup": dedup_result,
        "content_dedup": content_dedup_result,
        "verification": verification_result,
        "ranking": ranking_result,
    }

    episode_id = ranking_result.get("episode_id")

    if episode_id is None:
        # Ranking declined to create an episode (an existing one
        # already covers this episode_date, or a creation race was
        # lost -- see _run_ranking_selection). Nothing to produce/QA.
        print(f"[scheduled] Daily processing completed without a new episode: {result}")
        return result

    produce_result = produce_episode_video(episode_id)
    qa_result = run_episode_qa(episode_id)

    result["produce_status"] = produce_result.get("status")
    result["stories_failed"] = produce_result.get("stories_failed")
    result["backups_failed"] = produce_result.get("backups_failed")
    result["qa_status"] = qa_result.get("qa_status")

    print(f"[scheduled] Daily processing completed: {result}")

    return result
