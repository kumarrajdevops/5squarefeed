from datetime import date, datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.dates import target_collection_date
from app.db import SessionLocal
from app.dedup.relations import effective_duplicate_story_ids
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.ranking.engine import ai_relevance_input, compute_total_score
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
            # Stories whose source cannot support a briefing (app/content/briefing)
            # are never selected; NULL (not assessed) stays eligible.
            or_(StoryState.source_sufficiency.is_(None), StoryState.source_sufficiency != "insufficient"),
        )
    )

    if already_primary_story_ids:
        eligible_query = eligible_query.filter(NewsItem.id.notin_(already_primary_story_ids))

    # Stories the historical detector decided report a development already published
    # (app/dedup, editor overrides respected) never enter the pool; a story judged a
    # new_development of covered ground stays eligible and ranks as usual.
    historical_duplicates = effective_duplicate_story_ids(db)
    if historical_duplicates:
        eligible_query = eligible_query.filter(NewsItem.id.notin_(historical_duplicates))

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
            ai_relevance_score=ai_relevance_input(
                state.ai_relevance_score,
                classifier_version=state.classifier_version,
                classifier_disposition=state.classifier_disposition,
                review_decision=state.review_decision,
            ),
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


def _free_positions(used: set[int], first: int, last: int) -> list[int]:
    return [p for p in range(first, last + 1) if p not in used]


def _update_draft_episode(db, episode: Episode, now: datetime) -> dict:
    """
    Re-running selection for an episode that is still a draft: the
    first run's stories stick. Every existing EpisodeStory row (primary
    and backup, with its rank_position and any dashboard reorder/swap)
    is left exactly as it is; only slots still empty (primary < 25,
    backup < 5) are filled, best-scoring new story first, into the
    lowest free positions of the group (primary 1-25, backup 26-30).

    A new story whose content-dedup repeats_story_id points at a story
    already in this episode is skipped -- it covers the same event the
    episode already has.

    Once the episode is approved (or rejected/published) the caller
    never reaches this: classify_existing_episode blocks those, and
    reprocess is the only way to redo a rejected one.
    """

    existing_rows = (
        db.query(EpisodeStory).filter(EpisodeStory.episode_id == episode.id).all()
    )
    kept_ids = {row.story_id for row in existing_rows}
    used_positions = {row.rank_position for row in existing_rows}
    primary_count = sum(1 for row in existing_rows if row.selection_status == "primary")
    backup_count = len(existing_rows) - primary_count

    scored, _top, already_primary_story_ids, content_repeats_flagged = (
        _score_and_select_top_stories(db, now, episode.episode_date)
    )

    candidates = []
    skipped_repeats = 0
    for entry in scored:
        item, state = entry[0], entry[1]
        if item.id in kept_ids:
            continue
        if state.repeats_story_id is not None and state.repeats_story_id in kept_ids:
            skipped_repeats += 1
            continue
        candidates.append(entry)

    primary_free = _free_positions(used_positions, 1, PRIMARY_SLOTS)[: max(0, PRIMARY_SLOTS - primary_count)]
    backup_free = _free_positions(used_positions, PRIMARY_SLOTS + 1, TOTAL_SLOTS)[: max(0, BACKUP_SLOTS - backup_count)]

    additions = [(pos, "primary") for pos in primary_free] + [(pos, "backup") for pos in backup_free]

    added_primary = 0
    added_backup = 0
    for (position, status), (item, state, score, reason) in zip(additions, candidates):
        db.add(EpisodeStory(
            episode_id=episode.id,
            story_id=item.id,
            rank_position=position,
            selection_status=status,
            rank_score=score,
            rank_reason=reason,
        ))
        if status == "primary":
            added_primary += 1
        else:
            added_backup += 1

    if added_primary or added_backup:
        episode.content_changed_at = now

    db.commit()

    result = {
        "episode_id": episode.id,
        "episode_date": episode.episode_date.isoformat(),
        "created": False,
        "updated": True,
        "reason": "existing_draft_updated",
        "kept_stories": len(existing_rows),
        "added_primary": added_primary,
        "added_backup": added_backup,
        "primary_total": primary_count + added_primary,
        "backup_total": backup_count + added_backup,
        "eligible_new": len(candidates),
        "skipped_repeats_of_episode": skipped_repeats,
    }

    print(f"[ranking] Draft episode updated, first-run stories kept: {result}")

    return result


def _run_ranking_selection(db, episode_date: date, now: datetime) -> dict:
    """
    The actual idempotent selection logic, taking `db` explicitly so it
    can be exercised directly in tests (same split as
    app/content/storyboard_service.py's ensure_storyboard/
    app/tasks/episode_video.py's produce_episode_video) rather than only
    through the Celery task, which owns its own SessionLocal().

    Idempotent per episode_date, enforced two ways
    (app/models.py's uq_editorial_episodes_episode_date is the final
    backstop; this function is the layer that actually understands
    *why*):

    - No existing Episode for episode_date -> create one, as before.
    - Existing Episode found -> never create another. A draft is
      UPDATED in place (_update_draft_episode): its first-run
      EpisodeStory rows stay untouched and only empty slots are
      topped up from newly eligible stories -- the explicit reprocess
      endpoint is still the only way to replace the selection.
      rejected/approved/published are blocked outright, since normal
      selection must never silently resurrect a rejection, invalidate
      an approval, or touch anything already live. See
      _existing_episode_result().
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
        if blocked_result.get("reason") == "existing_draft_reused":
            return _update_draft_episode(db, existing_episodes[0], now)
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
    episode.video_started_at = None
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
