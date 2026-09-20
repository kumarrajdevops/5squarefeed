import json
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, text

from app.content.video_composer import probe_video
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, Story, StoryContent
from app.tasks.content import generate_script_task
from app.tasks.dedup import deduplicate_new_stories
from app.tasks.episode_qa import run_episode_qa
from app.tasks.episode_video import produce_episode_video
from app.tasks.ingestion import ingest_news
from app.tasks.ingestion_hackernews import ingest_hackernews_stories
from app.tasks.publishing import publish_episode_to_youtube
from app.tasks.ranking import run_ranking_selection
from app.tasks.verification import run_fact_extraction_and_verification


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
    title="AI News Platform",
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
    return {"status": "ok", "service": "ai-news-api"}


@app.post("/api/v1/ingestion/rss")
def trigger_rss_ingestion():
    task = ingest_news.delay()
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/ingestion/hackernews")
def trigger_hackernews_ingestion():
    """
    Pull AI-related Hacker News stories (official Algolia search API)
    and chain into deduplication, same as RSS ingestion. Kept as a
    separate endpoint/task from RSS so an HN API outage can't affect
    RSS ingestion.
    """
    task = ingest_hackernews_stories.delay()
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/dedup/run")
def trigger_dedup():
    """
    Manually trigger the deduplication pass. Normally this runs
    automatically at the end of every ingestion cycle, but this
    endpoint is useful for testing/verification, or for re-running
    dedup without doing a full ingestion cycle first.
    """
    task = deduplicate_new_stories.delay()
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/verification/run")
def trigger_verification():
    """
    Manually trigger Fact Extraction + the Verification Engine.
    Normally this runs automatically at the end of every dedup pass
    (see app/tasks/dedup.py), but this endpoint is useful for testing,
    backfilling, or re-running without a full ingestion cycle first.
    Soft signal only -- see app/verification/engine.py's docstring.
    """
    task = run_fact_extraction_and_verification.delay()
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/episodes/select")
def trigger_ranking_selection(run_date: str | None = None):
    """
    Trigger ranking + Top-25/5-backup selection, creating a new
    Episode. Deliberately NOT auto-chained after ingestion/dedup --
    per the architecture's daily cycle, this should run once, after
    the collection cutoff, not after every ingestion pass.

    run_date: optional "YYYY-MM-DD" override, mainly for testing.
    Defaults to today (UTC date) if omitted.

    Validated here rather than left to the Celery task: the task runs
    out-of-process, so an invalid value would otherwise fail silently
    from the caller's perspective (HTTP 200 + queued task_id, with the
    actual ValueError only visible in the worker logs).
    """
    if run_date is not None:
        try:
            date.fromisoformat(run_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid run_date {run_date!r}; expected YYYY-MM-DD.",
            )

    task = run_ranking_selection.delay(run_date)
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
                "run_date": episode.run_date,
                "status": episode.status,
                "video_status": episode.video_status,
                "qa_status": episode.qa_status,
                "publish_status": episode.publish_status,
                "created_at": episode.created_at,
                "primary_count": primary_count,
                "backup_count": backup_count,
            })

        return result


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


def _discovery_info(story: Story) -> dict | None:
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
    if story.source_type == "hackernews" and story.external_id:
        return {
            "label": "Hacker News",
            "url": f"https://news.ycombinator.com/item?id={story.external_id}",
        }
    return None


def _serialize_episode(db, episode: Episode) -> dict:
    """
    Shared serialization for the two episode-viewing endpoints above.
    Splits the selection into primary (Top 25) and backup (next 5)
    lists, each ordered by rank_position, joined against the actual
    story data.
    """

    rows = (
        db.query(EpisodeStory, Story, StoryContent)
        .join(Story, EpisodeStory.story_id == Story.id)
        .outerjoin(StoryContent, StoryContent.story_id == Story.id)
        .filter(EpisodeStory.episode_id == episode.id)
        .order_by(EpisodeStory.rank_position.asc())
        .all()
    )

    primary = []
    backup = []

    for episode_story, story, content in rows:
        entry = {
            "rank_position": episode_story.rank_position,
            "rank_score": episode_story.rank_score,
            "rank_reason": episode_story.rank_reason,
            "story_id": story.id,
            "title": story.title,
            "url": story.url,
            "source_name": story.source_name,
            "author": story.author,
            "published_at": story.published_at,
            "collected_at": story.collected_at,
            "discovery": _discovery_info(story),
            "verification_status": story.verification_status,
            "verification_reason": story.verification_reason,
            "extracted_facts": json.loads(story.extracted_facts) if story.extracted_facts else None,
            # Soft signal only (see app/tasks/ranking.py's eligibility
            # comment) -- non-null means content-based similarity
            # flagged this story as likely repeating an already-
            # narrated past story, but it was still selectable; the
            # editor decides whether to swap it out.
            "repeats_story_id": story.repeats_story_id,
            "repeat_reason": story.repeat_reason,
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
        "run_date": episode.run_date,
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
        "primary": primary,
        "backup": backup,
    }


@app.get("/api/v1/stories")
def list_stories(limit: int = 30):
    # Keep the API limit between 1 and 100.
    limit = max(1, min(limit, 100))

    with SessionLocal() as db:
        stories = db.scalars(
            select(Story)
            # Only expose stories classified as AI candidates.
            .where(Story.ai_relevance == "ai_candidate")
            # Exclude stories that were grouped as duplicates of
            # another story -- only the canonical representative of
            # each duplicate cluster should reach downstream ranking.
            .where(Story.canonical_story_id.is_(None))
            # Show newest published stories first.
            .order_by(Story.published_at.desc())
            # Apply the requested result limit.
            .limit(limit)
        ).all()

        return [
            {
                "id": story.id,
                "title": story.title,
                "url": story.url,
                "source_name": story.source_name,
                "source_type": story.source_type,
                "published_at": story.published_at,
                "collected_at": story.collected_at,
                "status": story.status,
                # Return the filter classification for API consumers.
                "ai_relevance": story.ai_relevance,
                # Return the deterministic relevance score.
                "ai_relevance_score": story.ai_relevance_score,
                # Return why the filter classified the story this way.
                "filter_reason": story.filter_reason,
            }
            for story in stories
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
        story = db.get(Story, story_id)

        if story is None:
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
        duplicates = db.scalars(
            select(Story)
            .where(Story.canonical_story_id == story_id)
            .order_by(Story.published_at.asc())
        ).all()

        return [
            {
                "id": story.id,
                "title": story.title,
                "url": story.url,
                "source_name": story.source_name,
                "published_at": story.published_at,
                "dedup_reason": story.dedup_reason,
            }
            for story in duplicates
        ]