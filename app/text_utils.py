"""Text hygiene for scraped/RSS text: HTML entities and wrongly decoded bytes."""
import re
from html import unescape

_ENTITY_RE = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")
# UTF-8 bytes read as cp1252/latin-1 show up as one of these lead characters
# (A-circumflex, A-tilde, a-circumflex) followed by a cp1252/latin-1 symbol.
_MOJIBAKE_RE = re.compile(
    "[ÂÃâ][-¿ŒœŠšŸŽž"
    "ƒˆ˜–-›€™]"
)


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 text that was decoded as cp1252/latin-1 (the right single
    quote showing up as a-circumflex + euro + trademark). Leaves the text
    alone unless the round trip decodes cleanly."""
    if not text or not _MOJIBAKE_RE.search(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            return text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return text


def clean_text(text: str | None) -> str | None:
    """Decode HTML entities (up to twice, for double-escaped feeds) and repair
    mojibake. Never alters wording."""
    if text is None:
        return None
    text = repair_mojibake(text)
    for _ in range(2):
        if not _ENTITY_RE.search(text):
            break
        text = unescape(text)
    return text


def response_text(response) -> str:
    """response.text, but a response with no declared charset is decoded as
    UTF-8 (requests would otherwise assume ISO-8859-1 and garble it)."""
    if "charset" in response.headers.get("content-type", "").lower():
        return response.text
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode(response.apparent_encoding or "utf-8", errors="replace")
