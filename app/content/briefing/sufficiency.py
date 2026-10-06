"""Is there enough real, new information in the source to write a briefing?
Pure and deterministic. Thresholds are constants so they can be calibrated."""
from dataclasses import dataclass, field

from app.content.briefing.composer import compose
from app.content.briefing.events import EVENT, headline_is_event, is_explainer_headline
from app.content.briefing.facts import Candidate, build_candidates
from app.content.briefing.hygiene import CleanedSource, clean_source
from app.content.briefing.relevance import IRRELEVANT, RELEVANT, Relevance, assess_relevance, relevance_source_text
from app.content.briefing.textutil import word_count

SUFFICIENT = "sufficient"
THIN = "thin"
INSUFFICIENT = "insufficient"

# Fact points of the chosen lead (event verb + entity + number/quote = 3 of a possible 4).
LEAD_FULL_POINTS = 3
MAX_PROMO_RATIO = 0.5
# Below this many usable words an article is treated as a teaser, not a story.
ARTICLE_MIN_WORDS = 80


@dataclass
class Assessment:
    status: str = INSUFFICIENT
    reasons: list[str] = field(default_factory=list)
    source_kind: str = "none"  # article | rss | none
    usable_words: int = 0
    raw_words: int = 0
    meaningful_sentences: int = 0
    novel_sentences: int = 0
    fact_points: int = 0
    promo_ratio: float = 0.0
    truncated: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    cleaned: CleanedSource | None = None
    relevance: Relevance | None = None
    event_sentences: int = 0
    lead_points: int = 0
    headline_only: bool = False

    def summary(self) -> dict:
        return {
            "status": self.status,
            "event_sentences": self.event_sentences,
            "headline_only": self.headline_only,
            "reasons": self.reasons,
            "source_kind": self.source_kind,
            "usable_words": self.usable_words,
            "raw_words": self.raw_words,
            "meaningful_sentences": self.meaningful_sentences,
            "novel_sentences": self.novel_sentences,
            "fact_points": self.fact_points,
            "promo_ratio": self.promo_ratio,
            "truncated": self.truncated,
            "relevance": self.relevance.as_dict() if self.relevance else None,
        }


def prepare_source(title: str, raw_summary: str | None, raw_content: str | None) -> tuple[CleanedSource, str]:
    """The article when it holds a real body, else the RSS summary."""
    article = clean_source(raw_content, title)
    if article.usable_words >= ARTICLE_MIN_WORDS:
        return article, "article"
    summary = clean_source(raw_summary, title)
    if article.usable_words > summary.usable_words:
        return article, "article"
    if summary.sentences:
        summary.truncated = summary.truncated or article.truncated
        return summary, "rss"
    article.truncated = article.truncated or summary.truncated
    return article, "none"


def assess(title: str, raw_summary: str | None, raw_content: str | None) -> Assessment:
    cleaned, kind = prepare_source(title, raw_summary, raw_content)
    result = Assessment(
        source_kind=kind,
        usable_words=cleaned.usable_words,
        raw_words=cleaned.raw_words,
        promo_ratio=cleaned.promo_ratio,
        truncated=cleaned.truncated,
        cleaned=cleaned,
    )
    result.relevance = assess_relevance(title, relevance_source_text(raw_summary, raw_content))
    result.candidates = build_candidates(cleaned.sentences, title)
    result.meaningful_sentences = sum(1 for s in cleaned.sentences if word_count(s) >= 8)
    result.novel_sentences = len(result.candidates)
    result.fact_points = sum(c.points for c in result.candidates)

    reasons = result.reasons
    if not cleaned.sentences:
        reasons.append("no usable text after cleaning")
        result.status = INSUFFICIENT
        return result
    if kind == "rss":
        reasons.append("only an RSS teaser is available")
    if result.relevance.status == IRRELEVANT:
        reasons.append(f"source does not match the story: {result.relevance.reason}")
        result.status = INSUFFICIENT
        return result
    if is_explainer_headline(title):
        reasons.append("how-to / explainer headline, not a news event")
        result.status = INSUFFICIENT
        return result
    if cleaned.promo_ratio > MAX_PROMO_RATIO:
        reasons.append(f"mostly boilerplate ({cleaned.promo_ratio:.0%} of sentences)")
        result.status = INSUFFICIENT
        return result

    event_pool = [c for c in result.candidates if c.cls == EVENT]
    result.event_sentences = len(event_pool)
    body = compose(title, result.candidates).body
    if body:
        lead = body[0]
        result.lead_points = lead.points
        if lead.points >= LEAD_FULL_POINTS and (kind == "article" or lead.points >= LEAD_FULL_POINTS + 1):
            result.status = SUFFICIENT
            reasons.append("an event sentence with concrete facts")
        else:
            result.status = THIN
            reasons.append("one event sentence with few concrete facts")
    elif not event_pool and headline_is_event(title) and result.relevance.status == RELEVANT:
        # Genuinely nothing to add: every source sentence is explanation, background,
        # significance or motive, so the headline stands alone.
        result.status = THIN
        result.headline_only = True
        reasons.append("headline-only: the source adds no event fact beyond the headline")
    elif event_pool:
        result.status = INSUFFICIENT
        reasons.append("no usable lead: no event sentence fits in 35 words and the caption limits without truncation")
    else:
        result.status = INSUFFICIENT
        reasons.append("no event sentence beyond the headline")

    if cleaned.truncated and result.status != INSUFFICIENT:
        reasons.append("source is truncated")
    return result
