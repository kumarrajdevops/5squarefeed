const API = "/api/v1";

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

// escapeHtml() escapes &/</> (safe for text nodes) but not quotes, so
// its output isn't safe to embed inside an HTML attribute value (e.g.
// href="..."). Used wherever a URL is placed inside an attribute below.
function escapeAttr(str) {
  return escapeHtml(str).replace(/"/g, "&quot;");
}

// Shared between the story list rows and the edit panel: shows the
// actual source URL as clickable (opens in a new tab) AND selectable/
// copyable text, plus a one-click Copy button, so an editor can proofread
// against the source and also grab the link to share it elsewhere.
function renderSourceLinkHtml(url) {
  if (!url) return "";
  const safeUrl = escapeAttr(url);
  return `
    <div class="source-link-row">
      <a class="story-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer" title="Open source article in a new tab to proofread">${safeUrl}</a>
      <button class="link-copy-btn" type="button" data-url="${safeUrl}" title="Copy link">Copy</button>
    </div>
  `;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function renderEditMetaHtml(story) {
  const rows = [
    ["Source", story.source_name],
    ["Author", story.author],
    ["Published", formatDateTime(story.published_at)],
    ["Collected", formatDateTime(story.collected_at)],
  ];

  let html = rows.map(([label, value]) => `
    <div class="edit-meta-row">
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(value || "—")}</dd>
    </div>
  `).join("");

  // Only present when this story surfaced via an aggregator/discovery
  // channel distinct from its resolved publisher above (e.g. found via
  // Hacker News, actually published on some other site) -- see
  // app/main.py's _discovery_info(). Absent for directly-ingested RSS
  // stories, where there's no separate discovery channel to show.
  if (story.discovery) {
    const safeUrl = escapeAttr(story.discovery.url);
    html += `
      <div class="edit-meta-row">
        <dt>Discovered via</dt>
        <dd><a class="story-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer" title="Open the discovery thread">${escapeHtml(story.discovery.label)} &#8599;</a></dd>
      </div>
    `;
  }

  // Soft signal only -- see app/verification/engine.py. Always shown
  // (even "pending") so the editor knows whether this story has been
  // through the Verification Engine pass yet at all.
  html += `
    <div class="edit-meta-row">
      <dt>Verification</dt>
      <dd><span class="pill ${story.verification_status}">${escapeHtml(story.verification_status)}</span></dd>
    </div>
  `;
  if (story.verification_reason) {
    html += `
      <div class="edit-meta-row">
        <dt>Verification reason</dt>
        <dd>${escapeHtml(story.verification_reason)}</dd>
      </div>
    `;
  }

  // Soft signal only (see app/tasks/ranking.py's eligibility comment)
  // -- content-similarity flagged this story as a possible repeat of
  // an already-narrated past story, but it was still selectable. Only
  // shown when actually flagged.
  if (story.repeats_story_id) {
    html += `
      <div class="edit-meta-row">
        <dt>Possible repeat</dt>
        <dd>Story #${story.repeats_story_id} -- ${escapeHtml(story.repeat_reason || "")}</dd>
      </div>
    `;
  }

  const facts = story.extracted_facts;
  if (facts && (facts.companies.length || facts.products.length || facts.events.length || facts.dates.length || facts.claims.length)) {
    const parts = [];
    if (facts.companies.length) parts.push(`Companies: ${facts.companies.join(", ")}`);
    if (facts.products.length) parts.push(`Products: ${facts.products.join(", ")}`);
    if (facts.events.length) parts.push(`Events: ${facts.events.join(", ")}`);
    if (facts.dates.length) parts.push(`Dates: ${facts.dates.join(", ")}`);
    if (facts.claims.length) parts.push(`Claims: ${facts.claims.join(", ")}`);
    html += `
      <div class="edit-meta-row">
        <dt>Extracted facts</dt>
        <dd>${escapeHtml(parts.join(" · "))}</dd>
      </div>
    `;
  }

  return html;
}

function wireSourceLinkCopyButtons(container) {
  container.querySelectorAll(".link-copy-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const url = btn.dataset.url;
      try {
        await navigator.clipboard.writeText(url);
        const original = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = original; }, 1500);
      } catch (err) {
        alert("Couldn't copy automatically -- here's the link:\n" + url);
      }
    });
  });
}

async function apiGet(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

async function apiSend(method, path, body) {
  const res = await fetch(API + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

const apiPost = (path, body) => apiSend("POST", path, body);
const apiPatch = (path, body) => apiSend("PATCH", path, body);

const app = document.getElementById("app");
let currentEpisode = null;
let editingStoryId = null;
let appEnv = null;

function getEpisodeIdFromUrl() {
  const id = new URLSearchParams(location.search).get("episode");
  return id ? parseInt(id, 10) : null;
}

// Mirrors the backend's fail-closed allow-list (app/main.py's
// NON_PRODUCTION_APP_ENVS) -- the dashboard only ever hides/shows the
// button, the backend endpoint enforces this for real, so if the two
// ever disagree the worst case is a visible button that the backend
// still correctly rejects, never the other way around.
const DEV_APP_ENVS = new Set(["local", "dev"]);
function isDevEnvironment(env) {
  return typeof env === "string" && DEV_APP_ENVS.has(env.trim().toLowerCase());
}

async function init() {
  try {
    const health = await fetch("/health").then((res) => res.json());
    appEnv = health.app_env;
  } catch (err) {
    appEnv = null; // Fail closed on the dashboard too -- unknown env hides the button.
  }

  const episodeId = getEpisodeIdFromUrl();
  if (episodeId) {
    await renderEpisodeStudio(episodeId);
  } else {
    await renderEpisodeList();
  }
}

// ---------------------------------------------------------
// Collect New Stories (dev/local only) -- fires the single
// POST /api/v1/collection/run endpoint, which queues ONE
// app.tasks.collection.run_collection task covering RSS + Hacker
// News together (one raw.collection_runs row -- see that task's
// docstring). Collection is deliberately NOT chained into processing
// (classify/dedup/rank/etc, see app/tasks/scheduled.py) -- this panel
// only ever reports raw counts, never "new stories available for
// selection" (that only becomes true after a separate
// POST /api/v1/episodes/select run for the same date).
//
// The task's own result (read back via GET /api/v1/tasks/{id}/result,
// wrapping Celery's Redis result backend) is the authoritative source
// for what happened -- no before/after story-list comparison needed,
// since run_collection already reports real seen/inserted/updated
// counts per source directly.
// ---------------------------------------------------------

const MIN_LOADING_MS = 1000;     // UI pacing only -- not a real collection duration
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 30000;
// Processing includes real video production (TTS + ffmpeg per story,
// sequential) + QA, not just the classify/dedup/rank stages -- can
// genuinely take minutes for a full Top-25, unlike Collect's ~20s.
const PROCESS_POLL_TIMEOUT_MS = 10 * 60 * 1000;
const RESULT_COLLAPSE_MS = 10000; // how long the result summary stays up before collapsing

let collectCollapseTimer = null;

async function fetchTaskResult(taskId) {
  try {
    return await apiGet(`/tasks/${taskId}/result`);
  } catch (err) {
    return null;
  }
}

function formatSourceLine(label, source) {
  if (!source) return `${label}: no data`;
  if (source.error) return `${label}: failed -- ${escapeHtml(source.error)}`;
  const seen = source.items_seen !== undefined ? source.items_seen : source.seen;
  return `${label}: seen ${seen} · inserted ${source.inserted} · updated ${source.updated}`;
}

function wireCollectButton() {
  const btn = document.getElementById("collect-btn");
  const resultEl = document.getElementById("collect-result");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    clearTimeout(collectCollapseTimer);
    resultEl.innerHTML = "";

    const startTime = Date.now();

    const tick = () => {
      btn.innerHTML = `<span class="spinner" aria-hidden="true"></span> Collecting… ${formatElapsed(Date.now() - startTime)}`;
    };
    tick();
    const timerInterval = setInterval(tick, 1000);

    const stopLoading = () => {
      clearInterval(timerInterval);
      btn.disabled = false;
      btn.textContent = "Collect New Stories";
    };

    let queued;
    try {
      queued = await apiPost("/collection/run");
    } catch (err) {
      stopLoading();
      resultEl.innerHTML = `<strong>Collection failed to queue</strong><br>${escapeHtml(err.message)}`;
      return;
    }

    const elapsedSoFar = Date.now() - startTime;
    if (elapsedSoFar < MIN_LOADING_MS) {
      await new Promise((resolve) => setTimeout(resolve, MIN_LOADING_MS - elapsedSoFar));
    }

    const pollStart = Date.now();
    let finished = null;

    while (Date.now() - pollStart < POLL_TIMEOUT_MS) {
      const res = await fetchTaskResult(queued.task_id);
      if (res && res.status !== "pending") {
        finished = res;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }

    stopLoading();

    if (!finished) {
      resultEl.innerHTML =
        `Collection queued (task ${escapeHtml(queued.task_id)}) -- still running.<br>` +
        `Check back shortly, or GET /api/v1/raw/collection-runs.`;
      return;
    }

    if (finished.status === "failed") {
      resultEl.innerHTML = `<strong>Collection task failed</strong><br>${escapeHtml(finished.error || "")}`;
      return;
    }

    const result = finished.result || {};
    const rssLine = formatSourceLine("RSS", result.rss);
    const hnLine = formatSourceLine("Hacker News", result.hackernews);
    const statusLabel = result.status === "success" ? "✓ Collection complete" : `Collection finished (${escapeHtml(result.status || "unknown")})`;

    resultEl.innerHTML =
      `<strong>${statusLabel}</strong> -- target date ${escapeHtml(result.collection_date || "?")}<br>` +
      `${rssLine}<br>${hnLine}`;

    collectCollapseTimer = setTimeout(() => {
      resultEl.innerHTML = "";
    }, RESULT_COLLAPSE_MS);
  });
}

// ---------------------------------------------------------
// Process Episode (dev/local only) -- fires POST /api/v1/episodes/
// select, which is now the processing trigger (classify -> dedup ->
// content-dedup -> verification -> rank/select -> produce -> QA, see
// app/tasks/scheduled.py's run_daily_processing). Deliberately a
// separate button/click from Collect New Stories above -- collection
// and processing are two distinct operations and processing is never
// auto-run after collection (see app/tasks/collection.py's docstring).
// No date param is sent -- the server always computes
// target_collection_date() (today IST - 1 day) itself, the same date
// Collect just targeted.
// ---------------------------------------------------------

let processCollapseTimer = null;

function formatProcessSummary(result) {
  const c = result.classify || {};
  const d = result.dedup || {};
  const cd = result.content_dedup || {};
  const v = result.verification || {};
  const r = result.ranking || {};

  const lines = [
    `Classify: ${c.classified ?? "?"} classified, ${c.ai_candidates ?? "?"} AI candidates`,
    `Dedup: ${d.duplicates_found ?? "?"} duplicates found (of ${d.checked ?? "?"} checked)`,
    `Content-dedup: ${cd.content_duplicates_found ?? "?"} content duplicates, ${cd.historical_repeats_found ?? "?"} historical repeats`,
    `Verification: ${v.verified ?? "?"} verified / ${v.unverified ?? "?"} unverified`,
  ];

  if (r.created) {
    lines.push(`Ranking: created episode #${r.episode_id} -- ${r.primary_selected ?? "?"} primary, ${r.backup_selected ?? "?"} backup`);
  } else {
    lines.push(`Ranking: no new episode (${escapeHtml(r.reason || "unknown")}, episode #${r.episode_id ?? "?"})`);
  }

  if (result.produce_status) lines.push(`Produce: ${escapeHtml(result.produce_status)}`);
  if (result.qa_status) lines.push(`QA: ${escapeHtml(result.qa_status)}`);

  return lines.join("<br>");
}

function wireProcessButton() {
  const btn = document.getElementById("process-btn");
  const resultEl = document.getElementById("process-result");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    clearTimeout(processCollapseTimer);
    resultEl.innerHTML = "";

    const startTime = Date.now();
    const tick = () => {
      btn.innerHTML = `<span class="spinner" aria-hidden="true"></span> Processing… ${formatElapsed(Date.now() - startTime)}`;
    };
    tick();
    const timerInterval = setInterval(tick, 1000);

    const stopLoading = () => {
      clearInterval(timerInterval);
      btn.disabled = false;
      btn.textContent = "Process Episode";
    };

    let queued;
    try {
      queued = await apiPost("/episodes/select");
    } catch (err) {
      stopLoading();
      resultEl.innerHTML = `<strong>Processing not started</strong><br>${escapeHtml(err.message)}`;
      return;
    }

    // A synchronous 200 with no task_id means the endpoint's own
    // pre-check already decided (existing draft reused) without
    // queuing anything -- nothing to poll.
    if (!queued.task_id) {
      stopLoading();
      resultEl.innerHTML =
        `Episode #${queued.episode_id} already exists for ${escapeHtml(queued.episode_date)} ` +
        `(${escapeHtml(queued.reason || "existing")}) -- nothing queued.`;
      processCollapseTimer = setTimeout(() => renderEpisodeList(), RESULT_COLLAPSE_MS);
      return;
    }

    const pollStart = Date.now();
    let finished = null;

    while (Date.now() - pollStart < PROCESS_POLL_TIMEOUT_MS) {
      const res = await fetchTaskResult(queued.task_id);
      if (res && res.status !== "pending") {
        finished = res;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }

    stopLoading();

    if (!finished) {
      resultEl.innerHTML =
        `Processing queued (task ${escapeHtml(queued.task_id)}) -- still running.<br>` +
        `This can take a few minutes (video production + QA) -- check back or refresh.`;
      return;
    }

    if (finished.status === "failed") {
      resultEl.innerHTML = `<strong>Processing task failed</strong><br>${escapeHtml(finished.error || "")}`;
      return;
    }

    resultEl.innerHTML = `<strong>✓ Processing complete</strong><br>${formatProcessSummary(finished.result || {})}`;

    // Full re-render replaces this message with the refreshed episode
    // list (showing the new/updated row) -- delayed so the user has
    // time to actually read the summary above first, rather than
    // clearing it immediately.
    processCollapseTimer = setTimeout(() => {
      renderEpisodeList();
    }, RESULT_COLLAPSE_MS);
  });
}

// ---------------------------------------------------------
// Episode list
// ---------------------------------------------------------

async function renderEpisodeList() {
  app.innerHTML = '<p class="loading">Loading episodes…</p>';

  let episodes;
  try {
    episodes = await apiGet("/episodes");
  } catch (err) {
    app.innerHTML = `<p class="error">Failed to load episodes: ${escapeHtml(err.message)}</p>`;
    return;
  }

  const collectPanelHtml = isDevEnvironment(appEnv)
    ? `
      <div class="panel collect-panel">
        <div class="collect-row">
          <button class="btn" id="collect-btn" type="button">Collect New Stories</button>
          <button class="btn" id="process-btn" type="button">Process Episode</button>
        </div>
        <p id="collect-result" class="collect-result"></p>
        <p id="process-result" class="collect-result"></p>
      </div>
    `
    : "";

  const wireDevPanels = () => {
    if (!isDevEnvironment(appEnv)) return;
    wireCollectButton();
    wireProcessButton();
  };

  if (!episodes.length) {
    app.innerHTML = collectPanelHtml + '<p class="loading">No episodes yet. Run ranking/selection first (POST /api/v1/episodes/select).</p>';
    wireDevPanels();
    return;
  }

  const rows = episodes.map((ep) => `
    <tr class="row-link" data-id="${ep.episode_id}">
      <td>#${ep.episode_id}</td>
      <td>${ep.episode_date}</td>
      <td><span class="pill ${ep.status}">${ep.status}</span></td>
      <td><span class="pill ${ep.video_status}">${ep.video_status}</span></td>
      <td><span class="pill ${ep.qa_status}">${ep.qa_status}</span></td>
      <td><span class="pill ${ep.publish_status}">${ep.publish_status.replace("_", " ")}</span></td>
      <td>${ep.primary_count} / ${ep.backup_count}</td>
    </tr>
  `).join("");

  app.innerHTML = `
    <h1>Episodes</h1>
    ${collectPanelHtml}
    <table class="episode-list">
      <thead>
        <tr><th>ID</th><th>Episode date</th><th>Status</th><th>Video</th><th>QA</th><th>Publish</th><th>Primary / Backup</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  wireDevPanels();

  app.querySelectorAll("tr.row-link").forEach((tr) => {
    tr.addEventListener("click", () => {
      location.search = "?episode=" + tr.dataset.id;
    });
  });
}

// ---------------------------------------------------------
// Episode Studio
// ---------------------------------------------------------

async function renderEpisodeStudio(episodeId) {
  app.innerHTML = '<p class="loading">Loading episode…</p>';

  try {
    currentEpisode = await apiGet(`/episodes/${episodeId}`);
  } catch (err) {
    app.innerHTML = `<p class="error">Failed to load episode ${episodeId}: ${escapeHtml(err.message)}</p>`;
    return;
  }

  renderStudioLayout(currentEpisode);
}

function qaIsStale(ep) {
  if (!ep.video_produced_at) return false;
  if (!ep.qa_run_at) return true;
  if (new Date(ep.video_produced_at) > new Date(ep.qa_run_at)) return true;
  // A reorder, swap, or a contained story's script edit also
  // invalidates QA -- not just a re-Produce.
  if (ep.content_changed_at && new Date(ep.content_changed_at) > new Date(ep.qa_run_at)) return true;
  return false;
}

// Video files are regenerated in place at a fixed per-episode/per-story
// URL, and browsers can keep serving a stale cached clip under that same
// URL from their media/range-request cache even with strict Cache-Control
// headers on the response (see server-side RevalidateStaticFiles). A
// version query param tied to a timestamp that changes on every real
// regeneration guarantees a brand-new URL the browser has never cached.
function cacheBust(url, timestamp) {
  if (!url || !timestamp) return url;
  return `${url}?v=${encodeURIComponent(timestamp)}`;
}

function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderStudioLayout(ep) {
  const producing = ep.video_status === "producing";
  const publishing = ep.publish_status === "publishing";
  const published = ep.publish_status === "published";
  const qaStale = qaIsStale(ep);

  let publishLabel = "Publish to YouTube";
  if (publishing) publishLabel = "Publishing… 0:00";
  else if (published) publishLabel = "Published ✓";
  else if (ep.publish_status === "failed") publishLabel = "Retry Publish";

  app.innerHTML = `
    <div><a href="/dashboard/">&larr; All episodes</a></div>

    <div class="studio-header">
      <div>
        <h1>Episode #${ep.episode_id}</h1>
        <div class="meta">
          ${ep.episode_date}
          &nbsp;<span class="pill ${ep.status}">${ep.status}</span>
          <span class="pill ${ep.video_status}">${ep.video_status}</span>
          <span class="pill ${ep.qa_status}">qa: ${ep.qa_status}</span>
          <span class="pill ${ep.publish_status}">${ep.publish_status.replace("_", " ")}</span>
        </div>
        ${ep.youtube_url
          ? `<p class="meta">Published: <a href="${ep.youtube_url}" target="_blank" rel="noopener">${ep.youtube_url}</a></p>`
          : ""}
        ${ep.publish_status === "failed" && ep.publish_error
          ? `<p class="meta publish-error">Publish failed: ${escapeHtml(ep.publish_error)}</p>`
          : ""}
      </div>
      <div class="studio-actions">
        <button class="btn" id="btn-produce" type="button" ${producing ? "disabled" : ""}>${producing ? "Producing… 0:00" : "Produce"}</button>
        <button class="btn${qaStale ? " btn-warn" : ""}" id="btn-qa" type="button" ${producing ? "disabled" : ""}>${qaStale ? "Run QA ⚠ (stale)" : "Run QA"}</button>
        <button class="btn btn-pass" id="btn-approve" type="button">Approve</button>
        <button class="btn btn-fail" id="btn-reject" type="button">Reject</button>
        <button class="btn" id="btn-publish" type="button" ${(producing || publishing || published) ? "disabled" : ""}>${publishLabel}</button>
        <span class="pill youtube-env youtube-env-${ep.youtube_environment}" title="A Publish click uploads to the ${ep.youtube_environment.toUpperCase()} YouTube channel/credentials (app/config.py's YOUTUBE_ENVIRONMENT)">${ep.youtube_environment} channel</span>
      </div>
    </div>

    ${ep.video_url
      ? `<video class="player" id="player" controls src="${cacheBust(ep.video_url, ep.video_produced_at)}"></video>`
      : '<p class="loading">No video produced yet -- click Produce.</p>'}

    <div class="studio-grid">
      <div class="panel">
        <h2>Top 30 &mdash; click rank to jump player, click title to edit</h2>
        <div class="story-group-label">Primary (${ep.primary_count})</div>
        <ul class="story-list" id="primary-list" data-group="primary"></ul>
        <div class="story-group-label">Backup (${ep.backup_count})</div>
        <ul class="story-list" id="backup-list" data-group="backup"></ul>
      </div>
      <div class="panel">
        <h2>Automated Video QA</h2>
        ${renderQAPanelHtml(ep)}
      </div>
    </div>

    <div class="panel">
      <div class="json-audit-header">
        <h2>Episode JSON (audit)</h2>
        <div class="json-audit-actions">
          <button class="btn" id="json-copy-btn" type="button">Copy JSON</button>
          <button class="btn" id="json-download-btn" type="button">Download JSON</button>
        </div>
      </div>
      <pre class="json-audit-pre"><code>${escapeHtml(JSON.stringify(ep, null, 2))}</code></pre>
    </div>
  `;

  renderStoryList("primary-list", ep.primary, ep, true);
  renderStoryList("backup-list", ep.backup, ep, false);
  wireDragAndDrop(ep);
  wireJsonAudit(ep);
  wireHeaderButtons(ep);
}

// Raw episode JSON at the bottom of the Studio view, for audit --
// exactly what the API returned (same object the whole page rendered
// from), copyable and downloadable so an editor can attach it to a
// ticket or diff it against a later state.
function wireJsonAudit(ep) {
  const jsonText = JSON.stringify(ep, null, 2);

  const copyBtn = document.getElementById("json-copy-btn");
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(jsonText);
      const original = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = original; }, 1500);
    } catch (err) {
      alert("Couldn't copy automatically -- select the text in the box and copy manually.");
    }
  });

  document.getElementById("json-download-btn").addEventListener("click", () => {
    const blob = new Blob([jsonText], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `episode_${ep.episode_id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });
}

function renderQAPanelHtml(ep) {
  if (!ep.qa_report) {
    return '<p class="loading">No QA run yet -- click Run QA (after producing the video).</p>';
  }

  const rows = ep.qa_report.map((check) => {
    const state = check.passed === true ? "pass" : (check.passed === false ? "fail" : "skip");
    const label = state === "pass" ? "PASS" : (state === "fail" ? "FAIL" : "SKIP");
    return `
      <li class="qa-row">
        <span class="pill ${state}">${label}</span>
        <div>
          <p class="qa-name">${escapeHtml(check.check)}</p>
          <p class="qa-detail">${escapeHtml(check.detail)}</p>
        </div>
      </li>
    `;
  }).join("");

  return `<ol class="qa-list">${rows}</ol>`;
}

// Matches GAP_DURATION_SECONDS in app/tasks/episode_video.py -- a
// silent 2s clip is inserted between consecutive *produced* story
// segments in the combined video, so this offset calculation has to
// count those gaps too or "jump to here" drifts out of sync later
// into the episode, same class of bug as the AV-duration mismatch
// that motivated adding the gaps in the first place.
const STORY_GAP_SECONDS = 0.5;

function computeStartOffset(ep, targetStoryId) {
  let offset = ep.intro_duration_seconds || 0;
  let addedFirst = false;

  for (const s of ep.primary) {
    // A story with no audio_duration_seconds never reached
    // video_ready and was excluded from the concatenated video
    // entirely -- it occupies no time and gets no gap.
    const included = !!s.audio_duration_seconds;

    if (included && addedFirst) offset += STORY_GAP_SECONDS;
    if (s.story_id === targetStoryId) return offset;

    if (included) {
      offset += s.audio_duration_seconds;
      addedFirst = true;
    }
  }

  return null;
}

// Display labels for app/extraction/taxonomy.py's 5-category taxonomy
// -- labels only, doesn't affect ranking/selection (see
// app/tasks/ranking.py's eligibility comment).
const TAXONOMY_LABELS = {
  major_news: "Major News",
  research: "Research",
  security_policy: "Security/Policy",
  business: "Business",
  developer_tools: "Developer/Tools",
};

function renderStoryList(listId, stories, ep, jumpable) {
  const ul = document.getElementById(listId);

  ul.innerHTML = stories.map((s) => `
    <li class="story-row" draggable="true" data-story-id="${s.story_id}">
      <span class="story-rank"${jumpable ? ' title="Jump player to here"' : ""}>${s.rank_position}</span>
      <div class="story-title-block" title="Click to edit script">
        <div class="story-title">${escapeHtml(s.title)}</div>
        <div class="story-source">${escapeHtml(s.source_name)}</div>
        ${renderSourceLinkHtml(s.url)}
      </div>
      <div class="story-pills">
        ${s.taxonomy_category
          ? `<span class="pill taxonomy-${s.taxonomy_category}">${TAXONOMY_LABELS[s.taxonomy_category] || s.taxonomy_category}</span>`
          : ""}
        <span class="pill ${s.content_status || "pending"}">${s.content_status || "no content"}</span>
        <span class="pill ${s.verification_status}" title="${escapeAttr(s.verification_reason || "")}">${s.verification_status}</span>
        ${s.repeats_story_id
          ? `<span class="pill repeat-flagged" title="${escapeAttr(s.repeat_reason || "")}">possible repeat</span>`
          : ""}
      </div>
    </li>
  `).join("");

  wireSourceLinkCopyButtons(ul);

  ul.querySelectorAll("li.story-row").forEach((li) => {
    const storyId = parseInt(li.dataset.storyId, 10);
    const story = stories.find((s) => s.story_id === storyId);

    li.querySelector(".story-title-block").addEventListener("click", () => openEditPanel(story));

    // The source link opens in its own tab natively (target="_blank") --
    // stop the click from bubbling to the title-block's edit-panel
    // handler above, which would otherwise also open the edit modal.
    li.querySelector(".story-link").addEventListener("click", (e) => e.stopPropagation());

    if (jumpable) {
      li.querySelector(".story-rank").style.cursor = "pointer";
      li.querySelector(".story-rank").addEventListener("click", () => {
        const player = document.getElementById("player");
        const offset = computeStartOffset(ep, storyId);
        if (player && offset != null) {
          player.currentTime = offset;
          player.play();
        }
      });
    }
  });
}

// ---------------------------------------------------------
// Drag-and-drop: same-group drag = reorder, cross-group = swap
// (native HTML5 drag events, no library)
// ---------------------------------------------------------

function wireDragAndDrop(ep) {
  const lists = [document.getElementById("primary-list"), document.getElementById("backup-list")];
  let dragged = null;

  lists.forEach((ul) => {
    ul.querySelectorAll("li.story-row").forEach((li) => {
      li.addEventListener("dragstart", () => {
        dragged = { storyId: parseInt(li.dataset.storyId, 10), group: ul.dataset.group, el: li };
        li.classList.add("dragging");
      });

      li.addEventListener("dragend", () => li.classList.remove("dragging"));

      li.addEventListener("dragover", (e) => {
        e.preventDefault();
        li.classList.add("drag-over");
      });

      li.addEventListener("dragleave", () => li.classList.remove("drag-over"));

      li.addEventListener("drop", async (e) => {
        e.preventDefault();
        li.classList.remove("drag-over");
        if (!dragged) return;

        const targetGroup = ul.dataset.group;
        const targetStoryId = parseInt(li.dataset.storyId, 10);

        try {
          if (dragged.group === targetGroup) {
            if (dragged.storyId === targetStoryId) { dragged = null; return; }

            const rect = li.getBoundingClientRect();
            const after = (e.clientY - rect.top) > rect.height / 2;
            ul.insertBefore(dragged.el, after ? li.nextSibling : li);

            const newOrder = Array.from(ul.querySelectorAll("li.story-row"))
              .map((x) => parseInt(x.dataset.storyId, 10));
            await apiPost(`/episodes/${ep.episode_id}/reorder`, { story_ids: newOrder });
          } else {
            const primaryId = targetGroup === "primary" ? targetStoryId : dragged.storyId;
            const backupId = targetGroup === "backup" ? targetStoryId : dragged.storyId;
            await apiPost(`/episodes/${ep.episode_id}/swap`, {
              primary_story_id: primaryId,
              backup_story_id: backupId,
            });
          }
        } catch (err) {
          alert("Reorder/swap failed: " + err.message);
        }

        dragged = null;
        await renderEpisodeStudio(ep.episode_id);
      });
    });
  });
}

// ---------------------------------------------------------
// Header actions
// ---------------------------------------------------------

// Produce takes 1-4 minutes (25 stories + 5 backups); polling GET
// /episodes/{id} for video_status leaving "producing" is much more
// useful than the old "refresh the page yourself" alert.
function startProducePolling(ep, startTime) {
  const produceBtn = document.getElementById("btn-produce");
  const qaBtn = document.getElementById("btn-qa");
  if (!produceBtn) return;

  produceBtn.disabled = true;
  if (qaBtn) qaBtn.disabled = true;

  const tick = () => { produceBtn.textContent = `Producing… ${formatElapsed(Date.now() - startTime)}`; };
  tick();
  const timerInterval = setInterval(tick, 1000);

  const pollInterval = setInterval(async () => {
    try {
      const fresh = await apiGet(`/episodes/${ep.episode_id}`);
      if (fresh.video_status !== "producing") {
        clearInterval(timerInterval);
        clearInterval(pollInterval);
        await renderEpisodeStudio(ep.episode_id);
      }
    } catch (err) {
      // Transient fetch error -- keep polling, next tick will retry.
    }
  }, 3000);
}

// QA is a fast file/ffprobe check (not a regeneration), so a short
// poll interval and a safety timeout are enough -- no risk of it
// running anywhere near as long as Produce.
function startQaPolling(ep, requestStartIso) {
  const qaBtn = document.getElementById("btn-qa");
  const produceBtn = document.getElementById("btn-produce");
  if (!qaBtn) return;

  const startTime = Date.now();
  const SAFETY_TIMEOUT_MS = 30000;

  qaBtn.disabled = true;
  if (produceBtn) produceBtn.disabled = true;
  qaBtn.classList.remove("btn-warn");

  const tick = () => { qaBtn.textContent = `Running QA… ${formatElapsed(Date.now() - startTime)}`; };
  tick();
  const timerInterval = setInterval(tick, 1000);

  const pollInterval = setInterval(async () => {
    const elapsed = Date.now() - startTime;
    try {
      const fresh = await apiGet(`/episodes/${ep.episode_id}`);
      const finished = fresh.qa_run_at && new Date(fresh.qa_run_at) >= new Date(requestStartIso);
      if (finished || elapsed > SAFETY_TIMEOUT_MS) {
        clearInterval(timerInterval);
        clearInterval(pollInterval);
        await renderEpisodeStudio(ep.episode_id);
      }
    } catch (err) {
      // Transient fetch error -- keep polling, next tick will retry.
    }
  }, 1500);
}

// Publish uploads the real combined episode video, so it can take a
// while (resumable upload, real network transfer) -- same live-timer
// polling pattern as Produce, no safety timeout (an upload legitimately
// can run several minutes for a 5-8 minute video on a slow connection).
function startPublishPolling(ep, startTime) {
  const publishBtn = document.getElementById("btn-publish");
  if (!publishBtn) return;

  publishBtn.disabled = true;

  const tick = () => { publishBtn.textContent = `Publishing… ${formatElapsed(Date.now() - startTime)}`; };
  tick();
  const timerInterval = setInterval(tick, 1000);

  const pollInterval = setInterval(async () => {
    try {
      const fresh = await apiGet(`/episodes/${ep.episode_id}`);
      if (fresh.publish_status !== "publishing") {
        clearInterval(timerInterval);
        clearInterval(pollInterval);
        await renderEpisodeStudio(ep.episode_id);
      }
    } catch (err) {
      // Transient fetch error -- keep polling, next tick will retry.
    }
  }, 3000);
}

function wireHeaderButtons(ep) {
  document.getElementById("btn-produce").addEventListener("click", async () => {
    const startTime = Date.now();
    try {
      await apiPost(`/episodes/${ep.episode_id}/produce`);
      startProducePolling(ep, startTime);
    } catch (err) {
      alert("Failed to queue production: " + err.message);
    }
  });

  document.getElementById("btn-qa").addEventListener("click", async () => {
    const requestStartIso = new Date().toISOString();
    try {
      await apiPost(`/episodes/${ep.episode_id}/qa`);
      startQaPolling(ep, requestStartIso);
    } catch (err) {
      alert("Failed to queue QA: " + err.message);
    }
  });

  if (ep.video_status === "producing") {
    // Page was loaded/reloaded mid-production -- resume the spinner.
    // The elapsed timer starts counting from now (no true start time
    // is persisted), so it's an approximation, not exact wall time.
    startProducePolling(ep, Date.now());
  }

  if (ep.publish_status === "publishing") {
    startPublishPolling(ep, Date.now());
  }

  document.getElementById("btn-publish").addEventListener("click", async () => {
    const startTime = Date.now();
    try {
      await apiPost(`/episodes/${ep.episode_id}/publish`);
      startPublishPolling(ep, startTime);
    } catch (err) {
      alert("Failed to queue publish: " + err.message);
    }
  });

  document.getElementById("btn-approve").addEventListener("click", async () => {
    try {
      await apiPost(`/episodes/${ep.episode_id}/approve`);
      await renderEpisodeStudio(ep.episode_id);
    } catch (err) {
      alert("Approve failed: " + err.message);
    }
  });

  document.getElementById("btn-reject").addEventListener("click", async () => {
    try {
      await apiPost(`/episodes/${ep.episode_id}/reject`);
      await renderEpisodeStudio(ep.episode_id);
    } catch (err) {
      alert("Reject failed: " + err.message);
    }
  });
}

// ---------------------------------------------------------
// Edit panel
// ---------------------------------------------------------

function openEditPanel(story) {
  editingStoryId = story.story_id;

  document.getElementById("edit-title").textContent = `Edit story #${story.story_id}`;

  const sourceEl = document.getElementById("edit-source");
  sourceEl.innerHTML = renderSourceLinkHtml(story.url);
  wireSourceLinkCopyButtons(sourceEl);

  document.getElementById("edit-meta").innerHTML = renderEditMetaHtml(story);

  document.getElementById("edit-headline").value = story.headline || story.title || "";
  document.getElementById("edit-summary").value = story.summary || "";
  document.getElementById("edit-script").value = story.script_text || "";
  document.getElementById("edit-status").textContent = story.content_status
    ? `content status: ${story.content_status}`
    : "no content generated yet";

  const preview = document.getElementById("edit-preview");
  preview.innerHTML = story.video_url
    ? `<video controls src="${cacheBust(story.video_url, story.content_updated_at)}"></video>`
    : "";

  document.getElementById("edit-overlay").hidden = false;
}

function closeEditPanel() {
  document.getElementById("edit-overlay").hidden = true;
  editingStoryId = null;
}

document.getElementById("edit-close").addEventListener("click", closeEditPanel);

document.getElementById("edit-overlay").addEventListener("click", (e) => {
  if (e.target.id === "edit-overlay") closeEditPanel();
});

document.getElementById("edit-save").addEventListener("click", async () => {
  const body = {
    headline: document.getElementById("edit-headline").value,
    summary: document.getElementById("edit-summary").value,
    script_text: document.getElementById("edit-script").value,
  };

  try {
    await apiPatch(`/stories/${editingStoryId}/content`, body);
    closeEditPanel();
    await renderEpisodeStudio(currentEpisode.episode_id);
  } catch (err) {
    alert("Save failed: " + err.message);
  }
});

init();
