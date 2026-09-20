from datetime import date

import pytest

from app.publishing.youtube_publisher import (
    MAX_DESCRIPTION_CHARS,
    YouTubeNotConfigured,
    build_video_metadata,
    upload_video,
)


def _story(headline="Some AI headline", source_name="TechCrunch AI", url="https://example.com/a"):
    return {"headline": headline, "source_name": source_name, "url": url}


def test_build_video_metadata_title_and_description():
    stories = [_story(headline=f"Story {i}") for i in range(1, 4)]

    metadata = build_video_metadata(date(2026, 9, 19), stories)

    assert "5squareFeed" in metadata["title"]
    assert "September 19, 2026" in metadata["title"]
    assert "3" in metadata["title"]

    for i in range(1, 4):
        assert f"Story {i}" in metadata["description"]
    assert "TechCrunch AI" in metadata["description"]
    assert "https://example.com/a" in metadata["description"]
    assert "5squareFeed" in metadata["tags"]


def test_build_video_metadata_truncates_long_description():
    """
    Regression guard for a real external constraint (YouTube's ~5000
    char description cap) rather than a theoretical one -- a full
    25-story episode with real headline/URL lengths can plausibly
    approach it.
    """
    stories = [
        _story(
            headline="A" * 150,
            source_name="Some Source",
            url="https://example.com/" + "b" * 80,
        )
        for _ in range(60)
    ]

    metadata = build_video_metadata(date(2026, 9, 19), stories)

    assert len(metadata["description"]) <= MAX_DESCRIPTION_CHARS
    assert metadata["description"].endswith("...")


def test_upload_video_fails_fast_when_not_configured(monkeypatch):
    """
    upload_video() must raise a clear, specific error before ever
    touching the network when YouTube OAuth credentials aren't set --
    this project has no real credentials yet (see TODO.md), so this
    is the actual, current, expected behavior, not just a defensive
    edge case.
    """
    from app.publishing import youtube_publisher

    # youtube_client_id/secret/refresh_token are read-only properties
    # derived from youtube_environment (dev/prod) -- monkeypatch the
    # underlying dev_* fields they resolve to instead (default
    # environment is "dev").
    monkeypatch.setattr(youtube_publisher.settings, "youtube_dev_client_id", None)
    monkeypatch.setattr(youtube_publisher.settings, "youtube_dev_client_secret", None)
    monkeypatch.setattr(youtube_publisher.settings, "youtube_dev_refresh_token", None)

    with pytest.raises(YouTubeNotConfigured):
        upload_video(
            video_path="media/videos/episode_1.mp4",
            title="t",
            description="d",
            tags=["a"],
        )
