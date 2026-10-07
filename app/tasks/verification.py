import json
from datetime import date

from app.dates import target_collection_date
from app.db import SessionLocal
from app.extraction.fact_extractor import extract_facts
from app.extraction.taxonomy import classify_category
from app.models import NewsItem, StoryState
from app.ranking.engine import compute_credibility_score
from app.verification.engine import count_independent_outlets, verify_story
from app.worker.celery_app import celery_app


def run_fact_extraction_and_verification(db, target_date) -> dict:
    """
    Runs Fact Extraction then the Verification Engine (project.md's two
    distinct pipeline boxes) over every canonical, AI-candidate story
    collected for target_date that hasn't been processed yet -- run
    together in one pass since both need the same story pool and
    there's no reason for two separate queries. One step in
    app/tasks/scheduled.py's run_daily_processing() sequence, called
    directly after app/tasks/content_dedup.py (no .delay() auto-chain;
    see app/tasks/dedup.py's docstring for why -- this was already the
    last stage in the old chain, so there's nothing after it to chain
    into).

    Soft signal only (see app/verification/engine.py's module
    docstring for why): populates StoryState.extracted_facts/
    verification_status/verification_reason for editorial visibility
    and a small ranking score nudge -- never excludes a story from
    becoming eligible for ranking/selection. Also classifies each
    story into a 5-category taxonomy label
    (StoryState.taxonomy_category -- see app/extraction/taxonomy.py),
    reusing the same extracted events; labels only, same as
    verification -- does not affect ranking/selection either.
    """

    # Same eligibility filter run_ranking_selection uses (canonical,
    # AI-candidate), scoped to target_date. Every canonical story is
    # re-assessed on each run: the status depends on how many other
    # outlets carry the story, and dedup can change that after the first
    # pass (a duplicate merged later, a duplicate promoted to canonical).
    # Facts/taxonomy are deterministic per story and are only computed
    # the first time (status still "pending").
    rows = (
        db.query(NewsItem, StoryState)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == target_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
        )
        .all()
    )

    # Sources of every story merged into a canonical (any date).
    dup_sources: dict[int, list[str]] = {}
    for canonical_id, source_name in (
        db.query(StoryState.canonical_story_id, NewsItem.source_name)
        .join(NewsItem, NewsItem.id == StoryState.id)
        .filter(StoryState.canonical_story_id.isnot(None))
        .all()
    ):
        dup_sources.setdefault(canonical_id, []).append(source_name)

    processed = 0
    verified_count = 0
    unverified_count = 0
    changed = 0

    for item, state in rows:
        if state.verification_status == "pending":
            facts = extract_facts(title=item.title, summary=item.raw_summary)
            state.extracted_facts = json.dumps(facts)
            state.taxonomy_category = classify_category(
                title=item.title,
                events=facts["events"],
                source_type=item.source_type,
            )

        duplicate_count = count_independent_outlets(item.source_name, dup_sources.get(item.id, []))
        credibility = compute_credibility_score(item.source_name)

        status, reason = verify_story(
            source_name=item.source_name,
            credibility_score=credibility,
            duplicate_count=duplicate_count,
        )
        if (state.verification_status, state.verification_reason) != (status, reason):
            changed += 1
        state.verification_status = status
        state.verification_reason = reason

        processed += 1
        if status == "verified":
            verified_count += 1
        else:
            unverified_count += 1

    db.commit()

    result = {
        "processed": processed,
        "verified": verified_count,
        "unverified": unverified_count,
        "changed": changed,
    }

    print(f"[verification] Completed: {result}")

    return result


@celery_app.task
def run_verification(target_date_iso: str | None = None) -> dict:
    """
    Standalone Celery entry point for
    run_fact_extraction_and_verification -- see
    run_classify_new_raw_items's docstring (app/tasks/classify.py) for
    why this exists alongside the full run_daily_processing sequence.
    """
    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()

    with SessionLocal() as db:
        return run_fact_extraction_and_verification(db, target_date)
