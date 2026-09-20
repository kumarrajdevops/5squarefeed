from datetime import date
from pathlib import Path

from app.config import settings


BRAND_NAME = "5squareFeed"

# YouTube's real limits: title <= 100 chars, description <= 5000 chars.
# MAX_DESCRIPTION_CHARS leaves headroom rather than hard-coding exactly
# 5000 -- a real external constraint worth guarding defensively, unlike
# most of this codebase's "no error handling for things that can't
# happen" stance, since a 25-story description genuinely can approach
# the limit depending on real headline/URL lengths.
MAX_DESCRIPTION_CHARS = 4900


def build_video_metadata(run_date: date, stories: list[dict]) -> dict:
    """
    Build the YouTube video title/description/tags for one episode --
    a pure function (no network, no DB), same "keep the deterministic
    logic pure and testable" pattern as compute_total_score()
    (app/ranking/engine.py) and extract_facts()
    (app/extraction/fact_extractor.py).

    `stories` is the episode's primary (Top 25) list in rank order,
    each a {"headline", "source_name", "url"} dict -- exactly the
    shape app/tasks/publishing.py already has on hand from
    EpisodeStory/Story, so no ORM objects need to leak into this
    module.
    """

    formatted_date = run_date.strftime("%B %d, %Y")
    title = f"{BRAND_NAME} — {formatted_date} — Today's Top {len(stories)} AI Stories"

    lines = [
        f"{BRAND_NAME} for {formatted_date}: today's top AI stories, "
        "auto-curated and narrated. Sources for every story below.",
        "",
    ]
    for index, story in enumerate(stories, start=1):
        lines.append(
            f"{index}. {story['headline']} ({story['source_name']}) — {story['url']}"
        )

    lines += [
        "",
        f"{BRAND_NAME} is produced by an automated news pipeline: stories are "
        "collected, deduplicated, fact-checked, and ranked deterministically -- "
        "no editorializing, no AI-generated commentary.",
    ]

    description = "\n".join(lines)
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 3].rstrip() + "..."

    tags = ["AI News", "Artificial Intelligence", BRAND_NAME, "Tech News", "Daily AI News"]

    return {"title": title, "description": description, "tags": tags}


class YouTubeNotConfigured(Exception):
    """
    Raised when a publish is attempted with no YouTube OAuth
    credentials configured (YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET/
    YOUTUBE_REFRESH_TOKEN in .env) -- fail fast with a clear reason
    before ever touching the network, rather than surfacing an opaque
    auth error from deep inside the google client library.
    """


def _get_client():
    if not settings.youtube_configured:
        raise YouTubeNotConfigured(
            "YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET/YOUTUBE_REFRESH_TOKEN are not "
            "all set in .env -- see README.md's Publishing section and run "
            "app/scripts/youtube_oauth_setup.py once to obtain a refresh token."
        )

    # Imported lazily so importing this module (e.g. for
    # build_video_metadata() in tests) never requires the google client
    # libraries to be functional, only installed.
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials(
        token=None,
        refresh_token=settings.youtube_refresh_token,
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=credentials)


def upload_video(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str = "private",
) -> dict:
    """
    Upload `video_path` to the configured YouTube channel via the
    YouTube Data API v3's resumable upload flow. Raises
    YouTubeNotConfigured before touching the network if credentials
    aren't set.

    privacy_status defaults to "private": this pipeline has no manual
    visibility control yet (see TODO.md's Publishing Worker entry), so
    every upload starts private and a human deliberately makes it
    public/unlisted later via YouTube Studio -- never auto-public on
    first publish.
    """
    from googleapiclient.http import MediaFileUpload

    client = _get_client()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            # YouTube category 28 = "Science & Technology".
            "categoryId": "28",
        },
        "status": {"privacyStatus": privacy_status},
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    request = client.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _status, response = request.next_chunk()

    video_id = response["id"]
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
