from pathlib import Path

from app.content.video_composer import probe_video
from app.tasks.ranking import PRIMARY_SLOTS


# The project's own name ("5min-ai-news") and the architecture's own
# framing (see proposal.md's "<5 minute requirement" critique) set the
# duration target -- 5 minutes, no tolerance added. Real current
# episodes run over this (see TODO.md); that's a genuine finding this
# check is meant to surface, not something to threshold around.
MAX_DURATION_SECONDS = 5 * 60


def run_qa_checks(episode, story_rows: list[tuple], video_path: Path | None) -> list[dict]:
    """
    Run every QA check from project.md's "AUTOMATED VIDEO QA" list
    against an already-produced episode. Each check is independent
    and re-verifies real state (DB fields AND actual files on disk),
    rather than trusting that an earlier stage's own success report
    was accurate.

    `story_rows` is a list of (EpisodeStory, NewsItem, StoryState,
    StoryContent) tuples for every primary story that reached
    video_ready and was actually included in the final video (see
    app/tasks/episode_qa.py for how this is assembled).

    Returns a list of {"check", "passed", "detail"} dicts.
    """

    checks: list[dict] = []
    total = len(story_rows)

    # 1. Exactly PRIMARY_SLOTS stories in the produced video.
    checks.append({
        "check": "story_count",
        "passed": total == PRIMARY_SLOTS,
        "detail": f"{total}/{PRIMARY_SLOTS} stories included in the produced video",
    })

    # 2. AI-only -- every included story is a classified ai_candidate.
    non_ai = [item.id for _, item, state, _ in story_rows if state.ai_relevance != "ai_candidate"]
    checks.append({
        "check": "ai_only",
        "passed": len(non_ai) == 0,
        "detail": (
            f"all {total} stories classified ai_candidate" if not non_ai
            else f"{len(non_ai)} non-AI stories included: {non_ai}"
        ),
    })

    # 3. Verified sources -- the Verification Engine (app/verification/
    # engine.py) marks each story "verified" (cross-source confirmation
    # or a primary/official source) or "unverified" ahead of ranking.
    # Same as duration_target below: this is an honest report, not a
    # gate -- QA never blocks approval (see main.py), and an episode
    # with unverified stories can still legitimately be approved.
    unverified = [item.id for _, item, state, _ in story_rows if state.verification_status != "verified"]
    checks.append({
        "check": "source_verification",
        "passed": len(unverified) == 0,
        "detail": (
            f"all {total} stories verified" if not unverified
            else f"{len(unverified)} unverified stories included: {unverified}"
        ),
    })

    # 4. Source links -- every included story has a real url.
    missing_urls = [item.id for _, item, _, _ in story_rows if not item.canonical_url]
    checks.append({
        "check": "source_links",
        "passed": len(missing_urls) == 0,
        "detail": (
            f"all {total} stories have a source url" if not missing_urls
            else f"{len(missing_urls)} stories missing a url: {missing_urls}"
        ),
    })

    # 5. Captions -- path recorded AND file actually exists on disk.
    missing_captions = [
        item.id for _, item, _, content in story_rows
        if not content.captions_path or not Path(content.captions_path).exists()
    ]
    checks.append({
        "check": "captions_present",
        "passed": len(missing_captions) == 0,
        "detail": (
            f"all {total} stories have caption files on disk" if not missing_captions
            else f"{len(missing_captions)} stories missing captions: {missing_captions}"
        ),
    })

    # 6. Audio -- path recorded AND file actually exists on disk.
    missing_audio = [
        item.id for _, item, _, content in story_rows
        if not content.audio_path or not Path(content.audio_path).exists()
    ]
    checks.append({
        "check": "audio_present",
        "passed": len(missing_audio) == 0,
        "detail": (
            f"all {total} stories have audio files on disk" if not missing_audio
            else f"{len(missing_audio)} stories missing audio: {missing_audio}"
        ),
    })

    # 7. Video integrity -- the combined episode file exists and has
    # both a video and an audio stream.
    if video_path is None or not video_path.exists():
        checks.append({
            "check": "video_integrity",
            "passed": False,
            "detail": "episode video file not found",
        })
        duration_seconds = None
    else:
        try:
            probe = probe_video(video_path)
            duration_seconds = probe["duration_seconds"]
            ok = probe["has_video"] and probe["has_audio"]
            checks.append({
                "check": "video_integrity",
                "passed": ok,
                "detail": (
                    f"video stream={probe['has_video']}, audio stream={probe['has_audio']}"
                ),
            })
        except Exception as exc:
            checks.append({
                "check": "video_integrity",
                "passed": False,
                "detail": f"ffprobe failed: {exc}",
            })
            duration_seconds = None

    # 8. Duration target.
    if duration_seconds is None:
        checks.append({
            "check": "duration_target",
            "passed": False,
            "detail": "could not determine duration (video_integrity check failed)",
        })
    else:
        within_target = duration_seconds <= MAX_DURATION_SECONDS
        checks.append({
            "check": "duration_target",
            "passed": within_target,
            "detail": (
                f"{duration_seconds:.1f}s "
                f"({'within' if within_target else 'exceeds'} the "
                f"{MAX_DURATION_SECONDS}s / 5 min target)"
            ),
        })

    return checks
