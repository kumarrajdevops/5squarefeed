import json
from datetime import datetime, timezone
from pathlib import Path

from app.content.script_generator import generate_script
from app.content.video_composer import (
    build_captions,
    compose_video,
    concat_videos,
    generate_gap_clip,
    get_audio_duration_seconds,
)
from app.content.visual_generator import generate_branding_card, generate_card
from app.content.voice_generator import synthesize_voice
from app.db import SessionLocal
from app.models import Episode, EpisodeStory, Story, StoryContent
from app.notifications.notifier import EPISODE_VIDEO_FAILED, notify
from app.tasks.content import MEDIA_ROOT, get_or_create_content, mark_content_failed
from app.worker.celery_app import celery_app


def _produce_branding_clip(main_text: str, sub_text: str, narration_text: str, clip_id: str) -> Path | None:
    """
    Render a short intro/outro clip: branding card + narration +
    burned-in captions, via the same pure functions used for a
    story's visual/voice/video stages. Not tied to a Story row --
    clip_id is a plain string (e.g. "episode_5_intro") used to name
    the generated files. Returns None (rather than raising) on
    failure, so a branding-clip problem doesn't block the episode's
    actual story content.
    """

    try:
        image_path = MEDIA_ROOT / "images" / f"{clip_id}.png"
        generate_branding_card(main_text, sub_text, image_path)

        audio_path = MEDIA_ROOT / "audio" / f"{clip_id}.mp3"
        segments = synthesize_voice(narration_text, audio_path)
        duration = get_audio_duration_seconds(audio_path)

        captions_path = MEDIA_ROOT / "captions" / f"{clip_id}.srt"
        build_captions(segments, captions_path)

        video_path = MEDIA_ROOT / "videos" / f"{clip_id}.mp4"
        compose_video(
            image_path=image_path,
            audio_path=audio_path,
            captions_path=captions_path,
            output_path=video_path,
            duration_seconds=duration,
        )

        return video_path

    except Exception as exc:
        print(f"[episode_video] Branding clip {clip_id!r} failed: {exc}")
        return None


def _produce_story_content(db, story: Story, content: StoryContent) -> bool:
    """
    Run a single story through all four content stages, synchronously
    and in-process (not via the Celery task chain in app/tasks/content.py
    -- those auto-chain the next stage with .delay(), which would run
    async and break the sequential-per-story loop this task needs).
    Reuses the same pure generation functions and the same
    get_or_create_content/mark_content_failed helpers, so behavior
    matches the single-story pipeline exactly; only the orchestration
    (synchronous vs. Celery-chained) differs.

    Returns True if the story reached video_ready, False if any stage
    failed (that story is then skipped from the final concatenation,
    not allowed to block the rest of the episode).
    """

    # Skip regenerating the script if one already exists -- either from
    # a prior run, or (critically) from a human edit via the dashboard's
    # edit panel (PATCH /stories/{id}/content, which deliberately clears
    # audio/image/captions/video to force those to regenerate FROM the
    # edited script, but leaves script_text as the human wrote it).
    # Regenerating unconditionally here would silently overwrite that
    # edit with the auto-generated template text on every Produce call.
    if not content.script_text:
        try:
            script = generate_script(title=story.title, raw_summary=story.raw_summary)
            content.headline = script["headline"]
            content.summary = script["summary"]
            content.script_text = script["script_text"]
            content.status = "script_ready"
            content.error_message = None
            db.commit()
        except Exception as exc:
            mark_content_failed(db, content, "script", exc)
            return False

    try:
        audio_path = MEDIA_ROOT / "audio" / f"{story.id}.mp3"
        segments = synthesize_voice(content.script_text, audio_path)
        content.audio_path = str(audio_path)
        content.audio_duration_seconds = get_audio_duration_seconds(audio_path)
        content.caption_segments = json.dumps(segments)
        content.status = "voice_ready"
        content.error_message = None
        db.commit()
    except Exception as exc:
        mark_content_failed(db, content, "voice", exc)
        return False

    try:
        image_path = MEDIA_ROOT / "images" / f"{story.id}.png"
        generate_card(content.headline, story.source_name, image_path)
        content.image_path = str(image_path)
        content.status = "visual_ready"
        content.error_message = None
        db.commit()
    except Exception as exc:
        mark_content_failed(db, content, "visual", exc)
        return False

    try:
        captions_path = MEDIA_ROOT / "captions" / f"{story.id}.srt"
        segments = json.loads(content.caption_segments) if content.caption_segments else []
        build_captions(segments, captions_path)

        video_path = MEDIA_ROOT / "videos" / f"{story.id}.mp4"
        compose_video(
            image_path=Path(content.image_path),
            audio_path=Path(content.audio_path),
            captions_path=captions_path,
            output_path=video_path,
            duration_seconds=content.audio_duration_seconds,
        )

        content.captions_path = str(captions_path)
        content.video_path = str(video_path)
        content.status = "video_ready"
        content.error_message = None
        db.commit()
    except Exception as exc:
        mark_content_failed(db, content, "video", exc)
        return False

    return True


# A 2s silent black clip inserted between consecutive story segments
# in the combined episode video, purely for pacing -- content-
# independent, so generated once and reused across every episode
# rather than regenerated per /produce call.
GAP_DURATION_SECONDS = 0.5
GAP_CLIP_PATH = MEDIA_ROOT / "videos" / "_story_gap_0_5s.mp4"


def _get_gap_clip() -> Path:
    if not GAP_CLIP_PATH.exists():
        generate_gap_clip(GAP_DURATION_SECONDS, GAP_CLIP_PATH)
    return GAP_CLIP_PATH


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

    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            return {"episode_id": episode_id, "status": "failed", "error": "Episode not found"}

        episode.video_status = "producing"
        db.commit()

        rows = (
            db.query(EpisodeStory, Story)
            .join(Story, EpisodeStory.story_id == Story.id)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.selection_status == "primary",
            )
            .order_by(EpisodeStory.rank_position.asc())
            .all()
        )

        if not rows:
            episode.video_status = "failed"
            notify(db, EPISODE_VIDEO_FAILED, episode_id, "No primary stories")
            db.commit()
            return {"episode_id": episode_id, "status": "failed", "error": "No primary stories"}

        succeeded = 0
        skipped_existing = 0
        failed = 0
        video_paths: list[Path] = []

        formatted_date = episode.run_date.strftime("%B %d, %Y")

        intro_path = _produce_branding_clip(
            main_text="5squareFeed",
            sub_text=f"{formatted_date} — 25 Stories A Day",
            narration_text=f"5squareFeed for {formatted_date}. Today's top 25 AI stories.",
            clip_id=f"episode_{episode_id}_intro",
        )
        if intro_path:
            video_paths.append(intro_path)

        added_first_story_clip = False

        for episode_story, story in rows:
            content = get_or_create_content(db, story.id)

            if content.status == "video_ready" and content.video_path:
                skipped_existing += 1
                clip_path = Path(content.video_path)
            elif _produce_story_content(db, story, content):
                succeeded += 1
                clip_path = Path(content.video_path)
            else:
                failed += 1
                print(f"[episode_video] Story {story.id} failed, excluded from episode {episode_id}")
                continue

            # A gap goes *between* stories only -- never before the
            # first one (right after the intro) and never doubled up
            # around a story that got excluded above.
            if added_first_story_clip:
                video_paths.append(_get_gap_clip())
            video_paths.append(clip_path)
            added_first_story_clip = True

        outro_path = _produce_branding_clip(
            main_text="That's all for today's 5squareFeed.",
            sub_text="See you tomorrow.",
            narration_text="That's all for today's 5squareFeed. See you tomorrow.",
            clip_id=f"episode_{episode_id}_outro",
        )
        if outro_path:
            video_paths.append(outro_path)

        if succeeded + skipped_existing == 0:
            # Intro/outro clips may still be in video_paths even if
            # every story failed -- that's not a usable episode video.
            episode.video_status = "failed"
            notify(
                db, EPISODE_VIDEO_FAILED, episode_id,
                f"No stories produced successfully ({failed} failed)",
            )
            db.commit()
            return {
                "episode_id": episode_id,
                "status": "failed",
                "error": "No stories produced successfully",
                "failed": failed,
            }

        output_path = MEDIA_ROOT / "videos" / f"episode_{episode_id}.mp4"

        try:
            concat_videos(video_paths, output_path)
            episode.video_path = str(output_path)
            episode.video_status = "ready"
            episode.video_produced_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            episode.video_status = "failed"
            notify(db, EPISODE_VIDEO_FAILED, episode_id, f"concat failed: {exc}")
            db.commit()
            print(f"[episode_video] Concat failed for episode {episode_id}: {exc}")
            return {"episode_id": episode_id, "status": "failed", "error": f"concat failed: {exc}"}

        # Second phase: best-effort produce the 5 backup stories too,
        # now that the primary video is already ready. Never appends
        # to video_paths (already consumed by concat_videos above) and
        # never fails the episode -- a backup with no/failed content
        # just means a future swap won't be instant for that one story.
        backup_rows = (
            db.query(EpisodeStory, Story)
            .join(Story, EpisodeStory.story_id == Story.id)
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
            content = get_or_create_content(db, story.id)

            if content.status == "video_ready" and content.video_path:
                backups_skipped_existing += 1
                continue

            if _produce_story_content(db, story, content):
                backups_succeeded += 1
            else:
                backups_failed += 1
                print(f"[episode_video] Backup story {story.id} failed for episode {episode_id} (non-fatal)")

    result = {
        "episode_id": episode_id,
        "status": "ready",
        "stories_total": len(rows),
        "stories_produced": succeeded,
        "stories_reused": skipped_existing,
        "stories_failed": failed,
        "backups_total": len(backup_rows),
        "backups_produced": backups_succeeded,
        "backups_reused": backups_skipped_existing,
        "backups_failed": backups_failed,
        "video_path": str(output_path),
    }

    print(f"[episode_video] Completed: {result}")

    return result
