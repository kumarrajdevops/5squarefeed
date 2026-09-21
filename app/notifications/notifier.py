import requests

from app.config import settings
from app.models import Notification


# Fixed, small category set -- see Notification's own docstring for why
# these two specifically (the only failures that mean "the whole day's
# episode didn't happen"), and why nothing else triggers a notification.
EPISODE_VIDEO_FAILED = "episode_video_failed"
EPISODE_PUBLISH_FAILED = "episode_publish_failed"

# Same timeout convention as app/sources/article_fetcher.py and
# app/content/article_extractor.py -- short and fixed, never retried.
SLACK_TIMEOUT_SECONDS = 8


def _send_slack_alert(category: str, episode_id: int | None, message: str) -> bool:
    """
    Best-effort Slack delivery via an Incoming Webhook -- never raises.
    A failure here (webhook not configured, network error, Slack
    itself erroring) must never break the caller, since notify() is
    always invoked from inside a failure's own except block; returns
    False rather than propagating so notify() can still record an
    accurate `delivered` flag.
    """

    if not settings.slack_webhook_url:
        return False

    episode_label = f"episode #{episode_id}" if episode_id is not None else "no episode"
    text = f"5squareFeed alert -- {category} ({episode_label}): {message}"

    try:
        response = requests.post(
            settings.slack_webhook_url,
            json={"text": text},
            timeout=SLACK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notification] Slack delivery failed: {exc}")
        return False


def notify(db, category: str, episode_id: int | None, message: str) -> None:
    """
    Record a failure alert, log it loudly to the worker's own output,
    and (if SLACK_WEBHOOK_URL is configured) post it to Slack.
    `Notification.delivered` reflects whether that Slack post actually
    succeeded -- False (not an error) when no webhook is configured at
    all, same "absence is a valid, non-error state" pattern as
    Story.verification_status's "pending".

    Takes the caller's existing db session rather than opening a new
    one -- this is always called from inside an already-open
    SessionLocal() block in the task that detected the failure, same
    pattern as mark_content_failed() in app/tasks/content.py. Does NOT
    commit -- the caller's own subsequent db.commit() (already
    persisting the failure status) covers this row too.
    """

    print(f"[notification] ALERT ({category}) episode_id={episode_id}: {message}")

    delivered = _send_slack_alert(category, episode_id, message)

    notification = Notification(
        category=category,
        episode_id=episode_id,
        message=message,
        delivered=delivered,
    )
    db.add(notification)
