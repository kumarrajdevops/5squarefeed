import json
from datetime import date, datetime, timezone
from pathlib import Path

from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, text

from app.config import settings
from app.content.video_composer import probe_video
from app.dates import episode_key, target_collection_date
from app.db import SessionLocal
from app.models import CollectionRun, Episode, EpisodeStory, NewsItem, Notification, StoryContent, StoryState
from app.tasks.collection import run_collection
from app.tasks.content import generate_script_task
from app.tasks.episode_qa import run_episode_qa
from app.tasks.episode_video import produce_episode_video
from app.tasks.publishing import publish_episode_to_youtube
from app.tasks.ranking import (
    REPROCESSABLE_STATUSES,
    classify_existing_episode,
    reprocess_episode,
)
from app.tasks.scheduled import run_daily_processing
from app.worker.celery_app import celery_app


# Object-storage-style local media root (see app/tasks/content.py).
# Created up front so the StaticFiles mount below always has a valid
# directory to serve, even before any content has been generated yet.
MEDIA_ROOT = Path("media")
for _subdir in ("audio", "images", "captions", "videos"):
    (MEDIA_ROOT / _subdir).mkdir(parents=True, exist_ok=True)


class RevalidateStaticFiles(StaticFiles):
    """
    Files under /media (episode_N.mp4, {story_id}.mp4, etc.) are
    regenerated in place at a fixed URL every time Produce runs --
    there's no content-hashed filename to bust a stale cache. Starlette's
    default FileResponse sends Last-Modified/ETag but no Cache-Control,
    which lets a browser use heuristic freshness and skip revalidation
    entirely, silently playing back an old cached copy after a
    regeneration. Forcing "no-cache" (validate every time, not "never
    cache") keeps the free conditional-GET/304 fast path while
    guaranteeing a changed file is never masked by a stale cache hit.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(
    title="5squareFeed",
    version="0.1.0",
    description="Local-first AI technology news pipeline.",
)

# Serves generated audio/images/captions/videos directly, e.g.
# GET /media/videos/15.mp4 -- lets a browser play back a produced
# story's video without a separate file server.
app.mount("/media", RevalidateStaticFiles(directory=str(MEDIA_ROOT)), name="media")

# Editorial Dashboard (v1) -- vanilla HTML/CSS/JS, no build step. See
# app/dashboard/ and TODO.md for scope/rationale. html=True lets
# GET /dashboard/ serve app/dashboard/index.html directly.
DASHBOARD_ROOT = Path("app/dashboard")
app.mount("/dashboard", StaticFiles(directory=str(DASHBOARD_ROOT), html=True), name="dashboard")


@app.middleware("http")
async def no_store_api_responses(request, call_next):
    """
    The dashboard polls GET /api/v1/episodes/{id} in a tight loop while
    Produce/QA are running (see app/dashboard/app.js) expecting each
    call to reflect current DB state. None of these responses carry
    validators (no ETag/Last-Modified), so browsers shouldn't cache
    them by default -- but forcing it explicitly removes any doubt
    that a poll could ever be satisfied from a stale cached response.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class StoryContentUpdate(BaseModel):
    headline: str | None = None
    summary: str | None = None
    script_text: str | None = None


class ReorderRequest(BaseModel):
    # Full ordered list of story_ids for one selection_status group
    # (primary or backup) -- position in this list becomes the new
    # rank_position, 1-indexed within that group's existing range.
    story_ids: list[int]


class SwapRequest(BaseModel):
    primary_story_id: int
    backup_story_id: int


@app.on_event("startup")
def startup() -> None:
    """
    Verify the database is reachable at startup.

    Schema management belongs entirely to Alembic
    (`alembic upgrade head`) -- this app deliberately does NOT call
    Base.metadata.create_all() here. It used to, and that caused a
    real bug: create_all() builds the full current schema straight
    from the latest models on any fresh database, but Alembic never
    finds out a migration was "applied" that way. A later
    `alembic upgrade head` then tries to re-run migration #1 from
    scratch and fails with DuplicateColumn, because the columns
    already exist. Keeping schema creation solely in Alembic's hands
    avoids that class of bug entirely.

    This still fails loudly and immediately if the database is
    completely unreachable (wrong host/credentials, Postgres not up
    yet) rather than deferring that failure to the first request.
    """
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-news-api", "app_env": settings.app_env}


# Fail-closed allow-list: manual ingestion only runs in these
# environments. Everything else (prod, production, prd, empty/unset,
# any unrecognized value) is rejected -- this only ever guards the two
# manual HTTP trigger endpoints below, never the Celery task functions
# themselves, so the scheduled Beat cycle (which calls those task
# functions directly, not through this API) is completely unaffected.
NON_PRODUCTION_APP_ENVS = {"local", "dev"}


def _reject_if_not_dev() -> None:
    if settings.app_env.strip().lower() not in NON_PRODUCTION_APP_ENVS:
        raise HTTPException(
            status_code=403,
            detail="Manual ingestion is only available in local/dev environments.",
        )


@app.post("/api/v1/collection/run")
def trigger_collection(target_date: str | None = None):
    """
    Trigger one complete "Collect News" operation -- RSS and Hacker
    News together, as a single app.tasks.collection.run_collection
    task producing exactly one raw.collection_runs row (see that
    task's docstring). Replaces the old separate
    /api/v1/ingestion/rss and /api/v1/ingestion/hackernews endpoints,
    which each queued an independent task -- collection is now always
    one operation, never two.

    target_date: optional "YYYY-MM-DD" override, DEV-only (same guard
    as the rest of this function). Defaults to target_collection_date()
    (today IST - 1 day) if omitted -- normal use never needs to pass
    this.
    """
    _reject_if_not_dev()

    if target_date is not None:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid target_date {target_date!r}; expected YYYY-MM-DD.",
            )

    task = run_collection.delay(target_date_iso=target_date, trigger_type="manual")
    return {"task_id": task.id, "status": "queued"}


@app.get("/api/v1/raw/collection-runs")
def list_collection_runs(collection_date: str | None = None, limit: int = 20):
    """
    Durable status/counts for past "Collect News" operations -- the
    authoritative answer to "what actually happened", independent of
    any specific task_id (see app.models.CollectionRun's docstring).
    Defaults to today's target_collection_date() when collection_date
    is omitted.
    """
    limit = max(1, min(limit, 100))

    if collection_date is not None:
        try:
            parsed_date = date.fromisoformat(collection_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid collection_date {collection_date!r}; expected YYYY-MM-DD.",
            )
    else:
        parsed_date = target_collection_date()

    with SessionLocal() as db:
        runs = db.scalars(
            select(CollectionRun)
            .where(CollectionRun.collection_date == parsed_date)
            .order_by(CollectionRun.started_at.desc())
            .limit(limit)
        ).all()

        return [
            {
                "id": run.id,
                "collection_date": run.collection_date,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "trigger_type": run.trigger_type,
                "status": run.status,
                "rss_items_seen": run.rss_items_seen,
                "rss_items_inserted": run.rss_items_inserted,
                "rss_items_updated": run.rss_items_updated,
                "hn_items_seen": run.hn_items_seen,
                "hn_items_inserted": run.hn_items_inserted,
                "hn_items_updated": run.hn_items_updated,
                "error_count": run.error_count,
                "error_details": run.error_details,
            }
            for run in runs
        ]


@app.get("/api/v1/tasks/{task_id}/result")
def get_task_result(task_id: str):
    """
    Read back a Celery task's return value once it's finished, via the
    result backend (Redis -- already configured, see
    app/worker/celery_app.py's `backend=`). Built specifically so the
    DEV "Collect New Stories" dashboard panel can show the real
    result of a run_collection task (RSS + HN counts, target_date --
    see app/tasks/collection.py) without a separate polling mechanism.

    Generic by design (works for any task id, not just ingestion), but
    intentionally minimal: no new persistent storage, no polling
    infrastructure beyond reading what Celery already stores. Returns
    "pending" for a task that hasn't finished (or doesn't exist --
    Celery can't distinguish those without eager result storage of the
    initial PENDING state, which this app doesn't configure).
    """
    result = AsyncResult(task_id, app=celery_app)

    if not result.ready():
        return {"task_id": task_id, "status": "pending"}

    if result.failed():
        return {"task_id": task_id, "status": "failed", "error": str(result.result)}

    return {"task_id": task_id, "status": "success", "result": result.result}


_EXISTING_EPISODE_MESSAGES = {
    "episode_published": "This episode is already published and can never be recreated or modified by selection.",
    "episode_approved": "This episode is already approved. Move it back to an editable state before reprocessing.",
    "episode_rejected": "This episode was rejected. Use POST /api/v1/episodes/{episode_id}/reprocess to explicitly reprocess it.",
}


@app.post("/api/v1/episodes/select")
def trigger_ranking_selection(episode_date: str | None = None):
    """
    Trigger processing (classify -> dedup -> content-dedup ->
    verification -> rank/select -> produce -> QA -- see
    app/tasks/scheduled.py's run_daily_processing) for episode_date.
    Idempotent per episode_date -- at most one Episode may ever exist
    per episode_date (uq_editorial_episodes_episode_date), and this
    endpoint performs a synchronous pre-check so a call that's already
    known to be a no-op never even queues the (expensive) processing
    task:

    - No existing episode -> queues run_daily_processing as before.
    - Existing draft -> 200, returns that episode's id, queues nothing.
    - Existing rejected/approved/published -> 409, queues nothing.
    - More than one existing episode for this episode_date (only
      possible for historical data predating
      uq_editorial_episodes_episode_date) -> 409, every candidate id
      listed, never guessed at.

    This check is a fast-feedback convenience, not the sole guard --
    the real, race-safe protection lives inside ranking's own
    selection step (app/tasks/ranking.py), backed by
    uq_editorial_episodes_episode_date. Two near-simultaneous calls
    can both pass this endpoint's check before either task actually
    inserts; that constraint plus the task's own IntegrityError
    handling is what closes that gap.

    episode_date: optional "YYYY-MM-DD" override, mainly for testing.
    Defaults to target_collection_date() (today IST - 1 day) if
    omitted -- collection and processing always target the same
    calendar day by default.

    Validated here rather than left to the Celery task: the task runs
    out-of-process, so an invalid value would otherwise fail silently
    from the caller's perspective (HTTP 200 + queued task_id, with the
    actual ValueError only visible in the worker logs).
    """
    if episode_date is not None:
        try:
            parsed_episode_date = date.fromisoformat(episode_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid episode_date {episode_date!r}; expected YYYY-MM-DD.",
            )
    else:
        parsed_episode_date = target_collection_date()

    with SessionLocal() as db:
        existing_episodes = (
            db.query(Episode).filter(Episode.episode_date == parsed_episode_date).all()
        )

    if len(existing_episodes) > 1:
        return JSONResponse(
            status_code=409,
            content={
                "episode_date": parsed_episode_date.isoformat(),
                "error": "multiple_existing_episodes",
                "detail": (
                    "Multiple episodes already exist for this episode_date "
                    "(predates uq_editorial_episodes_episode_date) -- "
                    "refusing to guess which one is canonical."
                ),
                "candidate_episode_ids": [e.id for e in existing_episodes],
            },
        )

    if existing_episodes:
        existing = existing_episodes[0]
        reason = classify_existing_episode(existing)

        if reason == "existing_draft_reused":
            return {
                "episode_id": existing.id,
                "episode_date": parsed_episode_date.isoformat(),
                "created": False,
                "reason": reason,
            }

        return JSONResponse(
            status_code=409,
            content={
                "episode_id": existing.id,
                "episode_date": parsed_episode_date.isoformat(),
                "error": reason,
                "detail": _EXISTING_EPISODE_MESSAGES[reason],
            },
        )

    task = run_daily_processing.delay(parsed_episode_date.isoformat())
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/episodes/{episode_id}/produce")
def trigger_episode_production(episode_id: int):
    """
    Produce (or reuse) content for every primary story in the episode
    and concatenate the results into one combined episode video (see
    app/tasks/episode_video.py). Idempotent -- stories that already
    have video_ready content are reused, not regenerated.

    Sets video_status = "producing" here, synchronously, rather than
    leaving that to the Celery task itself: the dashboard starts
    polling GET /episodes/{id} for video_status to leave "producing"
    the instant this endpoint responds, and it can't distinguish "task
    hasn't started yet" from "task already finished" -- both just look
    like "not producing". Flipping the status before returning closes
    that race so a poll can never observe a stale pre-produce status
    and wrongly conclude production is already done.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        episode.video_status = "producing"
        db.commit()

    task = produce_episode_video.delay(episode_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


@app.post("/api/v1/episodes/{episode_id}/qa")
def trigger_episode_qa(episode_id: int):
    """
    Run Automated Video QA (project.md's checklist) against an
    already-produced episode. Does not produce anything itself --
    run POST /api/v1/episodes/{id}/produce first.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

    task = run_episode_qa.delay(episode_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


@app.post("/api/v1/episodes/{episode_id}/reorder")
def reorder_episode_stories(episode_id: int, body: ReorderRequest):
    """
    Set a new rank order for a set of EpisodeStory rows within one
    episode (e.g. the dashboard's drag-to-reorder on the primary
    list). `body.story_ids` is the full ordered list for whichever
    group they currently belong to -- position in the list becomes
    the new rank_position, keeping that group's existing position
    range (so reordering the primary list can't accidentally shift
    ranks into the backup range or vice versa).

    Two-phase update (temporary negative placeholders, then final
    positions) avoids transiently violating the existing
    uq_episode_rank_position unique constraint mid-update.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        rows = (
            db.query(EpisodeStory)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.story_id.in_(body.story_ids),
            )
            .all()
        )

        if len(rows) != len(body.story_ids):
            raise HTTPException(
                status_code=400,
                detail="story_ids must exactly match existing EpisodeStory rows for this episode.",
            )

        rows_by_story_id = {row.story_id: row for row in rows}
        existing_positions = sorted(row.rank_position for row in rows)

        # Phase 1: move every row to a unique negative placeholder so
        # no two rows ever momentarily share a rank_position.
        for row in rows:
            row.rank_position = -row.rank_position
        db.flush()

        # Phase 2: assign final positions in the caller's requested
        # order, reusing the same set of positions that group already
        # occupied (so a primary reorder stays within ranks 1-25, etc).
        for position, story_id in zip(existing_positions, body.story_ids):
            rows_by_story_id[story_id].rank_position = position

        # A reorder changes what this episode's video will actually
        # look like -- flag any existing QA result as stale (see
        # qaIsStale() in app/dashboard/app.js).
        episode.content_changed_at = datetime.now(timezone.utc)

        db.commit()

    return {"episode_id": episode_id, "status": "reordered", "count": len(rows)}


@app.post("/api/v1/episodes/{episode_id}/swap")
def swap_episode_stories(episode_id: int, body: SwapRequest):
    """
    Swap a primary story with a backup story -- the "replace a
    defective story" action from project.md's Top-30 Safety
    Mechanism. Exchanges both rank_position and selection_status
    between the two rows.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        primary_row = (
            db.query(EpisodeStory)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.story_id == body.primary_story_id,
                EpisodeStory.selection_status == "primary",
            )
            .first()
        )
        backup_row = (
            db.query(EpisodeStory)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.story_id == body.backup_story_id,
                EpisodeStory.selection_status == "backup",
            )
            .first()
        )

        if primary_row is None or backup_row is None:
            raise HTTPException(
                status_code=400,
                detail="primary_story_id must be an existing primary story and "
                       "backup_story_id an existing backup story in this episode.",
            )

        # Same transient-collision problem as reorder() above: a
        # direct simultaneous swap sends both UPDATEs to Postgres in
        # one flush, and the unique constraint on (episode_id,
        # rank_position) is checked immediately per statement, not
        # deferred -- the first UPDATE would collide with the second
        # row's still-unchanged position. Move one row out of the
        # real rank range first (confirmed via a real 500 error during
        # testing, not just theory).
        old_primary_rank = primary_row.rank_position
        old_backup_rank = backup_row.rank_position

        primary_row.rank_position = -old_primary_rank
        db.flush()

        backup_row.rank_position = old_primary_rank
        backup_row.selection_status = "primary"
        db.flush()

        primary_row.rank_position = old_backup_rank
        primary_row.selection_status = "backup"

        # A swap changes which story is actually in the episode --
        # flag any existing QA result as stale.
        episode.content_changed_at = datetime.now(timezone.utc)

        db.commit()

    return {"episode_id": episode_id, "status": "swapped"}


@app.post("/api/v1/episodes/{episode_id}/approve")
def approve_episode(episode_id: int):
    """
    Human approval of the episode -- doesn't hard-block on qa_status;
    QA is surfaced prominently in the dashboard, but the human makes
    the final call, matching the architecture's human-in-the-loop
    intent.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        episode.status = "approved"
        db.commit()

    return {"episode_id": episode_id, "status": "approved"}


@app.post("/api/v1/episodes/{episode_id}/reject")
def reject_episode(episode_id: int):
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        episode.status = "rejected"
        db.commit()

    return {"episode_id": episode_id, "status": "rejected"}


@app.post("/api/v1/episodes/{episode_id}/reprocess")
def reprocess_episode_endpoint(episode_id: int):
    """
    Explicit, auditable redo of selection for an existing episode --
    the only sanctioned way to change a draft/rejected episode's story
    selection. Deliberately a separate, clearly-named operation rather
    than a force=true flag on /episodes/select: normal selection must
    stay safe to call repeatedly (see that endpoint's docstring);
    reprocessing is a distinct, intentional action operating on a
    specific episode_id, never on an episode_date.

    Allowed only when status is "draft" or "rejected". "approved" and
    "published" are always blocked -- reprocessing would either
    silently invalidate a human sign-off or rewrite content already
    live on YouTube. There is no override for either case here; an
    approved episode must be explicitly rejected/reset first.

    Synchronous pre-check for the same fast-feedback reason
    /episodes/select has one -- the task (reprocess_episode,
    app/tasks/ranking.py) repeats this check itself before doing
    anything, since it runs out-of-process.
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        if episode.status not in REPROCESSABLE_STATUSES:
            reason = classify_existing_episode(episode)
            return JSONResponse(
                status_code=409,
                content={
                    "episode_id": episode.id,
                    "episode_date": episode.episode_date.isoformat(),
                    "error": reason,
                    "detail": _EXISTING_EPISODE_MESSAGES.get(
                        reason,
                        "This episode cannot be reprocessed in its current state.",
                    ),
                },
            )

    task = reprocess_episode.delay(episode_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


@app.post("/api/v1/episodes/{episode_id}/publish")
def trigger_episode_publish(episode_id: int):
    """
    Publish an approved, produced episode to YouTube (see
    app/tasks/publishing.py). Manual trigger only, matching Produce/
    QA/Approve -- publishing is the one action in this pipeline with
    a real, externally-visible side effect, so it deliberately never
    auto-cascades from Approve.

    Sets publish_status = "publishing" here, synchronously, same
    race-avoidance reason as trigger_episode_production() above: a
    poller can't otherwise distinguish "not started yet" from
    "already finished".
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        if episode.status != "approved":
            raise HTTPException(
                status_code=400,
                detail="Episode must be approved before publishing.",
            )

        if episode.video_status != "ready":
            raise HTTPException(
                status_code=400,
                detail="Episode has no produced video yet -- run Produce first.",
            )

        episode.publish_status = "publishing"
        db.commit()

    task = publish_episode_to_youtube.delay(episode_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


@app.get("/api/v1/episodes")
def list_episodes():
    """
    Summary list of every episode, newest first -- powers the
    dashboard's episode list view. Full detail (Top 30, QA report)
    is fetched per-episode via GET /api/v1/episodes/{id}.
    """
    with SessionLocal() as db:
        episodes = db.scalars(
            select(Episode).order_by(Episode.created_at.desc())
        ).all()

        result = []
        for episode in episodes:
            primary_count = (
                db.query(EpisodeStory)
                .filter(
                    EpisodeStory.episode_id == episode.id,
                    EpisodeStory.selection_status == "primary",
                )
                .count()
            )
            backup_count = (
                db.query(EpisodeStory)
                .filter(
                    EpisodeStory.episode_id == episode.id,
                    EpisodeStory.selection_status == "backup",
                )
                .count()
            )
            result.append({
                "episode_id": episode.id,
                "episode_date": episode.episode_date,
                "episode_key": episode_key(episode.episode_date),
                "status": episode.status,
                "video_status": episode.video_status,
                "qa_status": episode.qa_status,
                "publish_status": episode.publish_status,
                "created_at": episode.created_at,
                "primary_count": primary_count,
                "backup_count": backup_count,
            })

        return result


@app.get("/api/v1/notifications")
def list_notifications(limit: int = 50):
    """
    Failure-alert log, newest first (see app/notifications/notifier.py
    -- episode video production totally failing, or a YouTube publish
    failing; nothing else triggers one, by design). Always recorded
    here regardless of Slack delivery (`delivered` reflects whether
    that actually succeeded) -- this endpoint is the full audit trail,
    Slack is just the real-time page.
    """
    limit = max(1, min(limit, 200))

    with SessionLocal() as db:
        notifications = db.scalars(
            select(Notification).order_by(Notification.created_at.desc()).limit(limit)
        ).all()

        return [
            {
                "id": n.id,
                "created_at": n.created_at,
                "category": n.category,
                "episode_id": n.episode_id,
                "message": n.message,
                "delivered": n.delivered,
            }
            for n in notifications
        ]


@app.get("/api/v1/episodes/latest")
def get_latest_episode():
    """
    Convenience endpoint: fetch the most recently created episode
    without needing to know its id.
    """
    with SessionLocal() as db:
        episode = db.scalars(
            select(Episode).order_by(Episode.created_at.desc()).limit(1)
        ).first()

        if episode is None:
            raise HTTPException(status_code=404, detail="No episodes yet.")

        return _serialize_episode(db, episode)


@app.get("/api/v1/episodes/{episode_id}")
def get_episode(episode_id: int):
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        return _serialize_episode(db, episode)


def _discovery_info(item: NewsItem) -> dict | None:
    """
    When a story was surfaced via an aggregator/discovery channel that's
    distinct from where it was actually published (source_name is
    already the *resolved publisher*, e.g. "nathannaveen.dev" -- see
    app/sources/publisher_resolver.py, not "Hacker News"), returns a
    label + link back to that discovery page so an editor can see how
    the story was found, not just who wrote it. None for sources
    ingested directly (source_type == "rss"), where there's no separate
    discovery channel to show.
    """
    if item.source_type == "hackernews" and item.external_id:
        return {
            "label": "Hacker News",
            "url": f"https://news.ycombinator.com/item?id={item.external_id}",
        }
    return None


def _serialize_episode(db, episode: Episode) -> dict:
    """
    Shared serialization for the two episode-viewing endpoints above.
    Splits the selection into primary (Top 25) and backup (next 5)
    lists, each ordered by rank_position, joined against the actual
    story data (raw.NewsItem for source/publish facts, editorial.
    StoryState for editorial judgment -- see app/models.py).
    """

    rows = (
        db.query(EpisodeStory, NewsItem, StoryState, StoryContent)
        .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
        .join(StoryState, StoryState.id == NewsItem.id)
        .outerjoin(StoryContent, StoryContent.story_id == NewsItem.id)
        .filter(EpisodeStory.episode_id == episode.id)
        .order_by(EpisodeStory.rank_position.asc())
        .all()
    )

    primary = []
    backup = []

    for episode_story, item, state, content in rows:
        entry = {
            "rank_position": episode_story.rank_position,
            "rank_score": episode_story.rank_score,
            "rank_reason": episode_story.rank_reason,
            "story_id": item.id,
            "title": item.title,
            "url": item.canonical_url,
            "source_name": item.source_name,
            "author": item.author,
            "published_at": item.published_at,
            "collected_at": item.collected_at,
            "discovery": _discovery_info(item),
            # Labels only (see app/extraction/taxonomy.py) -- does not
            # affect ranking eligibility or selection.
            "taxonomy_category": state.taxonomy_category,
            "verification_status": state.verification_status,
            "verification_reason": state.verification_reason,
            "extracted_facts": json.loads(state.extracted_facts) if state.extracted_facts else None,
            # Soft signal only (see app/tasks/ranking.py's eligibility
            # comment) -- non-null means content-based similarity
            # flagged this story as likely repeating an already-
            # narrated past story, but it was still selectable; the
            # editor decides whether to swap it out.
            "repeats_story_id": state.repeats_story_id,
            "repeat_reason": state.repeat_reason,
            # Needed by the dashboard's "click a story, jump the
            # player" feature (sums preceding durations) and its edit
            # panel -- None until that story's content pipeline runs.
            "content_status": content.status if content else None,
            "headline": content.headline if content else None,
            "summary": content.summary if content else None,
            "script_text": content.script_text if content else None,
            "audio_duration_seconds": content.audio_duration_seconds if content else None,
            # video_path is a fixed per-story URL that gets overwritten
            # in place on every regeneration (edit -> reset -> re-Produce)
            # -- content_updated_at lets the dashboard cache-bust the
            # <video> src so a browser's media/range-request cache can't
            # keep showing a stale clip under the same URL (see the
            # matching video_produced_at handling on the episode itself).
            "video_url": f"/{content.video_path}" if content and content.video_path else None,
            "content_updated_at": content.updated_at if content else None,
        }

        if episode_story.selection_status == "primary":
            primary.append(entry)
        else:
            backup.append(entry)

    # Needed by the dashboard's "click a story, jump the player"
    # feature -- the combined video starts with this clip before any
    # story. Probed rather than stored, since intro clips are
    # regenerated fresh on every /produce run (see
    # app/tasks/episode_video.py) with no dedicated DB field.
    intro_duration_seconds = None
    intro_path = MEDIA_ROOT / "videos" / f"episode_{episode.id}_intro.mp4"
    if intro_path.exists():
        try:
            intro_duration_seconds = probe_video(intro_path)["duration_seconds"]
        except Exception:
            pass

    return {
        "episode_id": episode.id,
        "episode_date": episode.episode_date,
        "episode_key": episode_key(episode.episode_date),
        "status": episode.status,
        "created_at": episode.created_at,
        "primary_count": len(primary),
        "backup_count": len(backup),
        "video_status": episode.video_status,
        "video_url": f"/{episode.video_path}" if episode.video_path else None,
        "video_produced_at": episode.video_produced_at,
        "content_changed_at": episode.content_changed_at,
        "intro_duration_seconds": intro_duration_seconds,
        "qa_status": episode.qa_status,
        "qa_report": json.loads(episode.qa_report) if episode.qa_report else None,
        "qa_run_at": episode.qa_run_at,
        "publish_status": episode.publish_status,
        "youtube_url": episode.youtube_url,
        "published_at": episode.published_at,
        "publish_error": episode.publish_error,
        # Not per-episode data -- read fresh from settings so the
        # dashboard can show which credential set/channel a Publish
        # click would actually use, before it's clicked. See
        # app/config.py's youtube_environment (dev vs prod).
        "youtube_environment": settings.youtube_environment,
        "primary": primary,
        "backup": backup,
    }


@app.get("/api/v1/stories")
def list_stories(limit: int = 30):
    # Keep the API limit between 1 and 100.
    limit = max(1, min(limit, 100))

    with SessionLocal() as db:
        rows = (
            db.query(NewsItem, StoryState)
            .join(StoryState, StoryState.id == NewsItem.id)
            # Only expose stories classified as AI candidates.
            .filter(StoryState.ai_relevance == "ai_candidate")
            # Exclude stories that were grouped as duplicates of
            # another story -- only the canonical representative of
            # each duplicate cluster should reach downstream ranking.
            .filter(StoryState.canonical_story_id.is_(None))
            # Show newest published stories first.
            .order_by(NewsItem.published_at.desc())
            # Apply the requested result limit.
            .limit(limit)
            .all()
        )

        return [
            {
                "id": item.id,
                "title": item.title,
                "url": item.canonical_url,
                "source_name": item.source_name,
                "source_type": item.source_type,
                "published_at": item.published_at,
                "collected_at": item.collected_at,
                "status": item.status,
                # Return the filter classification for API consumers.
                "ai_relevance": state.ai_relevance,
                # Return the deterministic relevance score.
                "ai_relevance_score": state.ai_relevance_score,
                # Return why the filter classified the story this way.
                "filter_reason": state.filter_reason,
            }
            for item, state in rows
        ]


@app.post("/api/v1/stories/{story_id}/produce")
def trigger_content_production(story_id: int):
    """
    Kick off the full script -> voice -> visual -> video pipeline for
    a single story (see app/tasks/content.py). Each stage chains into
    the next via .delay(); poll GET /api/v1/stories/{id}/content for
    progress.

    Deliberately scoped to one story at a time for now -- this proves
    out the architecture's Script/Voice/Visual/Video stages end to
    end before wiring them up to run across an entire Top-25 episode.
    """
    with SessionLocal() as db:
        item = db.get(NewsItem, story_id)

        if item is None:
            raise HTTPException(status_code=404, detail="Story not found.")

    task = generate_script_task.delay(story_id)
    return {"story_id": story_id, "task_id": task.id, "status": "queued"}


@app.get("/api/v1/stories/{story_id}/content")
def get_story_content(story_id: int):
    """
    Fetch the generated production artifacts for a story: script
    text, and URLs for the audio/image/captions/video files once
    each stage has completed. `status` tracks progress through the
    pipeline (pending -> script_ready -> voice_ready -> visual_ready
    -> video_ready, or failed -- see error_message).
    """
    with SessionLocal() as db:
        content = (
            db.query(StoryContent)
            .filter(StoryContent.story_id == story_id)
            .first()
        )

        if content is None:
            raise HTTPException(
                status_code=404,
                detail="No content generated for this story yet.",
            )

        def media_url(path: str | None) -> str | None:
            # Stored paths already look like "media/audio/15.mp3",
            # matching the /media StaticFiles mount above.
            return f"/{path}" if path else None

        return {
            "story_id": content.story_id,
            "status": content.status,
            "headline": content.headline,
            "summary": content.summary,
            "script_text": content.script_text,
            "audio_url": media_url(content.audio_path),
            "audio_duration_seconds": content.audio_duration_seconds,
            "image_url": media_url(content.image_path),
            "captions_url": media_url(content.captions_path),
            "video_url": media_url(content.video_path),
            "error_message": content.error_message,
            "created_at": content.created_at,
            "updated_at": content.updated_at,
        }


@app.patch("/api/v1/stories/{story_id}/content")
def update_story_content(story_id: int, body: StoryContentUpdate):
    """
    Human override of a story's generated script (dashboard edit
    panel). Only provided fields are changed.

    Resets status to "script_ready" and clears the downstream
    audio/image/captions/video paths -- otherwise a stale video would
    be silently reused by produce_episode_video's existing
    `if content.status == "video_ready"` skip-check
    (app/tasks/episode_video.py), leaving the edited script's audio/
    video permanently out of sync with the text actually shown.

    Also flags any episode containing this story as QA-stale -- a
    story can appear in more than one episode (EpisodeStory is a
    many-to-many join), so every episode referencing it gets stamped,
    not just "the current one" (there isn't one at this endpoint's
    level -- it only knows the story_id).
    """
    with SessionLocal() as db:
        content = (
            db.query(StoryContent)
            .filter(StoryContent.story_id == story_id)
            .first()
        )

        if content is None:
            raise HTTPException(
                status_code=404,
                detail="No content generated for this story yet.",
            )

        if body.headline is not None:
            content.headline = body.headline
        if body.summary is not None:
            content.summary = body.summary
        if body.script_text is not None:
            content.script_text = body.script_text

        content.status = "script_ready"
        content.audio_path = None
        content.audio_duration_seconds = None
        content.caption_segments = None
        content.image_path = None
        content.captions_path = None
        content.video_path = None
        content.error_message = None

        now = datetime.now(timezone.utc)
        affected_episode_ids = (
            db.query(EpisodeStory.episode_id)
            .filter(EpisodeStory.story_id == story_id)
            .distinct()
            .all()
        )
        for (episode_id,) in affected_episode_ids:
            episode = db.get(Episode, episode_id)
            if episode is not None:
                episode.content_changed_at = now

        db.commit()

    return {"story_id": story_id, "status": "script_ready"}


@app.get("/api/v1/stories/{story_id}/duplicates")
def list_duplicates(story_id: int):
    """
    List every story that was grouped as a duplicate of the given
    canonical story. Useful for verifying dedup behavior and for a
    future editorial dashboard ("this story also covered by: ...").
    """

    with SessionLocal() as db:
        rows = (
            db.query(NewsItem, StoryState)
            .join(StoryState, StoryState.id == NewsItem.id)
            .filter(StoryState.canonical_story_id == story_id)
            .order_by(NewsItem.published_at.asc())
            .all()
        )

        return [
            {
                "id": item.id,
                "title": item.title,
                "url": item.canonical_url,
                "source_name": item.source_name,
                "published_at": item.published_at,
                "dedup_reason": state.dedup_reason,
            }
            for item, state in rows
        ]