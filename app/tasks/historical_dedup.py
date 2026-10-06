import logging
from datetime import date

from app.dates import target_collection_date
from app.db import SessionLocal
from app.dedup.decision import METHOD_VERSION
from app.dedup.detector import Stats, build_corpus, detect_stories
from app.dedup.embedder import Embedder, EmbedderUnavailable, default_embedder
from app.dedup.relations import decided_story_ids, save_verdict
from app.models import EpisodeStory, NewsItem, StoryState
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def deduplicate_against_history(db, target_date: date, embedder: Embedder | None = None) -> dict:
    """
    Decide, for each still-canonical AI-candidate story collected for target_date, whether it
    reports a development 5squareFeed has already published (duplicate) or not (new_development),
    and record the verdict in editorial.historical_story_relations.

    Corpus: primaries of approved/published episodes dated before target_date. Idempotent per
    (story, matched story, method version): rerunning inserts nothing. If the embedding model is
    unavailable the stage logs it, records nothing and lets the pipeline continue -- an
    undecided story simply stays eligible, as it was before this stage existed.
    """
    ai_candidates = (
        db.query(NewsItem.id)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == target_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
        )
        .all()
    )
    past_primaries = {
        row[0]
        for row in db.query(EpisodeStory.story_id).filter(EpisodeStory.selection_status == "primary").all()
    }
    candidate_ids = [row[0] for row in ai_candidates if row[0] not in past_primaries]
    already_decided = decided_story_ids(db, candidate_ids)
    todo = [sid for sid in candidate_ids if sid not in already_decided]

    result = {
        "method_version": METHOD_VERSION,
        "candidates": len(candidate_ids),
        "already_decided": len(already_decided),
        "corpus_size": 0,
        "duplicates": 0,
        "new_developments": 0,
        "unrelated": 0,
        "skipped_reason": None,
    }
    if not todo:
        return result

    cached = embedder is None
    embedder = embedder or default_embedder()
    stats = Stats()
    try:
        corpus = build_corpus(db, embedder)
        result["corpus_size"] = len(corpus)
        if len(corpus) == 0:
            result["unrelated"] = len(todo)
            return result
        verdicts = detect_stories(db, todo, corpus, embedder, {sid: target_date for sid in todo}, stats)
    except EmbedderUnavailable as exc:
        logger.error("[historical-dedup] skipped, embedding model unavailable: %s", exc)
        result["skipped_reason"] = str(exc)
        return result
    finally:
        if cached:
            embedder.save()

    for sid in todo:
        verdict = verdicts.get(sid)
        if verdict is None:
            result["unrelated"] += 1
            continue
        if save_verdict(db, verdict):
            result["duplicates" if verdict.is_duplicate else "new_developments"] += 1
            print(
                f"[historical-dedup] Story {sid} -> {verdict.decision.decision} "
                f"(matches {verdict.matched_story_id}: {verdict.decision.reason})"
            )
    db.commit()
    result["runtime_seconds"] = round(stats.runtime_seconds, 2)
    print(f"[historical-dedup] Completed: {result}")
    return result


@celery_app.task
def run_historical_dedup(target_date_iso: str | None = None) -> dict:
    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()
    with SessionLocal() as db:
        return deduplicate_against_history(db, target_date)
