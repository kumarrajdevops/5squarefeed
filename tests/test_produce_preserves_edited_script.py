from datetime import date, datetime, timezone

from app.models import NewsItem, StoryContent
from app.tasks import content as content_task


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


def test_ensure_script_and_voice_preserves_a_human_edited_script(db_session, monkeypatch):
    """
    Direct regression for this project's most damaging content bug:
    Produce used to unconditionally call generate_script() on every
    call, silently overwriting a script the editor had just saved via
    the dashboard's edit panel with the auto-generated template text.

    ensure_script_and_voice() must skip script (re)generation whenever
    content.script_text is already populated -- whether that's from a
    prior auto-generation or (this test's scenario) a human edit -- and
    still regenerate voice/captions from whatever script is there.

    (Migrated from calling the now-removed app.tasks.episode_video.
    _produce_story_content, which just called through to this same
    function for its own script/voice stages -- the guarantee under
    test is identical, only the call target changed.)
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

    def fake_generate_script(title, raw_summary):
        generate_script_calls.append((title, raw_summary))
        return {"headline": "SHOULD NOT BE USED", "summary": "SHOULD NOT BE USED", "script_text": "SHOULD NOT BE USED"}

    def fake_synthesize_voice(text, output_path):
        voice_calls.append(text)
        return []

    def fake_get_audio_duration_seconds(path):
        return 12.5

    monkeypatch.setattr(content_task, "generate_script", fake_generate_script)
    monkeypatch.setattr(content_task, "synthesize_voice", fake_synthesize_voice)
    monkeypatch.setattr(content_task, "get_audio_duration_seconds", fake_get_audio_duration_seconds)

    ok = content_task.ensure_script_and_voice(db_session, story, content)

    assert ok is True
    # The script stage must have been skipped entirely.
    assert generate_script_calls == []
    # The edited text must survive completely unchanged.
    assert content.script_text == edited_text
    assert content.headline == "Editor's headline"
    assert content.summary == "Editor's summary"
    # Voice must still regenerate FROM the edited script.
    assert voice_calls == [edited_text]
    assert content.status == "voice_ready"


def test_ensure_script_and_voice_generates_script_for_brand_new_story(db_session, monkeypatch):
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

    monkeypatch.setattr(content_task, "generate_script", fake_generate_script)
    monkeypatch.setattr(content_task, "synthesize_voice", lambda text, output_path: [])
    monkeypatch.setattr(content_task, "get_audio_duration_seconds", lambda path: 5.0)

    ok = content_task.ensure_script_and_voice(db_session, story, content)

    assert ok is True
    assert len(generate_script_calls) == 1
    assert content.script_text == "Generated script"
    assert content.status == "voice_ready"


def test_ensure_script_and_voice_retry_after_voice_failure_does_not_reregenerate_script(db_session, monkeypatch):
    """
    A story that already has a script but failed before reaching
    audio_path/caption_segments (e.g. a previous voice-synthesis
    failure, or any later stage that never got that far) should retry
    voice generation from the existing script, not waste a script
    regeneration it doesn't need -- same guard, different trigger than
    the human-edit case above.
    """
    story = _make_story(db_session)
    content = StoryContent(
        story_id=story.id,
        headline="Existing headline",
        summary="Existing summary",
        script_text="Existing script text",
        status="failed",
        error_message="[voice] previous synthesis failure",
    )
    db_session.add(content)
    db_session.flush()

    generate_script_calls = []
    monkeypatch.setattr(
        content_task,
        "generate_script",
        lambda title, raw_summary: generate_script_calls.append(1) or {},
    )
    monkeypatch.setattr(content_task, "synthesize_voice", lambda text, output_path: [])
    monkeypatch.setattr(content_task, "get_audio_duration_seconds", lambda path: 8.0)

    ok = content_task.ensure_script_and_voice(db_session, story, content)

    assert ok is True
    assert generate_script_calls == []
    assert content.script_text == "Existing script text"
    assert content.status == "voice_ready"
