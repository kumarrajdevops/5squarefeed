from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.content import brand_assets


# Rendered natively at 2560x1440 (exactly 4/3 the pixel count of the
# 1920x1080 final output, still 16:9) -- ffmpeg's zoompan/scale filter
# (app/content/storyboard_composer.py) downsamples this for Ken-Burns
# headroom without visible blur. app/content/visual_generator.py's
# 1280x720 single-card path (used by every OTHER story's production
# video) is completely untouched by this module.
SCENE_WIDTH = 2560
SCENE_HEIGHT = 1440

# Same navy/text palette as visual_generator.py, at the new scale.
BACKGROUND_COLOR = (12, 16, 28)
TEXT_COLOR = (240, 240, 245)
SOURCE_COLOR = (150, 160, 180)
DIVIDER_COLOR = (45, 52, 70)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

CONTENT_MARGIN_X = 140

# All card content (numbers/labels/logo) stays above this line -- the
# bottom margin is reserved clear space so the burned .srt caption
# track (app/content/storyboard_composer.py, positioned at the very
# bottom like today's compose_video()) never overlaps a card's own
# drawn elements.
CAPTION_SAFE_TOP = int(SCENE_HEIGHT * 0.78)

LOGO_BADGE_SIZE = 190
DEFAULT_ACCENT_COLOR = (64, 156, 255)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Same word-wrap algorithm as app/content/visual_generator.py, at this module's scale."""
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


def _accent_color(storyboard: dict) -> tuple:
    accent = storyboard.get("accent_color")
    return tuple(accent) if accent else DEFAULT_ACCENT_COLOR


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), BACKGROUND_COLOR)
    return image, ImageDraw.Draw(image)


def _draw_top_bar(draw: ImageDraw.ImageDraw, accent: tuple) -> None:
    draw.rectangle([(0, 0), (SCENE_WIDTH, 20)], fill=accent)


def _draw_logo_badge(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    size: int = LOGO_BADGE_SIZE,
    position: tuple[int, int] = (CONTENT_MARGIN_X, 90),
) -> None:
    """
    All logo placement goes through app.content.brand_assets -- never
    a hardcoded file path here. When the resolved asset is a "badge"
    (today: an opaque tile with its own background), draw a thin
    border so it reads as a deliberate mark, not a stray screenshot; a
    future "cutout" asset (real alpha transparency) pastes directly
    with no border.
    """
    logo_path = brand_assets.get_logo_asset_path()
    if not logo_path.exists():
        return

    logo = Image.open(logo_path).convert("RGBA").resize((size, size))
    x, y = position

    if brand_assets.get_logo_render_mode() == "badge":
        draw.rectangle([x - 3, y - 3, x + size + 3, y + size + 3], outline=(255, 255, 255, 90), width=2)

    image.paste(logo, position, logo)


def _draw_taxonomy_badge(draw: ImageDraw.ImageDraw, taxonomy_category: str | None, accent: tuple) -> None:
    if not taxonomy_category:
        return

    label = taxonomy_category.replace("_", " ").upper()
    font = ImageFont.truetype(FONT_BOLD, 34)
    text_width = draw.textlength(label, font=font)
    pad_x, pad_y = 26, 14

    x1 = SCENE_WIDTH - CONTENT_MARGIN_X
    x0 = x1 - text_width - pad_x * 2
    y0, y1 = 90, 90 + 34 + pad_y * 2

    draw.rectangle([x0, y0, x1, y1], outline=accent, width=3)
    draw.text((x0 + pad_x, y0 + pad_y - 2), label, font=font, fill=accent)


def _draw_source_line(draw: ImageDraw.ImageDraw, source_name: str, y: int | None = None) -> None:
    if not source_name:
        return
    font = ImageFont.truetype(FONT_REGULAR, 34)
    draw.text((CONTENT_MARGIN_X, y if y is not None else CAPTION_SAFE_TOP - 60), f"Source: {source_name}", font=font, fill=SOURCE_COLOR)


def _render_hero_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    """
    Dominant visual is the short, real-title-derived `kicker` only --
    the full title is NOT redrawn a second time mid-screen. The burned
    caption (app/content/storyboard_composer.py) already carries the
    full real narration text at the bottom of the frame; a second,
    smaller rendering of that same full sentence mid-screen would just
    reproduce the "same sentence appears twice on screen at once"
    duplication this scene exists to eliminate, merely at a smaller
    size. Every other scene type already follows this same pattern
    (dominant text + caption, no third redundant supporting line).
    """
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)
    _draw_logo_badge(image, draw)
    _draw_taxonomy_badge(draw, storyboard.get("taxonomy_category"), accent)

    kicker = scene.get("kicker") or scene.get("narration_text") or ""
    kicker_font = ImageFont.truetype(FONT_BOLD, 100)
    max_width = SCENE_WIDTH - CONTENT_MARGIN_X * 2
    kicker_lines = _wrap_text(draw, kicker, kicker_font, max_width)[:3]
    line_height = 118
    kicker_height = line_height * len(kicker_lines)
    start_y = max(320, (CAPTION_SAFE_TOP - kicker_height) // 2)

    for i, line in enumerate(kicker_lines):
        draw.text((CONTENT_MARGIN_X, start_y + i * line_height), line, font=kicker_font, fill=TEXT_COLOR)

    _draw_source_line(draw, storyboard.get("source_name", ""))
    image.save(output_path, "PNG")


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


def _render_statistic_scene(scene: dict, storyboard: dict, output_path: Path, progress: float = 1.0) -> None:
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)
    _draw_logo_badge(image, draw)
    _draw_taxonomy_badge(draw, storyboard.get("taxonomy_category"), accent)

    stat, unit = _progress_stat(scene.get("stat"), progress), scene.get("unit") or ""
    stat_font = ImageFont.truetype(FONT_BOLD, 260)
    stat_text = f"{stat}{unit}" if stat else (scene.get("headline") or "")
    draw.text((CONTENT_MARGIN_X, 420), stat_text, font=stat_font, fill=TEXT_COLOR)

    entity = scene.get("entity") or scene.get("headline") or ""
    entity_font = ImageFont.truetype(FONT_BOLD, 56)
    draw.text((CONTENT_MARGIN_X, 720), entity, font=entity_font, fill=TEXT_COLOR)

    footer_parts = [p for p in (scene.get("tier"), scene.get("date"), scene.get("source")) if p]
    if footer_parts:
        footer_font = ImageFont.truetype(FONT_REGULAR, 36)
        draw.text((CONTENT_MARGIN_X, 800), "  ·  ".join(footer_parts), font=footer_font, fill=SOURCE_COLOR)

    _draw_source_line(draw, storyboard.get("source_name", ""))
    image.save(output_path, "PNG")


_ICON_SIZE = 150


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
        draw.ellipse([center_x - r, center_y - r, center_x + r, center_y + r], fill=BACKGROUND_COLOR)


_ICON_DRAWERS = {
    "vehicle": _draw_vehicle_silhouette,
    "robot": _draw_robot_silhouette,
    "generic": _draw_generic_icon_silhouette,
}


def _render_comparison_scene(scene: dict, storyboard: dict, output_path: Path, state: str = "hold", progress: float = 1.0) -> None:
    """
    Split-screen card: concise extracted fields only on each side
    (stat/entity/tier/date/source/icon) -- deliberately NEVER the full
    narration sentence. Framed as two independently-sourced scale
    signals (a neutral `header`, no "VS"/contest treatment) -- both
    sides always get identical typography scale and identical
    reveal/count-up duration, regardless of which reveals first, so
    neither is made to look more editorially important than the other.

    `state` drives which reveal phase is drawn (see
    storyboard_generator._build_comparison_motion_states): "intro"
    (header only, both sides dimmed/outlined) -> "reveal_left" (left
    counts up) -> "reveal_right" (right counts up, left already
    settled) -> "both_context"/"hold" (both final, full tier/date/
    source context shown). A hold longer than the QA duration cap is
    split into multiple states named "hold_1"/"hold_2"/... (see
    _split_hold_states) -- any state name starting with "hold" is
    treated identically to plain "hold" here (same fully-revealed,
    full-context visual; only the underlying motion segment count
    differs). The short-duration fallback's "reveal_both" state
    reveals both sides simultaneously with full context, exactly
    matching this scene type's original (pre-this-iteration) behavior.
    """
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)
    _draw_logo_badge(image, draw)
    _draw_taxonomy_badge(draw, storyboard.get("taxonomy_category"), accent)

    header = scene.get("header")
    if header:
        header_font = ImageFont.truetype(FONT_BOLD, 46)
        header_text = header.upper()
        w = draw.textlength(header_text, font=header_font)
        draw.text(((SCENE_WIDTH - w) / 2, 300), header_text, font=header_font, fill=accent)

    divider_x = SCENE_WIDTH // 2
    draw.line([(divider_x, 380), (divider_x, CAPTION_SAFE_TOP - 40)], fill=DIVIDER_COLOR, width=4)

    is_hold = state == "hold" or state.startswith("hold_")
    left_revealed = is_hold or state in ("reveal_left", "reveal_right", "both_context", "reveal_both")
    right_revealed = is_hold or state in ("reveal_right", "both_context", "reveal_both")
    left_progress = progress if state in ("reveal_left", "reveal_both") else 1.0
    right_progress = progress if state in ("reveal_right", "reveal_both") else 1.0
    show_context = is_hold or state in ("both_context", "reveal_both")

    def _draw_side(side: dict | None, x_start: int, x_end: int, revealed: bool, side_progress: float) -> None:
        if not side:
            return
        center_x = (x_start + x_end) // 2
        icon_color = accent if revealed else DIVIDER_COLOR
        icon_fn = _ICON_DRAWERS.get(side.get("icon") or "generic", _draw_generic_icon_silhouette)
        icon_fn(draw, center_x, 460, _ICON_SIZE, icon_color, outline=not revealed)

        stat_font = ImageFont.truetype(FONT_BOLD, 200)
        if revealed:
            stat_text = f"{_progress_stat(side.get('stat'), side_progress) or ''}{side.get('unit') or ''}"
        else:
            stat_text = "––"
        stat_color = TEXT_COLOR if revealed else DIVIDER_COLOR
        w = draw.textlength(stat_text, font=stat_font)
        draw.text((center_x - w / 2, 620), stat_text, font=stat_font, fill=stat_color)

        entity_font = ImageFont.truetype(FONT_BOLD, 46)
        entity = side.get("entity") or ""
        w = draw.textlength(entity, font=entity_font)
        draw.text((center_x - w / 2, 850), entity, font=entity_font, fill=stat_color)

        if show_context:
            y = 910
            if side.get("tier"):
                tier_font = ImageFont.truetype(FONT_REGULAR, 36)
                w = draw.textlength(side["tier"], font=tier_font)
                draw.text((center_x - w / 2, y), side["tier"], font=tier_font, fill=SOURCE_COLOR)
                y += 54

            footer = " · ".join(p for p in (side.get("date"), side.get("source")) if p)
            if footer:
                footer_font = ImageFont.truetype(FONT_REGULAR, 32)
                w = draw.textlength(footer, font=footer_font)
                draw.text((center_x - w / 2, y), footer, font=footer_font, fill=SOURCE_COLOR)

    _draw_side(scene.get("left"), CONTENT_MARGIN_X, divider_x, left_revealed, left_progress)
    _draw_side(scene.get("right"), divider_x, SCENE_WIDTH - CONTENT_MARGIN_X, right_revealed, right_progress)

    image.save(output_path, "PNG")


def _draw_kicker(draw: ImageDraw.ImageDraw, accent: tuple, text: str, position: tuple[int, int] = (CONTENT_MARGIN_X, 360)) -> None:
    kicker_font = ImageFont.truetype(FONT_BOLD, 44)
    draw.text(position, text.upper(), font=kicker_font, fill=accent)


def _render_headline_card(scene: dict, storyboard: dict, output_path: Path, kicker: str | None = None) -> None:
    """Shared layout for quote/concept/key_fact/company/product/takeaway -- a kicker label + a short headline."""
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)
    _draw_logo_badge(image, draw)
    _draw_taxonomy_badge(draw, storyboard.get("taxonomy_category"), accent)

    if kicker:
        _draw_kicker(draw, accent, kicker)

    headline = scene.get("headline") or scene.get("quote_text") or ""
    headline_font = ImageFont.truetype(FONT_BOLD, 84)
    max_width = SCENE_WIDTH - CONTENT_MARGIN_X * 2
    lines = _wrap_text(draw, headline, headline_font, max_width)[:4]
    line_height = 100

    for i, line in enumerate(lines):
        draw.text((CONTENT_MARGIN_X, 480 + i * line_height), line, font=headline_font, fill=TEXT_COLOR)

    _draw_source_line(draw, storyboard.get("source_name", ""))
    image.save(output_path, "PNG")


def _render_quote_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    _render_headline_card(scene, storyboard, output_path, kicker="Quote")


def _render_concept_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    _render_headline_card(scene, storyboard, output_path, kicker="Concept")


def _render_key_fact_progression(scene: dict, storyboard: dict, output_path: Path) -> None:
    """
    Two "stage pill" boxes connected by an arrow -- built ONLY when
    app.content.storyboard_generator._extract_progression_stages()
    found a real "from X to Y" relationship in this scene's own real
    narration (never fabricated, never more than the 2 stages the
    sentence actually supports).
    """
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)
    _draw_logo_badge(image, draw)
    _draw_taxonomy_badge(draw, storyboard.get("taxonomy_category"), accent)

    kicker = (scene.get("event_category") or "Key fact").replace("_", " ")
    _draw_kicker(draw, accent, kicker)

    stages = scene.get("stages") or ["", ""]
    pill_w, pill_h = 820, 220
    gap = 220
    total_w = pill_w * 2 + gap
    start_x = (SCENE_WIDTH - total_w) // 2
    pill_y = (CAPTION_SAFE_TOP - pill_h) // 2 + 40

    stage_font = ImageFont.truetype(FONT_BOLD, 52)

    def _draw_pill(x: int, label: str) -> None:
        draw.rounded_rectangle(
            [x, pill_y, x + pill_w, pill_y + pill_h], radius=24, outline=accent, width=4, fill=BACKGROUND_COLOR,
        )
        lines = _wrap_text(draw, label, stage_font, pill_w - 60)[:2]
        line_height = 60
        text_y = pill_y + (pill_h - line_height * len(lines)) // 2
        for i, line in enumerate(lines):
            w = draw.textlength(line, font=stage_font)
            draw.text((x + (pill_w - w) / 2, text_y + i * line_height), line, font=stage_font, fill=TEXT_COLOR)

    left_x = start_x
    right_x = start_x + pill_w + gap
    _draw_pill(left_x, stages[0])
    _draw_pill(right_x, stages[1])

    # Arrow connecting the two pills.
    arrow_y = pill_y + pill_h // 2
    arrow_x0 = left_x + pill_w + 30
    arrow_x1 = right_x - 30
    draw.line([(arrow_x0, arrow_y), (arrow_x1, arrow_y)], fill=accent, width=6)
    draw.polygon(
        [(arrow_x1, arrow_y - 18), (arrow_x1, arrow_y + 18), (arrow_x1 + 26, arrow_y)],
        fill=accent,
    )

    _draw_source_line(draw, storyboard.get("source_name", ""))
    image.save(output_path, "PNG")


def _render_key_fact_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    if scene.get("stages"):
        _render_key_fact_progression(scene, storyboard, output_path)
        return
    kicker = (scene.get("event_category") or "Key fact").replace("_", " ")
    _render_headline_card(scene, storyboard, output_path, kicker=kicker)


def _render_company_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    _render_headline_card(scene, storyboard, output_path, kicker=scene.get("company") or "Company")


def _render_product_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    _render_headline_card(scene, storyboard, output_path, kicker=scene.get("product") or "Product")


def _render_takeaway_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    _render_headline_card(scene, storyboard, output_path, kicker="Takeaway")


def _render_source_card_scene(scene: dict, storyboard: dict, output_path: Path) -> None:
    """
    The closing card used when the story's real narration has no
    genuine concluding sentence to show as a `takeaway` -- plain
    source attribution only, never a fabricated editorial conclusion.
    """
    image, draw = _new_canvas()
    accent = _accent_color(storyboard)
    _draw_top_bar(draw, accent)

    size = 420
    position = ((SCENE_WIDTH - size) // 2, (SCENE_HEIGHT - size) // 2 - 140)
    _draw_logo_badge(image, draw, size=size, position=position)

    closing_font = ImageFont.truetype(FONT_REGULAR, 48)
    closing_line = scene.get("closing_line") or ""
    w = draw.textlength(closing_line, font=closing_font)
    draw.text(((SCENE_WIDTH - w) / 2, position[1] + size + 60), closing_line, font=closing_font, fill=SOURCE_COLOR)

    image.save(output_path, "PNG")


_RENDERERS = {
    "hero": _render_hero_scene,
    "statistic": _render_statistic_scene,
    "comparison": _render_comparison_scene,
    "quote": _render_quote_scene,
    "concept": _render_concept_scene,
    "key_fact": _render_key_fact_scene,
    "company": _render_company_scene,
    "product": _render_product_scene,
    "takeaway": _render_takeaway_scene,
    "source_card": _render_source_card_scene,
}


def render_scene_image(scene: dict, storyboard: dict, output_path: Path) -> None:
    """Dispatches on scene['scene_type'] to the matching renderer above."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    renderer = _RENDERERS.get(scene["scene_type"], _render_hero_scene)
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
    - Every other scene type (including `statistic`, and a comparison
      scene that fell back to the original flat countup_seconds shape)
      returns the SAME 1-or-2-segment shape this function always
      returned before this change: a single static frame for no
      count-up, or a ramp segment + one held-final-frame segment for
      the remainder of the duration.

    Baking the animation directly into Pillow frames -- rather than
    trying to have ffmpeg overlay animated text at a fixed pixel
    position -- means the count-up always lines up exactly with each
    card's own dynamically-centered layout (the same drawing code
    computes both), with no separate coordinate-matching step that
    could silently drift out of sync with a layout change.
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
