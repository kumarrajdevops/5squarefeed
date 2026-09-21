# 5squareFeed

Local-first pipeline for a daily Top-25 (+5 backup) AI technology news
video: ingest → deduplicate → rank/select → script/voice/visual/video
generation → Automated QA → human review (Editorial Dashboard) →
publish to YouTube (Instagram is a future phase).

> **Naming note:** the repo/dev project name (used by Docker Compose,
> container prefixes, etc.) is `5min-ai-news`. "5squareFeed" (domain
> `5squarefeed.in`) is the production/public brand name for the same
> project.

## No LLM in the pipeline

"AI News" describes the subject matter, not the implementation. Every
decision-making and content-generation stage is deterministic and
rule-based -- same input always produces the same output, fully
explainable, nothing that can hallucinate or vary run to run:

| Stage | How it works | AI/LLM involved? |
|---|---|---|
| Ingestion (RSS, Hacker News) | `feedparser`/`requests`, plain HTTP | No |
| AI-relevance filter | Regex keyword matching against a fixed word list | No |
| Deduplication | Title string-similarity + time window, plus full-article-text TF-IDF/cosine similarity (same-batch and against past narrated stories) | No |
| Fact extraction | Keyword/regex matching (companies, products, events, dates, numeric claims) | No |
| Verification (soft signal) | Cross-source count + source credibility threshold | No |
| Ranking | A fixed scoring formula (recency, source credibility, momentum, verification) | No |
| Script generation | String templates from the raw RSS/article text | No |
| Voice synthesis | Microsoft's `edge-tts` neural voice (`en-US-GuyNeural`) | **Yes -- the one exception** |
| Visual card | Pillow drawing text on a static template | No |
| Video composition | ffmpeg | No |
| Automated QA | File/duration checks via `ffprobe` | No |

Voice synthesis is real ML inference (a genuine neural text-to-speech
model, free, no API key) -- but it's pure speech synthesis, not an
LLM. It doesn't understand, generate, or reason about anything; it
just converts the already-deterministic script text into audio.

No OpenAI/Anthropic/Gemini/any LLM SDK appears in `requirements.txt`
or anywhere in `app/` -- every source hit for words like "openai" or
"claude" in the code is either a *news source name* (OpenAI's own blog
is an RSS source) or a *keyword in the topic classifier* (detecting
whether an article is about AI), never a call to an AI API.

## Current slice

- FastAPI API
- PostgreSQL (schema managed by Alembic -- see "First-time setup" below)
- Redis + Celery worker
- Multi-source RSS ingestion (11 enabled sources incl. OpenAI, Google AI,
  Google DeepMind, TechCrunch, The Verge, MIT Technology Review,
  Microsoft Research Blog, NVIDIA, Hugging Face, Ars Technica, Wired)
  with a deterministic AI-relevance filter. A story with no summary of
  its own falls back to fetching the linked article's own description
  (`app/sources/article_fetcher.py`) rather than shipping empty.
- Hacker News ingestion (official Algolia search API) -- resolves the
  real publisher for link-posts (e.g. "The Guardian") instead of
  attributing everything to "Hacker News", so credibility scoring
  reflects the actual outlet
- Deterministic duplicate-story detection, two layers:
  1. Title similarity + time window (fast, in-memory, same-batch only).
  2. Full-article-text similarity (`app/content/article_extractor.py`
     fetches the linked article's real body via `trafilatura`;
     `app/filters/content_similarity.py` scores it with TF-IDF +
     cosine similarity, `scikit-learn` -- classic deterministic
     information retrieval, not an LLM) against **both** today's batch
     (catches cross-outlet duplicates under a completely different
     headline that title-matching alone misses -- same hard exclusion
     as #1, just a stronger signal) **and** the full historical corpus
     of every story ever narrated as primary (catches a *different*
     story repeating an event already told to the public, which the
     identity-based "never re-select" check above can't catch on its
     own -- **soft signal only**, surfaced in the dashboard as a
     "possible repeat" pill rather than a hard exclusion, after live
     testing found real false-positive risk at the current,
     unvalidated similarity threshold; see `TODO.md`). A blocked/
     paywalled/JS-rendered fetch degrades gracefully
     (`content_fetch_status`) and falls back to the short summary --
     never fought, no headless browser, no CAPTCHA-solving, same
     standing rule as the VentureBeat RSS source.
- Fact Extraction (companies, products, event categories, dates,
  numeric claims -- `app/extraction/fact_extractor.py`) and a
  Verification Engine (`app/verification/engine.py`: verified if
  corroborated by another outlet, or from a source credible enough to
  be its own primary source) between dedup and ranking. **Soft signal
  only** -- nothing is excluded from ranking; verification status is
  shown in the dashboard and gives ranking a small score nudge (same
  "surface prominently, human decides" philosophy as Automated QA).
- Multi-factor ranking engine (recency, source credibility, AI
  relevance, cross-source momentum, verification) + Top-25/5-backup
  selection, persisted per run as an "Episode". A story that's already
  been a **primary** (narrated) selection in any earlier episode is
  never selected again, in any future episode -- an unused backup
  (never promoted, never narrated) remains eligible.
- Script/voice/visual/video content generation, free/local (no API
  keys) -- runs per-story or across a full episode, produces one
  combined branded video with intro/outro
- Automated Video QA against the architecture's checklist (story
  count, AI-only, source links, captions/audio present, video
  integrity, duration target)
- Editorial Dashboard -- a browser UI for episode review: watch the
  video, reorder/swap Top 30 stories, edit scripts, run QA, approve/reject

## First-time setup

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec api alembic upgrade head
```

The `alembic upgrade head` step is **required** before the API can
serve any data-backed endpoint -- the app does not auto-create
tables on startup (see `app/main.py`'s `startup()` docstring for why).

## Health check

```bash
curl http://localhost:8000/health
```

OpenAPI docs: http://localhost:8000/docs

## Scheduled Daily News Cycle

The full cycle (three overnight collection passes, then a cutoff that
ranks/produces/QAs the episode) is built and verified, via a `celery
beat` process:

| Time (IST) | What runs |
|---|---|
| 10:00 PM | Collect (RSS + Hacker News) |
| 1:00 AM | Collect again |
| 3:30 AM | Final collection |
| 4:00 AM | **Cutoff**: rank + select Top 25 + 5 backups -> produce the episode video -> run QA |

Human approval (the step before the 6 AM IST publish target) is still a
manual dashboard action -- the scheduled cutoff stops once the episode
is produced and QA'd, ready for review.

**Not started by default** -- real scheduling is a production concern,
not something that should fire unprompted during dev/testing just
because the stack happens to be up at those IST times. Plain
`docker compose up -d` (per "First-time setup" above) brings up
`postgres`/`redis`/`api`/`worker` only. Start the scheduler
deliberately, whenever you actually want it running:

```bash
docker compose up -d beat                  # just the scheduler
# or
docker compose --profile scheduler up -d   # everything, beat included
```

`beat` only decides *when* a task should run and enqueues it -- `worker`
is what executes it, and must be up too. `docker compose stop beat`
turns scheduling back off without touching anything else. Times are
evaluated in `Asia/Kolkata` regardless of the host/container's system
clock (verified directly, not just assumed from config -- see
`TODO.md`).

Everything below (manual ingest/produce/QA endpoints) still works the
same way and is useful for testing, backfilling, or triggering a step
out of cycle.

## Running the pipeline

**1. Ingest news** (pulls from all enabled RSS sources, filters for
AI relevance, then automatically chains into deduplication):

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/rss
```

**2. (Optional) Also ingest Hacker News** (AI-related stories above a
points threshold, official Algolia API, chains into the same
deduplication step):

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/hackernews
```

**3. (Optional) Re-run deduplication manually**, without a full
ingestion cycle (auto-chains into Fact Extraction + Verification, same
as after a real ingestion run):

```bash
curl -X POST http://localhost:8000/api/v1/dedup/run
```

**3b. (Optional) Re-run Fact Extraction + Verification manually**,
without a full dedup pass -- e.g. after backfilling data. Soft signal
only, safe to re-run (only processes stories still at `verification_status: "pending"`):

```bash
curl -X POST http://localhost:8000/api/v1/verification/run
```

**4. Rank + select the Top 25 + 5 backups.** This is intentionally
a separate, manually-triggered step (not auto-chained after
ingestion) -- it's meant to run once, after the daily collection
window closes, not after every ingestion pass:

```bash
curl -X POST http://localhost:8000/api/v1/episodes/select
```

## Producing content (script + voice + visual + video)

**Single story.** Pick a `story_id` (e.g. from `/api/v1/episodes/latest`)
and kick off the full chain -- script generation, then voice synthesis,
then the visual card, then video composition, each auto-chained into
the next:

```bash
curl -X POST http://localhost:8000/api/v1/stories/{story_id}/produce
```

Poll for progress/results (`status` moves through `pending` ->
`script_ready` -> `voice_ready` -> `visual_ready` -> `video_ready`,
or `failed` -- see `error_message`):

```bash
curl http://localhost:8000/api/v1/stories/{story_id}/content
```

**Full episode.** Produce (or reuse) content for every primary story
in an episode and concatenate the results into one combined video, in
rank order:

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/produce
```

Idempotent -- stories that already have `video_ready` content are
reused, not regenerated, so re-running after adding a few new stories
only produces what's missing. Fault-isolated -- a story whose pipeline
fails is skipped from the final video rather than blocking the whole
episode. `video_status` flips to `"producing"` synchronously, before
the endpoint even returns (closes a race where a poll landing before
the background task starts could mistake "not started yet" for
"already finished"). Poll `GET /api/v1/episodes/{episode_id}` for
`video_status` (`pending` -> `producing` -> `ready`, or `failed`),
`video_url`, and `video_produced_at`.

After the primary video is ready, the 5 backup stories are also
produced (or reused), best-effort, as a second phase that can never
delay or block the primary episode -- so a later dashboard swap
(promoting a backup to primary) is instant instead of triggering a
slow on-demand regeneration.

Both endpoints' responses include playable URLs (served from `/media`,
e.g. `http://localhost:8000/media/videos/{story_id}.mp4` or
`.../videos/episode_{episode_id}.mp4`) for the generated audio, image,
captions, and video. The combined episode video opens with a narrated
intro card ("5squareFeed -- [date] -- 25 Stories A Day") and closes
with a narrated outro ("That's all for today's 5squareFeed. See you
tomorrow.") -- still a straight concatenation otherwise, no transitions
or background music.

**Automated Video QA.** Once an episode is produced, validate it
against the architecture's QA checklist (story count, AI-only, source
links, captions/audio present, video integrity, duration target):

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/qa
```

Poll `GET /api/v1/episodes/{episode_id}` for `qa_status`
(`pending` -> `passed`/`failed`), `qa_report` (per-check pass/fail
detail), and `qa_run_at`. `source_verification` still always reports
as not implemented -- the Verification Engine now exists (see below),
but this specific QA check hasn't been wired up to consume its data
yet -- rather than faking a pass.

A QA result is stale once the episode is reproduced afterward
(`video_produced_at > qa_run_at`) -- the dashboard surfaces this as a
warning on its Run QA button rather than the API enforcing it.

All four stages are free/local, no API keys required:

- **Script** -- deterministic, template-based: headline + a
  deterministic summary of the story, nothing more (no editorializing
  or speculative "why it matters" commentary). Same pattern as the
  AI-relevance/dedup filters. For Hacker News link-posts (HN's API has
  no article content, only submission metadata), the summary is
  fetched from the linked article's own `og:description`/`meta
  description`/first paragraph (`app/sources/article_fetcher.py`)
  rather than falling back to "N points, M comments on Hacker News."
  Promotional/newsletter-pitch sentences ("subscribe", "sign up",
  etc.) are filtered out of any summary before narration.
- **Voice** -- [edge-tts](https://github.com/rany2/edge-tts) (free
  Microsoft neural TTS, one branded voice for every story). Current
  voice: `en-US-GuyNeural`, still under review -- shortlisted
  candidates being compared for the final pick: `en-US-AriaNeural`,
  `en-US-JennyNeural`, `en-GB-RyanNeural` (see `TODO.md` for the full
  sample-comparison process across edge-tts's ~46 English voices).
- **Visual** -- a branded title card rendered with Pillow.
- **Video** -- ffmpeg composes the image + audio + burned-in captions
  into an mp4, hard-capped to the real audio duration (`-t
  <duration>`, not just `-shortest` -- see `TODO.md` for why that
  matters). A silent 0.5s clip is inserted between consecutive stories
  in the combined episode video for pacing. Captions are timed with
  edge-tts's own real per-sentence timing (`SentenceBoundary` events
  captured during synthesis), not an estimate -- see `TODO.md`.

## Editorial Dashboard

A browser UI for the human-in-the-loop review step -- browse episodes,
watch the produced video, review/reorder/edit the Top 30, check QA,
and approve or reject:

```text
http://localhost:8000/dashboard/
```

v1 is deliberately lightweight: vanilla HTML/CSS/JS
(`app/dashboard/`) served directly by FastAPI, no build step, no new
dependencies. Partially click-tested in a real browser via
`claude-in-chrome`; several real bugs (a Produce status race, stale
cached video playback, an edit-clobbering bug -- see `TODO.md`) were
only caught through actual live use once that browser connection
dropped mid-session, not through automated verification alone.

Carries the real 5squareFeed brand: favicon, apple-touch-icon, and a
`site.webmanifest` (generated from the real logo -- see
`app/dashboard/branding/` for the source assets and every generated
size) so it's installable as a home-screen "app" with the actual icon,
not a generic browser tab.

**What you can do from it:**

- **Browse episodes** -- list view links into each episode's Studio view.
- **Watch the video** -- native player; click a story's rank number to
  jump playback to roughly that point (computed from intro + preceding
  stories' narration durations).
- **Reorder the Top 30** -- drag a story within Primary or Backup to
  re-rank it (native HTML5 drag-and-drop, no library).
- **Swap in a backup** -- drag a Backup story onto a Primary slot to
  replace it (the "defective story" swap from the architecture's
  Top-30 Safety Mechanism). Since backups are pre-produced (see
  above), the swap is instant, no regeneration wait.
- **Edit a script** -- click a story's title to open headline/summary/
  script text as editable fields, alongside the source article link
  (opens in a new tab, plus a one-click Copy button) and its metadata
  (source, author, published/collected timestamps, verification
  status/reason, extracted facts, and -- when the story was surfaced
  via an aggregator like Hacker News rather than ingested directly --
  a "Discovered via" link back to that discussion thread). Saving
  invalidates that story's audio/visual/video so the next Produce
  regenerates them **from the edited script** (Produce no longer
  silently regenerates and overwrites a saved edit -- see `TODO.md`).
- **Proofread against the source** -- every story in the Top 30 list
  also shows its source article link (new tab + Copy button) directly
  below the title, not just in the edit panel.
- **See verification status at a glance** -- a second pill next to each
  story's content status (verified = green, unverified = amber) with
  the reason as a tooltip. Soft signal only -- nothing is hidden or
  excluded, it's there so the editor can make an informed call.
- **Produce / Run QA** -- both buttons disable and show a live
  elapsed-time progress indicator while running, then auto-refresh
  the view when done (Produce polls `video_status`, QA polls
  `qa_run_at` against click time). Run QA shows an amber "stale"
  warning whenever the episode's been reproduced since QA last ran.
- **Approve / Reject** -- sets the episode's overall status. Doesn't
  hard-block on a failing QA result -- QA is surfaced prominently, but
  the human makes the final call.
- **Episode JSON (audit)** -- the exact API response the page rendered
  from, at the bottom of the Studio view: view, copy, or download it
  (`episode_{id}.json`) for a paper trail independent of the UI.

**The same actions as raw API calls** (for scripting, or anything the
UI doesn't cover):

```bash
# List every episode
curl http://localhost:8000/api/v1/episodes

# Edit a story's script (only provided fields change)
curl -X PATCH http://localhost:8000/api/v1/stories/{story_id}/content \
  -H "Content-Type: application/json" \
  -d '{"script_text": "..."}'

# Reorder one group's (primary or backup) full rank order
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/reorder \
  -H "Content-Type: application/json" \
  -d '{"story_ids": [15, 16, 22, ...]}'

# Swap a backup into a primary slot
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/swap \
  -H "Content-Type: application/json" \
  -d '{"primary_story_id": 39, "backup_story_id": 21}'

# Approve / reject
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/approve
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/reject
```

Instagram publishing, analytics, and the fuller Next.js-based vision
from `dashboard-proposal.md` are a separate, later workstream -- see
`TODO.md`. YouTube publishing is built (see below).

## Publishing (YouTube)

A **Publish to YouTube** button appears in the Studio view, next to
Approve/Reject -- manual only, same human-in-the-loop philosophy as
every other stage here: publishing is the one action with a real,
externally-visible side effect, so it never auto-cascades from
Approve.

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/publish
```

Requires the episode to already be `approved` with a `ready` video (400
otherwise). Uploads the real combined episode video via the YouTube
Data API v3's resumable upload, with a title/description/tags built
deterministically from the episode's actual Top 25 (headline + source
+ link per story, same pattern as everything else in this pipeline --
see `build_video_metadata()` in `app/publishing/youtube_publisher.py`).
New uploads default to **private** visibility -- there's no visibility
control in the dashboard yet, so a human always makes a video
public/unlisted deliberately via YouTube Studio afterward, never
automatically on first publish.

**Requires real Google OAuth credentials, which this project does not
ship with** -- the one exception to "free/local, no API keys" (see
above): publishing to a real channel inherently needs a real account.

**Dev and prod are deliberately fully separate** -- different Google
Cloud OAuth clients *and* different destination YouTube channels, not
just different secrets pointed at the same channel. Reason: YouTube's
API quota (10,000 units/day by default; one upload costs ~1,600) is
tracked per Google Cloud project, not per channel -- sharing prod's
credentials for dev testing risks burning the day's quota on test
uploads and blocking a real publish. A separate test channel also
means dev's `private`-visibility test uploads never clutter the real
channel's video library. `YOUTUBE_ENVIRONMENT` (`dev` or `prod`, in
`.env`) picks which credential pair is actually live -- both can be
configured at once, so switching modes never means editing secrets.
The dashboard shows which one is active as a pill next to the Publish
button (red/bold for `prod`, impossible to miss).

**Current real status**: dev is fully set up and verified -- a real
`/publish` call successfully uploaded episode #9 to the dev channel
(private visibility), confirmed via the API response
(`publish_status: "published"`, `publish_error: null`) and the worker
log explicitly showing `YOUTUBE_ENVIRONMENT='dev'` was used throughout.
That was the first real network call this integration ever made; the
whole build up to that point (title/description generation, the upload
call, DB status tracking, dashboard polling UI, the `YouTubeNotConfigured`
fail-fast path, dev/prod credential switching) had only been verified
without real credentials. **Prod is not set up yet** -- repeat the same
steps below with a `YOUTUBE_PROD_*` pair and the real "5squareFeed"
channel whenever ready to go live.

### One-time setup (run once per environment: dev now, prod later)

None of this can be done by an AI assistant -- it all needs your own,
live, authenticated Google session.

**Part 1 -- create the channel** (dev needs its own, separate from
whatever prod will eventually use):

1. Sign in to [youtube.com](https://youtube.com) as the Gmail that
   owns this brand (e.g. `5squarefeed@gmail.com`).
2. Profile picture (top right) -> **Settings** (gear icon).
3. **"Add or manage your channel(s)"** -> **"Create a channel"**.
4. Choose **"Use a custom name"** (not your personal name) -- this
   creates a separate Brand Account channel, not tied to your personal
   profile, and is what lets one Google account manage multiple
   distinct channels.
5. Name it unmistakably not-the-real-thing for dev, e.g.
   **"5squareFeed Dev"** / **"5squareFeed (Test)"**. Optionally set it
   unlisted and add a description noting it's an internal test
   channel. Do the same later for prod with the real "5squareFeed" name.

**Part 2 -- create the Google Cloud project + OAuth client** (one per
environment; never reuse prod's project/client for dev):

1. [console.cloud.google.com](https://console.cloud.google.com/),
   signed in as the same account.
2. Top-left project dropdown -> **"New Project"** -> name it e.g.
   **"5squareFeed Dev"** -> Create, then make sure it's selected in the
   dropdown before continuing.
3. **Enable the API**: "APIs & Services" -> "Library" -> search
   **"YouTube Data API v3"** -> **Enable**.
4. **Configure the OAuth consent screen**: "APIs & Services" ->
   "OAuth consent screen":
   - User type: **External**
   - App name / support email / developer email: e.g. "5squareFeed Dev"
     / `5squarefeed@gmail.com`
   - Scopes step: skip/save, not required here
   - **Test users**: add your own Gmail here -- **required**, since
     this app stays in "Testing" publishing status (unverified); Google
     blocks sign-in for any account not explicitly listed here.
5. **Create the OAuth client**: "APIs & Services" -> "Credentials" ->
   **"+ Create Credentials"** -> **"OAuth client ID"**:
   - Application type: **Desktop app**
   - Name: e.g. "5squareFeed Dev Desktop Client"
   - Create -> copy the **Client ID** and **Client Secret** shown.
6. Put them in `.env`: dev as `YOUTUBE_DEV_CLIENT_ID`/
   `YOUTUBE_DEV_CLIENT_SECRET`, prod (later) as
   `YOUTUBE_PROD_CLIENT_ID`/`YOUTUBE_PROD_CLIENT_SECRET`.

**Part 3 -- tie them together (get the refresh token)**:

1. Confirm `.env` has `YOUTUBE_ENVIRONMENT=dev` and both dev fields
   from Part 2 filled in.
2. On your **host machine** (not inside Docker -- this needs a real
   browser):
   ```bash
   pip install google-auth-oauthlib google-api-python-client google-auth
   python -m app.scripts.youtube_oauth_setup
   ```
3. It opens a browser to Google's consent screen. Sign in as the
   account from Part 1/2. You'll see an "unverified app" warning --
   click "Advanced" -> "Go to \<app name\> (unsafe)" (expected and
   fine, it's your own app in Testing mode).
4. **If it asks which channel/brand account to use, pick the channel
   from Part 1** -- this is the step that actually links the credential
   to the right channel. If it doesn't ask (sometimes it just uses the
   account's only/default channel), verify afterward by checking which
   channel the first real published video actually landed on.
5. The script prints a refresh token -- save it as
   `YOUTUBE_DEV_REFRESH_TOKEN` (or `YOUTUBE_PROD_REFRESH_TOKEN` for
   the prod run).
6. **Restart is not enough** -- Docker Compose only re-reads `.env` on
   container *creation*, not a plain restart. After editing `.env`,
   run:
   ```bash
   docker compose up -d --force-recreate api worker
   ```
   Confirm it took: `docker compose exec api python -c "from app.config import settings; print(settings.youtube_configured)"`
   should print `True`.
7. **Complete YouTube's own one-off link verification, per channel**
   -- separate from everything above, and easy to miss: a brand-new
   channel's very first video(s) will have source links in the
   description show up as **plain text, not clickable**, even though
   the description YouTube received is completely correct (verified
   directly: no truncation, clean ASCII spacing, no invisible
   characters). This is a real YouTube-side gate, confirmed via the
   message *"To make external links clickable, first complete a
   one-off verification"* -- complete it once per channel (in YouTube
   Studio; it's channel-level, not something in this repo). Confirmed
   fixed on the dev channel: after completing it, the *same*
   already-published video's links became real, clickable links
   immediately, no republish needed. **Do this for the prod channel
   too**, the first time you publish for real, or its links will look
   broken to viewers.

Until the currently-selected pair is fully set, `/publish` fails fast
with a clear `YouTubeNotConfigured` error -- before ever touching the
network -- rather than an opaque auth failure.

## Notifications

A narrow, deliberately-not-noisy failure-alert system (see
`app/notifications/notifier.py`) -- only two things trigger one:
episode video production totally failing, or a YouTube publish
failing. Nothing else does: a single story's content generation
failing is already tolerated by design (that story is just excluded),
and Automated QA's `duration_target` check failing is a known,
documented limitation, not a real problem -- alerting on either would
just be noise.

```bash
# Every alert ever recorded, newest first
curl http://localhost:8000/api/v1/notifications
```

Every alert is recorded in the `notifications` table regardless of
delivery -- that's the durable audit trail. Real-time delivery is via
a Slack Incoming Webhook, optional:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Create one at [api.slack.com/apps](https://api.slack.com/apps) → your
app → **Incoming Webhooks** → activate → **Add New Webhook to
Workspace** → pick the channel. (Not a Slack app with OAuth bot scopes
-- if it offers you an access token *and* a refresh token, that's
**Token Rotation**, a different and more complex setup than this
project supports today, since the refresh token itself changes on
every use and would need somewhere durable to live besides `.env`.
Use the plain Incoming Webhook feature instead.) Each `Notification`
row's `delivered` field reflects whether the Slack post actually
succeeded -- `false` both when no webhook is configured and when the
post itself fails; a Slack outage never breaks the pipeline's own
failure handling, since delivery is always best-effort.

## Inspecting results

```bash
# All AI-candidate stories, deduplicated (canonical only)
curl http://localhost:8000/api/v1/stories

# Every story grouped as a duplicate of a given canonical story
curl http://localhost:8000/api/v1/stories/{story_id}/duplicates

# Every episode, newest first (id, status, video/QA status, counts)
curl http://localhost:8000/api/v1/episodes

# The most recently selected episode (Top 25 + backups, with scores
# and per-story ranking rationale)
curl http://localhost:8000/api/v1/episodes/latest

# A specific episode by id
curl http://localhost:8000/api/v1/episodes/{episode_id}
```

## Running tests

```bash
docker exec 5squarefeed-api-1 pip install -r requirements-dev.txt
docker exec -w /app 5squarefeed-api-1 pytest
```

98 tests, no running Postgres required -- DB-backed tests use an
in-memory SQLite database (`tests/conftest.py`'s `db_session` fixture;
every model uses portable column types, so this is a faithful stand-in)
rather than the real dev database. Covers the deterministic filters
(AI-relevance, dedup, ranking, script generation, fact extraction,
verification -- including the promo-sentence/truncation-marker fixes
from an earlier session) plus direct regression tests for the three
hardest-won bugs found in that session: the AV-duration desync (real
ffmpeg, not mocked), the script-clobbering bug, and the ingestion race
condition. Each regression test was
verified to actually fail when its bug is reintroduced, not just pass
tautologically.

Not yet covered: the Celery task orchestration layer itself (e.g.
`produce_episode_video`, `ingest_news` end to end) and the FastAPI
endpoints -- see `TODO.md`.

## Stop / reset

```bash
docker compose down       # stop, keep data
docker compose down -v    # stop and wipe the database volume
```

## Contributing / guardrails

`CLAUDE.md` documents dev-environment gotchas and hard rules distilled
from real bugs found in this project (script-clobbering, stale cached
media, background-task status races, ffmpeg duration overrun, and
more) -- read it before touching the content/video pipeline or
dashboard. `.claude/skills/verify-episode/` and
`.claude/agents/episode-verifier.md` codify the AV-sync/QA/idempotency
checklist used to catch and verify those bugs, for reuse on future
pipeline changes.