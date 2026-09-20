---
name: verify-episode
description: Verify a produced episode's audio/video/caption sync, QA result, and idempotency against the live system. Use after any change touching the content pipeline, video composition, or concatenation (app/tasks/episode_video.py, app/content/video_composer.py, app/tasks/content.py) before claiming the change works.
---

# Verify Episode

Run this after any change that could affect script/voice/visual/video
generation or episode-level concatenation, before telling the user it
works. This is the exact checklist that caught real bugs in this
project (a 2s-per-clip AV overrun accumulating to ~34s across an
episode, a Produce status race, stale cached video playback) — don't
skip steps because the code "looks right."

Pick a real episode id that has primary stories with generated content
(ask the user, or query `GET /api/v1/episodes` for one with
`video_status: "ready"`).

## 1. Trigger and wait for Produce

```bash
curl -s -X POST http://localhost:8000/api/v1/episodes/{episode_id}/produce
```

Poll until `video_status` leaves `"producing"`:

```bash
until curl -s http://localhost:8000/api/v1/episodes/{episode_id} | grep -q '"video_status":"ready"\|"video_status":"failed"'; do sleep 2; done
```

Check the worker's log for the task's completion summary
(`docker logs 5squarefeed-worker-1 --tail 20`) — confirm
`stories_failed: 0` and `backups_failed: 0` (or investigate why not).

## 2. Check AV sync on the combined video

```bash
MSYS_NO_PATHCONV=1 docker exec 5squarefeed-api-1 ffprobe -v error \
  -show_entries stream=codec_type,duration -show_entries format=duration \
  -of json /app/media/videos/episode_{episode_id}.mp4
```

Compare the `video` stream's duration to the `audio` stream's duration.
**They should match within about one frame (~0.03-0.05s at 25fps).**
A gap of seconds means something is letting the video stream overrun
the audio — see CLAUDE.md rule #4 (the `-shortest` + `-t` issue) before
assuming it's a new problem.

## 3. Spot-check 2-3 individual story clips the same way

```bash
MSYS_NO_PATHCONV=1 docker exec 5squarefeed-api-1 ffprobe -v error \
  -show_entries stream=codec_type,duration -of csv=p=0 \
  /app/media/videos/{story_id}.mp4
```

Same tolerance as above. If individual clips are in sync but the
combined video isn't, the bug is in `concat_videos()` or gap-clip
insertion, not per-story composition — check the gap clip itself too:

```bash
MSYS_NO_PATHCONV=1 docker exec 5squarefeed-api-1 ffprobe -v error \
  -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \
  /app/media/videos/_story_gap_*.mp4
```

## 4. Run QA and check the report

```bash
curl -s -X POST http://localhost:8000/api/v1/episodes/{episode_id}/qa
sleep 3
curl -s http://localhost:8000/api/v1/episodes/{episode_id} | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('qa_status:', d['qa_status'])
for c in d['qa_report']:
    print(f\"  {c['check']}: passed={c['passed']} | {c['detail']}\")
"
```

Every check should `PASS` except `source_verification` (`passed: None`,
not implemented, expected) and `duration_target` (fails whenever the
episode exceeds the 300s/5min target — a known, already-documented
limitation, not something this skill is meant to catch or fix).
Anything else failing is a real regression.

## 5. Confirm idempotency

Re-run step 1 immediately. The worker log's completion summary should
show everything reused (`stories_reused` == the primary count,
`stories_produced: 0`, same for backups) unless you deliberately changed
something (an edit, a swap, a code change to composition) that should
force regeneration for specific stories — if so, confirm it's *only*
those stories that got reproduced, not everything.

## If something's off

Don't just re-run and hope — this project's history is full of "looks
fixed" turning out not to be. Isolate: test one story's `compose_video()`
call directly in a Python one-liner inside the container before
re-running a full (slow) episode regeneration, the same way the
`-t`-duration fix was verified in this project before rolling it out
episode-wide.
