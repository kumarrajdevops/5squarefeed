import json

from app.content.storyboard_composer import compose_storyboard_video
from app.content.storyboard_generator import generate_storyboard
from app.db import SessionLocal
from app.models import NewsItem, StoryState
from app.qa.storyboard_qa import run_storyboard_qa_checks
from app.tasks.content import MEDIA_ROOT, get_or_create_content
from app.worker.celery_app import celery_app


def produce_storyboard_prototype(db, story_id: int) -> dict:
    """
    DEV-only prototype pipeline: story -> deterministic storyboard ->
    rendered scenes -> composed video -> QA, for exactly ONE story.
    Fully story-agnostic (no story_id branching anywhere in this
    function or anything it calls) -- story_id is hardcoded only in
    the dashboard's DEV trigger button (app/dashboard/app.js), never
    in application code.

    Writes NOTHING to the database -- no Episode/EpisodeStory row, no
    new StoryState row, no StoryContent write (get_or_create_content
    is only ever read from here, never committed to). The only
    persistence is a plain JSON file (media/storyboard/{story_id}/
    storyboard.json, directly inspectable) and the rendered media
    files, both simply overwritten in place on re-run -- completely
    isolated from the production per-story video, the production
    episode, and every other story.
    """
    item = db.get(NewsItem, story_id)
    state = db.get(StoryState, story_id)
    content = get_or_create_content(db, story_id)

    if item is None or state is None:
        return {"story_id": story_id, "status": "failed", "error": "Story not found"}

    if not content.script_text or not content.audio_path or not content.caption_segments:
        return {
            "story_id": story_id,
            "status": "failed",
            "error": "Story has no production content yet -- run POST /api/v1/stories/{id}/produce first.",
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

    print(f"[storyboard-prototype] Completed: {result}")

    return result


@celery_app.task
def run_produce_storyboard_prototype(story_id: int) -> dict:
    with SessionLocal() as db:
        return produce_storyboard_prototype(db, story_id)
