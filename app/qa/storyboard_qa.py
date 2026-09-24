from pathlib import Path

from app.content import brand_assets
from app.content.storyboard_composer import (
    CAPTION_MAX_LINES,
    MAX_CUE_DURATION_SECONDS,
    MAX_WORDS_PER_CUE,
    _caption_cues,
    _wrap_caption_text,
)
from app.content.storyboard_generator import (
    _comparison_header,
    _derive_hero_kicker,
    _extract_progression_stages,
    _extract_stat_fields,
    _split_comparison,
)
from app.content.video_composer import probe_video


# Per-scene-type visual-duration caps (section 0's editorial-fidelity
# principle: a single unchanging visual shouldn't sit on screen for an
# unusually long time). For `comparison`, each named motion state is
# checked individually against its own cap, not the scene's total
# duration -- this is the direct regression check for the original
# "long static hold after count-up" problem.
_DURATION_CAPS = {
    "hero": 6.0, "takeaway": 6.0, "source_card": 6.0,
    "quote": 8.0, "concept": 8.0, "company": 8.0, "product": 8.0,
    "key_fact": 8.0, "statistic": 8.0,
    "comparison": 5.0,
}


def _dominant_text_field(scene: dict) -> str | None:
    scene_type = scene["scene_type"]
    if scene_type == "hero":
        return scene.get("kicker")
    if scene_type == "key_fact":
        stages = scene.get("stages")
        return " -> ".join(stages) if stages else scene.get("headline")
    if scene_type == "quote":
        return scene.get("quote_text")
    if scene_type in ("statistic", "concept", "company", "product", "takeaway"):
        return scene.get("headline")
    return None


def _normalize(text: str | None) -> str:
    return (text or "").strip().rstrip(".…").strip()


def _recompute_comparison_sides(scene: dict) -> tuple[dict, dict] | tuple[None, None]:
    comparison = _split_comparison(scene.get("narration_text") or "")
    if not comparison:
        return None, None
    left_clause, right_clause = comparison
    return _extract_stat_fields(left_clause), _extract_stat_fields(right_clause)


def _clip_path_for_scene(scene_dir: Path, scene: dict) -> Path:
    # Same naming convention as app/content/storyboard_composer.py's
    # own _clip_path_for_scene -- duplicated here (not imported) since
    # it's a one-line convention, not shared logic.
    return scene_dir / f"scene_{scene['order']}_{scene['scene_id']}.mp4"


def run_storyboard_qa_checks(storyboard: dict, scene_dir: Path, video_path: Path) -> list[dict]:
    """
    QA checks specific to a storyboard-composed video -- independent
    of and never touching app/qa/video_qa.py's episode-level checks.
    Each check re-verifies real state (the actual JSON, the actual
    files on disk, the actual rendered video), same "never just trust
    an earlier stage's own success report" spirit as video_qa.py.
    """
    checks: list[dict] = []
    scenes = sorted(storyboard.get("scenes", []), key=lambda s: s["order"])

    # 1. Structure: opens on hero, closes on a source/takeaway card,
    # and has more than just those two beats.
    first_ok = bool(scenes) and scenes[0]["scene_type"] == "hero"
    last_ok = bool(scenes) and scenes[-1]["scene_type"] in ("source_card", "takeaway")
    structure_ok = first_ok and last_ok and len(scenes) >= 2
    checks.append({
        "check": "scene_structure",
        "passed": structure_ok,
        "detail": (
            f"{len(scenes)} scenes, first={scenes[0]['scene_type'] if scenes else None}, "
            f"last={scenes[-1]['scene_type'] if scenes else None}"
        ),
    })

    # 2. Contiguous, non-overlapping timing, starting at 0.0.
    timing_ok = bool(scenes) and abs(scenes[0]["start"]) < 0.01
    gaps = []
    for previous, current in zip(scenes, scenes[1:]):
        if abs(current["start"] - previous["end"]) > 0.01:
            timing_ok = False
            gaps.append((previous["scene_id"], current["scene_id"]))
    checks.append({
        "check": "scene_timing_contiguous",
        "passed": timing_ok,
        "detail": "contiguous from 0.0" if timing_ok else f"gap/overlap at: {gaps}",
    })

    # 3. Every scene's own rendered clip exists and is non-empty.
    missing = [
        scene["scene_id"] for scene in scenes
        if not _clip_path_for_scene(scene_dir, scene).exists()
        or _clip_path_for_scene(scene_dir, scene).stat().st_size == 0
    ]
    checks.append({
        "check": "scene_assets_present",
        "passed": len(missing) == 0,
        "detail": "all scene clips present" if not missing else f"missing/empty: {missing}",
    })

    # 4/6. Real ffprobe measurement of the final composed video.
    if not video_path.exists():
        checks.append({"check": "resolution_1080p", "passed": False, "detail": "video file not found"})
        checks.append({"check": "audio_video_duration_match", "passed": False, "detail": "video file not found"})
    else:
        probe = probe_video(video_path)

        resolution_ok = probe.get("width") == 1920 and probe.get("height") == 1080
        checks.append({
            "check": "resolution_1080p",
            "passed": resolution_ok,
            "detail": f"{probe.get('width')}x{probe.get('height')}",
        })

        expected_duration = storyboard.get("total_duration_seconds", 0.0)
        actual_duration = probe.get("duration_seconds", 0.0)
        duration_diff = abs(actual_duration - expected_duration)
        checks.append({
            "check": "audio_video_duration_match",
            "passed": duration_diff < 0.3,
            "detail": f"expected {expected_duration:.2f}s, measured {actual_duration:.2f}s (diff {duration_diff:.2f}s)",
        })

    # 5. The brand asset placeholder actually resolves to a real file.
    logo_path = brand_assets.get_logo_asset_path()
    checks.append({
        "check": "brand_logo_present",
        "passed": logo_path.exists(),
        "detail": str(logo_path),
    })

    # 7. No scene's dominant on-screen text duplicates its full real
    # narration verbatim (skip scenes short enough that verbatim
    # reproduction isn't duplication -- matches _shorten()'s own cap).
    duplication_violations = []
    for scene in scenes:
        narration = scene.get("narration_text")
        if not narration or len(narration.split()) <= 9:
            continue
        dominant = _dominant_text_field(scene)
        if dominant and _normalize(dominant) == _normalize(narration):
            duplication_violations.append(scene["scene_id"])
    checks.append({
        "check": "text_duplication",
        "passed": len(duplication_violations) == 0,
        "detail": "no scene duplicates its full narration as dominant text" if not duplication_violations
        else f"duplicated in: {duplication_violations}",
    })

    # 8. No single unchanging visual state sits on screen longer than
    # its per-scene-type cap. Any scene with a named motion.states list
    # (comparison's own multi-state sequence, or -- Phase 3A -- a
    # generalized long-scene split for any other scene type, see
    # storyboard_generator._build_static_states) is checked per named
    # state; a scene with no states list is checked as a whole (the
    # regression check for "long static hold after count-up", now
    # scene-type-agnostic rather than comparison-specific).
    duration_violations = []
    for scene in scenes:
        scene_type = scene["scene_type"]
        cap = _DURATION_CAPS.get(scene_type, 8.0)
        states = (scene.get("motion") or {}).get("states")
        if states:
            for state in states:
                if state["duration"] > cap:
                    duration_violations.append(f"{scene['scene_id']}.{state['name']} ({state['duration']:.1f}s > {cap}s)")
        elif scene["duration"] > cap:
            duration_violations.append(f"{scene['scene_id']} ({scene['duration']:.1f}s > {cap}s)")
    checks.append({
        "check": "scene_visual_duration",
        "passed": len(duration_violations) == 0,
        "detail": "no single visual state exceeds its cap" if not duration_violations
        else f"exceeded: {duration_violations}",
    })

    # 9. Every stored numeric field matches a fresh re-derivation from
    # the scene's own real narration_text -- catches a hand-edited or
    # stale JSON, or a future extraction-logic change that silently
    # diverges from what's persisted.
    numeric_violations = []
    for scene in scenes:
        scene_type = scene["scene_type"]
        if scene_type == "comparison":
            recomputed_left, recomputed_right = _recompute_comparison_sides(scene)
            if recomputed_left is None:
                numeric_violations.append(f"{scene['scene_id']}: could not re-derive comparison")
                continue
            for side_name, stored, recomputed in (
                ("left", scene.get("left") or {}, recomputed_left),
                ("right", scene.get("right") or {}, recomputed_right),
            ):
                for field in ("stat", "unit", "tier", "date"):
                    if stored.get(field) != recomputed.get(field):
                        numeric_violations.append(
                            f"{scene['scene_id']}.{side_name}.{field}: stored={stored.get(field)!r} recomputed={recomputed.get(field)!r}"
                        )
        elif scene_type == "statistic":
            recomputed = _extract_stat_fields(scene.get("narration_text") or "")
            for field in ("stat", "unit", "tier", "date"):
                if scene.get(field) != recomputed.get(field):
                    numeric_violations.append(
                        f"{scene['scene_id']}.{field}: stored={scene.get(field)!r} recomputed={recomputed.get(field)!r}"
                    )
    checks.append({
        "check": "numeric_integrity",
        "passed": len(numeric_violations) == 0,
        "detail": "all stored numeric fields match fresh re-derivation" if not numeric_violations
        else f"mismatches: {numeric_violations}",
    })

    # 10. Each comparison side's entity stays paired with its own real
    # source attribution (e.g. ABI Research never drifts onto the
    # industrial-robots side) -- re-derivation against the same real
    # narration_text, not new external data.
    attribution_violations = []
    for scene in scenes:
        if scene["scene_type"] != "comparison":
            continue
        recomputed_left, recomputed_right = _recompute_comparison_sides(scene)
        if recomputed_left is None:
            continue
        stored_left, stored_right = scene.get("left") or {}, scene.get("right") or {}
        if stored_left.get("entity") != recomputed_left.get("entity") or stored_left.get("source") != recomputed_left.get("source"):
            attribution_violations.append(f"{scene['scene_id']}.left entity/source pairing")
        if stored_right.get("entity") != recomputed_right.get("entity") or stored_right.get("source") != recomputed_right.get("source"):
            attribution_violations.append(f"{scene['scene_id']}.right entity/source pairing")
    checks.append({
        "check": "source_attribution",
        "passed": len(attribution_violations) == 0,
        "detail": "every side's entity stays paired with its own source" if not attribution_violations
        else f"mismatched pairing: {attribution_violations}",
    })

    # 11. Provenance auditability (section 0's editorial-fidelity
    # principle): every scene records which real segment(s) it came
    # from; any generated progression, hero kicker, or comparison
    # header is confirmed to have actually been derived from real
    # story data -- never backfilled/invented to satisfy the schema.
    # The hero.kicker/comparison.header re-derivations are
    # conditionally exercised: they only run when the storyboard's own
    # top-level `title` field is present (real generation always sets
    # it; a synthetic fixture that omits it simply isn't checked on
    # this sub-rule -- the same conditionally-exercised philosophy
    # already used above for stories with no comparison/statistic
    # scene).
    fidelity_violations = []
    title = storyboard.get("title")
    for scene in scenes:
        indices = scene.get("source_segment_indices")
        if indices is None:
            fidelity_violations.append(f"{scene['scene_id']}: missing source_segment_indices")
            continue
        if not indices and scene["scene_type"] != "source_card":
            fidelity_violations.append(f"{scene['scene_id']}: no source segment recorded")

        if scene["scene_type"] == "key_fact" and scene.get("stages"):
            recomputed_stages = _extract_progression_stages(scene.get("narration_text") or "")
            if recomputed_stages != scene.get("stages"):
                fidelity_violations.append(f"{scene['scene_id']}: stored stages don't match fresh re-derivation")

        if title and scene["scene_type"] == "hero" and scene.get("kicker") is not None:
            recomputed_kicker = _derive_hero_kicker(title)
            if recomputed_kicker != scene.get("kicker"):
                fidelity_violations.append(f"{scene['scene_id']}: stored kicker doesn't match fresh re-derivation")

        if title and scene["scene_type"] == "comparison" and scene.get("header") is not None:
            recomputed_header = _comparison_header(
                title, scene.get("narration_text") or "",
                scene.get("left") or {}, scene.get("right") or {},
            )
            if recomputed_header != scene.get("header"):
                fidelity_violations.append(f"{scene['scene_id']}: stored header doesn't match fresh re-derivation")
    checks.append({
        "check": "source_fidelity",
        "passed": len(fidelity_violations) == 0,
        "detail": "every visual element traces to a real source segment" if not fidelity_violations
        else f"issues: {fidelity_violations}",
    })

    # 12-15. Caption safety (Phase 3B -- the Broader Validation's
    # confirmed caption-overflow finding). All four re-derive from the
    # scene's own real narration_segments using the SAME cue-splitting
    # functions app/content/storyboard_composer.py's real pipeline
    # uses to actually burn captions -- never a separate estimate.
    # "passed": False is a HARD FAILURE (a cue that would genuinely
    # overflow the reserved caption region). A cue at or above 80% of
    # a budget but still under it is noted in "detail" as a WARNING --
    # it does not fail the check, matching this project's existing
    # pass/fail check schema (no separate "level" field is introduced).
    duration_violations, warning_durations = [], []
    word_violations, warning_words = [], []
    line_violations = []
    coverage_violations = []

    for scene in scenes:
        if scene.get("silent"):
            continue
        cues = _caption_cues(scene)

        if scene["duration"] > MAX_CUE_DURATION_SECONDS and len(cues) <= 1:
            coverage_violations.append(f"{scene['scene_id']} ({scene['duration']:.1f}s, 1 cue)")

        for index, cue in enumerate(cues):
            cue_duration = cue["end"] - cue["start"]
            cue_words = len(cue["text"].split())
            cue_lines = len(_wrap_caption_text(cue["text"]).split("\n"))
            label = f"{scene['scene_id']}.cue{index}"

            if cue_duration > MAX_CUE_DURATION_SECONDS:
                duration_violations.append(f"{label} ({cue_duration:.1f}s > {MAX_CUE_DURATION_SECONDS}s)")
            elif cue_duration >= 0.8 * MAX_CUE_DURATION_SECONDS:
                warning_durations.append(f"{label} ({cue_duration:.1f}s)")

            if cue_words > MAX_WORDS_PER_CUE:
                word_violations.append(f"{label} ({cue_words} words > {MAX_WORDS_PER_CUE})")
            elif cue_words >= 0.8 * MAX_WORDS_PER_CUE:
                warning_words.append(f"{label} ({cue_words} words)")

            if cue_lines > CAPTION_MAX_LINES:
                line_violations.append(f"{label} ({cue_lines} lines > {CAPTION_MAX_LINES})")

    checks.append({
        "check": "caption_cue_duration",
        "passed": len(duration_violations) == 0,
        "detail": "every cue is within the duration budget" if not duration_violations
        else f"exceeded: {duration_violations}",
        **({"warning": warning_durations} if warning_durations else {}),
    })
    checks.append({
        "check": "caption_cue_word_count",
        "passed": len(word_violations) == 0,
        "detail": "every cue is within the word-count budget" if not word_violations
        else f"exceeded: {word_violations}",
        **({"warning": warning_words} if warning_words else {}),
    })
    checks.append({
        "check": "caption_cue_line_count",
        "passed": len(line_violations) == 0,
        "detail": f"no cue wraps past {CAPTION_MAX_LINES} lines" if not line_violations
        else f"exceeded: {line_violations}",
    })
    checks.append({
        "check": "caption_segmentation_coverage",
        "passed": len(coverage_violations) == 0,
        "detail": "every long scene has real caption segmentation" if not coverage_violations
        else f"under-segmented: {coverage_violations}",
    })

    return checks
