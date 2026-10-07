import re
from datetime import datetime
from difflib import SequenceMatcher


# ---------------------------------------------------------
# Threshold rationale (measured against real headline pairs,
# see project notes / conversation history for the test data):
#
#   True-duplicate pairs (same story, different outlets) scored
#   seq=0.58-0.90, overlap=0.20-0.83.
#
#   Genuinely different stories topped out at seq=0.47, overlap=0.07.
#
# These thresholds sit with a wide safety margin above the
# different-story ceiling, so false positives (merging two distinct
# stories) should be rare. The known tradeoff: a story that is
# heavily paraphrased between outlets (e.g. "Drama swirls around
# OpenAI's milestone" vs "OpenAI's sly breakthrough sends a chill")
# can score below these thresholds and slip through as two separate
# stories. Catching that reliably needs semantic (embedding-based)
# similarity, which is out of scope for this deterministic filter.
# ---------------------------------------------------------
SEQUENCE_SIMILARITY_THRESHOLD = 0.55
TOKEN_OVERLAP_THRESHOLD = 0.35

# Cross-source coverage of the same story is almost always published
# within a couple of days of each other. Requiring stories to fall
# within this window before comparing titles avoids accidentally
# matching an old recurring headline pattern (e.g. two unrelated
# "OpenAI releases X" stories months apart).
TIME_WINDOW_HOURS = 48


def normalize_title(title: str) -> str:
    """
    Lowercase, strip punctuation, collapse whitespace.
    This makes "Meta's Llama 4 launches today" and
    "Meta launches Llama 4 today" comparable.
    """

    text = title.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sequence_similarity(a: str, b: str) -> float:
    """
    Character-sequence similarity ratio (0.0-1.0). Good at catching
    titles that share most of the same wording, even reordered.
    """
    return SequenceMatcher(None, a, b).ratio()


def token_overlap_ratio(a: str, b: str) -> float:
    """
    Jaccard similarity over whitespace tokens (0.0-1.0). Good at
    catching titles that share key entities/nouns even when the
    surrounding phrasing differs.
    """

    tokens_a = set(a.split())
    tokens_b = set(b.split())

    if not tokens_a or not tokens_b:
        return 0.0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b

    return len(intersection) / len(union)


def _names_different_companies(norm_a: str, norm_b: str) -> bool:
    """Both headlines name known companies and share none ("OpenAI admits..." vs "Anthropic says...").
    Shared template wording ("AI agents ... government websites - video") must not merge them: a title
    link is never re-judged later, so a false merge silently drops a different development."""
    from app.dedup.features import COMPANY_TOKENS, _COMPANY_FILLER

    names = COMPANY_TOKENS - _COMPANY_FILLER
    a, b = set(norm_a.split()) & names, set(norm_b.split()) & names
    return bool(a and b and not (a & b))


def is_likely_duplicate_title(title_a: str, title_b: str) -> tuple[bool, float, float]:
    """
    Compare two titles and decide whether they likely describe the
    same story. Returns (is_duplicate, sequence_score, overlap_score)
    so callers can log the reasoning.
    """

    norm_a = normalize_title(title_a)
    norm_b = normalize_title(title_b)

    seq_score = sequence_similarity(norm_a, norm_b)
    overlap_score = token_overlap_ratio(norm_a, norm_b)

    is_duplicate = (
        seq_score >= SEQUENCE_SIMILARITY_THRESHOLD
        or overlap_score >= TOKEN_OVERLAP_THRESHOLD
    ) and not _names_different_companies(norm_a, norm_b)

    return is_duplicate, seq_score, overlap_score


def find_duplicate_match(
    candidate_title: str,
    candidate_published_at: datetime | None,
    candidate_id: int,
    canonical_pool: list,
) -> tuple[object | None, str | None]:
    """
    Search a pool of already-canonical stories for the best duplicate
    match for the given candidate. `canonical_pool` items must expose
    `.id`, `.title`, `.published_at`.

    Returns (best_match_story_or_None, reason_string_or_None).
    """

    if candidate_published_at is None:
        # Without a reliable published_at we can't safely apply the
        # time-window check, so we conservatively treat it as unique
        # rather than risk merging unrelated stories.
        return None, None

    best_match = None
    best_score = 0.0
    best_reason = None

    for other in canonical_pool:

        if other.id == candidate_id:
            continue

        if other.published_at is None:
            continue

        time_diff_hours = abs(
            (candidate_published_at - other.published_at).total_seconds()
        ) / 3600.0

        if time_diff_hours > TIME_WINDOW_HOURS:
            continue

        is_duplicate, seq_score, overlap_score = is_likely_duplicate_title(
            candidate_title,
            other.title,
        )

        if not is_duplicate:
            continue

        # Multiple candidates in the pool might match; keep the
        # strongest match (highest of the two similarity metrics).
        combined_score = max(seq_score, overlap_score)

        if combined_score > best_score:
            best_score = combined_score
            best_match = other
            best_reason = (
                f"title_sequence_similarity={seq_score:.2f}, "
                f"token_overlap={overlap_score:.2f}, "
                f"time_diff_hours={time_diff_hours:.1f}, "
                f"matched_against_story_id={other.id}"
            )

    return best_match, best_reason
