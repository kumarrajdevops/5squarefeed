from datetime import date, datetime, timezone

from app.models import NewsItem, StoryContent
from app.tasks import episode_video


def _make_story(db, **overrides):
    defaults = dict(
        title="Original RSS Headline",
        canonical_url="https://example.com/story",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=date(2026, 9, 22),
        status="collected",
        raw_summary="The original RSS summary text.",
    )
    defaults.update(overrides)
    story = NewsItem(**defaults)
    db.add(story)
    db.flush()
    return story


def test_produce_story_content_preserves_a_human_edited_script(db_session, monkeypatch):
    """
    Direct regression for this session's most damaging bug: Produce
    used to unconditionally call generate_script() on every call,
    silently overwriting a script the editor had just saved via the
    dashboard's edit panel with the auto-generated template text.

    _produce_story_content() must skip script (re)generation whenever
    content.script_text is already populated -- whether that's from a
    prior auto-generation or (this test's scenario) a human edit -- and
    still regenerate voice/visual/video from whatever script is there.
    """
    story = _make_story(db_session)

    edited_text = "This is the editor's own carefully rewritten narration."
    content = StoryContent(
        story_id=story.id,
        headline="Editor's headline",
        summary="Editor's summary",
        script_text=edited_text,
        status="script_ready",  # set by PATCH /stories/{id}/content
    )
    db_session.add(content)
    db_session.flush()

    generate_script_calls = []
    voice_calls = []
    visual_calls = []
    video_calls = []

    def fake_generate_script(title, raw_summary):
        generate_script_calls.append((title, raw_summary))
        return {"headline": "SHOULD NOT BE USED", "summary": "SHOULD NOT BE USED", "script_text": "SHOULD NOT BE USED"}

    def fake_synthesize_voice(text, output_path):
        voice_calls.append(text)
        return []

    def fake_generate_card(headline, source_name, output_path):
        visual_calls.append(headline)

    def fake_get_audio_duration_seconds(path):
        return 12.5

    def fake_build_captions(segments, output_path):
        pass

    def fake_compose_video(**kwargs):
        video_calls.append(kwargs)

    monkeypatch.setattr(episode_video, "generate_script", fake_generate_script)
    monkeypatch.setattr(episode_video, "synthesize_voice", fake_synthesize_voice)
    monkeypatch.setattr(episode_video, "generate_card", fake_generate_card)
    monkeypatch.setattr(episode_video, "get_audio_duration_seconds", fake_get_audio_duration_seconds)
    monkeypatch.setattr(episode_video, "build_captions", fake_build_captions)
    monkeypatch.setattr(episode_video, "compose_video", fake_compose_video)

    ok = episode_video._produce_story_content(db_session, story, content)

    assert ok is True
    # The script stage must have been skipped entirely.
    assert generate_script_calls == []
    # The edited text must survive completely unchanged.
    assert content.script_text == edited_text
    assert content.headline == "Editor's headline"
    assert content.summary == "Editor's summary"
    # Voice/visual/video must still regenerate FROM the edited script.
    assert voice_calls == [edited_text]
    assert len(visual_calls) == 1
    assert len(video_calls) == 1
    assert content.status == "video_ready"


def test_produce_story_content_generates_script_for_brand_new_story(db_session, monkeypatch):
    """
    The opposite case: a story with no script yet must still get one
    generated -- the fix must not accidentally skip script generation
    for genuinely new content, only for content that already has one.
    """
    story = _make_story(db_session, raw_summary="Some real RSS summary.")
    content = StoryContent(story_id=story.id, status="pending")
    db_session.add(content)
    db_session.flush()

    generate_script_calls = []

    def fake_generate_script(title, raw_summary):
        generate_script_calls.append((title, raw_summary))
        return {"headline": "Generated headline", "summary": "Generated summary", "script_text": "Generated script"}

    monkeypatch.setattr(episode_video, "generate_script", fake_generate_script)
    monkeypatch.setattr(episode_video, "synthesize_voice", lambda text, output_path: [])
    monkeypatch.setattr(episode_video, "generate_card", lambda headline, source_name, output_path: None)
    monkeypatch.setattr(episode_video, "get_audio_duration_seconds", lambda path: 5.0)
    monkeypatch.setattr(episode_video, "build_captions", lambda segments, output_path: None)
    monkeypatch.setattr(episode_video, "compose_video", lambda **kwargs: None)

    ok = episode_video._produce_story_content(db_session, story, content)

    assert ok is True
    assert len(generate_script_calls) == 1
    assert content.script_text == "Generated script"
    assert content.status == "video_ready"


def test_produce_story_content_retry_after_video_failure_does_not_reregenerate_script(db_session, monkeypatch):
    """
    A story that already reached script_ready/voice_ready but failed at
    a later stage (e.g. video composition) should retry from where it
    left off, not waste a script regeneration it doesn't need -- same
    guard, different trigger than the human-edit case above.
    """
    story = _make_story(db_session)
    content = StoryContent(
        story_id=story.id,
        headline="Existing headline",
        summary="Existing summary",
        script_text="Existing script text",
        status="failed",
        error_message="[video] previous ffmpeg failure",
    )
    db_session.add(content)
    db_session.flush()

    generate_script_calls = []
    monkeypatch.setattr(
        episode_video,
        "generate_script",
        lambda title, raw_summary: generate_script_calls.append(1) or {},
    )
    monkeypatch.setattr(episode_video, "synthesize_voice", lambda text, output_path: [])
    monkeypatch.setattr(episode_video, "generate_card", lambda headline, source_name, output_path: None)
    monkeypatch.setattr(episode_video, "get_audio_duration_seconds", lambda path: 8.0)
    monkeypatch.setattr(episode_video, "build_captions", lambda segments, output_path: None)
    monkeypatch.setattr(episode_video, "compose_video", lambda **kwargs: None)

    ok = episode_video._produce_story_content(db_session, story, content)

    assert ok is True
    assert generate_script_calls == []
    assert content.script_text == "Existing script text"
    assert content.status == "video_ready"
