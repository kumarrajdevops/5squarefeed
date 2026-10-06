"""Deterministic script quality gate. Information value, grounding and source
sufficiency carry the weight. Length is measured on the BODY (what happened, not
the headline) and only has an upper bound: there is no minimum, so a short, true
event statement is never failed for being short. A headline-only script (the
source adds no event fact) skips the body checks and is capped at review."""
import re
from dataclasses import dataclass, field

from app.content.briefing.events import EVENT, classify, headline_is_event
from app.content.briefing.facts import entities_in
from app.content.briefing.hygiene import TRUNCATION_RE, is_boilerplate
from app.content.support_facts import split_sentences
from app.content.briefing.relevance import IRRELEVANT, RELEVANT, UNKNOWN, WEAK, Relevance, body_alignment
from app.content.briefing.sufficiency import INSUFFICIENT, SUFFICIENT, THIN
from app.content.briefing.textutil import coverage, end_sentence, numbers, tokens, word_count

PASS = "pass"
REVIEW = "review"
FAIL = "fail"

PASS_SCORE = 0.8
REVIEW_SCORE = 0.6
IDEAL_BODY_WORDS = (15, 25)  # a preference, reported but never required
PASS_BODY_MAX = 30
MAX_BODY_WORDS = 35  # hard maximum
MAX_BODY_SENTENCES = 2
GROUNDING_MIN = 0.95
MAX_HEADLINE_OVERLAP = 0.6
MIN_NEW_WORDS = 4

WEIGHTS = {
    "information": 0.25,
    "grounding": 0.25,
    "sufficiency": 0.20,
    "headline_repetition": 0.10,
    "length": 0.10,
    "sentence_count": 0.05,
    "hygiene": 0.05,
}

_QUOTED_RE = re.compile(r"[“\"]([^”\"]{8,})[”\"]")


@dataclass
class QualityReport:
    status: str = FAIL
    score: float = 0.0
    words: int = 0
    body_words: int = 0
    sentences: int = 0
    chars: int = 0
    headline_only: bool = False
    checks: list[dict] = field(default_factory=list)
    components: dict = field(default_factory=dict)
    gates: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    distinct_facts: int = 0

    def as_dict(self) -> dict:
        return {
            "status": self.status, "score": self.score, "words": self.words, "body_words": self.body_words,
            "sentences": self.sentences, "chars": self.chars, "headline_only": self.headline_only,
            "checks": self.checks, "components": self.components,
            "gates": self.gates, "reasons": self.reasons, "distinct_facts": self.distinct_facts,
        }


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"check": name, "passed": bool(passed), "detail": detail}


def distinct_facts(text: str) -> int:
    facts = set(numbers(text))
    facts |= {q.lower() for q in _QUOTED_RE.findall(text)}
    facts |= set(entities_in(text))
    return len(facts)


def _length_score(body_words: int) -> float:
    if body_words <= PASS_BODY_MAX:
        return 1.0
    if body_words <= MAX_BODY_WORDS:
        return 0.6
    return 0.0


def _sentence_score(body_sentences: int) -> float:
    return 1.0 if body_sentences <= MAX_BODY_SENTENCES else 0.0


def evaluate(
    headline: str,
    script_text: str,
    source_texts: list[str],
    sufficiency_status: str,
    relevance: Relevance | None = None,
) -> QualityReport:
    """`source_texts` are the raw summary/content (and any corroborating
    articles) the script must be traceable to."""
    report = QualityReport()
    sentences = split_sentences(script_text) if script_text else []
    report.sentences = len(sentences)
    report.words = word_count(script_text)
    report.chars = len(script_text)
    head_sentences = len(split_sentences(end_sentence(headline))) if headline else 1
    body = sentences[head_sentences:]
    body_text = " ".join(body)
    report.body_words = word_count(body_text)
    headline_only = bool(script_text) and not body
    report.headline_only = headline_only
    source = " \n ".join(t for t in source_texts if t)
    checks = report.checks

    checks.append(_check("source_sufficient", sufficiency_status != INSUFFICIENT, f"source is {sufficiency_status}"))

    facts_total = distinct_facts(body_text)
    report.distinct_facts = facts_total
    head_tokens = tokens(headline)
    new_tokens = tokens(body_text) - head_tokens
    new_words = sum(1 for w in body_text.split() if tokens(w) and not (tokens(w) & head_tokens))
    body_cov = coverage(body_text, headline) if body_text else 0.0
    repeated = bool(body) and body_cov >= MAX_HEADLINE_OVERLAP
    adds_info = new_words >= MIN_NEW_WORDS
    non_events = [s for s in body if classify(s) != EVENT]
    ungrounded: list[str] = []

    if headline_only:
        checks.append(_check("headline_only", True, "the source adds no event fact beyond the headline"))
        checks.append(_check("headline_is_event", headline_is_event(headline), "headline states a development"))
    else:
        meaningful = bool(body) and facts_total >= 1
        checks.append(_check("meaningful_content", meaningful, f"{facts_total} distinct fact(s) in the body"))
        checks.append(_check("adds_information_beyond_headline", adds_info, f"{new_words} new body word(s), {len(new_tokens)} new concept(s)"))
        checks.append(_check("headline_repetition", not repeated, f"body shares {body_cov:.0%} of its content words with the headline"))
        checks.append(_check("body_is_news_event", not non_events, non_events[0][:90] if non_events else "every body sentence reports an event"))

        if report.body_words <= PASS_BODY_MAX:
            band = "ideal" if IDEAL_BODY_WORDS[0] <= report.body_words <= IDEAL_BODY_WORDS[1] else "short and complete"
            wc_detail, wc_ok = f"{report.body_words} body words ({band})", True
        elif report.body_words <= MAX_BODY_WORDS:
            wc_detail, wc_ok = f"{report.body_words} body words (above the 30-word target)", False
        else:
            wc_detail, wc_ok = f"{report.body_words} body words (over the 35-word maximum)", False
        checks.append(_check("word_count", wc_ok, wc_detail))
        checks.append(_check("sentence_count", len(body) <= MAX_BODY_SENTENCES, f"{len(body)} body sentence(s)"))

    boiler = [s for s in sentences if is_boilerplate(s)]
    checks.append(_check("boilerplate_detected", not boiler, boiler[0][:80] if boiler else "none"))

    trunc = [s for s in sentences if TRUNCATION_RE.search(s)]
    if sentences and sentences[-1][-1] not in ".!?”\"'’":
        trunc.append(sentences[-1])
    checks.append(_check("truncation_detected", not trunc, trunc[0][-60:] if trunc else "none"))

    source_numbers = numbers(source)
    for s in body:
        if coverage(s, source) < GROUNDING_MIN or (numbers(s) - source_numbers):
            ungrounded.append(s)
    checks.append(_check("unsupported_claims", not ungrounded, ungrounded[0][:90] if ungrounded else "every sentence traces to the source"))

    relevance_status = relevance.status if relevance else UNKNOWN
    checks.append(_check(
        "source_relevance", relevance_status in (RELEVANT, UNKNOWN),
        f"{relevance_status}" + (f": {relevance.reason}" if relevance and relevance.reason else ""),
    ))
    topic_hits, topic_terms = body_alignment(headline, body_text) if body_text else (0, 0)
    off_topic = bool(body_text) and topic_terms >= 2 and topic_hits == 0
    checks.append(_check(
        "body_on_topic", not off_topic,
        f"{topic_hits} of the headline's {topic_terms} term(s) appear in the narrated body",
    ))

    if not headline_only:
        checks.append(_check("factual_completeness", bool(body) and facts_total >= 1, f"{facts_total} distinct fact(s)"))

    grounded_frac = 1.0 if not body else 1 - len(ungrounded) / len(body)
    info = 1.0 if headline_only else 0.5 * min(1.0, new_words / 10) + 0.5 * min(1.0, facts_total / 2)
    suff = {SUFFICIENT: 1.0}.get(sufficiency_status, 0.6 if sufficiency_status != INSUFFICIENT else 0.0)
    rep = 1.0 if body_cov <= 0.3 else max(0.0, 1 - (body_cov - 0.3) / 0.3)
    components = {
        "information": round(info, 3),
        "grounding": round(grounded_frac, 3),
        "sufficiency": suff,
        "headline_repetition": round(rep, 3),
        "length": 1.0 if headline_only else _length_score(report.body_words),
        "sentence_count": _sentence_score(len(body)),
        "hygiene": 0.0 if (boiler or trunc) else 1.0,
    }
    report.components = components
    report.score = round(sum(WEIGHTS[k] * v for k, v in components.items()), 3)

    gates = report.gates
    if ungrounded:
        gates.append("unsupported claim")
    if boiler:
        gates.append("boilerplate in script")
    if trunc:
        gates.append("truncated sentence in script")
    if repeated:
        gates.append("body repeats the headline")
    if body and not adds_info:
        gates.append("adds almost nothing beyond the headline")
    if body and non_events:
        gates.append("body is not a news event")
    if report.body_words > MAX_BODY_WORDS:
        gates.append("body exceeds 35 words")
    if sufficiency_status == INSUFFICIENT:
        gates.append("insufficient source")
    if relevance_status == IRRELEVANT:
        gates.append("source does not match the story")
    if not script_text:
        gates.append("empty script")

    if gates or report.score < REVIEW_SCORE:
        report.status = FAIL
    elif (
        report.score >= PASS_SCORE
        and report.body_words <= PASS_BODY_MAX
        and not headline_only
        and sufficiency_status != THIN
        and relevance_status != WEAK
        and not off_topic
    ):
        report.status = PASS
    else:
        report.status = REVIEW

    for c in checks:
        if not c["passed"]:
            report.reasons.append(f'{c["check"]}: {c["detail"]}')
    return report
