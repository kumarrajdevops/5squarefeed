"""compose_briefing: RSS/article text in, a graded, source-grounded script out."""
from dataclasses import dataclass, field

from app.content.briefing.composer import compose
from app.content.briefing.facts import Candidate, build_candidates
from app.content.briefing.hygiene import clean_source
from app.content.briefing.quality import FAIL, QualityReport, evaluate
from app.content.briefing.relevance import IRRELEVANT
from app.content.briefing.sufficiency import INSUFFICIENT, THIN, Assessment, assess
from app.content.briefing.textutil import end_sentence

MAX_CORROBORATING = 2


@dataclass
class Corroborating:
    """A same-day article about the same story from a different outlet."""
    source_name: str | None
    text: str
    url: str | None = None


@dataclass
class BriefingResult:
    headline: str
    body: str = ""
    script_text: str = ""
    sufficiency: Assessment | None = None
    quality: QualityReport | None = None
    roles: dict = field(default_factory=dict)
    provenance: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    corroborated: bool = False
    rejected_draft: str = ""
    headline_only: bool = False
    second_reason: str | None = None
    runner_up: str | None = None

    @property
    def sufficient_for_script(self) -> bool:
        return bool(self.script_text)

    def meta(self) -> dict:
        return {
            "sufficiency": self.sufficiency.summary() if self.sufficiency else None,
            "quality": self.quality.as_dict() if self.quality else None,
            "roles": self.roles,
            "provenance": self.provenance,
            "conflicts": self.conflicts,
            "corroborated": self.corroborated,
            "rejected_draft": self.rejected_draft or None,
            "headline_only": self.headline_only,
            "second_sentence_reason": self.second_reason,
            "runner_up": self.runner_up,
        }

    def generation_reason(self) -> str:
        if not self.script_text and self.rejected_draft and self.quality:
            return "failed the script quality gate: " + "; ".join(self.quality.gates + self.quality.reasons[:2])
        if not self.script_text:
            reasons = self.sufficiency.reasons if self.sufficiency else []
            return "insufficient for briefing: " + ("; ".join(reasons) or "no usable source text")
        if self.headline_only:
            return "headline-only: the source adds no event fact beyond the headline"
        parts = [f"{len(self.provenance)} source sentence(s) selected"]
        if self.corroborated:
            parts.append("corroborated by another outlet")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflicting sentence(s) dropped")
        return "; ".join(parts)


def _corroborating_candidates(headline: str, items: list[Corroborating]) -> tuple[list[Candidate], list[str]]:
    cands: list[Candidate] = []
    texts: list[str] = []
    for index, item in enumerate(items[:MAX_CORROBORATING]):
        cleaned = clean_source(item.text, headline)
        found = build_candidates(
            cleaned.sentences, headline, source_id=f"corroborating:{index}",
            source_name=item.source_name, url=item.url,
        )
        if found:
            cands.extend(found)
            texts.append(item.text)
    return cands, texts


def compose_briefing(
    title: str,
    raw_summary: str | None,
    raw_content: str | None,
    corroborating: list[Corroborating] | None = None,
) -> BriefingResult:
    headline = " ".join((title or "").split())
    suff = assess(headline, raw_summary, raw_content)
    result = BriefingResult(headline=headline, sufficiency=suff)

    extra: list[Candidate] = []
    extra_texts: list[str] = []
    if corroborating and suff.status != "sufficient":
        extra, extra_texts = _corroborating_candidates(headline, corroborating)
        irrelevant = suff.relevance is not None and suff.relevance.status == IRRELEVANT
        if suff.status == INSUFFICIENT and extra and not irrelevant:
            # A shallow teaser backed by an independent article that carries real facts.
            suff.reasons.append("corroborated by another outlet")
            suff.status = THIN

    if suff.status == INSUFFICIENT:
        result.quality = evaluate(headline, "", [], suff.status, suff.relevance)
        return result

    comp = compose(headline, suff.candidates, extra)
    result.body = comp.body_text
    result.script_text = comp.script_text
    result.roles = comp.roles
    result.conflicts = comp.conflicts
    result.second_reason = comp.second_reason
    result.runner_up = comp.runner_up
    result.corroborated = any(c.source_id != "primary" for c in comp.body)
    result.provenance = [
        {"sentence": c.text, "source": c.source_name if c.source_id != "primary" else "primary",
         "url": c.url, "roles": c.roles, "score": c.score, "class": c.cls}
        for c in comp.body
    ]
    sources = [raw_summary or "", raw_content or ""] + extra_texts
    if not comp.body and suff.headline_only:
        # Headline-only: only reached when the source has no event fact beyond the headline.
        result.headline_only = True
        result.script_text = end_sentence(headline)
    result.quality = evaluate(headline, result.script_text, sources, suff.status, suff.relevance)
    if not comp.body and not result.headline_only:
        result.script_text = ""
        result.quality.status = FAIL
    elif result.quality.status == FAIL:
        # Never narrate a script that fails the gate; keep it for inspection only.
        result.rejected_draft = result.script_text
        result.script_text = ""
        result.body = ""
    return result
