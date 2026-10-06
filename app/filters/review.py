"""Mapping from the classification gate to pipeline state, and the editor's review decision.

Review stories carry ai_relevance="ai_review". Every pool filter downstream (dedup, content-dedup,
verification, ranking, video QA, the stories API) selects on ai_relevance == "ai_candidate", so a
review story stays out of all of them until an editor promotes it.
"""
from datetime import datetime, timezone

from app.filters.classification_rules import CANDIDATE, REJECT, REVIEW

AI_CANDIDATE = "ai_candidate"
AI_REVIEW = "ai_review"
NOT_AI = "not_ai"

RELEVANCE_BY_DISPOSITION = {CANDIDATE: AI_CANDIDATE, REVIEW: AI_REVIEW, REJECT: NOT_AI}

PROMOTE = "promote"
REJECT_DECISION = "reject"


class ReviewError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def apply_review_decision(db, story_id: int, decision: str, now: datetime | None = None):
    """Promote an ai_review story to ai_candidate, or reject it to not_ai, and record the override."""
    from app.models import StoryState

    if decision not in (PROMOTE, REJECT_DECISION):
        raise ReviewError(f"Unknown decision {decision!r}; expected 'promote' or 'reject'.", 422)

    state = db.get(StoryState, story_id)
    if state is None:
        raise ReviewError(f"Story {story_id} not found.", 404)
    if state.ai_relevance != AI_REVIEW:
        raise ReviewError(f"Story {story_id} is not awaiting review (state: {state.ai_relevance}).", 409)

    if decision == PROMOTE:
        state.ai_relevance = AI_CANDIDATE
        state.review_decision = "promoted"
    else:
        state.ai_relevance = NOT_AI
        state.review_decision = "rejected"
    state.reviewed_at = now or datetime.now(timezone.utc)
    db.commit()
    return state
