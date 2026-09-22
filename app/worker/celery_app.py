from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "ai_news",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.collection",
        "app.tasks.classify",
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

# Daily News Cycle -- three overnight collection passes (still at the
# same times as always; NOT redesigned by the calendar-day change),
# then a 4 AM IST processing pass. Times are IST directly since
# `timezone` above is "Asia/Kolkata" -- Celery Beat evaluates crontab
# entries in that zone, not UTC. Requires a `celery beat` process
# running alongside the worker (see docker-compose.yml's `beat`
# service) -- the worker alone does not schedule anything on its own.
#
# Mechanical update only, not a scheduler redesign: each collection
# entry now points at app.tasks.collection.run_collection (the single
# combined RSS+HN task -- see app/tasks/ingestion.py's and
# app/tasks/ingestion_hackernews.py's docstrings) instead of two
# separate per-source tasks, since those per-source functions are no
# longer Celery tasks in their own right. run_collection computes its
# own target_date at whatever moment it actually executes
# (app/dates.py's target_collection_date() -- today's IST date minus
# one day) -- it is NOT given an explicit date here. Documented
# consequence, not solved in this change: IST midnight falls between
# the 10 PM pass and the 1 AM pass, so on any given real calendar day
# the 10 PM pass computes target_date = today-1 while the 1 AM and
# 3:30 AM passes (already past midnight, on the next calendar day)
# compute target_date = today -- one day later than the 10 PM pass,
# not the same day a naive reading would suggest.
celery_app.conf.beat_schedule = {
    "collect-10pm-ist": {
        "task": "app.tasks.collection.run_collection",
        "schedule": crontab(hour=22, minute=0),
        "kwargs": {"trigger_type": "scheduled"},
    },
    "collect-1am-ist": {
        "task": "app.tasks.collection.run_collection",
        "schedule": crontab(hour=1, minute=0),
        "kwargs": {"trigger_type": "scheduled"},
    },
    "collect-330am-ist-final": {
        "task": "app.tasks.collection.run_collection",
        "schedule": crontab(hour=3, minute=30),
        "kwargs": {"trigger_type": "scheduled"},
    },
    "process-4am-ist": {
        "task": "app.tasks.scheduled.run_daily_processing",
        "schedule": crontab(hour=4, minute=0),
    },
}
