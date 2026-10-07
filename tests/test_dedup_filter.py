from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.filters.dedup import (
    find_duplicate_match,
    is_likely_duplicate_title,
    normalize_title,
    sequence_similarity,
    token_overlap_ratio,
)


def test_normalize_title_strips_punctuation_and_case():
    # Punctuation (including apostrophes) is replaced with a space, not
    # removed outright -- "Meta's" normalizes to "meta s", not "metas".
    assert normalize_title("Meta's Llama 4 Launches Today!") == "meta s llama 4 launches today"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("Too   many    spaces") == "too many spaces"


def test_identical_titles_are_duplicates():
    is_dup, seq, overlap = is_likely_duplicate_title(
        "OpenAI releases new model", "OpenAI releases new model"
    )
    assert is_dup
    assert seq == 1.0
    assert overlap == 1.0


def test_reworded_same_story_is_duplicate():
    """
    Real measured example from app/filters/dedup.py's own threshold
    rationale comment -- two outlets covering the same underlying
    story with different headline wording.
    """
    is_dup, _, _ = is_likely_duplicate_title(
        "Meta's Llama 4 launches today",
        "Meta launches Llama 4 today",
    )
    assert is_dup


def test_unrelated_titles_are_not_duplicates():
    is_dup, seq, overlap = is_likely_duplicate_title(
        "OpenAI releases new model",
        "Local team wins championship game",
    )
    assert not is_dup
    assert seq < 0.55
    assert overlap < 0.35


def test_shared_template_wording_does_not_merge_stories_about_different_companies():
    is_dup, seq, _ = is_likely_duplicate_title(
        "OpenAI admits misstep in handling AI agent interactions with Australian government sites – video",
        "Anthropic says AI agents didn’t breach Australian government websites – video",
    )
    assert seq >= 0.55 and not is_dup


def test_same_company_in_both_titles_still_merges():
    is_dup, _, _ = is_likely_duplicate_title(
        "OpenAI admits misstep in handling AI agent interactions with Australian government sites",
        "OpenAI admits misstep in AI agent interactions with Australian government websites",
    )
    assert is_dup


@dataclass
class FakeStory:
    id: int
    title: str
    published_at: datetime | None


def test_find_duplicate_match_finds_matching_story_within_window():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = [
        FakeStory(id=1, title="OpenAI releases new model", published_at=now),
        FakeStory(id=2, title="Unrelated story about weather", published_at=now),
    ]

    match, reason = find_duplicate_match(
        candidate_title="OpenAI releases a new model",
        candidate_published_at=now + timedelta(hours=2),
        candidate_id=99,
        canonical_pool=pool,
    )

    assert match is not None
    assert match.id == 1
    assert "matched_against_story_id=1" in reason


def test_find_duplicate_match_ignores_matches_outside_time_window():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = [
        FakeStory(id=1, title="OpenAI releases new model", published_at=now),
    ]

    # Same headline, but published far outside the 48h window --
    # should not be treated as the same real-world event.
    match, reason = find_duplicate_match(
        candidate_title="OpenAI releases new model",
        candidate_published_at=now + timedelta(days=30),
        candidate_id=99,
        canonical_pool=pool,
    )

    assert match is None
    assert reason is None


def test_find_duplicate_match_skips_self():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = [FakeStory(id=42, title="Some story", published_at=now)]

    match, _ = find_duplicate_match(
        candidate_title="Some story",
        candidate_published_at=now,
        candidate_id=42,
        canonical_pool=pool,
    )

    assert match is None


def test_find_duplicate_match_returns_none_without_published_at():
    """
    Without a reliable published_at, the filter conservatively treats
    the candidate as unique rather than risk merging unrelated stories
    -- see find_duplicate_match's own docstring.
    """
    pool = [FakeStory(id=1, title="OpenAI releases new model", published_at=None)]

    match, reason = find_duplicate_match(
        candidate_title="OpenAI releases new model",
        candidate_published_at=None,
        candidate_id=99,
        canonical_pool=pool,
    )

    assert match is None
    assert reason is None


def test_find_duplicate_match_picks_strongest_of_multiple_matches():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = [
        FakeStory(id=1, title="OpenAI releases a brand new model today", published_at=now),
        FakeStory(id=2, title="OpenAI releases new model", published_at=now),
    ]

    match, _ = find_duplicate_match(
        candidate_title="OpenAI releases new model",
        candidate_published_at=now,
        candidate_id=99,
        canonical_pool=pool,
    )

    # Story 2 is an exact title match (score 1.0) vs. story 1's partial
    # match -- the stronger match must win.
    assert match.id == 2


def test_sequence_and_token_helpers_are_symmetric():
    a, b = "openai releases new model", "new model openai releases"
    assert sequence_similarity(a, a) == 1.0
    assert token_overlap_ratio(a, b) == 1.0
