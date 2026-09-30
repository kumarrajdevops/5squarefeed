import io
import json
from unittest.mock import MagicMock, patch

from PIL import Image

from app.content import visual_assets as va


def _png_bytes(size=(1600, 900), color=(20, 120, 220)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _resp(status=200, content=b"", text="", ctype="image/png"):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.text = text
    r.headers = {"content-type": ctype}
    return r


def test_detect_entity_single_named_company_only():
    assert va.detect_entity("OpenAI ships GPT-5.6 to everyone", "TechCrunch") == "OpenAI"
    assert va.detect_entity("OpenAI and Google race on agents", "TechCrunch") is None
    assert va.detect_entity("A quiet day in AI", "TechCrunch") is None
    assert va.detect_entity("A quiet day in AI", "NVIDIA Blog") == "NVIDIA"
    assert va.detect_entity("Experts worry about Nvidia's AI chip sales in China", "Ars Technica AI") == "NVIDIA"


def test_extract_image_urls_prefers_og_and_resolves_relative():
    html_doc = (
        '<meta name="twitter:image" content="https://cdn.example.com/t.jpg">'
        '<meta property="og:image" content="/img/lead.jpg">'
    )
    urls = va.extract_image_urls(html_doc, "https://example.com/post/1")
    assert urls == ["https://example.com/img/lead.jpg", "https://cdn.example.com/t.jpg"]


def test_sensitive_titles_are_flagged():
    assert va.SENSITIVE_RE.search("OpenAI sued over school shooting")
    assert not va.SENSITIVE_RE.search("OpenAI launches a new model")


def test_photo_tier_records_provenance_and_reuses_cache(tmp_path):
    page = _resp(text='<meta property="og:image" content="https://cdn.example.com/lead.png">', ctype="text/html")
    img = _resp(content=_png_bytes())

    def fake_get(url, **kw):
        return img if url.endswith("lead.png") else page

    item = {"title": "A quiet day in AI", "source_name": "Example", "url": "https://example.com/a"}
    with patch.object(va, "ASSET_ROOT", tmp_path), patch.object(va, "_http_get", side_effect=fake_get):
        prov = va.ensure_story_visual(7, item)
        assert prov["kind"] == "photo" and prov["source_url"].endswith("lead.png")
        assert prov["story_url"] == "https://example.com/a" and "not verified" in prov["license_note"].lower()
        assert (tmp_path / "7" / prov["asset_file"]).exists()
        assert json.loads((tmp_path / "7" / "provenance.json").read_text())["story_id"] == 7
        with patch.object(va, "_http_get", side_effect=AssertionError("network used")):
            assert va.ensure_story_visual(7, item)["cached"] is True
            assert va.cached_visual(7)["kind"] == "photo"


def test_blocked_page_and_small_image_fall_back_without_raising(tmp_path):
    item = {"title": "A quiet day in AI", "source_name": "Example", "url": "https://example.com/a"}
    with patch.object(va, "ASSET_ROOT", tmp_path), patch.object(va, "_http_get", return_value=_resp(status=403)):
        prov = va.ensure_story_visual(8, item)
    assert prov["kind"] == "fallback" and "403" in prov["reason"]

    page = _resp(text='<meta property="og:image" content="https://cdn.example.com/tiny.png">', ctype="text/html")
    tiny = _resp(content=_png_bytes((64, 64)))
    with patch.object(va, "ASSET_ROOT", tmp_path), patch.object(
        va, "_http_get", side_effect=lambda url, **kw: tiny if url.endswith("tiny.png") else page
    ):
        prov = va.ensure_story_visual(9, item)
    assert prov["kind"] == "fallback" and "quality gate" in prov["reason"]


def test_sensitive_story_skips_logo_tier(tmp_path):
    item = {"title": "OpenAI sued after school shooting", "source_name": "News", "url": ""}
    with patch.object(va, "ASSET_ROOT", tmp_path), patch.object(va, "_try_logo") as logo:
        prov = va.ensure_story_visual(10, item)
    logo.assert_not_called()
    assert prov["kind"] == "fallback" and "sensitive" in prov["reason"]
