import re

# ---------------------------------------------------------
# Fact Extraction (project.md's "FACT EXTRACTION" box: claims, dates,
# companies, products, events) -- deterministic keyword/regex matching,
# same pattern and rationale as app/filters/ai_relevance.py: explainable,
# no LLM call, no API key. "Claims" specifically means the most
# concrete, checkable factual assertions a deterministic pass can
# actually pull out (dollar amounts, percentages, multipliers) rather
# than attempting free-form claim understanding, which needs an LLM
# this project deliberately doesn't use (see CLAUDE.md).
# ---------------------------------------------------------

KNOWN_COMPANIES = {
    "openai", "anthropic", "google", "google deepmind", "deepmind",
    "microsoft", "meta", "amazon", "aws", "nvidia", "apple", "xai",
    "mistral", "mistral ai", "hugging face", "ibm", "salesforce", "adobe",
    "tesla", "samsung", "intel", "qualcomm", "baidu", "alibaba", "tencent",
    "huawei", "cohere", "stability ai", "perplexity", "databricks",
    "scale ai", "character.ai", "inflection ai", "runway", "midjourney",
}

KNOWN_PRODUCTS = {
    "gpt-4", "gpt-4o", "gpt-5", "chatgpt", "claude", "gemini", "llama",
    "copilot", "dall-e", "sora", "midjourney", "stable diffusion", "bard",
    "bing chat", "gpt-3", "palm", "mistral", "mixtral", "grok", "deepseek",
    "qwen",
}

# Event *categories*, not free text -- each maps to a set of trigger
# words/phrases. A story can match more than one category.
EVENT_KEYWORDS: dict[str, set[str]] = {
    "launch": {
        "launches", "launch", "launched", "launching", "unveils",
        "unveiled", "unveiling", "releases", "released", "releasing",
        "debuts", "debuted", "debuting",
    },
    "funding": {
        "raises", "raised", "raising", "funding", "series a", "series b",
        "series c", "series d", "series e", "valuation", "investment",
    },
    "acquisition": {
        "acquires", "acquired", "acquiring", "acquisition", "buys",
        "buying", "bought", "merger",
    },
    "lawsuit_regulatory": {
        "lawsuit", "sues", "sued", "suing", "antitrust", "regulator",
        "ban", "banned", "fined", "investigation",
    },
    # Deliberately not bare "fine" -- confirmed live (during the
    # taxonomy redesign session) it false-positives on "fine-tuning"/
    # "fine-tuned": regex \b treats the hyphen as a word boundary, so
    # "fine" inside "fine-tuning" matches as a whole word even though
    # it has nothing to do with a regulatory fine. "fined" alone is
    # unambiguous and doesn't have this collision, so it stays.
    "research": {"paper", "study", "research", "benchmark"},
    "partnership": {
        "partners with", "partnered with", "partnership", "collaborates",
        "collaborated", "collaboration",
    },
    "personnel": {
        "hires", "hired", "hiring", "resigns", "resigned", "steps down",
        "stepped down", "appointed", "fired", "layoffs",
    },
    # Deliberately not the bare word "policy" -- confirmed via live data
    # this session that it false-positives on completely unrelated
    # stories (an eBPF access-control story matched on "enforcing a
    # policy (allow/deny)", nothing to do with AI safety/regulation).
    "safety_policy": {
        "safety", "ai policy", "ai regulation", "regulatory", "guardrails",
        "alignment",
    },
}

# Explicit, checkable dates only -- "Month Day[, Year]", ISO
# YYYY-MM-DD, and M/D/YYYY. Deliberately no fuzzy relative-date
# guessing ("yesterday", "next quarter") -- those aren't verifiable
# facts on their own.
_MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
DATE_RE = re.compile(
    rf"\b(?:{_MONTH_NAMES})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b"
    rf"|\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    rf"|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b",
    re.IGNORECASE,
)

# Dollar amounts, percentages, multipliers ("10x"), and large plain
# numbers with thousands separators -- the concrete, checkable numeric
# assertions in a news story.
NUMERIC_CLAIM_RE = re.compile(
    r"\$[\d,]+(?:\.\d+)?\s?(?:million|billion|trillion|M|B|T|K)?"
    r"|\b\d+(?:\.\d+)?%"
    r"|\b\d+(?:\.\d+)?x\b"
    r"|\b\d{1,3}(?:,\d{3})+\b",
    re.IGNORECASE,
)


def _find_keyword_matches(text: str, keywords: set[str]) -> list[str]:
    text = text.lower()
    matches = []
    for keyword in keywords:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, text):
            matches.append(keyword)
    return matches


def extract_companies(text: str) -> list[str]:
    return _find_keyword_matches(text, KNOWN_COMPANIES)


def extract_products(text: str) -> list[str]:
    return _find_keyword_matches(text, KNOWN_PRODUCTS)


def extract_events(text: str) -> list[str]:
    text_lower = text.lower()
    matched_categories = []
    for category, triggers in EVENT_KEYWORDS.items():
        for trigger in triggers:
            if re.search(r"\b" + re.escape(trigger) + r"\b", text_lower):
                matched_categories.append(category)
                break
    return matched_categories


def extract_dates(text: str) -> list[str]:
    return DATE_RE.findall(text)


def extract_numeric_claims(text: str) -> list[str]:
    return NUMERIC_CLAIM_RE.findall(text)


def extract_facts(title: str, summary: str | None) -> dict:
    """
    Run every extractor over the combined title + summary text.
    Returns a plain dict -- callers JSON-serialize it into
    Story.extracted_facts, same plain-text audit-trail style as
    filter_reason/dedup_reason/rank_reason elsewhere in this codebase.
    """

    combined_text = f"{title} {summary or ''}"

    return {
        "companies": extract_companies(combined_text),
        "products": extract_products(combined_text),
        "events": extract_events(combined_text),
        "dates": extract_dates(combined_text),
        "claims": extract_numeric_claims(combined_text),
    }
