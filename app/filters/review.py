"""Mapping from the classification gate to pipeline state.

Classification is fully automated and binary: candidate -> ai_candidate, reject -> not_ai.
There is no human step in classification; editorial approval happens after the episode exists.

AI_REVIEW is a legacy value. Rules-v1/v2 parked uncertain stories as ai_review; those rows may
still exist in old data, but nothing produces the value any more and nothing waits on it. The
classification stage re-evaluates any leftover ai_review row with the current rules
(app/tasks/classify.py: reevaluate_legacy_review).
"""
from app.filters.classification_rules import CANDIDATE, REJECT

AI_CANDIDATE = "ai_candidate"
NOT_AI = "not_ai"
AI_REVIEW = "ai_review"  # legacy, read-only: never assigned to a new classification

RELEVANCE_BY_DISPOSITION = {CANDIDATE: AI_CANDIDATE, REJECT: NOT_AI}
