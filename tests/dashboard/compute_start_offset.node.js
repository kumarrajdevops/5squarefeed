// Standalone Node assertion script for app/dashboard/app.js's
// computeStartOffset() -- loaded via tests/test_dashboard_offsets.py's
// pytest wrapper so `pytest tests/` exercises it alongside everything
// else, without introducing a separate JS test runner.
//
// app.js is a plain browser script (top-level DOM-touching code, no
// module exports), so it's loaded here with `vm` against a minimal
// mocked `document`/`window` -- just enough for the script's top-level
// statements and its later async init() to not throw before
// computeStartOffset (a hoisted top-level `function` declaration) is
// already available on the sandbox. Nothing here re-implements the
// function; it calls the real one straight out of the real file.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const APP_JS = path.join(__dirname, "..", "..", "app", "dashboard", "app.js");
const src = fs.readFileSync(APP_JS, "utf8");

function noop() {}
const sandbox = {
  console,
  document: {
    addEventListener: noop,
    getElementById: () => ({ addEventListener: noop, textContent: "" }),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ textContent: "" }),
  },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
  setInterval: () => 0,
  clearInterval: noop,
  setTimeout: () => 0,
  localStorage: { getItem: () => null, setItem: noop },
  URLSearchParams,
  location: { search: "" },
};
sandbox.window = sandbox;

const vm = require("vm");
vm.createContext(sandbox);
vm.runInContext(src, sandbox); // top-level sync execution only; async init() errors happen later and don't affect this

const { computeStartOffset } = sandbox;
assert.strictEqual(typeof computeStartOffset, "function", "computeStartOffset must be defined by app.js");

// --- Fixture mirroring the real GET /episodes/{id} shape, using the
// EXACT real per-story durations from Episode 3 (122, 114, 123, 111),
// already independently verified this session via audio
// cross-correlation against the real produced file. ---
const ep = {
  primary: [
    { story_id: 122, audio_duration_seconds: 10.896 },
    { story_id: 114, audio_duration_seconds: 24.552 },
    { story_id: 123, audio_duration_seconds: 13.848 },
    { story_id: 111, audio_duration_seconds: 24.792 },
    { story_id: 999, audio_duration_seconds: null }, // never reached video_ready -- excluded, no gap either side
    { story_id: 121, audio_duration_seconds: 5.0 },
  ],
};

function assertClose(actual, expected, message) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${message}: got ${actual}, expected ${expected}`);
}

// Real, independently-verified checkpoints (episode_renderer.py:
// INTRO_DUR=1.8 + GAP_DUR=0.2 lead-in, STORY_GAP=0.6 between stories).
// Approximate equality throughout -- these are chained floating-point
// sums, not exact decimals.
assertClose(computeStartOffset(ep, 122), 2, "first story starts right after the fixed 2.0s intro+gap");
assertClose(computeStartOffset(ep, 114), 13.496, "second story: 2.0 + 10.896 + 0.6 gap");
assertClose(computeStartOffset(ep, 123), 38.648, "third story: 13.496 + 24.552 + 0.6 gap");
assertClose(computeStartOffset(ep, 111), 53.096, "fourth story: 38.648 + 13.848 + 0.6 gap");

// A story with no audio_duration_seconds (never reached video_ready)
// must not occupy time or add a gap on either side of it.
const afterExcluded = computeStartOffset(ep, 121);
const expectedAfterExcluded = 53.096 + 24.792 + 0.6; // 111's own duration + one gap, story 999 contributes nothing
assert.ok(
  Math.abs(afterExcluded - expectedAfterExcluded) < 1e-9,
  `story after an excluded one must skip its duration/gap entirely: got ${afterExcluded}, expected ${expectedAfterExcluded}`
);

// Unknown story_id -> null, not a thrown error or a wrong offset.
assert.strictEqual(computeStartOffset(ep, 424242), null, "unknown story_id must return null");

console.log("compute_start_offset.node.js: all assertions passed");
process.exit(0);
