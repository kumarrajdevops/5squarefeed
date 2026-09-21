from datetime import datetime

import requests


# Official Algolia-powered Hacker News search API -- the same one
# that backs hn.algolia.com. Free, no API key.
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"

# Community-vetted threshold: below this, results skew toward noise
# (low-engagement submissions) rather than genuinely newsworthy
# discussion. Verified during research: points>15 across 7 sampled
# days consistently returned 19-24 qualifying AI stories/day.
MIN_POINTS = 15

REQUEST_TIMEOUT_SECONDS = 15


def fetch_ai_stories_for_range(start: datetime, end: datetime) -> list[dict]:
    """
    Fetch Hacker News stories mentioning "AI", posted within
    [start, end), with more than MIN_POINTS points.

    Unlike RSS (which only ever exposes a feed's *current* live
    contents -- no date-range query exists), the Algolia search API
    genuinely supports an exact [start, end) window on created_at_i,
    so collection and "backfill" are the same operation here: this is
    the ONLY Hacker News fetch function. There used to be a separate
    rolling `fetch_ai_stories(window_hours, now)` computing its window
    as "now minus N hours" -- deleted outright (not kept as a
    fallback) now that every collection call targets an explicit
    calendar day (see app/dates.py's coverage_window()).

    Paginated (Algolia caps at 100 hits/page) so a real day with more
    than 100 qualifying stories isn't silently truncated -- unlikely at
    this project's observed volume (~19-24 qualifying stories/day) but
    not assumed away.
    """

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    hits: list[dict] = []
    page = 0

    while True:
        response = requests.get(
            HN_SEARCH_URL,
            params={
                "tags": "story",
                "query": "AI",
                "numericFilters": f"points>{MIN_POINTS},created_at_i>{start_ts},created_at_i<{end_ts}",
                "hitsPerPage": 100,
                "page": page,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        page_hits = payload.get("hits", [])
        hits.extend(page_hits)

        if len(page_hits) < 100 or page + 1 >= payload.get("nbPages", 1):
            break
        page += 1

    return hits
