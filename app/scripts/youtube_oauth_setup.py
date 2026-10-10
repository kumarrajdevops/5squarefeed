# One-time interactive script to obtain a YouTube Data API v3 refresh
# token for the Publishing Worker (app/tasks/publishing.py). Celery
# workers run headless and can't complete an interactive OAuth consent
# flow themselves -- this script does it once, on a machine with a real
# browser, and prints the resulting refresh token to paste into .env.
#
# Run this on your HOST machine, not inside a Docker container --
# it opens a real browser window, which a container can't do:
#   pip install google-auth-oauthlib google-api-python-client google-auth
#   python -m app.scripts.youtube_oauth_setup
#
# Before running, you need a Google Cloud OAuth client (see README.md's
# Publishing section for the full setup): a Cloud project with the
# YouTube Data API v3 enabled, an OAuth consent screen configured, and
# an OAuth Client ID of type "Desktop app". Put its client_id/secret in
# .env as YOUTUBE_DEV_CLIENT_ID/YOUTUBE_DEV_CLIENT_SECRET (or the
# YOUTUBE_PROD_* equivalents) before running this -- whichever pair
# YOUTUBE_ENVIRONMENT currently points at is what gets used, and this
# script tells you exactly which one, and which env var to save the
# resulting refresh token under, so dev and prod never get crossed.
#
# Run it TWICE total, once per environment: set YOUTUBE_ENVIRONMENT=dev
# in .env, run this, save the printed token, then set
# YOUTUBE_ENVIRONMENT=prod, run it again against the prod OAuth client
# and the real channel, and save that token too. Both pairs can then
# coexist in .env -- YOUTUBE_ENVIRONMENT just picks which is live.

from app.config import settings

# youtube.readonly is only used here, to print which channel the token belongs to.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def authorized_channel(access_token: str) -> tuple[str, str]:
    import json
    import urllib.request

    request = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        items = json.load(response).get("items", [])
    if not items:
        raise RuntimeError("the signed-in account has no YouTube channel")
    return items[0]["id"], items[0]["snippet"]["title"]


def run_oauth_setup() -> None:
    env = settings.youtube_environment
    token_var_name = f"YOUTUBE_{env.upper()}_REFRESH_TOKEN"

    if not settings.youtube_client_id or not settings.youtube_client_secret:
        print(
            f"YOUTUBE_ENVIRONMENT is currently {env!r}, but "
            f"YOUTUBE_{env.upper()}_CLIENT_ID and "
            f"YOUTUBE_{env.upper()}_CLIENT_SECRET aren't both set in .env -- "
            "see README.md's Publishing section."
        )
        return

    print(f"Running OAuth setup for YOUTUBE_ENVIRONMENT={env!r}.")
    print(
        "Make sure you sign in, in the browser window this opens, with the "
        "Google account that owns the channel you want THIS environment to "
        "publish to -- dev and prod should be different accounts/channels.\n"
    )

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # Opens your default browser for the Google consent screen and
    # runs a temporary local server to catch the redirect.
    # select_account makes Google always show the account chooser instead of silently reusing
    # whichever account the browser is signed into (which sent prod uploads to the dev channel).
    credentials = flow.run_local_server(port=0, prompt="select_account consent")

    channel_id, channel_title = authorized_channel(credentials.token)
    print(f"\nThis token publishes to the channel: {channel_title!r} ({channel_id})")
    print(f"Check that this is the channel you want {env!r} to publish to BEFORE saving it.")
    print(f"\nIf it is, add this line to .env:\n")
    print(f"{token_var_name}={credentials.refresh_token}\n")


if __name__ == "__main__":
    run_oauth_setup()
