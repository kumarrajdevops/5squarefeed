import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


# Starting constant -- UNVALIDATED, unlike SEQUENCE_SIMILARITY_THRESHOLD/
# TOKEN_OVERLAP_THRESHOLD in app/filters/dedup.py (those cite measured
# real headline pairs). There is no equivalent measured TF-IDF corpus
# for this project yet. Plan: log every computed score (not just ones
# crossing this threshold) for the first 1-2 weeks of real runs, then
# tune from real data the same way the title thresholds were derived.
CONTENT_SIMILARITY_THRESHOLD = 0.35

# Below this many characters, a TF-IDF vector is too thin to trust as a
# real signal -- treat it as "no comparable text" rather than feed noise
# into the vectorizer.
MIN_COMPARABLE_CHARS = 50

_VECTORIZER_KWARGS = dict(
    stop_words="english",
    lowercase=True,
    strip_accents="unicode",
    ngram_range=(1, 1),
    max_df=0.9,
)

# Known, real limitation (measured during implementation, not just
# theoretical): plain TF-IDF unigrams, with no stemming/lemmatization,
# score two independent journalists' from-scratch rewrites of the same
# event surprisingly low (~0.14 in a real measured case) once word
# choice diverges enough ("closed"/"wrapped up", "center"/"centers",
# "AI"/"artificial intelligence"). What this DOES reliably catch:
# syndicated/wire-sourced coverage and lightly-edited reposts, which
# share most of the same vocabulary. Same class of gap
# app/filters/dedup.py's own title-threshold comment already
# documents for headlines ("Catching [heavy paraphrase] reliably needs
# semantic (embedding-based) similarity, which is out of scope").


def get_comparable_text(
    raw_content: str | None,
    raw_summary: str | None,
    title: str,
) -> str | None:
    """
    Best available text for similarity comparison: full article body
    (raw_content) if a fetch succeeded, else the short RSS/HN summary
    already stored at ingestion -- both only count if they clear
    MIN_COMPARABLE_CHARS, otherwise they're too thin to trust as a real
    signal. Title is the unconditional last resort (returned as-is
    regardless of length) since a story always has one; None is only
    possible if title itself is falsy, which shouldn't happen in
    practice.
    """

    for candidate in (raw_content, raw_summary):
        if candidate and len(candidate.strip()) >= MIN_COMPARABLE_CHARS:
            return candidate.strip()

    if title:
        return title.strip()

    return None


def compute_pairwise_cosine_matrix(texts: list[str]) -> np.ndarray:
    """
    Fit ONE TfidfVectorizer over all of `texts` and return the full
    len(texts) x len(texts) cosine similarity matrix. Used for the
    same-batch content-dedup pass (app/tasks/content_dedup.py) --
    fit once per run, then index into the matrix during the
    oldest-first walk, instead of refitting per pairwise comparison.
    """

    vectorizer = TfidfVectorizer(**_VECTORIZER_KWARGS)
    matrix = vectorizer.fit_transform(texts)
    return cosine_similarity(matrix)


def compute_cross_corpus_similarity(
    new_texts: list[str],
    historical_texts: list[str],
) -> np.ndarray:
    """
    Fit ONE TfidfVectorizer over new_texts + historical_texts combined
    (a shared vocabulary is required for the two sets' vectors to be
    comparable), then return cosine_similarity(new_matrix,
    historical_matrix) -- shape (len(new_texts), len(historical_texts)).
    Used for the historical-repeat-detection pass: refit over the whole
    corpus each run rather than caching vectors, deliberately -- see
    TODO.md's design note on why this is fine at this project's scale.
    """

    vectorizer = TfidfVectorizer(**_VECTORIZER_KWARGS)
    combined = vectorizer.fit_transform(new_texts + historical_texts)

    new_matrix = combined[: len(new_texts)]
    historical_matrix = combined[len(new_texts) :]

    return cosine_similarity(new_matrix, historical_matrix)


def is_likely_duplicate_content(
    score: float,
    threshold: float = CONTENT_SIMILARITY_THRESHOLD,
) -> bool:
    return score >= threshold
