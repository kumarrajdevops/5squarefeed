"""Deterministic unit tests for the Episode 3 visual-polish pass: caption
paging, text-run/fade filters, source-only support facts and photo framing."""
from datetime import datetime

from PIL import Image, ImageChops

from app.content import episode_renderer as er
from app.content import scene_renderer as sr
from app.content import support_facts as sf


LONG = ("Gemini 3.8 Flash-Lite TTS and Gemini 3.8 Flash TTS are our most expressive audio models yet, "
        "built for developers who want natural voices, fine control over pacing and tone, and reliable "
        "output across many languages and use cases in production applications today.")


def test_captions_never_exceed_two_lines_and_keep_every_word():
    cue = {"text": LONG, "start": 2.0, "end": 14.0}
    pages = er.split_caption_cue(cue, "Default")
    assert len(pages) >= 2
    assert all(len(p["lines"]) <= er.CAPTION_MAX_LINES for p in pages)
    assert " ".join(" ".join(p["lines"]) for p in pages).split() == LONG.split()
    assert pages[0]["start"] == 2.0 and pages[-1]["end"] == 14.0
    assert all(a["end"] == b["start"] or abs(a["end"] - b["start"]) < 1e-9 for a, b in zip(pages, pages[1:]))


def test_short_caption_is_a_single_page_with_unchanged_timing():
    pages = er.split_caption_cue({"text": "Short line.", "start": 1.0, "end": 3.0}, "Subordinate")
    assert len(pages) == 1 and pages[0]["lines"] == ["Short line."]
    assert (pages[0]["start"], pages[0]["end"]) == (1.0, 3.0)


def test_caption_box_stays_inside_the_frame():
    for style in ("Default", "Subordinate"):
        pages = er.split_caption_cue({"text": LONG, "start": 0, "end": 10}, style)
        for page in pages:
            x, y, _path = er._caption_box_path(page["lines"], style)
            assert x >= 100 and y >= er.OUTPUT_H * 0.8


def test_identical_text_runs_merge_and_fades_never_overlap():
    keys = [("a", "s", False), ("a", "s", False), ("b", "t", False)]
    runs = er.text_runs(keys, [0, 4, 8], [4, 8, 12])
    assert [(r["first"], r["start"], r["end"]) for r in runs] == [(0, 0, 8), (2, 8, 12)]
    _f0, en0 = er.text_layer_filter(0, "x", runs[0], True, False)
    f1, en1 = er.text_layer_filter(1, "y", runs[1], False, True)
    out_end = 8 + er.TEXT_LEAD + er.TEXT_FADE
    in_start = 8 + er.TEXT_LEAD + er.TEXT_FADE
    assert out_end <= in_start
    assert "fade=t=in" in f1 and "fade=t=out" not in f1


def test_photo_scenes_are_static_with_no_zoompan():
    assert not hasattr(er, "kenburns_filter")


def test_still_images_are_held_in_the_filter_graph_not_looped_inputs():
    flt = er.still(137.776)
    assert flt.startswith("loop=loop=-1:size=1") and "trim=duration=137.776" in flt
    run_ = {"start": 0.0, "end": 4.0}
    held, _ = er.text_layer_filter(2, "t", run_, True, True, hold=10.0)
    assert held.startswith("[2:v]loop=loop=-1") and "format=rgba" in held
    plain, _ = er.text_layer_filter(2, "t", run_, True, True)
    assert "loop=" not in plain


def test_intro_and_outro_cards_are_the_same_lockup_with_no_date_text(tmp_path):
    intro, outro = tmp_path / "a.png", tmp_path / "b.png"
    er.render_intro(intro)
    er.render_outro(outro)
    a, b = Image.open(intro).convert("RGB"), Image.open(outro).convert("RGB")
    assert ImageChops.difference(a, b).getbbox() is None
    band = (0, sr.SCENE_HEIGHT - 120, sr.SCENE_WIDTH, sr.SCENE_HEIGHT)
    assert len(set(a.crop(band).resize((64, 8)).getdata())) > 1


def test_intro_date_sits_just_below_the_tagline_and_nowhere_else(tmp_path):
    plain, dated = tmp_path / "a.png", tmp_path / "b.png"
    er.render_intro(plain)
    er.render_intro(dated, "September 30, 2026")
    a, b = Image.open(plain).convert("RGB"), Image.open(dated).convert("RGB")
    box = ImageChops.difference(a, b).getbbox()
    assert box is not None
    logo_bottom = int(sr.SCENE_HEIGHT * 0.03) + int(sr.SCENE_HEIGHT * 0.74)
    assert box[1] >= logo_bottom and box[3] < sr.SCENE_HEIGHT * 0.88
    assert abs((box[0] + box[2]) / 2 - sr.SCENE_WIDTH / 2) < 4


def test_bumper_specs_fit_their_cards_and_captions_stay_within_two_lines():
    assert er.INTRO_DUR == 7.5 and er.OUTRO_DUR == 6.5
    for name, spec in er.BUMPERS.items():
        assert spec["lead"] + er.BUMPER_TAIL < spec["duration"]
        for text in spec["captions"]:
            pages = er.split_caption_cue({"text": text, "start": 0, "end": 2}, "Default")
            assert len(pages) == 1 and len(pages[0]["lines"]) <= er.CAPTION_MAX_LINES
    assert "Good morning" in er.BUMPERS["intro"]["captions"][0]
    assert any("Thanks for watching" in c for c in er.BUMPERS["outro"]["captions"])


def test_bumper_voice_is_cached_and_cues_carry_the_lead_in(tmp_path, monkeypatch):
    calls = []

    def fake_synth(text, path):
        calls.append(text)
        path.write_bytes(b"x")
        return [{"text": "s1", "start": 0.0, "end": 1.0}, {"text": "s2", "start": 1.2, "end": 2.4},
                {"text": "s3", "start": 2.6, "end": 4.0}]

    monkeypatch.setattr(er, "BUMPER_DIR", tmp_path)
    monkeypatch.setattr(er, "synthesize_voice", fake_synth)
    monkeypatch.setattr(er, "get_audio_duration_seconds", lambda _p: 4.2)
    _mp3, cues = er.ensure_bumper_voice("intro")
    er.ensure_bumper_voice("intro")
    assert len(calls) == 1
    assert [c["text"] for c in cues] == er.BUMPERS["intro"]["captions"]
    assert cues[0]["start"] == er.BUMPERS["intro"]["lead"] and cues[-1]["end"] <= er.INTRO_DUR


def test_bumper_voice_that_overruns_its_card_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "BUMPER_DIR", tmp_path)
    monkeypatch.setattr(er, "synthesize_voice", lambda t, p: (p.write_bytes(b"x"), [{"text": "a", "start": 0, "end": 9}])[1])
    monkeypatch.setattr(er, "get_audio_duration_seconds", lambda _p: 9.0)
    import pytest
    with pytest.raises(ValueError):
        er.ensure_bumper_voice("outro")


def test_made_date_is_the_ist_date_of_created_at_even_when_naive():
    from datetime import timezone
    from app.dates import today_ist
    assert today_ist(datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc)).isoformat() == "2026-09-25"


def test_article_fact_is_verbatim_and_adds_information():
    article = (
        "Subscribe to our newsletter for more.\n"
        "The company said the new model cut inference cost by 40% across 12 data centers last quarter.\n"
        "It is great.\n"
    )
    fact = sf.pick_article_fact(article, "Vendor unveils product", "A launch happened today")
    assert fact == "The company said the new model cut inference cost by 40% across 12 data centers last quarter."
    assert fact in article


def test_article_fact_rejects_boilerplate_and_headline_repeats():
    title = "Google ships a new private compute service for everyone"
    article = "Google ships a new private compute service for everyone today and tomorrow.\nClick here to read more about our cookie policy and terms."
    assert sf.pick_article_fact(article, title, "") is None


def test_support_info_falls_back_to_source_only_data():
    info = sf.build_support_info("T", "", "", "", datetime(2026, 9, 23), "Example Source")
    assert info["article_fact"] is None and info["published"] == "Published Sep 23, 2026"
    scene = {"scene_type": "hero"}
    board = {"title": "Some Title", "source_name": "Example Source", "_support": info}
    text, quoted = sr._support_text(scene, board, "Different headline entirely")
    assert text in ("Some Title", "Published Sep 23, 2026") and quoted is False
    empty = sr._support_text(scene, {"title": "Same Headline Words Here", "source_name": "S"}, "Same Headline Words Here")
    assert empty == ("", False)


def test_photo_always_fills_full_window_height_undistorted(tmp_path):
    path = tmp_path / "busy.png"
    busy = Image.new("RGB", (1600, 900), (255, 255, 255))
    px = busy.load()
    for y in range(300, 900):
        for x in range(1600):
            px[x, y] = (0, 0, 0) if (x // 64 + y // 64) % 2 else (255, 255, 255)
    busy.save(path)
    win = sr._window_photo(path)
    assert win.size == (sr.WINDOW[2], sr.WINDOW[3])
    fit_w = round(1600 * sr.WINDOW[3] / 900)
    left = (sr.WINDOW[2] - fit_w) // 2
    assert min(win.getpixel((left + 2 + dx, 2))[0] for dx in range(0, 200, 4)) >= 250
    bottom = [win.getpixel((left + 2 + dx, sr.WINDOW[3] - 2))[0] for dx in range(0, 400, 4)]
    assert max(bottom) - min(bottom) > 150


def test_truncated_stored_headline_uses_story_title():
    board = {"title": "Anthropic’s biolab made a discovery", "scenes": []}
    scene = {"scene_type": "explainer", "headline": "Anthropic’s biolab made a discovery it’s comparing to…"}
    assert sr._headline_text(scene, board) == "Anthropic’s biolab made a discovery"


def test_complete_headline_is_left_unchanged():
    board = {"title": "Some Other Title"}
    scene = {"scene_type": "explainer", "headline": "A complete headline"}
    assert sr._headline_text(scene, board) == "A complete headline"


def test_overlong_title_keeps_stored_headline():
    board = {"title": "word " * 40}
    scene = {"scene_type": "explainer", "headline": "Short stored prefix…"}
    assert sr._headline_text(scene, board) == "Short stored prefix…"


def test_base_card_has_no_headline_box():
    overlay = sr._base_card({"source_name": "S"})
    x, y, w, h = sr.HEADLINE
    assert overlay.getpixel((x + w // 2, y + h // 2))[3] == 0


def test_html_entities_are_unescaped_in_captions():
    pages = er.split_caption_cue({"text": "Anthropic&#8217;s biolab &amp; it&#8217;s Crispr", "start": 0, "end": 4}, "Default")
    shown = " ".join(" ".join(p["lines"]) for p in pages)
    assert shown == "Anthropic’s biolab & it’s Crispr" and "&#" not in shown


def test_headline_text_contrasts_with_backdrop_and_has_a_thin_outline_only():
    dark = sr._headline_layer("Plain headline", set(), light=False)
    light = sr._headline_layer("Plain headline", set(), light=True)
    def solid(layer):
        return [p[:3] for p in layer.getdata() if p[3] == 255]
    assert (255, 255, 255) in solid(dark) and sr.NAVY in solid(light)
    total = dark.getchannel("A").getbbox()
    white = dark.getchannel("A").point(lambda v: 0).copy()
    white.putdata([255 if p[:3] == (255, 255, 255) and p[3] > 200 else 0 for p in dark.getdata()])
    core = white.getbbox()
    assert total[2] - core[2] <= 5 and core[0] - total[0] <= 5 and core[1] - total[1] <= 5


def test_light_backdrop_detection():
    x, y, w, h = sr.HEADLINE
    white = Image.new("RGB", (sr.WINDOW[2], sr.WINDOW[3]), (240, 240, 245))
    navy = Image.new("RGB", (sr.WINDOW[2], sr.WINDOW[3]), sr.NAVY)
    assert sr._window_is_light(white) and not sr._window_is_light(navy)
    assert not sr._scene_headline_is_light({"scene_type": "comparison"}, {"story_id": 1})


def test_adjacent_caption_cues_swap_cleanly_without_overlap_or_fade():
    cues = [
        {"lines": ["one"], "start": 1.0, "end": 4.10, "style": "Default"},
        {"lines": ["two"], "start": 4.05, "end": 7.0, "style": "Default"},
        {"lines": ["three"], "start": 9.0, "end": 11.0, "style": "Default"},
    ]
    events = [e for e in er.caption_events(cues).splitlines() if e.startswith("Dialogue: 1")]
    assert len(events) == 3
    starts = [e.split(",")[1] for e in events]
    ends = [e.split(",")[2] for e in events]
    assert starts[1] == ends[0]
    assert "fad(80,0)" in events[0] and "fad(0,80)" in events[1] and "fad(80,80)" in events[2]


def _solid_clip(path, color, seconds=1.0):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r={er.FPS}:d={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)


def _frame_mean(path, t):
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    return [sum(raw[c::3]) / (len(raw) // 3) for c in range(3)]


def test_transition_clip_sweeps_between_the_real_outgoing_and_incoming_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "OUTPUT_W", 320)
    monkeypatch.setattr(er, "OUTPUT_H", 180)
    a, b, out = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "t.mp4"
    _solid_clip(a, "red"), _solid_clip(b, "blue")
    er.make_transition_clip(a, b, out, er.STORY_GAP)

    import subprocess
    frames = int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                 "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                                check=True, capture_output=True, text=True).stdout.strip())
    assert frames == round(er.STORY_GAP * er.FPS)

    start, mid, end = _frame_mean(out, 0.0), _frame_mean(out, er.STORY_GAP / 2), _frame_mean(out, er.STORY_GAP - 0.05)
    assert start[0] > 200 and start[2] < 60        # begins on the outgoing (red) frame
    assert end[2] > 200 and end[0] < 60            # lands on the incoming (blue) frame
    assert mid[0] > 40 and mid[2] > 40             # mid-sweep shows both, not a flat colour


def test_every_boundary_is_one_transition_slot():
    assert er.GAP_DUR == er.STORY_GAP == er.OUTRO_GAP == 0.6
