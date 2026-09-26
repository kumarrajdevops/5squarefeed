"""
CLI entry point for the canonical enhanced Pillow/storyboard episode
renderer. This file contains NO rendering logic of its own -- it only
parses --episode-id and calls app.content.episode_renderer.render_episode,
the exact same function the production Celery task
(app.tasks.episode_video.produce_episode_video) calls. DEV and PROD are
guaranteed to never drift apart because there is only one implementation.

Usage:
    python build_episode.py --episode-id 2
    python build_episode.py --episode-id 3

Works regardless of the shell's starting directory -- episode_renderer
resolves all paths against the repo root, not the process cwd.
"""
import argparse
import json

from app.content.episode_renderer import render_episode

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-id", type=int, required=True, help="Episode.id to render, e.g. 3")
    args = parser.parse_args()

    result = render_episode(args.episode_id)
    print(json.dumps(result, indent=2))
