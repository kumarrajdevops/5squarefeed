from datetime import date, datetime, timezone

from sqlalchemy import func

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, Story
from app.ranking.engine import compute_total_score
from app.worker.celery_app import celery_app


PRIMARY_SLOTS = 25
BACKUP_SLOTS = 5
TOTAL_SLOTS = PRIMARY_SLOTS + BACKUP_SLOTS  # 30


@celery_app.task
def run_ranking_selection(run_date_iso: str | None = None) -> dict:
    """
    Score every canonical, AI-candidate story, select the Top 25
    (primary) + next 5 (backup), and persist the result as a new
    Episode + EpisodeStory rows.

    This does NOT touch the `stories` table at all -- every run
    creates a fresh, independent snapshot in episode_stories, so
    re-running selection (e.g. during testing, or later in the day
    as new stories arrive) never destroys a previous run's history.

    run_date_iso: optional "YYYY-MM-DD" string. Defaults to today
    (UTC date) if not provided. Exposed as a task arg mainly for
    testing/backfilling; normal operation will just call this with
    no argument once a day per the 6 AM IST publish cycle.
    """

    now = datetime.now(timezone.utc)

    if run_date_iso:
        run_date = date.fromisoformat(run_date_iso)
    else:
        run_date = now.date()

    with SessionLocal() as db:

        # -------------------------------------------------
        # Never re-select a story that's already been a primary
        # (narrated) selection in ANY earlier episode, regardless of
        # that episode's later approve/reject status -- "once an
        # episode reads a story, it never appears again" (confirmed
        # against real data: episodes #6/#7 shared 14 of 25 primary
        # slots before this fix). A story that only ever sat as an
        # unused backup (never promoted to primary, never narrated)
        # remains eligible -- it was never actually presented to
        # anyone. Queried fresh on every run (not a stored flag), so
        # a backup promoted to primary later via the dashboard's swap
        # is caught by the very next ranking run too.
        # -------------------------------------------------

        already_primary_story_ids = {
            row[0] for row in
            db.query(EpisodeStory.story_id)
            .filter(EpisodeStory.selection_status == "primary")
            .distinct()
            .all()
        }

        # -------------------------------------------------
        # Eligible pool: canonical (non-duplicate), AI-candidate
        # stories. repeats_story_id (see app/tasks/content_dedup.py --
        # a content-similarity complement to the already_primary_
        # story_ids identity check above, catching a *different*
        # story covering an already-narrated event) is deliberately
        # NOT a hard exclusion here: live verification found real
        # false-positive risk at the current, still-unvalidated
        # similarity threshold (two topically-related-but-distinct
        # opinion pieces scored above threshold). A false positive on
        # a hard exclusion silently and permanently drops a
        # legitimately distinct story -- too asymmetric a risk before
        # the threshold has real tuning data behind it. Soft signal
        # only for now, same as verification_status/Automated QA:
        # surfaced in the dashboard (see rank_reason/repeat_reason
        # below and _serialize_episode in app/main.py), human decides.
        # Revisit hard-excluding once CONTENT_SIMILARITY_THRESHOLD is
        # validated against real data (see TODO.md).
        # -------------------------------------------------

        eligible_query = db.query(Story).filter(
            Story.ai_relevance == "ai_candidate",
            Story.canonical_story_id.is_(None),
        )

        if already_primary_story_ids:
            eligible_query = eligible_query.filter(
                Story.id.notin_(already_primary_story_ids)
            )

        stories = eligible_query.all()

        content_repeats_flagged = sum(
            1 for story in stories if story.repeats_story_id is not None
        )

        # -------------------------------------------------
        # Duplicate counts per canonical story, computed in one
        # query rather than N+1 queries per story.
        # -------------------------------------------------

        dup_count_rows = (
            db.query(Story.canonical_story_id, func.count(Story.id))
            .filter(Story.canonical_story_id.isnot(None))
            .group_by(Story.canonical_story_id)
            .all()
        )
        dup_counts: dict[int, int] = dict(dup_count_rows)

        # -------------------------------------------------
        # Score every eligible story.
        # -------------------------------------------------

        scored: list[tuple[Story, float, str]] = []

        for story in stories:
            duplicate_count = dup_counts.get(story.id, 0)

            score, reason = compute_total_score(
                published_at=story.published_at,
                source_name=story.source_name,
                ai_relevance_score=story.ai_relevance_score,
                duplicate_count=duplicate_count,
                now=now,
                window_hours=settings.news_window_hours,
                verification_status=story.verification_status,
            )

            scored.append((story, score, reason))

        # Highest score first.
        scored.sort(key=lambda item: item[1], reverse=True)

        # -------------------------------------------------
        # Take the Top 30 (or fewer, if not enough eligible stories
        # exist yet -- expected during early testing).
        # -------------------------------------------------

        top_slots = scored[:TOTAL_SLOTS]

        # -------------------------------------------------
        # Create the Episode and its EpisodeStory rows.
        # -------------------------------------------------

        episode = Episode(run_date=run_date, status="draft")
        db.add(episode)
        db.flush()  # populate episode.id without committing yet

        for position, (story, score, reason) in enumerate(top_slots, start=1):
            selection_status = "primary" if position <= PRIMARY_SLOTS else "backup"

            episode_story = EpisodeStory(
                episode_id=episode.id,
                story_id=story.id,
                rank_position=position,
                selection_status=selection_status,
                rank_score=score,
                rank_reason=reason,
            )
            db.add(episode_story)

        db.commit()

        result = {
            "episode_id": episode.id,
            "run_date": run_date.isoformat(),
            "eligible_stories": len(scored),
            "already_used_excluded": len(already_primary_story_ids),
            "content_repeats_flagged": content_repeats_flagged,
            "primary_selected": min(len(top_slots), PRIMARY_SLOTS),
            "backup_selected": max(0, len(top_slots) - PRIMARY_SLOTS),
        }

        print(f"[ranking] Completed: {result}")

        return result
