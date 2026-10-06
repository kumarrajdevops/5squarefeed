"""Compose-time cleaning of scraped article / RSS text. `raw_content` itself is
never rewritten (dedup hashes, support facts and the dashboard read it as-is);
this works on a copy and only ever DROPS text, never rewords it."""
import re
from dataclasses import dataclass, field
from html import unescape

from app.content.briefing.textutil import coverage, overlap, tokens, word_count
from app.content.support_facts import split_sentences
from app.text_utils import clean_text, repair_mojibake

BOILERPLATE_RE = re.compile(
    r"subscribe|newsletter|sign up|sign in|log in|login|create an account|cookie|all rights reserved|"
    r"click here|read more|read the full|continue reading|follow us|follow @|share this|share on|"
    r"advertisement|sponsored|privacy policy|terms of (?:use|service)|contact us|download the app|"
    r"appeared first on|image credit|photo:|photo by|credit:|getty images|updated:|©|"
    r"related (?:articles?|stories|posts|coverage)|you might also like|this (?:story|article) "
    r"(?:originally|first) appeared|skip to|play (?:now|video)|leave a comment|comments section|"
    r"requests? for comment|declined to comment|did not (?:immediately )?respond|didn't (?:immediately )?respond|"
    r"feel free to|reach out|get in touch|(?:registered )?trademarks? of|registered (?:trademark|mark)|copyright\s+(?:\(c\)\s*)?\d{4}|does not sponsor|licensed under|permission is hereby|"
    r"@\w+|https?://|www\.|\.com/|^\s*tags?:",
    re.I,
)

TRUNCATION_RE = re.compile(r"(?:\[\s*(?:\.\.\.|…)\s*\]|\(\s*(?:\.\.\.|…)\s*\)|\.\.\.|…)\s*$")

_BAD_CHARS_RE = re.compile(r"[�]|Ã|â€")
_TAG_BREAK_RE = re.compile(r"</(?:p|div|li|h[1-6]|tr|blockquote)>|<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class CleanedSource:
    sentences: list[str] = field(default_factory=list)
    truncated: bool = False
    promo_ratio: float = 0.0
    raw_words: int = 0
    removed: dict = field(default_factory=dict)

    @property
    def usable_words(self) -> int:
        return sum(word_count(s) for s in self.sentences)


def _to_plain(text: str) -> str:
    text = repair_mojibake(text or "")
    text = _TAG_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = clean_text(unescape(text)) or ""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def is_boilerplate(sentence: str) -> bool:
    return bool(BOILERPLATE_RE.search(sentence)) or bool(_BAD_CHARS_RE.search(sentence))


def clean_source(text: str | None, headline: str = "") -> CleanedSource:
    """Plain, de-duplicated, boilerplate-free sentences in source order."""
    result = CleanedSource(removed={"boilerplate": 0, "duplicate": 0, "headline_echo": 0, "truncated": 0, "heading": 0})
    plain = _to_plain(text or "")
    result.raw_words = word_count(plain)
    if not plain:
        return result

    lines = [ln.strip() for ln in plain.split("\n") if ln.strip()]
    # A trailing excerpt cut-off ("... [...]", "Read more") belongs to the last paragraph.
    if lines and TRUNCATION_RE.search(lines[-1]):
        result.truncated = True

    sentences: list[str] = []
    for line in lines:
        if word_count(line) <= 4 and not re.search(r"[.!?\"”]$", line):
            result.removed["heading"] += 1
            continue
        sentences.extend(split_sentences(line))

    total = len(sentences) or 1
    promo = 0
    seen: list[str] = []
    for index, sentence in enumerate(sentences):
        sentence = sentence.strip()
        if TRUNCATION_RE.search(sentence):
            result.truncated = True
            result.removed["truncated"] += 1
            continue
        if is_boilerplate(sentence):
            promo += 1
            result.removed["boilerplate"] += 1
            continue
        if headline and (coverage(sentence, headline) >= 0.8 and overlap(sentence, headline) >= 0.8):
            result.removed["headline_echo"] += 1
            continue
        if any(len(tokens(sentence)) >= 5 and overlap(sentence, prior) >= 0.85 and coverage(sentence, prior) >= 0.85 for prior in seen):
            result.removed["duplicate"] += 1
            continue
        seen.append(sentence)
        result.sentences.append(sentence)

    result.promo_ratio = round(promo / total, 3)
    return result
