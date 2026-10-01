"""Editable source/quote line: stored on StoryContent.support_text, wins over the automatic pick."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.content.scene_renderer import _support_text
from app.db import Base
from app.models import NewsItem, StoryContent


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    engine = engine.execution_options(schema_translate_map={"raw": None, "editorial": None})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def story_id(factory):
    with factory() as db:
        item = NewsItem(
            title="T", canonical_url="https://example.com/a", source_name="S", source_type="rss",
            collected_at=datetime(2026, 10, 1), collection_date=date(2026, 10, 1), status="collected",
        )
        db.add(item)
        db.flush()
        db.add(StoryContent(story_id=item.id, headline="H", script_text="S.", support_text="old"))
        db.commit()
        return item.id


def test_override_beats_every_scene_type():
    storyboard = {"_support": {"override": "  My own line.  ", "article_fact": "Auto fact."}}
    for scene in ({"scene_type": "hero"}, {"scene_type": "statistic", "stat": "5%", "tier": "x"}):
        assert _support_text(scene, storyboard, "Headline") == ("My own line.", False)


def test_patch_sets_and_clears_support_text(monkeypatch, factory, story_id):
    monkeypatch.setattr(main, "SessionLocal", factory)

    main.update_story_content(story_id, main.StoryContentUpdate(support_text="  “Quote,” said X.  "))
    with factory() as db:
        assert db.query(StoryContent).one().support_text == "“Quote,” said X."

    main.update_story_content(story_id, main.StoryContentUpdate(support_text="  "))
    with factory() as db:
        assert db.query(StoryContent).one().support_text is None


def test_patch_without_support_text_leaves_it_alone(monkeypatch, factory, story_id):
    monkeypatch.setattr(main, "SessionLocal", factory)
    main.update_story_content(story_id, main.StoryContentUpdate(headline="New"))
    with factory() as db:
        assert db.query(StoryContent).one().support_text == "old"
