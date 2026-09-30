"""
Real third-party visual sourcing for a story card's central visual area.

Deterministic (no LLM, no image generation, no headless browser). Per
story, in priority order:

1. "photo": the source article's own lead image (og:image/twitter:image
   of the story's own URL) -- the publisher's image for that exact story.
2. "logo": when the story's title (or, if the title names no company,
   its company-blog source) names exactly ONE known company, that
   company's logo from Wikimedia Commons (which exposes license/trademark
   metadata we record verbatim).
3. "fallback": nothing safe/reliable was found -> the renderer draws its
   existing deterministic Pillow visual. Never an invented image.

A blocked/403/405 page is recorded and skipped, never fought (same
standing rule as app/content/article_extractor.py).

Every sourced asset is cached under media/story_assets/<story_id>/ with a
provenance.json (source URL, source name, filename, retrieval time, the
license text the source itself reports). Licensing is only ever RECORDED,
never asserted: `license_note` says what the source reports and that it
is unverified.
"""
import hashlib
import html
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image

APP_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = APP_ROOT / "media" / "story_assets"

REQUEST_TIMEOUT_SECONDS = 12
MAX_HTML_BYTES = 400_000
MAX_IMAGE_BYTES = 12_000_000
MIN_PHOTO_WIDTH, MIN_PHOTO_HEIGHT = 800, 400
UA_PAGE = "Mozilla/5.0 (compatible; AIDaily25/1.0; +https://github.com/) article-summary-fetcher"
UA_COMMONS = "5squareFeedBot/1.0 (https://5squarefeed.in; 5squarefeed@gmail.com) python-requests"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"
LICENSE_UNVERIFIED = (
    "Recorded as reported by the source; licensing/usage rights NOT verified -- "
    "review before publishing."
)

# entity -> (title/source regex, Commons file name, source-name regex for company blogs)
ENTITIES = {
    "OpenAI": (r"\bOpenAI\b|\bChatGPT\b|\bGPT[-‑ ]?\d", "OpenAI Logo.svg", r"^OpenAI$"),
    "Google": (r"\bGoogle\b|\bDeepMind\b|\bGemini\b", "Google 2015 logo.svg", r"^Google\b"),
    "Microsoft": (r"\bMicrosoft\b", "Microsoft logo (2012).svg", r"^Microsoft\b"),
    "NVIDIA": (r"\bNVIDIA\b|\bNvidia\b", "NVIDIA logo.svg", r"^NVIDIA\b"),
    "Anthropic": (r"\bAnthropic\b|\bClaude\b", "Anthropic logo.svg", r"^Anthropic\b"),
    "Meta": (r"\bMeta\b", "Meta Platforms Inc. logo.svg", r"^Meta\b"),
}

# A company logo beside a story about violence/litigation can read as an
# endorsement or accusation; such stories get the neutral fallback instead.
SENSITIVE_RE = re.compile(r"\b(lawsuits?|sues?|sued|shootings?|shooter|killed|deaths?|suicide|murders?|attacks?)\b", re.I)

_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z_:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def _http_get(url: str, *, ua: str = UA_PAGE, params: dict | None = None) -> requests.Response:
    return requests.get(url, headers={"User-Agent": ua}, params=params, timeout=REQUEST_TIMEOUT_SECONDS)


def detect_entity(title: str, source_name: str) -> str | None:
    """Exactly one named company in the title (else exactly-one company blog
    source when the title names none); ambiguity -> None (no logo)."""
    title = html.unescape(title or "")
    named = [name for name, (pat, _f, _s) in ENTITIES.items() if re.search(pat, title)]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        return None
    for name, (_p, _f, src_pat) in ENTITIES.items():
        if re.search(src_pat, source_name or ""):
            return name
    return None


def extract_image_urls(page_html: str, page_url: str) -> list[str]:
    found: dict[str, str] = {}
    for tag in _META_RE.findall(page_html[:MAX_HTML_BYTES]):
        attrs = {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and attrs.get("content"):
            found.setdefault(key, urljoin(page_url, attrs["content"].strip()))
    order = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
    urls: list[str] = []
    for k in order:
        if k in found and found[k] not in urls:
            urls.append(found[k])
    return urls


def _decode_ok(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


def _save_asset(story_dir: Path, img: Image.Image, stem: str) -> Path:
    story_dir.mkdir(parents=True, exist_ok=True)
    if img.mode in ("RGBA", "LA", "P"):
        out = story_dir / f"{stem}.png"
        img.convert("RGBA").save(out, "PNG")
    else:
        out = story_dir / f"{stem}.jpg"
        img.convert("RGB").save(out, "JPEG", quality=95)
    return out


def _used_hashes(exclude_story: int) -> set[str]:
    hashes = set()
    if ASSET_ROOT.exists():
        for prov in ASSET_ROOT.glob("*/provenance.json"):
            if prov.parent.name == str(exclude_story):
                continue
            try:
                h = json.loads(prov.read_text()).get("sha256")
            except Exception:
                continue
            if h:
                hashes.add(h)
    return hashes


def _try_photo(story_id: int, page_url: str, story_dir: Path) -> tuple[dict | None, str]:
    try:
        page = _http_get(page_url)
    except Exception as e:
        return None, f"page fetch error: {type(e).__name__}"
    if page.status_code != 200:
        return None, f"page fetch HTTP {page.status_code} (blocked/unavailable -- skipped, not fought)"
    candidates = extract_image_urls(page.text, page_url)
    if not candidates:
        return None, "no og:image/twitter:image on the article page"
    used = _used_hashes(story_id)
    why = "image failed quality gate"
    for img_url in candidates:
        try:
            resp = _http_get(img_url)
        except Exception as e:
            why = f"image fetch error: {type(e).__name__}"
            continue
        ctype = resp.headers.get("content-type", "").lower()
        if resp.status_code != 200 or not ctype.startswith("image/") or "svg" in ctype:
            why = f"image HTTP {resp.status_code} / content-type {ctype or '?'}"
            continue
        if len(resp.content) > MAX_IMAGE_BYTES:
            why = "image too large"
            continue
        img = _decode_ok(resp.content)
        if img is None:
            why = "image did not decode"
            continue
        w, h = img.size
        if w < MIN_PHOTO_WIDTH or h < MIN_PHOTO_HEIGHT or not (0.6 <= w / h <= 3.5):
            why = f"image {w}x{h} below quality gate"
            continue
        sha = hashlib.sha256(resp.content).hexdigest()
        if sha in used:
            why = "duplicate of another story's image"
            continue
        path = _save_asset(story_dir, img, "asset")
        return {
            "kind": "photo", "tier": "article_lead_image", "asset_file": path.name,
            "source_url": img_url, "page_url": page_url, "width": w, "height": h, "sha256": sha,
            "license_note": LICENSE_UNVERIFIED + " Publisher's own lead image for this article.",
        }, ""
    return None, why


def _try_logo(entity: str, story_dir: Path) -> tuple[dict | None, str]:
    fname = ENTITIES[entity][1]
    try:
        meta = _http_get(COMMONS_API, ua=UA_COMMONS, params={
            "action": "query", "titles": "File:" + fname, "prop": "imageinfo",
            "iiprop": "extmetadata|url", "format": "json"}).json()
        page = list(meta["query"]["pages"].values())[0]
        info = page["imageinfo"][0]
        em = info.get("extmetadata", {})
        resp = _http_get(COMMONS_FILEPATH + fname.replace(" ", "_"), ua=UA_COMMONS, params={"width": 1600})
    except Exception as e:
        return None, f"commons lookup error: {type(e).__name__}"
    if resp.status_code != 200:
        return None, f"commons image HTTP {resp.status_code}"
    img = _decode_ok(resp.content)
    if img is None:
        return None, "commons image did not decode"
    license_name = (em.get("LicenseShortName") or {}).get("value", "unknown")
    restrictions = (em.get("Restrictions") or {}).get("value", "")
    path = _save_asset(story_dir, img, "asset")
    note = f"Wikimedia Commons reports license: {license_name}"
    if restrictions:
        note += f"; restrictions: {restrictions} (trademark)"
    return {
        "kind": "logo", "tier": "entity_logo_commons", "asset_file": path.name,
        "source_url": info.get("descriptionurl") or info.get("url"), "page_url": None,
        "width": img.size[0], "height": img.size[1],
        "sha256": hashlib.sha256(resp.content).hexdigest(),
        "license_note": note + ". " + LICENSE_UNVERIFIED,
    }, ""


def _load_item(story_id: int) -> dict:
    from app.db import SessionLocal
    from app.models import NewsItem

    with SessionLocal() as db:
        item = db.get(NewsItem, story_id)
        if item is None:
            raise ValueError(f"story {story_id} not found")
        return {"title": item.title, "source_name": item.source_name, "url": item.canonical_url}


def ensure_story_visual(story_id: int, item: dict | None = None, force: bool = False) -> dict:
    """Never raises. Returns the provenance dict (kind photo|logo|fallback);
    reuses a valid cached result without touching the network."""
    story_dir = ASSET_ROOT / str(story_id)
    prov_path = story_dir / "provenance.json"
    if prov_path.exists() and not force:
        try:
            prov = json.loads(prov_path.read_text())
            if prov.get("kind") == "fallback" or (story_dir / prov.get("asset_file", "?")).exists():
                prov["cached"] = True
                return prov
        except Exception:
            pass

    try:
        item = item or _load_item(story_id)
    except Exception as e:
        return {"story_id": story_id, "kind": "fallback", "reason": f"story lookup failed: {e}"}

    title, source_name, url = item.get("title", ""), item.get("source_name", ""), item.get("url", "")
    entity = detect_entity(title, source_name)
    reasons: list[str] = []
    prov = None
    try:
        if url:
            prov, why = _try_photo(story_id, url, story_dir)
            if why:
                reasons.append(f"photo: {why}")
        if prov is None and SENSITIVE_RE.search(title or ""):
            reasons.append("logo: skipped -- sensitive story (litigation/violence); no logo association implied")
        elif prov is None and entity:
            prov, why = _try_logo(entity, story_dir)
            if why:
                reasons.append(f"logo: {why}")
        elif prov is None:
            reasons.append("logo: no single unambiguous company in title/source")
    except Exception as e:
        reasons.append(f"unexpected: {type(e).__name__}: {e}")

    if prov is None:
        prov = {"kind": "fallback", "tier": "deterministic_pillow", "reason": "; ".join(reasons)}
    prov.update({
        "story_id": story_id, "entity": entity, "story_title": html.unescape(title),
        "source_name": source_name, "story_url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if reasons and prov["kind"] != "fallback":
        prov["skipped_tiers"] = reasons
    story_dir.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(prov, indent=2, ensure_ascii=False))
    return prov


def asset_path(prov: dict) -> Path | None:
    if prov.get("kind") in ("photo", "logo") and prov.get("asset_file"):
        p = ASSET_ROOT / str(prov["story_id"]) / prov["asset_file"]
        return p if p.exists() else None
    return None


def cached_visual(story_id: int) -> dict | None:
    """Network-free lookup of an already-resolved visual (None if never
    resolved). Renderers use this so any render path picks up the same
    sourced asset once ensure_story_visual() has run for the story."""
    prov_path = ASSET_ROOT / str(story_id) / "provenance.json"
    if not prov_path.exists():
        return None
    try:
        prov = json.loads(prov_path.read_text())
    except Exception:
        return None
    if prov.get("kind") == "fallback" or asset_path(prov) is not None:
        return prov
    return None
