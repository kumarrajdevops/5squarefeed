"""
Storyboard stage for one episode, runnable on its own before Produce:
for every primary AND backup story (rank order; Produce pre-warms the
backups too) make sure content/audio/captions
and a valid storyboard exist (app.content.storyboard_service.
ensure_storyboard -- the SAME prerequisite Produce runs first, so a
later Produce simply reuses what this built), then collect the stories
that could not be built into a report the dashboard shows. (Storyboard QA
warnings -- caption length etc. -- are deliberately not reported here.)

Report lives in media/storyboard/episode_{id}_report.json (no schema
change). The endpoint writes status="running" synchronously before
queueing (CLAUDE.md rule 3); the task overwrites it as it progresses.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import Episode, EpisodeStory, NewsItem, StoryContent
from app.worker.celery_app import celery_app

MEDIA_ROOT = Path("media")

# A "running" report older than this is a task that died (worker restart);
# surfaced as failed instead of spinning forever.
STALE_AFTER = timedelta(minutes=90)


def report_path(episode_id: int) -> Path:
    return MEDIA_ROOT / "storyboard" / f"episode_{episode_id}_report.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_report(episode_id: int, report: dict) -> None:
    path = report_path(episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, path)


def start_report(episode_id: int) -> dict:
    report = {
        "episode_id": episode_id,
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "total": 0,
        "processed": 0,
        "built": 0,
        "reused": 0,
        "problems": [],
    }
    write_report(episode_id, report)
    return report


def read_report(episode_id: int) -> dict | None:
    path = report_path(episode_id)
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    if report.get("status") == "running":
        started = datetime.fromisoformat(report["started_at"])
        if datetime.now(timezone.utc) - started > STALE_AFTER:
            report["status"] = "failed"
            report["error"] = "Storyboard run was interrupted (no progress for 90+ minutes)."
    return report


def _issues_from_result(result: dict) -> list[dict]:
    if result.get("status") != "failed":
        return []
    return [{
        "severity": "error",
        "stage": result.get("stage") or "unknown",
        "message": result.get("error") or "Storyboard could not be built",
    }]


def run_storyboards_for_episode(db, episode_id: int, report: dict) -> dict:
    from app.content.storyboard_service import ensure_storyboard

    rows = (
        db.query(EpisodeStory, NewsItem)
        .join(NewsItem, EpisodeStory.story_id == NewsItem.id)
        .filter(EpisodeStory.episode_id == episode_id, EpisodeStory.selection_status.in_(("primary", "backup")))
        .order_by(EpisodeStory.rank_position.asc())
        .all()
    )
    report["total"] = len(rows)
    write_report(episode_id, report)

    for episode_story, item in rows:
        try:
            result = ensure_storyboard(db, item.id)
        except Exception as exc:
            db.rollback()
            result = {"story_id": item.id, "status": "failed", "stage": "exception", "error": str(exc)}

        issues = _issues_from_result(result)
        if result.get("status") != "failed":
            report["reused" if result.get("reused") else "built"] += 1
        if issues:
            content = db.query(StoryContent).filter(StoryContent.story_id == item.id).first()
            report["problems"].append({
                "story_id": item.id,
                "rank": episode_story.rank_position,
                "backup": episode_story.selection_status == "backup",
                "headline": (content.headline if content and content.headline else item.title),
                "issues": issues,
            })

        report["processed"] += 1
        write_report(episode_id, report)

    return report


@celery_app.task
def run_episode_storyboards(episode_id: int) -> dict:
    report = read_report(episode_id) or start_report(episode_id)
    try:
        with SessionLocal() as db:
            if db.get(Episode, episode_id) is None:
                raise ValueError("Episode not found")
            run_storyboards_for_episode(db, episode_id, report)
        report["status"] = "done"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    report["finished_at"] = _now_iso()
    write_report(episode_id, report)
    print(f"[episode_storyboard] Episode {episode_id}: {report['status']}, "
          f"{len(report['problems'])} stories with problems")
    return report
