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


def _capture_draws():
    """Records every (text, font_size) Pillow's text-drawing call receives."""
    drawn = []
    original_text = ImageDraw.ImageDraw.text

    def _capture(self, xy, text, *args, **kwargs):
        font = kwargs.get("font")
        drawn.append((text, font.size if font else None))
        return original_text(self, xy, text, *args, **kwargs)

    return drawn, patch.object(ImageDraw.ImageDraw, "text", _capture)


def test_statistic_scene_with_no_stat_wraps_the_headline_inside_the_headline_card(tmp_path):
    """Regression for the giant-headline overflow: a headline-mode statistic
    scene uses the shared headline card -- fixed font size, at most
    HEADLINE_MAX_LINES lines, every word drawn exactly once, never at the
    stat font size."""
    headline = "In a press release on Monday, Instinct confirmed a Series C round"
    scene = {
        "scene_type": "statistic", "narration_text": headline + " for the startup.",
        "stat": None, "unit": None, "entity": headline, "tier": None,
        "date": "2026", "source": None, "headline": headline, "visual_mode": "headline",
    }
    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(), tmp_path / "statistic_headline.png")

    headline_words = [t for t, size in drawn if size == scene_renderer.HEADLINE_FONT_SIZE]
    assert headline_words == headline.split()
    assert not any(size == scene_renderer.STAT_FONT_SIZE for _, size in drawn)
    assert (tmp_path / "statistic_headline.png").exists()


def test_statistic_scene_with_a_real_stat_draws_the_number_big_and_the_entity_in_the_headline(tmp_path):
    scene = {
        "scene_type": "statistic", "narration_text": "The company reached 49 million users this year.",
        "stat": "49", "unit": "M", "entity": "Users", "tier": None, "date": None, "source": None, "headline": None,
    }
    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(), tmp_path / "statistic_real.png")

    assert ("49M", scene_renderer.STAT_FONT_SIZE) in drawn
    assert ("Users", scene_renderer.HEADLINE_FONT_SIZE) in drawn


def test_statistic_and_comparison_use_a_flat_card_but_story_visual_scenes_are_layered():
    assert scene_renderer.uses_story_visual({"scene_type": "hero"})
    assert scene_renderer.uses_story_visual({"scene_type": "statistic", "stat": None})
    assert not scene_renderer.uses_story_visual({"scene_type": "statistic", "stat": "49"})
    assert not scene_renderer.uses_story_visual({"scene_type": "comparison"})
    assert not scene_renderer.uses_story_visual({"scene_type": "key_fact", "stages": ["a", "b"]})
    assert scene_renderer.uses_story_visual({"scene_type": "key_fact", "stages": None})


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


def test_hero_scene_headline_is_the_real_story_title_never_invented_copy(tmp_path):
    """The hero headline card shows the story's own real title (HTML entities
    decoded), drawn word by word at one fixed size; the only other text is
    template chrome and the source attribution -- no invented phrase."""
    title = "Why Deploying Physical AI at Scale Demands Safety &amp; Trust"
    scene = {"scene_type": "hero", "narration_text": "n", "kicker": "Deploying Physical AI..."}
    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(title=title, story_id=None), tmp_path / "hero.png")

    headline_words = [t for t, size in drawn if size == scene_renderer.HEADLINE_FONT_SIZE]
    assert headline_words == "Why Deploying Physical AI at Scale Demands Safety & Trust".split()
    known = {"NEWS", "Example Source", "\u2014 Example Source \u2197", "SOURCE"}
    others = [t for t, size in drawn if size != scene_renderer.HEADLINE_FONT_SIZE]
    assert all(t in known for t in others), others


def test_episode_counter_is_derived_from_position_and_omitted_standalone(tmp_path):
    scene = {"scene_type": "hero", "narration_text": "n", "kicker": "k"}
    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(title="T", _episode_position=(7, 25)), tmp_path / "a.png")
    assert ("07 / 25", 32) in drawn

    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(title="T"), tmp_path / "b.png")
    assert not any("/" in t for t, _ in drawn)


def test_headline_is_fixed_size_and_capped_at_three_lines_with_ellipsis(tmp_path):
    long_title = " ".join(["Extraordinarily"] * 60)
    scene = {"scene_type": "hero", "narration_text": "n", "kicker": "k"}
    drawn, ctx = _capture_draws()
    with ctx:
        scene_renderer.render_scene_image(scene, _storyboard(title=long_title), tmp_path / "long.png")
    headline_draws = [t for t, size in drawn if size == scene_renderer.HEADLINE_FONT_SIZE]
    assert any(t.endswith("\u2026") for t in headline_draws)
    assert len(headline_draws) < 60
    assert {size for t, size in drawn if "Extraordinarily" in t} == {scene_renderer.HEADLINE_FONT_SIZE}


def test_fallback_window_used_when_no_real_asset_exists():
    with patch.object(scene_renderer.visual_assets, "cached_visual", return_value=None):
        window, kind = scene_renderer.story_visual_window(_storyboard(story_id=999999))
    assert kind == "fallback"
    assert window.size == (scene_renderer.WINDOW[2], scene_renderer.WINDOW[3])


def test_real_photo_is_cover_cropped_without_distortion(tmp_path):
    photo = tmp_path / "photo.png"
    src = Image.new("RGB", (1600, 900), (10, 200, 10))
    ImageDraw.Draw(src).ellipse([700, 350, 900, 550], fill=(255, 0, 0))
    src.save(photo)
    out = scene_renderer._window_photo(photo)
    assert out.size == (scene_renderer.WINDOW[2], scene_renderer.WINDOW[3])
    pts = [(x, y) for x in range(0, out.width, 8) for y in range(0, out.height, 8)
           if out.getpixel((x, y))[0] > 200 and out.getpixel((x, y))[1] < 60]
    xs, ys = zip(*pts)
    assert abs((max(xs) - min(xs)) - (max(ys) - min(ys))) <= 24  # a circle stays a circle


def test_layered_render_produces_transparent_window_hole_and_window_layer(tmp_path):
    scene = {"scene_type": "hero", "narration_text": "n", "kicker": "k"}
    with patch.object(scene_renderer.visual_assets, "cached_visual", return_value=None):
        kind = scene_renderer.render_scene_layers(scene, _storyboard(title="Title", story_id=1),
                                                  tmp_path / "card.png", tmp_path / "win.png")
    assert kind == "fallback"
    card = Image.open(tmp_path / "card.png")
    assert card.mode == "RGBA" and card.size == (scene_renderer.SCENE_WIDTH, scene_renderer.SCENE_HEIGHT)
    assert card.getpixel((scene_renderer.WINDOW[0] + 600, scene_renderer.WINDOW[1] + 100))[3] == 0
    assert Image.open(tmp_path / "win.png").size == (scene_renderer.WINDOW[2], scene_renderer.WINDOW[3])
    assert scene_renderer.render_scene_layers({"scene_type": "comparison"}, _storyboard(),
                                              tmp_path / "c.png", tmp_path / "w.png") is None


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


def test_card_mark_and_lockup_resolve_through_brand_assets_abstraction(tmp_path, monkeypatch):
    """Swapping the finalized brand files must only require touching
    app.content.brand_assets -- the card chrome picks the swap up."""
    fake_mark = tmp_path / "fake_mark.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(fake_mark)
    monkeypatch.setattr(brand_assets, "get_card_mark_path", lambda: fake_mark)
    scene = {"scene_type": "hero", "narration_text": "n", "kicker": "k"}
    out = tmp_path / "hero.png"
    scene_renderer.render_scene_image(scene, _storyboard(title="T"), out)
    px = Image.open(out).getpixel((scene_renderer.MARK_BOX[0] + 56, scene_renderer.MARK_BOX[1] + 56))
    assert px[0] > 200 and px[1] < 60 and px[2] < 60
    assert brand_assets.get_lockup_path().name == "5squarefeed-logo-primary.png"


# ---------------------------------------------------------------------
# Phase 3A: generalized long-scene split (any non-comparison scene
# whose real duration exceeded its own QA cap at generation time).
# ---------------------------------------------------------------------

def test_frame_sequence_generalized_split_reuses_one_static_frame_across_segments(tmp_path):
    """
    A non-comparison scene (e.g. concept) with a states list -- the
    Phase 3A generalization -- renders its real content ONCE and
    reuses that same file across every static segment; only each
    segment's own duration/zoom differ. Direct regression shape for
    Story #54's concept_1 (12.39s vs 8.0s cap).
    """
    scene = {
        "scene_type": "concept",
        "narration_text": "text",
        "duration": 12.39,
        "headline": "Clean energy isn't hard to come by",
        "motion": {"type": "headline_reveal", "states": [
            {"name": "segment_1", "duration": 6.195, "is_ramp": False, "zoom": 1.0},
            {"name": "segment_2", "duration": 6.195, "is_ramp": False, "zoom": 1.03},
        ]},
    }
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=25)

    assert len(segments) == 2
    assert segments[0]["frame_paths"] == segments[1]["frame_paths"]  # same file reused, not re-rendered
    assert segments[0]["zoom"] == 1.0
    assert segments[1]["zoom"] == 1.03
    assert abs(sum(s["duration"] for s in segments) - 12.39) < 1e-9
    assert all(p.exists() for s in segments for p in s["frame_paths"])


def test_frame_sequence_generalized_split_preserves_a_real_countup_ramp(tmp_path):
    """
    A count-up-capable scene (statistic) whose duration exceeds its
    cap keeps its real ramp as a genuine Pillow frame sequence -- only
    the static remainder reuses one frame across split segments.
    Direct regression shape for Story #41's statistic_1 (9.84s, 8.0s
    cap, 1.2s real countup).
    """
    scene = {
        "scene_type": "statistic",
        "narration_text": "text",
        "duration": 9.84,
        "stat": "250", "unit": "M", "entity": "Settlement",
        "tier": None, "date": None, "source": None,
        "visual_mode": "count_up",
        "motion": {"type": "count_up", "countup_seconds": 1.2, "states": [
            {"name": "reveal", "duration": 1.2, "is_ramp": True, "zoom": 1.0},
            {"name": "hold_1", "duration": 4.32, "is_ramp": False, "zoom": 1.0},
            {"name": "hold_2", "duration": 4.32, "is_ramp": False, "zoom": 1.03},
        ]},
    }
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=10)

    assert segments[0]["is_ramp"] is True
    assert len(segments[0]["frame_paths"]) == 12  # 1.2s at 10fps
    assert segments[1]["is_ramp"] is False and segments[2]["is_ramp"] is False
    assert segments[1]["frame_paths"] == segments[2]["frame_paths"]  # static portion reuses one file
    assert segments[1]["zoom"] != segments[2]["zoom"]
    assert abs(sum(s["duration"] for s in segments) - 9.84) < 1e-9


def test_frame_sequence_without_states_is_unaffected_by_the_generalization(tmp_path):
    """A scene under its cap (no states attached at generation time) renders exactly as before -- zero regression."""
    scene = {"scene_type": "hero", "narration_text": "text", "duration": 3.0, "kicker": "A short kicker", "motion": {"type": "zoom_in", "max_zoom": 1.12}}
    frame_dir = tmp_path / "frames"

    segments = scene_renderer.render_scene_frame_sequence(scene, _storyboard(), frame_dir, fps=25)

    assert len(segments) == 1
    assert "zoom" not in segments[0]
    assert segments[0]["duration"] == 3.0


def test_statistic_headline_falls_back_to_title_for_dangling_entity_fragment():
    scene = {"scene_type": "statistic", "stat": "140", "unit": "M", "entity": "To date and", "headline": "x"}
    assert scene_renderer._headline_text(scene, _storyboard(title="Ema raises $77M")) == "Ema raises $77M"
    ok = dict(scene, entity="Less cost vs. GPT-4.1.")
    assert scene_renderer._headline_text(ok, _storyboard(title="T")) == "Less cost vs. GPT-4.1."
