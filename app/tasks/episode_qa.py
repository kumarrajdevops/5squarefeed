import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem, StoryContent, StoryState
from app.qa.video_qa import run_qa_checks
from app.worker.celery_app import celery_app


@celery_app.task
def run_episode_qa(episode_id: int) -> dict:
    """
    Run Automated Video QA (project.md's checklist) against an
    already-produced episode and persist the result. Does not
    re-produce anything -- run POST /api/v1/episodes/{id}/produce
    first.
    """

    with SessionLocal() as db:
        episode = db.get(Episode, episode_id)

        if episode is None:
            return {"episode_id": episode_id, "status": "failed", "error": "Episode not found"}

        # Only stories that actually reached video_ready count as
        # "included in the produced video" -- a story whose content
        # generation failed was already excluded from the concat step,
        # and QA should judge what's actually in the video, not the
        # original ranking selection.
        rows = (
            db.query(EpisodeStory, NewsItem, StoryState, StoryContent)
            .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
            .join(StoryState, StoryState.id == NewsItem.id)
            .join(StoryContent, StoryContent.story_id == NewsItem.id)
            .filter(
                EpisodeStory.episode_id == episode_id,
                EpisodeStory.selection_status == "primary",
                StoryContent.status == "video_ready",
            )
            .order_by(EpisodeStory.rank_position.asc())
            .all()
        )

        video_path = Path(episode.video_path) if episode.video_path else None

        checks = run_qa_checks(episode, rows, video_path)

        # A check with passed=None ("not implemented") doesn't count
        # against the overall result -- only an explicit False does.
        overall_passed = all(check["passed"] is not False for check in checks)

        episode.qa_status = "passed" if overall_passed else "failed"
        episode.qa_report = json.dumps(checks)
        episode.qa_run_at = datetime.now(timezone.utc)
        db.commit()

    result = {
        "episode_id": episode_id,
        "qa_status": episode.qa_status,
        "checks": checks,
    }

    status_label = {True: "PASS", False: "FAIL", None: "SKIP"}

    print(f"[episode_qa] Completed: episode {episode_id} -> {episode.qa_status}")
    for check in checks:
        print(f"  [{status_label[check['passed']]}] {check['check']}: {check['detail']}")

    return result
