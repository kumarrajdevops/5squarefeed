import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import main
from app.content import storyboard_service
from app.models import Episode, EpisodeStory, NewsItem
from app.tasks import episode_storyboard as sb


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MEDIA_ROOT", tmp_path)
    return tmp_path


def _episode_with_stories(db, n=3):
    episode = Episode(episode_date=date(2026, 9, 22), status="draft")
    db.add(episode)
    db.flush()
    ids = []
    for i in range(n):
        item = NewsItem(
            title=f"Story {i}", canonical_url=f"https://e.test/{i}", source_name="S",
            source_type="rss", published_at=datetime(2026, 9, 22, 8, 0),
            collected_at=datetime(2026, 9, 22, 8, 0), collection_date=date(2026, 9, 22),
            status="collected",
        )
        db.add(item)
        db.flush()
        db.add(EpisodeStory(
            episode_id=episode.id, story_id=item.id, rank_position=i + 1,
            selection_status="primary", rank_score=1.0, rank_reason="t",
        ))
        ids.append(item.id)
    db.commit()
    return episode.id, ids


def _fake_ensure(results):
    def fake(db, story_id):
        return results[story_id]
    return fake


def test_report_lists_only_stories_that_could_not_be_built(db_session, monkeypatch):
    episode_id, (a, b, c) = _episode_with_stories(db_session)
    monkeypatch.setattr(storyboard_service, "ensure_storyboard", _fake_ensure({
        a: {"story_id": a, "status": "ready", "reused": True},
        b: {"story_id": b, "status": "qa_failed", "reused": False,
            "qa": [{"check": "caption_fit", "passed": False, "detail": "3 lines"}]},
        c: {"story_id": c, "status": "failed", "stage": "content", "error": "TTS down"},
    }))

    report = sb.run_storyboards_for_episode(db_session, episode_id, sb.start_report(episode_id))

    assert (report["total"], report["processed"], report["built"], report["reused"]) == (3, 3, 1, 1)
    assert [p["story_id"] for p in report["problems"]] == [c]
    issue = report["problems"][0]["issues"][0]
    assert (issue["severity"], issue["stage"], issue["message"]) == ("error", "content", "TTS down")
    assert report["problems"][0]["backup"] is False
    assert json.loads(sb.report_path(episode_id).read_text())["processed"] == 3


def test_backup_stories_are_included_and_marked(db_session, monkeypatch):
    episode_id, (a, b, c) = _episode_with_stories(db_session)
    row = db_session.query(EpisodeStory).filter_by(episode_id=episode_id, story_id=c).one()
    row.selection_status, row.rank_position = "backup", 26
    db_session.commit()
    monkeypatch.setattr(storyboard_service, "ensure_storyboard", _fake_ensure({
        a: {"story_id": a, "status": "ready"},
        b: {"story_id": b, "status": "ready"},
        c: {"story_id": c, "status": "failed", "stage": "voice", "error": "timeout"},
    }))

    report = sb.run_storyboards_for_episode(db_session, episode_id, sb.start_report(episode_id))

    assert report["total"] == 3
    assert [(p["story_id"], p["rank"], p["backup"]) for p in report["problems"]] == [(c, 26, True)]


def test_unexpected_exception_in_one_story_is_an_error_not_a_crash(db_session, monkeypatch):
    episode_id, _ = _episode_with_stories(db_session, 2)
    stories = [r.story_id for r in db_session.query(EpisodeStory).filter_by(episode_id=episode_id)]

    def boom(db, story_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(storyboard_service, "ensure_storyboard", boom)
    report = sb.run_storyboards_for_episode(db_session, episode_id, sb.start_report(episode_id))

    assert report["processed"] == 2
    assert {p["story_id"] for p in report["problems"]} == set(stories)
    assert report["problems"][0]["issues"][0]["message"] == "kaboom"


def test_read_report_marks_old_running_report_failed():
    report = sb.start_report(7)
    report["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sb.write_report(7, report)

    assert sb.read_report(7)["status"] == "failed"
    assert sb.read_report(999) is None


def test_endpoint_writes_running_report_then_queues(monkeypatch, db_session, media_root):
    episode_id, _ = _episode_with_stories(db_session)
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    delay = MagicMock(return_value=MagicMock(id="t1"))
    monkeypatch.setattr(main.run_episode_storyboards, "delay", delay)

    result = main.trigger_episode_storyboard(episode_id)

    assert result == {"episode_id": episode_id, "task_id": "t1", "status": "queued"}
    assert main.get_episode_storyboard(episode_id)["status"] == "running"
    delay.assert_called_once_with(episode_id)

    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_storyboard(episode_id)
    assert exc.value.status_code == 409


def test_endpoint_rejects_unknown_episode_and_producing_episode(monkeypatch, db_session):
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(main.run_episode_storyboards, "delay", MagicMock())

    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_storyboard(12345)
    assert exc.value.status_code == 404

    episode_id, _ = _episode_with_stories(db_session)
    db_session.get(Episode, episode_id).video_status = "producing"
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_storyboard(episode_id)
    assert exc.value.status_code == 409


def test_get_report_when_never_run():
    assert main.get_episode_storyboard(4242) == {"episode_id": 4242, "status": "none"}
