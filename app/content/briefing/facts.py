"""Candidate sentences and the deterministic facts we can read out of them.
A candidate is a verbatim source sentence; nothing here paraphrases."""
import re
from dataclasses import dataclass, field

from app.content.briefing.events import EVENT, classify, has_background_inline, has_strong_event
from app.content.briefing.textutil import coverage, number_unit, numbers, tokens, word_count
from app.content.support_facts import _balanced_quotes
from app.extraction.fact_extractor import DATE_RE, extract_companies, extract_events, extract_products

MIN_WORDS = 6  # fragment guard only; there is no minimum body length
MAX_WORDS = 45

_QUOTE_RE = re.compile(r"[“\"][^”\"]{12,}[”\"]")
_EVENT_VERB_RE = re.compile(
    r"\b(?:announc|launch|releas|introduc|unveil|acqui|rais|cut|ban|sue|sued|fil(?:e|ed)|quit|resign|"
    r"fire|hire|lay(?:s|ed)? off|laid off|find|found|show|reveal|report|plan|expect|build|built|train|"
    r"open|updat|partner|deliver|support|allow|enabl|ship|reach|win|won|lose|lost|drop|rise|rose|grow|grew|"
    r"doubl|tripl|surpass|beat|approv|reject|block|warn|admit|confirm|deny|denied|agree|sign|settle|"
    r"invest|fund|buy|bought|sell|sold|pay|paid|charge|offer|add|remov|replac|expand|cancel|delay|test|"
    r"publish|discover|develop|creat|use|deploy|adopt|accus|alleg|claim|says|said|told|want|need|will|"
    r"begin|began|start|end|leave|left|join|name|appoint|replace|step|steps)\w*",
    re.I,
)
_WHY_RE = re.compile(
    r"\b(?:means|meaning|could|enables?|allows?|implications?|significant(?:ly)?|first[- ](?:ever|time)|largest|"
    r"biggest|record|raises? (?:concerns?|questions?)|marks the|critical|signals?|reshape|transform|threat|"
    r"risk|important|matters?)\b",
    re.I,
)
_CONTINUATION_START = re.compile(
    r"^(?:but|and|so|then|still|however|also|yet|or|nor|instead|otherwise|conversely|as a result|"
    r"that said|even so|because of this|for example|for instance|in addition|additionally|moreover|"
    r"furthermore|meanwhile|overall|altogether|in fact|indeed|similarly|likewise)\b",
    re.I,
)
_IMPERATIVE_START = re.compile(
    r"^(?:create|direct|generate|turn|bring|make|meet|start|take|scale|learn|try|get|explore|read|watch|"
    r"discover|build|use|join|see|check|note|click|download|visit|sign|follow|share|subscribe|listen)\b",
    re.I,
)
_META_RE = re.compile(
    r"\b(?:below|above|this article|this post|this story|read on|here(?:'s| is| are)|in this (?:article|post|guide))\b", re.I
)
_CODEISH_RE = re.compile(r"[→↔⇒|`]|^\s*[-*•]\s|[.!?]\s+[-*•]\s|https?:|\w+\.(?:py|js|ts|md|json)\b")
_FIRST_PERSON_RE = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|my|me|we|we're|we've|our|ours|us)\b")
_PRONOUN_START = re.compile(
    r"^(?:he|she|they|it|this(?! (?:week|month|year|morning|afternoon|evening|weekend|quarter)\b)|these|those|his|her|their|its|such|that|"
    r"the (?:company|firm|startup|study|paper|report|course|motivation|move|deal|agency|group|team|"
    r"researchers|authors?|app|tool|feature|platform|project|results?|decision|announcement|change|changes|"
    r"latter|former|same|latest|incident|case|move|plan|effort|initiative))\b",
    re.I,
)
_NOT_ENTITY = {"AI", "I", "The", "A", "An", "In", "On", "At", "It", "He", "She", "They", "We", "This", "That",
               "January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"}


def entities_in(sentence: str) -> list[str]:
    found = {c for c in extract_companies(sentence)} | {p for p in extract_products(sentence)}
    words = sentence.split()
    for word in words[1:]:
        clean = word.strip(".,;:!?()[]“”\"'’‘—-")
        if len(clean) > 1 and clean[0].isupper() and clean not in _NOT_ENTITY:
            found.add(clean.lower())
    return sorted(found)


def has_number_or_date(sentence: str) -> bool:
    return bool(re.search(r"\d", sentence)) or bool(DATE_RE.search(sentence))


def has_quote(sentence: str) -> bool:
    return bool(_QUOTE_RE.search(sentence))


def has_event(sentence: str) -> bool:
    return bool(extract_events(sentence)) or bool(_EVENT_VERB_RE.search(sentence))


def fact_points(sentence: str) -> int:
    """0-4: number/date, quote, a named entity, an event/claim verb."""
    return (
        int(has_number_or_date(sentence))
        + int(has_quote(sentence))
        + int(bool(entities_in(sentence)))
        + int(has_event(sentence))
    )


def significance_stated(sentence: str) -> bool:
    return bool(_WHY_RE.search(sentence))


def starts_with_pronoun(sentence: str) -> bool:
    return bool(_PRONOUN_START.match(sentence))


@dataclass
class Candidate:
    text: str
    position: int
    source_id: str = "primary"
    source_name: str | None = None
    url: str | None = None
    novelty: float = 1.0
    points: int = 0
    entities: list[str] = field(default_factory=list)
    score: float = 0.0
    roles: list[str] = field(default_factory=list)
    cls: str = "other"

    @property
    def words(self) -> int:
        return word_count(self.text)


def usable_sentence(sentence: str) -> bool:
    words = word_count(sentence)
    if not (MIN_WORDS <= words <= MAX_WORDS):
        return False
    if not (sentence[0].isupper() or sentence[0].isdigit() or sentence[0] in "“\"‘'"):
        return False
    if sentence[-1] not in ".!?”\"'’":
        return False
    if _CONTINUATION_START.match(sentence) or _IMPERATIVE_START.match(sentence) or _META_RE.search(sentence):
        return False
    if _CODEISH_RE.search(sentence):
        return False
    if not _balanced_quotes(sentence):
        return False
    letters = [c for c in sentence if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) > len(letters) * 0.35:
        return False
    return True


def score_candidate(c: Candidate, total_candidates: int) -> float:
    s = c.text
    score = 0.0
    if has_number_or_date(s):
        score += 2.0
    if has_quote(s):
        score += 1.0
    score += min(2, len(c.entities)) * 0.5
    if has_event(s):
        score += 1.5
    topical = 1.0 - c.novelty  # share of the sentence's content words that the headline also uses
    if topical < 0.05:
        score -= 1.0  # shares nothing with the headline
    elif topical <= 0.45:
        score += 2.0  # elaborates the headline: the most likely 'what happened'
    # above 0.45 it largely echoes the headline: no bonus
    score += max(0.0, 4.0 - c.position * 0.4)
    if _FIRST_PERSON_RE.search(_QUOTE_RE.sub("", s)):
        score -= 4.0
    if starts_with_pronoun(s):
        score -= 3.0
    if has_background_inline(s):
        score -= 2.0
    if c.cls == EVENT:
        score += 2.0
        if c.entities and has_strong_event(s):
            score += 1.0
    words = c.words
    if words <= 25:
        score += 0.5
    elif words > 35:
        score -= 1.0
    return round(score, 3)


def build_candidates(
    sentences: list[str],
    headline: str,
    *,
    source_id: str = "primary",
    source_name: str | None = None,
    url: str | None = None,
    start_position: int = 0,
) -> list[Candidate]:
    """Usable, informative sentences with their scores. A sentence with no
    concrete fact (number/date/quote/entity/event) is dropped: it would only pad."""
    kept: list[Candidate] = []
    for index, sentence in enumerate(sentences):
        if not usable_sentence(sentence):
            continue
        novelty = round(1.0 - coverage(sentence, headline), 3) if headline else 1.0
        if novelty < 0.35:
            continue
        points = fact_points(sentence)
        if points < 1:
            continue
        kept.append(Candidate(
            text=sentence, position=start_position + index, source_id=source_id,
            source_name=source_name, url=url, novelty=novelty, points=points,
            entities=entities_in(sentence), cls=classify(sentence),
        ))
    for cand in kept:
        cand.score = score_candidate(cand, len(kept))
    return kept


def numeric_conflict(a: str, b: str) -> bool:
    """True when two sentences state different values of the same measured unit
    (percent, dollar, multiplier). Plain numbers/years never conflict."""
    from app.content.briefing.textutil import numbers_by_unit
    na, nb = numbers_by_unit(a), numbers_by_unit(b)
    for unit in ("pct", "usd", "mult"):
        if na.get(unit) and nb.get(unit) and not (na[unit] & nb[unit]):
            return True
    return False


__all__ = [
    "Candidate", "build_candidates", "fact_points", "entities_in", "has_number_or_date", "has_quote",
    "has_event", "significance_stated", "starts_with_pronoun", "usable_sentence", "numeric_conflict",
    "number_unit", "numbers", "tokens",
]
