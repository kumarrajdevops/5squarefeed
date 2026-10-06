"""Compose WHAT HAPPENED for a story.

The narrated script is the headline (storyboard segment 0 is a forced hero scene)
followed by one verbatim source sentence that reports the event, and a second only
when the first cannot stand alone. Nothing is invented, padded or truncated:
compression only deletes low-value clauses, never attribution, quotes, numbers or
negations. There is no minimum length; 15-25 words is a preference, not a target."""
import re
from dataclasses import dataclass, field, replace

from app.content.briefing.events import EVENT, is_attributed
from app.content.briefing.facts import Candidate, numeric_conflict, starts_with_pronoun
from app.content.briefing.textutil import end_sentence, overlap, word_count

IDEAL_WORDS = (15, 25)
ACCEPT_WORDS = (10, 30)
MAX_BODY_WORDS = 35
MAX_BODY = 2
MAX_OVERLAP = 0.6

_PAREN_RE = re.compile(r"\s*\((?![^)]*\d)[^)]{3,80}\)")
_TRAILING_CLAUSE_RE = re.compile(r",\s+(?:which|while|although|though|whereas)\b[^,]*$", re.I)
_APPOSITIVE_RE = re.compile(r"^([^,]{2,60}),\s+(?:a|an|the|one of)\s[^,\d“\"]{3,90},\s+(?=\w)")
_NEGATION_RE = re.compile(r"\b(?:not|never|no|none|neither|nor|without|denied|denies)\b|n't", re.I)
_ATTRIBUTION_WORDS_RE = re.compile(
    r"\b(?:said|says|told|according to|announced|stated|confirmed|wrote|claimed|claims|reported|alleged|"
    r"accused|argued|noted|added|explained)\b",
    re.I,
)
_QUOTE_SPAN_RE = re.compile(r"[“\"][^”\"]{4,}[”\"]")


@dataclass
class Composition:
    headline: str
    body: list[Candidate] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    second_reason: str | None = None
    considered: int = 0
    runner_up: str | None = None

    @property
    def body_text(self) -> str:
        return " ".join(c.text for c in self.body)

    @property
    def body_words(self) -> int:
        return word_count(self.body_text)

    @property
    def script_text(self) -> str:
        if not self.body:
            return ""
        return re.sub(r"\s+", " ", f"{end_sentence(self.headline)} {self.body_text}").strip()

    @property
    def roles(self) -> dict:
        return {
            "what_happened": self.body[0].text if self.body else None,
            "detail": self.body[1].text if len(self.body) > 1 else None,
        }


def _safe_compression(before: str, after: str) -> bool:
    """A compression must keep every attribution word, quote span and negation."""
    if not after.strip() or not (after[0].isupper() or after[0].isdigit() or after[0] in "“\"‘'"):
        return False
    if len(_ATTRIBUTION_WORDS_RE.findall(after)) < len(_ATTRIBUTION_WORDS_RE.findall(before)):
        return False
    if any(q not in after for q in _QUOTE_SPAN_RE.findall(before)):
        return False
    if len(_NEGATION_RE.findall(after)) != len(_NEGATION_RE.findall(before)):
        return False
    return True


def _compress(text: str, limit: int = MAX_BODY_WORDS) -> str:
    """Bring a too-long sentence within `limit` by deleting, in order: a number-free
    parenthetical, a trailing which/while clause (no digits, quotes, names or
    attribution), a number-free appositive after the subject. Returns the text
    unchanged when it already fits or when no safe deletion is enough."""
    if word_count(text) <= limit:
        return text
    out = text
    steps = (
        lambda t: _PAREN_RE.sub("", t),
        lambda t: _drop_trailing_clause(t),
        lambda t: _APPOSITIVE_RE.sub(r"\1 ", t, count=1),
    )
    for step in steps:
        candidate = re.sub(r"\s+", " ", step(out)).strip()
        if candidate != out and _safe_compression(text, candidate):
            out = candidate
        if word_count(out) <= limit:
            return out
    return text


def _drop_trailing_clause(text: str) -> str:
    m = _TRAILING_CLAUSE_RE.search(text)
    if not m:
        return text
    clause = m.group(0)
    if re.search(r"\d|[“\"]", clause) or re.search(r"\b[A-Z][a-z]+", clause[2:]) or _ATTRIBUTION_WORDS_RE.search(clause):
        return text
    return text[: m.start()].rstrip() + "."


def _redundant(cand: Candidate, chosen: list[Candidate]) -> bool:
    return any(overlap(cand.text, c.text) >= MAX_OVERLAP for c in chosen)


def _fitted(cand: Candidate) -> Candidate | None:
    """The candidate at <= MAX_BODY_WORDS (compressed if needed) whose captions fit the cue budget, else None."""
    text = _compress(cand.text)
    if word_count(text) > MAX_BODY_WORDS or not _caption_fits(text):
        return None
    return cand if text == cand.text else replace(cand, text=text)


NARRATION_SECONDS_PER_WORD = 0.41


def _caption_fits(text: str) -> bool:
    """Would the storyboard's own cue splitter keep every caption cue within its
    20-word / 8 s / 3-line budget at the usual narration pace? A long sentence with
    no clause boundary cannot be split, so it would overflow a single cue."""
    from app.content.storyboard_composer import (
        CAPTION_MAX_LINES, MAX_CUE_DURATION_SECONDS, MAX_WORDS_PER_CUE, _split_long_cue, _wrap_caption_text,
    )
    seconds = word_count(text) * NARRATION_SECONDS_PER_WORD
    return all(
        len(cue["text"].split()) <= MAX_WORDS_PER_CUE
        and cue["end"] - cue["start"] <= MAX_CUE_DURATION_SECONDS
        and len(_wrap_caption_text(cue["text"]).splitlines()) <= CAPTION_MAX_LINES
        for cue in _split_long_cue(text, 0.0, seconds)
    )


def _ranked(pool: list[Candidate]) -> list[Candidate]:
    return sorted(pool, key=lambda c: (-c.score, c.position))


def _needs_second(lead: Candidate) -> str | None:
    """Why a lone `lead` cannot be left to stand by itself, or None."""
    if not lead.entities and not re.search(r"\d", lead.text):
        return "the event sentence names no company, product or figure"
    return None


def compose(headline: str, candidates: list[Candidate], corroborating: list[Candidate] | None = None) -> Composition:
    comp = Composition(headline=headline)
    primary = [c for c in candidates if c.source_id == "primary" and c.cls == EVENT]
    extra = [c for c in (corroborating or []) if c.cls == EVENT]
    comp.considered = len(primary) + len(extra)
    by_pos = {c.position: c for c in primary}

    def pick_lead(pool: list[Candidate]) -> Candidate | None:
        for cand in _ranked(pool):
            if starts_with_pronoun(cand.text):
                continue
            fit = _fitted(cand)
            if fit:
                return fit
        return None

    lead = pick_lead(primary)
    second_reason = None
    chosen: list[Candidate] = []

    if lead is None:
        # A pronoun-led event is usable only when its immediate predecessor (itself an
        # event) supplies the antecedent and both fit together.
        for cand in _ranked(primary):
            prev = by_pos.get(cand.position - 1)
            fit, prev_fit = _fitted(cand), (_fitted(prev) if prev else None)
            if prev_fit and fit and starts_with_pronoun(cand.text) and prev_fit.words + fit.words <= ACCEPT_WORDS[1]:
                chosen = [prev_fit, fit]
                second_reason = "the event sentence refers back to the previous sentence"
                break
    else:
        chosen = [lead]
        reason = _needs_second(lead)
        if reason:
            for cand in _ranked(primary):
                fit = _fitted(cand)
                if (
                    fit and fit.position != lead.position and fit.entities and not _redundant(fit, chosen)
                    and not starts_with_pronoun(fit.text) and lead.words + fit.words <= ACCEPT_WORDS[1]
                ):
                    chosen.append(fit)
                    second_reason = reason
                    break

    # Corroboration only fills a story whose own source has no usable event sentence.
    if not chosen and extra:
        for cand in _ranked(extra):
            if starts_with_pronoun(cand.text):
                continue
            fit = _fitted(cand)
            if fit:
                chosen = [fit]
                break

    if chosen and corroborating:
        for cand in corroborating:
            if numeric_conflict(cand.text, chosen[0].text) and overlap(cand.text, chosen[0].text) >= 0.3:
                comp.conflicts.append({"source": cand.source_name, "sentence": cand.text})

    if chosen:
        ranked = [c for c in _ranked(primary + extra) if c.text not in {x.text for x in chosen}]
        comp.runner_up = ranked[0].text if ranked else None
    chosen.sort(key=lambda c: (c.source_id != "primary", c.position))
    for index, cand in enumerate(chosen):
        cand.roles = ["WHAT_HAPPENED"] if index == 0 else ["DETAIL"]
    comp.body = chosen
    comp.second_reason = second_reason if len(chosen) > 1 else None
    return comp


__all__ = ["Composition", "compose", "is_attributed", "IDEAL_WORDS", "ACCEPT_WORDS", "MAX_BODY_WORDS", "MAX_BODY"]
