from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import main
from app.tasks.ingestion import ingest_news
from app.tasks.ingestion_hackernews import ingest_hackernews_stories
from app.worker.celery_app import celery_app


# No httpx/TestClient in this project's dependencies (Starlette's
# TestClient requires it) -- these call the FastAPI path-operation
# functions directly instead, same as the rest of this codebase already
# tests Celery task functions by calling them as plain Python functions
# rather than through the broker.


def _mock_delay(monkeypatch, task, task_id="fake-task-id"):
    mock = MagicMock(return_value=MagicMock(id=task_id))
    monkeypatch.setattr(task, "delay", mock)
    return mock


@pytest.mark.parametrize("app_env", ["local", "dev", "LOCAL", " Dev "])
def test_rss_ingestion_allowed_in_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, ingest_news)

    result = main.trigger_rss_ingestion()

    mock.assert_called_once_with()
    assert result == {"task_id": "fake-task-id", "status": "queued"}


@pytest.mark.parametrize("app_env", ["local", "dev", "LOCAL", " Dev "])
def test_hackernews_ingestion_allowed_in_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, ingest_hackernews_stories)

    result = main.trigger_hackernews_ingestion()

    mock.assert_called_once_with()
    assert result == {"task_id": "fake-task-id", "status": "queued"}


@pytest.mark.parametrize(
    "app_env", ["prod", "production", "prd", "PROD", "", "staging", "unknown"]
)
def test_rss_ingestion_rejected_outside_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, ingest_news)

    with pytest.raises(HTTPException) as exc_info:
        main.trigger_rss_ingestion()

    assert exc_info.value.status_code == 403
    assert "local/dev" in exc_info.value.detail
    mock.assert_not_called()


@pytest.mark.parametrize(
    "app_env", ["prod", "production", "prd", "PROD", "", "staging", "unknown"]
)
def test_hackernews_ingestion_rejected_outside_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, ingest_hackernews_stories)

    with pytest.raises(HTTPException) as exc_info:
        main.trigger_hackernews_ingestion()

    assert exc_info.value.status_code == 403
    assert "local/dev" in exc_info.value.detail
    mock.assert_not_called()


def test_health_exposes_app_env(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "local")
    assert main.health()["app_env"] == "local"


def test_beat_schedule_unchanged():
    # Regression guard: this feature must never touch production
    # scheduling. Exact key set + a couple of task-path spot checks.
    schedule = celery_app.conf.beat_schedule

    assert set(schedule.keys()) == {
        "collect-10pm-ist",
        "collect-10pm-ist-hackernews",
        "collect-1am-ist",
        "collect-1am-ist-hackernews",
        "collect-330am-ist-final",
        "collect-330am-ist-final-hackernews",
        "collection-cutoff-4am-ist",
    }
    assert schedule["collect-10pm-ist"]["task"] == "app.tasks.ingestion.ingest_news"
    assert (
        schedule["collect-1am-ist-hackernews"]["task"]
        == "app.tasks.ingestion_hackernews.ingest_hackernews_stories"
    )
    assert schedule["collection-cutoff-4am-ist"]["task"] == "app.tasks.scheduled.run_nightly_cutoff"
