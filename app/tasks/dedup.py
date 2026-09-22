from datetime import date

from app.dates import target_collection_date
from app.db import SessionLocal
from app.filters.dedup import find_duplicate_match
from app.models import NewsItem, StoryState
from app.worker.celery_app import celery_app


def deduplicate_new_stories(db, target_date) -> dict:
    """
    Group AI-candidate stories collected for target_date that describe
    the same underlying story into duplicate clusters (title
    similarity). Same-batch only -- a previous day's stories were
    already resolved by their own processing run.

    Scope: only ai_candidate stories are deduplicated. not_ai stories
    never reach ranking/publishing, so spending compute deduping them
    would be wasted work.

    Plain function, takes `db`/`target_date` explicitly -- called
    directly by app/tasks/scheduled.py's run_daily_processing() as one
    step in a sequence, not auto-chained via .delay() (that auto-chain
    is gone; see app/tasks/ingestion.py's docstring for why). Commits
    its own work at the end, same as before -- an earlier/later stage
    failing doesn't roll this stage back.
    """

    checked = 0
    duplicates_found = 0

    # -------------------------------------------------
    # Pull every ungrouped AI-candidate story collected for
    # target_date, oldest first. Oldest-first means the earliest-
    # published story in a matching cluster naturally becomes
    # canonical, which is a reasonable default (first outlet to
    # report something is usually the primary source).
    # -------------------------------------------------

    rows = (
        db.query(NewsItem, StoryState)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == target_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
        )
        .order_by(NewsItem.published_at.asc())
        .all()
    )

    # Stories confirmed canonical during this pass -- plain NewsItem
    # objects, since find_duplicate_match only needs .id/.title/
    # .published_at, all of which live on NewsItem.
    canonical_pool: list[NewsItem] = []

    for item, state in rows:

        checked += 1

        match, reason = find_duplicate_match(
            candidate_title=item.title,
            candidate_published_at=item.published_at,
            candidate_id=item.id,
            canonical_pool=canonical_pool,
        )

        if match is not None:
            # Link this story to its canonical match. The row is
            # kept, not deleted -- required for audit history.
            state.canonical_story_id = match.id
            state.dedup_reason = reason
            duplicates_found += 1

            print(
                f"[dedup] Story {item.id} ({item.title!r}) "
                f"marked as duplicate of story {match.id} "
                f"({match.title!r}) -- {reason}"
            )
        else:
            # No match found; this story becomes (or remains) a
            # canonical representative other stories can match
            # against for the rest of this pass.
            canonical_pool.append(item)

    db.commit()

    result = {
        "checked": checked,
        "duplicates_found": duplicates_found,
    }

    print(f"[dedup] Completed: {result}")

    return result


@celery_app.task
def run_deduplicate_new_stories(target_date_iso: str | None = None) -> dict:
    """
    Standalone Celery entry point for deduplicate_new_stories -- see
    run_classify_new_raw_items's docstring (app/tasks/classify.py) for
    why this exists alongside the full run_daily_processing sequence.
    """
    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()

    with SessionLocal() as db:
        return deduplicate_new_stories(db, target_date)
