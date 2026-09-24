from app.content.storyboard_composer import (
    CAPTION_MAX_LINES,
    MAX_CUE_DURATION_SECONDS,
    MAX_WORDS_PER_CUE,
    MIN_CUE_DURATION_SECONDS,
    _caption_cues,
    _comparison_caption_cues,
    _find_best_clause_split,
    _general_caption_cues,
    _split_long_cue,
    _wrap_caption_text,
)


# ---------------------------------------------------------------------
# Phase 3B: caption safety on long/merged scenes. All pure-Python --
# no ffmpeg needed (see tests/test_storyboard_compose_integration.py
# for the real end-to-end ffmpeg confirmation).
# ---------------------------------------------------------------------

def test_comparison_caption_cues_unchanged_byte_for_byte_regression():
    """
    Direct regression pin for Story #51's real comparison sentence --
    comparison scenes keep their own dedicated, unchanged 2-way split,
    never routed through the new Phase 3B generalized splitter.
    """
    narration = (
        "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
        "autonomous vehicles (AVs), while Omdia estimates that roughly 60 million "
        "industrial robots will be deployed between 2026 and 2035."
    )
    scene = {"scene_type": "comparison", "narration_text": narration, "duration": 14.2605}

    cues = _caption_cues(scene)

    assert len(cues) == 2
    assert cues[0]["text"] == (
        "By 2035, ABI Research projects an installed base of 49 million level 3-5 "
        "autonomous vehicles (AVs),"
    )
    assert cues[1]["text"] == (
        "Omdia estimates that roughly 60 million industrial robots will be deployed "
        "between 2026 and 2035."
    )
    assert cues[0]["start"] == 0.0
    assert cues[1]["end"] == 14.2605
    assert cues[0]["end"] == cues[1]["start"]
    # Identical to calling the dedicated function directly.
    assert cues == _comparison_caption_cues(scene)


def test_general_caption_cues_uses_real_narration_segments_when_present():
    """
    Tier 1: a merged scene's real per-sentence narration_segments each
    become their own cue with exact real timing -- zero estimation.
    Direct regression shape for Story #52 (2 real segments, each well
    under budget on their own, so Tier 2 never fires).
    """
    scene = {
        "scene_type": "hero",
        "duration": 10.39,
        "narration_segments": [
            {"text": "From Enablement to Execution, Egypt's AI Ecosystem Reaches Production Scale.", "start": 0.0, "end": 5.39},
            {"text": "Today, Egypt's AI builders gathered for a reception.", "start": 5.39, "end": 10.39},
        ],
    }

    cues = _general_caption_cues(scene)

    assert len(cues) == 2
    assert cues[0]["text"] == scene["narration_segments"][0]["text"]
    assert cues[1]["text"] == scene["narration_segments"][1]["text"]
    assert cues[0]["start"] == 0.0
    assert cues[1]["end"] == 10.39


def test_general_caption_cues_falls_back_to_whole_narration_text_without_narration_segments():
    """A scene missing narration_segments (an older/synthetic fixture) degrades gracefully to today's single-cue shape."""
    scene = {"scene_type": "hero", "duration": 5.0, "narration_text": "A short real sentence."}
    cues = _general_caption_cues(scene)
    assert cues == [{"text": "A short real sentence.", "start": 0.0, "end": 5.0}]


def test_split_long_cue_stays_a_single_cue_when_already_under_budget():
    cues = _split_long_cue("A short real sentence under budget.", start=0.0, end=3.0)
    assert cues == [{"text": "A short real sentence under budget.", "start": 0.0, "end": 3.0}]


def test_split_long_cue_splits_a_real_dense_run_on_sentence():
    """
    Direct regression shape for Story #19's real, punctuation-light
    run-on prose and Story #54's long single real sentence -- Tier 2
    fires because the sentence exceeds the word/duration budget even
    though it's a single real edge-tts sentence (no merge involved).
    Every resulting cue's text must be an exact, real, contiguous
    substring of the original -- never reworded or reordered.
    """
    text = (
        "Clean energy isn't hard to come by, but the pace of large-scale adoption has "
        "historically been slow due to bottlenecks, including out-of-date infrastructure, "
        "elongated research and development timelines, and upfront cost barriers."
    )
    duration = 12.39

    cues = _split_long_cue(text, start=0.0, end=duration)

    assert len(cues) > 1
    for cue in cues:
        assert (cue["end"] - cue["start"]) <= MAX_CUE_DURATION_SECONDS
        assert len(cue["text"].split()) <= MAX_WORDS_PER_CUE
        assert (cue["end"] - cue["start"]) >= MIN_CUE_DURATION_SECONDS - 1e-9
    # Reassembling every cue's text (they're real contiguous substrings
    # split at real word/clause boundaries) recovers the original words
    # in order -- nothing invented, reworded, or dropped.
    recovered_words = " ".join(cue["text"] for cue in cues).split()
    assert recovered_words == text.split()
    # Timing is contiguous and sums to the real total duration.
    for previous, current in zip(cues, cues[1:]):
        assert abs(previous["end"] - current["start"]) < 1e-9
    assert abs(cues[0]["start"] - 0.0) < 1e-9
    assert abs(cues[-1]["end"] - duration) < 1e-9


def test_split_long_cue_never_splits_below_the_minimum_cue_duration():
    """A real clause marker positioned right at the very start/end of a short-enough sentence must not produce a sliver cue."""
    text = "So, this is a real sentence with an early comma marker in it for testing purposes today."
    cues = _split_long_cue(text, start=0.0, end=6.0)
    for cue in cues:
        assert (cue["end"] - cue["start"]) >= MIN_CUE_DURATION_SECONDS - 1e-9


def test_find_best_clause_split_prefers_higher_priority_markers():
    """
    A conjunction marker (" while ") splits BEFORE itself, so the
    conjunction introduces the clause it belongs to -- reads more
    naturally than orphaning it onto the end of the first cue.
    """
    text = "Reports show growth, while analysts remain cautious about the outlook."
    split_at = _find_best_clause_split(text, duration=8.0)
    assert text[:split_at].strip() == "Reports show growth,"
    assert text[split_at:].strip() == "while analysts remain cautious about the outlook."
    # No word is ever dropped -- concatenating both sides recovers the original.
    assert (text[:split_at].strip() + " " + text[split_at:].strip()) == text


def test_find_best_clause_split_returns_none_without_any_real_marker():
    assert _find_best_clause_split("nomarkersinthisstringatall", duration=8.0) is None


def test_wrap_caption_text_never_changes_the_words_only_where_line_breaks_fall():
    text = "This is a real sentence that should wrap across more than one caption line."
    wrapped = _wrap_caption_text(text)
    assert wrapped.replace("\n", " ").split() == text.split()
    assert len(wrapped.split("\n")) >= 1


def test_wrap_caption_text_keeps_a_realistic_cue_within_the_max_line_budget():
    """Cross-checks the empirically-calibrated wrap width against a real 16-word example already accepted at 2 lines in production."""
    text = "By 2035, ABI Research projects an installed base of 49 million level 3-5 autonomous vehicles (AVs),"
    wrapped = _wrap_caption_text(text)
    assert len(wrapped.split("\n")) <= CAPTION_MAX_LINES
