"""Persistence of historical-duplicate verdicts (editorial.historical_story_relations)."""
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dedup.decision import METHOD_VERSION
from app.dedup.detector import Verdict
from app.models import HistoricalStoryRelation

DUPLICATE = "duplicate"


def effective_decision():
    """SQL expression for the decision that counts: an editor override wins over the detector."""
    return func.coalesce(HistoricalStoryRelation.editor_override, HistoricalStoryRelation.decision)


def effective_duplicate_story_ids(db: Session, method_version: str = METHOD_VERSION) -> set[int]:
    rows = db.execute(
        select(HistoricalStoryRelation.story_id).where(
            HistoricalStoryRelation.method_version == method_version,
            effective_decision() == DUPLICATE,
        )
    )
    return {row[0] for row in rows}


def decided_story_ids(db: Session, story_ids: list[int], method_version: str = METHOD_VERSION) -> set[int]:
    if not story_ids:
        return set()
    found: set[int] = set()
    for i in range(0, len(story_ids), 500):
        chunk = story_ids[i : i + 500]
        found |= {
            row[0]
            for row in db.execute(
                select(HistoricalStoryRelation.story_id).where(
                    HistoricalStoryRelation.method_version == method_version,
                    HistoricalStoryRelation.story_id.in_(chunk),
                )
            )
        }
    return found


def save_verdict(db: Session, verdict: Verdict, method_version: str = METHOD_VERSION) -> bool:
    """Insert one verdict unless this (story, matched story, method) already has one. Never
    updates an existing row, so replays and reruns are idempotent and overrides are never lost."""
    exists = db.execute(
        select(HistoricalStoryRelation.id).where(
            HistoricalStoryRelation.story_id == verdict.story_id,
            HistoricalStoryRelation.matched_story_id == verdict.matched_story_id,
            HistoricalStoryRelation.method_version == method_version,
        )
    ).first()
    if exists:
        return False
    d = verdict.decision
    db.add(
        HistoricalStoryRelation(
            story_id=verdict.story_id,
            matched_story_id=verdict.matched_story_id,
            decision=d.decision,
            semantic_similarity=round(verdict.similarity, 4),
            title_similarity=round(verdict.title_similarity, 4),
            development_match=d.development_match[:120],
            new_facts_detected=json.dumps(d.new_facts),
            reason=d.reason,
            rule=d.rule,
            content_basis=d.content_basis,
            method_version=method_version,
        )
    )
    return True
