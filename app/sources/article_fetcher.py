import re
from html import unescape
from html.parser import HTMLParser

import requests

from app.text_utils import response_text


# Hacker News link-posts have no article content available from HN's own
# API (Algolia search only returns submission metadata -- points,
# comments, author), so without this every such story's summary would
# just be "N points, M comments on Hacker News." (see
# app/tasks/ingestion_hackernews.py). This module fetches the linked
# page itself and pulls a real description out of it.
REQUEST_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 200_000  # cap parsing cost on unexpectedly huge pages
MIN_SUMMARY_CHARS = 20

USER_AGENT = (
    "Mozilla/5.0 (compatible; AIDaily25/1.0; "
    "+https://github.com/) article-summary-fetcher"
)


class _MetaDescriptionParser(HTMLParser):
    """
    Pulls <meta name="description">, <meta property="og:description">,
    and the first substantive <p> text out of an HTML page -- just
    enough for a real one-paragraph summary, without pulling in a full
    HTML-parsing dependency (BeautifulSoup) for this one use. Attribute
    order in the source HTML doesn't matter (unlike a regex approach).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta_description: str | None = None
        self.og_description: str | None = None
        self.first_paragraph: str | None = None
        self._in_p = False
        self._p_buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "meta":
            name = (attrs_dict.get("name") or "").strip().lower()
            prop = (attrs_dict.get("property") or "").strip().lower()
            content = attrs_dict.get("content")

            if content:
                if name == "description" and not self.meta_description:
                    self.meta_description = content
                if prop == "og:description" and not self.og_description:
                    self.og_description = content

        elif tag == "p" and self.first_paragraph is None and not self._in_p:
            self._in_p = True
            self._p_buffer = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._p_buffer).split())
            if len(text) >= 40:
                self.first_paragraph = text

    def handle_data(self, data):
        if self._in_p:
            self._p_buffer.append(data)


def fetch_article_summary(url: str) -> str | None:
    """
    Best-effort fetch of an article page's own description. Tries
    <meta property="og:description">, then <meta name="description">,
    then the first substantive <p> tag, in that order.

    Returns None (never raises) on any failure -- network error,
    timeout, non-HTML response, no extractable text -- so callers can
    fall back to whatever placeholder text they already have rather
    than blocking ingestion on a slow or hostile site.
    """

    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception:
        return None

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return None

    parser = _MetaDescriptionParser()
    try:
        parser.feed(response_text(response)[:MAX_RESPONSE_BYTES])
    except Exception:
        return None

    for candidate in (
        parser.og_description,
        parser.meta_description,
        parser.first_paragraph,
    ):
        if not candidate:
            continue

        text = unescape(candidate).strip()
        text = re.sub(r"\s+", " ", text)

        if len(text) >= MIN_SUMMARY_CHARS:
            return text

    return None
