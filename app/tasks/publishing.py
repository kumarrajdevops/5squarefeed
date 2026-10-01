from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodePublication, EpisodeStory, NewsItem
from app.notifications.notifier import EPISODE_PUBLISH_FAILED, notify
from app.publishing.summary import refresh_publish_summary
from app.publishing.youtube_publisher import build_video_metadata, upload_video
from app.worker.celery_app import celery_app


@celery_app.task
def publish_episode_to_youtube(episode_id: int, environment: str | None = None) -> dict:
    """
    Upload an already-approved, already-produced episode's combined
    video to YouTube (see app/publishing/youtube_publisher.py).

    Does not produce or approve anything itself -- POST
    /episodes/{id}/publish (app/main.py) already validated
    status == "approved" and video_status == "ready" and flipped
    publish_status to "publishing" synchronously before queuing this
    task, same status-flip-in-the-endpoint pattern as Produce/QA.

    `environment` ("dev"/"prod") picks the credential set/channel; the
    same produced video can be published to each environment once (one
    EpisodePublication row per environment, rolled up into
    Episode.publish_* by refresh_publish_summary). Defaults to
    YOUTUBE_ENVIRONMENT.

    Always logs and returns which environment (dev/prod --
    separate credentials AND separate destination channels, see
    app/config.py) it actually published under -- a real, easy mistake
    to make otherwise is publishing to the wrong channel without
    noticing, since both look identical from inside this task.
    """

    environment = environment or settings.youtube_environment
    print(f"[publishing] Episode {episode_id}: publishing to environment={environment!r}")

    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            return {"episode_id": episode_id, "status": "failed", "error": "Episode not found"}

        publication = (
            db.query(EpisodePublication)
            .filter(EpisodePublication.episode_id == episode_id, EpisodePublication.environment == environment)
            .one_or_none()
        )
        if publication is None:
            publication = EpisodePublication(
                episode_id=episode_id, environment=environment, status="publishing",
                started_at=datetime.now(timezone.utc),
            )
            db.add(publication)
            db.flush()

        try:
            rows = (
                db.query(EpisodeStory, NewsItem)
                .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
                .filter(
                    EpisodeStory.episode_id == episode_id,
                    EpisodeStory.selection_status == "primary",
                )
                .order_by(EpisodeStory.rank_position.asc())
                .all()
            )

            stories = [
                {"headline": item.title, "source_name": item.source_name, "url": item.canonical_url}
                for _episode_story, item in rows
            ]

            metadata = build_video_metadata(episode.episode_date, stories)

            result = upload_video(
                video_path=Path(episode.video_path),
                title=metadata["title"],
                description=metadata["description"],
                tags=metadata["tags"],
                environment=environment,
            )

            publication.status = "published"
            publication.youtube_video_id = result["video_id"]
            publication.youtube_url = result["url"]
            publication.published_at = datetime.now(timezone.utc)
            publication.error = None
            db.flush()
            refresh_publish_summary(db, episode)
            db.commit()

        except Exception as exc:
            publication.status = "failed"
            publication.error = str(exc)
            db.flush()
            refresh_publish_summary(db, episode)
            notify(
                db, EPISODE_PUBLISH_FAILED, episode_id,
                f"Publish failed (environment={environment}): {exc}",
            )
            db.commit()
            print(f"[publishing] Episode {episode_id} publish failed: {exc}")
            return {
                "episode_id": episode_id,
                "status": "failed",
                "error": str(exc),
                "environment": environment,
            }

    print(f"[publishing] Episode {episode_id} published ({environment}): {publication.youtube_url}")
    return {
        "episode_id": episode_id,
        "status": "published",
        "youtube_url": publication.youtube_url,
        "environment": environment,
    }
