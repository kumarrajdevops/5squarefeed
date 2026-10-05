"""
The same produced video can be published to dev and, separately, to prod
(e.g. dev now, prod once its credentials exist). State is per environment
(EpisodePublication rows); Episode.publish_* is their roll-up.
"""
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.models import Episode, EpisodePublication
from app.tasks import publishing
from app.tasks.publishing import publish_episode_to_youtube


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = engine.execution_options(schema_translate_map={"raw": None, "editorial": None})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def episode_id(factory):
    with factory() as db:
        ep = Episode(
            episode_date=date(2026, 9, 29), status="approved", video_status="ready",
            video_path="media/x.mp4",
        )
        db.add(ep)
        db.commit()
        return ep.id


def _queue(monkeypatch, factory):
    monkeypatch.setattr(main, "SessionLocal", factory)
    delay = MagicMock(return_value=MagicMock(id="t"))
    monkeypatch.setattr(publish_episode_to_youtube, "delay", delay)
    return delay


def test_publish_to_dev_then_prod_independently(monkeypatch, factory, episode_id):
    delay = _queue(monkeypatch, factory)

    main.trigger_episode_publish(episode_id, "dev")
    delay.assert_called_with(episode_id, "dev", 1)

    with factory() as db:
        pub = db.query(EpisodePublication).one()
        pub.status = "published"
        pub.youtube_url = "https://youtu.be/dev"
        db.commit()

    # dev done; prod is still publishable from the same video.
    main.trigger_episode_publish(episode_id, "prod")
    delay.assert_called_with(episode_id, "prod", 2)

    with factory() as db:
        rows = {r.environment: r.status for r in db.query(EpisodePublication).all()}
        assert rows == {"dev": "published", "prod": "publishing"}
        assert db.get(Episode, episode_id).publish_status == "publishing"


def test_publishing_again_needs_repost_and_records_next_sequence(monkeypatch, factory, episode_id):
    _queue(monkeypatch, factory)
    main.trigger_episode_publish(episode_id, "dev")
    with factory() as db:
        db.query(EpisodePublication).one().status = "published"
        db.commit()

    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_publish(episode_id, "dev")
    assert exc.value.status_code == 409

    main.trigger_episode_publish(episode_id, "dev", repost=True)
    with factory() as db:
        rows = db.query(EpisodePublication).order_by(EpisodePublication.sequence).all()
        assert [(r.sequence, r.status) for r in rows] == [(1, "published"), (2, "publishing")]

    # a second repost while #2 is still uploading is refused
    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_publish(episode_id, "dev", repost=True)
    assert exc.value.status_code == 409


def test_task_records_each_upload_as_its_own_publication(monkeypatch, factory, episode_id):
    monkeypatch.setattr(publishing, "SessionLocal", factory)
    uploads = iter(["v1", "v2"])
    monkeypatch.setattr(
        publishing, "upload_video", lambda **kw: {"video_id": (v := next(uploads)), "url": f"https://youtu.be/{v}"}
    )
    delay = _queue(monkeypatch, factory)

    main.trigger_episode_publish(episode_id, "prod")
    publish_episode_to_youtube(episode_id, "prod", delay.call_args.args[2])
    main.trigger_episode_publish(episode_id, "prod", repost=True)
    publish_episode_to_youtube(episode_id, "prod", delay.call_args.args[2])

    with factory() as db:
        rows = db.query(EpisodePublication).order_by(EpisodePublication.sequence).all()
        assert [(r.sequence, r.youtube_url) for r in rows] == [(1, "https://youtu.be/v1"), (2, "https://youtu.be/v2")]
        assert db.get(Episode, episode_id).youtube_url == "https://youtu.be/v2"
        pubs = main._serialize_publications(db, db.get(Episode, episode_id))
        assert pubs["prod"]["count"] == 2 and len(pubs["prod"]["history"]) == 2


def test_failed_publish_can_be_retried(monkeypatch, factory, episode_id):
    _queue(monkeypatch, factory)
    main.trigger_episode_publish(episode_id, "dev")
    with factory() as db:
        db.query(EpisodePublication).one().status = "failed"
        db.commit()

    main.trigger_episode_publish(episode_id, "dev")
    with factory() as db:
        assert db.query(EpisodePublication).one().status == "publishing"


def test_unknown_environment_rejected(monkeypatch, factory, episode_id):
    _queue(monkeypatch, factory)
    with pytest.raises(HTTPException) as exc:
        main.trigger_episode_publish(episode_id, "staging")
    assert exc.value.status_code == 400


def test_task_uploads_with_environment_and_records_per_env_url(monkeypatch, factory, episode_id):
    monkeypatch.setattr(publishing, "SessionLocal", factory)
    calls = []

    def fake_upload(**kwargs):
        calls.append(kwargs["environment"])
        return {"video_id": "abc", "url": f"https://youtu.be/{kwargs['environment']}"}

    monkeypatch.setattr(publishing, "upload_video", fake_upload)

    publish_episode_to_youtube(episode_id, "dev")
    publish_episode_to_youtube(episode_id, "prod")

    assert calls == ["dev", "prod"]
    with factory() as db:
        urls = {r.environment: r.youtube_url for r in db.query(EpisodePublication).all()}
        assert urls == {"dev": "https://youtu.be/dev", "prod": "https://youtu.be/prod"}
        assert db.get(Episode, episode_id).publish_status == "published"


def test_prod_failure_keeps_dev_published(monkeypatch, factory, episode_id):
    monkeypatch.setattr(publishing, "SessionLocal", factory)
    monkeypatch.setattr(publishing, "upload_video", lambda **kw: {"video_id": "v", "url": "https://youtu.be/v"})
    publish_episode_to_youtube(episode_id, "dev")

    def boom(**kwargs):
        raise RuntimeError("no prod credentials")

    monkeypatch.setattr(publishing, "upload_video", boom)
    result = publish_episode_to_youtube(episode_id, "prod")

    assert result["status"] == "failed"
    with factory() as db:
        rows = {r.environment: r.status for r in db.query(EpisodePublication).all()}
        assert rows == {"dev": "published", "prod": "failed"}
        assert db.get(Episode, episode_id).publish_status == "published"


def test_youtube_title_uses_made_date_not_coverage_date(monkeypatch, factory):
    from datetime import datetime, timezone

    monkeypatch.setattr(publishing, "SessionLocal", factory)
    with factory() as db:
        # Coverage day Sep 30; made 05:00 IST on Oct 1 (= 23:30 UTC Sep 30), like Episode 7.
        ep = Episode(
            episode_date=date(2026, 9, 30), status="approved", video_status="ready",
            video_path="media/x.mp4", created_at=datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc),
        )
        db.add(ep)
        db.commit()
        episode_id = ep.id

    seen = {}

    def fake_upload(**kwargs):
        seen.update(kwargs)
        return {"video_id": "v", "url": "https://youtu.be/v"}

    monkeypatch.setattr(publishing, "upload_video", fake_upload)
    publish_episode_to_youtube(episode_id, "dev")
    assert "October 01, 2026" in seen["title"]
    assert "October 01, 2026" in seen["description"]
