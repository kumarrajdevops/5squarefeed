from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.sources.hackernews_api import fetch_ai_stories_for_range


def test_fetch_ai_stories_for_range_uses_explicit_start_and_end(monkeypatch):
    """
    Both created_at_i bounds must come from the explicit start/end
    arguments, not any wall-clock read inside this function -- this is
    what makes the fetch a real [start, end) calendar-day query rather
    than a rolling "since now" window.
    """
    captured_params = {}

    def fake_get(url, params, timeout):
        captured_params.update(params)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"hits": [], "nbPages": 1}
        return response

    monkeypatch.setattr("app.sources.hackernews_api.requests.get", fake_get)

    start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
    fetch_ai_stories_for_range(start, end)

    assert f"created_at_i>{int(start.timestamp())}" in captured_params["numericFilters"]
    assert f"created_at_i<{int(end.timestamp())}" in captured_params["numericFilters"]


def test_fetch_ai_stories_for_range_returns_empty_hits(monkeypatch):
    def fake_get(url, params, timeout):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"hits": [], "nbPages": 1}
        return response

    monkeypatch.setattr("app.sources.hackernews_api.requests.get", fake_get)

    result = fetch_ai_stories_for_range(
        datetime(2026, 9, 20, tzinfo=timezone.utc),
        datetime(2026, 9, 21, tzinfo=timezone.utc),
    )

    assert result == []


def test_fetch_ai_stories_for_range_paginates_past_100_hits(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["page"])
        response = MagicMock()
        response.raise_for_status.return_value = None
        if params["page"] == 0:
            response.json.return_value = {"hits": [{"objectID": str(i)} for i in range(100)], "nbPages": 2}
        else:
            response.json.return_value = {"hits": [{"objectID": "100"}], "nbPages": 2}
        return response

    monkeypatch.setattr("app.sources.hackernews_api.requests.get", fake_get)

    result = fetch_ai_stories_for_range(
        datetime(2026, 9, 20, tzinfo=timezone.utc),
        datetime(2026, 9, 21, tzinfo=timezone.utc),
    )

    assert calls == [0, 1]
    assert len(result) == 101
