"""Deterministic, LLM-free classification gate ("rules-v2").

Decides whether a story enters the candidate pool, needs an editor's look, or is
rejected. Wired in via app/tasks/classify.py (the first processing stage).

Disposition:
    candidate  AI term in the title plus a development verb, no non-news pattern
    review     plausibly relevant but uncertain; a human decides
    reject     no AI/adjacent terms, or a clear non-news pattern (deal, event, tutorial, review)

Any change to a pattern or to the decision order must bump RULES_VERSION.
"""
import re
from dataclasses import dataclass

from app.filters.ai_relevance import AI_KEYWORDS

RULES_VERSION = "rules-v2"

CANDIDATE = "candidate"
REVIEW = "review"
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
ADJ_TERMS = [
    "robot", "robots", "robotaxi", "robotaxis", "humanoid", "drone", "drones", "autonomous", "self-driving",
    "data center", "data centers", "datacenter", "gpu", "gpus", "chip", "chips", "semiconductor", "tsmc",
    "surveillance", "smart glasses", "ray-ban", "open source", "open-source", "show hn", "github",
    "supercomputer", "facial recognition", "license plate", "quantum", "reinforcement learning",
]

DEAL_RE = re.compile(
    r"\b(deals?|discount(ed)?|price (drop|cut)|on sale|sale|lowest[- ]ever|all-time low|record-low|"
    r"\d+% off|\$\d+ off|£\d+ off|save up to|coupon|big deal days|prime day|black friday|cheaper than|"
    r"price hike|freebies?)\b", re.I)
STREAM_RE = re.compile(r"\b(free streams?|how to watch|live streams?|streams? online)\b", re.I)
EVENT_RE = re.compile(
    r"\b(register now|disrupt 2026|summit|webinar|lineup|judges|tickets?|conference pass|emtech|"
    r"what to expect during)\b", re.I)
REVIEW_RE = re.compile(
    r"\b(review|reviewed|i tested|i've tested|i test|hands-on|vs\.?|versus|which .* is right|"
    r"top \d|best .* (of|for)|\d+ best)\b", re.I)
TUT_RE = re.compile(
    r"^(how to|how do|how can|what are the|what is|are there|why is|here's why|here's how|"
    r"a (beginner|practical|complete)|guide to|\d+ (ways|tips|guidelines)|"
    r"computer vision:|tutorial)|\bhow to (build|use|turn|limit|disable|delete|install|set up|"
    r"fix|get|make)\b|\b(step-by-step|beginner)\b", re.I)
OPIN_RE = re.compile(
    r"\b(opinion|editorial|column|essay|op-ed|we need to|should|why (we|i|you)|the case for|"
    r"the guardian view)\b", re.I)
ROUNDUP_RE = re.compile(r"\b(podcast|newsletter|weekly|digest|roundup|this week|episode|on equity)\b", re.I)
PERSONAL_RE = re.compile(r"^(i |i've |my |we |our )", re.I)
DEV_RE = re.compile(
    r"\b(launch(es|ed)?|releas(es|ed)?|unveils?|announces?|introduc(es|ed)|adds?|adding|bans?|banned|"
    r"freez(es|ing)|froze|open[- ]sources?|ships?|rolls? out|raises?|acquires?|hires?|reports?|finds?|found|"
    r"sues?|arrest(ed|s)?|limit(s|ing)|halts?|cracks?|solves?|discovers?|detects?|tracking|tracks|"
    r"cuts?|kills?|blocks?|resigns?|rules?|ruled|testing|tests|new|beats?|outperforms?|"
    r"flagged|expands?|partners?|signs?|deploys?|deployed|"
    # rules-v2: event verbs seen in real headlines that v1 sent to review
    r"giv(es|ing)|gave|aims?|debuts?|delivers?|earmarks?|publishes|pitche[sd]|tackles?|removes?|"
    r"proposes?|confirms?|bets|lets|wins|loses|drops|pushes|quits?|changes|changed|hits|faces|"
    r"subpoenas?|subpoenaed|charges|charged|offers|runs|(re)?building|losing|holds|handles|"
    r"stick(s|ing)|watermark(s|ing)?)\b", re.I)


def _terms_re(terms) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b", re.I)


# Bare "AI" is matched case-sensitively so words like "said" or "fail" can never trigger it.
CORE_RE = _terms_re((AI_KEYWORDS - {"ai"}) | set(CORE_EXTRA))
BARE_AI_RE = re.compile(r"\bAI\b|\bA\.I\.(?!\w)")
ADJ_RE = _terms_re(ADJ_TERMS)


@dataclass(frozen=True)
class ClassificationResult:
    disposition: str          # candidate | review | reject
    ai_relatedness: str       # core | adj | none
    content_flag: str | None  # deal | event | tut | rev | opin | round, or None
    reason: str
    version: str = RULES_VERSION


def _has_core(text: str) -> bool:
    return bool(CORE_RE.search(text)) or bool(BARE_AI_RE.search(text))


_NONNEWS_REASON = {
    "deal": "deal or promotion pattern",
    "event": "event or conference pattern",
    "tut": "tutorial or how-to pattern",
    "rev": "product review or comparison pattern",
}
_SOFT_REASON = {"opin": "opinion or personal-voice pattern", "round": "roundup, podcast or newsletter pattern"}


def classify(title: str | None, summary: str | None = None) -> ClassificationResult:
    title = title or ""
    summary = summary or ""

    core_t = _has_core(title)
    core_s = _has_core(summary)
    adj = bool(ADJ_RE.search(title + " " + summary))
    dev = bool(DEV_RE.search(title))
    ai = AI_CORE if (core_t or core_s) else (AI_ADJACENT if adj else AI_NONE)

    nonnews = None
    if DEAL_RE.search(title) or STREAM_RE.search(title):
        nonnews = "deal"
    elif EVENT_RE.search(title + " " + summary):
        nonnews = "event"
    elif TUT_RE.search(title):
        nonnews = "tut"
    elif REVIEW_RE.search(title):
        nonnews = "rev"

    soft = None
    if OPIN_RE.search(title):
        soft = "opin"
    elif ROUNDUP_RE.search(title):
        soft = "round"
    elif PERSONAL_RE.search(title):
        soft = "opin"

    if nonnews:
        return ClassificationResult(REJECT, ai, nonnews, "Rejected: " + _NONNEWS_REASON[nonnews])
    if soft:
        if ai != AI_NONE:
            return ClassificationResult(REVIEW, ai, soft, "Review: AI-related but " + _SOFT_REASON[soft])
        return ClassificationResult(REJECT, ai, soft, "Rejected: no AI terms and " + _SOFT_REASON[soft])
    if core_t:
        if dev:
            return ClassificationResult(CANDIDATE, ai, None, "Candidate: AI term in title with a development verb")
        return ClassificationResult(REVIEW, ai, None, "Review: AI term in title but no development verb")
    if core_s:
        return ClassificationResult(REVIEW, ai, None, "Review: AI term only in the summary")
    if adj:
        return ClassificationResult(REVIEW, ai, None, "Review: adjacent technology term only")
    return ClassificationResult(REJECT, ai, None, "Rejected: no AI or adjacent technology terms")
