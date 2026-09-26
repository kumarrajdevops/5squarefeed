"""
Focused regression tests for app.qa.video_qa's captions_present check.

Real bug this guards against: the enhanced Pillow/storyboard renderer
burns captions directly via an ASS filter from StoryContent.caption_segments
(real per-sentence timing) and never writes a standalone .srt --
captions_path stays None for any story produced only through that
pipeline. The OLD check required captions_path to exist on disk, so a
perfectly-captioned enhanced story would incorrectly report
captions_present=false. _has_real_captions() now accepts EITHER real
signal; these tests cover both, plus the failure modes that must still
correctly fail (no silent QA weakening).
"""
import json

from app.qa.video_qa import _has_real_captions, run_qa_checks


class _FakeContent:
    def __init__(self, captions_path=None, caption_segments=None, audio_path=None):
        self.captions_path = captions_path
        self.caption_segments = caption_segments
        self.audio_path = audio_path


class _FakeItem:
    def __init__(self, id, canonical_url="https://example.com/x"):
        self.id = id
        self.canonical_url = canonical_url


class _FakeState:
    def __init__(self, ai_relevance="ai_candidate", verification_status="verified"):
        self.ai_relevance = ai_relevance
        self.verification_status = verification_status


# ---------------------------------------------------------------------
# _has_real_captions -- the two real signals, and the failure modes.
# ---------------------------------------------------------------------

def test_true_for_enhanced_pipeline_caption_segments():
    content = _FakeContent(caption_segments=json.dumps([{"text": "Hi.", "start": 0.0, "end": 1.0}]))
    assert _has_real_captions(content) is True


def test_false_for_empty_caption_segments_list():
    # json.dumps([]) is a truthy, non-empty STRING -- must not be
    # mistaken for a real caption, the same class of bug a naive
    # `bool(content.caption_segments)` check would reintroduce.
    content = _FakeContent(caption_segments=json.dumps([]))
    assert _has_real_captions(content) is False


def test_false_for_malformed_caption_segments_json():
    content = _FakeContent(caption_segments="not valid json")
    assert _has_real_captions(content) is False


def test_true_for_old_pipeline_srt_file_on_disk(tmp_path):
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n", encoding="utf-8")
    content = _FakeContent(captions_path=str(srt_path))
    assert _has_real_captions(content) is True


def test_false_when_captions_path_recorded_but_file_missing(tmp_path):
    missing_path = tmp_path / "gone.srt"
    content = _FakeContent(captions_path=str(missing_path))
    assert _has_real_captions(content) is False


def test_false_with_neither_signal():
    assert _has_real_captions(_FakeContent()) is False


def test_true_when_both_signals_present(tmp_path):
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n", encoding="utf-8")
    content = _FakeContent(
        captions_path=str(srt_path),
        caption_segments=json.dumps([{"text": "Hi.", "start": 0.0, "end": 1.0}]),
    )
    assert _has_real_captions(content) is True


# ---------------------------------------------------------------------
# run_qa_checks -- the actual check wired end to end.
# ---------------------------------------------------------------------

def test_run_qa_checks_captions_present_passes_for_a_pure_enhanced_pipeline_story(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake-audio")
    content = _FakeContent(
        caption_segments=json.dumps([{"text": "Hi.", "start": 0.0, "end": 1.0}]),
        audio_path=str(audio_path),
    )
    story_rows = [(None, _FakeItem(1), _FakeState(), content)]

    checks = run_qa_checks(episode=None, story_rows=story_rows, video_path=None)

    captions_check = next(c for c in checks if c["check"] == "captions_present")
    assert captions_check["passed"] is True


def test_run_qa_checks_captions_present_fails_for_a_story_with_no_captions_at_all(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake-audio")
    content = _FakeContent(audio_path=str(audio_path))  # no captions_path, no caption_segments
    story_rows = [(None, _FakeItem(1), _FakeState(), content)]

    checks = run_qa_checks(episode=None, story_rows=story_rows, video_path=None)

    captions_check = next(c for c in checks if c["check"] == "captions_present")
    assert captions_check["passed"] is False
    assert "1" in captions_check["detail"]
