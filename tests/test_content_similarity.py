import pytest

from app.filters.content_similarity import (
    CONTENT_SIMILARITY_THRESHOLD,
    compute_cross_corpus_similarity,
    compute_pairwise_cosine_matrix,
    get_comparable_text,
    is_likely_duplicate_content,
)


LONG_ENOUGH = "x" * 60  # comfortably over MIN_COMPARABLE_CHARS (50)


def test_get_comparable_text_prefers_raw_content():
    assert get_comparable_text(LONG_ENOUGH + " full body", "short summary here", "Title") == LONG_ENOUGH + " full body"


def test_get_comparable_text_falls_back_to_summary():
    assert get_comparable_text(None, LONG_ENOUGH + " summary", "Title") == LONG_ENOUGH + " summary"


def test_get_comparable_text_falls_back_to_title():
    # Title itself is short, but it's the last resort -- returned as-is
    # regardless of MIN_COMPARABLE_CHARS since there's nothing better.
    assert get_comparable_text(None, None, "A short title") == "A short title"


def test_get_comparable_text_skips_too_short_candidates():
    # raw_content/raw_summary present but too thin to trust -- skip to
    # the next candidate in the fallback chain.
    assert get_comparable_text("tiny", "also tiny", "A usable title") == "A usable title"


# Two outlets running near-identical, lightly-edited coverage of the
# same event (the realistic case plain TF-IDF unigrams DO reliably
# catch -- syndicated/wire-sourced or one outlet closely following
# another's wording) under different headlines. A third, genuinely
# unrelated article as the negative case. NOTE: a heavier, independent
# paraphrase (two journalists each writing from scratch) scores much
# lower with this technique -- a known, documented limitation, see the
# comment on CONTENT_SIMILARITY_THRESHOLD in
# app/filters/content_similarity.py.
ARTICLE_A = (
    "OpenAI announced Tuesday that it has closed a new funding round "
    "valued at forty billion dollars, marking the largest financing "
    "round in the company's history. The ChatGPT maker said the "
    "funding will support continued expansion of its data center "
    "infrastructure and further research into more capable AI models "
    "over the coming year."
)
ARTICLE_B = (
    "OpenAI said on Tuesday it has closed a new funding round valued "
    "at forty billion dollars, the largest financing round in the "
    "company's history. The ChatGPT maker stated the funding will "
    "support continued expansion of its data center infrastructure "
    "and additional research into more capable AI models in the "
    "coming year."
)
ARTICLE_C = (
    "The city council voted last night to approve a new zoning plan "
    "for the downtown waterfront district, clearing the way for a "
    "mixed-use development that includes affordable housing units, "
    "a public park, and retail space along the riverfront promenade."
)


def test_pairwise_cosine_matrix_scores_paraphrase_pair_high_and_unrelated_low():
    matrix = compute_pairwise_cosine_matrix([ARTICLE_A, ARTICLE_B, ARTICLE_C])

    assert matrix.shape == (3, 3)
    for i in range(3):
        assert matrix[i, i] == pytest.approx(1.0)

    paraphrase_score = matrix[0, 1]
    unrelated_score = matrix[0, 2]

    assert paraphrase_score > unrelated_score
    assert paraphrase_score >= CONTENT_SIMILARITY_THRESHOLD
    assert unrelated_score < CONTENT_SIMILARITY_THRESHOLD


def test_cross_corpus_similarity_finds_the_right_historical_match():
    # ARTICLE_A is "new", ARTICLE_B (paraphrase) and ARTICLE_C
    # (unrelated) are the "historical" corpus -- the best match for
    # ARTICLE_A must be ARTICLE_B (index 0), not ARTICLE_C (index 1).
    matrix = compute_cross_corpus_similarity([ARTICLE_A], [ARTICLE_B, ARTICLE_C])

    assert matrix.shape == (1, 2)
    best_col = matrix[0].argmax()
    assert best_col == 0
    assert matrix[0, 0] >= CONTENT_SIMILARITY_THRESHOLD


def test_is_likely_duplicate_content_threshold_boundary():
    assert is_likely_duplicate_content(CONTENT_SIMILARITY_THRESHOLD) is True
    assert is_likely_duplicate_content(CONTENT_SIMILARITY_THRESHOLD - 0.01) is False
