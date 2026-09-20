from typing import NamedTuple

import requests
import trafilatura


# Full-body fetch + extraction for a story's already-known URL (found
# via one of the existing RSS/HN sources -- NOT a new discovery
# mechanism). Mirrors app/sources/article_fetcher.py's defensive
# contract closely: same timeout/User-Agent/content-type-check/never-
# raises style, just extracting the full article body instead of a
# one-paragraph description. MAX_RESPONSE_BYTES is higher than that
# module's 200_000 since full bodies are bigger than a meta tag.
REQUEST_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 500_000
MIN_CONTENT_CHARS = 200

USER_AGENT = (
    "Mozilla/5.0 (compatible; AIDaily25/1.0; "
    "+https://github.com/) article-summary-fetcher"
)


class ArticleExtractionResult(NamedTuple):
    text: str | None
    status: str  # "success" | "empty_extraction" | "fetch_error" | "non_html"


def fetch_full_article_text(url: str) -> ArticleExtractionResult:
    """
    Best-effort full-body extraction of `url`'s article content, for
    the content-based dedup/repeat-detection stage (see
    app/tasks/content_dedup.py). Never raises: any failure -- network
    error, timeout, non-200, non-HTML content-type, or an extraction
    that yields too little usable text -- degrades to a result with
    `text=None` and a status explaining why, exactly like
    fetch_article_summary() degrades to None. No retries, no headless
    rendering, no CAPTCHA-solving -- a blocked/paywalled/JS-rendered
    page is recorded and skipped, never fought (same standing rule
    behind why VentureBeat's Vercel bot-challenge was left unfixed
    rather than building evasion tooling).

    Uses requests.get() for the actual HTTP fetch (so the same
    timeout/User-Agent/size-cap controls as the rest of this codebase
    apply) and trafilatura purely as the boilerplate/paywall-banner
    removal step on the already-fetched HTML -- not trafilatura's own
    networking.
    """

    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception:
        return ArticleExtractionResult(text=None, status="fetch_error")

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return ArticleExtractionResult(text=None, status="non_html")

    html = response.text[:MAX_RESPONSE_BYTES]

    try:
        extracted = trafilatura.extract(html, url=url, favor_precision=True)
    except Exception:
        return ArticleExtractionResult(text=None, status="fetch_error")

    if not extracted or len(extracted.strip()) < MIN_CONTENT_CHARS:
        return ArticleExtractionResult(text=None, status="empty_extraction")

    return ArticleExtractionResult(text=extracted.strip(), status="success")
