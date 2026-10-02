// Asserts app/dashboard/app.js's planStorySwap(), loaded from the real file
// the same way compute_start_offset.node.js loads computeStartOffset().
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "app", "dashboard", "app.js"), "utf8");
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
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const { planStorySwap } = sandbox;
assert.strictEqual(typeof planStorySwap, "function");

const primary = [10, 20, 30, 40];
const backup = [50, 60];

// Same group: the two exchange places, everything else stays put (not an insert-move).
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(planStorySwap(primary, backup, 10, 30))),
  { kind: "reorder", group: "primary", story_ids: [30, 20, 10, 40] },
);
// Argument order does not matter.
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(planStorySwap(primary, backup, 40, 20))),
  { kind: "reorder", group: "primary", story_ids: [10, 40, 30, 20] },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(planStorySwap(primary, backup, 60, 50))),
  { kind: "reorder", group: "backup", story_ids: [60, 50] },
);
// Primary + backup: the existing swap endpoint payload, whichever was ticked first.
for (const [a, b] of [[20, 60], [60, 20]]) {
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(planStorySwap(primary, backup, a, b))),
    { kind: "swap", primary_story_id: 20, backup_story_id: 60 },
  );
}
// Same id twice or an unknown id: nothing to do.
assert.strictEqual(planStorySwap(primary, backup, 10, 10), null);
assert.strictEqual(planStorySwap(primary, backup, 10, 999), null);
// The input lists are not mutated.
assert.deepStrictEqual(primary, [10, 20, 30, 40]);

console.log("ok");
