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
| Classification (AI relevance) | Deterministic rules gate (`rules-v1`): non-news patterns reject, an AI term plus a development verb in the title is a candidate, anything uncertain goes to an editor review lane | No |
| Deduplication | Title string-similarity + time window, plus full-article-text TF-IDF/cosine similarity within the same batch | No |
| Historical dedup | Local sentence embeddings (`fastembed`, `bge-small-en-v1.5`, ONNX CPU, no vector DB) plus deterministic rules decide whether a story reports a development already published; the same company/product alone is never a duplicate | No LLM (small local embedding model) |
| Fact extraction | Keyword/regex matching (companies, products, events, dates, numeric claims) | No |
| Verification (soft signal) | Cross-source count + source credibility threshold | No |
| Ranking | A fixed scoring formula (recency, source credibility, momentum, verification) | No |
| Script generation | String templates from the raw RSS/article text | No |
| Voice synthesis | Microsoft's `edge-tts` neural voice (`en-US-JennyNeural`) | **Yes -- the one exception** |
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
- Deterministic taxonomy classification (`app/extraction/taxonomy.py`)
  -- every story gets one of 5 category labels (Major News, Research,
  Security/Policy, Business, Developer/Tools), shown as a dashboard
  pill. Keyword-based (extends the same event categories Fact
  Extraction already computes), not an LLM. **Labels only** -- doesn't
  change which 25 stories get selected; see `TODO.md` for the
  budget/diversity-selection idea this could feed into later.
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

Collection targets a strict calendar-day model, not a rolling window:
`target_date = today's IST calendar date - 1 day` (`app/dates.py`'s
`target_collection_date()`). The full cycle (three overnight
collection passes, then a processing pass) is built and verified, via
a `celery beat` process:

| Time (IST) | What runs |
|---|---|
| 10:00 PM | Collect (RSS + Hacker News) into `raw.news_items` -- no editorial judgment yet |
| 1:00 AM | Collect again (same `target_date`, repeatable/idempotent) |
| 3:30 AM | Final collection |
| 4:00 AM | **Process**: classify -> dedup -> content-dedup -> historical-dedup -> verification -> rank/select Top 25 + 5 backups -> produce the episode video -> run QA |

Collection and processing are deliberately separate operations --
collecting never creates anything editorial by itself; processing is
the one place `raw.news_items` turns into an `editorial.episodes` row.
**Not solved by this design**: IST midnight falls between the 10 PM and
1 AM passes, so on any given real calendar day they don't both compute
the same `target_date` (see `app/worker/celery_app.py`'s beat_schedule
comment for the exact consequence) -- a production scheduler redesign,
deferred intentionally.

Human approval (the step before the 6 AM IST publish target) is still a
manual dashboard action -- the scheduled processing pass stops once the
episode is produced and QA'd, ready for review.

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

### Dev usage: "Collect New Stories" / "Process Episode" dashboard buttons

The Episodes list page shows two buttons, but only when `APP_ENV` is
`local` or `dev` (`.env`'s `APP_ENV`, exposed via `GET /health`'s
`app_env` field):

**Collect New Stories** calls `POST /api/v1/collection/run`, which
queues the single `run_collection` task (RSS + Hacker News together,
same as the scheduled cycle) for `target_collection_date()` (today IST
- 1 day). It:

- Stores whatever's currently available from RSS + Hacker News into
  `raw.news_items`, so you don't have to wait for the scheduled IST
  times to get stories to test with.
- Writes **only** raw data -- no `ai_relevance`, no dedup, no ranking,
  nothing editorial. Never auto-chains into processing.
- Shows a spinner + elapsed timer, then polls
  `GET /api/v1/tasks/{task_id}/result` for the real result once the
  task finishes: real per-source `seen`/`inserted`/`updated` counts and
  the actual `target_date` collected for -- not just "queued". The
  result also durably lands in `raw.collection_runs`, readable anytime
  via `GET /api/v1/raw/collection-runs?collection_date=YYYY-MM-DD`
  (useful after the button's own poll window has passed).

**Process Episode** calls `POST /api/v1/episodes/select` with no date
(so it targets the same `target_collection_date()` Collect just used),
which is the processing trigger described under "Running the pipeline"
below -- classify -> dedup -> content-dedup -> historical-dedup -> verification ->
rank/select -> produce -> QA, all as one task. This can take several
minutes (real video production, not just data processing) -- the
button polls for up to 10 minutes and shows a summary of every stage's
result once finished, then refreshes the episode list.

These are two deliberately separate clicks, in order -- Process is
never auto-run after Collect (see `app/tasks/collection.py`'s
docstring for why collection and processing are kept as distinct
operations).

**In production** (`APP_ENV=prod`), `POST /api/v1/collection/run`
rejects manual requests with `403 Forbidden` ("Manual ingestion is
only available in local/dev environments"), whether or not the button
is visible -- production collection only ever happens via the
scheduled Celery Beat cycle above, which calls the same task function
directly and is completely unaffected by this restriction.

## Running the pipeline

**1. Collect news** (RSS from all enabled sources + Hacker News,
together, as one operation -- writes only `raw.news_items` +
`raw.collection_runs`, nothing editorial, never auto-chains into
processing):

```bash
curl -X POST http://localhost:8000/api/v1/collection/run
```

Optionally pass `?target_date=YYYY-MM-DD` (DEV-only) to collect for an
explicit day instead of the default `target_collection_date()` (today
IST - 1 day). Check what a past run actually did any time via:

```bash
curl "http://localhost:8000/api/v1/raw/collection-runs?collection_date=YYYY-MM-DD"
```

**2. Process the episode.** Reads `raw.news_items` for the target date
and runs classify -> title-dedup -> content-dedup -> historical-dedup
(semantic) -> Fact Extraction + Verification -> rank/select the Top 25
+ 5 backups -> produce the episode video -> run Automated QA, all as
one task (`app/tasks/scheduled.py`'s `run_daily_processing`). This is
intentionally a separate, manually-triggered step from Collection --
it's meant to run once, after the daily collection window closes, not
after every collection pass:

```bash
curl -X POST http://localhost:8000/api/v1/episodes/select
```

**`episode_date` is the coverage day, not the day the episode was
made** (`target_collection_date()` = IST today - 1). Selection, the
unique constraint, the dashboard list/API and the YouTube metadata all
key on it. The moment the episode was actually made is
`editorial.episodes.created_at` (UTC timestamptz; with
`video_started_at`/`video_produced_at`/`publish_started_at`/
`published_at` alongside it). The video intro shows this made-date (IST), bold, just below the tagline.

**Idempotent per `episode_date`** -- `editorial.episodes.episode_date`
has a `uq_editorial_episodes_episode_date` unique constraint, and this
endpoint checks for an existing episode before queuing anything: no
existing episode creates one as before (200, `{"task_id": ...,
"status": "queued"}`); an existing **draft** is reused as-is,
unchanged, with no new task queued (200, `{"created": false, "reason":
"existing_draft_reused"}`); an existing **rejected**, **approved**, or
**published** episode is blocked outright (409) -- normal selection
can never resurrect a rejection, invalidate an approval, or touch
anything already live. To deliberately redo a draft/rejected episode's
selection, use the explicit reprocess endpoint instead of calling
`/select` again:

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/reprocess
```

Reprocess replaces that episode's story selection in place (never
creates a second episode) and resets its `video_status`/`qa_status`
back to `pending`. It's blocked the same way for approved/published
episodes -- there is no override.

## Producing content (canonical enhanced Pillow/storyboard renderer)

There is ONE story-video renderer in the codebase --
`render_story_enhanced()` (`app/content/episode_renderer.py`) --
storyboard-driven scenes (photos are held static -- no pan/zoom), a
light-icon watermark, real ASS captions (deterministic keyword emphasis,
subordinate style during statistic scenes), a 0.32s dark scene-to-scene
crossfade, no scene/story outro. Every story-production path below
renders through it; none duplicates it.

**Single story.** Pick a `story_id` (e.g. from `/api/v1/episodes/latest`)
and produce its own standalone enhanced video -- one task: ensure
content/audio/captions, ensure a valid storyboard (reuse if
valid/current, regenerate + QA if missing/stale), render, mux this
story's own processed narration on top:

```bash
curl -X POST http://localhost:8000/api/v1/stories/{story_id}/produce
```

Poll for progress/results (`status` moves through `pending` ->
`script_ready` -> `voice_ready` -> `video_ready`, or `failed` -- see
`error_message`; there is no separate `visual_ready` stage, unlike the
old single-card renderer this replaced):

```bash
curl http://localhost:8000/api/v1/stories/{story_id}/content
```

`video_url` in that response points at
`media/pillow_enhanced/{story_id}_pillow_enhanced.mp4`.
`image_url`/`captions_url` are always `null` for a story produced this
way -- no single title-card image, no separate downloadable `.srt`
(captions are burned via ASS directly into the video).

**Full episode.** Produce (or reuse) content + storyboard + enhanced
video for every primary story in an episode, then assemble one combined
episode video, in rank order:

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/produce
# or, the identical task under its clearer PROCESS-stage name:
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/process
```

Idempotent -- a story whose content/storyboard is already valid is
reused, not regenerated. Unlike the old single-card renderer, this is
**all-or-nothing at the content/storyboard prerequisite stage**: every
selected story's content+storyboard is checked *before* any rendering
begins, and a genuine failure for even one story stops the whole run
with a clear error rather than silently excluding that story and
continuing (a `qa_failed` storyboard is not fatal -- it still means a
usable storyboard+video exist). `video_status` flips to `"producing"`
synchronously, before the endpoint even returns (closes a race where a
poll landing before the background task starts could mistake "not
started yet" for "already finished"). Poll
`GET /api/v1/episodes/{episode_id}` for `video_status`
(`pending` -> `producing` -> `ready`, or `failed`), `video_url`, and
`video_produced_at`.

After the primary video is ready, the 5 backup stories' content +
storyboard are also ensured, best-effort, as a second phase that can
never delay or block the primary episode -- so a later dashboard swap
(promoting a backup to primary) reuses that work instead of triggering
a slow on-demand regeneration. (Backups only pre-warm the storyboard
prerequisite, not a full enhanced+audio-muxed clip -- that artifact
isn't cached/reused across runs regardless, so there's nothing to gain
from rendering it early.)

The combined episode video opens with a light-surface branded intro
card (the 5squareFeed lockup, loaded by path from
`app/dashboard/branding/logo/5squarefeed-logo-primary.png`, plus the made-date under the tagline) with a
spoken, captioned greeting ("Good morning! Welcome to 5squareFeed. Today's
Top Tech Headlines."), has a 0.6s left-to-right sweep transition + soft glass-ping SFX at every boundary (intro -> story, story -> story, story -> outro), and closes
with the same lockup card and a spoken, captioned sign-off ("Thanks for
watching. See you tomorrow on 5squareFeed.") -- one continuous master audio
track underneath (processed voice per story plus the two greetings, a ducked
ambient music bed, a soft glass-ping SFX at each of the three boundaries; the old
intro/outro chimes were removed -- see `TODO.md` for how to restore them). The
greetings use the same edge-tts voice, are synthesised once and cached in
`media/audio/bumpers/`, and the cards have fixed lengths
(`INTRO_DUR=7.5`, `OUTRO_DUR=6.5` in `episode_renderer.py`, mirrored by
`INTRO_LEAD_IN_SECONDS` in `app.js`).

**Automated Video QA.** Once an episode is produced, validate it
against the architecture's QA checklist (story count, AI-only, source
links, captions/audio present, video integrity, duration target):

```bash
curl -X POST http://localhost:8000/api/v1/episodes/{episode_id}/qa
```

Poll `GET /api/v1/episodes/{episode_id}` for `qa_status`
(`pending` -> `passed`/`failed`), `qa_report` (per-check pass/fail
detail), and `qa_run_at`. `source_verification` is wired to the
Verification Engine's real per-story status (see below) -- not a
placeholder. `captions_present` accepts either representation the
codebase can produce: a standalone `.srt` on disk (the old renderer)
or real per-sentence `caption_segments` (the enhanced renderer, which
burns them directly and never writes a separate file) -- see `TODO.md`
for the real bug this fixed (a perfectly-captioned enhanced story
reporting `captions_present: false`). A story only counts toward these
per-story checks once its `StoryContent.status` actually reaches
`"video_ready"` -- also see `TODO.md` for a similar real bug this
fixed (a story whose content pipeline stopped at `"voice_ready"` being
silently excluded even though its enhanced segment was really in the
produced video).

A QA result is stale once the episode is reproduced afterward
(`video_produced_at > qa_run_at`) -- the dashboard surfaces this as a
warning on its Run QA button rather than the API enforcing it.

Every stage is free/local, no API keys required:

- **Script** -- deterministic, template-based: headline + a
  deterministic summary of the story, nothing more (no editorializing
  or speculative "why it matters" commentary). Same pattern as the
  AI-relevance/dedup filters. Prefers the full scraped article body
  (`NewsItem.raw_content`, already fetched by content-dedup's TF-IDF
  pass -- `app/content/article_extractor.py`) over RSS's often thin/
  truncated/promotional `raw_summary` when a fetch already succeeded;
  falls back to `raw_summary` otherwise (script generation never
  triggers a fetch of its own). For Hacker News link-posts (HN's API
  has no article content, only submission metadata), the summary
  falls further back to the linked article's own `og:description`/
  `meta description`/first paragraph (`app/sources/article_fetcher.py`)
  rather than "N points, M comments on Hacker News." Promotional/
  newsletter-pitch sentences ("subscribe", "sign up", etc.) are
  filtered out of any summary before narration.
- **Voice** -- [edge-tts](https://github.com/rany2/edge-tts) (free
  Microsoft neural TTS, one branded voice for every story). Current
  voice: `en-US-JennyNeural` (final pick, chosen from the ~46-voice
  sample comparison described in `TODO.md`). Episodes produced before
  2026-10-01 keep the earlier `en-US-GuyNeural` narration until they are
  re-produced.
  Narration is further processed (highpass + gentle EQ lift +
  compression + loudness normalization) before being placed into any
  final mux -- the same one shared filter chain for every caller.
- **Storyboard/Visual** -- a deterministic scene plan (hero/statistic/
  comparison/etc.) rendered with Pillow: static photos,
  light-icon watermark, deterministic keyword-color caption emphasis,
  subordinate caption style during statistic scenes.
  `app/content/scene_renderer.py` + `app/content/storyboard_composer.py`
  render the underlying scenes; `app/content/episode_renderer.py`'s
  `render_story_enhanced()` is the one place that assembles them with
  the scene-to-scene crossfade and watermark repaint.
  `app/content/visual_generator.py`'s old single static title card is
  gone -- removed once `/stories/{id}/produce` migrated onto this
  renderer and its last caller disappeared (see `TODO.md`).
- **Video** -- ffmpeg composes each scene, hard-capped to a real
  measured duration (`-t <duration>` from a real ffprobe measurement,
  not just `-shortest` -- see `TODO.md` for why that matters). A
  0.32s dark crossfade joins consecutive scenes within one story; a
  0.6s left-to-right sweep (between the real last/first frames) + SFX joins consecutive stories, the intro and the outro.
  Captions are timed with edge-tts's own real per-sentence timing
  (`SentenceBoundary` events captured during synthesis), not an
  estimate -- see `TODO.md`.

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
cached video playback, an edit-clobbering bug, a resumed Produce/
Publish timer that reset to 0:00 on every reload instead of showing
real elapsed time -- see `TODO.md`) were only caught through actual
live use, not through automated verification alone.

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
- **See the full pipeline at a glance** -- a workflow chart (Collect ->
  Classify -> Dedup -> Content-Dedup -> Historical-Dedup -> Verify -> Rank & Select ->
  Produce -> QA -> Approve -> Publish) above the video, each stage's
  real status/count for that episode's date, derived live from
  existing data (no new schema). Complements, doesn't replace, the
  per-stage buttons and header status pills.
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
- **Process Episode / Run QA** -- both buttons disable and show a live
  elapsed-time progress indicator while running, then auto-refresh
  the view when done ("Process Episode" polls `video_status`, QA polls
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
channel's video library. Both credential pairs can be configured at
once. **The same produced video can be published to dev and prod
independently**: the episode page has a *Publish to DEV* and a *Publish
to PROD* button, and each environment's state (publishing / published /
failed, YouTube URL, error) is tracked separately in
`editorial.episode_publications` (one row per episode + environment).
`Episode.publish_status/published_at/youtube_url` are a roll-up of those
rows (any published -> "published"). A dev upload never blocks a later
prod upload, and a failed prod attempt never changes dev's state. The
PROD button stays disabled (tooltip: credentials not set) until all three
`YOUTUBE_PROD_*` values are in `.env`. `POST /episodes/{id}/publish`
takes `?environment=dev|prod` (default: `YOUTUBE_ENVIRONMENT`).
`YOUTUBE_ENVIRONMENT` now only picks the default for that endpoint and
which pair `app/scripts/youtube_oauth_setup.py` obtains a token for.

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
   - **Test users**: add your own Gmail here -- **required** while the
     app is in "Testing" publishing status; Google blocks sign-in for any
     account not explicitly listed here.
   - **Publishing status -> "In production"** (click **"Publish app"** on
     the consent screen, for BOTH dev and prod projects). **Do this --
     in "Testing" status Google expires the refresh token after 7 days**
     and every publish then fails with
     `invalid_grant: Bad Request` (this actually happened to the dev
     token, issued 2026-09-21, dead by 2026-10-01). "In production" needs
     no Google verification for your own account (you still see the
     "unverified app" warning once at consent). If you set it *after*
     getting a token, re-run Part 3 -- tokens issued while in Testing
     keep their 7-day expiry.
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
   fine, it's your own unverified app).
4. **If it asks which channel/brand account to use, pick the channel
   from Part 1** -- this is the step that actually links the credential
   to the right channel. If it doesn't ask (sometimes it just uses the
   account's only/default channel), verify afterward by checking which
   channel the first real published video actually landed on.
5. The script prints a refresh token -- save it as
   `YOUTUBE_DEV_REFRESH_TOKEN` (or `YOUTUBE_PROD_REFRESH_TOKEN` for
   the prod run).
   **Common OAuth errors and their cause:**
   - `Error 403: access_denied` ("has not completed the Google
     verification process ... can only be accessed by developer-approved
     testers") -- the consent screen is in "Testing" status and the
     Google account you signed in with is not listed. Fix in that
     environment's Cloud project (APIs & Services -> OAuth consent
     screen, or Google Auth Platform -> Audience): either **Publish app**
     (preferred, see the "In production" note in Part 2) or add that exact
     account under **Test users** (tokens then expire in 7 days). Used
     for prod on 2026-10-01 (fixed via Test users).
   - `unauthorized_client: Unauthorized` at publish time -- the refresh
     token was issued by a different OAuth client than the client
     ID/secret it is paired with in `.env` (e.g. the prod token was
     minted while the script was still using the dev client). A token only
     works with the client that issued it. Fix: set
     `YOUTUBE_ENVIRONMENT=<env>` in `.env` (not just a shell variable),
     re-run the setup script, check its first lines name the right env and
     client ID, then replace only that environment's refresh token.
   - `invalid_grant: Bad Request` -- token expired/revoked; usually the
     7-day "Testing" expiry. Re-run Part 3.
   - Pasting a token under the wrong key name (e.g. a second
     `YOUTUBE_DEV_REFRESH_TOKEN` in the prod block) silently overrides
     the first -- the last duplicate line wins. Keep exactly one line per
     key.
   - Quick check without publishing (from the repo root, host venv):
     exchange each environment's refresh token against its own client at
     `https://oauth2.googleapis.com/token` (`grant_type=refresh_token`);
     a JSON `access_token` means that pair is good.
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

## Classification and historical dedup

**Classification** (`app/filters/classification_rules.py`, `rules-v1`, first
processing stage). Each collected story becomes `ai_candidate`, `ai_review`
or `not_ai`, with the rule's reason stored on the row. `ai_review` stories
stay out of every pool until an editor promotes or rejects them
(`GET /api/v1/review-queue`, `POST /api/v1/stories/{id}/review`, and the
dashboard review lane). Changing a pattern requires bumping `RULES_VERSION`.
Evaluation data lives in `eval/classification/`.

**Historical dedup** (`app/dedup/`, `app/tasks/historical_dedup.py`,
`semantic-v1`, runs after same-day content dedup). A story is a duplicate
only when it reports substantially the same development that was already
published; sharing a company, product, model or topic is not enough
("OpenAI adds image generation to Dots" is new after "OpenAI launches Dots";
"OpenAI introduces Dots" is a duplicate). It compares against primaries of
approved/published episodes with no time cutoff, never against raw, rejected,
backup or unapproved-draft stories. Every decision (`duplicate` or
`new_development`) is stored with its rule and reason in
`editorial.historical_story_relations`; ranking excludes duplicates, and
`editor_override` is reserved for future manual overrides. The first run
loads a ~65 MB embedding model (about 28 s, downloaded once); if it is
unavailable the stage is skipped and stories stay eligible.

Read-only replay against the real database (changes nothing, writes a report
to `media/reports/`):

```bash
docker compose exec -T api python -m app.scripts.dedup_replay                     # all dates
docker compose exec -T api python -m app.scripts.dedup_replay --date 2026-10-05   # one date
docker compose exec -T api python -m app.scripts.dedup_replay --limit 40          # sample
```

Viewing it in the dashboard: the episode list has a **Historical dedup** link
(`?dedup=<date>`) listing, for a collection day, the stories held back as repeats
and (collapsed) those kept as new developments of covered ground, each with the
matched story, similarity, rule and reason. The same data is
`GET /api/v1/historical-dedup?date=YYYY-MM-DD`. Each story in an episode payload
carries `historical_relation`, the story edit panel shows it, and the workflow
chart has a Historical-Dedup stage. The stage can be run on its own with the
**4. Historical-Dedup** button (`POST /api/v1/processing/historical-dedup`); run
it after Content-Dedup so article text is available.

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

300+ tests, no running Postgres required -- DB-backed tests use an
in-memory SQLite database (`tests/conftest.py`'s `db_session` fixture;
every model uses portable column types, so this is a faithful stand-in)
rather than the real dev database. Covers the deterministic filters
(AI-relevance, dedup, ranking, script generation, fact extraction,
verification), the enhanced Pillow/storyboard renderer (scene
rendering, storyboard generation/composition/QA, real ffmpeg
end-to-end story renders gated on `ffmpeg` being installed), the
Celery task orchestration layer (`produce_episode_video`,
`produce_story_video_task`, `run_episode_qa`, episode
select/reprocess), and the FastAPI endpoints for all of the above --
plus direct regression tests for this project's hardest-won bugs (see
`TODO.md` for the full list: AV-duration desync, script-clobbering,
ingestion race, the Chrome-specific stream-copy stall, the missing-
narration `asplit` bug, absolute- vs. relative-path storage, and the
two QA-truthfulness bugs -- `video_ready` status and `captions_present`
-- fixed most recently). Each regression test was verified to actually
fail when its bug is reintroduced, not just pass tautologically.

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