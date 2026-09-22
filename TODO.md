# TODO — 5squareFeed (5min-ai-news)

Tracks implementation progress against the architecture in
[`project.md`](project.md). Update this file as work completes or
new work is identified — check items off in place rather than
deleting history, so it stays a running log.

## Done

### Core pipeline (pre-existing, per README "Current slice")
- [x] FastAPI API scaffold
- [x] PostgreSQL schema managed via Alembic
- [x] Redis + Celery worker
- [x] Multi-source RSS ingestion with deterministic AI-relevance filter
- [x] Deterministic duplicate-story detection (title similarity + time window)
- [x] Multi-factor ranking engine (recency, source credibility, AI
      relevance, cross-source momentum) + Top-25/5-backup selection,
      persisted as an "Episode"

### This session — 2026-09-15
- [x] Read `project.md` (architecture baseline) and full repo, reviewed every app file
- [x] Stopped stale containers running under the old `5min-ai-news` prefix before rebuilding
- [x] Fixed `docker-compose.yml` project name mismatch (`ai-news-platform` → `5min-ai-news`)
      so Compose reuses the real Postgres volume instead of silently creating an empty one
- [x] Added naming note to `README.md`: `5min-ai-news` = repo/dev name,
      "AI News Platform" = production/public brand
- [x] Built and started the stack (`docker compose up --build -d`), verified all 4
      services (api, worker, postgres, redis) healthy
- [x] Found and documented critical bug: no baseline Alembic migration created the
      `stories` table — `alembic upgrade head` failed on a fresh DB, breaking every
      data endpoint (ingestion, dedup, ranking, `/api/v1/stories`)
- [x] Wrote up findings in `errors.md` (renamed from `errors.txt`)
- [x] Added `alembic/versions/6f1e2a7b9c04_create_stories_table.py` as the true
      baseline migration; re-pointed `996b16094585`'s `down_revision` to it
- [x] Verified `alembic upgrade head` succeeds cleanly on a fresh DB (4 tables created)
- [x] Re-tested full pipeline end-to-end: ingestion (26 stories inserted from 2153
      seen across 8 sources) → dedup (0 duplicates, expected on first run) →
      ranking/episode selection (16 stories ranked and scored correctly)
- [x] Removed orphaned, empty `ai-news-platform_postgres_data` Docker volume
- [x] Renamed `project.txt` → `project.md`, `errors.txt` → `errors.md`
- [x] Created this `TODO.md` to track ongoing work
- [x] Ran a stability pass: ~15 min of live use (repeated ingestion/dedup/ranking
      triggers), all 4 containers stayed healthy, ingestion confirmed idempotent
      on re-run, dedup stable, 404s and query-param clamping verified correct.
      See `errors.md` sections 4-5.
- [x] Fixed the `run_date` validation bug: `trigger_ranking_selection()` now
      validates with `date.fromisoformat()` and returns HTTP 422 with a clear
      message on bad input, instead of silently queuing a task that fails in
      the worker. Verified against the live API. See `errors.md` §4 update.

### This session — 2026-09-15, part 2 (script/voice/visual/video, one-story proof of concept)
- [x] Added `story_content` table (1:1 with `stories`) tracking generated
      script/audio/image/captions/video paths and a `status` progress field
- [x] Script generation: deterministic, template-based (`app/content/script_generator.py`)
      -- headline, summary (HTML-stripped RSS summary), "why it matters"
      (built from the AI-relevance filter's already-computed keywords).
      No LLM, no API key -- same pattern as the existing AI-relevance/dedup filters.
- [x] Voice generation: `edge-tts` (`app/content/voice_generator.py`), one hardcoded
      branded voice (`en-US-GuyNeural`), free, no API key
- [x] Visual generation: Pillow-rendered branded title card (`app/content/visual_generator.py`),
      no API key
- [x] Video composition: ffmpeg combines image + audio + burned-in captions into
      an mp4 (`app/content/video_composer.py`); captions timed via a naive
      proportional estimate (character-count share of total audio duration)
- [x] New Celery chain (`app/tasks/content.py`): `generate_script_task` ->
      `generate_voice_task` -> `generate_visual_task` -> `compose_video_task`,
      each stage persisting progress/failure to `story_content.status`
- [x] New endpoints: `POST /api/v1/stories/{id}/produce`,
      `GET /api/v1/stories/{id}/content`; `/media` mounted via `StaticFiles`
      so generated audio/image/video are directly playable by URL
- [x] Dockerfile: added `ffmpeg` + `fonts-dejavu-core` (apt packages)
- [x] requirements.txt: added `edge-tts` and `Pillow`
- [x] **Bug found and fixed during testing:** pinned `edge-tts==6.1.9` failed
      every synthesis call with `403 Invalid response status` -- Microsoft's
      TTS endpoint now requires a `Sec-MS-GEC` signed token that 6.1.9
      predates. Bumped to `edge-tts==7.2.8` (latest at the time), fixed.
- [x] Verified end-to-end on story #15 (the top-ranked story from the latest
      episode): real ~30s narrated audio, correctly rendered branded visual
      card (Unicode included), proportionally-timed captions, valid h264/aac
      mp4 (confirmed via `ffprobe`). Also ruled out a false alarm: `’`
      appeared as mojibake in one local test command's output, but the raw
      Postgres bytes were correct UTF-8 the whole time -- a Windows/Git-Bash
      terminal decoding artifact in the test tool, not an app bug.

### This session — 2026-09-15, part 3 (summary truncation bug)
- [x] **Bug found and fixed:** `build_summary()` was including the source
      RSS feed's own truncated-excerpt marker (e.g. "...signing on at least
      partially to a [&#8230;]") as if it were a complete sentence, producing
      scripts that read as dangling mid-thought. Added `TRUNCATION_MARKER_RE`
      to detect and drop a trailing truncated fragment before assembling the
      summary. Verified on story #15: summary now ends cleanly at the last
      complete sentence.
- [x] **Learned:** the `worker` container does not hot-reload on code changes
      like the `api` container's `uvicorn --reload` does -- Celery loads task
      modules once at process startup and keeps them in memory. After editing
      any `app/tasks/*` or `app/content/*` file, `docker compose restart
      worker` is required before re-running a task, or the old code silently
      keeps running.

### This session — 2026-09-15, part 4 (source list expansion)
- [x] Reviewed an external analysis (`public-response.md`) of the episode/video
      pipeline; verified its claims against the live system rather than
      accepting them at face value. Its "Top-25 selection: Not Achieved" framing
      was misleading -- confirmed there are only 16 total canonical
      AI-candidate stories in the whole database right now (not a broken
      selector; the code correctly doesn't pad to 25 with fewer than 30
      candidates, already verified in `errors.md`). "Episode 3" was also just
      an artifact of earlier manual re-testing, not 3 real daily runs.
- [x] Answered directly: with the 8 sources enabled at the time, one real
      ingestion pass produced 2153 RSS entries seen -> 26 inserted -> only 16
      AI-candidate canonical stories. Not close to 30/day, and nothing was
      actually "global" (all sources English-language, US-centric tech press).
- [x] Researched and verified (via live `curl`, not just search results) two
      new officially-hosted, active RSS sources and added them to
      `app/sources/registry.py` + `app/ranking/engine.py`'s `CREDIBILITY_WEIGHTS`:
      **Google DeepMind News** (`https://deepmind.google/blog/rss.xml`) and
      **Wired — Artificial Intelligence** (`https://www.wired.com/feed/tag/ai/latest/rss`).
- [x] **Researched and deliberately skipped** (documented so this isn't
      silently re-researched later):
      - Anthropic, Meta AI -- no official RSS feed exists at all, only
        unofficial third-party scraper mirrors (e.g. GitHub Pages projects
        re-scraping their blogs). Skipped per user decision: same fragility
        class already worked around with VentureBeat/Microsoft AI Blog.
      - CNBC Technology -- the feed ID found via search was actually a video
        show feed ("Squawk Box Europe"), not Technology news. Dropped rather
        than guess further; the real Technology feed URL wasn't confirmed.
      - Axios AI, Engadget's AI tag, Business Insider, TechRadar's AI
        category -- no confirmed direct feed URL found in this research pass.
        Candidates for a future pass, not confirmed dead ends.
- [ ] **Two new sources alone do not guarantee ~30/day.** This is incremental,
      not a fix -- reaching a reliable daily average needs either more
      verified-source research passes, or the not-yet-built scheduled
      multi-pass daily collection (separate item below), since a single manual
      trigger only ever captures one snapshot in time.

### This session — 2026-09-15, part 5 (Hacker News as a non-RSS source)
- [x] Added Hacker News as a new "News API"-category source (per `project.md`'s
      architecture, distinct from RSS/Websites) -- new fetcher
      (`app/sources/hackernews_api.py`) using the official Algolia search API,
      new Celery task (`app/tasks/ingestion_hackernews.py`), new endpoint
      `POST /api/v1/ingestion/hackernews`, credibility weight added
      (`"Hacker News": 0.75`). New dependency: `requests`.
- [x] **Investigated and deliberately dropped GitHub** ("trending" via the
      official Search API proxy -- `topic:artificial-intelligence` +
      `created:>N days` + `sort:stars`). Verified live: repos created
      yesterday top out at 2 stars, repos near a 14-day window's edge top out
      at 16 stars -- nowhere close to real "trending," and structurally
      biased toward obscure repos since the API can only sort by cumulative
      stars, not stars-gained-recently. GitHub has no official trending API
      at all, only unofficial scrapers, which were already ruled out per the
      Anthropic/Meta AI precedent. Not adding GitHub as a source for now --
      revisit only if a genuinely reliable trending signal becomes available.
- [x] Verified Hacker News's actual daily consistency and content quality
      before building (not just once): sampled `points>15` AI stories for
      each of the last 7 individual days -- 19-24 qualifying stories every
      single day, no dry days, many in the 100-1200+ point range on
      substantive topics. Confirmed via a second immediate re-run of
      ingestion that already-stored HN stories are correctly skipped as
      duplicates, not re-surfaced as "new" -- answers "will these repeat
      every day?" directly for the shipped behavior: no. Confirmed with real
      numbers: first run `{'seen': 20, 'inserted': 20, 'duplicates': 0}`,
      immediate second run `{'seen': 20, 'inserted': 0, 'duplicates': 20}`.
      First real run also produced the first-ever cross-source duplicate
      detected all session (an Ars Technica article and an HN story both
      about the same Apple iOS 27 release, 77% title similarity) -- momentum
      had been 0 for every story until this point.

### This session — 2026-09-15, part 6 (HN source-name vs. publisher fix)
- [x] Reviewed `proposal.md` (an externally-authored document) and verified
      every specific claim it made against live Episode 4 data -- all
      accurate, including that a genuine Guardian article was stored with
      `source_name: "Hacker News"` and scored at HN's 0.75 credibility tier
      instead of whatever a Guardian-specific weight would be. Independently
      found one more case: a story titled "...(Not AI Gen)" was classified
      as an AI candidate purely because "AI" appears as a substring in its
      own title (`filter_reason: "Matched: ai"`) -- a separate, smaller issue
      not addressed by this fix, noted here for later.
- [x] Fixed the source-vs-publisher conflation: added
      `app/sources/publisher_resolver.py` (`resolve_publisher(url)`),
      resolving the real publisher from a story's URL domain instead of
      hardcoding `"Hacker News"` for every HN-discovered story.
      `source_type="hackernews"` still marks the discovery channel; genuine
      Ask/Show/Tell HN self-posts still correctly resolve to `"Hacker News"`
      as their real publisher.
- [x] Added credibility weights for the 4 outlets already confirmed present
      in real data: The Guardian (0.85), The Register (0.80), MacRumors
      (0.75), IEEE Spectrum (0.90). Everything else not curated correctly
      falls through to `DEFAULT_CREDIBILITY = 0.60`.
- [x] Added `app/scripts/backfill_hn_publisher.py` (same pattern as the
      existing `backfill_ai_relevance.py`) to re-resolve `source_name` for
      the 13 HN stories already in the database from earlier this session.
- [ ] Not addressed here (deliberately out of scope, noted above): the
      "(Not AI Gen)" false-positive and the much larger taxonomy-redesign
      proposal in `proposal.md` (Major News / Developer Radar / Research &
      Security / Tools sections, event clustering, content-type
      classification) -- a legitimate longer-term direction, needs its own
      dedicated planning pass.

### This session — 2026-09-15, part 7 (full-episode video production)
- [x] Scaled the script/voice/visual/video content pipeline from one story at
      a time to a full episode: new `POST /api/v1/episodes/{id}/produce`
      (`app/tasks/episode_video.py`) runs every primary story through the
      same 4-stage pipeline sequentially and in-process (not via the
      Celery-chained single-story tasks, which auto-chain async and would
      break sequencing), then concatenates the results into one combined
      episode video via `concat_videos()` (`app/content/video_composer.py`).
      Idempotent (stories already `video_ready` are reused, not
      regenerated) and fault-isolated (a failed story is skipped from the
      final video rather than blocking the whole episode).
- [x] Added `Episode.video_path` / `Episode.video_status` columns
      (migration `a7c3d9e1f204`), exposed via `video_url`/`video_status` on
      `GET /api/v1/episodes/{id}` and `/latest`.
- [x] Shared helpers (`get_or_create_content`, `mark_content_failed`) were
      de-underscored in `app/tasks/content.py` and reused directly, rather
      than duplicating the DB-write/error-handling glue in the new task.
- [x] Verified end-to-end on episode 5 (25 primary stories): first run --
      11 newly produced, 14 reused from earlier single-story tests, **0
      failures**, completed in 55s total; combined video valid (h264/aac,
      6:05, 7.4MB, confirmed via `ffprobe`). Re-ran immediately after --
      correctly reused all 25 (`stories_produced: 0, stories_reused: 25`),
      confirming idempotency.
- [x] ~~Still a straight concatenation only -- no intro/outro~~ -- intro/outro
      added, see "part 8" directly below. Transitions/background music
      remain out of scope (see part 8's own note).

### This session — 2026-09-15, part 8 (episode-level intro/outro branding)
- [x] Added narrated intro/outro clips to the combined episode video, so it
      reads as one produced show rather than 25 stitched clips. New
      `generate_branding_card()` (`app/content/visual_generator.py`) --
      same visual style as the per-story card, minus the "Source: " framing
      which doesn't apply here. New `_produce_branding_clip()` helper
      (`app/tasks/episode_video.py`) reuses the existing voice/caption/
      compose pure functions, same as a story's pipeline, just not tied to
      a Story row.
- [x] Intro: "AI Daily 25" + formatted run_date + "Today's Top 25 AI
      Stories", narrated. Outro: "That's all for today's AI Daily 25. See
      you tomorrow.", narrated. Confirmed with the user (simple +
      informative wording, narrated over silent).
- [x] Intro/outro are regenerated on every `/produce` call (not cached like
      story content) -- deliberately simple, since each clip only costs a
      few seconds and there's no new DB state to track staleness.
- [x] Verified end-to-end on episode 5: combined video duration grew from
      365.5s to 378.7s (+13.2s for both clips, reasonable), both card
      images visually confirmed clean and readable, video still valid
      (h264/aac via `ffprobe`).
- [ ] Still no transitions between segments (hard cut only) and no
      background music (deliberately out of scope -- licensing complexity
      for royalty-free audio, not attempted).

### This session — 2026-09-15/16, part 9 (Automated Video QA)
- [x] Built the architecture's Automated Video QA stage (`app/qa/video_qa.py`,
      `app/tasks/episode_qa.py`, `POST /api/v1/episodes/{id}/qa`) --
      implements every checkmark from `project.md`'s QA checklist: story
      count, AI-only, source links, captions present, audio present, video
      integrity, duration target. Each check independently re-verifies real
      state (actual files on disk, real `ffprobe` stream inspection) rather
      than trusting an earlier stage's own success report.
- [x] "Verified sources" is explicitly reported as **not implemented**
      (`passed: None`, doesn't count as a failure) rather than faked as a
      pass -- there's no Verification Engine built yet (see "Next up"
      below). Honest gap, not silently skipped.
- [x] Duration target set to 300s (5 min), from the project's own name
      ("5min-ai-news") and `proposal.md`'s "<5 minute requirement" framing
      -- not tuned to make current episodes pass.
- [x] Verified end-to-end on episode 5: 7/8 checks pass cleanly (story
      count 25/25, AI-only, source links, captions, audio, video integrity
      all real PASS). **duration_target genuinely FAILS** -- 378.7s vs the
      300s target. This is an honest, expected result (not a bug): 25
      stories' worth of narration plus intro/outro naturally runs long
      without per-story time budgeting, exactly the gap `proposal.md`'s
      "episode budgeting / variable story durations" section already
      flagged. Confirms QA is measuring something real, not rubber-stamping.
- [ ] Not addressed here: fixing the duration overage itself (needs
      variable per-story time budgets or fewer/shorter segments -- a
      content-pipeline change, not a QA change) and building the
      Verification Engine so "source_verification" can become a real check.

### This session — 2026-09-16, part 10 (Editorial Dashboard v1)
- [x] Built the first-ever frontend for this project: `app/dashboard/` (vanilla
      HTML/CSS/JS, served via `StaticFiles(html=True)` mounted in `app/main.py`
      -- no new dependencies, no build step). Scoped per `dashboard-proposal.md`
      (externally authored, full "AI News Studio" vision) with the user's
      confirmed decisions: start lightweight, defer YouTube/Instagram/
      analytics/cloud-storage/auth to a later workstream.
- [x] Episode List view + Episode Studio view (video player, Top 30 with
      primary/backup, QA panel, click-rank-to-jump-player, click-title-to-edit).
- [x] New backend endpoints: `PATCH /api/v1/stories/{id}/content` (script
      edit -- correctly resets content status + clears stale audio/image/
      captions/video paths so a later `/produce` regenerates rather than
      reusing stale media), `POST /api/v1/episodes/{id}/reorder`,
      `POST /api/v1/episodes/{id}/swap` (backup <-> primary), `POST
      .../approve`, `POST .../reject`, `GET /api/v1/episodes` (episode list --
      didn't exist before, only `/latest` and `/{id}` did).
- [x] Drag-and-drop reorder/swap uses native HTML5 drag events, no library
      (dnd-kit), per the confirmed "keep interactivity simple so nothing
      complex needs re-engineering if this ever migrates to React" approach.
- [x] **Bug found and fixed during testing:** the `/swap` endpoint's direct
      simultaneous position swap hit the same transient
      `uq_episode_rank_position` unique-constraint collision the `/reorder`
      endpoint had already been built to avoid -- reproduced with a real 500
      error, fixed with the same "move one row out of the real rank range
      first" technique, re-verified working.
- [x] Backend endpoints (PATCH content, reorder, swap, approve, reject, list)
      all verified directly against real data via `curl` -- reorder swapped
      ranks 1/2 correctly, swap exchanged a primary/backup pair correctly
      (after the fix), approve/reject correctly changed `Episode.status`,
      PATCH correctly reset a story's content status and cleared stale paths.
- [x] **Frontend UI click-tested in a real browser** (via `claude-in-chrome`,
      connected this session): Episode List loads and links to Episode
      Studio; video player loads and plays the produced episode 5 video with
      audio/captions; "click rank to jump player" seeks correctly; "click
      title to edit" opens the story edit modal with headline/summary/script
      pre-filled; QA panel renders all check rows. No console errors on
      load. Drag-and-drop reorder verified with real dispatched `DragEvent`s
      (synthetic mouse drag doesn't trigger native HTML5 DnD, so this needed
      actual `DragEvent`/`DataTransfer` objects) -- confirmed the DOM
      reorders and fires `POST /episodes/{id}/reorder` (200), then reverted
      the test reorder back to original order the same way.
- [ ] Minor known gap, not exploitable through normal dashboard usage but
      worth hardening later: `/reorder` doesn't explicitly validate that all
      provided `story_ids` belong to the same `selection_status` group
      (primary or backup) -- the dashboard only ever sends one group's full
      id list, so this doesn't misbehave in practice, but the endpoint would
      accept a mixed list without complaint.
- [ ] Episode-level `video_status`/`qa_status` don't automatically become
      stale after a reorder/swap/script edit -- only the edited story's own
      `content_status` resets. Re-running Produce/QA after any edit is on
      the human, not enforced by the system yet.

### This session — 2026-09-16, part 11 (produce backups too + Produce/QA progress UI + stale-QA indicator)
- [x] `produce_episode_video` (`app/tasks/episode_video.py`) now also produces
      (script/voice/visual/video) the 5 backup stories, best-effort, as a
      second phase that runs only after the primary video is already
      concatenated and marked `ready` -- so a slow/failing backup can never
      delay or block the primary episode. Backups are never appended to
      `video_paths`/the concatenated output. Verified end-to-end on episode
      5: first run produced 4/5 backups (1 was already `video_ready` from
      earlier testing) in 18.6s total; immediate re-run correctly reused all
      5 (`backups_produced: 0, backups_reused: 5`) in 5.7s, confirming
      idempotency. Confirmed a swap of a now-`video_ready` backup into
      primary needs no regeneration (`story_content.updated_at` unchanged
      by the swap) -- the actual goal of this change.
- [x] Added `Episode.video_produced_at` / `Episode.qa_run_at` (migration
      `c3f7a1d92e58`), set by `produce_episode_video` (success path only)
      and `run_episode_qa` respectively. Exposed via `_serialize_episode`.
      Verified via curl: `video_produced_at` set on produce, `qa_run_at`
      set (and later than `video_produced_at`) after running QA.
- [x] Dashboard: **Produce** button now disables and shows a live `m:ss`
      elapsed timer while queued, polling `GET /episodes/{id}` every 3s
      until `video_status` leaves `"producing"`, then auto-refreshes the
      studio view -- replaces the old "click Produce, refresh yourself in a
      bit" alert. Resumes automatically on page reload if `video_status` is
      still `"producing"` (elapsed timer restarts from reload time in that
      case -- no true start time is persisted, noted as an accepted
      approximation). **Run QA** button got the same treatment (poll every
      1.5s comparing fresh `qa_run_at` against the click time, 30s safety
      timeout), replacing the old blind `setTimeout(2500)`.
- [x] **Run QA** button now shows an amber "stale" state (`Run QA ⚠
      (stale)`, `.btn-warn` style reusing the existing `--skip` palette
      tokens) whenever `video_produced_at > qa_run_at` (or QA has never
      run) -- i.e. whenever Produce has completed since QA last ran.
      Deliberately scoped to Produce only, per what was asked; reorder/
      swap/script-edit still don't invalidate `qa_status` (pre-existing
      gap, unchanged here -- see below).
- [ ] **Not click-tested in a real browser this session** -- the
      claude-in-chrome MCP connection dropped mid-session and couldn't be
      re-established. All of the above was verified via curl/DB queries
      (timestamps, idempotency counts, swap behavior) and a careful
      line-by-line review of the `app.js` diff, plus confirming the served
      `/dashboard/app.js` reflects the new code -- but the actual spinner/
      timer/stale-button rendering and behavior in a live browser needs a
      human click-through before trusting the UI layer specifically.
- [x] **Bug found and fixed (user-reported, real click-through):** swapped a
      backup into primary rank 1, clicked Produce -- timer showed "3 secs"
      then refreshed showing the OLD video, unchanged. Root cause: `POST
      .../produce` only queued the Celery task and returned immediately;
      `episode.video_status` only flips to `"producing"` inside the task
      itself, a moment after the worker picks it up. The dashboard's poll
      stops as soon as `video_status !== "producing"` -- which can't tell
      "hasn't started yet" apart from "already finished". If the first poll
      (fired 3s after click) landed before the worker flipped the status,
      it read the stale pre-produce value, wrongly concluded production was
      already done, and re-rendered the still-old video while the real
      production kept running unseen in the background. Fixed by setting
      `episode.video_status = "producing"` synchronously in the `/produce`
      endpoint itself, before queuing the task (`app/main.py`), closing the
      race by construction. Verified: `video_status` now reads
      `"producing"` immediately on the POST response and stays that way for
      the full first few seconds (checked at t=0/1/3s); reproduced the
      exact scenario (swap backup to primary rank 1, Produce) and confirmed
      the combined video's duration changed and the API's reported rank-1
      story matched the swap -- then reverted the test swap/re-produce to
      restore episode 5's original state.
      Note: the QA polling (`qa_run_at` timestamp comparison) does not have
      this bug -- it compares against a fresh monotonic timestamp captured
      at click time, not a transient status word, so there's no equivalent
      "not started vs. already done" ambiguity.
- [x] **User reported the exact same symptom again after the fix above** --
      investigated further (still couldn't get claude-in-chrome reconnected
      to click-test live, see below). Found and closed a second real gap:
      `GET /media/videos/episode_N.mp4` was served with `Last-Modified`/
      `ETag` but **no `Cache-Control` header at all** -- since the file is
      regenerated in place at the same fixed URL every Produce run (no
      content-hashed filename), a browser could apply heuristic freshness
      and reuse a stale cached copy of the video without ever revalidating
      against the server. Added `RevalidateStaticFiles` (`app/main.py`), a
      thin `StaticFiles` subclass that sets `Cache-Control: no-cache` on
      every `/media` response -- keeps the free conditional-GET/304 fast
      path but forces revalidation every time, so a changed ETag is never
      masked by a stale hit. Also added a small `no_store_api_responses`
      middleware setting `Cache-Control: no-store` on all `/api/*`
      responses, to remove any remaining doubt about the dashboard's
      polling loop ever being satisfied from a cached JSON response
      (these had no validators to begin with, so unlikely to have been
      cached, but cheap to rule out explicitly). Verified via curl: both
      headers now present (`no-cache` on `/media/videos/episode_5.mp4`,
      `no-store` on `/api/v1/episodes/5`).
- [x] **User confirmed after retesting:** the story order/content itself
      now correctly updates after Produce (the two fixes above worked) --
      but the automatic re-render after Produce completes (button resets
      to normal on its own) still showed the OLD video; only a full manual
      page refresh showed the new one. Since the button resetting proves
      the poll->re-render cycle really did run a fresh fetch+DOM rebuild
      (ruling out the earlier race and the missing-Cache-Control-header
      issue, both already fixed), this pointed to a *different*, narrower
      cause: browser `<video>` elements stream via HTTP range requests
      (confirmed `accept-ranges: bytes` on the response), and browsers are
      known to keep serving stale cached video segments under an unchanged
      URL even with correct `Cache-Control` headers, because range-request
      caching is handled by a separate media-cache pipeline that doesn't
      always honor the same revalidation rules as a normal fetch.
- [x] **Fixed properly via cache-busting** (the standard, bulletproof fix
      for this class of bug -- sidesteps the media-cache question
      entirely instead of fighting it): added `Episode.video_produced_at`
      (already existed, from Part 11 above) as a `?v=` query-string suffix
      on the episode player's `src` (`cacheBust()` helper, `app.js`), so a
      real Produce always yields a URL the browser has never seen before.
      Applied the same treatment to each story's edit-panel preview video
      -- added `content_updated_at` to `_serialize_episode`'s per-story
      entry (`app/main.py`) and used it the same way, since a script edit
      -> re-Produce regenerates that story's video at the same fixed
      per-story URL and would hit the identical bug. Verified via curl:
      `video_produced_at`/`content_updated_at` are present in the API
      response, and the video route still resolves correctly with a
      `?v=...` suffix (query strings are ignored by StaticFiles routing,
      200 OK).
- [ ] **Still not click-tested live in a real browser this session** --
      claude-in-chrome remains unavailable (retried twice, no matching
      tools both times). Everything above verified via curl/DB checks
      plus confirming the served `/dashboard/app.js` reflects the new
      code. The user's own live retest (not this session's browser tool)
      is what actually diagnosed the button-resets-but-video-stale
      symptom that led to this fix -- please retest once more on your end
      to confirm the cache-busted URL resolves the remaining issue.

### This session — 2026-09-16, part 12 (source article link per story)
- [x] Added a source-article link below each story's title/source in the
      Top 30 list (`app/dashboard/app.js`'s `renderStoryList`), using the
      already-exposed `story.url` field. Opens in a new tab (`target=
      "_blank" rel="noopener noreferrer"`) so an editor can proofread the
      generated script against the original article without losing their
      place in the dashboard. Click on the link stops event propagation
      so it doesn't also trigger the row's existing "click to edit script"
      handler. No backend changes needed -- `url` was already in
      `_serialize_episode`'s per-story response.
- [x] **Follow-up (same session):** added the same source link to the edit
      panel too (it was missing there), and changed both places to show
      the actual URL text itself (selectable/copyable, not just an arrow
      icon) plus a one-click **Copy** button, so an editor can grab the
      link to share it elsewhere -- not just open it. Shared via one
      `renderSourceLinkHtml()`/`wireSourceLinkCopyButtons()` pair
      (`app.js`) used in both `renderStoryList` and `openEditPanel`; new
      `#edit-source` container added to `index.html`, right above the
      Headline field. Uses `navigator.clipboard.writeText()` with a
      fallback alert showing the raw link if the clipboard API is
      unavailable/denied.
- [x] **Correctness fix caught during review:** `escapeHtml()` escapes
      `&`/`<`/`>` (safe for text nodes) but not `"`, so its output isn't
      safe to embed inside an HTML attribute value -- this is the first
      place in the dashboard doing that (`href="..."`, `data-url="..."`).
      Added a dedicated `escapeAttr()` (escapeHtml + quote-escaping) and
      used it for both attributes, closing a latent HTML-injection risk if
      a story's URL (sourced from external RSS/API feeds) ever contained a
      literal `"` character.

### This session — 2026-09-16, part 13 (edit panel metadata)
- [x] Added a Source/Author/Published/Collected metadata block to the edit
      panel, right below the source-article link (`#edit-meta`, a `<dl>`
      in `index.html`; `renderEditMetaHtml()`/`formatDateTime()` in
      `app.js`). `published_at` and `source_name` were already exposed by
      the API; added `author` and `collected_at` ("when we took it") to
      `_serialize_episode`'s per-story entry (`app/main.py`) since those
      weren't returned before. Dates formatted via
      `toLocaleString()` for readability; missing values (author is often
      null) render as "—" rather than blank/undefined.

### This session — 2026-09-16, part 14 (discovery channel vs. resolved publisher)
- [x] Added a conditional "Discovered via" row to the edit panel's
      metadata block, linking back to the actual discovery page -- e.g. a
      story surfaced via Hacker News but published elsewhere now shows
      both "Source: nathannaveen.dev" (the resolved publisher, from
      `resolve_publisher()` -- see part 6) AND "Discovered via: Hacker
      News ↗" linking to `https://news.ycombinator.com/item?id={hn_id}`.
      New `_discovery_info()` helper (`app/main.py`) returns this only
      when `source_type == "hackernews"`; `None` (row omitted) for
      directly-ingested RSS stories, which have no separate discovery
      channel to show. Verified via the live API: a real HN-discovered
      story returns the expected `{"label": "Hacker News", "url": "...
      item?id=49697477"}`, a plain RSS story returns `discovery: None`.
      Written generically (keyed off `source_type`, not hardcoded to HN)
      so a future non-RSS aggregator source only needs one more branch
      in `_discovery_info()`, not dashboard changes.

### This session — 2026-09-16, part 15 (Produce was silently discarding edited scripts)
- [x] **Critical bug found and fixed, user-reported:** edited story #46's
      summary/script in the dashboard, clicked Save, clicked Produce --
      the video used the OLD auto-generated text, and reopening the edit
      panel showed the OLD text again too, as if the edit never happened.
      Root cause: `_produce_story_content()` (`app/tasks/episode_video.py`,
      used by episode-level Produce) unconditionally called
      `generate_script()` from the story's original RSS title/summary on
      **every** call, regardless of whether a script already existed --
      silently overwriting a human-edited script (which the edit panel's
      `PATCH /stories/{id}/content` deliberately preserves, only clearing
      the downstream audio/image/captions/video to force those to
      regenerate *from* the edit) with the auto-generated template text.
      The edit panel then correctly displayed whatever was actually in
      the DB -- which was the just-clobbered auto text, not a caching or
      rendering bug on the frontend.
- [x] Fixed by skipping script (re)generation whenever
      `content.script_text` is already populated -- covers both the
      edit-preservation case and, as a side benefit, a retry after a
      failure at the voice/visual/video stage (no longer wastefully/
      riskily redoes script generation it didn't need to). Applied the
      identical fix to `generate_script_task`
      (`app/tasks/content.py`, the single-story `POST /stories/{id}/produce`
      path) since it had the exact same unconditional-regeneration bug,
      even though the dashboard doesn't call that endpoint directly.
- [x] Verified end-to-end by reproducing the user's exact report: PATCHed
      story 46 with their exact summary/script text, confirmed the PATCH
      response showed `status: script_ready`; triggered episode 5's
      Produce; confirmed after completion that `GET /stories/46/content`
      still shows the **exact edited text** in both `summary` and
      `script_text`, `status: video_ready`, and a fresh `audio_duration_seconds`/
      `video_url`/`updated_at` -- proving voice/visual/video were correctly
      regenerated *from* the edited script, not from a re-run of the
      auto-generator.

### This session — 2026-09-16, part 16 (edit panel Close button placement)
- [x] Moved the edit panel's Close button from the header's top-right into
      the footer, right after Save (`index.html`/`style.css`) -- no JS
      changes needed, same `#edit-close` element/id, just repositioned.

### This session — 2026-09-16, part 17 (raw episode JSON audit panel)
- [x] Added a "Episode JSON (audit)" panel at the bottom of the Studio
      view (`app/dashboard/app.js`'s `wireJsonAudit()`) -- shows the exact
      JSON the page rendered from (`GET /episodes/{id}`'s full response,
      pretty-printed) in a scrollable box, with **Copy JSON** (clipboard)
      and **Download JSON** (saves `episode_{id}.json` via a Blob URL) 
      buttons. No new backend endpoint -- reuses the already-fetched `ep`
      object, so it's always exactly what's on screen. Verified the real
      payload is a reasonable size for this (episode 5: ~43KB/699 lines).

### This session — 2026-09-16, part 18 (summary quality audit + fix)
- [x] **User asked to audit summary quality** ("sensible summary... not
      like signup required, 128 points on HN"). Queried every
      `story_content.summary` in the DB directly (not just spot-checked)
      and confirmed the complaint with real numbers:
      - **Hacker News link-posts: 10 of 13 (77%) were pure junk** --
        "{points} points, {comments} comments on Hacker News." with zero
        information about the actual story. Root cause: HN's Algolia API
        only returns submission metadata, never the linked article's
        content (`app/tasks/ingestion_hackernews.py`'s `_build_summary()`).
        Only genuine Ask/Show/Tell HN self-posts (which carry their own
        `story_text`) had a real summary.
      - **RSS: 15-16 of 18 were already genuinely good.** Found one real
        "sign up" case (MIT Tech Review story #19: a newsletter-pitch
        sentence mixed into otherwise-real content) and two TechCrunch
        event-promo blurbs (#10, #12 -- "Don't miss this interactive
        session...") that read as ads, not news summaries.
- [x] **Fixed the HN gap** (user chose "fetch the article page" over
      dropping link-posts or leaving it as a known limitation): new
      `app/sources/article_fetcher.py` -- `fetch_article_summary(url)`
      fetches the linked page and extracts `og:description`, then
      `<meta name="description">`, then the first substantive `<p>`, in
      that order. Uses stdlib `html.parser.HTMLParser` (no new
      dependency -- `requests` was already in use for the HN API).
      Never raises; returns `None` on any failure (network error,
      timeout, non-HTML response, no extractable text) so a slow/hostile
      site can't block ingestion. Wired into
      `ingestion_hackernews.py`'s `_build_summary()`, tried before the
      old points/comments fallback, only for real external links (not
      self-posts' synthetic HN-permalink fallback url).
- [x] **Fixed the RSS "sign up" gap**: `script_generator.py`'s
      `build_summary()` now also drops any sentence matching
      `PROMO_SENTENCE_RE` (sign up/sign in/subscribe/newsletter/log in),
      same rationale and placement as the existing
      `TRUNCATION_MARKER_RE` handling. Left the 2 TechCrunch event-promo
      blurbs alone -- they don't contain those trigger words, and a
      generic "is this an ad" filter would be brittle; noted as a small
      residual gap rather than chased further.
- [x] **Backfilled existing data** (not just future ingestion) with two
      new one-time scripts, both following the existing
      `app/scripts/backfill_*.py` pattern and both **provably safe**
      against clobbering a human edit -- only regenerate a story's
      content when its *current* stored summary is still exactly the
      old junk text the new code would never produce (proof it predates
      the fix and hasn't been hand-edited since):
      - `backfill_hn_summaries.py`: re-fetched all 16 HN stories still
        carrying the junk points/comments text. **14 fixed, 2 fetch
        failures** (left as an honest fallback). Correctly **skipped
        stories #31 and #46** -- both already manually edited earlier
        this session -- proving the safety check works, not just
        assumed.
      - `backfill_promo_sentences.py`: found 3 `StoryContent` rows with
        a promo sentence baked in (RSS #19, plus HN #38/#39 which had
        just been regenerated by the script above, before this second
        fix existed) and regenerated all 3 from their unchanged
        `raw_summary`.
- [x] **Verified final state via a full DB query, not spot-checks**: of
      31 stories with generated content, only **1 residual junk case**
      remains (#36, a fetch failure -- article site likely blocked the
      request), 2 honest "no summary was available" fallbacks (one of
      which, #39, is itself correct: its entire fetched page content was
      a subscription pitch, so filtering left nothing real to show --
      better than displaying an ad as if it were a summary), and every
      other story now reads as a real, sensible 1-3 sentence summary
      tied to its headline.
- [ ] Regenerated stories' audio/visual/video were reset (same pattern
      as a manual edit) -- their currently-produced videos (if any) are
      now stale relative to the new text until the next Produce run.
      Expected, not a bug -- same as any other script edit.

### This session — 2026-09-16, part 19 (user-reported AV desync + story-to-story pacing gap)
- [x] **User reported a real AV desync**: in the final combined episode
      video, audio finished around 6:44 while video/captions ran to
      ~7:18-7:21 -- a ~34s drift by the end of the episode. Investigated
      directly: `ffprobe` on a single story's clip (#31) showed its
      **video stream running 26.68s vs. its own 24.55s audio** -- a
      2.1s per-clip overshoot. `compose_video()`'s `-shortest` flag,
      combined with a looped still-image input and the burned-in
      subtitles filter, was letting the video stream run past where the
      audio actually ends (GOP/keyframe-flush behavior, not simple
      rounding) -- and since `concat_videos()` stitches ~27 such clips
      together, that per-clip overshoot accumulated additively into the
      exact scale of drift reported.
- [x] **Fixed** by passing an explicit `-t <audio_duration_seconds>` to
      `compose_video()` (`app/content/video_composer.py`), hard-capping
      the output to the real audio length regardless of keyframe
      alignment (`-shortest` kept as a secondary safeguard, not the
      only mechanism). Updated all three call sites that already had
      the duration on hand: `_produce_story_content` and
      `_produce_branding_clip` (`episode_video.py`), and
      `compose_video_task` (`content.py`, the single-story chain).
      Captions didn't need a separate fix -- `build_captions()` already
      times them against the real audio duration; the bug was purely
      the video stream overrunning that, which also carried the
      burned-in captions past where they should've ended.
- [x] **Also added the requested 2s pacing gap between stories**: new
      `generate_gap_clip()` (`video_composer.py`) renders a silent
      black clip via ffmpeg's `lavfi` color/anullsrc sources, matched
      exactly to story clips' codec profile (h264/1280x720/yuv420p/
      25fps video, aac/24kHz/mono audio) so it splices cleanly via
      `concat_videos()`'s stream-copy concatenation. Generated once
      (content-independent) and reused across every episode, not
      regenerated per `/produce` call. Inserted in
      `produce_episode_video`'s primary loop between every pair of
      *successfully produced* consecutive stories only -- never before
      the first story (right after intro) and never doubled up around
      a story whose production failed and was excluded.
- [x] **Fixed a knock-on issue found while implementing the gap**: the
      dashboard's "click rank to jump player" (`computeStartOffset` in
      `app.js`) sums preceding stories' durations to compute where each
      story starts in the combined video -- with gaps now inserted,
      that offset calculation would have silently drifted later into
      the episode (the exact same class of bug just relocated to the
      jump feature). Updated it to add `STORY_GAP_SECONDS` (2.0,
      matching the backend) between each pair of *produced* stories,
      mirroring the backend's exact inclusion logic (a story with no
      `audio_duration_seconds` was excluded from the video entirely and
      contributes no gap either).
- [x] **Verified end-to-end on a full regeneration of episode 5** (reset
      all 30 stories -- 25 primary + 5 backup -- to force a genuine
      rebuild under the fixed pipeline, not a reuse of old drifted
      files; took 154.7s, 0 failures): isolated test on story #31 first
      confirmed the fix (video 24.52s vs audio 24.55s, 0.03s diff, down
      from 2.13s) before committing to the full regen. Final combined
      video: **video 453.376s vs. audio 453.419s -- a 0.04s difference**
      (one frame), down from the ~34s drift reported. Spot-checked 3
      more individual stories (#16, #27, #42), all within 0.03s. Gap
      clip file confirmed exactly 2.000s. Re-ran QA: all checks pass
      except `duration_target` (already failing before this fix at
      378-394s vs. the 300s target -- expected, tracked separately in
      "Next up" below; grew slightly further to 453s from the
      intentional +48s of pacing gaps, not a regression from this fix).

### This session — 2026-09-16, part 20 (pacing gap reduced to 0.5s)
- [x] Reduced the story-to-story pacing gap from 2s to 0.5s per user
      request -- `GAP_DURATION_SECONDS` (`episode_video.py`) and
      `STORY_GAP_SECONDS` (`app.js`, the matching "jump to here" offset
      constant) both updated; gap clip's cache path renamed to
      `_story_gap_0_5s.mp4` so it regenerates fresh rather than reusing
      the old 2s file.
- [x] Verified end-to-end on episode 5 -- since no story's own content
      changed, `/produce` correctly reused all 25 primary + 5 backup
      story clips (5.4s total, not a full rebuild) and only regenerated
      the gap clip + re-ran concatenation. New gap clip measured 0.52s
      (13 frames at 25fps -- `-t 0.5` quantizes to whole frames, a 20ms
      rounding artifact, same class/scale as the ~1-frame tolerance
      already accepted for story clips). Combined video: **417.856s
      video vs. 417.899s audio -- still just 0.043s apart**, identical
      to the 2.0s-gap version, confirming the change didn't reintroduce
      any drift. Duration delta checks out exactly: 453.376s (old) -
      417.856s (new) = 35.52s = 24 gaps x 1.48s saved/gap -- confirms
      24 gaps for 25 stories (correct, no off-by-one). Re-ran QA: same
      results as before (`duration_target` still fails, now 417.9s vs.
      the 300s target -- closer, unrelated pre-existing limitation).

### This session — 2026-09-16, part 21 (guardrails: CLAUDE.md, a skill, an agent)
- [x] User asked for skills/agents/guardrails so future sessions don't
      repeat this session's mistakes. Chose docs + skills/agents only
      (no enforced hooks, lowest friction, easy to adjust later).
      Added:
      - `CLAUDE.md` (repo root, auto-loaded every session) -- 8 hard
        rules, each one a real bug this session found and fixed (script-
        clobbering, cache-busting, the Produce status race, the
        `-shortest`/`-t` ffmpeg issue, the pacing-gap offset math, drag-
        and-drop testing needing real DragEvents, verify-against-live-
        system, don't silently expand scope), plus the dev-environment
        gotchas already in memory (worker no-hot-reload, postgres
        credentials, Windows path mangling).
      - `.claude/skills/verify-episode/SKILL.md` -- the exact AV-sync/
        QA/idempotency checklist used to find and verify the part 19/20
        fixes, so future pipeline changes get checked the same rigorous
        way rather than assumed correct.
      - `.claude/agents/episode-verifier.md` -- a subagent (Bash/Read/
        Grep only) that runs that same checklist independently against
        the live system and reports real measured numbers, for use
        proactively after pipeline changes or when the user reports a
        produced episode looking/sounding wrong.

### This session — 2026-09-16, part 22 (scheduled Daily News Cycle)
- [x] Built the architecture's Daily News Cycle (`project.md`'s three
      overnight collection passes + 4 AM IST cutoff) via Celery Beat --
      a `beat` service (`docker-compose.yml`), fires the existing
      `ingest_news`/`ingest_hackernews_stories` tasks at 10 PM/1 AM/
      3:30 AM IST, then a new orchestrating task,
      `run_nightly_cutoff` (`app/tasks/scheduled.py`), at 4 AM IST:
      rank + select -> produce the episode video -> run QA,
      sequentially. Calls each stage's function directly (not via
      `.delay()`) since produce needs the specific `episode_id`
      ranking just created and must genuinely wait for it -- same
      "call it in-process, don't auto-chain" rationale already used by
      `_produce_story_content`. Stops once QA'd; human approval stays
      a manual dashboard action, unchanged.
- [x] `celery_app.conf.timezone = "Asia/Kolkata"` was already set (from
      an earlier session) -- verified it actually works before relying
      on it: `celery_app.now()` correctly returns Asia/Kolkata time
      regardless of the container's UTC system clock, so
      `crontab(hour=22, minute=0)` genuinely means 10 PM IST, not 10
      PM UTC. `beat`'s own "LocalTime" log line is just the OS clock
      (UTC) -- a display artifact, not what's actually used for
      scheduling.
- [x] **Verified for real, not just configured**: added a temporary
      one-off schedule entry firing ~1 minute out, confirmed via logs
      that `beat` enqueued it and `worker` executed it (a real
      `ingest_news` run, 26 articles inserted, correctly auto-chained
      into dedup), then removed the test entry. Separately,
      manually triggered the new `run_nightly_cutoff` task directly:
      created episode #6 (51 eligible, 25 primary + 5 backup),
      produced 25 primary + 5 backup stories (0 failures) in ~120s,
      ran QA (same expected `duration_target`-only failure as every
      other episode this session). Confirmed episode #6 via the public
      API: `video_status: ready`, playable `video_url`, full QA report.
- [ ] Added `celerybeat-schedule*` to `.gitignore` (beat's local
      last-run-time state file, written to the bind-mounted project
      root by default -- not something to commit).
- [x] ~~Running `beat` continuously in local dev means these jobs will
      actually fire whenever the stack happens to be up~~ -- user
      flagged this directly: real scheduling should be a production
      concern, not something firing unprompted during dev testing.
      Fixed by gating `beat` behind Compose's `scheduler` profile
      (`docker-compose.yml`) -- plain `docker compose up -d` (the
      normal dev command) no longer starts it at all; `docker compose
      up -d beat` (explicit name) or `--profile scheduler` starts it
      deliberately when actually wanted. Verified all three states
      directly: stopped the already-running container, confirmed a
      plain `up -d` does not recreate it, confirmed `up -d beat` still
      starts it despite no active profile (Compose's documented
      behavior -- naming a service explicitly bypasses profile
      filtering), stopped it again afterward to leave the repo in its
      intended off-by-default state. No changes to the schedule itself
      or `app/tasks/scheduled.py` -- purely gating whether the process
      that reads it runs by default.

### This session — 2026-09-17, part 23 (closed 5 small known-issue gaps in one pass)

- [x] **QA staleness on reorder/swap/edit**: new `Episode.content_changed_at`
      (migration `d8f4b2a71c93`), stamped by `reorder_episode_stories`,
      `swap_episode_stories`, and `update_story_content` (the last one
      stamps *every* episode referencing the edited story, since a story
      can belong to more than one). `qaIsStale()` (`app.js`) now also
      flags stale when `content_changed_at > qa_run_at`, not just
      `video_produced_at`. Verified all three actions end-to-end on
      episode 6 via curl: each one flipped `stale` from false to true,
      confirmed by direct timestamp comparison.
- [x] **Story #28's missing summary**: reused `article_fetcher.py`
      (built for HN link-posts) rather than writing anything new --
      wired the same `fetch_article_summary()` fallback into
      `app/tasks/ingestion.py` for any RSS entry with no summary of its
      own. Backfilled story #28 directly (confirmed via live DB query
      it was the only RSS story affected): fetched a real summary from
      NVIDIA's actual article page, regenerated its script, and
      produced its video end-to-end -- verified `video_ready`, no
      error, real audio/visual/video files.
- [x] **Non-root worker/api containers**: added `appuser` (uid 1000) in
      the `Dockerfile`. Found a real problem during verification, not
      just theoretical: `media/` is bind-mounted in local dev, and
      files created by earlier root-run containers were owned by
      `root:root` mode `755` -- a build-time `chown` alone doesn't fix
      a bind mount that overlays it, so a real produce call failed with
      `PermissionError` the first time this was tested. Fixed with the
      standard pattern for this: `entrypoint.sh` (installs `gosu`)
      starts as root, `chown -R appuser:appuser /app/media` every
      container start (idempotent, cheap, self-healing regardless of
      how `media/`'s ownership got into a bad state), then `exec gosu
      appuser "$@"` to drop to non-root before the actual long-running
      process. Verified via `docker top`: every uvicorn/celery process
      (including all worker fork-pool children) runs as uid 1000, and
      a real produce call succeeds with files owned by `appuser`.
- [x] **Two disabled RSS sources**: re-confirmed both live rather than
      trusting the 2026-09-10 note. VentureBeat AI: still HTTP 429
      (Vercel bot challenge) -- left disabled; a real fix needs a
      headless browser to clear a JS challenge, out of scope for this
      project's minimal-dependency approach, and not something to build
      bot-detection evasion for. Microsoft AI Blog: still HTTP 410 on
      the old URL, but found a real, currently-active official
      replacement -- **Microsoft Research Blog**
      (`https://www.microsoft.com/en-us/research/blog/feed/`, verified
      200/valid RSS/recent posts) -- enabled it, updated the matching
      `CREDIBILITY_WEIGHTS` key in `app/ranking/engine.py` (found by
      checking, not assumed -- renaming a source name without updating
      this would have silently dropped it to the 0.60 default weight).
      Verified via a real ingestion run: `sources_processed: 11`,
      Microsoft Research Blog fetched successfully (10 entries seen, 0
      errors -- all happened to be outside the current time window,
      not a bug).
- [x] **Concurrent-ingestion race condition**: `app/tasks/ingestion.py`
      and `app/tasks/ingestion_hackernews.py` both used to check
      `existing_story` per-entry but commit once at the end of a whole
      source/run -- a `uq_stories_url` collision (a real possibility
      now that RSS and HN ingestion fire at the *same* Celery Beat
      scheduled times, not just theoretical) would roll back every
      other valid insert in that batch via the broad `except
      Exception`, not just the colliding row (the HN file didn't even
      have a try/except around its single end-of-run commit -- a
      collision there would have crashed the whole task). Fixed by
      committing each story individually with a narrow
      `try/except IntegrityError`. Verified both tasks still ingest
      normally after the change (real runs, sane insert/duplicate
      counts, zero unexpected errors).

Committed as `a71ea3c` once the user asked for it explicitly (per
current standing instruction, never committed proactively).

### This session — 2026-09-17, part 24 (first automated test suite)

- [x] Added `tests/` (pytest, 55 tests, `requirements-dev.txt`) --
      addresses the "no automated tests" known issue below, though
      scoped to the deterministic logic layer + targeted regression
      tests, not the full Celery/API surface (see what's NOT covered,
      noted below).
      - Pure unit tests, no DB/network/ffmpeg needed: AI-relevance
        filter, dedup similarity/matching, ranking engine (all 4
        sub-scores + the blended total), script generation (including
        direct regression cases for this session's truncation-marker
        and promo-sentence fixes), publisher resolution.
      - `tests/conftest.py`'s `db_session` fixture: an in-memory SQLite
        database for testing functions that take `db` as an explicit
        parameter (e.g. `_produce_story_content`) without touching the
        real Postgres database. Verified this is a faithful stand-in
        for the actual bugs being guarded against -- SQLAlchemy raises
        the same `IntegrityError` on a unique-constraint violation
        under SQLite as under Postgres.
      - Direct regression tests for the 3 hardest-won bugs this
        session found by hand: the AV-duration desync (real ffmpeg via
        `compose_video`/`generate_gap_clip`, not mocked -- generates a
        real synthetic audio+image, asserts output duration matches
        within one frame), the script-clobbering bug (`_produce_story_
        content` preserves an edited script, still regenerates voice/
        visual/video from it, and separately still generates a script
        for a genuinely new story -- 3 tests covering the edit case,
        the new-story case, and the retry-after-failure case so the
        fix doesn't overcorrect), and the ingestion race condition
        (per-row commit + narrow `except IntegrityError` isolates one
        collision from other valid inserts in the same batch).
      - **Verified the regression tests actually regress, not just
        pass tautologically**: temporarily reintroduced the exact old
        script-clobbering bug (reverted the `if not content.script_
        text:` guard), confirmed 2 of 3 tests in that file immediately
        failed, then restored the real fix and confirmed all 55 pass
        again. Didn't just trust that the tests "look right."
      - Documented how to run them in `README.md` (`pip install -r
        requirements-dev.txt` + `pytest` inside the `api` container, no
        separate test database needed).
- [ ] **Not covered by this pass**: the Celery task orchestration layer
      itself end-to-end (`produce_episode_video`, `ingest_news`, `run_
      ranking_selection` as whole tasks -- these open their own
      `SessionLocal()` internally rather than accepting `db` as a
      parameter, which would need either monkeypatching `SessionLocal`
      per-module or a refactor to accept an injected session) and the
      FastAPI endpoints (`app/main.py`) themselves. A reasonable next
      layer to add, not attempted in this pass.

### This session — 2026-09-17, part 25 (Fact Extraction + Verification Engine)

Built both of `project.md`'s remaining "News Engine" phases (per user
request, one after the other, then tested end-to-end). **Soft signal
only** (explicit user decision after discussing hard-gate vs.
soft-signal tradeoffs) -- neither phase excludes a story from ranking;
both add data + a small score nudge for editorial awareness, matching
how Automated QA already works here (surfaced prominently, never
auto-blocking).

- [x] **Fact Extraction** (`app/extraction/fact_extractor.py`):
      deterministic keyword/regex extraction of companies, products,
      event categories (launch/funding/acquisition/lawsuit-regulatory/
      research/partnership/personnel/safety-policy), explicit dates,
      and numeric claims ($/%/multipliers) -- same no-LLM pattern as
      `ai_relevance.py`. "Claims" specifically means the concrete,
      checkable numeric assertions a deterministic pass can actually
      pull out, not free-form claim understanding (that needs an LLM,
      which this project doesn't use -- see `CLAUDE.md`).
- [x] **Verification Engine** (`app/verification/engine.py`):
      `verify_story()` -- verified if corroborated by >=1 other
      independently-discovered outlet (reusing the same cross-source
      signal as ranking's momentum score) OR from a source credible
      enough to be its own primary source (>=0.85 credibility,
      reusing `compute_credibility_score`) -- e.g. OpenAI's own blog
      doesn't need a second outlet to "confirm" its own announcement.
      Otherwise "unverified" -- a real, expected, non-excluding value.
- [x] New `Story` columns (migration `e2a9c74f1b06`): `extracted_facts`
      (JSON text), `verification_status` (pending/verified/unverified),
      `verification_reason`. New task
      `app/tasks/verification.run_fact_extraction_and_verification`,
      chained automatically after dedup (the one place both
      `ingest_news` and `ingest_hackernews_stories` already funnel
      through) plus a manual `POST /api/v1/verification/run`.
- [x] Ranking integration (`app/ranking/engine.py`): a flat
      `VERIFICATION_BONUS = 0.05` added to `compute_total_score`'s
      total when verified -- deliberately NOT folded into the existing
      `SCORE_WEIGHTS` (already sum to 1.0, tuned against real headline
      pairs) so it can't destabilize that balance. Documented (and
      tested) that this means the total can exceed 1.0 by exactly the
      bonus at the extreme, rather than falsely claiming it's still
      hard-bounded.
- [x] Dashboard/API visibility (since a soft signal needs to actually
      be seen to matter): `_serialize_episode` exposes all 3 new
      fields; the story list shows a second pill (verified=green,
      unverified=amber, pending=neutral, reusing existing `.pill`
      classes) with the reason as a tooltip; the edit panel's metadata
      block shows verification status/reason and a readable summary of
      extracted facts.
- [x] 25 new pytest tests (`test_fact_extractor.py`,
      `test_verification_engine.py`, extended `test_ranking_engine.py`)
      -- 80 total now passing.
- [x] **Found and fixed 2 real bugs via live-data testing, not just unit
      tests**: (1) `EVENT_KEYWORDS["funding"]` only listed "raises",
      missing "raised"/"raising" -- a real headline ("after raising
      $300 million") silently failed to match until the tense variants
      were added (extended several other categories the same way --
      launch/acquisition/lawsuit/partnership/personnel all had similar
      gaps). (2) bare "policy" in the safety_policy category
      false-matched a completely unrelated eBPF access-control story
      ("enforcing a policy (allow/deny)") -- replaced with more
      specific phrases ("ai policy", "ai regulation", "regulatory").
      Confirmed the fix directly: reset that story to pending, re-ran
      verification, confirmed the false-positive category was gone and
      the genuine "90%" numeric claim was still correctly extracted.
- [x] **Verified end-to-end against the live stack, not just code
      review**: ran the migration; restarted `worker` cleanly (task
      registered); triggered `/api/v1/verification/run` on 63 real
      eligible stories -- 30 verified/33 unverified, spot-checked
      individual reason strings against real story content (both the
      primary-source and single-source-unverified cases look
      editorially correct); triggered a real HN ingestion and confirmed
      the full auto-chain (ingest -> dedup -> verification) fired
      correctly end-to-end on just the newly-inserted stories; ran
      `/api/v1/episodes/select` (episode #7, 63 eligible, 25 primary +
      5 backup) and confirmed real `rank_reason` strings show the
      bonus applied only when verified, with verified stories
      generally (not absolutely -- it's a nudge) outranking comparable
      unverified ones; confirmed the new fields appear in
      `GET /api/v1/episodes/{id}` and the dashboard's served
      `app.js`/`style.css`.

### This session — 2026-09-17, part 26 (never re-select an already-narrated story)

- [x] **Real bug, user-reported, confirmed against live data before
      fixing**: episodes #6 and #7 (both created earlier this session
      from the same growing story pool) shared 14 of 25 primary slots
      (19 of 30 total) -- nothing in `run_ranking_selection`
      (`app/tasks/ranking.py`) excluded a story just because an earlier
      episode already selected it. User's reasoning: the same news/
      story would end up narrated in multiple daily episodes.
- [x] Fixed by excluding any story that's ever had
      `EpisodeStory.selection_status == "primary"` in **any** episode,
      regardless of that episode's later approve/reject status ("in
      any case", per the user) -- queried fresh on every ranking run
      (not a stored flag), so a backup promoted to primary later via
      the dashboard's swap is caught by the very next run too, with no
      extra bookkeeping needed. Scoped to **primary only** (confirmed
      with the user): a story that only ever sat as an unused backup,
      never narrated, remains eligible for a future episode's primary
      slot. New `already_used_excluded` count added to the task's
      result dict for visibility, matching this codebase's habit of
      surfacing real counts for every stage.
- [x] **Verified end-to-end against the live stack**: triggered a new
      selection (episode #8) against the same pool that produced the
      14/25 overlap -- result: `already_used_excluded: 60`,
      `eligible_stories: 6` (this project's small candidate pool is
      now mostly "used up" after 3 selection runs today against
      largely the same underlying stories -- an honest, expected
      consequence of the fix, not a bug). Confirmed via direct SQL
      **exact zero overlap** between episode #8's primary stories and
      every prior episode's primary stories (not just "looks smaller").
      Also confirmed the positive case on real data: story #74, which
      sat as an unused **backup** in episode #6 (never narrated),
      correctly remained eligible and was selected as **primary** in
      episode #8 -- proving the primary-only scope, not just asserting
      it. Full `pytest` suite (80 tests) still passes.
- [ ] Not covered by an automated test (same documented gap as
      `run_ranking_selection`'s other logic -- it opens its own
      `SessionLocal()` internally rather than accepting `db` as a
      parameter, see part 24's "not covered by this pass" note).
      Verified live/manually instead, per above.

### This session — 2026-09-17, part 27 (wire QA's source_verification check to the Verification Engine)

- [x] Surfaced during a live dashboard click-through (not a fresh
      audit): `app/qa/video_qa.py`'s `source_verification` check still
      hardcoded `passed: None` / "not implemented -- no Verification
      Engine exists yet", stale since part 25 actually built the
      Verification Engine. Reported it rather than fixing silently in
      the same pass, per the project's "don't silently expand scope"
      rule; fixed once asked.
- [x] Fixed by checking each included story's real
      `Story.verification_status`: `passed = (unverified count == 0)`,
      same "all N have X" pattern as `source_links`/`captions_present`/
      `audio_present`. Non-gating, same as `duration_target` -- QA
      already doesn't block `/approve` (see `main.py`), so a real FAIL
      here is an honest signal, not a new hard gate.
- [x] **Verified against the live stack**: restarted `worker`, re-ran
      `/qa` on episode #9 -- report now reads `FAIL -- source_verification
      -- 15 unverified stories included: [97, 98, 92, ...]` with real
      story IDs matching the unverified pills already shown in the
      dashboard's story list, instead of the old permanent SKIP. Full
      `pytest` suite (80 tests) still passes.

### This session — 2026-09-17, part 28 (real caption timing via edge-tts, not a character-count estimate)

- [x] `build_captions()` (`app/content/video_composer.py`) used to
      allocate each sentence a slice of the total audio duration
      proportional to its character count -- a guess, not a
      measurement, and the known, already-documented reason captions
      could drift on longer/uneven sentences.
- [x] Fixed at the source instead of improving the guess: `edge-tts`
      (the one real TTS model in this pipeline) already reports exact
      per-sentence timing while it synthesizes, via
      `Communicate(..., boundary="SentenceBoundary").stream()` --
      `{"offset", "duration"}` in 100-ns ticks per sentence. No new
      dependency; the data was already there, just not being read.
      `synthesize_voice()` (`app/content/voice_generator.py`) now
      streams (instead of `.save()`) to capture these boundaries
      alongside the audio bytes, converts ticks to seconds, and returns
      them as `[{"text", "start", "end"}, ...]`. `build_captions()` now
      just writes that real timing to `.srt` -- no estimation logic
      left at all.
- [x] New `StoryContent.caption_segments` column (migration
      `f9c2a5e8d371`) persists the real timing between the voice stage
      and the video stage, since those are separate Celery tasks (in
      `app/tasks/content.py`'s per-story chain) that can run at
      different times -- same reason `audio_path`/`captions_path` are
      already persisted rather than passed in memory. Also cleared
      alongside `audio_path`/`captions_path`/etc. in `PATCH
      /stories/{id}/content`'s edit-panel reset, so an edited script
      regenerates real timing too, not stale segments from the old text.
      Both content pipelines updated to match --
      `app/tasks/content.py`'s Celery-chained per-story stages AND
      `app/tasks/episode_video.py`'s synchronous per-episode production
      (including the intro/outro branding clips).
- [x] **Verified against the live stack**: reset story #121's content
      via the edit-panel endpoint, re-triggered `/produce`, confirmed
      `caption_segments` in the DB holds real per-sentence
      `start`/`end` values, the resulting `.srt` file's timestamps
      match them exactly, and `ffprobe`'d the composed video's duration
      (9.88s) against the real audio duration (9.888s) -- within the
      established one-frame tolerance. Added `tests/test_build_captions.py`
      (2 new tests) covering the exact "short sentence then a much
      longer one" case that broke the old proportional estimate. Full
      `pytest` suite: 82 tests, all passing.

### This session — 2026-09-18, part 29 (voice selection: sample every English edge-tts voice)

- [x] User feedback: didn't like the current `en-US-GuyNeural` voice
      and asked for a way to compare alternatives with actual audio
      samples, not just names.
- [x] Since edge-tts is free/local with no API key (see "No LLM in the
      pipeline" in `README.md`), generated real samples rather than
      guessing from voice names/tags: `edge_tts.list_voices()` returned
      47 English voices (`en-US`, `en-GB`, `en-AU`, `en-CA`, `en-IN`,
      `en-IE`, `en-NZ`, `en-NG`, `en-KE`, `en-PH`, `en-SG`, `en-ZA`,
      `en-TZ`, `en-HK` locales). Synthesized the same two-sentence
      realistic news script (styled on this session's actual episode
      #9 headlines) through every one via `edge_tts.Communicate(...)`
      directly (a one-off script, not routed through
      `synthesize_voice()` since these are throwaway comparison
      samples, not production content) into `media/voice_samples/`
      (served automatically by the existing `/media` StaticFiles mount
      -- no new endpoint needed), plus a generated `index.html` comparison
      page with an `<audio>` player per voice, gender/locale/personality
      tags, and shortlisted voices starred and sorted first.
- [x] 46/47 succeeded; `en-IE-EmilyNeural` consistently failed
      (`NoAudioReceived`) across 3 retries despite `list_voices()`
      reporting `Status: GA` -- a real, service-side issue with that
      one voice specifically (same "confirmed still blocked" pattern
      as the VentureBeat RSS source), flagged as unavailable on the
      comparison page rather than silently omitted or retried forever.
- [x] User picked 3 shortlisted candidates from the first listening
      pass to compare further: `en-US-AriaNeural`, `en-US-JennyNeural`,
      `en-GB-RyanNeural`. Recorded in `README.md`'s Voice bullet as
      "still under review" -- `VOICE_NAME` in
      `app/content/voice_generator.py` deliberately left as
      `en-US-GuyNeural` for now until a final pick is made; this is a
      one-line change plus a full-episode re-verify when that happens
      (see `verify-episode` skill).
- [x] Also fixed a stale claim noticed in the same `README.md` section
      while editing it: the Video bullet still said captions use "a
      naive proportional estimate," which part 28 above already
      replaced with real edge-tts sentence timing -- corrected in
      place rather than left inconsistent with part 28's own entry.
- [ ] The `media/voice_samples/` directory and its one-off generator
      scripts are dev-only scratch artifacts (not committed to git,
      not part of the production pipeline) -- fine to delete once a
      final voice is chosen and confirmed working end-to-end.

### This session — 2026-09-19, part 30 (Publishing Worker: YouTube, first pass)

- [x] User decision (explicit, via three scoped questions): **YouTube
      only** for now (Instagram later, as a second adapter); **no real
      OAuth credentials yet** -- build the full integration
      ready-to-plug-in rather than waiting; **manual "Publish" button**
      only, never auto-cascading from Approve -- consistent with every
      other stage in this pipeline (Produce/QA/Approve are all manual
      triggers today).
- [x] `app/publishing/youtube_publisher.py` (new): `build_video_metadata()`
      is a pure function (title/description/tags from the episode's
      real Top 25 -- headline + source + link per story), same
      "keep the deterministic logic pure and testable" pattern as
      `compute_total_score()`/`extract_facts()`. Guards YouTube's real
      ~5000-char description cap defensively (a genuine external
      constraint, not a theoretical one, given a full 25-story
      episode). `upload_video()` does the actual YouTube Data API v3
      resumable upload; raises `YouTubeNotConfigured` **before ever
      touching the network** if `YOUTUBE_CLIENT_ID`/
      `YOUTUBE_CLIENT_SECRET`/`YOUTUBE_REFRESH_TOKEN` aren't all set,
      rather than surfacing an opaque auth error. Uploads start
      **private** by default -- no visibility control in the dashboard
      yet, so a human always makes it public/unlisted deliberately via
      YouTube Studio, never automatically on first publish.
- [x] `app/tasks/publishing.py` (new): `publish_episode_to_youtube`
      Celery task, `app/scripts/youtube_oauth_setup.py` (new): one-time
      interactive script (must run on a host with a real browser, not
      in Docker) to obtain the refresh token, documented step-by-step
      in `README.md`'s new Publishing section.
- [x] New `Episode` fields (migration `a4d7c1e69f28`): `publish_status`
      (not_published -> publishing -> published/failed),
      `youtube_video_id`, `youtube_url`, `published_at`,
      `publish_error`. New `POST /episodes/{id}/publish` endpoint --
      400s if not `approved` or video not `ready`; flips
      `publish_status` to `"publishing"` synchronously before queuing,
      same race-avoidance reason as `trigger_episode_production()`.
- [x] Dashboard: new "Publish to YouTube" button (relabels to "Retry
      Publish" after a failure, "Published ✓" and disabled once
      published -- re-publishing would create a real duplicate upload,
      a genuinely hard-to-reverse external action, unlike everything
      else this dashboard lets you redo freely), a `publish_status`
      pill (studio header + episode list table's new "Publish"
      column), a live elapsed-time indicator while publishing (same
      pattern as Produce/QA polling), the resulting YouTube link once
      published, and the failure reason inline in red once failed.
- [x] New dependencies: `google-api-python-client`, `google-auth`,
      `google-auth-oauthlib`, `google-auth-httplib2` -- the first
      genuine exception to this project's "no API key required"
      pipeline claim (see `README.md`'s No-LLM section), and
      deliberately so: publishing to a real channel inherently needs a
      real account, unlike every deterministic content-generation
      stage before it.
- [x] Real bug caught live, not by code review: the new task module
      wasn't in `app/worker/celery_app.py`'s `include=[...]` list, so
      `.delay()` silently queued a task the worker never picked up --
      caught by checking the worker's `[tasks]` startup log before
      declaring this done (per this repo's "verify against the live
      system" rule), not assumed from the code looking correct. Fixed
      and added as a new `CLAUDE.md` dev-environment gotcha so it isn't
      rediscovered next time a new task module is added.
- [x] **Verified against the live stack**: rebuilt the `api`/`worker`
      images (new deps installed cleanly), applied the migration,
      confirmed the worker's `[tasks]` log lists
      `app.tasks.publishing.publish_episode_to_youtube`. Real endpoint
      test: `/publish` on a **non-approved** episode (#10) correctly
      400s ("Episode must be approved before publishing"); `/publish`
      on an **approved, video-ready** episode (#9) correctly queues,
      runs, and fails fast with the exact `YouTubeNotConfigured`
      message (no real credentials exist yet -- this is the accurate
      current state, not a bug), setting `publish_status = "failed"`
      and `publish_error` in the DB. Confirmed the dashboard renders
      all of this correctly in a real browser: the red error banner,
      the "failed" pill, the "Retry Publish" button relabel, and the
      new episode-list "Publish" column across every episode's real
      status. Added `tests/test_youtube_publisher.py` (3 new tests) for
      the pure metadata builder (including the description-truncation
      edge case) and the fail-fast credentials guard -- the full Celery
      task itself is intentionally not unit tested, same accepted gap
      as `run_ranking_selection`/`run_episode_qa` (opens its own
      `SessionLocal()`; verified live instead, per above). Full
      `pytest` suite: 85 tests, all passing.
- [ ] Not yet tested against a real YouTube account -- genuinely
      blocked on the user creating a real Google Cloud OAuth client
      and running the one-time setup script; the code path beyond
      `YouTubeNotConfigured` (the actual upload, response parsing,
      real quota/auth-error handling) is unverified until then.
- [ ] Instagram publishing, a dashboard visibility control (private/
      unlisted/public choice at publish time), and multi-platform
      publish status are all deliberately out of scope for this first
      pass -- see "Next up" below.

### This session — 2026-09-20, part 31 (full-text content similarity: stronger dedup + historical repeat detection)

- [x] **User's two goals, verbatim**: (1) don't publish near-duplicate
      stories under different headlines but the same underlying story/
      context -- today's dedup was title-only (`difflib`/Jaccard, 48h
      window), which the module's own comment already admitted misses
      heavily-paraphrased cross-outlet coverage; (2) don't repeat, to
      the public, a story covering the same event as something already
      narrated in a past episode -- the existing "never re-select"
      check (part 26) is identity-based (same story row only), not
      content-based, so a *different* story about an already-narrated
      event was never caught.
- [x] **Explicit design choices** (after discussing tradeoffs, incl.
      the project's two prior scraping rejections -- Anthropic/Meta AI
      blogs, GitHub Trending, both about using scraping as a
      *discovery* mechanism via fragile unofficial mirrors, a different
      problem from fetching the body of a URL a story already has via
      an existing official source): full article-body extraction
      (`trafilatura`, new dependency) over a lighter excerpt, and
      TF-IDF + cosine similarity (`scikit-learn`, new dependency) over
      fact-overlap-only. Both scopes checked: same-day batch AND the
      full historical corpus of past primary stories. No new ingestion
      sources; no bot-detection evasion (a blocked/paywalled fetch
      degrades to a `content_fetch_status`, never fought).
- [x] New `app/content/article_extractor.py` (full-text fetch,
      mirrors `app/sources/article_fetcher.py`'s defensive contract),
      `app/filters/content_similarity.py` (pure TF-IDF/cosine
      functions), `app/tasks/content_dedup.py`
      (`enrich_and_dedup_by_content`, new pipeline stage between
      title-dedup and verification). Reused the two previously-dead
      `Story.raw_content`/`content_hash` columns; added
      `repeats_story_id`/`repeat_reason`/`content_fetch_status`
      (migration `b6d3f8c1a927`). `app/tasks/ranking.py`'s eligibility
      filter extended with `repeats_story_id.is_(None)`, complementing
      (not replacing) the existing identity-based exclusion.
- [x] **Real bug caught live, not by code review**: the first live run
      flagged 85 stories as "repeating" a past primary story -- but
      closer inspection showed most were a story flagged as repeating
      **itself**, at `content_tfidf_cosine=1.00`. Root cause: the
      candidate query didn't exclude stories that are themselves
      already past primaries, so they ended up being compared against
      a historical corpus that included their own row. Fixed by
      computing `historical_story_ids` once, up front, and excluding
      it from the candidate pool entirely (not just from the
      historical-repeat pass) -- exactly the kind of thing this
      project's "verify against the live system" rule exists to catch;
      re-ran clean after the fix (2 genuine historical repeats found,
      zero self-matches).
- [x] **Verified against the live stack**: `docker compose build`
      succeeded with no extra system packages needed for `lxml`/
      trafilatura on `python:3.12-slim`. Ran the real content-dedup
      task against 115 real never-fetched stories: 100 fetch
      successes, 15 graceful fallbacks (non-HTML/empty-extraction/
      fetch-error), 12 same-batch content duplicates found, 2 genuine
      historical repeats found (after the self-match bug fix). A known
      already-blocked site (VentureBeat's Vercel challenge, part 23)
      confirmed to degrade to `content_fetch_status="fetch_error"`
      with no crash and no evasion attempt. `POST /episodes/select`
      confirmed end-to-end: `content_repeats_excluded` (new
      observability count added to the result dict, matching this
      project's habit) matched exactly. Full `pytest` suite: 92 tests
      (7 new), all passing.
- [x] **Found a genuine, unambiguous true positive** validating the
      whole feature's premise: story #53 "Your AI agents can now
      control your Google Home devices" and story #65 "Google will now
      let any AI agent run your smart home" -- same story, completely
      different headline, `content_tfidf_cosine=0.77`. Title-only
      dedup would never have caught this pair.
- [x] **Real, measured evidence the `CONTENT_SIMILARITY_THRESHOLD =
      0.35` starting guess is likely too loose for a HARD exclusion**
      (manually reviewed every match from the live run: several
      borderline 0.35-0.41 matches, and even one mid-range 0.54 match,
      looked like two distinct, topically-adjacent opinion/analysis
      pieces sharing AI-safety vocabulary rather than the same
      underlying news event -- e.g. "Is the AI safety debate about
      safety or control?" vs. "A brief history of AI executives
      calling for regulation" at 0.35. The strong matches -- 0.54+
      cases that were genuine dupes, 0.77, 1.00 -- all looked correct).
      **Surfaced to the user rather than silently picking a new
      number; decision: make `repeats_story_id` a soft signal**, same
      as `verification_status`/Automated QA -- `app/tasks/ranking.py`'s
      eligibility query no longer filters on it at all (a flagged
      story is still fully selectable); instead
      `repeats_story_id`/`repeat_reason` are surfaced in the dashboard
      (`_serialize_episode` in `app/main.py`, a "possible repeat" pill
      + edit-panel row in `app/dashboard/app.js`, matching the
      verification pill's exact styling/pattern) so the editor decides.
      `run_ranking_selection`'s result dict reports
      `content_repeats_flagged` (informational count, not an exclusion
      count) for observability. **Verified live**: re-ran selection
      after this change -- both previously-flagged stories were
      correctly included in the eligible pool and one was actually
      selected as primary, confirming the signal is genuinely
      non-blocking. Revisit hard-excluding once a proper multi-week
      score-logging/tuning pass (still planned) provides real data.
- [ ] No stemming/lemmatization in the TF-IDF vectorizer -- confirmed
      live that this measurably hurts recall for genuinely independent
      paraphrases (a synthetic "two journalists write from scratch"
      test pair scored only 0.14; the original test fixture had to be
      changed to a more realistic "lightly-edited/syndicated coverage"
      pair, which scores reliably). Documented as a known limitation in
      `app/filters/content_similarity.py`, same honest-caveat style as
      `app/filters/dedup.py`'s own paraphrase-limitation comment.
- [ ] `raw_content` is not backfilled for historical primary stories
      that predate this change -- they participate in the historical
      corpus via the `raw_summary`/title fallback until naturally
      refreshed. Deferred, not blocking.

### This session — 2026-09-21, part 32 (real brand identity: 5squareFeed)

- [x] User bought the domain `5squarefeed.in`, created
      `5squarefeed@gmail.com`, and commissioned a real logo/icon set
      under the name **5squareFeed** (tagline "25 Stories A Day",
      "Today's AI. A brighter tomorrow.") -- replacing the placeholder
      "AI News Platform"/"AI Daily 25" names used everywhere before a
      real brand existed.
- [x] Full text rebrand across every user-facing surface: dashboard
      title/wordmark (`app/dashboard/index.html`), FastAPI app title
      (`app/main.py`), video intro/outro branding cards + narration
      (`app/tasks/episode_video.py`), YouTube video title/description/
      tags (`app/publishing/youtube_publisher.py`'s `BRAND_NAME`),
      `README.md`, `CLAUDE.md`'s project-identity section, the
      `episode-verifier` agent, and the corresponding test assertions
      (`tests/test_youtube_publisher.py`). Deliberately did NOT rewrite
      historical dated `TODO.md` entries (they describe what was true
      at the time) -- only the doc's living title header.
      `app/content/visual_generator.py`'s branding-card generator still
      only draws text, not the real logo image assets -- noted as
      future work, not done here (a separate, larger image-compositing
      task, not asked for).
- [x] **GitHub repo rename to `5squarefeed`**: `gh` CLI isn't installed
      on this machine, so this was done manually by the user via
      GitHub's web UI rather than `gh repo rename` -- local `origin`
      remote URL updated to match afterward.
- [x] **Docker Compose project rename, `5min-ai-news` -> `5squarefeed`**
      (the one place CLAUDE.md explicitly warns this is dangerous --
      a naive rename would silently point at a fresh empty Postgres
      volume). Done via a full backup/migrate/verify sequence, not a
      raw rename, per the user's explicit instruction:
      1. `pg_dump -F c` (custom format, schema+data+sequences) from the
         live `5min-ai-news-postgres-1` container, copied to
         `.db-backups/5min-ai-news-backup.dump` on the host (now
         gitignored).
      2. `docker compose down` (volumes preserved by default, not
         `-v`), confirmed the old `5min-ai-news_postgres_data` volume
         was still intact via `docker volume ls`.
      3. Changed `docker-compose.yml`'s `name:` to `5squarefeed`,
         brought up only `postgres` -- confirmed a genuinely fresh,
         separate `5squarefeed_postgres_data` volume was created (old
         one untouched).
      4. `pg_restore --no-owner --no-privileges` the dump into the new,
         empty database -- succeeded cleanly (schema, data, sequences,
         indexes, FKs, `alembic_version` all restored in one pass).
      5. **Verified byte-for-byte via independent row counts**: spun up
         the OLD volume in an isolated, throwaway `postgres:16`
         container (mounted read-only via the same volume, never
         touching the live stack) purely to compare counts against the
         new database -- `stories` (153), `episodes` (13),
         `episode_stories` (223), `story_content` (77) matched exactly
         on both sides. Not just "looks right" -- an actual independent
         cross-check, per this project's verification-before-declaring-
         done standard.
      6. Brought the full stack up under the new project name,
         confirmed the API/worker/dashboard all work correctly against
         the migrated data (13 episodes returned correctly, all 14
         Celery tasks registered, dashboard title/wordmark render
         "5squareFeed"). Full `pytest` suite: 92 tests, still all
         passing.
      7. Both the old volume (`5min-ai-news_postgres_data`, currently
         orphaned but not deleted) and the `.db-backups/` dump file are
         being kept as a safety net for now -- deleting either is a
         real, irreversible action not taken without the user asking
         for it explicitly.
- [x] Local project folder deliberately left unrenamed (user's explicit
      choice) -- this session is rooted in
      `D:\developer\projects\5min-ai-news` and renaming it mid-session
      would have risked breaking file access for the rest of the
      session. `5min-ai-news` remains both the local folder name and
      the internal dev/repo-level identifier -- see the updated
      `CLAUDE.md` project-identity section for the final naming split.
- [ ] The old, now-orphaned `5min-ai-news_postgres_data` Docker volume
      and the `.db-backups/5min-ai-news-backup.dump` file are safe to
      delete once the new `5squarefeed` project has been live a while
      with no issues -- not done automatically, ask before removing.

### This session — 2026-09-21, part 33 (real logo -> favicon/app-icon set)

- [x] User's designer-provided logo assets (light/dark/gradient "5²"
      icon variants + a full brand sheet) were only shared as generic
      renders before -- extracted a clean, tightly-cropped 562x562
      master from the highest-quality standalone render (light icon on
      white), plus lower-res dark/gradient variants cropped from the
      full brand sheet, since only the light icon had a dedicated
      high-quality standalone image. All three plus the original,
      unmodified source files kept in `app/dashboard/branding/` as the
      brand asset source of truth for future use.
- [x] Generated a full favicon/PWA icon set from the light-icon master
      (`app/dashboard/`): `favicon.ico` (multi-res 16/32/48),
      `favicon-16x16.png`/`favicon-32x32.png`/`favicon-48x48.png`,
      `apple-touch-icon.png` (180x180, iOS home screen), Android/PWA
      `android-chrome-192x192.png`/`android-chrome-512x512.png`, and a
      `maskable-icon-512.png` (content scaled to ~72% of canvas so an
      Android adaptive-icon circular mask can't clip the logo). Added
      `site.webmanifest` (name/icons/theme_color -- theme_color
      `#0c101c` and background_color `#f5f7fb` pulled directly from
      `style.css`'s own `--bg` tokens, not invented) so the dashboard
      is installable as a home-screen "app" with the real icon, not
      just a bookmarked tab.
- [x] Wired all of it into `app/dashboard/index.html`'s `<head>` --
      favicon links (ico + 3 png sizes), apple-touch-icon,
      manifest link, theme-color meta tag.
- [x] **Verified live**: every new file returns `200` from the running
      dashboard (`favicon.ico` through `site.webmanifest`), the
      manifest JSON is well-formed and correct, and the generated
      icons were visually inspected at actual output sizes (48px,
      512px, and the maskable variant) -- the "5²" mark stays
      legible even at favicon size.
- [x] **Real mistake caught by the user asking to cross-check, fixed**:
      the light-icon master above was built from a separate standalone
      JPEG render, not from this brand sheet's own labeled "LIGHT ICON"
      reference -- never directly compared the two before using it.
      They turned out to be two different icon treatments: the JPEG
      had an extra inset double-bezel/ring style with more internal
      padding; the brand sheet's actual "LIGHT ICON" (the canonical,
      labeled reference) is a plain single rounded-square card, glyph
      filling more of it. Confirmed by cropping the sheet's own
      LIGHT/DARK/GRADIENT thumbnails directly and comparing side by
      side against what had been generated. Rebuilt the light-icon
      master from a tight crop of the sheet's own reference card
      instead (native 308x308, smaller than the JPEG's 562x562 -- a
      real, accepted resolution tradeoff for correctness), and
      re-cropped dark/gradient masters slightly tighter at the same
      time. Regenerated and re-verified the entire favicon/PWA set
      live from the corrected masters. The standalone JPEGs
      (`icon-light-original.jpg`, `logo-vertical-original.jpg`) are
      kept in `app/dashboard/branding/` as reference material only --
      not the source for any generated icon.

### This session — 2026-09-21, part 34 (separate dev/prod YouTube credentials + channels)

- [x] User's call, after discussing the tradeoff: dev and prod get
      **fully separate** Google Cloud OAuth clients *and* separate
      destination YouTube channels, not just separate secrets pointed
      at one channel. Reason (from that discussion): YouTube's API
      quota is tracked per Cloud project, not per channel, so sharing
      prod's credentials for dev testing risks burning the day's quota
      on test uploads and blocking a real publish; a separate channel
      also keeps dev's private test uploads out of the real channel's
      video library entirely.
- [x] `app/config.py`: `youtube_client_id`/`_client_secret`/
      `_refresh_token` are now **computed properties**, not raw
      fields -- they resolve to `youtube_dev_*` or `youtube_prod_*`
      based on a new `youtube_environment` setting (`"dev"` default,
      `"prod"` the other option). Both credential pairs can be
      configured in `.env` simultaneously; switching modes is a
      one-line env var change, never editing secrets back and forth.
      `app/publishing/youtube_publisher.py` and
      `app/tasks/publishing.py` needed **zero changes** to their own
      logic -- they still just read `settings.youtube_client_id` etc.,
      unaware credential resolution got smarter underneath them.
- [x] `app/tasks/publishing.py` now logs and returns which
      `youtube_environment` a publish actually used -- a real,
      previously-possible mistake (publishing to the wrong channel
      without noticing, since dev/prod look identical from inside the
      task) now has an explicit trail.
- [x] Dashboard: a `youtube_environment` pill next to the Publish
      button, styled distinctly for `prod` (red/bold/bordered) vs
      `dev` (neutral) -- a visible safety rail so a real-channel
      publish is never accidentally clicked while believing you're
      still in dev mode. New `_serialize_episode` field (read fresh
      from settings at request time, not stored per-episode -- it's
      config, not episode data).
- [x] `app/scripts/youtube_oauth_setup.py` is now environment-aware:
      prints which `YOUTUBE_ENVIRONMENT` it's running for, reminds the
      user to sign in with the right channel's account during the
      browser consent flow, and tells them the exact env var
      (`YOUTUBE_DEV_REFRESH_TOKEN` vs `YOUTUBE_PROD_REFRESH_TOKEN`) to
      save the resulting token under. Meant to be run twice total, once
      per environment.
- [x] **Verified live**: confirmed `youtube_environment` defaults to
      `"dev"` and `youtube_client_id` correctly resolves to the
      `youtube_dev_client_id` field; confirmed overriding
      `YOUTUBE_ENVIRONMENT=prod` correctly switches resolution to the
      prod fields instead; confirmed the new field appears correctly
      in a real `GET /episodes/{id}` response. One real test failure
      caught and fixed: `test_upload_video_fails_fast_when_not_configured`
      tried to monkeypatch the now-read-only `youtube_client_id`
      property directly, which pydantic correctly rejects (no setter)
      -- fixed to monkeypatch the underlying `youtube_dev_*` fields
      instead. Full `pytest` suite: 92 tests, all passing.
- [x] User completed the manual setup themselves (dev Brand Account
      channel, Cloud project, OAuth client, `youtube_oauth_setup.py`
      run) -- `YOUTUBE_DEV_CLIENT_ID`/`_SECRET`/`_REFRESH_TOKEN` all
      landed in `.env`. Confirmed `youtube_configured` reads `True` in
      both the `api` and `worker` containers after a
      `--force-recreate` (a plain `docker compose restart` does not
      re-read `.env` -- only container recreation does).
- [x] **Real milestone, verified live, not assumed**: triggered a real
      `/publish` on episode #9 (already `approved`/`ready` from earlier
      testing). Result: `publish_status: "published"`,
      `youtube_url: "https://youtu.be/bfCxIhsPw5w"`,
      `publish_error: null`. Worker log explicitly confirms
      `YOUTUBE_ENVIRONMENT='dev'` was used throughout, and the upload
      (a real 9.5MB video) completed in ~9.7s. This is the **first
      real network call this integration has ever made** -- everything
      up to this point (the YouTubeNotConfigured fail-fast path, the
      dashboard UI, the dev/prod credential switch) had only been
      tested without real credentials. The Publishing Worker (part 30)
      is now genuinely, not just structurally, complete for YouTube/dev.
      Prod remains unconfigured until the user repeats the same manual
      setup with a `YOUTUBE_PROD_*` pair and the real "5squareFeed"
      channel.
- [x] **Follow-up, resolved**: user reported the published description's
      source links rendered as plain text, not clickable, on the real
      watch page (not just Studio's editor). Investigated properly
      before assuming a bug: re-derived the exact description
      submitted at publish time from the DB and confirmed it was
      clean (25 well-formed `https://` URLs, correct ASCII spacing
      around each, no truncation, no invisible/non-breaking-space
      characters); confirmed via the watch page's accessibility tree
      that zero `&lt;a&gt;` tags existed around the URL text (not just a
      styling issue). Root cause: a real, one-off YouTube channel
      verification gate ("To make external links clickable, first
      complete a one-off verification") -- unrelated to our data,
      confirmed by the user completing it in YouTube Studio, after
      which the same video's links immediately became real, clickable
      `<a href="https://www.youtube.com/redirect?...">` links (checked
      the accessibility tree again to confirm, not just visually).
      **The prod channel will need this same one-off verification
      completed too** before its first real publish's links will be
      clickable -- add it to the prod setup checklist.

### This session — 2026-09-21/22, part 35 (Notification Worker: detection + audit trail, no delivery yet)

- [x] Scoped via explicit user decisions before building (matching
      this project's habit of pinning down design questions rather
      than guessing): **no real delivery channel yet** (email/Slack) --
      build detection + a DB record + a loud worker-log line only, add
      real delivery later. **Trigger scope deliberately narrow**: only
      episode video production totally failing and YouTube publish
      failing -- explicitly NOT individual story content failures
      (already tolerated/expected by design, would be noisy) and NOT
      QA check failures (soft signal, `duration_target` is a known,
      always-fails limitation, not a real problem).
- [x] New `Notification` model (migration `c8f1a5d92e63`): `category`
      (fixed small set: `episode_video_failed` /
      `episode_publish_failed`), `episode_id`, `message`, `created_at`.
      New `app/notifications/notifier.py`: `notify(db, category,
      episode_id, message)` -- takes the caller's existing db session
      (same pattern as `mark_content_failed()` in
      `app/tasks/content.py`) rather than opening a new one, so it's
      naturally atomic with the failure-status commit that already
      happens right after it, and is directly unit-testable (no
      `SessionLocal()` of its own to mock around).
- [x] Wired into all 3 real failure points in
      `app/tasks/episode_video.py` ("No primary stories", "No stories
      produced successfully", concat failing) and the 1 failure point
      in `app/tasks/publishing.py` (any exception during upload).
      New `GET /api/v1/notifications` endpoint (newest first) -- the
      only way to see them right now short of the worker's own logs,
      since there's no dashboard panel or real delivery yet.
- [x] **Verified live with real triggers, not mocks**: inserted a
      throwaway episode with zero primary stories, called `/produce`,
      confirmed the exact log line
      (`[notification] ALERT (episode_video_failed) episode_id=14: No
      primary stories`) and a correctly-shaped row via
      `GET /api/v1/notifications`. Then set that same episode to
      `approved`/`ready` with a deliberately nonexistent `video_path`,
      called `/publish`, confirmed a real `FileNotFoundError` from the
      actual upload attempt correctly produced an
      `episode_publish_failed` notification with the real error
      message. Cleaned up the throwaway episode and its test
      notifications afterward. Added `tests/test_notifier.py` (3 new
      tests: persists correctly, allows a null `episode_id`, doesn't
      auto-commit). Full `pytest` suite: 95 tests, all passing.
- [x] ~~No real delivery channel wired up~~ -- Slack delivery added,
      see "part 36" below.
- [ ] No dashboard UI for notifications -- the API endpoint is the only
      way to see them today. Not asked for this pass; straightforward
      to add later (mirrors every other list view in the dashboard).

### This session — 2026-09-22, part 36 (real Slack delivery for the Notification Worker)

- [x] User created a real Slack workspace (`5squarefeed.slack.com`,
      `#dev-alerts` channel) and asked to wire it up. First attempt
      went a different route than planned: a Slack app with OAuth bot
      scopes + **Token Rotation** enabled (access token + refresh
      token, the refresh token itself rotating on every use) rather
      than a plain Incoming Webhook -- meaningfully more complex to
      support correctly (would need a durable place to persist a
      constantly-changing refresh token, not just `.env`). Presented
      the tradeoff rather than silently building either path; user
      chose to switch to a plain Incoming Webhook instead (their
      existing app, just enabling that feature) -- no code changes
      needed since `_send_slack_alert()` was already built for a
      static webhook URL.
- [x] `app/notifications/notifier.py`: `notify()` now posts to
      `SLACK_WEBHOOK_URL` (new optional setting, `app/config.py`) when
      configured, via a plain `requests.post` -- never raises out to
      the caller (a Slack outage must never break the failure-handling
      flow that's already invoking `notify()` from inside its own
      except block). New `Notification.delivered` column (migration
      `d3e7b4a2c951`) records whether that post actually succeeded --
      `False` both when unconfigured and when the post itself fails,
      same "absence is a valid, non-error state" pattern as
      `verification_status`'s `"pending"`.
- [x] **Real bug caught in my own new tests, not shipped**: the first
      draft of the new Slack-delivery tests called `notify()` then
      queried the DB without an intervening commit/flush -- the
      `db_session` fixture uses `autoflush=False`, so the query
      couldn't see the pending row yet. Caught immediately by running
      the suite (3 failures), fixed by adding the missing
      `db_session.commit()` calls, matching the existing tests' own
      pattern right next to them.
- [x] **Separately caught and fixed before it could bite in production**:
      the initial test design didn't account for `settings` being
      loaded ambiently from the real `.env` -- once a real
      `SLACK_WEBHOOK_URL` exists, running `pytest` with no test-side
      guard would have made every `notify()` call in the test suite
      **actually post to the real #dev-alerts channel** (test messages
      like "No primary stories", "concat failed: boom"). Fixed with an
      autouse fixture that force-disables the webhook for every test
      in the file by default; the handful of tests that specifically
      exercise Slack delivery re-enable it with a fake URL and mock
      `requests.post`, never touching the real network.
- [x] **Verified live end-to-end with the real webhook**: recreated
      containers to load `SLACK_WEBHOOK_URL`, confirmed
      `settings.slack_webhook_url` loaded correctly, created a
      throwaway episode with zero primary stories, triggered
      `/produce`, confirmed via `GET /api/v1/notifications` that
      `delivered: true` (a real, successful HTTPS POST to Slack's API,
      not a mock) -- and asked the user to independently confirm the
      alert actually appeared in `#dev-alerts`. Cleaned up the
      throwaway episode/notification afterward. `tests/test_notifier.py`
      now has 7 tests (persistence, null episode_id, no-auto-commit,
      undelivered-when-unconfigured, delivers-when-configured,
      undelivered-when-Slack-post-fails). Full `pytest` suite: 98
      tests, all passing.

### This session — 2026-09-22, part 37 (taxonomy redesign: 5-category labels, no selection change)

- [x] Scoped via explicit user decisions before building, since
      `proposal.md`'s own "final taxonomy" section conflicts with
      decisions already made this session: it proposes a local LLM for
      classification/clustering (this project has a hard, repeatedly-
      verified no-LLM rule) and a 9-category list. User chose: **fully
      deterministic keyword classifier** (extends
      `app/extraction/fact_extractor.py`'s existing `EVENT_KEYWORDS`,
      no new dependency), a **simpler 5-category set** (Major News,
      Research, Security/Policy, Business, Developer/Tools -- maps
      cleanly onto existing event categories + `source_type`), and
      **labels only for this pass** -- does NOT change which 25
      stories get selected (`app/tasks/ranking.py` untouched), matching
      how Verification/QA/content-similarity were all shipped as soft
      signals first. "Event clustering" and "why it matters"
      (`proposal.md`'s other two big ideas) are out of scope entirely
      -- clustering is already effectively solved by existing dedup
      (`canonical_story_id`), and "why it matters" was explicitly
      rejected by the user in an earlier session.
- [x] New `app/extraction/taxonomy.py`: `classify_category(title,
      events, source_type)`, a pure, priority-ordered cascade (HN
      community-post title convention -> content-event signals ->
      generic-HN fallback -> Major News default). New
      `Story.taxonomy_category` column (migration `e5a8c3f716d4`),
      populated in `run_fact_extraction_and_verification`
      (`app/tasks/verification.py`) reusing the events already
      extracted there -- no separate pass, no re-computation. Exposed
      via `_serialize_episode` and a new dashboard pill (5 new
      light/dark CSS color tokens, one hue per category, distinct from
      the existing pass/fail/skip semantic colors).
- [x] **Two real bugs found via live verification against real data
      (not shipped)**, both fixed:
      1. **HN "Show HN:"/"Launch HN:" posts misclassified as
         Research.** A real post ("Show HN: Swift-Qwen3.8-27B, ...")
         about a model speedup got tagged Research because its body
         text's benchmark numbers tripped `fact_extractor.py`'s
         `"research"` event keyword (`"benchmark"`) -- a coincidental
         body-text hit outranked the author's own deliberate title
         convention. Fixed by checking the HN title-prefix signal
         FIRST, before content-event signals, in the priority cascade
         (previously second).
      2. **`"fine-tuning"`/`"fine-tuned"` false-positived as a
         regulatory fine**, in `fact_extractor.py` itself (pre-existing,
         not new code) -- `\bfine\b` matches inside `"fine-tuning"`
         since regex treats the hyphen as a word boundary. A story
         about an AI email assistant's fine-tuning got mislabeled
         Security/Policy. Fixed by dropping bare `"fine"` from
         `lawsuit_regulatory`'s keyword set (kept `"fined"`, which
         has no such collision) -- same class of fix as this session's
         earlier bare-`"policy"` false positive.
      Both caught by resetting real, already-ingested stories back to
      `verification_status="pending"` and re-running classification
      against them (not synthetic data), inspecting the real category
      distribution and individual titles for plausibility -- not just
      confirming the code ran without erroring.
- [x] Added `tests/test_taxonomy.py` (9 tests, including a direct
      regression test for the Show-HN-vs-research-event priority
      conflict) and a new regression test in
      `tests/test_fact_extractor.py` for the fine-tuning false
      positive. Full `pytest` suite: 109 tests, all passing.
- [x] **Verified live end-to-end**: real distribution across the
      eligible pool (44 major_news, 37 developer_tools, 14
      security_policy, 4 business, 4 research) with individual titles
      spot-checked per category for plausibility; confirmed the
      dashboard pill renders correctly (distinct color per category)
      against a real episode's story list.
- [ ] Whether this ever becomes a real budget/diversity selection
      algorithm (guaranteeing category representation in the Top 25,
      not just labeling it) is an open, deliberately deferred decision
      -- revisit once real category-distribution data over many days
      shows whether one category actually crowds out others.

### This session — 2026-09-22, part 38 (human-navigable repeat labels: "epXsY")

- [x] User noticed a story (`#108`, "Microsoft AI CEO says AI threats
      are real...") had no taxonomy label in an old episode's view,
      and asked why -- root cause explained (not a bug): that story is
      itself now a content-similarity duplicate of story `#78`
      (`canonical_story_id = 78`, a borderline 0.36-similarity match
      already flagged as questionable when the content-similarity
      feature was built), so it's correctly excluded from taxonomy/
      verification reprocessing; it still shows in episode #9's
      dashboard view because that episode was selected before content-
      dedup existed this session, and episode selections are frozen
      snapshots, never retroactively updated.
- [x] That explanation surfaced a real usability gap: `repeat_reason`
      only ever showed a bare numeric `repeats_past_primary_story_id`,
      not where an editor could actually go look at it. Added a
      human-navigable label: `app/tasks/content_dedup.py`'s historical-
      repeat detection now looks up which episode/rank position
      (`EpisodeStory.episode_id`/`rank_position`) the matched story was
      primary in -- safe to treat as a single unambiguous slot per
      story thanks to the "never re-select" invariant
      (`app/tasks/ranking.py`) -- and appends it to `repeat_reason` as
      `(epXsY)`, e.g. `(ep13s1)` = episode #13, rank 1. Shows up
      automatically everywhere `repeat_reason` already renders (the
      dashboard's "possible repeat" pill tooltip and edit-panel row) --
      no new UI wiring needed, since it's baked into the existing text
      field at detection time (same "reason string is the audit trail"
      pattern as `dedup_reason`/`filter_reason`/`rank_reason`).
- [x] **Verified live against real data**: confirmed the two stories
      previously flagged as historical repeats (`#149`/`#155`) had
      since themselves been selected as primary in episode #13 --
      exactly why they no longer showed up as reprocessable candidates
      when first attempting to re-test this (a real, correct
      side-effect of the soft-signal design: a flagged-but-still-
      selectable story can absolutely go on to become a historical
      primary itself). Constructed a controlled synthetic duplicate
      (copied story #149's real `raw_content` verbatim into a
      throwaway test story) specifically to exercise the new label
      path, confirmed `repeat_reason` correctly read
      `"...repeats_past_primary_story_id=149 (ep13s1)"` -- matching
      story #149's real, independently-confirmed episode/rank exactly.
      Cleaned up the throwaway story afterward. Full `pytest` suite:
      109 tests, all passing (no new dedicated test added -- this
      logic lives inside `enrich_and_dedup_by_content` itself, same
      accepted not-unit-tested-directly pattern as the rest of that
      task; covered by this live verification instead).

### This session — 2026-09-22, part 39 (episode idempotency: one Episode per run_date)

- [x] User reported duplicate Episode records sharing the same
      `run_date` on the dashboard (e.g. four separate rows for
      `2026-09-21`). Investigated before changing anything: root cause
      was a **deliberate MVP-era decision**, documented verbatim in the
      original `add_episodes` migration -- `run_ranking_selection()`
      always inserted a new `Episode` with no existing-row check at
      all, by design, so heavy dev/test traffic (repeated manual
      `/episodes/select` calls, this session's own verification passes)
      produced 13 stray duplicate rows across 5 dates. Confirmed via
      direct DB inspection this was 100% real duplicate rows, not a
      dashboard rendering bug (`GET /episodes` has no filtering at
      all).
- [x] Added `uq_episodes_run_date` (migration `b3e7f0a1c9d5`) enforcing
      **at most one Episode per run_date** at the DB layer, plus
      explicit application-level idempotency in
      `app/tasks/ranking.py`: an existing draft is reused as-is
      (no new row, `EpisodeStory` untouched); existing
      rejected/approved/published are blocked outright, never silently
      resurrected/invalidated/overwritten. `POST /api/v1/episodes/select`
      (`app/main.py`) does a synchronous pre-check so a blocked/reused
      call never even queues a Celery task; the real, race-safe
      protection lives in the task itself
      (`_create_episode_or_recover`), which catches the
      `IntegrityError` a losing concurrent request's `INSERT` produces
      and returns the winner's episode instead of crashing.
- [x] New explicit `POST /api/v1/episodes/{episode_id}/reprocess`
      endpoint + `reprocess_episode` task -- the only sanctioned way to
      redo a draft/rejected episode's story selection (replaces its
      `EpisodeStory` snapshot, resets `video_status`/`qa_status` to
      `pending`, stamps `content_changed_at` reusing the existing
      reorder/swap staleness mechanism). Deliberately a separate,
      auditable operation, not a `force=true` flag -- approved/
      published episodes are always blocked, no override.
- [x] **Existing-data cleanup, performed live before adding the
      constraint** (a unique constraint can't be added while duplicates
      exist): verified every DELETE-candidate episode's publish/video
      status and checked for orphaned media first (`episode_19.mp4` +
      intro/outro *will* be orphaned on disk by deleting episode #19's
      row -- files deliberately left alone, out of scope; DB rows only).
      Also found and fixed a real FK issue mid-cleanup: 3
      `Notification` rows referenced 3 of the delete-candidate episodes
      -- nulled their `episode_id` (the column is nullable exactly for
      this) rather than deleting the audit records. Deleted exactly 13
      episode ids (never a broad `WHERE run_date = ...`) inside one
      transaction, verified zero duplicate `run_date`s and episode #9
      (published) untouched *before* committing. Canonical episodes
      remaining: `#5` (09-15), `#6` (09-16), `#9` (09-17, published),
      `#10` (09-18), `#11` (09-20), `#21` (09-21).
- [x] **Verified live end-to-end** against the real dev DB: repeated
      `/episodes/select` calls for an existing draft date return the
      same episode, no new row, no queued task; calling it for the
      published date (`2026-09-17`) returns 409 immediately; reprocess
      on the real draft episode #11 replaced its selection in place
      (18 → 30 `EpisodeStory` rows, same episode id, `video_status`/
      `qa_status` reset to `pending`) and reprocessing episode #9
      (published) is blocked the same way select is. Full `pytest`
      suite: 162 tests, all passing (29 new -- `tests/test_episode_idempotency.py`).

### This session — 2026-09-22, part 40 (real ingestion window on the DEV Collect panel; removed a wrong episode-page field)

- [x] Added `Collected: <min> → <max> (N hrs)` to the episode details
      page, computed from selected stories' `collected_at`. User caught
      this was wrong: it measured when the 2 *winning* stories for that
      episode happened to be inserted (which can span many separate
      ingestion runs, even days apart, since an episode's selection
      draws from the whole accumulated story pool) -- not the 22h
      `news_window_hours` ingestion filter the user actually wanted to
      see. Removed the field entirely (`computeCollectionWindow` and
      its usage in `renderStudioLayout`, `app/dashboard/app.js`).
- [x] Investigated before rebuilding: `news_window_hours` (22) is a
      transient filter -- `ingest_news()` (`app/tasks/ingestion.py`)
      computes `window_start = now - 22h` at its own execution time and
      discards it once used; `fetch_ai_stories()`
      (`app/sources/hackernews_api.py`) computed an independent
      `time.time()`-based window the same way. Neither value was ever
      returned or stored anywhere -- confirmed no existing mechanism
      (Celery `task_track_started`, `AsyncResult` usage) already
      exposed a task's real execution time.
- [x] Made both ingestion tasks capture and return their own window:
      `ingest_news()` now includes `window_start`/`window_end`/
      `configured_window_hours` in its result (already computed `now`
      internally, just wasn't exposing it). `fetch_ai_stories()`'s
      signature changed to take `now` explicitly instead of an internal
      `time.time()` call, so `ingest_hackernews_stories()` captures one
      shared reference instant and returns the same three fields --
      same filter math, no behavior change, no new dependency.
- [x] New minimal `GET /api/v1/tasks/{task_id}/result` (`app/main.py`)
      wrapping Celery's already-configured Redis result backend
      (`AsyncResult`) -- reads back a finished task's own result dict.
      Deliberately generic (works for any task id) but narrow in scope:
      no new persistent storage, no task-tracking config changes.
- [x] The DEV "Collect New Stories" panel now polls this endpoint (via
      its existing ~2s poll loop -- no second timer/loop introduced)
      and shows the real per-source window once each task finishes,
      e.g. `RSS: Sep 20, 7:12 PM → Sep 21, 5:12 PM (22h configured) ·
      inserted 4`, task id kept as small secondary text. Falls back to
      "queued" text if a task hasn't finished by the time the story-
      detection poll concludes.
- [x] **Verified live**: triggered real RSS + HN ingestion via curl,
      confirmed `GET /api/v1/tasks/{id}/result` returns real captured
      `window_start`/`window_end` (`2026-09-20T19:12:24Z` →
      `2026-09-21T17:12:24Z`, `configured_window_hours: 22`) matching
      the task's actual execution time, not the request time. Confirmed
      in-browser: the panel correctly rendered both sources' real
      windows + inserted counts (0, since this dev DB's story pool is
      already exhausted from this session's heavy testing) without
      claiming new stories were found. Confirmed the removed episode-
      page field no longer appears. Full `pytest` suite: 167 tests, all
      passing (5 new -- `tests/test_hackernews_api.py` +
      `tests/test_ingestion_endpoints.py` additions).

### This session — 2026-09-22, part 41 (calendar-day model + raw/editorial schema split -- clean break)

- [x] Replaced the rolling 22h ingestion window entirely with a strict
      calendar-day model: `target_date = today_ist() - 1 day`. New
      single authoritative date module `app/dates.py`
      (`today_ist`/`target_collection_date`/`coverage_window`/
      `episode_key`) -- every ingestion/processing/API date decision
      now imports from here, no `datetime.now()` used directly for a
      business-date decision anywhere else (verified via grep).
- [x] Split one Postgres database into two schemas: `raw` (pure
      collection, zero editorial judgment) and `editorial` (dedup/
      verification/taxonomy/ranking/episodes/production/QA/publishing).
      `app/models.py` rewritten from scratch: old single `Story` table
      replaced by `raw.NewsItem` (dual identity --
      `(source_name, canonical_url)` always unique, plus
      `(source_name, external_id)`) + `raw.CollectionRun` (one row per
      complete "Collect News" operation, RSS+HN counts together, per
      explicit user clarification) + `editorial.StoryState` (1:1
      shared-PK companion to NewsItem, created only by processing,
      never by collection) + `Episode.run_date` renamed
      `episode_date` (`episode_key` computed on demand, not stored).
      Clean-break Alembic migration (`a3f6c92e1d47`): old
      `public.stories/episodes/episode_stories/story_content/
      notifications` dropped outright (confirmed empty), new schemas
      created fresh -- deliberately one-way, no downgrade path.
- [x] Collection and processing are now two fully separate operations,
      per explicit user requirement: `ingest_news`/
      `ingest_hackernews_stories` (`app/tasks/ingestion*.py`) are plain
      functions writing ONLY `raw.news_items`, no `.delay()` auto-chain
      into anything editorial. New `app/tasks/collection.py`
      (`run_collection`, the only Celery task) runs both sources in
      one shared session and writes exactly one `CollectionRun` row.
      RSS keeps its existing single-live-fetch mechanism (historical
      backfill explicitly deferred, not solved here) -- only the
      filter boundary changed to calendar-day. Hacker News's
      `fetch_ai_stories_for_range` (built earlier this session) is now
      the only HN fetch path; the old rolling `fetch_ai_stories()` is
      deleted outright.
- [x] Converted the old `.delay()` auto-chain
      (dedup -> content_dedup -> verification) into direct sequential
      function calls, each keeping its own commit boundary/retry/
      idempotency behavior unchanged, per explicit user requirement.
      New `app/tasks/classify.py` (`classify_new_raw_items` -- the
      "create StoryState + compute ai_relevance" step, moved out of
      ingestion since raw collection must never compute anything
      editorial) and new orchestrator
      `app/tasks/scheduled.py::run_daily_processing` calling
      classify -> dedup -> content_dedup -> verification ->
      rank/select -> produce -> QA as one sequence, sharing one DB
      session (each stage still commits its own work independently).
      `app/tasks/ranking.py` rewritten to join `raw.NewsItem` +
      `editorial.StoryState` (scoped by `NewsItem.collection_date`,
      replacing the old `published_at` coverage-window filter
      entirely) and use `episode_date` throughout.
- [x] Updated every remaining consumer of the deleted `Story` model
      that the plan's own file list initially missed -- flagged, not
      silently expanded: `app/tasks/content.py`, `episode_video.py`,
      `episode_qa.py` (+ `app/qa/video_qa.py`), `publishing.py`, and
      `app/main.py`'s API layer (new `POST /api/v1/collection/run` +
      `GET /api/v1/raw/collection-runs` replacing the old two-endpoint
      ingestion trigger; `/episodes/select` is now the processing
      trigger). Deleted four now-obsolete one-time backfill scripts
      that imported the deleted model against already-wiped data
      (`app/scripts/backfill_*.py`). Dashboard's Collect panel
      rewritten around the single combined task's real result
      (no more per-source "22h configured" wording).
- [x] **Verified live**: real collection run for 2026-09-20 (11 RSS
      sources + Hacker News) -> 24 raw items, one `CollectionRun` row,
      zero `editorial.stories` rows immediately after (collection
      really writes nothing editorial). Triggered processing for the
      same date -> classify (24 seen, 17 ai_candidate) -> dedup ->
      content-dedup -> verification -> ranking created episode #1 (17
      primary, 0 backup -- real ceiling given source limits that day)
      -> video produced (0 failures) -> QA ran (failed only on the
      expected `story_count` 17/25 check). Confirmed via
      `GET /api/v1/episodes/1` that `episode_date`/`episode_key`,
      taxonomy labels, and verification status all serialize
      correctly end to end. Full `pytest` suite: 175 passing (3
      existing files fixed for the model rename, 2 new files added --
      `test_raw_ingestion.py`, `test_processing_flow.py`).

## Known issues / follow-ups

- [x] ~~Automated QA's `source_verification` check always reports
      `passed: None`/"not implemented"~~ -- wired up to
      `Story.verification_status`, see "part 27" above.
- [x] ~~Caption timing in `compose_video_task` is a naive proportional
      estimate~~ -- replaced with real per-sentence timing from
      edge-tts, see "part 28" above.
- [x] ~~Composed video duration doesn't exactly match the source audio
      duration~~ -- root-caused and fixed, see "part 19" above (was
      `-shortest` overrunning by 1-2s/clip, not simple rounding;
      accumulated to a ~34s desync across a full episode before the fix).
- [x] ~~Script/voice/visual/video pipeline is scoped to one story at a time~~
      -- resolved, see "part 7 (full-episode video production)" above.

- [x] ~~`worker` container runs Celery as root~~ -- fixed, see "part 23" above.
- [x] ~~No automated tests exist yet for ingestion/dedup/ranking logic~~
      -- a first test suite now covers the deterministic logic layer
      (ai-relevance, dedup, ranking) plus 3 targeted regression tests
      for this session's hardest-won bugs, see "part 24" above. Still
      genuinely open: task-orchestration-level and API-endpoint-level
      tests (noted as not-yet-covered in that same entry).
- [x] ~~Two RSS sources are disabled and need real fixes~~ -- Microsoft
      fixed (real replacement feed), VentureBeat re-confirmed still
      blocked and deliberately left disabled -- see "part 23" above.
- [x] ~~Potential race condition on concurrent ingestion~~ -- fixed, see
      "part 23" above. Was no longer just theoretical once RSS and HN
      ingestion started firing at the same Celery Beat scheduled times.

## Next up (near-term, per architecture but not yet built)

- [x] ~~Fact Extraction phase~~ -- built, see "part 25" above.
- [x] ~~Verification Engine~~ -- built as a soft signal (not a hard
      gate, per explicit user decision), see "part 25" above. Every
      AI-candidate story still reaches ranking regardless of
      verification status.
- [x] ~~Editorial Dashboard~~ -- built, see "part 10" onward above.
- [x] ~~Scheduled collection cycle~~ -- built via Celery Beat, see
      "part 22" above. Human approval remains manual, per the
      architecture.

### Requested 2026-09-22 (not yet scheduled)

- [ ] **Full article-content web scraping as the real content source.**
      Full-article fetching already exists
      (`app/content/article_extractor.py::fetch_full_article_text`),
      but today it's only used transiently inside content-dedup
      (`app/tasks/content_dedup.py`) for TF-IDF comparison -- the
      result isn't treated as the story's real content. Extend this
      so script generation (`app/tasks/content.py`/`episode_video.py`)
      can draw on the full scraped body instead of RSS's often-thin
      `raw_summary`. Needs a decision on whether `raw.news_items.
      raw_content` becomes the permanent full-text store (it's
      currently populated by content-dedup's fetch as a side effect)
      or a separate field/table, and how failures degrade (same
      fetch_error/non_html/empty_extraction statuses already exist).
- [ ] **More RSS feeds.** `app/sources/registry.py` currently has 11
      sources (one disabled -- VentureBeat, Vercel bot-challenge).
      Expand coverage to raise the daily raw-item ceiling (candidates:
      Anthropic blog, Meta AI blog, Simon Willison's blog, r/
      MachineLearning, etc.) -- directly helps the "will we run out of
      25+5 stories" concern from earlier this session, now that a
      real full Top-25+5 selection has been observed (2026-09-21,
      "part 41" above) once RSS+HN volume was high enough for one day.
- [ ] **More taxonomy labels.** Current 5-category deterministic
      taxonomy (`app/extraction/taxonomy.py`, "part 37" above) is
      intentionally coarse, labels-only. Revisit alongside (not
      before) the budget/diversity selection algorithm described in
      the design-principle note just below -- expanding labels with no
      consumer for them yet is premature.
- [ ] **Visual workflow chart in the dashboard.** A diagram/flowchart
      view of the full pipeline (Collect -> Classify -> Dedup ->
      Content-Dedup -> Verify -> Rank & Select -> Produce -> QA ->
      Approve -> Publish) showing each stage's current status for a
      given date/episode at a glance -- complements, doesn't replace,
      the per-stage buttons added this session ("part 41"'s Process
      Episode / individual stage buttons).
- [ ] **Surface task/worker logs in the dashboard.** No way today to
      see what a task actually did short of `docker compose logs
      worker` on the host. Consider a per-task-id or per-episode log
      viewer -- could reuse the existing Celery Redis result backend
      (`GET /api/v1/tasks/{task_id}/result` already exposes the return
      value, but not stdout/print output) or a dedicated log capture/
      tail endpoint.

## Future phases (per `project.md`)

Script/Voice/Visual/Video are now built in simplified/free form, at
full-episode scale, with episode branding -- see parts 5-8 above.
Noting here what's still genuinely missing from each, since the
original `project.md` description was broader than what's built:

- [~] Script Generation -- deterministic headline + summary only, no
      LLM/fact-extraction, no "why it matters" (deliberately removed
      per user direction), no explicit source citation in the spoken
      narration (source is shown on-screen in the visual card only).
- [x] Voice Generation -- one branded AI voice (edge-tts), as designed.
- [~] Visual/Asset Engine -- static branded title cards only. No avatar,
      no screenshots, no motion graphics, no background music.
- [~] Video Composition -- voice + visuals + captions + episode-level
      branding (intro/outro) all working. No avatar, no transitions.
- [x] Automated Video QA -- 8 checks (story count, AI-only, source
      verification, source links, captions, audio, video integrity,
      duration), see "part 9" and "part 27" above. Soft signal only,
      same as Verification Engine -- never blocks approval.
- [x] Final Human Approval workflow -- Approve/Reject buttons in the
      dashboard, see "part 10" onward above. Manual only, no
      auto-approval, per the architecture.
- [~] Publishing Worker -- YouTube built and verified live for dev
      (see "part 30" and the dev/prod credential-split session above:
      manual publish button, real API integration, a real
      `/publish` call successfully uploaded to the dev channel).
      Prod channel/credentials not set up yet. Instagram not started.
- [ ] Analytics Worker (views, retention, watch time, shares, likes/comments, followers)
- [x] Notification Worker -- detection + audit trail + real Slack
      delivery (see part 35/36 below), verified live end-to-end.
- [~] Taxonomy redesign -- deterministic 5-category classification
      built and labels shown in the dashboard (see "part 37" above);
      still just labels, no budget/diversity selection algorithm yet
      (see the design principle note right below).
- [ ] Optimization Engine (feed analytics back into ranking)
- [ ] AWS evolution (EventBridge scheduled jobs, RDS, S3)
- [ ] Kubernetes/EKS evolution

### Design principle for the future taxonomy/categorized-episode work

From `proposal.md` -- worth preserving on its own since it reframes
what "Top 25" even means, independent of whether/when an actual
budget/diversity selection algorithm gets built on top of the
category labels ("part 37" above) that exist today:

> The 25 is a **daily information budget**, not a claim that 25 major
> news events happened. Some days: 9 major news + 4 research + 3
> security + 7 developer + 2 public impact = 25. Other days: 15 major
> news + 4 research + 3 security + 3 developer = 25. Other days: 6
> major news + 3 research + 2 security + 8 developer + 6 tools = 25.
> All three are "perfect" -- none is a shortfall.

Why this matters for this project specifically: it's the design
answer to "will we ever run out of 30 stories/day?" (the question
that originally motivated expanding sources to Hacker News, RSS
additions, etc. -- see the RSS/HN expansion notes above). Treating
research/security/developer/tool content as legitimate, differently-
weighted budget categories rather than diluted "news" means a quiet
major-news day doesn't have to mean an under-filled or padded-with-
junk episode -- Hacker News alone reliably supplies 19-24 qualifying
items/day (verified this session) across exactly these categories.
Content-type classification now exists ("part 37" above); per-category
*ranking* (an actual budget algorithm, not just labels) does not --
recorded here now so the principle isn't lost before that work starts,
whenever it does.
