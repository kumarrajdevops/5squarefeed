from datetime import date

import pytest

from app.models import Episode, Notification
from app.notifications import notifier
from app.notifications.notifier import (
    EPISODE_PUBLISH_FAILED,
    EPISODE_VIDEO_FAILED,
    notify,
)


def _make_episode(db):
    episode = Episode(episode_date=date(2026, 9, 22), status="draft")
    db.add(episode)
    db.flush()
    return episode


@pytest.fixture(autouse=True)
def _no_real_slack_webhook(monkeypatch):
    """
    Every test in this file must never make a real network call --
    without this, a real SLACK_WEBHOOK_URL configured in .env (which
    `settings` loads ambiently) would make running `pytest` actually
    post test messages like "No primary stories" to the real channel.
    Individual tests that want a webhook "configured" set it back to a
    fake URL explicitly.
    """
    monkeypatch.setattr(notifier.settings, "slack_webhook_url", None)


def test_notify_persists_a_notification_row(db_session):
    episode = _make_episode(db_session)

    notify(db_session, EPISODE_VIDEO_FAILED, episode.id, "No primary stories")
    db_session.commit()

    rows = db_session.query(Notification).all()
    assert len(rows) == 1
    assert rows[0].category == EPISODE_VIDEO_FAILED
    assert rows[0].episode_id == episode.id
    assert rows[0].message == "No primary stories"


def test_notify_allows_a_null_episode_id(db_session):
    # Episode not found is checked before an episode row necessarily
    # exists in every caller -- category/message must still work.
    notify(db_session, EPISODE_PUBLISH_FAILED, None, "Episode not found")
    db_session.commit()

    row = db_session.query(Notification).one()
    assert row.episode_id is None
    assert row.category == EPISODE_PUBLISH_FAILED


def test_notify_does_not_commit_itself(db_session):
    # notify() only calls db.add() -- the caller's own subsequent
    # commit (already persisting the failure status change) is what
    # actually saves the row. Verify no auto-commit sneaks in by
    # rolling back and confirming the row disappears.
    episode = _make_episode(db_session)
    db_session.commit()

    notify(db_session, EPISODE_VIDEO_FAILED, episode.id, "concat failed: boom")
    db_session.rollback()

    assert db_session.query(Notification).count() == 0


def test_notify_marks_undelivered_when_no_webhook_configured(db_session):
    episode = _make_episode(db_session)

    notify(db_session, EPISODE_VIDEO_FAILED, episode.id, "No primary stories")
    db_session.commit()

    row = db_session.query(Notification).one()
    assert row.delivered is False


def test_notify_delivers_to_slack_when_configured(db_session, monkeypatch):
    episode = _make_episode(db_session)
    monkeypatch.setattr(notifier.settings, "slack_webhook_url", "https://hooks.slack.com/services/fake")

    posted = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    notify(db_session, EPISODE_PUBLISH_FAILED, episode.id, "Publish failed: boom")
    db_session.commit()

    row = db_session.query(Notification).one()
    assert row.delivered is True
    assert posted["url"] == "https://hooks.slack.com/services/fake"
    assert "episode_publish_failed" in posted["json"]["text"]
    assert f"episode #{episode.id}" in posted["json"]["text"]
    assert "Publish failed: boom" in posted["json"]["text"]


def test_notify_marks_undelivered_when_slack_post_fails(db_session, monkeypatch):
    episode = _make_episode(db_session)
    monkeypatch.setattr(notifier.settings, "slack_webhook_url", "https://hooks.slack.com/services/fake")

    def fake_post(url, json, timeout):
        raise ConnectionError("network down")

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    # Must not raise -- a Slack outage must never break the caller's
    # own failure-handling flow.
    notify(db_session, EPISODE_VIDEO_FAILED, episode.id, "concat failed: boom")
    db_session.commit()

    row = db_session.query(Notification).one()
    assert row.delivered is False
