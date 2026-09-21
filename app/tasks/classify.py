from app.filters.ai_relevance import calculate_ai_relevance
from app.models import NewsItem, StoryState


def classify_new_raw_items(db, target_date) -> dict:
    """
    The first step of processing (app/tasks/scheduled.py's
    run_daily_processing): creates the editorial.StoryState companion
    row for every raw.news_items row collected for target_date that
    doesn't have one yet, computing ai_relevance here -- this used to
    run inline during ingestion; it moves here so raw collection never
    computes anything editorial (see app/models.py's NewsItem vs
    StoryState split).

    Idempotent: only touches raw.news_items rows that don't already
    have a StoryState row, so calling this again for a target_date
    already processed does nothing.
    """

    already_classified_ids = {row[0] for row in db.query(StoryState.id).all()}

    query = db.query(NewsItem).filter(NewsItem.collection_date == target_date)
    if already_classified_ids:
        query = query.filter(NewsItem.id.notin_(already_classified_ids))

    unclassified = query.all()

    classified = 0
    ai_candidates = 0

    for item in unclassified:
        ai_relevance, ai_score, filter_reason = calculate_ai_relevance(
            title=item.title,
            summary=item.raw_summary,
        )

        db.add(
            StoryState(
                id=item.id,
                ai_relevance=ai_relevance,
                ai_relevance_score=ai_score,
                filter_reason=filter_reason,
            )
        )
        classified += 1
        if ai_relevance == "ai_candidate":
            ai_candidates += 1

    db.commit()

    result = {"classified": classified, "ai_candidates": ai_candidates}
    print(f"[classify] Completed: {result}")
    return result
