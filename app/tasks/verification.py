import json

from sqlalchemy import func

from app.db import SessionLocal
from app.extraction.fact_extractor import extract_facts
from app.extraction.taxonomy import classify_category
from app.models import Story
from app.ranking.engine import compute_credibility_score
from app.verification.engine import verify_story
from app.worker.celery_app import celery_app


@celery_app.task
def run_fact_extraction_and_verification() -> dict:
    """
    Runs Fact Extraction then the Verification Engine (project.md's two
    distinct pipeline boxes) over every canonical, AI-candidate story
    that hasn't been processed yet -- run together in one task/DB pass
    since both need the same story pool and there's no reason for two
    separate queries. Chained automatically after dedup (see
    app/tasks/dedup.py) so this runs on every ingestion cycle, same as
    dedup itself.

    Soft signal only (see app/verification/engine.py's module
    docstring for why): populates Story.extracted_facts/
    verification_status/verification_reason for editorial visibility
    and a small ranking score nudge -- never excludes a story from
    becoming eligible for ranking/selection. Also classifies each
    story into a 5-category taxonomy label
    (Story.taxonomy_category -- see app/extraction/taxonomy.py),
    reusing the same extracted events; labels only, same as
    verification -- does not affect ranking/selection either.
    """

    with SessionLocal() as db:
        # Same eligibility filter run_ranking_selection uses (canonical,
        # AI-candidate), scoped to ones this task hasn't touched yet so
        # re-running is cheap and idempotent.
        stories = (
            db.query(Story)
            .filter(
                Story.ai_relevance == "ai_candidate",
                Story.canonical_story_id.is_(None),
                Story.verification_status == "pending",
            )
            .all()
        )

        # Duplicate counts across ALL stories (not just the ones being
        # processed this run) -- a story processed in an earlier run
        # could have gained a new duplicate since, but re-processing
        # already-verified stories isn't this task's job (ranking
        # reads duplicate_count fresh itself); this count is only used
        # for stories currently in the `stories` list above.
        dup_count_rows = (
            db.query(Story.canonical_story_id, func.count(Story.id))
            .filter(Story.canonical_story_id.isnot(None))
            .group_by(Story.canonical_story_id)
            .all()
        )
        dup_counts: dict[int, int] = dict(dup_count_rows)

        processed = 0
        verified_count = 0
        unverified_count = 0

        for story in stories:
            facts = extract_facts(title=story.title, summary=story.raw_summary)
            story.extracted_facts = json.dumps(facts)
            story.taxonomy_category = classify_category(
                title=story.title,
                events=facts["events"],
                source_type=story.source_type,
            )

            duplicate_count = dup_counts.get(story.id, 0)
            credibility = compute_credibility_score(story.source_name)

            status, reason = verify_story(
                source_name=story.source_name,
                credibility_score=credibility,
                duplicate_count=duplicate_count,
            )
            story.verification_status = status
            story.verification_reason = reason

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
