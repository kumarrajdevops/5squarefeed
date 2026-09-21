from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import main
from app.tasks.collection import run_collection
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
def test_collection_trigger_allowed_in_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, run_collection)

    result = main.trigger_collection()

    mock.assert_called_once_with(target_date_iso=None, trigger_type="manual")
    assert result == {"task_id": "fake-task-id", "status": "queued"}


@pytest.mark.parametrize(
    "app_env", ["prod", "production", "prd", "PROD", "", "staging", "unknown"]
)
def test_collection_trigger_rejected_outside_dev_envs(monkeypatch, app_env):
    monkeypatch.setattr(main.settings, "app_env", app_env)
    mock = _mock_delay(monkeypatch, run_collection)

    with pytest.raises(HTTPException) as exc_info:
        main.trigger_collection()

    assert exc_info.value.status_code == 403
    assert "local/dev" in exc_info.value.detail
    mock.assert_not_called()


def test_collection_trigger_rejects_invalid_target_date(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "local")
    mock = _mock_delay(monkeypatch, run_collection)

    with pytest.raises(HTTPException) as exc_info:
        main.trigger_collection(target_date="not-a-date")

    assert exc_info.value.status_code == 422
    mock.assert_not_called()


def test_health_exposes_app_env(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "local")
    assert main.health()["app_env"] == "local"


def test_beat_schedule_unchanged():
    # Regression guard: collection (RSS + Hacker News, one combined
    # run_collection task per pass) still runs at 10 PM / 1 AM /
    # 3:30 AM IST, and processing still runs once at 4 AM IST -- see
    # app/worker/celery_app.py's beat_schedule and its own comment on
    # why IST midnight falling between the 10 PM and 1 AM passes means
    # they don't all target the same calendar day.
    schedule = celery_app.conf.beat_schedule

    assert set(schedule.keys()) == {
        "collect-10pm-ist",
        "collect-1am-ist",
        "collect-330am-ist-final",
        "process-4am-ist",
    }
    assert schedule["collect-10pm-ist"]["task"] == "app.tasks.collection.run_collection"
    assert schedule["collect-1am-ist"]["task"] == "app.tasks.collection.run_collection"
    assert schedule["collect-330am-ist-final"]["task"] == "app.tasks.collection.run_collection"
    assert schedule["process-4am-ist"]["task"] == "app.tasks.scheduled.run_daily_processing"


# ---------------------------------------------------------
# GET /api/v1/tasks/{task_id}/result -- wraps Celery's AsyncResult
# (backed by the already-configured Redis result backend). Monkeypatch
# main.AsyncResult itself rather than hitting real Redis/Celery.
# Generic by design (app/main.py) -- not tied to any one task's result
# shape, so these tests use a plain example dict rather than an
# ingestion-specific one.
# ---------------------------------------------------------

def _fake_async_result(*, ready, failed=False, result=None):
    fake = MagicMock()
    fake.ready.return_value = ready
    fake.failed.return_value = failed
    fake.result = result
    return fake


def test_task_result_pending(monkeypatch):
    monkeypatch.setattr(
        main, "AsyncResult", MagicMock(return_value=_fake_async_result(ready=False))
    )

    result = main.get_task_result("some-task-id")

    assert result == {"task_id": "some-task-id", "status": "pending"}


def test_task_result_success_echoes_task_result(monkeypatch):
    task_result = {
        "collection_run_id": 1,
        "collection_date": "2026-09-20",
        "status": "success",
        "rss": {"items_seen": 3, "items_inserted": 2, "items_updated": 1},
        "hackernews": {"seen": 5, "inserted": 4, "updated": 1},
    }
    monkeypatch.setattr(
        main,
        "AsyncResult",
        MagicMock(return_value=_fake_async_result(ready=True, failed=False, result=task_result)),
    )

    result = main.get_task_result("some-task-id")

    assert result == {"task_id": "some-task-id", "status": "success", "result": task_result}


def test_task_result_failed(monkeypatch):
    monkeypatch.setattr(
        main,
        "AsyncResult",
        MagicMock(return_value=_fake_async_result(ready=True, failed=True, result=ValueError("boom"))),
    )

    result = main.get_task_result("some-task-id")

    assert result["status"] == "failed"
    assert "boom" in result["error"]
