from datetime import date

from app.dates import target_collection_date
from app.db import SessionLocal
from app.filters.ai_relevance import calculate_ai_relevance
from app.filters.classification_rules import classify
from app.filters.review import AI_CANDIDATE, AI_REVIEW, NOT_AI, RELEVANCE_BY_DISPOSITION
from app.models import NewsItem, StoryState
from app.worker.celery_app import celery_app


def _apply_verdict(state: StoryState, verdict, *, reason_prefix: str = "") -> str:
    state.ai_relevance = RELEVANCE_BY_DISPOSITION[verdict.disposition]
    state.filter_reason = reason_prefix + verdict.reason
    state.classifier_version = verdict.version
    state.classifier_disposition = verdict.disposition
    state.classifier_ai_relatedness = verdict.ai_relatedness
    state.classifier_content_flag = verdict.content_flag
    return state.ai_relevance


def reevaluate_legacy_review(db, target_date) -> dict:
    """
    Automatically re-evaluate stories an earlier rules version parked as ai_review.

    There is no promote/reject step: each such row is classified again from its title and
    summary with the current rules and becomes ai_candidate or not_ai. The previous state is
    kept in filter_reason ("was ai_review under rules-v2"). Only rows still in the legacy
    state are touched, so this is idempotent and a no-op on any database without them.
    """
    rows = (
        db.query(StoryState, NewsItem)
        .join(NewsItem, NewsItem.id == StoryState.id)
        .filter(NewsItem.collection_date == target_date, StoryState.ai_relevance == AI_REVIEW)
        .all()
    )
    to_candidate = to_not_ai = 0
    for state, item in rows:
        prefix = f"Re-evaluated (was ai_review under {state.classifier_version or 'earlier rules'}). "
        verdict = classify(title=item.title, summary=item.raw_summary)
        if _apply_verdict(state, verdict, reason_prefix=prefix) == AI_CANDIDATE:
            to_candidate += 1
        else:
            to_not_ai += 1
    return {"reevaluated": len(rows), "to_ai_candidate": to_candidate, "to_not_ai": to_not_ai}


def classify_new_raw_items(db, target_date) -> dict:
    """
    The first step of processing (app/tasks/scheduled.py's
    run_daily_processing): creates the editorial.StoryState companion
    row for every raw.news_items row collected for target_date that
    doesn't have one yet, computing ai_relevance here -- this used to
    run inline during ingestion; it moves here so raw collection never
    computes anything editorial (see app/models.py's NewsItem vs
    StoryState split).

    The decision comes from app/filters/classification_rules.py and is
    fully automated and binary: candidate -> ai_candidate, reject ->
    not_ai. Nothing here produces or waits on a human decision; leftover
    legacy ai_review rows for the date are re-evaluated automatically
    (reevaluate_legacy_review). The structured result (rules version,
    disposition, reason) is stored on the row.

    Idempotent: only touches raw.news_items rows that don't already
    have a StoryState row (plus any leftover legacy ai_review rows), so
    calling this again for a target_date already processed does nothing.
    """

    already_classified_ids = {row[0] for row in db.query(StoryState.id).all()}

    query = db.query(NewsItem).filter(NewsItem.collection_date == target_date)
    if already_classified_ids:
        query = query.filter(NewsItem.id.notin_(already_classified_ids))

    unclassified = query.all()

    classified = 0
    ai_candidates = 0
    not_ai = 0

    for item in unclassified:
        verdict = classify(title=item.title, summary=item.raw_summary)
        ai_relevance = RELEVANCE_BY_DISPOSITION[verdict.disposition]

        # The keyword scorer's output is stored verbatim; it only feeds ranking and does not
        # affect the classification above.
        _, ai_score, _ = calculate_ai_relevance(title=item.title, summary=item.raw_summary)

        db.add(
            StoryState(
                id=item.id,
                ai_relevance=ai_relevance,
                ai_relevance_score=ai_score,
                filter_reason=verdict.reason,
                classifier_version=verdict.version,
                classifier_disposition=verdict.disposition,
                classifier_ai_relatedness=verdict.ai_relatedness,
                classifier_content_flag=verdict.content_flag,
            )
        )
        classified += 1
        if ai_relevance == AI_CANDIDATE:
            ai_candidates += 1
        elif ai_relevance == NOT_AI:
            not_ai += 1

    legacy = reevaluate_legacy_review(db, target_date)
    db.commit()

    result = {
        "classified": classified,
        "ai_candidates": ai_candidates,
        "not_ai": not_ai,
        "reevaluated_legacy_review": legacy["reevaluated"],
        "legacy_to_ai_candidate": legacy["to_ai_candidate"],
        "legacy_to_not_ai": legacy["to_not_ai"],
    }
    print(f"[classify] Completed: {result}")
    return result


@celery_app.task
def run_classify_new_raw_items(target_date_iso: str | None = None) -> dict:
    """
    Standalone Celery entry point for classify_new_raw_items -- lets
    this one stage be triggered independently (dev/debug -- see
    POST /api/v1/processing/classify in app/main.py) rather than only
    as part of app/tasks/scheduled.py's full run_daily_processing
    sequence. Same idempotency as that function itself.
    """
    target_date = date.fromisoformat(target_date_iso) if target_date_iso else target_collection_date()

    with SessionLocal() as db:
        return classify_new_raw_items(db, target_date)
