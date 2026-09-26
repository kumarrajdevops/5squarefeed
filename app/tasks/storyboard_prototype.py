"""
DEV-only convenience entry point: POST /api/v1/dev/storyboard-prototype/
{story_id} -> this Celery task -> the SAME canonical enhanced story
renderer episode production uses (app.content.episode_renderer.
render_story_standalone, which itself calls app.content.storyboard_service
.ensure_storyboard for the shared content/storyboard prerequisite and
app.content.episode_renderer.render_story_enhanced for the actual video).
No separate rendering logic lives in this module -- it exists only so a
developer can trigger a single-story enhanced render from the dashboard
without going through full episode production.
"""
from app.content.episode_renderer import render_story_standalone
from app.worker.celery_app import celery_app

__all__ = ["render_story_standalone", "run_produce_storyboard_prototype"]


@celery_app.task
def run_produce_storyboard_prototype(story_id: int) -> dict:
    return render_story_standalone(story_id)
