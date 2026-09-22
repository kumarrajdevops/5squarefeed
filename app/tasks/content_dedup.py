import hashlib
import time
from datetime import date

from app.content.article_extractor import fetch_full_article_text
from app.dates import target_collection_date
from app.db import SessionLocal
from app.filters.dedup import TIME_WINDOW_HOURS
from app.filters.content_similarity import (
    CONTENT_SIMILARITY_THRESHOLD,
    compute_cross_corpus_similarity,
    compute_pairwise_cosine_matrix,
    get_comparable_text,
)
from app.models import EpisodeStory, NewsItem, StoryState
from app.worker.celery_app import celery_app


# Polite, fixed delay between full-article fetches -- there's no
# existing rate-limiting infrastructure in this codebase to reuse
# (app/sources/article_fetcher.py has none either, but it's only ever
# invoked as a rare fallback; this stage runs for every new candidate
# story). At ~45-60 stories/day this adds well under a minute of total
# wall-clock time to a task that already runs unattended overnight.
REQUEST_DELAY_SECONDS = 1.0


def _content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enrich_and_dedup_by_content(db, target_date) -> dict:
    """
    Runs after title-based dedup (app/tasks/dedup.py), before
    verification -- one step in app/tasks/scheduled.py's
    run_daily_processing() sequence, called directly (no .delay()
    auto-chain into verification anymore; see app/tasks/dedup.py's
    docstring for why). Two independent duplicate-context checks, both
    using full article text + TF-IDF cosine similarity where a fetch
    succeeds (falling back to raw_summary/title otherwise -- a failed
    fetch never blocks either check, just weakens its signal for that
    one story):

    1. Same-batch content dedup: a second, more expensive pass over
       whatever today's cheap title-only pass (app/tasks/dedup.py)
       didn't already resolve, scoped to target_date -- catches
       cross-outlet duplicates under completely different headlines.
       Sets the SAME canonical_story_id/dedup_reason columns
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
    historical_repeats_found = 0

    # -------------------------------------------------
    # Every story_id ever selected as "primary" in a past episode --
    # computed once, up front, and excluded from `candidates` entirely
    # (not just from the historical-repeat pass below). A story that
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

    # -------------------------------------------------
    # Step 1: fetch full article text for anything not already
    # attempted (content_fetch_status IS NULL -- a prior failure still
    # counts as "attempted", so a permanently-blocked site is never
    # re-fetched on every run).
    # -------------------------------------------------

    for item, state in candidates:
        if state.content_fetch_status is not None:
            continue

        fetch_attempted += 1
        result = fetch_full_article_text(item.canonical_url)
        state.content_fetch_status = result.status

        if result.text:
            item.raw_content = result.text
            item.content_hash = _content_hash(result.text)
            fetch_success += 1
        else:
            fetch_fallback += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    db.commit()

    # -------------------------------------------------
    # Step 2: same-batch content dedup (goal 1). Same oldest-first
    # canonical-pool walk as find_duplicate_match(), just backed by
    # full-text TF-IDF cosine similarity instead of title matching,
    # and re-checking the same TIME_WINDOW_HOURS.
    # -------------------------------------------------

    remaining = [(item, state) for item, state in candidates if state.canonical_story_id is None]

    texts = [
        get_comparable_text(item.raw_content, item.raw_summary, item.title)
        for item, state in remaining
    ]

    comparable_indices = [i for i, t in enumerate(texts) if t is not None]

    if len(comparable_indices) >= 2:
        comparable_texts = [texts[i] for i in comparable_indices]
        similarity_matrix = compute_pairwise_cosine_matrix(comparable_texts)

        canonical_pool_positions: list[int] = []  # positions into comparable_indices

        for local_pos, story_idx in enumerate(comparable_indices):
            item, state = remaining[story_idx]

            best_score = 0.0
            best_match_item = None

            for pool_pos in canonical_pool_positions:
                other_idx = comparable_indices[pool_pos]
                other_item, other_state = remaining[other_idx]

                if other_item.published_at is None or item.published_at is None:
                    continue

                time_diff_hours = abs(
                    (item.published_at - other_item.published_at).total_seconds()
                ) / 3600.0
                if time_diff_hours > TIME_WINDOW_HOURS:
                    continue

                score = similarity_matrix[local_pos, pool_pos]
                if score >= CONTENT_SIMILARITY_THRESHOLD and score > best_score:
                    best_score = score
                    best_match_item = other_item

            if best_match_item is not None:
                state.canonical_story_id = best_match_item.id
                state.dedup_reason = (
                    f"content_tfidf_cosine={best_score:.2f}, "
                    f"matched_against_story_id={best_match_item.id}"
                )
                content_duplicates_found += 1
                print(
                    f"[content-dedup] Story {item.id} ({item.title!r}) "
                    f"marked as duplicate of story {best_match_item.id} "
                    f"({best_match_item.title!r}) -- {state.dedup_reason}"
                )
            else:
                canonical_pool_positions.append(local_pos)

    db.commit()

    # -------------------------------------------------
    # Step 3: historical repeat detection (goal 2). Compare every
    # remaining still-canonical story against the full corpus of past
    # primary (narrated) stories.
    # -------------------------------------------------

    still_remaining = [(item, state) for item, state in remaining if state.canonical_story_id is None]

    if historical_story_ids and still_remaining:
        historical_items = (
            db.query(NewsItem)
            .filter(NewsItem.id.in_(historical_story_ids))
            .all()
        )

        # "never re-select" (app/tasks/ranking.py) guarantees a story
        # is primary in at most one episode ever, so this
        # story_id -> (episode_id, rank_position) mapping is safe to
        # build as a plain dict -- used to give repeat_reason a
        # human-navigable "epXsY" label instead of a bare story id
        # (e.g. "ep9s2" = episode #9, rank 2), so an editor can go
        # straight to the episode/rank that actually narrated it.
        episode_slot_by_story_id = {
            story_id: (episode_id, rank_position)
            for story_id, episode_id, rank_position in (
                db.query(
                    EpisodeStory.story_id,
                    EpisodeStory.episode_id,
                    EpisodeStory.rank_position,
                )
                .filter(
                    EpisodeStory.selection_status == "primary",
                    EpisodeStory.story_id.in_(historical_story_ids),
                )
                .all()
            )
        }

        new_texts_map = {
            item.id: get_comparable_text(item.raw_content, item.raw_summary, item.title)
            for item, state in still_remaining
        }
        historical_texts_map = {
            item.id: get_comparable_text(item.raw_content, item.raw_summary, item.title)
            for item in historical_items
        }

        new_ids = [sid for sid, t in new_texts_map.items() if t is not None]
        historical_ids = [sid for sid, t in historical_texts_map.items() if t is not None]

        if new_ids and historical_ids:
            new_texts = [new_texts_map[sid] for sid in new_ids]
            historical_texts = [historical_texts_map[sid] for sid in historical_ids]

            cross_matrix = compute_cross_corpus_similarity(new_texts, historical_texts)

            items_by_id = {item.id: item for item, state in still_remaining}
            states_by_id = {item.id: state for item, state in still_remaining}

            for row_idx, new_id in enumerate(new_ids):
                best_col = int(cross_matrix[row_idx].argmax())
                best_score = float(cross_matrix[row_idx, best_col])

                if best_score >= CONTENT_SIMILARITY_THRESHOLD:
                    matched_story_id = historical_ids[best_col]
                    item = items_by_id[new_id]
                    state = states_by_id[new_id]
                    state.repeats_story_id = matched_story_id

                    slot = episode_slot_by_story_id.get(matched_story_id)
                    episode_label = f"ep{slot[0]}s{slot[1]}" if slot else "unknown slot"

                    state.repeat_reason = (
                        f"content_tfidf_cosine={best_score:.2f}, "
                        f"repeats_past_primary_story_id={matched_story_id} "
                        f"({episode_label})"
                    )
                    historical_repeats_found += 1
                    print(
                        f"[content-dedup] Story {item.id} ({item.title!r}) "
                        f"flagged as repeating past primary story "
                        f"{matched_story_id} -- {state.repeat_reason}"
                    )

    db.commit()

    result = {
        "fetch_attempted": fetch_attempted,
        "fetch_success": fetch_success,
        "fetch_fallback": fetch_fallback,
        "content_duplicates_found": content_duplicates_found,
        "historical_repeats_found": historical_repeats_found,
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
