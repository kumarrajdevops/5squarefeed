"""Deterministic, source-only supporting information for the story card's
light support area. Nothing here is generated: every candidate is a verbatim
sentence (or the publish date) already stored for the story, chosen by fixed
rules. No LLM, no paraphrasing, no invented quotes."""

import html
import re

MAX_FACT_CHARS = 135
MIN_FACT_CHARS = 45

_ABBREVIATIONS = ("u.s", "u.k", "dr", "mr", "mrs", "ms", "inc", "ltd", "co", "vs", "e.g", "i.e", "no", "st", "jr", "sr")
_BOILERPLATE = re.compile(
    r"subscribe|newsletter|sign up|sign in|cookie|copyright|all rights reserved|click here|read more|follow us|"
    r"share this|advertis|sponsored|@|https?://|www\.|\.com/|image:|photo:|credit:|getty|updated:|"
    r"request for comment|requests for comment|declined to comment|did not respond|privacy policy|terms of|contact us|download the|listen to|watch the|register",
    re.I,
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(text or "").lower()).strip()


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Sentences, never spanning a line break (the extractor puts headings and
    captions on their own lines)."""
    merged: list[str] = []
    for line in re.split(r"\n+", html.unescape(text or "")):
        line = clean_text(line)
        if not line:
            continue
        start = len(merged)
        for part in re.split(r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z0-9“\"‘'(])", line):
            if len(merged) > start and merged[-1].rstrip(".").lower().split(" ")[-1] in _ABBREVIATIONS:
                merged[-1] = f"{merged[-1]} {part}"
            else:
                merged.append(part)
    return [p.strip() for p in merged if p.strip()]


_CONTEXT_DEPENDENT = re.compile(
    r"\b(?:he|she|they|it|this|these|those|them|him|his|her|we|our|us|you|your|you've|below|above|post|article|here)\b", re.I
)
_BAD_START = re.compile(
    r"^(?:more broadly|moreover|additionally|furthermore|in fact|altogether|overall|meanwhile|however|but|and|also|still|so|then|whatever|create|direct|generate|turn|bring|make|meet|start|take|now|scale|learn|try|get|"
    r"explore|read|watch|discover|build|use|join|see|check|note|for example|in addition|for instance|this year)\b",
    re.I,
)


def _balanced_quotes(sentence: str) -> bool:
    return sentence.count("“") == sentence.count("”") and sentence.count("‘") <= sentence.count("’") + sentence.count("'") and sentence.count('"') % 2 == 0


def _tokens(text: str) -> set:
    return {t for t in _norm(text).split() if len(t) > 2}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _score(sentence: str, position: int) -> float:
    score = 0.0
    if re.search(r"[“\"][^”\"]{12,}[”\"]", sentence):
        score += 3.0
    if re.search(r"\d", sentence):
        score += 2.0
    if re.search(r"\b(?:said|says|told|according to)\b", sentence, re.I):
        score += 1.0
    score += min(1.0, sum(1 for w in sentence.split()[1:] if w[:1].isupper()) / 4)
    if 70 <= len(sentence) <= 125:
        score += 1.0
    return score - position * 0.02


def pick_article_fact(article_text: str, title: str, narration: str) -> str | None:
    """One verbatim article sentence that adds information the headline and
    narration do not already carry; None when nothing qualifies."""
    best: tuple[float, str] | None = None
    for position, sentence in enumerate(split_sentences(article_text)[:60]):
        if not (MIN_FACT_CHARS <= len(sentence) <= MAX_FACT_CHARS):
            continue
        if _BOILERPLATE.search(sentence) or not sentence[0].isupper() or sentence[-1] not in ".!?”\"":
            continue
        if (_CONTEXT_DEPENDENT.search(sentence) or re.search(r'[Â-Å][-¿]|â', sentence)) or _BAD_START.match(sentence) or not _balanced_quotes(sentence):
            continue
        if sum(1 for c in sentence if c.isupper()) > len(sentence) * 0.25:
            continue
        if _overlap(sentence, title) > 0.6 or _overlap(sentence, narration) > 0.6:
            continue
        score = _score(sentence, position)
        if best is None or score > best[0]:
            best = (score, sentence)
    return best[1] if best else None


def build_support_info(title: str, raw_summary: str, article_text: str, narration: str, published_at, source_name: str) -> dict:
    """Ordered, source-only candidates for the support area (see
    scene_renderer._support_text for how a scene picks one)."""
    summary = ""
    for sentence in split_sentences(raw_summary):
        if _norm(sentence) != _norm(title):
            summary = sentence
            break
    published = ""
    if published_at is not None:
        published = f"Published {published_at.strftime('%b')} {published_at.day}, {published_at.year}"
    return {
        "article_fact": pick_article_fact(article_text, title, narration),
        "summary": summary,
        "published": published,
        "source": source_name or "",
    }


def load_support_info(story_id: int) -> dict | None:
    """Reads the story's stored source data; None if the story is missing."""
    from app.db import SessionLocal
    from app.models import NewsItem, StoryContent

    with SessionLocal() as db:
        item = db.get(NewsItem, story_id)
        if item is None:
            return None
        content = db.query(StoryContent).filter_by(story_id=story_id).first()
        narration = (content.script_text if content else "") or ""
        return build_support_info(
            html.unescape(item.title or ""),
            item.raw_summary or "",
            item.raw_content or "",
            narration,
            item.published_at,
            item.source_name or "",
        )
