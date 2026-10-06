from datetime import date, datetime, timezone

from app.models import NewsItem, StoryState
from app.tasks.verification import run_fact_extraction_and_verification
from app.verification.engine import count_independent_outlets, verify_story

DAY = date(2026, 10, 6)


def _story(db, slug, source, **state):
    item = NewsItem(
        title=f"Story {slug}", canonical_url=f"https://example.com/{slug}", source_name=source,
        source_type="rss", published_at=datetime.now(timezone.utc), collected_at=datetime.now(timezone.utc),
        collection_date=DAY, status="collected",
    )
    db.add(item)
    db.flush()
    db.add(StoryState(id=item.id, ai_relevance="ai_candidate", **state))
    db.commit()
    return item.id


def _state(db, story_id):
    db.expire_all()
    return db.get(StoryState, story_id)


def test_same_outlet_duplicates_are_not_independent():
    assert count_independent_outlets("The Guardian AI", ["The Guardian AI", " the guardian ai "]) == 0
    assert count_independent_outlets("The Guardian AI", ["The Guardian AI", "The Verge AI"]) == 1
    assert count_independent_outlets("A", ["B", "b", "C"]) == 2


def test_same_outlet_copy_does_not_verify(db_session):
    main = _story(db_session, "main", "Unlisted Blog")
    _story(db_session, "copy", "Unlisted Blog", canonical_story_id=main)

    run_fact_extraction_and_verification(db_session, DAY)

    assert _state(db_session, main).verification_status == "unverified"


def test_other_outlet_copy_verifies(db_session):
    main = _story(db_session, "main", "Unlisted Blog")
    _story(db_session, "copy", "Another Outlet", canonical_story_id=main)

    run_fact_extraction_and_verification(db_session, DAY)

    state = _state(db_session, main)
    assert state.verification_status == "verified"
    assert "1 other outlet" in state.verification_reason


def test_status_follows_later_dedup_changes_and_rerun_is_idempotent(db_session):
    main = _story(db_session, "main", "Unlisted Blog")
    copy = _story(db_session, "copy", "Another Outlet")

    first = run_fact_extraction_and_verification(db_session, DAY)
    assert _state(db_session, main).verification_status == "unverified"
    assert first["changed"] == 2

    # dedup later merges the second outlet's story into the first
    copy_state = _state(db_session, copy)
    copy_state.canonical_story_id = main
    db_session.commit()

    second = run_fact_extraction_and_verification(db_session, DAY)
    assert _state(db_session, main).verification_status == "verified"
    assert second["processed"] == 1 and second["changed"] == 1

    third = run_fact_extraction_and_verification(db_session, DAY)
    assert third["changed"] == 0
    assert _state(db_session, main).verification_status == "verified"


def test_story_promoted_from_duplicate_to_canonical_leaves_pending(db_session):
    main = _story(db_session, "main", "Unlisted Blog")
    copy = _story(db_session, "copy", "Another Outlet", canonical_story_id=main)
    run_fact_extraction_and_verification(db_session, DAY)
    assert _state(db_session, copy).verification_status == "pending"

    # the copy is promoted (e.g. same-day election): it must now be assessed, and facts are computed once
    db_session.get(StoryState, main).canonical_story_id = copy
    db_session.get(StoryState, copy).canonical_story_id = None
    db_session.commit()

    run_fact_extraction_and_verification(db_session, DAY)
    promoted = _state(db_session, copy)
    assert promoted.verification_status == "verified"
    assert promoted.extracted_facts is not None


def test_primary_source_still_verifies_alone():
    assert verify_story("OpenAI", 0.95, 0)[0] == "verified"
    assert verify_story("Unlisted Blog", 0.60, 0)[0] == "unverified"
