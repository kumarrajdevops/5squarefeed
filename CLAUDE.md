# CLAUDE.md

Guardrails and conventions for working in this repo, distilled from real
bugs found during development (mostly via live user testing, not
automated checks catching them first). Read this before making pipeline
or dashboard changes. Detailed session-by-session history is in
`TODO.md`; architecture baseline is in `project.md`.

## Project identity

- **"5squareFeed"** is the production/public brand (domain
  `5squarefeed.in`, Gmail `5squarefeed@gmail.com`, GitHub repo
  `5squarefeed`) -- replaces the older "AI News Platform"/"AI Daily 25"
  names used before this brand existed. Use "5squareFeed" (exact
  capitalization) in any user-facing text: dashboard, video branding
  cards, YouTube titles/descriptions, README.
- `5min-ai-news` remains the internal dev/repo-level identifier for
  anything infra-plumbing-only (Docker Compose project name, container
  prefixes, local folder name) -- deliberately kept separate from the
  stylized brand name above so a technical identifier never needs exact
  capitalization/spacing to match. Don't casually rename one to match
  the other -- see TODO.md for the one deliberate, careful exception
  (a planned Compose-project-name migration with a full data backup/
  restore either side of it).
- Stack: FastAPI + PostgreSQL (Alembic) + Redis + Celery, via Docker
  Compose. Dashboard at `http://localhost:8000/dashboard/`.

## Before touching the content/video pipeline

Read `TODO.md`'s most recent dated entries first — a lot of what looks
like a bug has already been found, fixed, and verified with real numbers.
Don't re-diagnose from scratch.

## Hard rules (each one is a real bug this project already shipped and fixed)

1. **Never unconditionally regenerate a story's script.** A human edit
   via the dashboard's edit panel is expected to survive a later
   Produce. Any code that calls `generate_script()` (or reruns the
   script stage) must first check whether `content.script_text` already
   exists and skip regeneration if so. (`_produce_story_content` in
   `app/tasks/episode_video.py`, `generate_script_task` in
   `app/tasks/content.py` — both guarded this way; keep new script-stage
   code guarded the same way.)

2. **Any media file regenerated at a fixed URL must be cache-busted.**
   Episode/story videos are overwritten in place at the same URL every
   Produce call. Browsers can keep serving a stale cached copy via their
   media/range-request cache even with correct `Cache-Control` headers —
   this is a separate caching pipeline from normal HTTP caching. Always
   append `?v=<a timestamp that changes on real regeneration>` to a
   `<video>`/`<audio>` `src` that points at a fixed-path media file
   (see `cacheBust()` in `app/dashboard/app.js`, driven by
   `Episode.video_produced_at` / `StoryContent.updated_at`).

3. **A background task's status flip must happen synchronously in the
   API endpoint that queues it, not inside the task.** If a poller can
   ever check status before the task has actually started, it can't
   distinguish "not started yet" from "already finished" — both look
   like "not producing". Set the transitional status (e.g.
   `video_status = "producing"`) in the endpoint, before `.delay()`
   is even called (see `trigger_episode_production` in `app/main.py`).

4. **When using ffmpeg's `-shortest` to cap a video's length to an audio
   track, also pass an explicit `-t <duration>`.** `-shortest` alone
   overran by ~2s per clip here (looped image + subtitles filter +
   GOP/keyframe flush behavior, not simple rounding) — harmless on one
   clip, but it accumulates additively once many clips are concatenated
   into one episode video, producing a large audio/video/caption desync
   by the end. `compose_video()` in `app/content/video_composer.py`
   takes an explicit `duration_seconds` for exactly this reason — pass
   it whenever the audio duration is known at the call site (it always
   is, via `get_audio_duration_seconds()`).

5. **If you add a pacing element to the concatenated video (gaps,
   transitions, intro/outro), update `computeStartOffset()` in
   `app.js` too.** It sums preceding stories' durations to compute
   where each story starts in the combined video for "click rank to
   jump player" — anything that changes the actual timeline (a gap
   clip, a resequencing) has to be reflected there or that feature
   silently drifts out of sync, which is the same class of bug as #4
   just relocated to a different feature.

6. **Drag-and-drop in the dashboard needs real `DragEvent`s to test.**
   Synthetic mouse drags (`left_click_drag` in browser automation) do
   NOT trigger native HTML5 drag-and-drop — dispatch actual
   `DragEvent`/`DataTransfer` objects via `javascript_tool` instead.

7. **Verify against the live system before declaring something fixed.**
   Query the database directly, curl the real endpoint, `ffprobe` the
   real file. Code that looks obviously correct has repeatedly turned
   out to be wrong in this project once checked against live state —
   several of the bugs above were only caught because the user tested
   the actual output, not because a code review caught them first.

8. **Never run more than one `beat` replica.** `celery beat`
   (`docker-compose.yml`'s `beat` service, schedule in
   `app/worker/celery_app.py`) only decides *when* a scheduled task
   should run and enqueues it once — a second instance would
   double-enqueue every scheduled job (double ingestion, double
   nightly-cutoff episodes). `worker` can safely scale to multiple
   replicas; `beat` cannot. `beat` is deliberately gated behind the
   `scheduler` Compose profile — real scheduling is a production
   concern, not something that should fire unprompted during dev
   testing. Plain `docker compose up -d` does not start it; use
   `docker compose up -d beat` (or `--profile scheduler`) only when you
   actually want the nightly jobs running.

9. **Don't silently expand scope.** If an audit or investigation
   surfaces a second, related issue, report it and ask (or fix it only
   if clearly low-risk and directly in the spirit of what was asked) —
   don't just fix everything you notice in the same pass without saying so.

## Dev environment gotchas

- The `worker` container does **not** hot-reload — Celery loads task
  modules once at startup. After editing any `app/tasks/*.py` or
  `app/content/*.py` file, run `docker compose restart worker` before
  re-triggering a task, or the old code silently keeps running. The
  `api` container's `uvicorn --reload` picks up changes automatically —
  no restart needed there.
- Postgres credentials are `ai_news`/`ai_news` (db `ai_news`), not the
  `postgres` default: `docker exec 5min-ai-news-postgres-1 psql -U
  ai_news -d ai_news`.
- A brand-new `app/tasks/*.py` module must be added to `include=[...]`
  in `app/worker/celery_app.py`, or `.delay()` silently queues a task
  the worker never picks up (confirmed live when the Publishing
  Worker's task didn't appear in the worker's `[tasks]` startup log
  until this was fixed). Editing an *existing* task file doesn't need
  this -- only a new module.
- `requirements-dev.txt` (pytest) is not installed in the built image
  by design (keeps prod lean) -- `docker compose build` alone wipes any
  ad hoc `pip install` done in a running container. After a rebuild,
  re-run `pip install -r requirements-dev.txt` inside the `api`
  container before `pytest` works again.
- On Windows/Git Bash, prefix `docker exec ... ffprobe /app/media/...`
  style commands with `MSYS_NO_PATHCONV=1` or the leading `/` gets
  mangled into a Windows path.

## Verification checklist after touching the produce/QA pipeline

Use the `verify-episode` skill, or manually:
1. Trigger `/produce` on a real episode, wait for `video_status` to
   leave `"producing"`.
2. `ffprobe` the combined episode video AND at least 2-3 individual
   story clips — compare video-stream duration to audio-stream
   duration on each. They should match within about one frame
   (~0.04s at 25fps), not seconds.
3. Run `/qa` and check the report — `duration_target` failing against
   the 300s/5min target is a known, already-documented limitation, not
   a new bug; every other check should pass.
4. Re-run `/produce` once more and confirm it's idempotent (reused
   counts match, no new file mtimes) unless you deliberately changed
   something that should force regeneration.
