"""
LinkedIn post for a published episode: the post text and the "Today's episode is live" image,
both stamped with the episode's IST made-on date, a fixed 9:00 AM time and its prod YouTube link.

LinkedIn's compose link can prefill text but cannot attach an image, so the dashboard puts the
image on the clipboard for a paste. Nothing here posts anything.
"""
import io
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).parent
TEMPLATE_TEXT = _HERE / "post_template.txt"
TEMPLATE_IMAGE = _HERE / "live_episode_template.png"
FONT_PATH = _HERE / "fonts" / "Poppins-SemiBold.ttf"

COMPOSE_URL = "https://www.linkedin.com/feed/?shareActive=true&text="

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TIME_RE = re.compile(r"^(1[0-2]|[1-9]):[0-5]\d (AM|PM)$")

# Geometry of the date pill in the template image (1254 x 1254), measured from the original text.
_ERASE_X = (420, 966)
_ERASE_Y = (830, 878)
_SAMPLE_Y = (824, 884)  # clean pill background just above and below the text
_BASELINE_Y = 870
_CENTER_X = 686
_ORIGINAL_TEXT_WIDTH = 504
_MAX_TEXT_WIDTH = 540
_TEXT_COLOR = (12, 14, 38)


POST_TIME_LABEL = "9:00 AM"


def post_labels(made_date: date) -> tuple[str, str]:
    """(DD/MM/YYYY, "9:00 AM"): the episode's IST made-on date and the fixed daily post time."""
    return made_date.strftime("%d/%m/%Y"), POST_TIME_LABEL


def validate_labels(date_label: str, time_label: str) -> None:
    if not DATE_RE.match(date_label):
        raise ValueError("date must look like 03/10/2026")
    if not TIME_RE.match(time_label):
        raise ValueError("time must look like 9:00 AM")


def build_post_text(date_label: str, time_label: str, youtube_url: str) -> str:
    validate_labels(date_label, time_label)
    return TEMPLATE_TEXT.read_text(encoding="utf-8").format(
        date=date_label, time=time_label, youtube_url=youtube_url
    ).strip() + "\n"


def compose_url(text: str) -> str:
    return COMPOSE_URL + quote(text, safe="")


def render_post_image(date_label: str, time_label: str) -> bytes:
    """The template with its date pill repainted: the old text is replaced by interpolating the
    pill's own background between the rows above and below it, then the new text is drawn in the
    same weight, colour and baseline, shrunk only if it would not fit."""
    validate_labels(date_label, time_label)
    image = Image.open(TEMPLATE_IMAGE).convert("RGB")
    x0, x1 = _ERASE_X
    y0, y1 = _ERASE_Y
    top_y, bottom_y = _SAMPLE_Y
    for x in range(x0, x1):
        top, bottom = image.getpixel((x, top_y)), image.getpixel((x, bottom_y))
        for y in range(y0, y1 + 1):
            t = (y - top_y) / (bottom_y - top_y)
            image.putpixel((x, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))

    text = f"{date_label} • {time_label} IST"
    size = 46
    font = ImageFont.truetype(str(FONT_PATH), size)
    # Match the original pill text width, then shrink if the new text is longer than the pill allows.
    size = max(1, round(size * _ORIGINAL_TEXT_WIDTH / font.getlength("03/10/2026 • 9:00 AM IST")))
    font = ImageFont.truetype(str(FONT_PATH), size)
    while font.getlength(text) > _MAX_TEXT_WIDTH and size > 20:
        size -= 1
        font = ImageFont.truetype(str(FONT_PATH), size)
    ImageDraw.Draw(image).text((_CENTER_X, _BASELINE_Y), text, font=font, fill=_TEXT_COLOR, anchor="ms")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
