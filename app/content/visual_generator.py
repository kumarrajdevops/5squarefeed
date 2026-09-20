from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# 16:9, matches standard video output resolution expectations.
CARD_WIDTH = 1280
CARD_HEIGHT = 720

# Dark-navy background with an electric-blue accent -- placeholder
# "5squareFeed" branding (text only; the real logo assets exist but
# aren't composited into these cards yet -- future work). The point
# here is a consistent, readable card generated with no API key.
BACKGROUND_COLOR = (12, 16, 28)
ACCENT_COLOR = (64, 156, 255)
TEXT_COLOR = (240, 240, 245)
SOURCE_COLOR = (150, 160, 180)

# Installed via the Dockerfile's fonts-dejavu-core package.
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TITLE_MARGIN_X = 60
TITLE_LINE_HEIGHT = 68


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
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


def generate_card(headline: str, source_name: str, output_path: Path) -> None:
    """
    Render a branded 1280x720 title card for a single story: wordmark,
    word-wrapped headline, source credit. No AI image generation, no
    API key -- pure programmatic drawing via Pillow.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    # Top accent bar + wordmark.
    draw.rectangle([(0, 0), (CARD_WIDTH, 10)], fill=ACCENT_COLOR)
    brand_font = ImageFont.truetype(FONT_BOLD, 32)
    draw.text((TITLE_MARGIN_X, 50), "AI NEWS", font=brand_font, fill=ACCENT_COLOR)

    # Headline, word-wrapped and vertically centered.
    title_font = ImageFont.truetype(FONT_BOLD, 56)
    max_text_width = CARD_WIDTH - (TITLE_MARGIN_X * 2)
    lines = _wrap_text(draw, headline, title_font, max_text_width)

    total_height = TITLE_LINE_HEIGHT * len(lines)
    start_y = (CARD_HEIGHT - total_height) // 2

    for i, line in enumerate(lines):
        draw.text(
            (TITLE_MARGIN_X, start_y + i * TITLE_LINE_HEIGHT),
            line,
            font=title_font,
            fill=TEXT_COLOR,
        )

    # Source credit, bottom-left.
    source_font = ImageFont.truetype(FONT_REGULAR, 28)
    draw.text(
        (TITLE_MARGIN_X, CARD_HEIGHT - 80),
        f"Source: {source_name}",
        font=source_font,
        fill=SOURCE_COLOR,
    )

    image.save(output_path, "PNG")


def generate_branding_card(main_text: str, sub_text: str, output_path: Path) -> None:
    """
    Render an episode-level intro/outro card: wordmark, main line,
    subtitle line. Same visual style as generate_card(), but without
    the "Source: " framing -- there's no story here, just episode
    branding.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    draw.rectangle([(0, 0), (CARD_WIDTH, 10)], fill=ACCENT_COLOR)
    brand_font = ImageFont.truetype(FONT_BOLD, 32)
    draw.text((TITLE_MARGIN_X, 50), "AI NEWS", font=brand_font, fill=ACCENT_COLOR)

    main_font = ImageFont.truetype(FONT_BOLD, 56)
    max_text_width = CARD_WIDTH - (TITLE_MARGIN_X * 2)
    lines = _wrap_text(draw, main_text, main_font, max_text_width)

    total_height = TITLE_LINE_HEIGHT * len(lines)
    # Shift up from dead-center to leave room for the subtitle below.
    start_y = (CARD_HEIGHT - total_height) // 2 - 40

    for i, line in enumerate(lines):
        draw.text(
            (TITLE_MARGIN_X, start_y + i * TITLE_LINE_HEIGHT),
            line,
            font=main_font,
            fill=TEXT_COLOR,
        )

    sub_font = ImageFont.truetype(FONT_REGULAR, 32)
    sub_y = start_y + len(lines) * TITLE_LINE_HEIGHT + 20
    draw.text((TITLE_MARGIN_X, sub_y), sub_text, font=sub_font, fill=SOURCE_COLOR)

    image.save(output_path, "PNG")
