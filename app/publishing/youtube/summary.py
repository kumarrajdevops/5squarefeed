from app.models import Episode, EpisodePublication

ENVIRONMENTS = ("dev", "prod")


def refresh_publish_summary(db, episode: Episode) -> None:
    """Roll the per-environment EpisodePublication rows up into the
    Episode.publish_* fields that the rest of the app (ranking guard,
    pipeline chart, episode list) reads. Caller commits."""
    rows = db.query(EpisodePublication).filter(EpisodePublication.episode_id == episode.id).all()
    statuses = {r.status for r in rows}

    if "publishing" in statuses:
        episode.publish_status = "publishing"
    elif "published" in statuses:
        episode.publish_status = "published"
    elif "failed" in statuses:
        episode.publish_status = "failed"
    else:
        episode.publish_status = "not_published"

    done = [r for r in rows if r.status == "published"]
    if done:
        latest = max(done, key=lambda r: r.published_at.timestamp() if r.published_at else 0)
        episode.published_at = latest.published_at
        episode.youtube_video_id = latest.youtube_video_id
        episode.youtube_url = latest.youtube_url

    failed = [r for r in rows if r.status == "failed"]
    episode.publish_error = failed[-1].error if failed else None
