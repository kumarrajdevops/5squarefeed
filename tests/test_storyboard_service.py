"""
Focused tests for the new automatic storyboard prerequisite
(app.content.storyboard_service) added by the production-pipeline
consolidation: reuse-vs-regenerate staleness logic, and ensure_storyboard's
missing/valid/stale/failure paths. Does not exercise the real ffmpeg
render pipeline (produce_storyboard_prototype's storyboard_generate/
storyboard_compose stages are monkeypatched) -- that's already covered by
tests/test_storyboard_compose_integration.py and friends.
"""
from datetime import date, datetime, timedelta, timezone

from app.content import storyboard_service
from app.models import NewsItem, StoryContent, StoryState


def _make_story(db, **overrides):
    defaults = dict(
        title="A Story",
        canonical_url="https://example.com/a-story",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=date(2026, 9, 22),
        status="collected",
        raw_summary="Some raw summary.",
    )
    defaults.update(overrides)
    story = NewsItem(**defaults)
    db.add(story)
    db.flush()
    db.add(StoryState(id=story.id))
    db.flush()
    return story


def _ready_content(db, story_id, **overrides):
    defaults = dict(
        story_id=story_id,
        script_text="Some script.",
        audio_path="media/audio/x.mp3",
        caption_segments="[]",
    )
    defaults.update(overrides)
    content = StoryContent(**defaults)
    db.add(content)
    db.flush()
    return content


def test_storyboard_is_valid_false_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    content = StoryContent(story_id=1, updated_at=datetime.now(timezone.utc))
    assert storyboard_service._storyboard_is_valid(1, content) is False


def test_storyboard_is_valid_true_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story_dir = tmp_path / "storyboard" / "1"
    story_dir.mkdir(parents=True)
    (story_dir / "storyboard.json").write_text("{}")

    content = StoryContent(story_id=1, updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert storyboard_service._storyboard_is_valid(1, content) is True


def test_storyboard_is_valid_false_when_content_updated_after_storyboard(tmp_path, monkeypatch):
    """The exact staleness case: a human edit (or content regeneration)
    happened AFTER the storyboard was last written -- must regenerate,
    not silently render stale content."""
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story_dir = tmp_path / "storyboard" / "1"
    story_dir.mkdir(parents=True)
    (story_dir / "storyboard.json").write_text("{}")

    content = StoryContent(story_id=1, updated_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert storyboard_service._storyboard_is_valid(1, content) is False


def test_ensure_storyboard_reuses_valid_storyboard_without_regenerating(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story(db_session)
    _ready_content(db_session, story.id)

    story_dir = tmp_path / "storyboard" / str(story.id)
    story_dir.mkdir(parents=True)
    (story_dir / "storyboard.json").write_text("{}")

    calls = []
    monkeypatch.setattr(
        storyboard_service, "produce_storyboard_prototype",
        lambda db, sid: calls.append(sid) or {"story_id": sid, "status": "ready"},
    )

    result = storyboard_service.ensure_storyboard(db_session, story.id)

    assert result["status"] == "ready"
    assert result["reused"] is True
    assert calls == []  # never regenerated


def test_ensure_storyboard_generates_when_missing(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story(db_session)
    _ready_content(db_session, story.id)

    calls = []

    def fake_produce(db, sid):
        calls.append(sid)
        return {"story_id": sid, "status": "ready", "video_path": "x", "qa": []}

    monkeypatch.setattr(storyboard_service, "produce_storyboard_prototype", fake_produce)

    result = storyboard_service.ensure_storyboard(db_session, story.id)

    assert calls == [story.id]
    assert result["status"] == "ready"
    assert result["reused"] is False


def test_ensure_storyboard_regenerates_stale_storyboard(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story(db_session)
    updated_at = datetime.now(timezone.utc)
    _ready_content(db_session, story.id, updated_at=updated_at)

    story_dir = tmp_path / "storyboard" / str(story.id)
    story_dir.mkdir(parents=True)
    (story_dir / "storyboard.json").write_text("{}")
    # Force the on-disk storyboard to look older than the content row by
    # setting updated_at into the future relative to the file's real mtime.
    db_session.query(StoryContent).filter(StoryContent.story_id == story.id).update(
        {"updated_at": updated_at + timedelta(hours=1)}
    )
    db_session.flush()

    calls = []
    monkeypatch.setattr(
        storyboard_service, "produce_storyboard_prototype",
        lambda db, sid: calls.append(sid) or {"story_id": sid, "status": "ready"},
    )

    content = db_session.query(StoryContent).filter(StoryContent.story_id == story.id).first()
    result = storyboard_service.ensure_storyboard(db_session, story.id)

    assert calls == [story.id]
    assert result["reused"] is False


def test_ensure_storyboard_fails_clearly_when_content_missing_and_cannot_be_produced(db_session, tmp_path, monkeypatch):
    """No script/audio/captions, and content production itself fails --
    ensure_storyboard must surface a clear failed status/stage/error, not
    raise an unhandled exception or silently skip the story."""
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story(db_session)
    content = StoryContent(story_id=story.id, status="pending")
    db_session.add(content)
    db_session.flush()

    def fake_ensure_script_and_voice(db, item, content):
        content.error_message = "[script] boom"
        db.commit()
        return False

    monkeypatch.setattr(storyboard_service, "ensure_script_and_voice", fake_ensure_script_and_voice)

    result = storyboard_service.ensure_storyboard(db_session, story.id)

    assert result["status"] == "failed"
    assert result["stage"] == "content"
    assert "boom" in result["error"]


def test_ensure_storyboard_fails_clearly_for_unknown_story(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    result = storyboard_service.ensure_storyboard(db_session, 999999)
    assert result["status"] == "failed"
    assert result["stage"] == "lookup"
