from datetime import date, datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.dates import target_collection_date
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.ranking.engine import compute_total_score
from app.worker.celery_app import celery_app


PRIMARY_SLOTS = 25
BACKUP_SLOTS = 5
TOTAL_SLOTS = PRIMARY_SLOTS + BACKUP_SLOTS  # 30


def classify_existing_episode(existing: Episode) -> str:
    """
    One of "episode_published" / "episode_approved" / "episode_rejected"
    / "existing_draft_reused" -- the single source of truth for how an
    already-existing episode blocks (or doesn't block) a new selection
    request. Shared by this task's own upfront check and race-recovery
    path below, and by POST /api/v1/episodes/select's synchronous
    pre-check (app/main.py) and POST /api/v1/episodes/{id}/reprocess's
    guard, so the same episode is always classified the same way
    everywhere it matters.
    """
    if existing.publish_status == "published":
        return "episode_published"
    if existing.status == "approved":
        return "episode_approved"
    if existing.status == "rejected":
        return "episode_rejected"
    return "existing_draft_reused"


def _existing_episode_result(existing: Episode, episode_date: date) -> dict:
    """
    Shared by both the upfront existing-episode check and the
    IntegrityError race-recovery path below, so a request that loses
    the creation race gets exactly the same response shape as one that
    saw the existing episode from the start.
    """
    return {
        "episode_id": existing.id,
        "episode_date": episode_date.isoformat(),
        "created": False,
        "reason": classify_existing_episode(existing),
    }


def _classify_episode_lookup(existing_episodes: list[Episode], episode_date: date) -> dict | None:
    """
    Given already-fetched Episode rows for an episode_date, returns the
    blocking/reuse result dict if a new episode must NOT be created,
    or None if it's clear to proceed (zero existing rows). Takes a
    plain list rather than querying itself so it's a pure function,
    directly unit-testable with lightweight fake episodes -- including
    the "multiple existing" branch, which uq_editorial_episodes_episode_date
    makes otherwise impossible to reproduce in any real (or properly-
    constrained test) database going forward.
    """
    if len(existing_episodes) > 1:
        return {
            "episode_date": episode_date.isoformat(),
            "created": False,
            "reason": "multiple_existing_episodes",
            "candidate_episode_ids": [e.id for e in existing_episodes],
        }
    if existing_episodes:
        return _existing_episode_result(existing_episodes[0], episode_date)
    return None


def _score_and_select_top_stories(
    db, now: datetime, episode_date: date
) -> tuple[list[tuple[NewsItem, StoryState, float, str]], list[tuple[NewsItem, StoryState, float, str]], set[int], int]:
    """
    Shared by run_ranking_selection (new Episode) and reprocess_episode
    (existing Episode, fresh EpisodeStory snapshot) -- the eligibility
    filter and scoring pass itself is identical either way; only what
    happens to the result (create vs. replace) differs between the two
    callers.

    episode_date: the coverage day this selection is for. Eligibility
    is scoped by raw.news_items.collection_date == episode_date --
    that column IS the authoritative partition a row was collected
    for (see app/dates.py), so there's no separate published_at bounds
    check to apply here anymore (the old compute_coverage_window hard
    cutoff is gone -- collection_date already means the same thing,
    computed once at collection time instead of re-derived here).

    Returns (scored, top_slots, already_primary_story_ids,
    content_repeats_flagged), where each entry in scored/top_slots is
    (NewsItem, StoryState, score, reason).
    """

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
    # Eligible pool: canonical (non-duplicate), AI-candidate stories
    # collected for episode_date. repeats_story_id (see
    # app/tasks/content_dedup.py -- a content-similarity complement to
    # the already_primary_story_ids identity check above, catching a
    # *different* story covering an already-narrated event) is
    # deliberately NOT a hard exclusion here: live verification found
    # real false-positive risk at the current, still-unvalidated
    # similarity threshold (two topically-related-but-distinct opinion
    # pieces scored above threshold). A false positive on a hard
    # exclusion silently and permanently drops a legitimately distinct
    # story -- too asymmetric a risk before the threshold has real
    # tuning data behind it. Soft signal only for now, same as
    # verification_status/Automated QA: surfaced in the dashboard (see
    # rank_reason/repeat_reason below and _serialize_episode in
    # app/main.py), human decides. Revisit hard-excluding once
    # CONTENT_SIMILARITY_THRESHOLD is validated against real data (see
    # TODO.md).
    # -------------------------------------------------

    eligible_query = (
        db.query(NewsItem, StoryState)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == episode_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
        )
    )

    if already_primary_story_ids:
        eligible_query = eligible_query.filter(NewsItem.id.notin_(already_primary_story_ids))

    rows = eligible_query.all()

    content_repeats_flagged = sum(
        1 for item, state in rows if state.repeats_story_id is not None
    )

    # -------------------------------------------------
    # Duplicate counts per canonical story, computed in one query
    # rather than N+1 queries per story.
    # -------------------------------------------------

    dup_count_rows = (
        db.query(StoryState.canonical_story_id, func.count(StoryState.id))
        .filter(StoryState.canonical_story_id.isnot(None))
        .group_by(StoryState.canonical_story_id)
        .all()
    )
    dup_counts: dict[int, int] = dict(dup_count_rows)

    # -------------------------------------------------
    # Score every eligible story.
    # -------------------------------------------------

    scored: list[tuple[NewsItem, StoryState, float, str]] = []

    for item, state in rows:
        duplicate_count = dup_counts.get(item.id, 0)

        score, reason = compute_total_score(
            published_at=item.published_at,
            source_name=item.source_name,
            ai_relevance_score=state.ai_relevance_score,
            duplicate_count=duplicate_count,
            now=now,
            window_hours=settings.news_window_hours,
            verification_status=state.verification_status,
        )

        scored.append((item, state, score, reason))

    # Highest score first.
    scored.sort(key=lambda entry: entry[2], reverse=True)

    # -------------------------------------------------
    # Take the Top 30 (or fewer, if not enough eligible stories
    # exist yet -- expected during early testing).
    # -------------------------------------------------

    top_slots = scored[:TOTAL_SLOTS]

    return scored, top_slots, already_primary_story_ids, content_repeats_flagged


def _create_episode_or_recover(
    db, episode_date: date, top_slots: list[tuple[NewsItem, StoryState, float, str]]
) -> tuple[int | None, dict | None]:
    """
    Insert the Episode + EpisodeStory rows for an episode_date already
    confirmed (by the caller) to have no existing episode. Separated
    out from _run_ranking_selection so the
    uq_editorial_episodes_episode_date race-recovery path can be
    exercised directly in a test without needing real concurrent
    threads: pre-insert a competing Episode for episode_date, then
    call this function -- its own INSERT will genuinely conflict,
    exactly like a real concurrent second request.

    Returns (episode_id, None) on success, or (None, result_dict) if
    the insert lost a race against another request that committed an
    Episode for this episode_date first -- the caller should return
    result_dict as-is in that case.
    """

    try:
        episode = Episode(episode_date=episode_date, status="draft")
        db.add(episode)
        db.flush()  # populate episode.id without committing yet

        for position, (item, state, score, reason) in enumerate(top_slots, start=1):
            selection_status = "primary" if position <= PRIMARY_SLOTS else "backup"

            episode_story = EpisodeStory(
                episode_id=episode.id,
                story_id=item.id,
                rank_position=position,
                selection_status=selection_status,
                rank_score=score,
                rank_reason=reason,
            )
            db.add(episode_story)

        db.commit()
        return episode.id, None
    except IntegrityError:
        db.rollback()
        winner = db.query(Episode).filter(Episode.episode_date == episode_date).one()
        result = _existing_episode_result(winner, episode_date)
        result["race_lost"] = True
        print(f"[ranking] Lost creation race for {episode_date}, reusing winner: {result}")
        return None, result


def _run_ranking_selection(db, episode_date: date, now: datetime) -> dict:
    """
    The actual idempotent selection logic, taking `db` explicitly so it
    can be exercised directly in tests (same split as
    app/tasks/episode_video.py's _produce_story_content/
    produce_episode_video) rather than only through the Celery task,
    which owns its own SessionLocal().

    Idempotent per episode_date, enforced two ways
    (app/models.py's uq_editorial_episodes_episode_date is the final
    backstop; this function is the layer that actually understands
    *why*):

    - No existing Episode for episode_date -> create one, as before.
    - Existing Episode found -> never create another. A draft is
      reused as-is (its EpisodeStory rows are NOT touched -- use the
      explicit reprocess endpoint for that); rejected/approved/
      published are blocked outright, since normal selection must
      never silently resurrect a rejection, invalidate an approval, or
      touch anything already live. See _existing_episode_result().
    - More than one existing Episode for episode_date (only possible
      for historical data from before this constraint existed) ->
      blocked, every candidate id reported, never guessed at.
    - Two concurrent calls both seeing "no existing episode" is a real
      TOCTOU race this check alone can't close -- the loser's INSERT
      hits uq_editorial_episodes_episode_date and raises
      IntegrityError, caught below, which re-queries for the winner
      and returns the same "existing" shape rather than crashing.
    """

    existing_episodes = (
        db.query(Episode).filter(Episode.episode_date == episode_date).all()
    )

    blocked_result = _classify_episode_lookup(existing_episodes, episode_date)
    if blocked_result is not None:
        print(f"[ranking] Not creating a new episode: {blocked_result}")
        return blocked_result

    scored, top_slots, already_primary_story_ids, content_repeats_flagged = (
        _score_and_select_top_stories(db, now, episode_date)
    )

    episode_id, race_lost_result = _create_episode_or_recover(db, episode_date, top_slots)
    if race_lost_result is not None:
        return race_lost_result

    result = {
        "episode_id": episode_id,
        "episode_date": episode_date.isoformat(),
        "created": True,
        "eligible_stories": len(scored),
        "already_used_excluded": len(already_primary_story_ids),
        "content_repeats_flagged": content_repeats_flagged,
        "primary_selected": min(len(top_slots), PRIMARY_SLOTS),
        "backup_selected": max(0, len(top_slots) - PRIMARY_SLOTS),
    }

    print(f"[ranking] Completed: {result}")

    return result


@celery_app.task
def run_ranking_selection(episode_date_iso: str | None = None) -> dict:
    """
    Score every canonical, AI-candidate story collected for
    episode_date, select the Top 25 (primary) + next 5 (backup), and
    persist the result as a new Episode + EpisodeStory rows. See
    _run_ranking_selection() for the actual idempotency behavior.

    episode_date_iso: optional "YYYY-MM-DD" string. Defaults to
    target_collection_date() (today IST - 1 day) if not provided --
    normal callers (app/tasks/scheduled.py's run_daily_processing)
    always pass the same target_date the rest of that day's
    processing used; this default exists mainly for direct
    manual/dev invocation.
    """

    now = datetime.now(timezone.utc)

    episode_date = date.fromisoformat(episode_date_iso) if episode_date_iso else target_collection_date()

    with SessionLocal() as db:
        return _run_ranking_selection(db, episode_date, now)


REPROCESSABLE_STATUSES = {"draft", "rejected"}


def _reprocess_episode(db, episode_id: int, now: datetime) -> dict:
    """
    The actual reprocess logic, taking `db` explicitly for direct
    testability (same split as _run_ranking_selection above).

    Only allowed when the episode's status is "draft" or "rejected"
    (REPROCESSABLE_STATUSES). "approved" and "published" are refused
    unconditionally -- reprocessing an approved-but-unpublished episode
    would silently invalidate a human sign-off, and reprocessing a
    published one would rewrite content already live on YouTube. There
    is no override for either case in this phase; an approved episode
    must be explicitly rejected/reset first if a redo is genuinely
    needed.

    Replaces this episode's EpisodeStory rows outright (delete + fresh
    insert) -- this phase does not version the previous selection. The
    old rank_reason/rank_score audit trail for this episode is not
    preserved once reprocessed; only the episode itself persists.

    Resets video_status/qa_status to "pending" and stamps
    content_changed_at, reusing the exact staleness mechanism already
    built for reorder/swap edits (qaIsStale() in the dashboard) rather
    than inventing a new one -- a stale QA badge is exactly the right
    signal here, since the story selection genuinely changed.

    A successful reprocess also resets status to "draft" -- a
    rejected episode that's just been given an entirely new story
    selection should go back through editorial review, not remain
    marked "rejected" against content nobody has seen yet.
    """

    episode = db.get(Episode, episode_id)

    if episode is None:
        result = {"episode_id": episode_id, "reprocessed": False, "reason": "not_found"}
        print(f"[ranking] Reprocess failed: {result}")
        return result

    if episode.status not in REPROCESSABLE_STATUSES:
        reason = classify_existing_episode(episode)
        result = {
            "episode_id": episode.id,
            "episode_date": episode.episode_date.isoformat(),
            "reprocessed": False,
            "reason": reason,
        }
        print(f"[ranking] Reprocess blocked: {result}")
        return result

    # Delete this episode's existing snapshot FIRST, before scoring --
    # otherwise its own previous primary selections would incorrectly
    # self-exclude via the "never re-select" check inside
    # _score_and_select_top_stories, since that check only looks at
    # EpisodeStory rows, not which episode is being reprocessed.
    db.query(EpisodeStory).filter(EpisodeStory.episode_id == episode.id).delete()

    scored, top_slots, already_primary_story_ids, content_repeats_flagged = (
        _score_and_select_top_stories(db, now, episode.episode_date)
    )

    for position, (item, state, score, reason) in enumerate(top_slots, start=1):
        selection_status = "primary" if position <= PRIMARY_SLOTS else "backup"

        episode_story = EpisodeStory(
            episode_id=episode.id,
            story_id=item.id,
            rank_position=position,
            selection_status=selection_status,
            rank_score=score,
            rank_reason=reason,
        )
        db.add(episode_story)

    episode.status = "draft"
    episode.video_status = "pending"
    episode.qa_status = "pending"
    episode.content_changed_at = now

    db.commit()

    result = {
        "episode_id": episode.id,
        "episode_date": episode.episode_date.isoformat(),
        "reprocessed": True,
        "eligible_stories": len(scored),
        "already_used_excluded": len(already_primary_story_ids),
        "content_repeats_flagged": content_repeats_flagged,
        "primary_selected": min(len(top_slots), PRIMARY_SLOTS),
        "backup_selected": max(0, len(top_slots) - PRIMARY_SLOTS),
    }

    print(f"[ranking] Reprocess completed: {result}")

    return result


@celery_app.task
def reprocess_episode(episode_id: int) -> dict:
    """
    Explicit, auditable redo of selection for an EXISTING episode --
    the only sanctioned way to change what a draft/rejected episode
    contains once created. Never creates a second Episode row; operates
    on episode_id directly, so there's no episode_date ambiguity to
    resolve. See _reprocess_episode() for the actual behavior.
    """

    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        return _reprocess_episode(db, episode_id, now)
