from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "ai_news",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.ingestion",
        "app.tasks.ingestion_hackernews",
        "app.tasks.dedup",
        "app.tasks.content_dedup",
        "app.tasks.verification",
        "app.tasks.ranking",
        "app.tasks.content",
        "app.tasks.episode_video",
        "app.tasks.episode_qa",
        "app.tasks.publishing",
        "app.tasks.scheduled",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
)

# Daily News Cycle (project.md's "Daily Execution Architecture") --
# three overnight collection passes, then a 4 AM IST cutoff that
# ranks/selects/produces/QAs the episode, ready for human approval by
# 6 AM IST. Times are IST directly since `timezone` above is
# "Asia/Kolkata" -- Celery Beat evaluates crontab entries in that zone,
# not UTC. Requires a `celery beat` process running alongside the
# worker (see docker-compose.yml's `beat` service) -- the worker alone
# does not schedule anything on its own.
celery_app.conf.beat_schedule = {
    "collect-10pm-ist": {
        "task": "app.tasks.ingestion.ingest_news",
        "schedule": crontab(hour=22, minute=0),
    },
    "collect-10pm-ist-hackernews": {
        "task": "app.tasks.ingestion_hackernews.ingest_hackernews_stories",
        "schedule": crontab(hour=22, minute=0),
    },
    "collect-1am-ist": {
        "task": "app.tasks.ingestion.ingest_news",
        "schedule": crontab(hour=1, minute=0),
    },
    "collect-1am-ist-hackernews": {
        "task": "app.tasks.ingestion_hackernews.ingest_hackernews_stories",
        "schedule": crontab(hour=1, minute=0),
    },
    "collect-330am-ist-final": {
        "task": "app.tasks.ingestion.ingest_news",
        "schedule": crontab(hour=3, minute=30),
    },
    "collect-330am-ist-final-hackernews": {
        "task": "app.tasks.ingestion_hackernews.ingest_hackernews_stories",
        "schedule": crontab(hour=3, minute=30),
    },
    "collection-cutoff-4am-ist": {
        "task": "app.tasks.scheduled.run_nightly_cutoff",
        "schedule": crontab(hour=4, minute=0),
    },
}
