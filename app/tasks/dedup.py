from app.db import SessionLocal
from app.filters.dedup import find_duplicate_match
from app.models import Story
from app.tasks.content_dedup import enrich_and_dedup_by_content
from app.worker.celery_app import celery_app


@celery_app.task
def deduplicate_new_stories() -> dict:
    """
    Group AI-candidate stories that describe the same underlying
    story into duplicate clusters.

    Scope: only ai_candidate stories are deduplicated. not_ai stories
    never reach ranking/publishing, so spending compute deduping them
    would be wasted work.

    Idempotent-ish design: each run only looks at stories that are
    still ungrouped (canonical_story_id IS NULL). Already-canonical
    stories from previous runs are pulled in as the comparison pool,
    so new stories get checked against everything that's canonical
    so far -- but stories already marked as duplicates are never
    re-examined.
    """

    checked = 0
    duplicates_found = 0

    with SessionLocal() as db:

        # -------------------------------------------------
        # Pull every ungrouped AI-candidate story, oldest first.
        # Oldest-first means the earliest-published story in a
        # matching cluster naturally becomes canonical, which is
        # a reasonable default (first outlet to report something
        # is usually the primary source).
        # -------------------------------------------------

        ungrouped_stories = (
            db.query(Story)
            .filter(
                Story.ai_relevance == "ai_candidate",
                Story.canonical_story_id.is_(None),
            )
            .order_by(Story.published_at.asc())
            .all()
        )

        # Stories confirmed canonical during this pass. Starts empty
        # and grows as we walk through ungrouped_stories in order.
        canonical_pool: list[Story] = []

        for story in ungrouped_stories:

            checked += 1

            match, reason = find_duplicate_match(
                candidate_title=story.title,
                candidate_published_at=story.published_at,
                candidate_id=story.id,
                canonical_pool=canonical_pool,
            )

            if match is not None:
                # Link this story to its canonical match. The row is
                # kept, not deleted -- required for audit history.
                story.canonical_story_id = match.id
                story.dedup_reason = reason
                duplicates_found += 1

                print(
                    f"[dedup] Story {story.id} ({story.title!r}) "
                    f"marked as duplicate of story {match.id} "
                    f"({match.title!r}) -- {reason}"
                )
            else:
                # No match found; this story becomes (or remains) a
                # canonical representative other stories can match
                # against for the rest of this pass.
                canonical_pool.append(story)

        db.commit()

    result = {
        "checked": checked,
        "duplicates_found": duplicates_found,
    }

    print(f"[dedup] Completed: {result}")

    # Content-based dedup + historical repeat detection (full article
    # text + TF-IDF similarity) run next, same as Fact Extraction +
    # Verification used to run directly from here -- this is the one
    # place both ingest_news and ingest_hackernews_stories already
    # funnel through. That stage chains into verification itself once
    # it's done (see app/tasks/content_dedup.py).
    content_dedup_task = enrich_and_dedup_by_content.delay()
    print(f"[dedup] Queued content-dedup task {content_dedup_task.id}")
    result["content_dedup_task_id"] = content_dedup_task.id

    return result
