from typing import NamedTuple

import json
import re

import requests
import trafilatura

from app.text_utils import response_text


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
MAX_TEXT_CHARS = 20_000  # cap on stored article text
RETRY_STATUS = {429, 500, 502, 503, 504}

USER_AGENT = (
    "Mozilla/5.0 (compatible; AIDaily25/1.0; "
    "+https://github.com/) article-summary-fetcher"
)


class ArticleExtractionResult(NamedTuple):
    text: str | None
    status: str  # "success" | "partial" | "empty_extraction" | "fetch_error" | "non_html"
    method: str | None = None  # "trafilatura_precision" | "trafilatura_recall" | "json_ld"


_JSON_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_PAYWALL_RE = re.compile(
    r"(subscribe to (continue|read)|sign in to (continue|read)|log in to (continue|read)|"
    r"this (article|content) is for subscribers|already a subscriber|create a free account to (continue|read)|"
    r"paywall|to continue reading)",
    re.I,
)


def _fetch(url: str):
    """One request, retried once on a timeout or a transient 5xx/429."""
    for _ in range(2):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError):
            continue
        except Exception:
            return None
        if response.status_code in RETRY_STATUS:
            continue
        try:
            response.raise_for_status()
        except Exception:
            return None
        return response
    return None


def _json_ld_body(html: str) -> str | None:
    """`articleBody` from schema.org JSON-LD, the fallback for pages whose body
    is script-rendered or that trafilatura cannot isolate."""
    for raw in _JSON_LD_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body.strip()) >= MIN_CONTENT_CHARS:
                    return body.strip()
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def _extract(html: str, url: str) -> tuple[str | None, str | None]:
    for method, kwargs in (
        ("trafilatura_precision", {"favor_precision": True}),
        ("trafilatura_recall", {"favor_recall": True}),
    ):
        try:
            text = trafilatura.extract(html, url=url, **kwargs)
        except Exception:
            text = None
        if text and len(text.strip()) >= MIN_CONTENT_CHARS:
            return text.strip(), method
    body = _json_ld_body(html)
    if body:
        return body, "json_ld"
    return None, None


def fetch_full_article_text(url: str) -> ArticleExtractionResult:
    """
    Best-effort full-body extraction of `url`'s article content, for the
    content dedup / sufficiency stage (app/tasks/content_dedup.py). Never
    raises: any failure degrades to `text=None` plus a status explaining why.
    One retry on a timeout or transient 5xx; extraction tries trafilatura
    (precision, then recall) and finally JSON-LD `articleBody`. A page that
    looks paywalled or cut off is returned as status "partial" (text kept, so
    the sufficiency check can judge it). No headless rendering and no
    CAPTCHA/paywall evasion -- a blocked page is recorded and skipped.
    """

    response = _fetch(url)
    if response is None:
        return ArticleExtractionResult(text=None, status="fetch_error")

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return ArticleExtractionResult(text=None, status="non_html")

    html = response_text(response)[:MAX_RESPONSE_BYTES]
    text, method = _extract(html, url)

    if not text:
        return ArticleExtractionResult(text=None, status="empty_extraction")

    text = text[:MAX_TEXT_CHARS]
    status = "partial" if _PAYWALL_RE.search(text) and len(text) < 2500 else "success"
    return ArticleExtractionResult(text=text, status=status, method=method)
