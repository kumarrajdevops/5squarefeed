"""
Single authoritative source for every business-date decision in the
pipeline. Nothing else in the codebase should call datetime.now() (or
inspect a browser-supplied date, or a story's own published_at) to
decide what "today" or "the target coverage day" means -- it all comes
through here, so the rule stays in exactly one place:

    target_date = current IST calendar date - 1 calendar day

The business calendar is IST (Asia/Kolkata), not UTC and not whatever
timezone the host/container happens to be running in.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_ist(reference: datetime | None = None) -> date:
    """The current IST calendar date. `reference` is for tests/dev only."""
    now = reference if reference is not None else datetime.now(timezone.utc)
    return now.astimezone(IST).date()


def target_collection_date(reference: datetime | None = None) -> date:
    """
    today_ist() - 1 calendar day -- the coverage day any collection or
    processing operation targets "today", regardless of what time of
    day it actually runs. `reference` overrides "now", for tests/dev
    only (e.g. simulating a different real-world date).
    """
    return today_ist(reference) - timedelta(days=1)


def coverage_window(target_date: date) -> tuple[datetime, datetime]:
    """
    The [start, end] UTC instants spanning target_date's full IST
    calendar day: target_date 00:00:00.000000 IST through
    target_date 23:59:59.999999 IST, converted to UTC (Story/raw
    timestamps are stored in UTC).
    """
    start_utc = datetime.combine(target_date, time.min, tzinfo=IST).astimezone(timezone.utc)
    end_utc = datetime.combine(target_date, time.max, tzinfo=IST).astimezone(timezone.utc)
    return start_utc, end_utc


def episode_key(target_date: date) -> str:
    """YYYYMMDD, e.g. "20260920" -- the human-facing business identifier."""
    return target_date.strftime("%Y%m%d")
