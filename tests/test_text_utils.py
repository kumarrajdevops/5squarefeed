from types import SimpleNamespace

from app.content.script_generator import generate_script
from app.text_utils import clean_text, repair_mojibake, response_text


def test_entities_are_decoded():
    assert clean_text("Anthropic&#8217;s biolab &amp; more") == "Anthropic’s biolab & more"


def test_double_escaped_entities_are_decoded():
    assert clean_text("it&amp;#8217;s") == "it’s"


def test_mojibake_is_repaired():
    garbled = "doesn’t".encode("utf-8").decode("cp1252")
    assert garbled != "doesn’t"
    assert repair_mojibake(garbled) == "doesn’t"
    assert clean_text(garbled) == "doesn’t"


def test_clean_text_leaves_normal_text_alone():
    for text in ["Café opens", "It's 5 < 6 & fine", "Trump’s plan", ""]:
        assert clean_text(text) == text


def test_none_passes_through():
    assert clean_text(None) is None


def test_generate_script_cleans_headline_and_script():
    out = generate_script("Here&#8217;s what leaders say", "<p>It doesn&#8217;t stop.</p>")
    assert out["headline"] == "Here’s what leaders say"
    assert "&#" not in out["script_text"]
    assert "doesn’t" in out["script_text"]


def test_generate_script_repairs_mojibake_summary():
    garbled = "It doesn’t stop.".encode("utf-8").decode("cp1252")
    out = generate_script("Headline", garbled)
    assert "doesn’t" in out["script_text"]


def _resp(content: bytes, content_type: str):
    return SimpleNamespace(
        content=content, headers={"content-type": content_type},
        text=content.decode("iso-8859-1"), apparent_encoding="utf-8",
    )


def test_response_without_charset_is_decoded_as_utf8():
    body = "doesn’t".encode("utf-8")
    assert response_text(_resp(body, "text/html")) == "doesn’t"


def test_response_with_declared_charset_uses_requests_decoding():
    body = "café".encode("iso-8859-1")
    assert response_text(_resp(body, "text/html; charset=iso-8859-1")) == "café"
