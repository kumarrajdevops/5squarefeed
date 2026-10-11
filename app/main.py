import json
import re
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text

from app.config import settings
from app.dates import episode_key, episode_made_date, target_collection_date
from app.text_utils import clean_text
from app.db import SessionLocal
from app.dedup.decision import METHOD_VERSION as DEDUP_METHOD_VERSION
from app.models import (
    CollectionRun,
    Episode,
    EpisodePublication,
    EpisodeStory,
    HistoricalStoryRelation,
    NewsItem,
    Notification,
    StoryContent,
    StoryState,
)
from app.publishing.linkedin import post as linkedin
from app.publishing.youtube.summary import ENVIRONMENTS, refresh_publish_summary
from app.tasks.classify import run_classify_new_raw_items
from app.tasks.collection import run_collection
from app.tasks.content import produce_story_video_task
from app.tasks.content_dedup import run_enrich_and_dedup_by_content
from app.tasks.dedup import run_deduplicate_new_stories
from app.tasks.episode_qa import run_episode_qa
from app.tasks.episode_storyboard import read_report as read_storyboard_report
from app.tasks.episode_storyboard import run_episode_storyboards, start_report as start_storyboard_report
from app.tasks.episode_video import produce_episode_video
from app.tasks.historical_dedup import run_historical_dedup
from app.tasks.publishing import publish_episode_to_youtube
from app.tasks.ranking import (
    REPROCESSABLE_STATUSES,
    classify_existing_episode,
    reprocess_episode,
    run_ranking_selection,
)
# _score_and_select_top_stories is the one scoring/eligibility pass run_ranking_selection and
# reprocess_episode both already share -- reused read-only here too, so the Collected Stories
# page's Primary box can show a live "who'd make the cut right now" preview before Rank & Select
# has actually run (Classify/Dedup/etc. change what's eligible; this just re-derives the same
# answer those stages would, nothing is persisted until Rank & Select is actually clicked).
from app.tasks.ranking import PRIMARY_SLOTS, _score_and_select_top_stories
from app.tasks.scheduled import run_daily_processing
from app.tasks.verification import run_verification
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


def _default_support_text(story_id: int, content) -> str:
    """The automatic source/quote line the video would show without an override."""
    try:
        from app.content.scene_renderer import default_support_text
        from app.content.support_facts import load_support_info

        info = load_support_info(story_id)
        if not info:
            return ""
        with SessionLocal() as db:
            item = db.get(NewsItem, story_id)
            title = item.title if item else ""
        return default_support_text(info, title or "", content.headline or "")
    except Exception:
        return ""


class StoryContentUpdate(BaseModel):
    headline: str | None = None
    summary: str | None = None
    script_text: str | None = None
    support_text: str | None = None  # "" clears the override (back to automatic)


class NewsItemUpdate(BaseModel):
    raw_summary: str


class StoryExcludeUpdate(BaseModel):
    excluded: bool = True


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


def _validate_date_param(value: str | None, param_name: str) -> None:
    """
    Shared "YYYY-MM-DD or omit" validation for the DEV-only per-stage
    trigger endpoints below. Validated here rather than left to the
    Celery task: the task runs out-of-process, so an invalid value
    would otherwise fail silently from the caller's perspective
    (HTTP 200 + queued task_id, with the actual ValueError only
    visible in the worker logs).
    """
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {param_name} {value!r}; expected YYYY-MM-DD.",
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
    _validate_date_param(target_date, "target_date")

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


_TAG_BREAK_RE = re.compile(r"</(?:p|div|li|h[1-6]|tr|blockquote)>|<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def _display_summary(raw_summary: str | None) -> str | None:
    """
    Plain-text version of raw_summary for the Collected Stories page's Summary column/editor --
    many RSS feeds (WordPress ones especially) ship full unstripped HTML in their <description>
    (<p>/<img> tags, an "appeared first on <a href=...>" boilerplate link), which read as garbage
    in a plain textarea. Same building blocks app/content/briefing/hygiene.py's _to_plain uses for
    the real script-generation pipeline (that path already cleans this before scripting -- this
    page's display/edit path never did). raw.news_items itself is NEVER rewritten here -- this
    only reshapes a copy for display; PATCHing this field back only overwrites the stored value
    if an editor explicitly saves an edit.
    """
    if not raw_summary:
        return raw_summary
    text_ = _TAG_BREAK_RE.sub("\n", raw_summary)
    text_ = _TAG_RE.sub(" ", text_)
    text_ = clean_text(unescape(text_)) or ""
    text_ = _WS_RE.sub(" ", text_)
    return _BLANK_LINES_RE.sub("\n", text_).strip()


def _sufficiency_reason(state: StoryState | None) -> str | None:
    # sufficiency_detail is content_dedup.assess_source_sufficiency's JSON blob (scoring
    # internals); its own "reason" key is already the human-readable summary -- same pattern as
    # dedup_reason/verification_reason, just one JSON-decode away instead of a plain column.
    if state is None or not state.sufficiency_detail:
        return None
    try:
        return json.loads(state.sufficiency_detail).get("reason")
    except (ValueError, AttributeError):
        return None


def _news_item_dict(item: NewsItem, state: StoryState | None) -> dict:
    return {
        "id": item.id,
        "source_name": item.source_name,
        "source_type": item.source_type,
        "title": item.title,
        "canonical_url": item.canonical_url,
        "author": item.author,
        "raw_summary": _display_summary(item.raw_summary),
        "raw_summary_edit_count": item.raw_summary_edit_count,
        "published_at": item.published_at,
        "collected_at": item.collected_at,
        "status": item.status,
        "ai_relevance": state.ai_relevance if state else None,
        "classifier_disposition": state.classifier_disposition if state else None,
        "canonical_story_id": state.canonical_story_id if state else None,
        "dedup_reason": state.dedup_reason if state else None,
        "verification_status": state.verification_status if state else None,
        "verification_reason": state.verification_reason if state else None,
        "source_sufficiency": state.source_sufficiency if state else None,
        "sufficiency_reason": _sufficiency_reason(state),
        "excluded_by_editor": state.excluded_by_editor if state else False,
    }


@app.get("/api/v1/raw/news-items")
def list_news_items(collection_date: str | None = None, limit: int = 500):
    """
    The raw items collected for one day, split into Primary (this date's
    draft/approved episode's episode_stories, ranks 1-30, ordered) and
    Secondary (everything else in the day's pool) -- powers the
    dashboard's Collected Stories page. Primary is empty until Rank &
    Select has created the episode. Left-joins editorial.stories so
    classification shows up once it exists, without requiring it
    (collection itself writes zero rows there -- see StoryState's
    docstring). Defaults to today's target_collection_date() when
    collection_date is omitted.
    """
    limit = max(1, min(limit, 1000))

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
        episode = db.query(Episode).filter(Episode.episode_date == parsed_date).first()
        # This page is for pre-approval triage -- once an episode is approved/published it's
        # locked (same REPROCESSABLE_STATUSES reprocess_episode already enforces), so its
        # selection is never shown or editable here. The pool looks exactly like a fresh,
        # untouched day rather than showing a locked-in primary. No episode at all is NOT
        # locked -- that's the normal pre-ranking state, handled below.
        locked = episode is not None and episode.status not in REPROCESSABLE_STATUSES

        primary = []
        primary_ids: set[int] = set()
        primary_is_preview = False

        persisted_rows = []
        if episode is not None and not locked:
            persisted_rows = (
                db.query(EpisodeStory, NewsItem, StoryState)
                .join(NewsItem, NewsItem.id == EpisodeStory.story_id)
                .outerjoin(StoryState, StoryState.id == EpisodeStory.story_id)
                .filter(EpisodeStory.episode_id == episode.id)
                .order_by(EpisodeStory.rank_position)
                .all()
            )

        if persisted_rows:
            for episode_story, item, state in persisted_rows:
                primary_ids.add(item.id)
                primary.append({
                    **_news_item_dict(item, state),
                    "rank_position": episode_story.rank_position,
                    "selection_status": episode_story.selection_status,
                    "rank_score": episode_story.rank_score,
                    "rank_reason": episode_story.rank_reason,
                })

            # source_sufficiency == "insufficient" is a structural exclusion -- these canonicals
            # never got (and never would get) an EpisodeStory row, since _score_and_select_top_
            # stories drops them before scoring (not enough real article text to generate a
            # script at all). That's categorically different from "scored but didn't make the
            # cut" (real backups, which stay promotable), so they get their own status rather
            # than appearing as a misleading "backup" -- still shown in Primary (not lost in
            # Secondary) since they're genuine ai_candidates worth an editor's attention. "thin"
            # is deliberately NOT included here: it doesn't exclude a story from real scoring (see
            # app/content/briefing/sufficiency.py), so an unselected thin story just lost on score
            # like any other candidate -- nothing structurally special about it.
            needs_source_rows = (
                db.query(NewsItem, StoryState)
                .join(StoryState, StoryState.id == NewsItem.id)
                .filter(
                    NewsItem.collection_date == parsed_date,
                    StoryState.ai_relevance == "ai_candidate",
                    StoryState.canonical_story_id.is_(None),
                    StoryState.source_sufficiency == "insufficient",
                    StoryState.excluded_by_editor.is_(False),
                    NewsItem.id.notin_(primary_ids),
                )
                .order_by(NewsItem.id)
                .all()
            )
            for item, state in needs_source_rows:
                primary_ids.add(item.id)
                primary.append({
                    **_news_item_dict(item, state),
                    "rank_position": None,
                    "selection_status": "needs_source",
                    "rank_score": None,
                    "rank_reason": _sufficiency_reason(state),
                })
        elif not locked:
            # Rank & Select hasn't actually run yet (no episode, or one exists with nothing
            # selected) -- show a live "who'd make the cut right now" preview. ai_candidate is
            # the one and only membership rule for Primary here: every ai_candidate (and its
            # duplicates) belongs in Primary, full stop -- sufficiency, historical-duplicate and
            # already-used-elsewhere are real ranking rules, but they only decide primary/backup/
            # excluded STATUS, not whether something is still an ai_candidate worth seeing here,
            # and not display order (see the chronological sort below `_add_canonical`). So:
            # everyone `_score_and_select_top_stories` would accept gets real-score-derived status,
            # every OTHER ai_candidate canonical it excluded (insufficient source, historical
            # repeat, already used) is still included too, status "primary" so it isn't mistaken
            # for a real exclusion. Nothing here is persisted.
            scored, _, _, _ = _score_and_select_top_stories(db, datetime.now(timezone.utc), parsed_date)
            scored_ids = {item.id for item, _, _, _ in scored}

            extra_canonicals = (
                db.query(NewsItem, StoryState)
                .join(StoryState, StoryState.id == NewsItem.id)
                .filter(
                    NewsItem.collection_date == parsed_date,
                    StoryState.ai_relevance == "ai_candidate",
                    StoryState.canonical_story_id.is_(None),
                    StoryState.excluded_by_editor.is_(False),
                    NewsItem.id.notin_(scored_ids),
                )
                .order_by(NewsItem.collected_at.asc())
                .all()
            )

            # Duplicates (ai_candidate stories pointing at one of these canonicals via
            # canonical_story_id) are excluded from both lists above -- they only ever appear as
            # someone else's canonical_story_id. Shown here too, grouped right under their
            # canonical, so a duplicate ai_candidate is never silently invisible on this page.
            canonical_ids = list(scored_ids) + [item.id for item, _ in extra_canonicals]
            duplicates_by_canonical: dict[int, list] = {}
            if canonical_ids:
                # Same published_at-ascending rule as the canonicals themselves (see the sort
                # below `_add_canonical`) -- sorted in Python, not SQL, so a missing published_at
                # sorts last the same way in both places rather than depending on the DB's NULLS
                # ordering default.
                dup_rows = (
                    db.query(NewsItem, StoryState)
                    .join(StoryState, StoryState.id == NewsItem.id)
                    .filter(
                        StoryState.canonical_story_id.in_(canonical_ids),
                        StoryState.excluded_by_editor.is_(False),
                    )
                    .all()
                )
                dup_rows.sort(key=lambda row: (row[0].published_at is None, row[0].published_at))
                for dup_item, dup_state in dup_rows:
                    duplicates_by_canonical.setdefault(dup_state.canonical_story_id, []).append((dup_item, dup_state))

            canonical_position = 0

            def _add_canonical(item, state, score, reason, status):
                nonlocal canonical_position
                canonical_position += 1
                position = canonical_position
                primary_ids.add(item.id)
                primary.append({
                    **_news_item_dict(item, state),
                    "rank_position": position,
                    "selection_status": status,
                    "rank_score": score,
                    "rank_reason": reason,
                })
                for dup_item, dup_state in duplicates_by_canonical.get(item.id, []):
                    primary_ids.add(dup_item.id)
                    primary.append({
                        **_news_item_dict(dup_item, dup_state),
                        "rank_position": position,
                        "selection_status": "duplicate",
                        "rank_score": score,
                        "rank_reason": dup_state.dedup_reason,
                    })

            # Real score (and PRIMARY_SLOTS cutoff) still decides primary/backup/excluded status --
            # that's a ranking question. Display SEQUENCE on this page is a separate, purely
            # chronological concern until Rank & Select actually runs: every pre-selection stage
            # (Classify, Dedup, Content-Dedup, Historical-Dedup, Verify) should show the same
            # order, so the sequence doesn't jump around run to run -- verified stories first
            # (Verify's cross-source/primary-source evidence), earliest-published-first within
            # each group. A missing published_at sorts last (unknown time, not "earliest").
            candidates = [
                (item, state, score, reason, "primary" if position <= PRIMARY_SLOTS else "backup")
                for position, (item, state, score, reason) in enumerate(scored, start=1)
            ]
            for item, state in extra_canonicals:
                if state.source_sufficiency == "insufficient":
                    # Same "needs_source" status as the persisted branch below -- structurally
                    # excluded (no real article text), not a normal ranking loss.
                    candidates.append((item, state, None, _sufficiency_reason(state), "needs_source"))
                else:
                    candidates.append(
                        (item, state, None, "excluded from real ranking: already used or historical repeat", "primary")
                    )

            candidates.sort(
                key=lambda c: (c[1].verification_status != "verified", c[0].published_at is None, c[0].published_at)
            )

            for item, state, score, reason, status in candidates:
                _add_canonical(item, state, score, reason, status)
            primary_is_preview = True

        secondary_rows = (
            db.query(NewsItem, StoryState)
            .outerjoin(StoryState, StoryState.id == NewsItem.id)
            .filter(NewsItem.collection_date == parsed_date)
            .order_by(NewsItem.collected_at.desc())
            .limit(limit)
            .all()
        )
        secondary = [
            _news_item_dict(item, state) for item, state in secondary_rows if item.id not in primary_ids
        ]
        # "insufficient" ai_candidates now live in Primary as "needs_source" (see above), not here.
        # "thin" stays in Secondary -- unlike "insufficient" it doesn't structurally exclude a
        # story from real scoring (app/content/briefing/sufficiency.py), so an unselected thin
        # story is just a normal ranking loss, not a special case -- still worth sorting to the
        # top so it isn't lost among "not_ai"/unclassified rows in the same pool, just not worth
        # a Primary slot of its own. An editor-excluded ("Not Required") story sorts dead last,
        # after even a plain not_ai row -- it was deliberately removed, the opposite of needing
        # attention. Stable sort: collected_at desc preserved within each of these three groups.
        secondary.sort(
            key=lambda d: (
                2 if d["excluded_by_editor"]
                else 0 if d["ai_relevance"] == "ai_candidate" and d["source_sufficiency"] == "thin"
                else 1
            )
        )

        # Historical-Dedup verdicts live in editorial.historical_story_relations, separate from
        # StoryState.canonical_story_id (same-day dedup) -- reusing _historical_relations (already
        # used by the episode-story-detail endpoint below) rather than duplicating that query/
        # editor_override-coalescing logic here.
        hist = _historical_relations(db, [d["id"] for d in primary + secondary])
        for d in primary + secondary:
            rel = hist.get(d["id"])
            is_dup = rel is not None and rel["decision"] == "duplicate"
            d["historical_duplicate_of"] = rel["matched_story_id"] if is_dup else None
            d["historical_reason"] = rel["reason"] if is_dup else None

        return {
            "collection_date": parsed_date,
            "episode_id": episode.id if episode else None,
            "episode_status": episode.status if episode else None,
            "episode_locked": locked,
            "primary": primary,
            "primary_count": len(primary),
            "primary_is_preview": primary_is_preview,
            "secondary": secondary,
            "secondary_count": len(secondary),
        }


@app.patch("/api/v1/raw/news-items/{item_id}")
def update_news_item_summary(item_id: int, body: NewsItemUpdate):
    """
    Editorial override of the raw, as-collected summary/teaser text (e.g. fixing a source's
    truncated RSS blurb -- see CLAUDE.md's historical "dangling [...] fragment" bug) --
    Collected Stories page only, DEV-only (same guard as the rest of this section).

    raw.news_items is otherwise collection-owned (re-collecting the same item overwrites it, see
    NewsItem's docstring); editing it here is a deliberate, explicit exception. Downstream stages
    that already ran against the OLD text (Classify's relevance call, Content-Dedup's sufficiency
    assessment) are NOT re-run automatically -- re-trigger the relevant stage button to pick up
    the edit, same as any other raw-data change.
    """
    _reject_if_not_dev()

    with SessionLocal() as db:
        item = db.query(NewsItem).filter(NewsItem.id == item_id).first()
        if item is None:
            raise HTTPException(status_code=404, detail=f"No news item with id {item_id}.")

        item.raw_summary = body.raw_summary.strip() or None
        # Every explicit Save counts, even re-saving identical text -- this drives the Summary
        # button's white-to-black gradient (an edit-frequency signal), not a dirty-check.
        item.raw_summary_edit_count += 1
        db.commit()
        return {
            "id": item.id,
            "raw_summary": item.raw_summary,
            "raw_summary_edit_count": item.raw_summary_edit_count,
        }


@app.post("/api/v1/stories/{story_id}/exclude")
def update_story_excluded(story_id: int, body: StoryExcludeUpdate):
    """
    Collected Stories page's "Not Required" button -- an editor's manual "this doesn't belong in
    Primary" call. DEV-only (same guard as the rest of this section).

    Sets StoryState.excluded_by_editor; list_news_items drops an excluded story from Primary/
    needs_source entirely and sorts it last in Secondary. When excluding (not un-excluding) a
    story that's a real EpisodeStory row in this date's episode, that row is deleted too -- a
    display-only hide would leave the actual episode selection (and anything Produce renders)
    including a story the page claims was removed. Same "removed story is eligible again on a
    future reprocess/Rank & Select" dynamic CLAUDE.md already documents for any manual
    EpisodeStory edit -- un-excluding doesn't resurrect the deleted row by itself.
    """
    _reject_if_not_dev()

    with SessionLocal() as db:
        state = db.query(StoryState).filter(StoryState.id == story_id).first()
        if state is None:
            raise HTTPException(status_code=404, detail=f"No editorial state for story {story_id}.")

        state.excluded_by_editor = body.excluded
        episode_stories_deleted = 0

        if body.excluded:
            item = db.query(NewsItem).filter(NewsItem.id == story_id).first()
            episode = (
                db.query(Episode).filter(Episode.episode_date == item.collection_date).first()
                if item
                else None
            )
            if episode is not None:
                episode_stories_deleted = (
                    db.query(EpisodeStory)
                    .filter(EpisodeStory.episode_id == episode.id, EpisodeStory.story_id == story_id)
                    .delete(synchronize_session=False)
                )

        db.commit()
        return {
            "id": story_id,
            "excluded_by_editor": state.excluded_by_editor,
            "episode_stories_deleted": episode_stories_deleted,
        }


@app.post("/api/v1/stories/{story_id}/force-rank")
def force_story_into_rank(story_id: int):
    """
    Collected Stories page's "Move to Rank" button -- an editor's manual "this belongs on the
    board regardless" override, shown on every row in BOTH Primary and Secondary (unlike "Not
    Required", which is Primary-only). Bypasses every normal eligibility rule this page otherwise
    respects -- classification, source sufficiency, dedup/historical-duplicate status, even a
    prior "Not Required" -- and inserts a real EpisodeStory row (selection_status "primary") for
    this story in this date's episode, so it's genuinely selected and will be picked up by
    Produce/the content pipeline, not just displayed differently on this page.

    Creates the date's Episode row if none exists yet (status "draft") -- works at any pipeline
    stage, not only after Rank & Select has already run. Clears excluded_by_editor (force-include
    contradicts a prior exclude). Idempotent: a story already selected in this episode is left
    alone. Deliberately does NOT check episode.status/REPROCESSABLE_STATUSES -- same "operates
    regardless of lock status" precedent as this section's Reset-to-collected button; forcing a
    story into an already-approved/published episode's selection won't itself change anything
    already rendered, but a future re-Produce would pick it up.
    """
    _reject_if_not_dev()

    with SessionLocal() as db:
        item = db.query(NewsItem).filter(NewsItem.id == story_id).first()
        if item is None:
            raise HTTPException(status_code=404, detail=f"No news item with id {story_id}.")

        state = db.query(StoryState).filter(StoryState.id == story_id).first()
        if state is None:
            state = StoryState(id=story_id)
            db.add(state)
        state.excluded_by_editor = False

        episode = db.query(Episode).filter(Episode.episode_date == item.collection_date).first()
        created_episode = False
        if episode is None:
            episode = Episode(episode_date=item.collection_date)
            db.add(episode)
            db.flush()
            created_episode = True

        existing = (
            db.query(EpisodeStory)
            .filter(EpisodeStory.episode_id == episode.id, EpisodeStory.story_id == story_id)
            .first()
        )
        if existing is not None:
            db.commit()
            return {
                "id": story_id,
                "episode_id": episode.id,
                "created_episode": created_episode,
                "already_in_board": True,
                "selection_status": existing.selection_status,
            }

        max_position = (
            db.query(func.max(EpisodeStory.rank_position))
            .filter(EpisodeStory.episode_id == episode.id)
            .scalar()
        ) or 0

        db.add(EpisodeStory(
            episode_id=episode.id,
            story_id=story_id,
            rank_position=max_position + 1,
            selection_status="primary",
            rank_score=1.0,
            rank_reason="Manually forced onto the board via 'Move to Rank' (editor override, bypasses normal eligibility).",
        ))
        db.commit()
        return {
            "id": story_id,
            "episode_id": episode.id,
            "created_episode": created_episode,
            "already_in_board": False,
            "selection_status": "primary",
        }


# ---------------------------------------------------------
# Per-stage processing triggers -- DEV-only. Production never calls
# these individually; the scheduled 4 AM IST pass always runs the full
# sequence via POST /api/v1/episodes/select (app/tasks/scheduled.py's
# run_daily_processing). These exist purely so each stage of that
# sequence (classify -> dedup -> content-dedup -> verify -> rank) can
# be triggered and inspected on its own while testing/debugging --
# same rationale, same _reject_if_not_dev() guard, as
# POST /api/v1/collection/run above.
# ---------------------------------------------------------


@app.post("/api/v1/processing/classify")
def trigger_classify(target_date: str | None = None):
    """
    Create editorial.StoryState rows (ai_relevance) for target_date's
    not-yet-classified raw.news_items -- see
    app/tasks/classify.py::classify_new_raw_items. Idempotent: already-
    classified rows are skipped.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")

    task = run_classify_new_raw_items.delay(target_date_iso=target_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/dedup")
def trigger_dedup(target_date: str | None = None):
    """
    Title-similarity dedup pass over target_date's ai_candidate stories
    -- see app/tasks/dedup.py::deduplicate_new_stories. Idempotent:
    only touches stories with canonical_story_id IS NULL.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")

    task = run_deduplicate_new_stories.delay(target_date_iso=target_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/content-dedup")
def trigger_content_dedup(target_date: str | None = None):
    """
    Full-article-text fetch + same-day content dedup over
    target_date's remaining canonical stories -- see
    app/tasks/content_dedup.py::enrich_and_dedup_by_content. Fetches
    real article pages for stories not already attempted; can take a
    while for a large batch.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")

    task = run_enrich_and_dedup_by_content.delay(target_date_iso=target_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/historical-dedup")
def trigger_historical_dedup(target_date: str | None = None):
    """
    Semantic duplicate check of target_date's remaining canonical AI
    candidates against already-published stories -- see
    app/tasks/historical_dedup.py::deduplicate_against_history. Run it
    after Content-Dedup so the article text is available. Idempotent:
    already-decided stories are skipped; verdicts land in
    editorial.historical_story_relations.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")

    task = run_historical_dedup.delay(target_date_iso=target_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/verify")
def trigger_verify(target_date: str | None = None):
    """
    Fact Extraction + Verification Engine + taxonomy pass over
    target_date's remaining canonical, ai_candidate, still-pending
    stories -- see
    app/tasks/verification.py::run_fact_extraction_and_verification.
    Soft signal only -- never excludes a story from ranking.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")

    task = run_verification.delay(target_date_iso=target_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/rank")
def trigger_rank(episode_date: str | None = None):
    """
    Rank + select the Top 25 + 5 backups and create the episode_date's
    Episode row -- see app/tasks/ranking.py::run_ranking_selection.
    Same idempotency as POST /api/v1/episodes/select (an existing
    episode is never duplicated; an existing draft is topped up with
    new stories while its first-run stories stick, and an approved/
    rejected/published one is left alone), but without that
    endpoint's synchronous pre-check -- the task itself still returns
    a blocked/reused result rather than erroring, it's just not known
    until the task result is read back.
    """
    _reject_if_not_dev()
    _validate_date_param(episode_date, "episode_date")

    task = run_ranking_selection.delay(episode_date)
    return {"task_id": task.id, "status": "queued"}


@app.post("/api/v1/processing/reset-to-collected")
def reset_to_collected(target_date: str | None = None):
    """
    TEMPORARY dev/testing convenience: wipe everything Classify-through-Rank&Select produced for
    target_date, back to "just collected" -- so the whole pipeline can be re-run from Classify
    without re-collecting real news. Synchronous, not a Celery task (plain deletes, no reason to
    queue it). raw.news_items is never touched -- only derived editorial.* state for this date's
    stories: any Episode (and its episode_stories), story_content, historical_story_relations, and
    the editorial.stories rows themselves. DEV-only, same guard as the rest of this section.
    Remove this endpoint (and its dashboard button) once the Collected Stories feature work is done.
    """
    _reject_if_not_dev()
    _validate_date_param(target_date, "target_date")
    parsed_date = date.fromisoformat(target_date) if target_date else target_collection_date()

    with SessionLocal() as db:
        story_ids = [
            row[0] for row in
            db.query(NewsItem.id).filter(NewsItem.collection_date == parsed_date).all()
        ]

        # EpisodeStory (episode_stories_episode_id_fkey) must be deleted before Episode, not after
        # -- Postgres enforces the FK immediately (NOT DEFERRABLE), so deleting the parent first
        # raised ForeignKeyViolation and aborted the whole transaction uncommitted whenever the
        # episode actually had any EpisodeStory rows (i.e. Rank & Select had run) -- the exact
        # "reset was not happening" case, confirmed live: `update or delete on table "episodes"
        # violates foreign key constraint "episode_stories_episode_id_fkey"`.
        episode_stories_deleted = 0
        story_content_deleted = 0
        historical_relations_deleted = 0
        stories_deleted = 0
        if story_ids:
            episode_stories_deleted = (
                db.query(EpisodeStory).filter(EpisodeStory.story_id.in_(story_ids)).delete(synchronize_session=False)
            )
            story_content_deleted = (
                db.query(StoryContent).filter(StoryContent.story_id.in_(story_ids)).delete(synchronize_session=False)
            )
            historical_relations_deleted = (
                db.query(HistoricalStoryRelation)
                .filter(
                    (HistoricalStoryRelation.story_id.in_(story_ids))
                    | (HistoricalStoryRelation.matched_story_id.in_(story_ids))
                )
                .delete(synchronize_session=False)
            )
            stories_deleted = (
                db.query(StoryState).filter(StoryState.id.in_(story_ids)).delete(synchronize_session=False)
            )

        episodes_deleted = db.query(Episode).filter(Episode.episode_date == parsed_date).delete()

        db.commit()

    return {
        "target_date": parsed_date,
        "episodes_deleted": episodes_deleted,
        "episode_stories_deleted": episode_stories_deleted,
        "story_content_deleted": story_content_deleted,
        "historical_relations_deleted": historical_relations_deleted,
        "stories_deleted": stories_deleted,
        "news_items_kept": len(story_ids),
    }


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
    - Existing draft -> queues run_daily_processing, which UPDATES the
      draft: its first-run stories stick and only empty slots are
      topped up (see app/tasks/ranking.py::_update_draft_episode).
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

        if reason != "existing_draft_reused":
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


@app.post("/api/v1/episodes/{episode_id}/process")
def process_episode(episode_id: int):
    """
    Preferred semantic entry point for the PROCESS stage (COLLECT ->
    SELECT -> PROCESS -> REVIEW -> PUBLISH): ensures content, audio,
    storyboard, and per-story enhanced Pillow video for every primary
    story, assembles the episode, mixes master audio, muxes, and runs
    QA. This is the exact same task as /produce above (same queued
    Celery job, same canonical renderer) -- deliberately not a second
    implementation, just the clearer name for the "Process Episode"
    dashboard button. Kept as a separate route (rather than replacing
    /produce) to preserve existing callers/compatibility.
    """
    return trigger_episode_production(episode_id)


@app.post("/api/v1/episodes/{episode_id}/storyboard")
def trigger_episode_storyboard(episode_id: int):
    """
    Build (or reuse) content, audio and the storyboard for every primary
    story, then report what is wrong (storyboard QA failures, stories
    that could not be built) -- the first half of Process, runnable on
    its own so problems show up before the long render. Process later
    reuses everything built here. The report is written as "running"
    here, synchronously, before queueing (same race as Produce).
    """
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")

        if episode.video_status == "producing":
            raise HTTPException(status_code=409, detail="Episode is being processed; try again when it finishes.")

    existing = read_storyboard_report(episode_id)
    if existing is not None and existing.get("status") == "running":
        raise HTTPException(status_code=409, detail="A storyboard check is already running for this episode.")

    start_storyboard_report(episode_id)
    task = run_episode_storyboards.delay(episode_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


@app.get("/api/v1/episodes/{episode_id}/storyboard")
def get_episode_storyboard(episode_id: int):
    """Latest storyboard report for the episode, or status "none"."""
    report = read_storyboard_report(episode_id)
    return report if report is not None else {"episode_id": episode_id, "status": "none"}


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
def trigger_episode_publish(episode_id: int, environment: str | None = None, repost: bool = False):
    """
    Publish an approved, produced episode to YouTube (see
    app/tasks/publishing.py). Manual trigger only, matching Produce/
    QA/Approve -- publishing is the one action in this pipeline with
    a real, externally-visible side effect, so it deliberately never
    auto-cascades from Approve.

    `environment` ("dev"/"prod", default YOUTUBE_ENVIRONMENT) picks the
    destination channel. The same produced video can be published to
    each environment once -- e.g. dev now, prod later once its
    credentials exist -- with per-environment state in
    EpisodePublication rows (rolled up into Episode.publish_*).

    Sets the publication's status = "publishing" here, synchronously,
    same race-avoidance reason as trigger_episode_production() above: a
    poller can't otherwise distinguish "not started yet" from
    "already finished".
    """
    environment = environment or settings.youtube_environment
    if environment not in ENVIRONMENTS:
        raise HTTPException(status_code=400, detail="environment must be 'dev' or 'prod'.")

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

        latest = (
            db.query(EpisodePublication)
            .filter(EpisodePublication.episode_id == episode_id, EpisodePublication.environment == environment)
            .order_by(EpisodePublication.sequence.desc())
            .first()
        )
        if latest is not None and latest.status == "publishing":
            raise HTTPException(status_code=409, detail=f"Episode is already publishing to {environment}.")
        if latest is not None and latest.status == "published" and not repost:
            raise HTTPException(
                status_code=409,
                detail=f"Episode is already published to {environment} (publication #{latest.sequence}); "
                "pass repost=true to upload it again as a new publication.",
            )
        now = datetime.now(timezone.utc)
        if latest is not None and latest.status == "failed":
            publication = latest  # retry the failed upload in place
        else:
            publication = EpisodePublication(
                episode_id=episode_id, environment=environment,
                sequence=(latest.sequence + 1) if latest is not None else 1,
            )
            db.add(publication)
        publication.status = "publishing"
        publication.started_at = now
        publication.error = None
        db.flush()
        episode.publish_started_at = now
        refresh_publish_summary(db, episode)
        db.commit()
        publication_id = publication.id

    task = publish_episode_to_youtube.delay(episode_id, environment, publication_id)
    return {"episode_id": episode_id, "task_id": task.id, "status": "queued"}


def _prod_youtube_url(db, episode_id: int) -> str:
    latest = (
        db.query(EpisodePublication)
        .filter(
            EpisodePublication.episode_id == episode_id,
            EpisodePublication.environment == "prod",
            EpisodePublication.status == "published",
            EpisodePublication.youtube_url.isnot(None),
        )
        .order_by(EpisodePublication.sequence.desc())
        .first()
    )
    if latest is None:
        raise HTTPException(status_code=409, detail="Publish this episode to PROD on YouTube first.")
    return latest.youtube_url


@app.get("/api/v1/episodes/{episode_id}/linkedin")
def episode_linkedin_post(episode_id: int, date: str | None = None, time: str | None = None):
    """The LinkedIn post for an episode: text stamped with the episode's IST made-on date and the
    fixed 9:00 AM time, the latest PROD YouTube link, the matching image URL, and the LinkedIn
    compose link. `date` (DD/MM/YYYY) and `time` (H:MM AM) are only for overrides; the image URL
    carries the same values so the text and image always agree. Nothing is posted."""
    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="Episode not found.")
        made_date, made_time = linkedin.post_labels(episode_made_date(episode.created_at))
        date, time = date or made_date, time or made_time
        youtube_url = _prod_youtube_url(db, episode_id)
    try:
        text_ = linkedin.build_post_text(date, time, youtube_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "episode_id": episode_id, "date": date, "time": time, "youtube_url": youtube_url,
        "text": text_,
        "image_url": f"/api/v1/episodes/{episode_id}/linkedin/image?{urlencode({'date': date, 'time': time})}",
        "compose_url": linkedin.compose_url(text_),
    }


@app.get("/api/v1/episodes/{episode_id}/linkedin/image")
def episode_linkedin_image(episode_id: int, date: str | None = None, time: str | None = None):
    if date is None or time is None:
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            if episode is None:
                raise HTTPException(status_code=404, detail="Episode not found.")
            made_date, made_time = linkedin.post_labels(episode_made_date(episode.created_at))
        date, time = date or made_date, time or made_time
    try:
        png = linkedin.render_post_image(date, time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def _serialize_publications(db, episode: Episode) -> dict:
    """Per-environment publish state for the dashboard's dev/prod
    Publish buttons (configured = credentials present in .env)."""
    by_env: dict[str, list] = {}
    for r in (
        db.query(EpisodePublication)
        .filter(EpisodePublication.episode_id == episode.id)
        .order_by(EpisodePublication.sequence.asc())
        .all()
    ):
        by_env.setdefault(r.environment, []).append(r)
    out = {}
    for env in ENVIRONMENTS:
        history = by_env.get(env, [])
        r = history[-1] if history else None  # latest publication drives the button
        out[env] = {
            "count": sum(1 for h in history if h.status == "published"),
            "history": [
                {
                    "sequence": h.sequence, "status": h.status, "published_at": h.published_at,
                    "youtube_url": h.youtube_url, "error": h.error,
                }
                for h in history
            ],
            "sequence": r.sequence if r else None,
            "configured": settings.youtube_configured_for(env),
            "status": r.status if r else "not_published",
            "started_at": r.started_at if r else None,
            "published_at": r.published_at if r else None,
            "youtube_url": r.youtube_url if r else None,
            "error": r.error if r else None,
        }
    return out


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
                "published_at": episode.published_at,
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


def _compute_pipeline_stages(db, episode: Episode, primary_count: int, backup_count: int) -> list[dict]:
    """
    Derives each of the daily pipeline's 10 stages' real status for
    this episode's episode_date -- purely from data that already
    exists (CollectionRun, StoryState, the episode's own fields), no
    new persistence. Collect -> Rank & Select are, by construction,
    already complete for any episode that exists at all: rank/select
    is what creates the Episode row, after running classify -> dedup ->
    content-dedup -> verify synchronously in the same
    run_daily_processing() call (see app/tasks/scheduled.py) -- so
    their real value here is showing WHAT happened (real counts), not
    IF. Produce -> Publish reuse the episode's own existing status
    fields, unchanged.

    Complements, does not replace, the per-stage dashboard buttons
    (see TODO.md's "Visual workflow chart" item) -- a status/count
    snapshot for this one episode's date, not a control surface.
    """
    target_date = episode.episode_date

    collection_run = (
        db.query(CollectionRun)
        .filter(CollectionRun.collection_date == target_date)
        .order_by(CollectionRun.started_at.desc())
        .first()
    )
    if collection_run is None:
        collect_status, collect_detail = "pending", "not yet run"
    elif collection_run.status == "failed":
        collect_status, collect_detail = "failed", f"{collection_run.error_count} error(s)"
    else:
        collect_status = "done" if collection_run.status == "success" else "pending"
        seen = collection_run.rss_items_seen + collection_run.hn_items_seen
        inserted = collection_run.rss_items_inserted + collection_run.hn_items_inserted
        collect_detail = f"{seen} seen, {inserted} new"

    # StoryState rows are created ONLY by classify (never by
    # collection, see that model's own docstring) -- their mere
    # existence for this date's stories IS "classify has run"; every
    # later count below is scoped to this same cohort.
    story_states = (
        db.query(StoryState)
        .join(NewsItem, StoryState.id == NewsItem.id)
        .filter(NewsItem.collection_date == target_date)
        .all()
    )
    classified = len(story_states)
    processed_status = "done" if classified else "pending"

    ai_candidates = sum(1 for s in story_states if s.ai_relevance == "ai_candidate")
    dup_count = sum(1 for s in story_states if s.canonical_story_id is not None)
    repeat_count = sum(1 for s in story_states if s.repeats_story_id is not None)
    historical = _historical_relations(db, [s.id for s in story_states])
    historical_duplicates = sum(1 for r in historical.values() if r["decision"] == "duplicate")
    fetched_count = sum(1 for s in story_states if s.content_fetch_status is not None)
    verified_count = sum(1 for s in story_states if s.verification_status == "verified")
    unverified_count = sum(1 for s in story_states if s.verification_status == "unverified")

    produce_status = {"ready": "done", "failed": "failed"}.get(episode.video_status, "pending")
    produce_detail = {
        "ready": "video ready", "producing": "producing…",
        "failed": "failed", "pending": "not yet run",
    }.get(episode.video_status, episode.video_status)

    if episode.qa_status == "passed":
        qa_stage_status, qa_detail = "done", "all checks passed"
    elif episode.qa_status == "failed":
        qa_report = json.loads(episode.qa_report) if episode.qa_report else []
        failed_checks = sum(1 for c in qa_report if c.get("passed") is False)
        # QA never blocks approval (see main.py's approve endpoint) --
        # a real, honest finding, not a hard failure, so "warn" (same
        # amber family as "pending"), not "failed" (red).
        qa_stage_status, qa_detail = "warn", f"{failed_checks} check(s) failed"
    else:
        qa_stage_status, qa_detail = "pending", "not yet run"

    approve_status = {"approved": "done", "rejected": "failed"}.get(episode.status, "pending")
    approve_detail = {"approved": "approved", "rejected": "rejected", "draft": "awaiting review"}.get(
        episode.status, episode.status
    )

    publish_status = {"published": "done", "failed": "failed"}.get(episode.publish_status, "pending")
    publish_detail = {
        "published": "published", "publishing": "publishing…",
        "failed": episode.publish_error or "failed", "not_published": "not yet published",
    }.get(episode.publish_status, episode.publish_status)

    return [
        {"stage": "collect", "label": "Collect", "status": collect_status, "detail": collect_detail},
        {
            "stage": "classify", "label": "Classify", "status": processed_status,
            "detail": (
                f"{classified} classified, {ai_candidates} AI candidates"
                if classified else "not yet run"
            ),
        },
        {
            "stage": "dedup", "label": "Dedup", "status": processed_status,
            "detail": f"{dup_count} duplicate(s) removed" if classified else "not yet run",
        },
        {
            "stage": "content_dedup", "label": "Content-Dedup", "status": processed_status,
            "detail": (
                f"{fetched_count} article(s) fetched"
                if classified else "not yet run"
            ),
        },
        {
            "stage": "historical_dedup", "label": "Historical-Dedup",
            "status": "done" if historical else "pending",
            "detail": (
                f"{historical_duplicates} repeat(s) held back, {len(historical) - historical_duplicates} new development(s) of covered ground"
                if historical else "not run for this date"
            ),
        },
        {
            "stage": "verify", "label": "Verify", "status": processed_status,
            "detail": f"{verified_count} verified, {unverified_count} unverified" if classified else "not yet run",
        },
        {
            "stage": "rank_select", "label": "Rank & Select", "status": "done",
            "detail": f"{primary_count} primary, {backup_count} backup selected",
        },
        {"stage": "produce", "label": "Produce", "status": produce_status, "detail": produce_detail},
        {"stage": "qa", "label": "QA", "status": qa_stage_status, "detail": qa_detail},
        {"stage": "approve", "label": "Approve", "status": approve_status, "detail": approve_detail},
        {"stage": "publish", "label": "Publish", "status": publish_status, "detail": publish_detail},
    ]


def _historical_relations(db, story_ids) -> dict:
    """
    Historical-dedup verdict per story (current method_version): the duplicate
    verdict if there is one, else the closest related one, with the matched
    story's title and the date of the episode that first published it.
    """
    ids = list(story_ids)
    if not ids:
        return {}
    rows = (
        db.query(HistoricalStoryRelation)
        .filter(
            HistoricalStoryRelation.method_version == DEDUP_METHOD_VERSION,
            HistoricalStoryRelation.story_id.in_(ids),
        )
        .all()
    )
    best: dict[int, HistoricalStoryRelation] = {}
    for row in rows:
        current = best.get(row.story_id)
        key = (row.decision == "duplicate", row.semantic_similarity)
        if current is None or key > (current.decision == "duplicate", current.semantic_similarity):
            best[row.story_id] = row
    matched_ids = {row.matched_story_id for row in best.values()}
    titles = dict(db.query(NewsItem.id, NewsItem.title).filter(NewsItem.id.in_(matched_ids)).all()) if matched_ids else {}
    published = {}
    if matched_ids:
        for story_id, episode_date in (
            db.query(EpisodeStory.story_id, Episode.episode_date)
            .join(Episode, Episode.id == EpisodeStory.episode_id)
            .filter(EpisodeStory.selection_status == "primary", EpisodeStory.story_id.in_(matched_ids))
            .order_by(Episode.episode_date.desc())
            .all()
        ):
            published[story_id] = episode_date  # ends on the earliest date
    return {
        story_id: {
            "decision": row.editor_override or row.decision,
            "detector_decision": row.decision,
            "editor_override": row.editor_override,
            "matched_story_id": row.matched_story_id,
            "matched_title": titles.get(row.matched_story_id),
            "matched_episode_date": published.get(row.matched_story_id),
            "similarity": row.semantic_similarity,
            "title_similarity": row.title_similarity,
            "development_match": row.development_match,
            "new_facts": json.loads(row.new_facts_detected) if row.new_facts_detected else [],
            "rule": row.rule,
            "reason": row.reason,
            "content_basis": row.content_basis,
            "method_version": row.method_version,
        }
        for story_id, row in best.items()
    }


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
    relations = _historical_relations(db, [item.id for _, item, _, _ in rows])

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
            # Semantic historical-dedup verdict (app/dedup); None when the story had no related
            # published coverage. Stories judged duplicates never reach an episode.
            "historical_relation": relations.get(item.id),
            # Needed by the dashboard's "click a story, jump the
            # player" feature (sums preceding durations) and its edit
            # panel -- None until that story's content pipeline runs.
            "content_status": content.status if content else None,
            "headline": content.headline if content else None,
            "summary": content.summary if content else None,
            "script_text": content.script_text if content else None,
            "support_text": content.support_text if content else None,
            # Source sufficiency + script grade (app/content/briefing); labels only.
            "source_sufficiency": state.source_sufficiency,
            "sufficiency_detail": json.loads(state.sufficiency_detail) if state.sufficiency_detail else None,
            "content_extraction_method": state.content_extraction_method,
            "source_word_count": state.source_word_count,
            "script_word_count": content.script_word_count if content else None,
            "script_quality_status": content.script_quality_status if content else None,
            "script_generation_reason": content.script_generation_reason if content else None,
            "content_error": content.error_message if content else None,
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
        "video_started_at": episode.video_started_at,
        "content_changed_at": episode.content_changed_at,
        "qa_status": episode.qa_status,
        "qa_report": json.loads(episode.qa_report) if episode.qa_report else None,
        "qa_run_at": episode.qa_run_at,
        "publish_status": episode.publish_status,
        "publish_started_at": episode.publish_started_at,
        "youtube_url": episode.youtube_url,
        "published_at": episode.published_at,
        "publish_error": episode.publish_error,
        # Not per-episode data -- read fresh from settings so the
        # dashboard can show which credential set/channel a Publish
        # click would actually use, before it's clicked. See
        # app/config.py's youtube_environment (dev vs prod).
        "youtube_environment": settings.youtube_environment,
        "publications": _serialize_publications(db, episode),
        "pipeline": _compute_pipeline_stages(db, episode, len(primary), len(backup)),
        "primary": primary,
        "backup": backup,
    }


@app.post("/api/v1/stories/{story_id}/produce")
def trigger_content_production(story_id: int):
    """
    Kick off the canonical enhanced Pillow story renderer for a single
    story (see app/tasks/content.py's produce_story_video_task): ensure
    content/audio/captions -> ensure a valid storyboard -> the SAME
    render_story_enhanced() used inside every full episode and by the
    DEV standalone endpoint -> mux this story's own processed narration
    on top (app.content.episode_renderer.render_story_standalone). One
    task, not a multi-stage chain -- the old script -> voice -> visual ->
    video Celery chain (generate_card/compose_video) is gone. Poll
    GET /api/v1/stories/{id}/content for progress; once status is
    "video_ready", video_url points at the enhanced standalone MP4.
    """
    with SessionLocal() as db:
        item = db.get(NewsItem, story_id)

        if item is None:
            raise HTTPException(status_code=404, detail="Story not found.")

    task = produce_story_video_task.delay(story_id)
    return {"story_id": story_id, "task_id": task.id, "status": "queued"}


@app.get("/api/v1/stories/{story_id}/content")
def get_story_content(story_id: int):
    """
    Fetch the generated production artifacts for a story: script
    text, and a URL for the enhanced standalone video once it's ready.
    `status` tracks progress (pending -> script_ready -> voice_ready ->
    video_ready, or failed -- see error_message). No separate
    visual_ready stage anymore -- the enhanced Pillow/storyboard
    renderer has no discrete single-image stage the old
    generate_card/compose_video renderer had. image_url/captions_url
    are always None for a story produced this way (no single title-
    card image, no separate downloadable .srt -- captions are burned
    via ASS inside the video itself); kept in the response shape only
    for any pre-migration story whose fields were set by the old,
    now-removed renderer.
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
            "support_text": content.support_text,
            "support_text_default": _default_support_text(story_id, content),
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
    keep being served under this story's video_path/video_url,
    leaving the edited script's audio/video permanently out of sync
    with the text actually shown. Clearing script_text-dependent
    fields (not script_text itself) is also what makes
    ensure_script_and_voice's "skip regeneration if script_text is
    already set" guard regenerate audio/captions FROM the edit rather
    than reusing stale ones.

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
            # A human wrote this text: the generated-script grade no longer applies.
            from app.content.support_facts import split_sentences

            content.script_word_count = len(body.script_text.split())
            content.script_sentence_count = len(split_sentences(body.script_text))
            content.script_quality_status = "edited"
            content.script_generation_reason = "edited by a human"
        if body.support_text is not None:
            content.support_text = body.support_text.strip() or None

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