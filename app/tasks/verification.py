import json

from sqlalchemy import func

from app.extraction.fact_extractor import extract_facts
from app.extraction.taxonomy import classify_category
from app.models import NewsItem, StoryState
from app.ranking.engine import compute_credibility_score
from app.verification.engine import verify_story


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
    # AI-candidate), scoped to target_date and to ones this task
    # hasn't touched yet so re-running is cheap and idempotent.
    rows = (
        db.query(NewsItem, StoryState)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(
            NewsItem.collection_date == target_date,
            StoryState.ai_relevance == "ai_candidate",
            StoryState.canonical_story_id.is_(None),
            StoryState.verification_status == "pending",
        )
        .all()
    )

    # Duplicate counts across ALL stories (not just the ones being
    # processed this run) -- a story processed in an earlier run could
    # have gained a new duplicate since, but re-processing
    # already-verified stories isn't this task's job (ranking reads
    # duplicate_count fresh itself); this count is only used for
    # stories currently in the `rows` list above.
    dup_count_rows = (
        db.query(StoryState.canonical_story_id, func.count(StoryState.id))
        .filter(StoryState.canonical_story_id.isnot(None))
        .group_by(StoryState.canonical_story_id)
        .all()
    )
    dup_counts: dict[int, int] = dict(dup_count_rows)

    processed = 0
    verified_count = 0
    unverified_count = 0

    for item, state in rows:
        facts = extract_facts(title=item.title, summary=item.raw_summary)
        state.extracted_facts = json.dumps(facts)
        state.taxonomy_category = classify_category(
            title=item.title,
            events=facts["events"],
            source_type=item.source_type,
        )

        duplicate_count = dup_counts.get(item.id, 0)
        credibility = compute_credibility_score(item.source_name)

        status, reason = verify_story(
            source_name=item.source_name,
            credibility_score=credibility,
            duplicate_count=duplicate_count,
        )
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
    }

    print(f"[verification] Completed: {result}")

    return result
