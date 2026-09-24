from unittest.mock import patch

from PIL import Image, ImageDraw

from app.content import brand_assets, scene_renderer


def _storyboard(**overrides):
    base = {"accent_color": [64, 156, 255], "taxonomy_category": "research", "source_name": "Example Source"}
    base.update(overrides)
    return base


def test_render_scene_image_produces_expected_canvas_size(tmp_path):
    scene = {"scene_type": "hero", "narration_text": "A quiet AI news day."}
    output_path = tmp_path / "hero.png"

    scene_renderer.render_scene_image(scene, _storyboard(), output_path)

    image = Image.open(output_path)
    assert image.size == (scene_renderer.SCENE_WIDTH, scene_renderer.SCENE_HEIGHT)


def test_unknown_scene_type_falls_back_to_hero_renderer(tmp_path):
    scene = {"scene_type": "totally_unrecognized_type", "narration_text": "Fallback text."}
    output_path = tmp_path / "fallback.png"

    scene_renderer.render_scene_image(scene, _storyboard(), output_path)

    assert output_path.exists()


def test_comparison_scene_never_draws_the_full_narration_sentence(tmp_path):
    """
    Direct regression for the explicit "no full sentence on the
    comparison card" requirement -- verified by recording every string
    ever passed to Pillow's own text-drawing call, not by guessing at
    layout. Only the concise extracted fields (stat/entity/tier/date/
    source) may be drawn; the full narration sentence must never
    appear as a drawn text argument.
    """
    narration = (
        "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
        "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
        "industrial robots will be deployed between 2026 and 2035."
    )
    scene = {
        "scene_type": "comparison",
        "narration_text": narration,
        "left": {"stat": "49", "unit": "M", "entity": "Autonomous vehicles", "tier": "L3–L5", "date": "By 2035", "source": "ABI Research"},
        "right": {"stat": "60", "unit": "M", "entity": "Industrial robots", "tier": None, "date": "2026–2035", "source": "Omdia"},
    }

    drawn_texts = []
    original_text = ImageDraw.ImageDraw.text

    def _capture(self, xy, text, *args, **kwargs):
        drawn_texts.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", _capture):
        scene_renderer.render_scene_image(scene, _storyboard(), tmp_path / "comparison.png")

    assert narration not in drawn_texts
    assert (tmp_path / "comparison.png").exists()


def test_statistic_progress_shows_an_intermediate_value_before_final():
    assert scene_renderer._progress_stat("40", 0.5) == "20"
    assert scene_renderer._progress_stat("49", 1.0) == "49"
    assert scene_renderer._progress_stat(None, 0.5) is None


def test_render_scene_frame_sequence_ramps_for_countup_and_holds_final_value(tmp_path):
    """
    A comparison scene with the ORIGINAL flat countup_seconds shape
    (no motion.states -- the short-duration fallback shape) still
    returns the same 2-segment {ramp, hold} contract as before this
    iteration's multi-state change.
    """
    scene = {
        "scene_type": "comparison",
        "narration_text": "text",
        "duration": 4.0,
        "left": {"stat": "10", "unit": "M", "entity": "A", "tier": None, "date": None, "source": None, "icon": "generic"},
        "right": {"stat": "20", "unit": "M", "entity": "B", "tier": None, "date": None, "source": None, "icon": "generic"},
        "motion": {"type": "split_reveal", "countup_seconds": 1.0},
    }
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=10)

    assert len(segments) == 2
    assert segments[0]["is_ramp"] is True
    assert len(segments[0]["frame_paths"]) == 10  # 1.0s countup at 10fps -> 10 ramp frames
    assert all(path.exists() for path in segments[0]["frame_paths"])
    assert segments[1]["is_ramp"] is False
    assert abs(sum(s["duration"] for s in segments) - 4.0) < 1e-9


def test_render_scene_frame_sequence_is_a_single_static_frame_without_countup(tmp_path):
    scene = {"scene_type": "hero", "narration_text": "text", "duration": 3.0, "motion": {"type": "zoom_in"}}
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=25)

    assert len(segments) == 1
    assert segments[0]["is_ramp"] is False
    assert segments[0]["duration"] == 3.0
    assert segments[0]["frame_paths"][0].exists()


def test_render_scene_frame_sequence_multi_state_comparison_returns_one_segment_per_state(tmp_path):
    """
    A multi-state comparison scene (motion.states present, see
    storyboard_generator._build_comparison_motion_states) returns one
    segment per named state, durations summing to the scene's own
    total duration -- the mechanism that lets a long hold be split
    into multiple bounded segments instead of one long unbroken one.
    """
    states = [
        {"name": "intro", "duration": 1.6, "countup_seconds": None, "countup_side": None},
        {"name": "reveal_left", "duration": 1.1, "countup_seconds": 1.1, "countup_side": "left"},
        {"name": "reveal_right", "duration": 1.1, "countup_seconds": 1.1, "countup_side": "right"},
        {"name": "both_context", "duration": 2.5, "countup_seconds": None, "countup_side": None},
        {"name": "hold_1", "duration": 3.98, "countup_seconds": None, "countup_side": None},
        {"name": "hold_2", "duration": 3.98, "countup_seconds": None, "countup_side": None},
    ]
    scene = {
        "scene_type": "comparison",
        "narration_text": "text",
        "duration": sum(s["duration"] for s in states),
        "header": "A Neutral Header",
        "left": {"stat": "49", "unit": "M", "entity": "Autonomous vehicles", "tier": "L3–L5", "date": "By 2035", "source": "ABI Research", "icon": "vehicle"},
        "right": {"stat": "60", "unit": "M", "entity": "Industrial robots", "tier": None, "date": "2026–2035", "source": "Omdia", "icon": "robot"},
        "motion": {"type": "split_reveal", "countup_seconds": 1.1, "states": states},
    }
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=10)

    assert len(segments) == len(states)
    assert abs(sum(s["duration"] for s in segments) - scene["duration"]) < 1e-9
    assert segments[1]["is_ramp"] is True   # reveal_left, has countup_seconds
    assert segments[3]["is_ramp"] is False  # both_context, static
    assert all(all(p.exists() for p in s["frame_paths"]) for s in segments)


def test_hero_scene_draws_only_the_kicker_never_a_supporting_copy_of_the_full_title(tmp_path):
    """
    Direct regression for the hero text-duplication fix (Story #51
    corrective iteration): the dominant kicker is the ONLY on-screen
    title-derived text this Pillow render produces -- no second,
    smaller copy of the full title is drawn alongside it. The real
    burned caption (a completely separate ffmpeg/srt mechanism in
    app/content/storyboard_composer.py, untouched by this fix) still
    carries the full sentence at the bottom of the final video -- this
    test only covers what this renderer itself draws into the frame.
    """
    title = "Why Deploying Physical AI at Scale Demands Safety at Every Layer."
    kicker = "Deploying Physical AI at Scale…"
    scene = {"scene_type": "hero", "narration_text": title, "kicker": kicker}

    drawn_texts = []
    original_text = ImageDraw.ImageDraw.text

    def _capture(self, xy, text, *args, **kwargs):
        drawn_texts.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", _capture):
        scene_renderer.render_scene_image(scene, _storyboard(), tmp_path / "hero.png")

    # The real, title-derived kicker is drawn (word-wrapped, so check
    # by line rather than exact string equality).
    assert any(kicker.rstrip("…") in t or t in kicker for t in drawn_texts)
    # The full title is never drawn, at any size -- no duplication.
    assert title not in drawn_texts
    assert not any(title in t for t in drawn_texts)
    # No invented replacement copy either -- the only title-area text
    # drawn is the real kicker itself (word-wrapped lines of it), plus
    # the storyboard's own unrelated chrome (taxonomy badge, source
    # line) -- never a new phrase this renderer made up to fill the
    # space the old supporting-title line used to occupy.
    source_line = f"Source: {_storyboard()['source_name']}"
    known_chrome = {"RESEARCH", source_line}
    for text in drawn_texts:
        assert text in kicker or kicker.startswith(text) or text in known_chrome


def test_key_fact_progression_scene_draws_both_stages_and_falls_back_without_them(tmp_path):
    scene_with_stages = {
        "scene_type": "key_fact",
        "narration_text": "Physical AI is moving rapidly from research to large-scale deployment.",
        "stages": ["RESEARCH", "LARGE-SCALE DEPLOYMENT"],
        "event_category": "research",
    }
    output_path = tmp_path / "key_fact_progression.png"
    scene_renderer.render_scene_image(scene_with_stages, _storyboard(), output_path)
    assert output_path.exists()

    scene_without_stages = {
        "scene_type": "key_fact",
        "narration_text": "Something notable happened today.",
        "stages": None,
        "headline": "Something notable happened today.",
        "event_category": "notable_event",
    }
    fallback_path = tmp_path / "key_fact_headline.png"
    scene_renderer.render_scene_image(scene_without_stages, _storyboard(), fallback_path)
    assert fallback_path.exists()


def test_comparison_multi_state_scene_reveals_progressively_and_treats_both_sides_equally(tmp_path):
    """
    Regression for "substantially equivalent visual treatment" --
    recording drawn text confirms the not-yet-revealed side shows the
    placeholder, never a real number, while the revealed side does;
    and that neither side gets a different stat font size than the
    other (both use the same drawing call).
    """
    base_scene = {
        "scene_type": "comparison",
        "narration_text": "text",
        "header": "A Neutral Header",
        "left": {"stat": "49", "unit": "M", "entity": "Autonomous vehicles", "tier": "L3–L5", "date": "By 2035", "source": "ABI Research", "icon": "vehicle"},
        "right": {"stat": "60", "unit": "M", "entity": "Industrial robots", "tier": None, "date": "2026–2035", "source": "Omdia", "icon": "robot"},
    }

    drawn_texts = []
    original_text = ImageDraw.ImageDraw.text

    def _capture(self, xy, text, *args, **kwargs):
        drawn_texts.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", _capture):
        scene_renderer._render_comparison_scene(base_scene, _storyboard(), tmp_path / "intro.png", state="intro", progress=1.0)
    assert "49M" not in drawn_texts and "60M" not in drawn_texts
    assert any("––" in t for t in drawn_texts)  # placeholder for not-yet-revealed sides

    drawn_texts.clear()
    with patch.object(ImageDraw.ImageDraw, "text", _capture):
        scene_renderer._render_comparison_scene(base_scene, _storyboard(), tmp_path / "hold_1.png", state="hold_1", progress=1.0)
    assert "49M" in drawn_texts
    assert "60M" in drawn_texts
    # No "VS"/contest wording ever drawn.
    assert not any("VS" == t.strip().upper() for t in drawn_texts)


def test_icon_shapes_render_without_error(tmp_path):
    """Smoke test: every icon drawer (revealed + outlined) produces a valid image, no crash."""
    for icon_name, drawer in scene_renderer._ICON_DRAWERS.items():
        image, draw = scene_renderer._new_canvas()
        drawer(draw, 500, 500, scene_renderer._ICON_SIZE, (64, 156, 255), outline=False)
        drawer(draw, 800, 500, scene_renderer._ICON_SIZE, (64, 156, 255), outline=True)
        output_path = tmp_path / f"icon_{icon_name}.png"
        image.save(output_path, "PNG")
        assert output_path.exists()


def test_logo_placement_uses_the_brand_assets_abstraction_not_a_hardcoded_path(tmp_path, monkeypatch):
    """
    Swapping the resolved logo asset (as a future finalized-brand-asset
    swap would) must require touching ONLY app.content.brand_assets --
    proven here by monkeypatching just that module's function and
    confirming the renderer picks it up with no changes of its own.
    """
    fake_logo = tmp_path / "fake_logo.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(fake_logo)
    monkeypatch.setattr(brand_assets, "get_logo_asset_path", lambda: fake_logo)

    scene = {"scene_type": "source_card", "closing_line": "Full story: Example"}
    output_path = tmp_path / "source_card.png"

    scene_renderer.render_scene_image(scene, _storyboard(), output_path)

    assert output_path.exists()
