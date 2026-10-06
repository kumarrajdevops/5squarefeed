"""Deterministic, LLM-free, fully automated classification gate ("rules-v3").

Answers one question: is this relevant AI news worth considering for the candidate pool?
It does not decide the final line-up; ranking does that later. There is no human step here.

Disposition (binary):
    candidate  AI-related AND reads as a concrete development (an action, release, change,
               figure or reported event), with no non-news pattern
    reject     not AI-related, a non-news pattern (deal, event, tutorial, review), an
               opinion / explainer / question / personal-voice headline, or AI without any
               concrete development

Decision order in classify():
    1. non-news patterns (deal, stream, event, tutorial, review)            -> reject
    2. non-news *form* (opinion, roundup, personal voice, question, byline,
       poll)                                                                -> reject
    3. AI term in the title: development signal -> candidate, else reject
    4. AI term only in the summary: needs a verb or intent development in the title
       (a statement or a bare figure is not enough)
    5. everything else, including AI-adjacent technology (robots, chips, data
       centres) with no AI term                                             -> reject

A "development signal" is detected generically (see _development): an inflected action verb
from a verb-stem table, an intent construction ("plans to", "set to"), a modal + base verb
("will pay"), a concrete figure, a versioned product name, a Show HN project, or a reported
statement. Summaries are stripped of HTML before any matching.

Any change to a pattern or to the decision order must bump RULES_VERSION.
"""
import html
import re
from dataclasses import dataclass

from app.filters.ai_relevance import AI_KEYWORDS

RULES_VERSION = "rules-v3"

CANDIDATE = "candidate"
REJECT = "reject"

AI_CORE = "core"
AI_ADJACENT = "adj"
AI_NONE = "none"

CORE_EXTRA = [
    "chatgpt", "codex", "copilot", "apple intelligence", "genai", "gen ai", "model router", "openai",
    "qwen", "kimi", "grok", "perplexity", "muse", "inference engine", "fine-tuning", "fine-tuned",
    "open-weight", "open weight", "open weights", "agentic", "coding agent", "coding agents",
    "agent", "agents", "vibe-coded", "vibe coding", "chatbot", "chatbots", "machine intelligence",
    "ai-generated", "ai-powered", "superintelligence", "slop", "transformer", "embedding", "embeddings",
    "token", "tokens", "prompt", "prompts", "text-to-image", "diffusion model", "model weights",
    "siri", "alexa", "omni-model", "omni model",
]
# Adjacent terms are split: "strong" ones are AI infrastructure / applied-AI topics that can be
# news on their own; "weak" ones (chips, GPUs, open source...) are generic technology and only
# count when the summary also carries a core AI term.
ADJ_STRONG_TERMS = [
    "robot", "robots", "robotaxi", "robotaxis", "humanoid", "drone", "drones", "autonomous", "self-driving",
    "data center", "data centers", "datacenter", "datacenters", "surveillance", "smart glasses", "supercomputer",
    "facial recognition", "license plate", "reinforcement learning", "quantum",
]
ADJ_WEAK_TERMS = [
    "gpu", "gpus", "chip", "chips", "semiconductor", "tsmc", "ray-ban", "open source", "open-source",
    "show hn", "github",
]
ADJ_TERMS = ADJ_STRONG_TERMS + ADJ_WEAK_TERMS

# ----------------------------------------------------------------------------- non-news
DEAL_RE = re.compile(
    r"\b(deals?|discount(ed)?|price (drop|cut)|on sale|sale|lowest[- ]ever|all-time low|record-low|"
    r"\d+% off|\$\d+ off|£\d+ off|save up to|coupon|big deal days|prime day|black friday|cheaper than|"
    r"price hike|freebies?|msrp|gaming (pc|laptop|rig)s?|"
    r"(hits|falls|drops|crashes|sinks) to \$\d)\b", re.I)
STREAM_RE = re.compile(r"\b(free streams?|how to watch|live streams?|streams? online)\b", re.I)
EVENT_RE = re.compile(
    r"\b(register now|disrupt 2026|summit|webinar|lineup|judges|tickets?|conference pass|emtech|"
    r"what to expect during)\b", re.I)
REVIEW_RE = re.compile(
    r"\b(review\s*[:|(–-]|[:|(–-]\s*review\b|reviewed\b|\w+ review$|^review\b|i tested|i've tested|"
    r"i test|hands-on|vs\.?|versus|which .* is right|top \d|best .* (of|for)|\d+ best)", re.I)
TUT_RE = re.compile(
    r"^(how to|how do|how can|what are the|what is|are there|why is|here's why|here's how|"
    r"a (beginner|practical|complete)|guide to|\d+ (ways|tips|guidelines)|"
    r"computer vision:|tutorial)|\bhow to (build|use|turn|limit|disable|delete|install|set up|"
    r"fix|get|make)\b|\b(step-by-step|beginner)\b", re.I)

# ----------------------------------------------------------------------------- non-news form
OPIN_RE = re.compile(
    r"\b(opinion|editorial|column|essay|op-ed|we need to|why (we|i|you)|the guardian view|"
    r"quote of the day)\b", re.I)
# Softer cues: opinion only when the headline has no hard development verb or attribution
# ("X releases Y, which makes the case for Z" and "quits, says labs should ..." are reports).
WEAK_OPIN_RE = re.compile(r"\b(should|the case (for|against)|take on)\b", re.I)
LISTICLE_RE = re.compile(r"^\d+\s+(insights?|things|lessons|takeaways|reasons|ways|tips|trends|predictions)\b", re.I)
GERUND_START_RE = re.compile(
    r"^(building|creating|using|understanding|bringing|making|exploring|rethinking|getting started)\b", re.I)
TWO_SENTENCE_RE = re.compile(r"[a-z0-9\"”’)]\.\s+[A-Z]")
ROUNDUP_RE = re.compile(
    r"\b(podcast|newsletter|weekly|digest|roundup|this week|episode|on equity)\b|\[ainews\]", re.I)
PERSONAL_RE = re.compile(r"^(i |i've |my |we |our |quoting )", re.I)
# Headlines that ask rather than report, pundit/byline tails and evaluative language.
QUESTION_RE = re.compile(
    r"\?\s*(\||$)|^(why|what|who|how|is|are|can|could|should|does|do|did|will|would|has|have|when|"
    r"where|which|tell us)\b", re.I)
BYLINE_RE = re.compile(r"\|\s*(letters|[A-Z][\w'’.-]+(\s[A-Z][\w'’.-]+){1,3})\s*$")
SAYS_NAME_TAIL_RE = re.compile(r",\s*(says|warns|claims|argues)\s+[A-Z][\w'’.-]+(\s[A-Z][\w'’.-]+)?\s*$")
EVALUATIVE_RE = re.compile(
    r"\b(alarmism|parasites?|believers|hidden cost|the drama|all the drama|totally worth|worth it|"
    r"pissing off|myth|rant|love letter|stop worrying|reckoning|the end of|surprise,? surprise|dumber|"
    r"disaster|meltdown|doom loops?|almost impossible|left behind|apparently,)\b", re.I)
POLL_RE = re.compile(r"\b(poll|surveyed?|survey finds|percent of (americans|people|adults))\b", re.I)

# ----------------------------------------------------------------------------- development
# rules-v1/v2 vocabulary, kept verbatim so no previously accepted headline is lost.
LEGACY_DEV_RE = re.compile(
    r"\b(launch(es|ed)?|releas(es|ed)?|unveils?|announces?|introduc(es|ed)|adds?|adding|bans?|banned|"
    r"freez(es|ing)|froze|open[- ]sources?|ships?|rolls? out|raises?|acquires?|hires?|reports?|finds?|found|"
    r"sues?|arrest(ed|s)?|limit(s|ing)|halts?|cracks?|solves?|discovers?|detects?|tracking|tracks|"
    r"cuts?|kills?|blocks?|resigns?|rules?|ruled|testing|tests|new|beats?|outperforms?|"
    r"flagged|expands?|partners?|signs?|deploys?|deployed)\b", re.I)

# Stem table for the generalised detector. Each stem is expanded by _forms() into its
# -s / -ed / -ing forms (3rd-person present dominates headlines); the bare form is only
# accepted after a modal or "to" (_BASE_CONTEXT) because many of these double as nouns.
_ACTION_STEMS = """
announce introduce unveil launch release debut ship publish reveal showcase preview open expand
extend update upgrade improve integrate enable allow support offer provide give gift adopt deploy
roll sign license acquire buy purchase invest fund finance raise secure win land pay agree plan aim
target move restrict ban block limit require mandate approve reject deny authorize permit regulate
fine sue charge indict arrest investigate probe subpoena file settle shut halt pause freeze stop end
discontinue retire remove drop cut slash lower reduce boost double triple surge soar jump fall
plunge slump climb rise hit reach top surpass beat outperform overtake lead join leave resign quit
depart exit hire appoint name promote fire sack replace switch shift convert merge delay cancel
scrap postpone resume restart reopen revive relaunch rebrand rename revamp redesign overhaul rebuild
build create develop train test trial pilot find discover detect identify uncover expose leak breach
hack steal exploit crack solve prove demonstrate confirm admit allege accuse blame urge demand
endorse oppose slam condemn threaten bring turn let stick watermark advance power
unlock tackle handle deliver earmark pitch propose bet back push press hold run make_available
confirm commit pledge vow promise partner team expand grow scale lose gain beat tie sell spin
decommission replace shutter wind ban investigate sanction tariff fund bankroll back
change curb hand codesign co-design quash smuggle ask refuse abandon issue use stretch
""".split()
_IRREGULAR = (
    "gave given found froze frozen began begun sold bought built won lost led held left quit ran "
    "hit cut spent paid sank rose fell slid spun withdrew broke shut stole overtook sought fled slashed"
).split()


def _forms(stem: str) -> set[str]:
    stem = stem.replace("_", " ")
    out = {stem + "s", stem + "ing"}
    if stem.endswith("e"):
        out |= {stem + "d", stem[:-1] + "ing"}
    elif stem.endswith("y") and len(stem) > 1 and stem[-2] not in "aeiou":
        out |= {stem[:-1] + "ies", stem[:-1] + "ied"}
    elif stem.endswith(("s", "x", "z", "ch", "sh")):
        out |= {stem + "es", stem + "ed"}
    else:
        out.add(stem + "ed")
    if re.search(r"[^aeiou][aeiou][bdgmnprt]$", stem) and len(stem) <= 5:
        out |= {stem + stem[-1] + "ed", stem + stem[-1] + "ing"}
    return out


def _alt(words) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(set(words), key=len, reverse=True))


_STEMS = [s for s in _ACTION_STEMS if s != "make_available"]
_INFLECTED = set(_IRREGULAR)
for _s in _STEMS:
    _INFLECTED |= _forms(_s)
BARE_OK = ("co-design announce unveil introduce propose agree adopt deploy acquire invest approve reject restrict "
           "require enable allow provide confirm deny reveal urge demand sue hire quit resign abandon refuse").split()
INFLECTED_VERB_RE = re.compile(r"\b(" + _alt(_INFLECTED) + r"|makes?\s+available|rolls?\s+out|"
                               r"teams?\s+up|lays?\s+off|steps?\s+down|pulls?\s+out)\b", re.I)
BARE_VERB_RE = re.compile(r"\b(" + _alt(BARE_OK) + r")\b", re.I)
# "will pay", "to limit", "now supports": a modal/"to"/"now" makes the bare form a verb.
BASE_VERB_RE = re.compile(
    r"\b(will|would|can|could|may|might|must|won't|to|now|also|soon|first|set to|going to)\s+(not\s+)?("
    + _alt(_STEMS) + r")\b", re.I)
INTENT_RE = re.compile(
    r"\b(want|wants|wanted)\b|\b(plans?|aims?|seeks?|hopes?|intends?|looks?|moves?|vows?|pledges?|promises?|threatens?|"
    r"about|set|poised|ready|due|slated|likely|starts?|begins?|starting|beginning)\s+to\s+\w+", re.I)
# Reported statements ("X says ...") are news when the speaker is an organisation or a witness,
# weaker when it is a pundit; the opinion-form checks above remove the pundit shapes.
STATEMENT_RE = re.compile(
    r"\b(says|said|warns|warned|claims|claimed|argues|insists|admits|admitted|reports|explains|expects|"
    r"predicts|suggests|hints|plans|revealed|apologi[sz]es|responds|denies|rejects)\b", re.I)
FIGURE_RE = re.compile(
    r"(\$|£|€)\s?\d[\d.,]*\s?(billion|million|trillion|bn|[mbk])?\b|\b\d[\d.,]*\s?(billion|million|trillion)\b|"
    r"\b\d+(\.\d+)?\s?%|\b\d+x\b", re.I)
# A named model/product with a version number: "Mistral Large 4", "Qwen3.8", "GPT-5", "Rho-1".
PRODUCT_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+)?[ -]?v?\d+(?:\.\d+)*[A-Za-z]?\b")
SHOW_HN_RE = re.compile(r"^show hn\b", re.I)

# Statements that are really a person's take, even when phrased with "says".
PERSON_SAYS_RE = re.compile(r"^(?:[A-Za-z]+['’]s\s+)?[A-Z][a-z]+\s+[A-Z][a-z]+\s+(?i:says|warns|claims|argues)\b")


def _terms_re(terms) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b", re.I)


# Bare "AI" is matched case-sensitively so words like "said" or "fail" can never trigger it.
CORE_RE = _terms_re((AI_KEYWORDS - {"ai"}) | set(CORE_EXTRA))
BARE_AI_RE = re.compile(r"\bAI\b|\bA\.I\.(?!\w)")
ADJ_RE = _terms_re(ADJ_TERMS)


@dataclass(frozen=True)
class ClassificationResult:
    disposition: str          # candidate | reject
    ai_relatedness: str       # core | adj | none
    content_flag: str | None  # deal | event | tut | rev | opin | round | ask, or None
    reason: str
    version: str = RULES_VERSION
    development: str | None = None  # verb | intent | figure | product | project | statement, or None


def plain_text(text: str | None) -> str:
    """Summaries arrive as raw HTML; tags and URLs must never supply AI terms."""
    text = html.unescape(re.sub(r"<[^>]*>", " ", text or ""))
    return re.sub(r"https?://\S+", " ", text)


def _has_core(text: str) -> bool:
    return bool(CORE_RE.search(text)) or bool(BARE_AI_RE.search(text))


def _development(title: str) -> str | None:
    """The kind of concrete development the headline reports, or None."""
    if (LEGACY_DEV_RE.search(title) or INFLECTED_VERB_RE.search(title) or BASE_VERB_RE.search(title)
            or BARE_VERB_RE.search(title)):
        return "verb"
    if INTENT_RE.search(title):
        return "intent"
    if SHOW_HN_RE.search(title):
        return "project"
    if FIGURE_RE.search(title):
        return "figure"
    if PRODUCT_RE.search(title):
        return "product"
    if STATEMENT_RE.search(title) and not PERSON_SAYS_RE.search(title):
        return "statement"
    return None


_NONNEWS_REASON = {
    "deal": "deal or promotion pattern",
    "event": "event or conference pattern",
    "tut": "tutorial or how-to pattern",
    "rev": "product review or comparison pattern",
}
_FORM_REASON = {
    "opin": "opinion or personal-voice pattern",
    "round": "roundup, podcast or newsletter pattern",
    "ask": "question, byline or poll headline rather than a report",
}


def _reject(ai, flag, reason, development=None):
    return ClassificationResult(REJECT, ai, flag, "Rejected: " + reason, development=development)


def _candidate(ai, reason, development):
    return ClassificationResult(CANDIDATE, ai, None, "Candidate: " + reason, development=development)


def classify(title: str | None, summary: str | None = None) -> ClassificationResult:
    title = (title or "").strip()
    summary = plain_text(summary)

    core_t = _has_core(title)
    core_s = _has_core(summary)
    
    adj = bool(ADJ_RE.search(title + " " + summary))
    ai = AI_CORE if (core_t or core_s) else (AI_ADJACENT if adj else AI_NONE)
    dev = _development(title)

    nonnews = None
    if DEAL_RE.search(title) or STREAM_RE.search(title):
        nonnews = "deal"
    elif EVENT_RE.search(title + " " + summary):
        nonnews = "event"
    elif TUT_RE.search(title):
        nonnews = "tut"
    elif REVIEW_RE.search(title):
        nonnews = "rev"
    if nonnews:
        return _reject(ai, nonnews, _NONNEWS_REASON[nonnews])

    hard_dev = bool(LEGACY_DEV_RE.search(title)) or bool(STATEMENT_RE.search(title))
    form = None
    if (OPIN_RE.search(title) or PERSONAL_RE.search(title) or EVALUATIVE_RE.search(title)
            or (WEAK_OPIN_RE.search(title) and not hard_dev)):
        form = "opin"
    elif ROUNDUP_RE.search(title):
        form = "round"
    elif (QUESTION_RE.search(title) or BYLINE_RE.search(title) or SAYS_NAME_TAIL_RE.search(title)
          or POLL_RE.search(title) or LISTICLE_RE.search(title) or GERUND_START_RE.search(title)
          or TWO_SENTENCE_RE.search(title)):
        form = "ask"
    if form:
        if ai == AI_NONE:
            return _reject(ai, form, "no AI terms and " + _FORM_REASON[form])
        return _reject(ai, form, "AI-related but " + _FORM_REASON[form])

    if core_t:
        if dev:
            return _candidate(ai, f"AI term in title with a concrete development ({dev})", dev)
        return _reject(ai, None, "AI term in title but no concrete development")
    if core_s:
        if dev in ("verb", "intent"):
            return _candidate(ai, f"AI term in the summary with a development in the title ({dev})", dev)
        return _reject(ai, None, "AI term only in the summary and no development in the title")
    if adj:
        return _reject(ai, None, "adjacent technology term without an AI development")
    return _reject(ai, None, "no AI or adjacent technology terms")
