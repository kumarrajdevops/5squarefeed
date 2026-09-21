from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.dates import IST, coverage_window, episode_key, target_collection_date, today_ist


IST_ZONE = ZoneInfo("Asia/Kolkata")


def _ist(y, m, d, h, mi, s=0, micro=0):
    return datetime(y, m, d, h, mi, s, micro, tzinfo=IST_ZONE)


def test_today_ist_uses_asia_kolkata_explicitly():
    assert IST.key == "Asia/Kolkata"


def test_target_date_2026_09_21_is_2026_09_20():
    reference = _ist(2026, 9, 21, 15, 0).astimezone(timezone.utc)
    assert target_collection_date(reference) == date(2026, 9, 20)


def test_target_date_2026_09_22_is_2026_09_21():
    reference = _ist(2026, 9, 22, 9, 30).astimezone(timezone.utc)
    assert target_collection_date(reference) == date(2026, 9, 21)


def test_today_ist_matches_reference_ist_date_not_utc_date():
    # 2026-09-21 00:30 IST is still 2026-09-20 in UTC (IST = UTC+5:30) --
    # today_ist() must report the IST date, not the UTC date.
    reference = _ist(2026, 9, 21, 0, 30).astimezone(timezone.utc)
    assert today_ist(reference) == date(2026, 9, 21)


def test_coverage_window_starts_exactly_at_midnight_ist():
    start, _ = coverage_window(date(2026, 9, 20))
    assert start == _ist(2026, 9, 20, 0, 0, 0, 0).astimezone(timezone.utc)


def test_coverage_window_ends_at_last_microsecond_of_the_day_ist():
    _, end = coverage_window(date(2026, 9, 20))
    assert end == _ist(2026, 9, 20, 23, 59, 59, 999999).astimezone(timezone.utc)


def test_coverage_window_is_disjoint_across_consecutive_days():
    _, end_20 = coverage_window(date(2026, 9, 20))
    start_21, _ = coverage_window(date(2026, 9, 21))
    assert end_20 < start_21


@pytest.mark.parametrize(
    "moment_ist, expected_in_window",
    [
        (_ist(2026, 9, 19, 23, 59, 59), False),   # just before the target day
        (_ist(2026, 9, 20, 0, 0, 0), True),        # exact start
        (_ist(2026, 9, 20, 23, 59, 59, 999999), True),  # exact end
        (_ist(2026, 9, 21, 0, 0, 0), False),       # just after the target day
    ],
)
def test_boundary_timestamps(moment_ist, expected_in_window):
    start, end = coverage_window(date(2026, 9, 20))
    moment_utc = moment_ist.astimezone(timezone.utc)
    assert (start <= moment_utc <= end) == expected_in_window


def test_episode_key_format():
    assert episode_key(date(2026, 9, 20)) == "20260920"
    assert episode_key(date(2026, 9, 21)) == "20260921"
