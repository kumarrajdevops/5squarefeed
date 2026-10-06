"""Shared, dependency-free text helpers for the briefing pipeline.
Everything here is deterministic; no LLM, no network."""
import re

STOPWORDS = frozenset(
    "a an the and or but if of to in on at for from by with as is are was were be been being it its "
    "this that these those has have had will would can could may might do does did not no than then "
    "so such into over under about after before while which who whom whose their there they them he "
    "she his her we our you your i also more most other some any each new said says say".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9](?:[a-z0-9.\-']*[a-z0-9])?")


def stem(token: str) -> str:
    token = token.replace("'s", "").strip(".-'")
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    return token


def tokens(text: str) -> set[str]:
    """Content tokens: lowercased, stopwords removed, plural-stemmed."""
    out = set()
    for raw in _TOKEN_RE.findall((text or "").lower()):
        tok = stem(raw)
        if len(tok) > 1 and tok not in STOPWORDS:
            out.add(tok)
    return out


def overlap(a: str, b: str) -> float:
    """Shared content tokens relative to the SHORTER text (0..1)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def coverage(sentence: str, reference: str) -> float:
    """Fraction of `sentence`'s content tokens that also occur in `reference`."""
    ts = tokens(sentence)
    if not ts:
        return 0.0
    return len(ts & tokens(reference)) / len(ts)


def word_count(text: str) -> int:
    return len((text or "").split())


NUMBER_RE = re.compile(
    r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b|(?:million|billion|trillion|thousand)\b|[mbkx]\b)?", re.I
)


def number_unit(raw: str) -> str:
    low = raw.lower()
    if "%" in low or "percent" in low:
        return "pct"
    if low.startswith("$"):
        return "usd"
    if low.rstrip().endswith("x"):
        return "mult"
    return "plain"


def normalize_number(raw: str) -> str:
    return re.sub(r"[,\s$]|percent", "", raw.lower()).replace("%", "")


def numbers_by_unit(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for match in NUMBER_RE.finditer(text or ""):
        raw = match.group(0).strip()
        if not raw:
            continue
        out.setdefault(number_unit(raw), set()).add(normalize_number(raw))
    return out


def numbers(text: str) -> set[str]:
    result: set[str] = set()
    for values in numbers_by_unit(text).values():
        result |= values
    return result


def end_sentence(text: str) -> str:
    text = text.rstrip()
    stripped = text.rstrip("”\"'’)]")
    return text if text and stripped and stripped[-1] in ".!?" else text + "."
