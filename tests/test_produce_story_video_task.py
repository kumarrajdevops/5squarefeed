"""
Focused tests for the migrated POST /api/v1/stories/{id}/produce path:
app.tasks.content.produce_story_video_task (the task) and
app.main.trigger_content_production (the endpoint). Both are thin
orchestration around app.content.episode_renderer.render_story_standalone
-- the actual canonical enhanced-renderer behavior (content -> storyboard
-> render_story_enhanced -> mux) is exercised end to end by
tests/test_episode_renderer_standalone.py; these tests only confirm the
task persists that result onto StoryContent correctly, and that the
endpoint queues the right task, without re-running real ffmpeg.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from app import main
from app.models import NewsItem, StoryContent
from app.tasks import content as content_task


def _make_story(db, **overrides):
    defaults = dict(
        title="A Story",
        canonical_url="https://example.com/story",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=date(2026, 9, 26),
        status="collected",
        raw_summary="Some raw summary.",
    )
    defaults.update(overrides)
    story = NewsItem(**defaults)
    db.add(story)
    db.flush()
    return story


def test_produce_story_video_task_persists_enhanced_video_on_success(db_session, monkeypatch):
    story = _make_story(db_session)
    db_session.close = lambda: None
    monkeypatch.setattr(content_task, "SessionLocal", lambda: db_session)

    fake_result = {
        "story_id": story.id, "status": "ready", "reused": False,
        "video_path": f"media/pillow_enhanced/{story.id}_pillow_enhanced.mp4",
        "scene_count": 4, "duration_seconds": 20.1,
    }
    monkeypatch.setattr(
        "app.content.episode_renderer.render_story_standalone",
        lambda sid: fake_result,
    )

    result = content_task.produce_story_video_task(story.id)

    assert result == fake_result
    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert content.status == "video_ready"
    assert content.video_path == fake_result["video_path"]
    assert content.error_message is None


def test_produce_story_video_task_marks_content_failed_on_render_failure(db_session, monkeypatch):
    story = _make_story(db_session)
    db_session.close = lambda: None
    monkeypatch.setattr(content_task, "SessionLocal", lambda: db_session)

    fake_result = {"story_id": story.id, "status": "failed", "stage": "storyboard_compose", "error": "boom"}
    monkeypatch.setattr(
        "app.content.episode_renderer.render_story_standalone",
        lambda sid: fake_result,
    )

    result = content_task.produce_story_video_task(story.id)

    assert result == fake_result
    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert content.status == "failed"
    assert "storyboard_compose" in content.error_message
    assert content.video_path is None


def test_produce_endpoint_queues_the_canonical_render_task(db_session, monkeypatch):
    story = _make_story(db_session)
    db_session.close = lambda: None
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    mock_delay = MagicMock(return_value=MagicMock(id="fake-task-id"))
    monkeypatch.setattr(main.produce_story_video_task, "delay", mock_delay)

    result = main.trigger_content_production(story.id)

    assert result == {"story_id": story.id, "task_id": "fake-task-id", "status": "queued"}
    mock_delay.assert_called_once_with(story.id)


def test_produce_endpoint_404_for_missing_story(db_session, monkeypatch):
    db_session.close = lambda: None
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    mock_delay = MagicMock()
    monkeypatch.setattr(main.produce_story_video_task, "delay", mock_delay)

    with pytest.raises(HTTPException) as exc_info:
        main.trigger_content_production(999999)

    assert exc_info.value.status_code == 404
    mock_delay.assert_not_called()
