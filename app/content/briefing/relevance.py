"""Does the extracted source describe the story the headline names?

Alignment only -- this says nothing about whether the source is TRUE. The
headline's distinctive terms (entities, products, versions, topic words) are
looked for in the cleaned source text, with light stemming, hyphen/space
normalisation and a small company->product and terminology alias table, so
"Alphabet's Gemini" can satisfy "Google" and "sues" can satisfy "lawsuit".
Pure and deterministic; thresholds are constants so they can be calibrated."""
import re
from dataclasses import dataclass, field

from app.content.briefing.hygiene import _to_plain
from app.content.briefing.textutil import STOPWORDS

RELEVANT = "relevant"
WEAK = "weak_relevance"
IRRELEVANT = "irrelevant"
UNKNOWN = "unknown"

RELEVANT_MIN = 0.45
IRRELEVANT_MAX = 0.2
MIN_MATCHES = 2
MIN_ANCHOR_WEIGHT = 3
MIN_SOURCE_WORDS = 8
SPECIFIC_WEIGHT = 2

# Title words that carry no topic: post framing, announcement verbs, filler.
GENERIC = frozenset(
    "show hn ask launch tell new latest first best top using use used make made making build built "
    "building update updates now will just get gets got why how what when where who ai ml artificial "
    "intelligence tech technology announces announced announce unveils unveiled releases released "
    "release introduces introduced introducing launches launched says said report reports reported "
    "reportedly week day today year more less most many people company companies one two three big "
    "major could would should may might can still also over after before into out up down "
    "like way ways thing things need needs back us vs".split()
)

# A company named in a headline is satisfied by its own products in the text,
# never the other way round (a headline about "Gemini 3" is not about Google Maps).
COMPANY_ALIASES = {
    "openai": {"chatgpt", "gpt", "sora", "codex", "altman"},
    "google": {"alphabet", "gemini", "deepmind", "android", "pixel", "youtube", "waymo"},
    "alphabet": {"google", "gemini", "deepmind", "waymo"},
    "meta": {"facebook", "instagram", "whatsapp", "llama", "zuckerberg"},
    "microsoft": {"copilot", "azure", "windows", "bing", "github", "xbox"},
    "anthropic": {"claude"},
    "nvidia": {"cuda", "geforce", "blackwell", "hopper"},
    "apple": {"iphone", "siri", "ipad", "macos", "ios"},
    "amazon": {"aws", "alexa", "kindle", "bedrock"},
    "tesla": {"autopilot", "optimus"},
    "deepmind": {"google", "alphabet", "gemini"},
}

# Interchangeable wording for the same idea (symmetric).
TERM_GROUPS = [
    {"lawsuit", "sue", "sued", "suing", "suit", "litigation", "court", "lawsuits", "complaint"},
    {"acquire", "acquisition", "acquires", "acquired", "buy", "buys", "bought", "purchase", "takeover", "deal"},
    {"funding", "raise", "raises", "raised", "round", "investment", "invest", "invests", "valuation", "financing"},
    {"layoff", "layoffs", "cuts", "cut", "firing", "fires", "redundancies", "jobs"},
    {"ban", "bans", "banned", "prohibit", "prohibits", "outlaw", "forbid", "blocks", "blocked"},
    {"hack", "hacked", "breach", "breached", "cyberattack", "attack", "leak", "leaked", "intrusion", "exploit"},
    {"chip", "chips", "processor", "semiconductor", "silicon", "gpu", "accelerator"},
    {"llm", "llms", "language", "model", "models"},
    {"regulation", "regulate", "regulator", "regulators", "law", "legislation", "rules", "bill", "act"},
    {"quit", "quits", "resign", "resigns", "resigned", "resignation", "leaves", "left", "departure", "steps"},
    {"agent", "agents", "agentic", "autonomous", "assistant"},
    {"robot", "robots", "robotic", "robotics", "humanoid"},
    {"lobby", "lobbies", "lobbying", "lobbied", "persuade", "pitch", "advocate"},
    {"consciousness", "conscious", "sentient", "sentience", "awareness"},
]

_WORD_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?![A-Za-z])|[A-Za-z0-9][A-Za-z0-9.'’\-]*[A-Za-z0-9]|[A-Za-z0-9]")
_PREFIX_RE = re.compile(r"^\s*(?:show hn|ask hn|launch hn|tell hn)\s*[:\-–—]\s*", re.I)
_SUFFIXES = ("ing", "ed", "es", "ly", "s")


@dataclass
class Relevance:
    status: str = UNKNOWN
    score: float = 0.0
    anchors: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    missing_specific: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status, "score": round(self.score, 3), "anchors": self.anchors,
            "matched": self.matched, "missing": self.missing,
            "missing_specific": self.missing_specific, "reason": self.reason,
        }


def _light_stem(word: str) -> str:
    word = word.lower().replace("’", "'").replace("'s", "").strip(".-'")
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _flat(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _title_case_headline(words: list[str]) -> bool:
    longer = [w for w in words if len(w) > 3 and w[0].isalpha()]
    return len(longer) >= 4 and sum(w[0].isupper() for w in longer) / len(longer) > 0.6


def _anchors(headline: str) -> list[tuple[str, int]]:
    """(term, weight) pairs; weight 2 for entity-like terms, 1 for topic words."""
    text = _PREFIX_RE.sub("", headline or "")
    words = _WORD_RE.findall(text)
    title_case = _title_case_headline(words)
    out: dict[str, int] = {}
    for index, raw in enumerate(words):
        low = raw.lower().replace("’", "'")
        if "'" in low:
            low = low.split("'")[0]
        if low in STOPWORDS or low in GENERIC or len(low) < 2 or low.replace(",", "").replace(".", "").isdigit():
            continue
        specific = (
            any(ch.isdigit() for ch in raw)
            or (len(raw) > 1 and raw.isupper())
            or any(ch.isupper() for ch in raw[1:])
            or (raw[0].isupper() and index > 0 and not title_case)
        )
        out[low] = max(out.get(low, 0), SPECIFIC_WEIGHT if specific else 1)
    return list(out.items())


def _source_forms(text: str) -> tuple[set[str], set[str], str]:
    raw_words = [w.lower().replace("’", "'") for w in _WORD_RE.findall(text or "")]
    forms: set[str] = set()
    for w in raw_words:
        w = w.split("'")[0] if "'" in w else w
        forms.add(w)
        forms.add(_light_stem(w))
        forms.add(_flat(w))
        for part in re.split(r"[\-.]", w):
            if len(part) > 1:
                forms.add(part)
                forms.add(_light_stem(part))
    flat_text = _flat(text or "")
    stems = {f for f in forms if len(f) >= 5}
    return forms, stems, flat_text


def _alternatives(term: str) -> set[str]:
    alts = {term, _flat(term)} | COMPANY_ALIASES.get(term, set())
    for group in TERM_GROUPS:
        if term in group or _light_stem(term) in group:
            alts |= group
    return alts | {_light_stem(a) for a in alts}


def _term_present(term: str, forms: set[str], stems: set[str], flat_text: str) -> bool:
    alts = _alternatives(term)
    if alts & forms:
        return True
    flat = _flat(term)
    if len(flat) >= 4 and re.search(r"[\-.]", term) and flat in flat_text:
        return True
    parts = [p for p in re.split(r"[\-.]", term) if len(p) > 1]
    if len(parts) > 1 and all(_alternatives(p) & forms for p in parts):
        return True
    if any(ch.isdigit() for ch in term):
        return False  # versions/models must match exactly: Atlas-3 is not Atlas-2
    stem = _light_stem(term)
    if len(stem) >= 6:
        prefix = stem[:6]
        return any(s.startswith(prefix) for s in stems)
    return False


def assess_relevance(headline: str, source_text: str | None) -> Relevance:
    """Compare the headline's distinctive terms against the cleaned source."""
    result = Relevance()
    anchors = _anchors(headline)
    result.anchors = [a for a, _ in anchors]
    total = sum(w for _, w in anchors)
    if total < MIN_ANCHOR_WEIGHT:
        result.reason = "headline has too few distinctive terms to compare"
        return result
    if len((source_text or "").split()) < MIN_SOURCE_WORDS:
        result.reason = "source text is too short to compare"
        return result

    forms, stems, flat_text = _source_forms(source_text)
    matched_weight = 0
    for term, weight in anchors:
        if _term_present(term, forms, stems, flat_text):
            matched_weight += weight
            result.matched.append(term)
        else:
            result.missing.append(term)
            if weight >= SPECIFIC_WEIGHT:
                result.missing_specific.append(term)

    result.score = matched_weight / total
    specific = [t for t, w in anchors if w >= SPECIFIC_WEIGHT]
    specific_hit = len(specific) - len(result.missing_specific)
    matches = len(result.matched)

    if matches == 0 or result.score <= IRRELEVANT_MAX or (specific and specific_hit == 0 and result.score < RELEVANT_MIN):
        result.status = IRRELEVANT
        result.reason = f"source does not mention the headline's subject (missing: {', '.join(result.missing[:5])})"
    elif (
        result.score >= RELEVANT_MIN
        and matches >= MIN_MATCHES
        and not (specific and specific_hit / len(specific) < 0.5)
        and not any(any(ch.isdigit() for ch in t) for t in result.missing_specific)
    ):
        result.status = RELEVANT
        result.reason = f"source covers {result.score:.0%} of the headline's terms"
    else:
        result.status = WEAK
        missing = ", ".join(result.missing_specific or result.missing)[:80]
        result.reason = f"source matches only part of the headline (missing: {missing})"
    return result


def relevance_source_text(raw_summary: str | None, raw_content: str | None) -> str:
    """Everything retrieved for the story, as plain text. Deliberately NOT the
    boilerplate-stripped sentences: a line dropped as boilerplate ("LEGO(c)")
    can still be what proves the page is about the right subject."""
    return "\n".join(t for t in (_to_plain(raw_content or ""), _to_plain(raw_summary or "")) if t)


def body_alignment(headline: str, body_text: str) -> tuple[int, int]:
    """(headline terms found anywhere in the narrated body, headline terms).
    Per-sentence checking is too noisy -- good sentences refer back with
    pronouns -- so only the body as a whole is compared."""
    terms = [t for t, _ in _anchors(headline)]
    forms, stems, flat_text = _source_forms(body_text)
    return sum(1 for t in terms if _term_present(t, forms, stems, flat_text)), len(terms)
