from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem
from app.notifications.notifier import EPISODE_VIDEO_FAILED, notify
from app.worker.celery_app import celery_app


@celery_app.task
def produce_episode_video(episode_id: int) -> dict:
    """
    Produce (or reuse) content for every primary story in an episode,
    in rank order, then concatenate the resulting per-story videos
    into one combined episode video.

    Idempotent: a story whose content is already video_ready is
    reused as-is, not regenerated. Fault-isolated: a story whose
    pipeline fails is skipped from the final video rather than
    blocking the rest of the episode.

    After the primary video is ready, also produces (or reuses) the
    5 backup stories' content, best-effort. This is deliberately a
    second phase that runs after the primary video is already
    committed as "ready" -- so a slow or failing backup can never
    delay or block the primary episode -- and it exists so that a
    later dashboard swap (a backup replacing a defective primary
    story) is instant instead of triggering a slow on-demand
    regeneration at the last minute.
    """

    from app.content.episode_renderer import render_episode

    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            return {"episode_id": episode_id, "status": "failed", "error": "Episode not found"}

        episode.video_status = "producing"
        db.commit()

    # Canonical enhanced Pillow/storyboard renderer -- the SAME
    # implementation build_episode.py's CLI uses. Not the legacy
    # generate_card/compose_video path: that old renderer is no longer
    # used for the primary episode video (see episode_renderer.py's
    # module docstring). render_episode manages its own DB session(s)
    # internally (story lookup, the storyboard prerequisite pass) and
    # raises on a genuine failure rather than silently producing an
    # incomplete episode.
    try:
        render_result = render_episode(episode_id)
    except Exception as exc:
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            episode.video_status = "failed"
            notify(db, EPISODE_VIDEO_FAILED, episode_id, str(exc))
            db.commit()
        print(f"[episode_video] Canonical render failed for episode {episode_id}: {exc}")
        return {"episode_id": episode_id, "status": "failed", "error": str(exc)}

    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)
        episode.video_path = render_result["video_path"]
        episode.video_status = "ready"
        episode.video_produced_at = datetime.now(timezone.utc)
        db.commit()

        succeeded = render_result["stories_regenerated"]
        skipped_existing = render_result["stories_reused"]
        failed = 0
        output_path = Path(render_result["video_path"])

        # Second phase: best-effort pre-warm the 5 backup stories too,
        # now that the primary video is already ready. Never appends to
        # video_paths (the primary render already consumed its own story
        # list) and never fails the episode -- a backup with no/failed
        # content just means a future swap-to-primary won't be instant
        # for that one story.
        #
        # Uses the SAME canonical storyboard service the primary path
        # uses (ensure_storyboard: content -> storyboard -> render ->
        # QA), not the legacy generate_card/compose_video renderer --
        # a backup that later gets promoted to primary goes through
        # ensure_all_storyboards() on the next /process, which reuses
        # exactly this artifact (storyboard.json + {id}_storyboard.mp4)
        # if it's still valid, so pre-warming it here is what actually
        # makes a later swap instant. Pre-building the backup's own
        # enhanced captioned.mp4 (render_story_enhanced()'s output) would not help
        # -- that artifact isn't cached/reused across runs regardless
        # (see the known idempotency gap in the consolidation report),
        # so there's nothing to gain from rendering it early.
        from app.content.storyboard_service import ensure_storyboard

        backup_rows = (
            db.query(EpisodeStory, NewsItem)
            .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.selection_status == "backup",
            )
            .order_by(EpisodeStory.rank_position.asc())
            .all()
        )

        backups_succeeded = 0
        backups_skipped_existing = 0
        backups_failed = 0

        for episode_story, story in backup_rows:
            backup_result = ensure_storyboard(db, story.id)
            if backup_result.get("status") == "failed":
                backups_failed += 1
                print(f"[episode_video] Backup story {story.id} failed for episode {episode_id} "
                      f"(stage={backup_result.get('stage')}, non-fatal): {backup_result.get('error')}")
            elif backup_result.get("reused"):
                backups_skipped_existing += 1
            else:
                backups_succeeded += 1

    result = {
        "episode_id": episode_id,
        "status": "ready",
        "stories_total": render_result["stories_total"],
        "stories_produced": succeeded,
        "stories_reused": skipped_existing,
        "stories_failed": failed,
        "backups_total": len(backup_rows),
        "backups_produced": backups_succeeded,
        "backups_reused": backups_skipped_existing,
        "backups_failed": backups_failed,
        "video_path": str(output_path),
        "renderer": "storyboard_pillow",
        "timings": render_result["timings"],
    }

    print(f"[episode_video] Completed: {result}")

    return result
