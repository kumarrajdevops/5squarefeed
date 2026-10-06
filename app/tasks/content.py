import json
from pathlib import Path

from app.content.briefing.loader import load_corroborating
from app.content.script_generator import generate_script
from app.content.video_composer import get_audio_duration_seconds
from app.content.voice_generator import synthesize_voice
from app.db import SessionLocal
from app.models import NewsItem, StoryContent
from app.worker.celery_app import celery_app


# Object-storage-style layout, matching the folder structure described
# in the architecture (raw-news/, images/, audio/, captions/, videos/).
# Local filesystem for now, under the project's bind-mounted volume;
# swapping this for S3 later only touches this constant + the path
# helpers, not the task logic.
MEDIA_ROOT = Path("media")


def get_or_create_content(db, story_id: int) -> StoryContent:
    """
    Shared with app/tasks/episode_video.py -- not underscore-prefixed
    since it's used across task modules, not just this one.
    """
    content = (
        db.query(StoryContent)
        .filter(StoryContent.story_id == story_id)
        .first()
    )

    if content is None:
        content = StoryContent(story_id=story_id, status="pending")
        db.add(content)
        db.flush()

    return content


def mark_content_failed(db, content: StoryContent, stage: str, exc: Exception) -> None:
    content.status = "failed"
    content.error_message = f"[{stage}] {exc}"
    db.commit()
    print(f"[content] {stage} failed for story {content.story_id}: {exc}")


def _persist_briefing_meta(content: StoryContent, briefing: dict | None) -> None:
    if not briefing:
        return
    content.script_word_count = briefing.get("word_count")
    content.script_sentence_count = briefing.get("sentence_count")
    content.script_quality_status = briefing.get("quality_status")
    content.script_generation_reason = briefing.get("reason")
    content.script_meta = json.dumps(briefing.get("meta"), default=str)


def ensure_script_and_voice(db, story: NewsItem, content: StoryContent) -> bool:
    """
    Script, then voice/audio/caption-timing -- the real prerequisite the
    storyboard pipeline needs (app.content.storyboard_service.
    ensure_storyboard calls this, and so does produce_story_video_task
    below via ensure_storyboard). Deliberately stops short of a visual-
    card image and a separately-composed video -- the enhanced Pillow/
    storyboard renderer (app.content.episode_renderer.render_story_enhanced)
    needs only script+audio+captions as its content prerequisite, not a
    single static card image, so this function must never produce one
    as a side effect.

    Lives here (not in storyboard_service.py) so the latter can import
    it without a circular import -- this module has no dependency on
    storyboard_service.py or episode_renderer.py.

    Returns True if script + voice are ready, False if either stage
    failed.
    """
    if not content.script_text:
        try:
            kwargs = {}
            corroborating = load_corroborating(db, story)
            if corroborating:
                kwargs["corroborating"] = corroborating
            script = generate_script(
                title=story.title, raw_summary=story.raw_summary, raw_content=story.raw_content, **kwargs
            )
            briefing = script.get("briefing")
            _persist_briefing_meta(content, briefing)
            if not script["script_text"]:
                # Too thin to brief: never pad it. Left for a manual swap (QA flags it).
                content.headline = script["headline"]
                content.status = "failed"
                content.error_message = "[script] insufficient for briefing: " + (
                    (briefing or {}).get("reason") or "no usable source text"
                )
                db.commit()
                print(f"[content] story {story.id} insufficient for briefing")
                return False
            content.headline = script["headline"]
            content.summary = script["summary"]
            content.script_text = script["script_text"]
            content.status = "script_ready"
            content.error_message = None
            db.commit()
        except Exception as exc:
            mark_content_failed(db, content, "script", exc)
            return False

    if content.audio_path and content.caption_segments:
        return True

    try:
        audio_path = MEDIA_ROOT / "audio" / f"{story.id}.mp3"
        segments = synthesize_voice(content.script_text, audio_path)
        content.audio_path = str(audio_path)
        content.audio_duration_seconds = get_audio_duration_seconds(audio_path)
        content.caption_segments = json.dumps(segments)
        content.status = "voice_ready"
        content.error_message = None
        db.commit()
    except Exception as exc:
        mark_content_failed(db, content, "voice", exc)
        return False

    return True


@celery_app.task
def produce_story_video_task(story_id: int) -> dict:
    """
    Production implementation behind POST /api/v1/stories/{id}/produce.
    ONE task, not a multi-stage chain: app.content.episode_renderer.
    render_story_standalone already does the entire content -> storyboard
    -> render_story_enhanced -> mux pipeline end to end (the exact same
    canonical enhanced renderer full episode production and the DEV
    endpoint use -- no separate rendering implementation here). This
    task's only job is running that and persisting the result onto
    StoryContent so GET /api/v1/stories/{id}/content keeps working.

    Replaces the old script_task -> voice_task -> visual_task ->
    compose_video_task chain (generate_card/compose_video), which is
    fully removed now that this is the story's only production path.
    """
    from app.content.episode_renderer import render_story_standalone

    result = render_story_standalone(story_id)

    with SessionLocal() as db:
        content = get_or_create_content(db, story_id)

        if result.get("status") == "failed":
            mark_content_failed(db, content, result.get("stage", "render"), RuntimeError(result.get("error")))
            return result

        content.video_path = result["video_path"]
        content.status = "video_ready"
        content.error_message = None
        db.commit()

    print(f"[content] Enhanced video produced for story {story_id}: {result['video_path']}")
    return result
