"""
Pipeline integration of the rules classifier (app/tasks/classify.py): the three
dispositions map to ai_candidate / ai_review / not_ai, the structured result is
stored on the row, and the migration that adds those columns is consistent with
the model.
"""
import importlib.util
import re
from datetime import date, datetime, timezone
from pathlib import Path

from app.filters.classification_rules import RULES_VERSION
from app.filters.ai_relevance import calculate_ai_relevance
from app.models import NewsItem, StoryState
from app.tasks.classify import classify_new_raw_items

DAY = date(2026, 9, 20)

CANDIDATE_TITLE = "OpenAI announces new AI model"
REVIEW_TITLE = "The future of AI agents in banking"
REJECT_TITLE = "Local bakery wins regional award"

NEW_COLUMNS = [
    "classifier_version",
    "classifier_disposition",
    "classifier_ai_relatedness",
    "classifier_content_flag",
    "review_decision",
    "reviewed_at",
]


def _item(db, title, url, summary=None):
    item = NewsItem(
        title=title,
        canonical_url=url,
        source_name="Test Source",
        source_type="rss",
        published_at=datetime(2026, 9, 20, 8, 0),
        collected_at=datetime(2026, 9, 20, 9, 0),
        collection_date=DAY,
        raw_summary=summary,
        status="collected",
    )
    db.add(item)
    db.commit()
    return item


def _state(db, item):
    return db.get(StoryState, item.id)


def test_dispositions_map_to_existing_and_new_states(db_session):
    cand = _item(db_session, CANDIDATE_TITLE, "https://e.test/c")
    rev = _item(db_session, REVIEW_TITLE, "https://e.test/r")
    rej = _item(db_session, REJECT_TITLE, "https://e.test/x")

    result = classify_new_raw_items(db_session, DAY)

    assert result == {"classified": 3, "ai_candidates": 1, "ai_review": 1}
    assert _state(db_session, cand).ai_relevance == "ai_candidate"
    assert _state(db_session, rev).ai_relevance == "ai_review"
    assert _state(db_session, rej).ai_relevance == "not_ai"


def test_structured_result_version_and_reason_are_recorded(db_session):
    cand = _item(db_session, CANDIDATE_TITLE, "https://e.test/c")
    rev = _item(db_session, REVIEW_TITLE, "https://e.test/r")
    rej = _item(db_session, REJECT_TITLE, "https://e.test/x")
    classify_new_raw_items(db_session, DAY)

    for item, disposition, relatedness in [
        (cand, "candidate", "core"),
        (rev, "review", "core"),
        (rej, "reject", "none"),
    ]:
        state = _state(db_session, item)
        assert state.classifier_version == RULES_VERSION
        assert state.classifier_disposition == disposition
        assert state.classifier_ai_relatedness == relatedness
        assert state.filter_reason
        assert state.review_decision is None
        assert state.reviewed_at is None

    assert _state(db_session, cand).filter_reason.startswith("Candidate:")
    assert _state(db_session, rev).filter_reason.startswith("Review:")
    assert _state(db_session, rej).filter_reason.startswith("Rejected:")


def test_content_flag_is_recorded_for_non_news_rejects(db_session):
    deal = _item(db_session, "Best AI headphones deal: 40% off today", "https://e.test/d")
    classify_new_raw_items(db_session, DAY)

    state = _state(db_session, deal)
    assert state.ai_relevance == "not_ai"
    assert state.classifier_content_flag == "deal"


def test_score_is_the_legacy_keyword_scorer_output_for_every_disposition(db_session):
    items = [
        _item(db_session, CANDIDATE_TITLE, "https://e.test/c"),
        _item(db_session, REVIEW_TITLE, "https://e.test/r"),
        _item(db_session, REJECT_TITLE, "https://e.test/x"),
    ]
    classify_new_raw_items(db_session, DAY)

    for item in items:
        _, legacy_score, _ = calculate_ai_relevance(item.title, item.raw_summary)
        assert _state(db_session, item).ai_relevance_score == legacy_score


def test_candidate_the_legacy_keyword_list_misses_keeps_a_zero_score(db_session):
    # The rules recognise "Perplexity" (the legacy keyword list does not), so this is a new
    # candidate. Its score stays the legacy 0.0: classification does not touch ranking inputs.
    item = _item(db_session, "Perplexity launches new browser", "https://e.test/p")
    classify_new_raw_items(db_session, DAY)

    state = _state(db_session, item)
    assert state.ai_relevance == "ai_candidate"
    assert state.ai_relevance_score == 0.0


def test_rerun_is_idempotent_and_never_touches_classified_rows(db_session):
    rev = _item(db_session, REVIEW_TITLE, "https://e.test/r")
    classify_new_raw_items(db_session, DAY)

    # An editor decision made between runs must survive the next run.
    state = _state(db_session, rev)
    state.ai_relevance = "ai_candidate"
    state.review_decision = "promoted"
    state.reviewed_at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    db_session.commit()

    again = classify_new_raw_items(db_session, DAY)

    assert again == {"classified": 0, "ai_candidates": 0, "ai_review": 0}
    state = _state(db_session, rev)
    assert state.ai_relevance == "ai_candidate"
    assert state.review_decision == "promoted"
    assert db_session.query(StoryState).count() == 1


def test_only_the_target_date_is_classified(db_session):
    _item(db_session, CANDIDATE_TITLE, "https://e.test/c")
    other = _item(db_session, REVIEW_TITLE, "https://e.test/r")
    other.collection_date = date(2026, 9, 19)
    db_session.commit()

    result = classify_new_raw_items(db_session, DAY)

    assert result["classified"] == 1
    assert db_session.get(StoryState, other.id) is None


def test_legacy_rows_without_a_classifier_result_stay_valid(db_session):
    # Rows classified before this change have NULL in every new column.
    item = _item(db_session, "Legacy story", "https://e.test/l")
    db_session.add(StoryState(id=item.id, ai_relevance="ai_candidate", ai_relevance_score=0.8))
    db_session.commit()

    state = _state(db_session, item)
    assert all(getattr(state, name) is None for name in NEW_COLUMNS)


# --- migration / schema state -------------------------------------------------

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MIGRATION = VERSIONS / "d5b8e2c4f917_add_classification_result_columns.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_d5b8e2c4f917", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_columns_match_the_model_and_are_nullable():
    migration = _load_migration()
    model_columns = StoryState.__table__.columns

    assert [name for name, _ in migration.COLUMNS] == NEW_COLUMNS
    for name, type_ in migration.COLUMNS:
        column = model_columns[name]
        assert column.nullable is True
        assert type(column.type) is type(type_)
        assert getattr(column.type, "length", None) == getattr(type_, "length", None)


def test_migration_extends_the_previous_head_and_is_the_only_head():
    migration = _load_migration()
    assert migration.revision == "d5b8e2c4f917"
    assert migration.down_revision == "c3d7e9a1f524"

    revisions, parents = set(), set()
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?::[^=]+)?\s*=\s*"([^"]+)"', text, re.M)
        down = re.search(r'^down_revision(?::[^=]+)?\s*=\s*(?:"([^"]+)"|None)', text, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down and down.group(1):
            parents.add(down.group(1))
    assert revisions - parents == {"d5b8e2c4f917"}
