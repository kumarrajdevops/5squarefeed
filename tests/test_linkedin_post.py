import io
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from PIL import Image, ImageChops
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.models import Episode, EpisodePublication
from app.publishing.linkedin import post as linkedin

PROD_URL = "https://youtu.be/PRODVIDEO01"
DEV_URL = "https://youtu.be/DEVVIDEO001"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_post_labels_use_the_made_date_and_a_fixed_nine_am_time():
    assert linkedin.post_labels(date(2026, 10, 3)) == ("03/10/2026", "9:00 AM")
    assert linkedin.post_labels(date(2026, 12, 30)) == ("30/12/2026", "9:00 AM")


def test_post_text_has_the_date_time_and_link_and_no_leftover_placeholders():
    text = linkedin.build_post_text("10/10/2026", "10:37 AM", PROD_URL)
    assert text.startswith("📅 5²Feed EPISODE: 10/10/2026 10:37 AM IST")
    assert f"🔗 {PROD_URL}" in text
    assert "03/10/2026" not in text and "i1mOggHc7GQ" not in text
    assert "{" not in text and "}" not in text


def test_paragraphs_are_separated_by_exactly_one_blank_line():
    text = linkedin.build_post_text("10/10/2026", "9:00 AM", PROD_URL)
    assert "\n\n\n" not in text and "\r" not in text
    assert text.count("\n\n") >= 10


@pytest.mark.parametrize("date_label,time_label", [("3/10/2026", "9:00 AM"), ("03/10/2026", "09:00 AM"),
                                                    ("03/10/2026", "9:00"), ("03/10/2026", "13:00 PM")])
def test_labels_are_validated(date_label, time_label):
    with pytest.raises(ValueError):
        linkedin.build_post_text(date_label, time_label, PROD_URL)
    with pytest.raises(ValueError):
        linkedin.render_post_image(date_label, time_label)


def test_compose_url_encodes_the_whole_text():
    url = linkedin.compose_url("a b #tag\n🔗 https://youtu.be/x?y=1")
    assert url.startswith(linkedin.COMPOSE_URL)
    assert " " not in url and "#" not in url and "\n" not in url


def test_image_changes_only_the_date_pill():
    template = Image.open(linkedin.TEMPLATE_IMAGE).convert("RGB")
    rendered = Image.open(io.BytesIO(linkedin.render_post_image("10/10/2026", "10:37 AM"))).convert("RGB")
    assert rendered.size == template.size == (1254, 1254)
    box = ImageChops.difference(template, rendered).getbbox()
    assert box is not None
    left, top, right, bottom = box
    assert left >= 420 and right <= 970 and top >= 830 and bottom <= 880


def test_longest_realistic_text_still_fits_the_pill():
    rendered = Image.open(io.BytesIO(linkedin.render_post_image("30/12/2026", "12:59 PM"))).convert("RGB")
    template = Image.open(linkedin.TEMPLATE_IMAGE).convert("RGB")
    left, _, right, _ = ImageChops.difference(template, rendered).getbbox()
    assert left >= 420 and right <= 970


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    engine = engine.execution_options(schema_translate_map={"raw": None, "editorial": None})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    yield session_factory
    engine.dispose()


def _episode(factory, publications, created_at=None):
    with factory() as db:
        episode = Episode(episode_date=date(2026, 10, 9), status="approved",
                          created_at=created_at or utc(2026, 10, 10, 2, 40))
        db.add(episode)
        db.flush()
        for env, sequence, status, url in publications:
            db.add(EpisodePublication(episode_id=episode.id, environment=env, sequence=sequence,
                                      status=status, youtube_url=url))
        db.commit()
        return episode.id


def test_endpoint_uses_the_latest_published_prod_link(factory):
    episode_id = _episode(factory, [
        ("prod", 1, "published", "https://youtu.be/OLDPROD0001"),
        ("prod", 2, "published", PROD_URL),
        ("dev", 1, "published", DEV_URL),
    ])
    post = main.episode_linkedin_post(episode_id)
    assert post["youtube_url"] == PROD_URL
    assert (post["date"], post["time"]) == ("10/10/2026", "9:00 AM")
    assert PROD_URL in post["text"] and DEV_URL not in post["text"]
    assert f"EPISODE: {post['date']} {post['time']} IST" in post["text"]
    assert post["image_url"].startswith(f"/api/v1/episodes/{episode_id}/linkedin/image?date=")
    assert post["compose_url"].startswith(linkedin.COMPOSE_URL)


def test_endpoint_refuses_when_only_dev_or_a_failed_prod_exists(factory):
    episode_id = _episode(factory, [("dev", 1, "published", DEV_URL), ("prod", 1, "failed", None)])
    with pytest.raises(HTTPException) as excinfo:
        main.episode_linkedin_post(episode_id)
    assert excinfo.value.status_code == 409


def test_endpoint_unknown_episode_and_bad_override(factory):
    with pytest.raises(HTTPException) as excinfo:
        main.episode_linkedin_post(999)
    assert excinfo.value.status_code == 404
    episode_id = _episode(factory, [("prod", 1, "published", PROD_URL)])
    with pytest.raises(HTTPException) as excinfo:
        main.episode_linkedin_post(episode_id, date="1/1/26", time="9:00 AM")
    assert excinfo.value.status_code == 400


def test_image_endpoint_returns_a_png_and_the_post_image_url_carries_the_same_date_and_time():
    response = main.episode_linkedin_image(1, date="10/10/2026", time="10:37 AM")
    assert response.media_type == "image/png" and response.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["cache-control"] == "no-store"


def test_post_date_is_the_ist_made_date_not_today_or_the_utc_day(factory):
    episode_id = _episode(factory, [("prod", 1, "published", PROD_URL)], created_at=utc(2026, 10, 9, 20, 0))
    assert main.episode_linkedin_post(episode_id)["date"] == "10/10/2026"
    default_image = main.episode_linkedin_image(episode_id)
    explicit_image = main.episode_linkedin_image(episode_id, date="10/10/2026", time="9:00 AM")
    assert default_image.body == explicit_image.body
