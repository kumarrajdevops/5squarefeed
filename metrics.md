# Episode render metrics

Snapshot taken 2026-10-01. Wall times come from `editorial.episodes`
(`video_started_at` to `video_produced_at`) and the worker logs. CPU and memory
were never recorded for any episode; those columns are estimates or blank.

## Per episode

| Ep | Stories | Wall time to Produce | Video length | Memory | CPU |
|---|---|---|---|---|---|
| 1 | 17 | not recorded | not recorded | not recorded (old renderer) | not measured |
| 2 | 25 | not recorded | not recorded | not recorded | not measured |
| 3 | 25 | not recorded | not recorded | not recorded | not measured |
| 4 | 10 | not recorded (finished 2026-09-28 07:23 UTC) | not recorded | not recorded | not measured |
| 5 | 25 | 36.4 min | not recorded | not recorded | not measured |
| 6 | 25 | 21.9 min (final successful run) | not recorded | an earlier attempt exceeded 7 GB and was OOM-killed on a 140 s story; after the fix the peak is about 1.1 GB | not measured |
| 7 | 25 | 33.1 min | 721.6 s | about 1.1 GB peak per story render (expected, not re-measured) | not measured |

`video_started_at` only exists for Episodes 5 to 7, so earlier episodes have no
wall time. The only logged memory figures are from the Episode 6 incident.

## Episode 7 stage breakdown (33.1 min)

| Stage | Time | Share |
|---|---|---|
| Storyboards (0 of 25 reused) | 17.3 min (about 42 s per story) | 52% |
| Per-story renders (ffmpeg + Pillow) | 10.2 min (about 25 s per story) | 31% |
| Audio mix (loudnorm, ducking, per-boundary pings) | 1.5 min | 4% |
| Video assembly | 0.9 min | 3% |
| Voice check, final mux, QA, overhead | about 3.1 min | 9% |

12 minutes of video in 33 minutes, about 2.75x real time.

## Why it takes this long

1. **Storyboards were the biggest cost, and mostly wasted.** Episode 7
   regenerated all 25 storyboards although only a few stories were edited. Cause:
   `_mark_story_video_ready` wrote a status row after each render, which bumped
   `StoryContent.updated_at` past the `storyboard.json` mtime. Fixed with
   `mark_storyboard_current`, so a re-Produce should skip about 17 minutes. The
   first Produce after the worker restart still regenerates once, because
   Episode 7 was rendered by the old code.
2. **Encoding is CPU-bound.** Each story is Pillow scene rendering plus an ffmpeg
   encode; cost scales with story length and count. TTS is network-bound.
3. **The episode is long.** 721 s against the 300 s target (a known limitation),
   so every stage handles about 2.4x the intended material.
4. **The audio mix is one serial pass** over the whole episode (loudnorm,
   ducking, one SFX input per story boundary).
5. **Memory risk depends on story length, not episode length.** Before the fix,
   ffmpeg buffered frames from a looped still image in proportion to the story's
   length: a 140 s story reached about 7.3 GB on the 7.6 GiB Docker VM, which has
   no per-container limit, so the whole VM died. The fix feeds one frame and holds
   it in the filter graph (`still(duration)`), giving about 1.1 GB and 122 s for
   that story. See CLAUDE.md rule 12.

## Not yet known

- Real CPU percentage and core count used.
- Peak memory per stage for any episode other than the Episode 6 incident.
- Why Episode 5 took 36 min versus 22 min for Episode 6 (guess: Episode 6 reused
  more storyboards; unchecked).

To measure: sample `docker stats` every few seconds during a re-Produce of
Episode 7 and add CPU and memory per stage here.
