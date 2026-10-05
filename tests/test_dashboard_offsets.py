"""
Runs tests/dashboard/compute_start_offset.node.js so `pytest tests/`
exercises the dashboard's computeStartOffset() alongside everything
else, without introducing a separate JS test runner/CI step. See that
file for what's actually asserted (real, independently-verified
per-story offsets against the current canonical-renderer timing
constants: 2.0s intro+gap lead-in, 0.6s story-to-story gap).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "dashboard" / "compute_start_offset.node.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")
def test_compute_start_offset_matches_canonical_renderer_timing():
    result = subprocess.run(
        ["node", str(NODE_TEST)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"compute_start_offset.node.js failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


SWAP_NODE_TEST = Path(__file__).parent / "dashboard" / "plan_story_swap.node.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")
def test_plan_story_swap_exchanges_the_two_ticked_stories():
    result = subprocess.run(
        ["node", str(SWAP_NODE_TEST)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"plan_story_swap.node.js failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
