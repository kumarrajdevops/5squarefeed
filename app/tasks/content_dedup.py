import hashlib
import json
import time
from datetime import date

from app.content.article_extractor import fetch_full_article_text
from app.content.briefing import briefing_fingerprint
from app.content.briefing.loader import load_corroborating
from app.content.briefing.pipeline import compose_briefing
from app.dates import target_collection_date
from app.db import SessionLocal
from app.dedup.embedder import EmbedderUnavailable, default_embedder
from app.dedup.features import build_features
from app.dedup.same_day import DayStory, cluster_same_day
from app.models import EpisodeStory, NewsItem, StoryState
from app.tasks.dedup import pinned_story_ids
from app.worker.celery_app import celery_app


# Polite, fixed delay between full-article fetches -- there's no
# existing rate-limiting infrastructure in this codebase to reuse
# (app/sources/article_fetcher.py has none either, but it's only ever
# invoked as a rare fallback; this stage runs for every new candidate
# story). At ~45-60 stories/day this adds well under a minute of total
# wall-clock time to a task that already runs unattended overnight.
REQUEST_DELAY_SECONDS = 1.0


RETRYABLE_FETCH_STATUSES = ("fetch_error", "empty_extraction")


def _detail(state) -> dict:
    try:
        return json.loads(state.sufficiency_detail) if state.sufficiency_detail else {}
    except ValueError:
        return {}


def sufficiency_is_stale(state) -> bool:
    """Never assessed, or assessed under different briefing code than is running now
    (a verdict from before a composer change would otherwise rank stories that can no
    longer be narrated, and keep excluding ones that now can)."""
    return state.source_sufficiency is None or _detail(state).get("briefing_fingerprint") != briefing_fingerprint()


def assess_source_sufficiency(db, item, state) -> str:
    """Can this story support a source-grounded briefing? Runs the same
    deterministic composer the script stage uses, with same-day sibling
    coverage as corroboration, so a story is only called sufficient when a
    script would really be produced. Stored on StoryState; ranking excludes
    "insufficient" stories. No network, no LLM."""
    from app.content.briefing.textutil import word_count

    result = compose_briefing(
        item.title, item.raw_summary, item.raw_content, corroborating=load_corroborating(db, item)
    )
    status = result.sufficiency.status if result.sufficiency else "insufficient"
    if not result.script_text:
        status = "insufficient"
    detail = {**_detail(state), **(result.sufficiency.summary() if result.sufficiency else {})}
    detail.update(
        {
            "status": status,
            "reason": result.generation_reason(),
            "quality_status": result.quality.status if result.quality else None,
            "corroborated": result.corroborated,
            "briefing_fingerprint": briefing_fingerprint(),
        }
    )
    state.source_sufficiency = status
    state.sufficiency_detail = json.dumps(detail, default=str)
    state.source_word_count = word_count(item.raw_content or item.raw_summary or "")
    return status


def _content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enrich_and_dedup_by_content(db, target_date, embedder=None) -> dict:
    """
    Runs after title-based dedup (app/tasks/dedup.py), before
    verification -- one step in app/tasks/scheduled.py's
    run_daily_processing() sequence, called directly (no .delay()
    auto-chain into verification anymore; see app/tasks/dedup.py's
    docstring for why). Two independent duplicate-context checks, both
    using full article text where a fetch succeeds (falling back to
    raw_summary/title otherwise -- a failed fetch never blocks either
    check, just weakens its signal for that one story):

    1. Same-batch content dedup: a second, more expensive pass over
       whatever today's cheap title-only pass (app/tasks/dedup.py)
       didn't already resolve, scoped to target_date -- catches
       cross-outlet duplicates under completely different headlines.
       Judged by app/dedup/same_day.py (same-day-v2: the historical
       rule ladder + TF-IDF only as corroboration; the day's episode
       selections are in the pool and never demoted). Sets the SAME canonical_story_id/dedup_reason columns
       title-dedup uses, so verification's duplicate_count and
       ranking's exclusion benefit automatically, no changes needed
       there.

    2. Historical repeat detection: compares each still-canonical
       candidate against every story ever selected as "primary" in a
       past episode (unscoped by date -- this corpus is genuinely
       all-time) -- catches a *different* story (different outlet/
       URL/headline) covering an event already narrated to the
       public, which the identity-based "never re-select" check in
       app/tasks/ranking.py cannot catch on its own. Sets the
       repeats_story_id/repeat_reason columns.

    Never fails the pipeline on fetch failures (see
    app/content/article_extractor.py's never-raises contract) --
    always degrades to raw_summary/title-based comparison. Commits its
    own work at the end, same as before -- an earlier/later stage
    failing doesn't roll this stage back.
    """

    fetch_attempted = 0
    fetch_success = 0
    fetch_fallback = 0
    content_duplicates_found = 0
    insufficient_found = 0

    # -------------------------------------------------
    # Every story_id ever selected as "primary" in a past episode --
    # computed once, up front, and excluded from `candidates` entirely
    # (not only from the same-day comparison). A story that
    # IS already a past primary has already been through the full
    # pipeline; it must never be treated as a "new" candidate again,
    # or it ends up being compared against a historical corpus that
    # includes its own row and trivially "repeats" itself at
    # cosine=1.0 (caught live during verification -- see TODO.md).
    # -------------------------------------------------

    historical_story_ids = {
        row[0] for row in
        db.query(EpisodeStory.story_id)
        .filter(EpisodeStory.selection_status == "primary")
        .distinct()
        .all()
    }

    # -------------------------------------------------
    # Still-canonical AI-candidate stories collected for target_date --
    # the same pool app/tasks/dedup.py itself queries, minus anything
    # already a past primary. Anything title-dedup already resolved
    # never reaches this (more expensive) stage.
    # -------------------------------------------------

    candidates_query = (
        db.query(NewsItem, StoryState)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == target_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
        )
    )

    if historical_story_ids:
        candidates_query = candidates_query.filter(NewsItem.id.notin_(historical_story_ids))

    candidates = candidates_query.order_by(NewsItem.published_at.asc()).all()

    # Stories already in this date's episode (backups; primaries are
    # excluded above) go first and are never marked duplicates -- the
    # first run's selection sticks until approval.
    pinned = pinned_story_ids(db, target_date)
    candidates = [c for c in candidates if c[0].id in pinned] + [c for c in candidates if c[0].id not in pinned]

    # -------------------------------------------------
    # Step 1: fetch full article text for anything not already
    # attempted (content_fetch_status IS NULL). A prior failure counts as
    # "attempted" too, except fetch_error/empty_extraction, which get exactly
    # one more try, so a permanently-blocked site is never hammered.
    # -------------------------------------------------

    for item, state in candidates:
        refetch = (
            state.content_fetch_status in RETRYABLE_FETCH_STATUSES
            and not _detail(state).get("refetched")
        )
        if state.content_fetch_status is not None and not refetch:
            continue

        fetch_attempted += 1
        result = fetch_full_article_text(item.canonical_url)
        state.content_fetch_status = result.status
        if refetch:
            # One extra attempt per story, ever (a blocked site is not hammered).
            state.sufficiency_detail = json.dumps({**_detail(state), "refetched": True})

        if result.text:
            item.raw_content = result.text
            item.content_hash = _content_hash(result.text)
            state.content_extraction_method = result.method
            fetch_success += 1
        else:
            fetch_fallback += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    db.commit()

    # -------------------------------------------------
    # Step 2: same-day content dedup (app/dedup/same_day.py). Same-development test, not mere
    # similarity: the historical detector's rule ladder over embeddings, with TF-IDF overlap only as
    # corroboration. This date's draft-episode selections (primary AND backup) are in the pool as
    # pinned canonicals, so a second copy of a story already in the episode is caught. Past primaries
    # of other episodes stay out (historical dedup owns those).
    # -------------------------------------------------

    remaining = [(item, state) for item, state in candidates if state.canonical_story_id is None]
    skipped_reason = None
    comparisons = decide_calls = elected = 0

    walk_rows = list(remaining)
    walk_ids = {item.id for item, _ in walk_rows}
    pinned_primary_ids = (pinned & historical_story_ids) - walk_ids
    if pinned_primary_ids:
        walk_rows += (
            db.query(NewsItem, StoryState)
            .join(StoryState, StoryState.id == NewsItem.id)
            .filter(NewsItem.id.in_(pinned_primary_ids), StoryState.canonical_story_id.is_(None))
            .all()
        )

    day_stories = [
        DayStory(
            story_id=item.id,
            features=build_features(
                item.id, item.title, item.raw_content, item.raw_summary, state.content_fetch_status,
                item.canonical_url, item.content_hash, item.source_name,
            ),
            published_at=item.published_at,
            source_name=item.source_name,
            raw_content=item.raw_content,
            raw_summary=item.raw_summary,
            title=item.title,
        )
        for item, state in walk_rows
    ]
    by_id = {item.id: (item, state) for item, state in walk_rows}

    try:
        day = cluster_same_day(day_stories, pinned, embedder or default_embedder())
    except EmbedderUnavailable as exc:
        skipped_reason = str(exc)
        day = None
        print(f"[content-dedup] same-day semantic dedup skipped, stories stay eligible: {exc}")

    if day is not None:
        comparisons, decide_calls, elected = day.comparisons, day.decide_calls, len(day.demoted)
        for story_id, link in day.links.items():
            item, state = by_id[story_id]
            state.canonical_story_id = link.canonical_id
            state.dedup_reason = link.dedup_reason()
            content_duplicates_found += 1
            print(
                f"[content-dedup] Story {item.id} ({item.title!r}) marked as duplicate of story "
                f"{link.canonical_id} ({by_id[link.canonical_id][0].title!r}) -- {state.dedup_reason}"
            )
        if day.demoted:
            # Duplicates recorded by earlier runs (or title dedup) of a displaced canonical follow it,
            # so a duplicate never points at a duplicate.
            for stale in (
                db.query(StoryState)
                .filter(StoryState.canonical_story_id.in_(day.demoted))
                .all()
            ):
                if stale.id not in day.links:
                    stale.canonical_story_id = day.links[stale.canonical_story_id].canonical_id

    db.commit()

    # -------------------------------------------------
    # Step 2b: source sufficiency. After same-day dedup, so sibling
    # coverage can corroborate a thin story. Assessed once per story.
    # -------------------------------------------------

    for item, state in remaining:
        if state.canonical_story_id is None and sufficiency_is_stale(state):
            if assess_source_sufficiency(db, item, state) == "insufficient":
                insufficient_found += 1

    db.commit()

    # Historical repeat detection no longer lives here: app/tasks/historical_dedup.py decides
    # duplicate vs new development against published primaries. repeats_story_id/repeat_reason
    # keep their past values and are no longer written.

    result = {
        "fetch_attempted": fetch_attempted,
        "fetch_success": fetch_success,
        "fetch_fallback": fetch_fallback,
        "content_duplicates_found": content_duplicates_found,
        "same_day_comparisons": comparisons,
        "same_day_decide_calls": decide_calls,
        "same_day_canonicals_elected": elected,
        "same_day_skipped_reason": skipped_reason,
        "insufficient_found": insufficient_found,
    }

    print(f"[content-dedup] Completed: {result}")

    return result


@celery_app.task
def run_enrich_and_dedup_by_content(target_date_iso: str | None = None) -> dict:
    """
    Standalone Celery entry point for enrich_and_dedup_by_content --
    see run_classify_new_raw_items's docstring (app/tasks/classify.py)
    for why this exists alongside the full run_daily_processing
    sequence.
    """
    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()

    with SessionLocal() as db:
        return enrich_and_dedup_by_content(db, target_date)
