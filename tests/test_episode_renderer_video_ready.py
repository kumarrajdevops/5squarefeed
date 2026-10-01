"""
Focused tests for the QA-state consistency fix: a story rendered through
the canonical enhanced renderer must actually reach
StoryContent.status == "video_ready" (with video_path persisted), the
one state app/tasks/episode_qa.py's per-story filter relies on -- before
this fix, a story produced only through ensure_script_and_voice topped
out at "voice_ready" and was silently excluded from episode QA even
though its enhanced segment was really in the produced episode.

Covers, in order: the low-level DB write (_mark_story_video_ready), the
render-then-mark ordering that guarantees a failed render is never
mistaken for a successful one (_render_and_mark_story), and the actual
regression at the episode_qa.py level (a story only reachable through
the new pipeline now counts).
"""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.content import episode_renderer
from app.models import Episode, EpisodeStory, NewsItem, StoryContent, StoryState


def _make_story_with_content(db, status="voice_ready", slug="story"):
    story = NewsItem(
        title="A Story",
        canonical_url=f"https://example.com/{slug}",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=date(2026, 9, 26),
        status="collected",
        raw_summary="Some raw summary.",
    )
    db.add(story)
    db.flush()
    content = StoryContent(
        story_id=story.id,
        status=status,
        script_text="Some script.",
        audio_path=f"media/audio/{story.id}.mp3",
        caption_segments="[]",
    )
    db.add(content)
    db.flush()
    return story


# ---------------------------------------------------------------------
# _mark_story_video_ready -- the low-level write.
# ---------------------------------------------------------------------

def test_mark_story_video_ready_sets_status_and_relative_video_path(db_session):
    story = _make_story_with_content(db_session)

    episode_renderer._mark_story_video_ready(db_session, story.id)

    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert content.status == "video_ready"
    # Relative to APP_ROOT, matching every other stored video_path in
    # this codebase (never an absolute path -- see episode_renderer.py's
    # own render_episode()/render_story_standalone() comments on this
    # exact class of bug).
    assert content.video_path == f"media/videos/{story.id}_storyboard.mp4"
    assert content.error_message is None


def test_mark_story_video_ready_keeps_storyboard_reusable(db_session, tmp_path, monkeypatch):
    """The status write bumps StoryContent.updated_at; that must not make the
    already-valid storyboard look stale (full regeneration on every Produce)."""
    import os
    import time

    from app.content import storyboard_service

    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story_with_content(db_session)
    path = tmp_path / "storyboard" / str(story.id) / "storyboard.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    old = time.time() - 60
    os.utime(path, (old, old))

    episode_renderer._mark_story_video_ready(db_session, story.id)

    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert storyboard_service._storyboard_is_valid(story.id, content)


def test_edit_after_storyboard_still_invalidates_it(db_session, tmp_path, monkeypatch):
    import os
    import time

    from app.content import storyboard_service

    monkeypatch.setattr(storyboard_service, "MEDIA_ROOT", tmp_path)
    story = _make_story_with_content(db_session)
    path = tmp_path / "storyboard" / str(story.id) / "storyboard.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    old = time.time() - 60
    os.utime(path, (old, old))

    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    content.script_text = "Edited by a human."
    db_session.commit()

    assert not storyboard_service._storyboard_is_valid(story.id, content)


# ---------------------------------------------------------------------
# _render_and_mark_story -- render, THEN mark; never the reverse.
# ---------------------------------------------------------------------

def test_render_and_mark_story_marks_video_ready_after_a_successful_render(db_session, monkeypatch):
    story = _make_story_with_content(db_session)
    monkeypatch.setattr(
        episode_renderer, "render_story_enhanced",
        lambda story_id, tail_pad, work, position=None: (Path("clip.mp4"), 12.3, []),
    )

    clip, dur = episode_renderer._render_and_mark_story(db_session, story.id, Path("/tmp/work"))

    assert clip == Path("clip.mp4")
    assert dur == 12.3
    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert content.status == "video_ready"


def test_render_and_mark_story_preserves_failure_state_on_render_failure(db_session, monkeypatch):
    story = _make_story_with_content(db_session, status="voice_ready")

    def fake_render_story_enhanced(story_id, tail_pad, work, position=None):
        raise RuntimeError("ffmpeg boom")

    monkeypatch.setattr(episode_renderer, "render_story_enhanced", fake_render_story_enhanced)

    with pytest.raises(RuntimeError, match="ffmpeg boom"):
        episode_renderer._render_and_mark_story(db_session, story.id, Path("/tmp/work"))

    content = db_session.query(StoryContent).filter_by(story_id=story.id).first()
    assert content.status == "voice_ready"  # untouched, never marked video_ready
    assert content.video_path is None


def test_render_and_mark_story_does_not_roll_back_earlier_successful_stories(db_session, monkeypatch):
    """Two stories in one loop iteration-style call: the first renders
    fine and gets marked+committed; the second fails. The first's mark
    must survive (each _mark_story_video_ready call commits its own
    transaction, matching render_episode()'s per-story commit)."""
    story_ok = _make_story_with_content(db_session, slug="ok")
    story_fail = _make_story_with_content(db_session, slug="fail", status="voice_ready")

    def fake_render_story_enhanced(story_id, tail_pad, work, position=None):
        if story_id == story_fail.id:
            raise RuntimeError("ffmpeg boom")
        return (Path("clip.mp4"), 10.0, [])

    monkeypatch.setattr(episode_renderer, "render_story_enhanced", fake_render_story_enhanced)

    episode_renderer._render_and_mark_story(db_session, story_ok.id, Path("/tmp/work"))
    with pytest.raises(RuntimeError):
        episode_renderer._render_and_mark_story(db_session, story_fail.id, Path("/tmp/work"))

    ok_content = db_session.query(StoryContent).filter_by(story_id=story_ok.id).first()
    fail_content = db_session.query(StoryContent).filter_by(story_id=story_fail.id).first()
    assert ok_content.status == "video_ready"
    assert fail_content.status == "voice_ready"


# ---------------------------------------------------------------------
# The actual regression: episode_qa.py's per-story filter.
# ---------------------------------------------------------------------

def test_episode_qa_includes_a_story_only_reachable_through_the_new_pipeline(db_session, monkeypatch):
    """
    Direct regression test: before this fix, a story whose content was
    produced only via ensure_script_and_voice never advanced past
    "voice_ready", so app/tasks/episode_qa.py's
    StoryContent.status == "video_ready" filter silently excluded it
    from per-story QA (story_count, ai_only, source_verification, etc.)
    even though its enhanced segment was really rendered into the
    episode. This story starts at "voice_ready" (the new pipeline's own
    terminal status) and is only promoted to "video_ready" via
    _mark_story_video_ready -- exactly what render_episode()'s story
    loop now does after a successful render.
    """
    from app.tasks import episode_qa as episode_qa_task

    story = _make_story_with_content(db_session, status="voice_ready")
    db_session.add(StoryState(id=story.id, ai_relevance="ai_candidate", verification_status="verified"))
    episode = Episode(episode_date=date(2026, 9, 26), status="draft")
    db_session.add(episode)
    db_session.flush()
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=story.id, rank_position=1,
        selection_status="primary", rank_score=1.0,
    ))
    db_session.commit()

    # The fix under test: this is what render_episode()'s story loop
    # calls after a successful render_story_enhanced().
    episode_renderer._mark_story_video_ready(db_session, story.id)

    db_session.close = lambda: None
    monkeypatch.setattr(episode_qa_task, "SessionLocal", lambda: db_session)

    result = episode_qa_task.run_episode_qa(episode.id)

    story_count_check = next(c for c in result["checks"] if c["check"] == "story_count")
    assert story_count_check["detail"].startswith("1/"), story_count_check


def test_episode_qa_excludes_a_story_still_stuck_at_voice_ready(db_session, monkeypatch):
    """Sanity check for the test above: WITHOUT the fix applied (no
    _mark_story_video_ready call), the story is excluded -- confirms
    the previous test is actually exercising the fix, not something
    that would have passed anyway."""
    from app.tasks import episode_qa as episode_qa_task

    story = _make_story_with_content(db_session, status="voice_ready")
    db_session.add(StoryState(id=story.id, ai_relevance="ai_candidate", verification_status="verified"))
    episode = Episode(episode_date=date(2026, 9, 26), status="draft")
    db_session.add(episode)
    db_session.flush()
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=story.id, rank_position=1,
        selection_status="primary", rank_score=1.0,
    ))
    db_session.commit()

    db_session.close = lambda: None
    monkeypatch.setattr(episode_qa_task, "SessionLocal", lambda: db_session)

    result = episode_qa_task.run_episode_qa(episode.id)

    story_count_check = next(c for c in result["checks"] if c["check"] == "story_count")
    assert story_count_check["detail"].startswith("0/"), story_count_check
