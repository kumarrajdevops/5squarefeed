import html
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from app.content import brand_assets, visual_assets


# Rendered natively at 2560x1440 (exactly 4/3 the pixel count of the
# 1920x1080 final output, still 16:9). Every layout constant below is a
# multiple of 4 so it maps to an integer pixel at 1080p (x 0.75).
SCENE_WIDTH = 2560
SCENE_HEIGHT = 1440

# Legacy dark palette (statistic/comparison panels, intro-independent
# gap clip, caption text colour).
BACKGROUND_COLOR = (12, 16, 28)
TEXT_COLOR = (240, 240, 245)
SOURCE_COLOR = (150, 160, 180)
DIVIDER_COLOR = (45, 52, 70)

# 5squareFeed story-card template palette.
NAVY = (10, 16, 42)
NAVY_PANEL_A = (12, 20, 52)
NAVY_PANEL_B = (26, 38, 92)
INK = (22, 32, 64)
INK_MUTED = (74, 86, 120)
CYAN = (5, 216, 252)
CHIP_A, CHIP_B = (37, 99, 235), (124, 58, 237)
CARD_BORDER = (218, 224, 240)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

CONTENT_MARGIN_X = 80

# Card geometry (canvas px). The visual window is the template's rounded
# central visual; the navy headline card overlaps its lower part; the
# light support card sits under it; the caption bar (burned by ASS) lives
# below CAPTION_SAFE_TOP.
MARK_BOX = (80, 32, 112, 112)             # x, y, w, h
WINDOW = (80, 160, 2400, 752)             # visual window
HEADLINE = (104, 624, 2352, 276)          # navy headline card
SUPPORT = (80, 936, 2400, 184)            # light quote/fact + source card
CAPTION_SAFE_TOP = 1123
LOGO_BADGE_SIZE = MARK_BOX[2]
DEFAULT_ACCENT_COLOR = (64, 156, 255)

HEADLINE_FONT_SIZE = 66
HEADLINE_LINE_HEIGHT = 80
HEADLINE_MAX_LINES = 3
SUPPORT_FONT_SIZE = 40
SUPPORT_LINE_HEIGHT = 54
SUPPORT_MAX_LINES = 2
STAT_FONT_SIZE = 260
VISIBLE_CENTER_Y = 232                    # centre of the un-overlapped window area


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Deterministic greedy word-wrap: add words while they fit max_width, else start a new line."""
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def _fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    """Wrap to at most max_lines at a FIXED font size (never shrinks); overflow is ellipsised."""
    lines = _wrap_text(draw, text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    words = kept[-1].split()
    while words and draw.textlength(" ".join(words) + "…", font=font) > max_width:
        words.pop()
    kept[-1] = (" ".join(words) + "…") if words else "…"
    return kept


def _accent_color(storyboard: dict) -> tuple:
    accent = storyboard.get("accent_color")
    return tuple(accent) if accent else DEFAULT_ACCENT_COLOR


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), BACKGROUND_COLOR)
    return image, ImageDraw.Draw(image)


# ---------------------------------------------------------------------
# Deterministic emphasis (shared with the caption renderer)
# ---------------------------------------------------------------------
def build_emphasis_set(scene: dict, storyboard: dict) -> set:
    tokens = set()
    for key in ("stat", "tier", "date", "source", "entity", "company", "product"):
        v = scene.get(key)
        if v:
            tokens.add(str(v).lower())
    if storyboard.get("source_name"):
        tokens.add(storyboard["source_name"].lower())
    return tokens


def is_emphasized(word: str, emphasis_set: set) -> bool:
    clean = word.strip(".,;:()\"'—").strip()
    if not clean:
        return False
    if any(ch.isdigit() for ch in clean):
        return True
    return clean.lower() in emphasis_set


_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _headline_accent(word: str, emphasis_set: set) -> bool:
    """Selective, deterministic accent: numbers, named tokens from the scene,
    brand-style words (CamelCase like OpenAI/DeepMind, ALLCAPS >= 3 letters)."""
    if is_emphasized(word, emphasis_set):
        return True
    clean = re.sub(r"[^A-Za-z0-9]", "", word)
    if len(clean) >= 3 and clean.isalpha() and clean.isupper():
        return True
    return bool(_CAMEL_RE.search(clean))


# ---------------------------------------------------------------------
# Drawing primitives (anti-aliased rounded shapes)
# ---------------------------------------------------------------------
def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    scale = 3
    big = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, width * scale - 1, height * scale - 1], radius=radius * scale, fill=255)
    return big.resize((width, height), Image.LANCZOS)


def _rrect(target: Image.Image, box: tuple, radius: int, fill: tuple, outline: tuple | None = None, outline_width: int = 0) -> None:
    x, y, w, h = box
    if outline:
        target.paste(outline, (x, y), _rounded_mask(w, h, radius))
        inner = _rounded_mask(w - 2 * outline_width, h - 2 * outline_width, max(1, radius - outline_width))
        target.paste(fill, (x + outline_width, y + outline_width), inner)
    else:
        target.paste(fill, (x, y), _rounded_mask(w, h, radius))


def _horizontal_gradient(width: int, height: int, left: tuple, right: tuple) -> Image.Image:
    strip = Image.new("RGB", (2, 1))
    strip.putpixel((0, 0), left)
    strip.putpixel((1, 0), right)
    return strip.resize((width, height), Image.BICUBIC)


def _diagonal_gradient(width: int, height: int, a: tuple, b: tuple) -> Image.Image:
    tiny = Image.new("RGB", (2, 2))
    tiny.putpixel((0, 0), a)
    tiny.putpixel((1, 0), tuple((x + y) // 2 for x, y in zip(a, b)))
    tiny.putpixel((0, 1), tuple((x + y) // 2 for x, y in zip(a, b)))
    tiny.putpixel((1, 1), b)
    return tiny.resize((width, height), Image.BICUBIC)


@lru_cache(maxsize=2)
def _soft_background_cached(vivid: bool) -> Image.Image:
    if not vivid:
        tiny = Image.new("RGB", (2, 2))
        tiny.putpixel((0, 0), (236, 242, 255))
        tiny.putpixel((1, 0), (242, 236, 255))
        tiny.putpixel((0, 1), (255, 243, 232))
        tiny.putpixel((1, 1), (236, 243, 255))
        return tiny.resize((SCENE_WIDTH, SCENE_HEIGHT), Image.BICUBIC)

    base = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), (250, 251, 255))
    blobs = Image.new("RGBA", (SCENE_WIDTH, SCENE_HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(blobs)
    for box, color in (
        ((-500, -300, 900, 700), (150, 205, 255, 235)),
        ((1500, -500, 3000, 600), (198, 168, 255, 235)),
        ((1900, 700, 3200, 1800), (255, 200, 150, 235)),
        ((-600, 800, 700, 1900), (150, 225, 255, 210)),
        ((900, 1200, 1700, 1800), (255, 214, 232, 150)),
    ):
        d.ellipse(box, fill=color)
    blobs = blobs.filter(ImageFilter.GaussianBlur(190))
    base.paste(blobs, (0, 0), blobs)
    return base


def soft_background(vivid: bool = False) -> Image.Image:
    """Pastel light ground behind the story card (vivid=True: intro/outro)."""
    return _soft_background_cached(vivid).copy()


def _text_center(draw: ImageDraw.ImageDraw, box: tuple, text: str, font: ImageFont.FreeTypeFont, fill: tuple) -> None:
    x, y, w, h = box
    draw.text((x + w / 2, y + h / 2), text, font=font, fill=fill, anchor="mm")


# ---------------------------------------------------------------------
# Card chrome: mark, NEWS chip, NN / total counter
# ---------------------------------------------------------------------
def _episode_position(storyboard: dict) -> tuple[int, int] | None:
    pos = storyboard.get("_episode_position")
    if pos and len(pos) == 2 and pos[0] and pos[1]:
        return int(pos[0]), int(pos[1])
    return None


def _draw_chrome(overlay: Image.Image, draw: ImageDraw.ImageDraw, storyboard: dict) -> None:
    mark_path = brand_assets.get_card_mark_path()
    if mark_path.exists():
        mark = Image.open(mark_path).convert("RGBA").resize((MARK_BOX[2], MARK_BOX[3]), Image.LANCZOS)
        overlay.alpha_composite(mark, (MARK_BOX[0], MARK_BOX[1]))

    chip_w = 200
    x = SCENE_WIDTH - CONTENT_MARGIN_X - chip_w
    news_box = (x, 34, chip_w, 56)
    overlay.paste(_horizontal_gradient(chip_w, 56, CHIP_A, CHIP_B), (news_box[0], news_box[1]), _rounded_mask(chip_w, 56, 14))
    _text_center(draw, news_box, "NEWS", ImageFont.truetype(FONT_BOLD, 34), (255, 255, 255))

    position = _episode_position(storyboard)
    if position:
        counter_box = (x, 96, chip_w, 50)
        _rrect(overlay, counter_box, 14, (255, 255, 255), outline=CARD_BORDER, outline_width=2)
        _text_center(draw, counter_box, f"{position[0]:02d} / {position[1]:02d}", ImageFont.truetype(FONT_BOLD, 32), INK)


# ---------------------------------------------------------------------
# Headline (navy) and support (light) cards
# ---------------------------------------------------------------------
def _text_layer(size: tuple, items: list) -> Image.Image:
    """Straight-alpha RGBA layer of anti-aliased text. items: (xy, text,
    font, rgb, anchor). One solid colour per mask, so edges never carry a
    dark fringe when the layer is later overlaid."""
    masks: dict = {}
    for xy, text, font, color, anchor in items:
        mask = masks.setdefault(color, Image.new("L", size, 0))
        ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    for color, mask in masks.items():
        solid = Image.new("RGBA", size, color + (255,))
        solid.putalpha(mask)
        layer.alpha_composite(solid)
    return layer


HEADLINE_LIGHT_LUMA = 150            # mean grey behind the headline above which the text goes dark
HEADLINE_LIGHT_ACCENT = (20, 90, 220)
HEADLINE_DARK_HALO = (6, 10, 30)


def _headline_halo(text_img: Image.Image, halo: tuple) -> Image.Image:
    """Boxless legibility: a thin crisp outline in the contrast colour (no glow)."""
    alpha = text_img.getchannel("A")
    outline = alpha.filter(ImageFilter.MaxFilter(7)).point(lambda v: min(255, v * 2))
    out = Image.new("RGBA", text_img.size, halo + (0,))
    out.putalpha(outline)
    out.alpha_composite(text_img)
    return out


def _window_is_light(window: Image.Image) -> bool:
    x, y, w, h = HEADLINE
    region = window.convert("L").crop((x - WINDOW[0], y - WINDOW[1], x - WINDOW[0] + w, y - WINDOW[1] + h))
    return ImageStat.Stat(region).mean[0] > HEADLINE_LIGHT_LUMA


@lru_cache(maxsize=64)
def _story_window_is_light(story_id: int | None, kind: str, path: str) -> bool:
    return _window_is_light(story_visual_window({"story_id": story_id})[0])


def _scene_headline_is_light(scene: dict, storyboard: dict) -> bool:
    """Dark text on light backdrops (white photos, logo plates), white text otherwise.
    Data scenes (stat / comparison / source card) paint navy windows."""
    if not uses_story_visual(scene):
        return False
    kind, path = _story_visual(storyboard)
    return _story_window_is_light(storyboard.get("story_id"), kind, str(path))


def _headline_layer(text: str, emphasis: set, light: bool = False) -> Image.Image:
    _x, _y, w, h = HEADLINE
    pad = 56
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    font = ImageFont.truetype(FONT_BOLD, HEADLINE_FONT_SIZE)
    lines = _fit_lines(probe, text, font, w - 2 * pad, HEADLINE_MAX_LINES)
    top = (h - HEADLINE_LINE_HEIGHT * len(lines)) // 2
    space = probe.textlength(" ", font=font)
    items = []
    for i, line in enumerate(lines):
        cx = pad
        cy = top + i * HEADLINE_LINE_HEIGHT + HEADLINE_LINE_HEIGHT / 2
        for word in line.split():
            accent = _headline_accent(word, emphasis)
            color = (HEADLINE_LIGHT_ACCENT if accent else NAVY) if light else (CYAN if accent else (255, 255, 255))
            items.append(((cx, cy), word, font, color, "lm"))
            cx += probe.textlength(word, font=font) + space
    return _headline_halo(_text_layer((w, h), items), (255, 255, 255) if light else HEADLINE_DARK_HALO)


def _support_layer(text: str, quoted: bool) -> Image.Image:
    _x, _y, w, h = SUPPORT
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    font = ImageFont.truetype(FONT_REGULAR, SUPPORT_FONT_SIZE)
    if not text:
        label = ImageFont.truetype(FONT_BOLD, 34)
        return _text_layer((w, h), [((48, h / 2), "SOURCE", label, INK_MUTED, "lm")])
    body = f"“{text}”" if quoted else text
    lines = _fit_lines(probe, body, font, SUPPORT_LEFT_W, SUPPORT_MAX_LINES)
    top = (h - SUPPORT_LINE_HEIGHT * len(lines)) // 2
    items = [((48, top + i * SUPPORT_LINE_HEIGHT + SUPPORT_LINE_HEIGHT / 2), line, font, INK, "lm") for i, line in enumerate(lines)]
    return _text_layer((w, h), items)


def _draw_support_bg(overlay: Image.Image, source_name: str) -> None:
    x, y, w, h = SUPPORT
    shadow = Image.new("RGBA", (w + 160, h + 160), (0, 0, 0, 0))
    shadow.paste((30, 40, 90, 60), (80, 92), _rounded_mask(w, h, 40))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    overlay.alpha_composite(shadow, (x - 80, y - 80))
    _rrect(overlay, SUPPORT, 40, (255, 255, 255), outline=CARD_BORDER, outline_width=3)
    if source_name:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        attr_font = ImageFont.truetype(FONT_BOLD, 34)
        lines = _fit_lines(probe, f"— {source_name} ↗", attr_font, 640, 2)
        top = (h - 46 * len(lines)) // 2
        items = [((w - 48, top + i * 46 + 23), line, attr_font, INK_MUTED, "rm") for i, line in enumerate(lines)]
        overlay.alpha_composite(_text_layer((w, h), items), (x, y))


# ---------------------------------------------------------------------
# Text sources for the two cards -- always real, never invented
# ---------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(text or "").lower()).strip()


def _story_title(storyboard: dict) -> str:
    return html.unescape(storyboard.get("title") or "").strip()


_DANGLING_WORDS = {"a", "an", "and", "or", "the", "to", "of", "in", "on", "at", "for", "with", "by", "as", "but", "than", "that"}


def _is_usable_entity(entity: str) -> bool:
    """A statistic's entity label is extracted text; a dangling fragment such
    as "To date and" must not become the headline (falls back to the title)."""
    words = entity.split()
    return bool(words) and words[-1].strip(".,;:").lower() not in _DANGLING_WORDS


def _fits_headline(text: str, max_lines: int = 2) -> bool:
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    font = ImageFont.truetype(FONT_BOLD, HEADLINE_FONT_SIZE)
    return len(_wrap_text(probe, text, font, HEADLINE[2] - 2 * 56)) <= max_lines


def _headline_text(scene: dict, storyboard: dict) -> str:
    text = _stored_headline_text(scene, storyboard)
    if text.endswith("…"):
        title = _story_title(storyboard)
        if title and not title.endswith("…") and _fits_headline(title):
            return title
    return text


def _stored_headline_text(scene: dict, storyboard: dict) -> str:
    scene_type = scene.get("scene_type")
    if scene_type == "hero":
        text = _story_title(storyboard) or scene.get("kicker")
    elif scene_type == "statistic":
        entity = scene.get("entity") if scene.get("stat") else None
        if entity and not _is_usable_entity(entity):
            entity = _story_title(storyboard)
        text = entity or scene.get("headline")
    elif scene_type == "comparison":
        text = scene.get("header") or _story_title(storyboard)
    elif scene_type == "quote":
        text = scene.get("quote_text") or scene.get("headline")
    elif scene_type == "source_card":
        text = scene.get("closing_line")
    else:
        text = scene.get("headline") or scene.get("quote_text") or scene.get("kicker")
    return html.unescape(text or scene.get("narration_text") or _story_title(storyboard)).strip().strip('"')


SUPPORT_LEFT_W = SUPPORT[2] - 48 - 40 - 640 - 24     # text column beside the source attribution


def _fits_support(text: str) -> bool:
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    font = ImageFont.truetype(FONT_REGULAR, SUPPORT_FONT_SIZE)
    return len(_wrap_text(probe, text, font, SUPPORT_LEFT_W)) <= SUPPORT_MAX_LINES


def _support_text(scene: dict, storyboard: dict, headline: str) -> tuple[str, bool]:
    """(text, quoted). Only stored source data, in fixed priority order:
    statistic/comparison keep their own real context fields; otherwise a
    verbatim article sentence, the source's one-sentence summary, the
    article's own title (quoted), the publication date. Each candidate must
    fit in two lines untruncated and must not repeat the headline; "" means
    only the source attribution shows."""
    scene_type = scene.get("scene_type")
    if scene_type == "statistic" and scene.get("stat"):
        parts = [p for p in (scene.get("tier"), scene.get("date"), scene.get("source")) if p]
        if parts:
            return "  ·  ".join(parts), False
    if scene_type == "comparison":
        parts = []
        for side in (scene.get("left"), scene.get("right")):
            if side:
                bits = [b for b in (side.get("entity"), side.get("date"), side.get("source")) if b]
                if bits:
                    parts.append(" · ".join(bits))
        if parts:
            return "   |   ".join(parts), False

    from app.content.support_facts import _overlap

    info = storyboard.get("_support") or {}
    title = _story_title(storyboard)
    candidates = [(info.get("article_fact"), False), (info.get("summary"), False), (title, False), (info.get("published"), False)]
    head = headline.rstrip("…")
    for text, quoted in candidates:
        text = html.unescape(text or "").strip()
        if not text or _overlap(text, head) > 0.6:
            continue
        if _fits_support(f"“{text}”" if quoted else text):
            return text, quoted
    return "", False


# ---------------------------------------------------------------------
# The visual window: real third-party asset, or a deterministic fallback
# ---------------------------------------------------------------------
def _story_visual(storyboard: dict) -> tuple[str, Path | None]:
    story_id = storyboard.get("story_id")
    if story_id is None:
        return "fallback", None
    prov = visual_assets.cached_visual(int(story_id))
    if not prov:
        return "fallback", None
    path = visual_assets.asset_path(prov)
    return (prov["kind"], path) if path else ("fallback", None)


def _cover_crop(img: Image.Image, width: int, height: int, focus_y: float = 0.38) -> Image.Image:
    """Uniform scale to cover the box, then crop -- never a non-uniform stretch."""
    scale = max(width / img.width, height / img.height)
    new_w, new_h = max(width, round(img.width * scale)), max(height, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = round((new_h - height) * focus_y)
    return resized.crop((left, top, left + width, top + height))


def _window_photo(path: Path) -> Image.Image:
    """The whole photo at full window height (never cropped or stretched) on a
    blurred backdrop; the boxless headline text sits over its lower part."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img.convert("RGBA"))
        img = bg
    img = img.convert("RGB")
    fit_h = WINDOW[3]
    fit_w = round(img.width * fit_h / img.height)
    backdrop = _cover_crop(img, WINDOW[2], WINDOW[3], focus_y=0.38).filter(ImageFilter.GaussianBlur(40))
    backdrop = Image.blend(backdrop, Image.new("RGB", backdrop.size, NAVY), 0.25)
    backdrop.paste(img.resize((fit_w, fit_h), Image.LANCZOS), ((WINDOW[2] - fit_w) // 2, 0))
    return backdrop


def _window_logo(path: Path) -> Image.Image:
    plate = _diagonal_gradient(WINDOW[2], WINDOW[3], (232, 240, 255), (246, 236, 255))
    logo = Image.open(path).convert("RGBA")
    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)
    scale = min(1300 / logo.width, 300 / logo.height)
    logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
    plate = plate.convert("RGBA")
    plate.alpha_composite(logo, ((WINDOW[2] - logo.width) // 2, VISIBLE_CENTER_Y - logo.height // 2))
    return plate.convert("RGB")


def _window_navy_panel() -> Image.Image:
    panel = _diagonal_gradient(WINDOW[2], WINDOW[3], NAVY_PANEL_A, NAVY_PANEL_B)
    grid = ImageDraw.Draw(panel)
    for gx in range(0, WINDOW[2], 160):
        grid.line([(gx, 0), (gx, WINDOW[3])], fill=(24, 34, 80), width=2)
    for gy in range(0, WINDOW[3], 160):
        grid.line([(0, gy), (WINDOW[2], gy)], fill=(24, 34, 80), width=2)
    return panel


def _window_fallback(storyboard: dict) -> Image.Image:
    """Deterministic Pillow fallback when no safe real asset exists: navy
    panel + the publisher's own name. Never an invented image."""
    panel = _window_navy_panel()
    draw = ImageDraw.Draw(panel)
    name = html.unescape(storyboard.get("source_name") or "")
    font = ImageFont.truetype(FONT_BOLD, 120)
    lines = _fit_lines(draw, name, font, WINDOW[2] - 400, 2)
    top = VISIBLE_CENTER_Y - (140 * len(lines)) // 2
    for i, line in enumerate(lines):
        draw.text((WINDOW[2] / 2, top + i * 140 + 70), line, font=font, fill=(255, 255, 255), anchor="mm")
    draw.rectangle([WINDOW[2] // 2 - 120, top + 140 * len(lines) + 20, WINDOW[2] // 2 + 120, top + 140 * len(lines) + 28], fill=CYAN)
    return panel


def story_visual_window(storyboard: dict) -> tuple[Image.Image, str]:
    kind, path = _story_visual(storyboard)
    try:
        if kind == "photo" and path:
            return _window_photo(path), "photo"
        if kind == "logo" and path:
            return _window_logo(path), "logo"
    except Exception:
        pass
    return _window_fallback(storyboard), "fallback"


# ---------------------------------------------------------------------
# Card composition
# ---------------------------------------------------------------------
_WINDOW_ONLY = False


@contextmanager
def window_only():
    """Inside this context every scene renderer writes just the visual
    window (2400x752 RGB PNG) to its output path -- the card chrome and text
    are composited separately by the episode renderer, so a crossfade can
    blend the imagery without ghosting the headline text."""
    global _WINDOW_ONLY
    previous, _WINDOW_ONLY = _WINDOW_ONLY, True
    try:
        yield
    finally:
        _WINDOW_ONLY = previous


def _base_card(storyboard: dict) -> Image.Image:
    """Everything on the card that is constant for a story: soft ground,
    mark, NEWS chip, counter, support plate + attribution (no headline box),
    with a transparent hole where the visual window shows through."""
    overlay = soft_background().convert("RGBA")
    _draw_chrome(overlay, ImageDraw.Draw(overlay), storyboard)
    hole = Image.new("L", overlay.size, 255)
    hole.paste(ImageChops.invert(_rounded_mask(WINDOW[2], WINDOW[3], 48)), (WINDOW[0], WINDOW[1]))
    overlay.putalpha(hole)
    _draw_support_bg(overlay, html.unescape(storyboard.get("source_name") or ""))
    return overlay


def _compose_card(
    scene: dict,
    storyboard: dict,
    window: Image.Image | None,
    output_path: Path,
    *,
    window_out: Path | None = None,
) -> None:
    """window given + window_out None: flat opaque PNG. window_out given:
    output_path is an RGBA card with a transparent rounded visual window
    and the window image is written to window_out (for a layered,
    animated visual under a static card). Inside window_only(): just the
    window image is written to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _WINDOW_ONLY:
        window.convert("RGB").save(output_path, "PNG")
        return
    layered = window_out is not None
    overlay = _base_card(storyboard)
    if not layered:
        flat = soft_background().convert("RGBA")
        flat.paste(window.convert("RGB"), (WINDOW[0], WINDOW[1]), _rounded_mask(WINDOW[2], WINDOW[3], 48))
        flat.alpha_composite(overlay)
        overlay = flat
    else:
        window.save(window_out, "PNG")

    headline = _headline_text(scene, storyboard)
    support, quoted = _support_text(scene, storyboard, headline)
    overlay.alpha_composite(
        _headline_layer(headline, build_emphasis_set(scene, storyboard), _window_is_light(window)), (HEADLINE[0], HEADLINE[1])
    )
    overlay.alpha_composite(_support_layer(support, quoted), (SUPPORT[0], SUPPORT[1]))
    if layered:
        overlay.save(output_path, "PNG")
    else:
        overlay.convert("RGB").save(output_path, "PNG")


def render_card_parts(scene: dict, storyboard: dict, out_dir: Path, scale: float, base_path: Path) -> dict:
    """Constant base card + this scene's headline/support text layers, all
    pre-scaled to output resolution (straight-alpha PNGs) with their output
    positions. The episode renderer overlays them so text is never part of
    a crossfaded frame."""
    out_dir.mkdir(parents=True, exist_ok=True)
    headline = _headline_text(scene, storyboard)
    support, quoted = _support_text(scene, storyboard, headline)

    def scaled(img: Image.Image) -> Image.Image:
        return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)

    head_path, supp_path = out_dir / "headline.png", out_dir / "support.png"
    if not base_path.exists():
        scaled(_base_card(storyboard)).save(base_path, "PNG")
    light = _scene_headline_is_light(scene, storyboard)
    scaled(_headline_layer(headline, build_emphasis_set(scene, storyboard), light)).save(head_path, "PNG")
    scaled(_support_layer(support, quoted)).save(supp_path, "PNG")
    return {
        "base": base_path,
        "headline": head_path, "headline_xy": (round(HEADLINE[0] * scale), round(HEADLINE[1] * scale)),
        "support": supp_path, "support_xy": (round(SUPPORT[0] * scale), round(SUPPORT[1] * scale)),
        "key": (headline, support, quoted),
    }


def uses_story_visual(scene: dict) -> bool:
    """True when this scene's visual window shows the story's real asset
    (and is therefore eligible for the layered, animated-window render)."""
    scene_type = scene.get("scene_type")
    if scene_type in ("comparison", "source_card"):
        return False
    if scene_type == "statistic":
        return not scene.get("stat")
    if scene_type == "key_fact" and scene.get("stages"):
        return False
    return True


def render_scene_layers(scene: dict, storyboard: dict, card_path: Path, window_path: Path) -> str | None:
    """Layered render for a story-visual scene: returns the visual kind
    (photo|logo|fallback), or None if the scene isn't layered-eligible."""
    if not uses_story_visual(scene):
        return None
    window, kind = story_visual_window(storyboard)
    _compose_card(scene, storyboard, window, card_path, window_out=window_path)
    return kind


# ---------------------------------------------------------------------
# Per-scene-type painters for the window (statistic / comparison / key fact)
# ---------------------------------------------------------------------
def _progress_stat(stat: str | None, progress: float) -> str | None:
    """
    Renders the intermediate value a count-up animation shows partway
    through -- e.g. 49 at progress=1.0, ~29 at progress=0.6. Falls
    back to the final value verbatim if it isn't a plain number (the
    animation is a nice-to-have; the correct final value must always
    show).
    """
    if not stat:
        return stat
    try:
        final_value = float(stat)
    except ValueError:
        return stat
    if progress >= 1.0:
        return stat
    return str(int(round(final_value * max(0.0, progress))))


def _render_story_card(scene: dict, storyboard: dict, output_path: Path) -> None:
    """hero / quote / concept / company / product / takeaway / headline-mode
    statistic / key_fact without stages: shared card, real asset in the window."""
    window, _kind = story_visual_window(storyboard)
    _compose_card(scene, storyboard, window, output_path)


def _render_statistic_scene(scene: dict, storyboard: dict, output_path: Path, progress: float = 1.0) -> None:
    stat, unit = _progress_stat(scene.get("stat"), progress), scene.get("unit") or ""
    if not scene.get("stat"):
        _render_story_card(scene, storyboard, output_path)
        return

    window = _window_navy_panel()
    draw = ImageDraw.Draw(window)
    stat_text = f"{stat}{unit}"
    size = STAT_FONT_SIZE
    font = ImageFont.truetype(FONT_BOLD, size)
    while draw.textlength(stat_text, font=font) > WINDOW[2] - 300 and size > 120:
        size -= 20
        font = ImageFont.truetype(FONT_BOLD, size)
    draw.text((WINDOW[2] / 2, VISIBLE_CENTER_Y - 10), stat_text, font=font, fill=(255, 255, 255), anchor="mm")
    draw.rectangle([WINDOW[2] // 2 - 110, VISIBLE_CENTER_Y + 150, WINDOW[2] // 2 + 110, VISIBLE_CENTER_Y + 158], fill=CYAN)
    _compose_card(scene, storyboard, window, output_path)


_ICON_SIZE = 130


def _shape_kwargs(color: tuple, outline: bool) -> dict:
    return {"outline": color, "width": 6} if outline else {"fill": color}


def _draw_vehicle_silhouette(draw: ImageDraw.ImageDraw, center_x: int, center_y: int, size: int, color: tuple, outline: bool = False) -> None:
    """A generic vehicle CATEGORY marker -- body + cabin + wheels, pure geometry, no manufacturer/model implied."""
    kwargs = _shape_kwargs(color, outline)
    body_w, body_h = size, int(size * 0.42)
    body_x0, body_y0 = center_x - body_w // 2, center_y - body_h // 2
    body_x1, body_y1 = body_x0 + body_w, body_y0 + body_h
    draw.rounded_rectangle([body_x0, body_y0, body_x1, body_y1], radius=body_h // 3, **kwargs)

    cabin_w, cabin_h = int(body_w * 0.5), int(body_h * 0.8)
    cabin_x0 = center_x - cabin_w // 2
    cabin_y0 = body_y0 - cabin_h + 10
    draw.polygon(
        [
            (cabin_x0, body_y0 + 6),
            (cabin_x0 + int(cabin_w * 0.2), cabin_y0),
            (cabin_x0 + int(cabin_w * 0.8), cabin_y0),
            (cabin_x0 + cabin_w, body_y0 + 6),
        ],
        **kwargs,
    )

    wheel_r = int(body_h * 0.32)
    wheel_y = body_y1 - wheel_r // 2
    for wheel_x in (body_x0 + wheel_r, body_x1 - wheel_r):
        draw.ellipse([wheel_x - wheel_r, wheel_y - wheel_r, wheel_x + wheel_r, wheel_y + wheel_r], **kwargs)


def _draw_robot_silhouette(draw: ImageDraw.ImageDraw, center_x: int, center_y: int, size: int, color: tuple, outline: bool = False) -> None:
    """A generic robot CATEGORY marker -- head/eyes + torso + arms, pure geometry, no specific model implied."""
    kwargs = _shape_kwargs(color, outline)

    head_size = int(size * 0.42)
    head_x0 = center_x - head_size // 2
    head_y0 = center_y - int(size * 0.55)
    draw.rounded_rectangle([head_x0, head_y0, head_x0 + head_size, head_y0 + head_size], radius=head_size // 4, **kwargs)

    eye_r = max(4, head_size // 12)
    eye_y = head_y0 + head_size // 3
    eye_kwargs = {"outline": color, "width": 3} if outline else {"fill": color}
    for eye_x in (head_x0 + head_size // 3, head_x0 + head_size * 2 // 3):
        draw.ellipse([eye_x - eye_r, eye_y - eye_r, eye_x + eye_r, eye_y + eye_r], **eye_kwargs)

    torso_w, torso_h = int(size * 0.7), int(size * 0.5)
    torso_x0 = center_x - torso_w // 2
    torso_y0 = head_y0 + head_size + 8
    draw.rectangle([torso_x0, torso_y0, torso_x0 + torso_w, torso_y0 + torso_h], **kwargs)

    arm_w = int(size * 0.12)
    arm_h = int(torso_h * 0.7)
    arm_y0 = torso_y0 + 10
    draw.rectangle([torso_x0 - arm_w - 10, arm_y0, torso_x0 - 10, arm_y0 + arm_h], **kwargs)
    draw.rectangle([torso_x0 + torso_w + 10, arm_y0, torso_x0 + torso_w + arm_w + 10, arm_y0 + arm_h], **kwargs)


def _draw_generic_icon_silhouette(draw: ImageDraw.ImageDraw, center_x: int, center_y: int, size: int, color: tuple, outline: bool = False) -> None:
    """Abstract fallback marker (tile + centered circle) for any entity that doesn't match a known category."""
    kwargs = _shape_kwargs(color, outline)
    x0, y0 = center_x - size // 2, center_y - size // 2
    draw.rounded_rectangle([x0, y0, x0 + size, y0 + size], radius=size // 6, **kwargs)
    r = size // 4
    if outline:
        draw.ellipse([center_x - r, center_y - r, center_x + r, center_y + r], outline=color, width=4)
    else:
        draw.ellipse([center_x - r, center_y - r, center_x + r, center_y + r], fill=NAVY_PANEL_A)


_ICON_DRAWERS = {
    "vehicle": _draw_vehicle_silhouette,
    "robot": _draw_robot_silhouette,
    "generic": _draw_generic_icon_silhouette,
}


def _render_comparison_scene(scene: dict, storyboard: dict, output_path: Path, state: str = "hold", progress: float = 1.0) -> None:
    """
    Two independently-sourced scale signals inside the template's visual
    window (neutral header in the headline card, no "VS"/contest
    treatment) -- both sides always get identical typography scale and
    identical reveal/count-up duration, regardless of which reveals
    first, so neither is made to look more editorially important than
    the other. Never the full narration sentence.

    `state` drives the reveal phase (see
    storyboard_generator._build_comparison_motion_states): "intro" (both
    sides dimmed/outlined) -> "reveal_left" -> "reveal_right" ->
    "both_context"/"hold"/"hold_N" (both final). "reveal_both" reveals
    both sides simultaneously. The per-side tier/date/source context is
    always carried by the support card.
    """
    accent = _accent_color(storyboard)
    window = _window_navy_panel()
    draw = ImageDraw.Draw(window)

    divider_x = WINDOW[2] // 2
    draw.line([(divider_x, 50), (divider_x, 440)], fill=(60, 74, 130), width=4)

    is_hold = state == "hold" or state.startswith("hold_")
    left_revealed = is_hold or state in ("reveal_left", "reveal_right", "both_context", "reveal_both")
    right_revealed = is_hold or state in ("reveal_right", "both_context", "reveal_both")
    left_progress = progress if state in ("reveal_left", "reveal_both") else 1.0
    right_progress = progress if state in ("reveal_right", "reveal_both") else 1.0
    dim = (60, 74, 130)

    def _draw_side(side: dict | None, x_start: int, x_end: int, revealed: bool, side_progress: float) -> None:
        if not side:
            return
        center_x = (x_start + x_end) // 2
        icon_color = accent if revealed else dim
        icon_fn = _ICON_DRAWERS.get(side.get("icon") or "generic", _draw_generic_icon_silhouette)
        icon_fn(draw, center_x, 120, _ICON_SIZE, icon_color, outline=not revealed)

        stat_font = ImageFont.truetype(FONT_BOLD, 150)
        if revealed:
            stat_text = f"{_progress_stat(side.get('stat'), side_progress) or ''}{side.get('unit') or ''}"
        else:
            stat_text = "––"
        stat_color = (255, 255, 255) if revealed else dim
        draw.text((center_x, 270), stat_text, font=stat_font, fill=stat_color, anchor="mm")

        entity_font = ImageFont.truetype(FONT_BOLD, 40)
        entity_lines = _fit_lines(draw, side.get("entity") or "", entity_font, x_end - x_start - 160, 1)
        for line in entity_lines:
            draw.text((center_x, 392), line, font=entity_font, fill=stat_color, anchor="mm")

    _draw_side(scene.get("left"), 0, divider_x, left_revealed, left_progress)
    _draw_side(scene.get("right"), divider_x, WINDOW[2], right_revealed, right_progress)

    _compose_card(scene, storyboard, window, output_path)


def _render_key_fact_progression(scene: dict, storyboard: dict, output_path: Path) -> None:
    """
    Two "stage pill" boxes connected by an arrow -- built ONLY when
    app.content.storyboard_generator._extract_progression_stages()
    found a real "from X to Y" relationship in this scene's own real
    narration (never fabricated, never more than the 2 stages the
    sentence actually supports).
    """
    window = _window_navy_panel()
    draw = ImageDraw.Draw(window)
    accent = _accent_color(storyboard)
    stages = scene.get("stages") or ["", ""]
    pill_w, pill_h, gap = 820, 200, 200
    start_x = (WINDOW[2] - (pill_w * 2 + gap)) // 2
    pill_y = VISIBLE_CENTER_Y - pill_h // 2
    stage_font = ImageFont.truetype(FONT_BOLD, 50)

    def _draw_pill(x: int, label: str) -> None:
        draw.rounded_rectangle([x, pill_y, x + pill_w, pill_y + pill_h], radius=28, outline=CYAN, width=4, fill=NAVY_PANEL_A)
        lines = _wrap_text(draw, label, stage_font, pill_w - 60)[:2]
        text_y = pill_y + (pill_h - 60 * len(lines)) // 2
        for i, line in enumerate(lines):
            draw.text((x + pill_w / 2, text_y + i * 60 + 30), line, font=stage_font, fill=(255, 255, 255), anchor="mm")

    left_x, right_x = start_x, start_x + pill_w + gap
    _draw_pill(left_x, stages[0])
    _draw_pill(right_x, stages[1])
    arrow_y = pill_y + pill_h // 2
    ax0, ax1 = left_x + pill_w + 30, right_x - 30
    draw.line([(ax0, arrow_y), (ax1, arrow_y)], fill=accent, width=6)
    draw.polygon([(ax1, arrow_y - 18), (ax1, arrow_y + 18), (ax1 + 26, arrow_y)], fill=accent)
    _compose_card(scene, storyboard, window, output_path)


def _render_key_fact_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    if scene.get("stages"):
        _render_key_fact_progression(scene, storyboard, output_path)
    else:
        _render_story_card(scene, storyboard, output_path)


def _render_source_card_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    """
    Closing card used when a story's narration has no genuine concluding
    sentence -- plain source attribution only, never a fabricated
    editorial conclusion. (Episodes skip this scene; it only appears in
    a story's own storyboard preview video.) Uses the finalized primary
    lockup, undistorted, on the soft light ground.
    """
    image = soft_background(vivid=True).convert("RGBA")
    lockup_path = brand_assets.get_lockup_path()
    if lockup_path.exists():
        lockup = Image.open(lockup_path).convert("RGBA")
        target_h = 860
        lockup = lockup.resize((round(lockup.width * target_h / lockup.height), target_h), Image.LANCZOS)
        image.alpha_composite(lockup, ((SCENE_WIDTH - lockup.width) // 2, 100))
    draw = ImageDraw.Draw(image)
    closing_font = ImageFont.truetype(FONT_REGULAR, 48)
    draw.text((SCENE_WIDTH / 2, 1120), scene.get("closing_line") or "", font=closing_font, fill=INK_MUTED, anchor="mm")
    image.convert("RGB").save(output_path, "PNG")


_RENDERERS = {
    "hero": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "statistic": _render_statistic_scene,
    "comparison": _render_comparison_scene,
    "quote": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "concept": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "key_fact": _render_key_fact_scene,
    "company": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "product": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "takeaway": lambda scene, storyboard, path: _render_story_card(scene, storyboard, path),
    "source_card": _render_source_card_scene,
}


def render_scene_image(scene: dict, storyboard: dict, output_path: Path) -> None:
    """Dispatches on scene['scene_type'] to the matching renderer above."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    renderer = _RENDERERS.get(scene["scene_type"], _RENDERERS["hero"])
    renderer(scene, storyboard, output_path)


_COUNTUP_CAPABLE_TYPES = {"comparison", "statistic"}


def render_scene_frame_sequence(scene: dict, storyboard: dict, frame_dir: Path, fps: int = 25) -> list[dict]:
    """
    Returns an ORDERED list of segments -- `[{"duration", "frame_paths",
    "is_ramp"}, ...]` -- whose durations always sum to the scene's own
    total `duration`. app/content/storyboard_composer.py builds one
    ffmpeg input per segment and concatenates them, so this function
    is the single place that decides how many distinct visual beats a
    scene has.

    - A multi-state `comparison` scene (motion["states"] present, see
      storyboard_generator._build_comparison_motion_states) returns one
      segment per named state: a ramp segment for "reveal_left"/
      "reveal_right" (a real sequence of Pillow frames, that state's
      side counting up), a single static frame for every other state
      ("intro"/"both_context"/"hold"/the short-duration fallback's
      "reveal_both").
    - A non-comparison scene whose real duration exceeded its own QA
      cap at generation time (motion["states"] present, see
      storyboard_generator._build_static_states) returns one segment
      per state: the same real content rendered once (or ramped, if a
      real count-up value exists) and reused across every static
      segment, each carrying its own "zoom" for
      storyboard_composer.py to apply -- never a new Pillow frame per
      segment, just a different camera framing of the same frame.
    - Every other scene type (including `statistic` under its cap, and
      a comparison scene that fell back to the original flat
      countup_seconds shape) returns the SAME 1-or-2-segment shape
      this function always returned before this change: a single
      static frame for no count-up, or a ramp segment + one
      held-final-frame segment for the remainder of the duration.

    Baking the animation directly into Pillow frames -- rather than
    trying to have ffmpeg overlay animated text at a fixed pixel
    position -- means the count-up always lines up exactly with each
    card's own layout (the same drawing code computes both), with no
    separate coordinate-matching step that could silently drift out of
    sync with a layout change.
    """
    frame_dir.mkdir(parents=True, exist_ok=True)
    scene_type = scene["scene_type"]
    motion = scene.get("motion") or {}
    states = motion.get("states")

    if scene_type == "comparison" and states:
        segments = []
        for state_def in states:
            name = state_def["name"]
            duration = state_def["duration"]
            countup_seconds = state_def.get("countup_seconds")
            state_dir = frame_dir / name
            state_dir.mkdir(parents=True, exist_ok=True)

            if countup_seconds:
                frame_count = max(1, round(countup_seconds * fps))
                paths = [state_dir / f"frame_{i:05d}.png" for i in range(frame_count)]
                for i, path in enumerate(paths):
                    progress = (i + 1) / frame_count
                    _render_comparison_scene(scene, storyboard, path, state=name, progress=progress)
                segments.append({"duration": duration, "frame_paths": paths, "is_ramp": True})
            else:
                path = state_dir / "frame_00000.png"
                _render_comparison_scene(scene, storyboard, path, state=name, progress=1.0)
                segments.append({"duration": duration, "frame_paths": [path], "is_ramp": False})
        return segments

    countup_seconds = motion.get("countup_seconds")
    total_duration = scene["duration"]

    # A statistic scene with no real extracted value (visual_mode ==
    # "headline") never counts up, regardless of what motion.type says
    # -- explicit on the real data, not an implicit side effect of
    # motion happening to omit countup_seconds.
    if scene_type == "statistic" and scene.get("visual_mode") == "headline":
        countup_seconds = None

    if states:
        # Phase 3A generalization: a non-comparison scene whose real
        # duration exceeded its own QA cap at generation time (see
        # storyboard_generator._build_static_states) -- same real
        # content, rendered once (or, when a real count-up value
        # exists, ramped exactly as the single-segment path below
        # would) and reused across every static segment; only each
        # segment's own duration and camera zoom (carried on the state
        # dict, applied by app/content/storyboard_composer.py) differ,
        # so consecutive segments are never pixel-identical.
        segments = []
        static_path = None
        for state_def in states:
            if state_def.get("is_ramp"):
                renderer = _RENDERERS[scene_type]
                frame_count = max(1, round(state_def["duration"] * fps))
                ramp_paths = [frame_dir / f"frame_{i:05d}.png" for i in range(frame_count)]
                for i, path in enumerate(ramp_paths):
                    renderer(scene, storyboard, path, progress=(i + 1) / frame_count)
                segments.append({
                    "duration": state_def["duration"], "frame_paths": ramp_paths,
                    "is_ramp": True, "zoom": state_def.get("zoom", 1.0),
                })
            else:
                if static_path is None:
                    static_path = frame_dir / "frame_static.png"
                    render_scene_image(scene, storyboard, static_path)
                segments.append({
                    "duration": state_def["duration"], "frame_paths": [static_path],
                    "is_ramp": False, "zoom": state_def.get("zoom", 1.0),
                })
        return segments

    if scene_type not in _COUNTUP_CAPABLE_TYPES or not countup_seconds:
        path = frame_dir / "frame_00000.png"
        render_scene_image(scene, storyboard, path)
        return [{"duration": total_duration, "frame_paths": [path], "is_ramp": False}]

    renderer = _RENDERERS[scene_type]
    frame_count = max(1, round(countup_seconds * fps))
    ramp_paths = [frame_dir / f"frame_{i:05d}.png" for i in range(frame_count)]
    for i, path in enumerate(ramp_paths):
        progress = (i + 1) / frame_count
        renderer(scene, storyboard, path, progress=progress)

    hold_duration = max(0.04, total_duration - countup_seconds)

    return [
        {"duration": countup_seconds, "frame_paths": ramp_paths, "is_ramp": True},
        {"duration": hold_duration, "frame_paths": [ramp_paths[-1]], "is_ramp": False},
    ]
