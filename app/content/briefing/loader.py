"""DB lookup of same-day corroborating coverage (no web search)."""
from app.models import NewsItem, StoryState

MAX_CORROBORATING_SOURCES = 3


def load_corroborating(db, story: NewsItem) -> list:
    """Same-day coverage of this story by OTHER outlets that content-dedup already
    grouped under it (StoryState.canonical_story_id). DB only; no web search."""
    from app.content.briefing.pipeline import Corroborating

    siblings = (
        db.query(NewsItem)
        .join(StoryState, StoryState.id == NewsItem.id)
        .filter(StoryState.canonical_story_id == story.id, NewsItem.id != story.id)
        .filter(NewsItem.source_name != story.source_name)
        .order_by(NewsItem.id)
        .limit(MAX_CORROBORATING_SOURCES * 2)
        .all()
    )
    out = []
    for sib in siblings:
        text = (sib.raw_content or "").strip() or (sib.raw_summary or "").strip()
        if text:
            out.append(Corroborating(source_name=sib.source_name, text=text, url=getattr(sib, "canonical_url", None)))
        if len(out) >= MAX_CORROBORATING_SOURCES:
            break
    return out
