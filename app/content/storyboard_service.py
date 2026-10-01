"""
Shared storyboard service: story -> deterministic storyboard -> rendered
scenes -> composed video -> QA, plus the reuse-vs-regenerate prerequisite
check used by episode production.

This is the ONE storyboard-generation implementation in the codebase.
Both callers reach it the same way, via ensure_storyboard below:
- app.content.episode_renderer.render_episode (PROD/CLI episode
  production), for the automatic "every selected story must have a
  valid storyboard before rendering" prerequisite.
- app.content.episode_renderer.render_story_standalone (the DEV-only
  single-story endpoint, app.tasks.storyboard_prototype), for the exact
  same content+storyboard prerequisite before running the canonical
  enhanced story renderer on that one story.

produce_storyboard_prototype below is the unconditional generate ->
compose -> QA implementation ensure_storyboard calls internally
whenever a storyboard is missing or stale -- not a separate rendering
path, and no longer called directly by any DEV endpoint.
"""
import json
from datetime import datetime, timezone

from app.content.storyboard_composer import compose_storyboard_video
from app.content.storyboard_generator import generate_storyboard
from app.models import NewsItem, StoryContent, StoryState
from app.qa.storyboard_qa import run_storyboard_qa_checks
from app.tasks.content import MEDIA_ROOT, ensure_script_and_voice, get_or_create_content


def produce_storyboard_prototype(db, story_id: int) -> dict:
    """
    Unconditional generate -> compose -> QA for exactly ONE story.
    Fully story-agnostic (no story_id branching anywhere in this
    function or anything it calls).

    Writes NOTHING to the database beyond StoryContent (via
    ensure_script_and_voice, only if content isn't ready yet) -- no
    Episode/EpisodeStory row, no new StoryState row. The storyboard
    itself is a plain JSON file (media/storyboard/{story_id}/
    storyboard.json, directly inspectable) and the rendered media files,
    both simply overwritten in place on re-run.
    """
    item = db.get(NewsItem, story_id)
    state = db.get(StoryState, story_id)
    content = get_or_create_content(db, story_id)

    if item is None or state is None:
        return {"story_id": story_id, "status": "failed", "stage": "lookup", "error": "Story not found"}

    if not _content_ready(content):
        if not ensure_script_and_voice(db, item, content):
            return {
                "story_id": story_id,
                "status": "failed",
                "stage": "content",
                "error": content.error_message or "Content production failed",
            }

    try:
        storyboard = generate_storyboard(item, state, content)
    except Exception as exc:
        return {"story_id": story_id, "status": "failed", "stage": "storyboard_generate", "error": str(exc)}

    story_dir = MEDIA_ROOT / "storyboard" / str(story_id)
    story_dir.mkdir(parents=True, exist_ok=True)
    storyboard_path = story_dir / "storyboard.json"
    storyboard_path.write_text(json.dumps(storyboard, indent=2))

    output_path = MEDIA_ROOT / "videos" / f"{story_id}_storyboard.mp4"

    try:
        compose_storyboard_video(storyboard, content, story_dir, output_path)
    except Exception as exc:
        return {"story_id": story_id, "status": "failed", "stage": "storyboard_compose", "error": str(exc)}

    qa_results = run_storyboard_qa_checks(storyboard, story_dir, output_path)

    result = {
        "story_id": story_id,
        "status": "ready" if all(check["passed"] for check in qa_results) else "qa_failed",
        "video_path": str(output_path),
        "storyboard_path": str(storyboard_path),
        "scene_count": len(storyboard["scenes"]),
        "scene_types": [scene["scene_type"] for scene in storyboard["scenes"]],
        "qa": qa_results,
    }

    print(f"[storyboard-service] Completed: {result}")

    return result


def _content_ready(content: StoryContent) -> bool:
    return bool(content.script_text and content.audio_path and content.caption_segments)


def _storyboard_is_valid(story_id: int, content: StoryContent) -> bool:
    """Deterministic reuse-vs-regenerate check: a storyboard is valid if
    its JSON file exists and was written at or after this story's content
    was last updated. Mirrors the same "compare a content-changed
    timestamp against when the artifact was produced" pattern already
    used for episode-level QA staleness (Episode.content_changed_at vs
    qa_run_at, see app/tasks/ranking.py's reprocess docstring / the
    dashboard's qaIsStale()) -- here at story granularity, using
    StoryContent.updated_at, which already exists and is already kept
    current by SQLAlchemy's onupdate on every content change."""
    storyboard_path = MEDIA_ROOT / "storyboard" / str(story_id) / "storyboard.json"
    if not storyboard_path.exists():
        return False
    if content.updated_at is None:
        return True
    mtime = datetime.fromtimestamp(storyboard_path.stat().st_mtime, tz=timezone.utc)
    updated_at = content.updated_at
    if updated_at.tzinfo is None:
        # The real Postgres column is DateTime(timezone=True) and always
        # comes back aware; only a naive-datetime test backend (SQLite)
        # can reach here. Treat a naive value as UTC rather than raising,
        # so this check degrades safely instead of crashing.
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return mtime >= updated_at


def mark_storyboard_current(story_id: int) -> None:
    """Re-stamp the storyboard's mtime after a status-only write to the
    story's row (e.g. "video_ready"). That write bumps StoryContent.updated_at
    via onupdate, which would otherwise make every just-rendered story look
    edited and force a full storyboard regeneration on the next Produce.
    Only call it when the storyboard was already valid before the write."""
    storyboard_path = MEDIA_ROOT / "storyboard" / str(story_id) / "storyboard.json"
    if storyboard_path.exists():
        storyboard_path.touch()


def ensure_storyboard(db, story_id: int) -> dict:
    """
    Automatic prerequisite for episode production (called as a plain
    function -- no Celery task, no HTTP endpoint): make sure story_id
    has both ready production content and a valid storyboard, reusing
    whatever is already present and fresh, generating only what's
    missing or stale.

    - Content (script/audio/captions): reuses app.tasks.content's
      ensure_script_and_voice ("ensure audio") -- deliberately NOT the
      old visual-card/composed-video stages; the enhanced Pillow
      renderer never needs those, and must never trigger them as a
      hidden side effect of getting content ready.
    - Storyboard + rendered scene video + QA: reuses
      produce_storyboard_prototype's generate/compose/QA pipeline
      unchanged, gated by _storyboard_is_valid ("ensure storyboard" +
      "render story video" + "story QA").

    Returns the same result shape produce_storyboard_prototype does
    (status: "ready" | "qa_failed" | "failed", plus "stage"/"error" on
    failure), with an added "reused" key. Never raises -- a failure is
    reported in the returned dict so the caller can decide whether to
    stop (episode_renderer.ensure_all_storyboards treats only "failed"
    as fatal; "qa_failed" already means a usable storyboard and video
    exist, just with a documented QA warning).
    """
    item = db.get(NewsItem, story_id)
    content = get_or_create_content(db, story_id)

    if item is None:
        return {"story_id": story_id, "status": "failed", "stage": "lookup", "error": "Story not found"}

    if not _content_ready(content):
        if not ensure_script_and_voice(db, item, content):
            return {
                "story_id": story_id,
                "status": "failed",
                "stage": "content",
                "error": content.error_message or "Content production failed",
            }

    if _storyboard_is_valid(story_id, content):
        return {"story_id": story_id, "status": "ready", "reused": True}

    result = produce_storyboard_prototype(db, story_id)
    result["reused"] = False
    return result
