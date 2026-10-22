/* TurboTube UI controller — no dependencies, no build step. */
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const KEY_STORE = "tt_api_key";
const PREFS_STORE = "tt_prefs_v1";
const MAX_ENTRIES = 200;

const state = {
  jobs: new Map(),
  probe: null,
  probeUrl: "",
  selected: new Set(),
  entryFilter: "",
  showAll: false,
  quality: "best",
  system: null,
  apiKey: localStorage.getItem(KEY_STORE) || "",
  es: null,
  reconnect: 0,
  pollTimer: null,
  syncPromise: null,
  authPrompted: false,
};

const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  downloading: "Downloading",
  processing: "Processing",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  partial: "Partial",
  interrupted: "Interrupted",
};

const PREFS = [
  "audioOnly",
  "audioFormat",
  "container",
  "mode",
  "playlistItems",
  "reverse",
  "archive",
  "subtitles",
  "autoSubs",
  "subLangs",
  "sponsorblock",
  "embedSubs",
  "embedThumb",
  "embedMeta",
  "chapters",
  "useAria2",
  "fragments",
  "rateLimit",
  "proxy",
  "section",
  "browserCookies",
  "cookieFile",
];

/* ------------------------------------------------------------------ utils */
const esc = (value = "") =>
  String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

function bytes(value) {
  const number = Number(value) || 0;
  if (number <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = number;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "";
  let total = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(total / 3600);
  total %= 3600;
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
               : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function baseName(path) {
  return String(path || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

function icon(name, className = "") {
  return `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function toast(message, type = "ok") {
  const container = $("#toasts");
  if (!container) return;
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  const iconName = type === "ok" ? "check" : type === "warn" ? "alert" : "alert";
  node.innerHTML = `${icon(iconName)}<div>${esc(message)}</div>`;
  container.appendChild(node);
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 220);
  }, 4200);
}

function setLoading(button, loading, label) {
  if (!button) return;
  button.disabled = loading;
  button.classList.toggle("loading", loading);
  const span = button.querySelector("span");
  if (span && label) {
    if (!button.dataset.label) button.dataset.label = span.textContent;
    span.textContent = loading ? label : button.dataset.label;
  }
}

/* -------------------------------------------------------------------- api */
async function api(path, options = {}, retried = false) {
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("x-api-key", state.apiKey);
  const isForm = options.body instanceof FormData;
  if (options.body && !isForm) headers.set("content-type", "application/json");

  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && !retried) {
    const key = state.authPrompted ? "" : window.prompt("This server requires an API key:");
    state.authPrompted = false;
    if (key) {
      state.apiKey = key.trim();
      localStorage.setItem(KEY_STORE, state.apiKey);
      connectEvents();
      return api(path, options, true);
    }
    throw new Error("Unauthorized — set the API key to continue");
  }

  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    const detail = payload && (payload.detail || payload.message);
    throw new Error(typeof detail === "string" ? detail : `${response.status} ${response.statusText}`);
  }
  return payload;
}

/* ------------------------------------------------------------------ prefs */
function loadPrefs() {
  let prefs = {};
  try {
    prefs = JSON.parse(localStorage.getItem(PREFS_STORE) || "{}");
  } catch {
    prefs = {};
  }
  for (const id of PREFS) {
    const element = document.getElementById(id);
    if (!element || !(id in prefs)) continue;
    if (element.type === "checkbox") element.checked = Boolean(prefs[id]);
    else element.value = prefs[id];
  }
  state.quality = prefs.quality || "best";
  applyQualityUI();
  syncAudioUI();
}

function savePrefs() {
  const prefs = { quality: state.quality };
  for (const id of PREFS) {
    const element = document.getElementById(id);
    if (!element) continue;
    prefs[id] = element.type === "checkbox" ? element.checked : element.value;
  }
  try {
    localStorage.setItem(PREFS_STORE, JSON.stringify(prefs));
  } catch {
    /* storage may be unavailable */
  }
}

/* --------------------------------------------------------------- options */
function value(id) {
  const element = document.getElementById(id);
  return element ? element.value.trim() : "";
}

function checked(id) {
  const element = document.getElementById(id);
  return Boolean(element && element.checked);
}

function collectOptions() {
  const fragments = parseInt(value("fragments"), 10);
  return {
    mode: value("mode") || "auto",
    quality: state.quality,
    container: value("container") || "mp4",
    audio_only: checked("audioOnly"),
    audio_format: value("audioFormat") || "mp3",
    audio_quality: "0",
    playlist_items: value("playlistItems"),
    playlist_reverse: checked("reverse"),
    subtitles: checked("subtitles"),
    auto_subtitles: checked("autoSubs"),
    subtitle_langs: value("subLangs") || "en",
    embed_subtitles: checked("embedSubs"),
    embed_thumbnail: checked("embedThumb"),
    embed_metadata: checked("embedMeta"),
    chapters: checked("chapters"),
    sponsorblock: value("sponsorblock") || "off",
    cookies_file: value("cookieFile"),
    cookies_from_browser: value("browserCookies"),
    proxy: value("proxy"),
    rate_limit: value("rateLimit"),
    section: value("section"),
    archive: checked("archive"),
    use_aria2: checked("useAria2"),
    fragments: Number.isFinite(fragments) ? fragments : null,
    retries: null,
    output_template: "",
  };
}

function applyQualityUI() {
  $$("#quality-chips .chip").forEach((chip) => {
    chip.classList.toggle("is-active", chip.dataset.quality === state.quality);
    chip.setAttribute("aria-checked", String(chip.dataset.quality === state.quality));
  });
}

function syncAudioUI() {
  const audio = checked("audioOnly");
  $("#quality-chips").classList.toggle("disabled", audio);
  $("#container").disabled = audio;
  $("#container").closest(".mini-field").style.opacity = audio ? "0.45" : "";
}

function setQuality(quality) {
  state.quality = quality;
  applyQualityUI();
  savePrefs();
}

/* ---------------------------------------------------------------- system */
function renderSystem(system) {
  state.system = system;
  const transport = $("#pill-transport");
  const workers = system.max_concurrent_downloads;
  transport.className = "pill " + (system.aria2 ? "ok" : "warn");
  transport.innerHTML = `<span class="dot"></span>${system.aria2 ? `aria2c · ${workers}× workers` : `native · ${workers}× workers`}`;
  transport.title = system.aria2
    ? "aria2c multi-connection transport is active"
    : "aria2c not found — using yt-dlp's native transport";

  const ffmpeg = $("#pill-ffmpeg");
  ffmpeg.className = "pill subtle " + (system.ffmpeg ? "ok" : "err");
  ffmpeg.textContent = system.ffmpeg ? "ffmpeg ready" : "ffmpeg missing";

  const dir = $("#pill-dir");
  dir.querySelector("span").textContent = baseName(system.download_dir) || "downloads";
  dir.title = `Saving to ${system.download_dir}`;
  dir.className = "pill subtle dir ok";

  if (system.auth_required && !state.apiKey) {
    toast("This server requires an API key", "warn");
  }
}

async function loadSystem() {
  try {
    renderSystem(await api("/api/system"));
  } catch (error) {
    const transport = $("#pill-transport");
    transport.className = "pill err";
    transport.innerHTML = `<span class="dot"></span>server offline`;
    transport.title = error.message;
  }
}

/* ----------------------------------------------------------------- probe */
function renderProbeLoading() {
  const container = $("#probe");
  container.classList.remove("hidden");
  container.innerHTML = `<div class="probe-loading"><span class="spinner"></span><span>Reading formats and playlist entries…</span></div>`;
}

function renderProbeError(message) {
  const container = $("#probe");
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="probe-error">
      ${icon("alert")}
      <div><strong>Could not analyze this URL</strong>${esc(message)}</div>
    </div>`;
}

function renderProbe() {
  const container = $("#probe");
  if (!state.probe) {
    container.classList.add("hidden");
    container.innerHTML = "";
    return;
  }
  const probe = state.probe;
  const isPlaylist = probe.kind === "playlist";
  const thumb = probe.thumbnail
    ? `<img src="${esc(probe.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : "";
  const durationBadge = probe.duration ? `<span class="badge">${duration(probe.duration)}</span>` : "";
  const live = ["is_live", "is_upcoming"].includes(probe.live_status)
    ? `<span class="live">${probe.live_status === "is_live" ? "live" : "upcoming"}</span>`
    : "";

  const metaParts = [];
  if (probe.uploader) metaParts.push(esc(probe.uploader));
  if (probe.duration) metaParts.push(duration(probe.duration));
  if (isPlaylist) {
    metaParts.push(`${probe.entry_count} video${probe.entry_count === 1 ? "" : "s"}`);
    if (probe.entries.length < probe.entry_count) metaParts.push(`${probe.entries.length} listed`);
  } else {
    metaParts.push(`${(probe.qualities || []).length || "—"} qualities`);
  }

  let body = "";
  if (isPlaylist) {
    body = `
      <div class="playlist-toolbar">
        <input class="grow" id="entryFilter" type="text" placeholder="Filter titles…" value="${esc(state.entryFilter)}" ${probe.entries.length ? "" : "disabled"}>
        <button type="button" class="toolbar-btn" id="sel-all">All</button>
        <button type="button" class="toolbar-btn" id="sel-none">None</button>
        <button type="button" class="toolbar-btn" id="sel-invert">Invert</button>
        <span class="sel-count" id="sel-count">${state.selected.size} of ${probe.entries.length}</span>
      </div>
      <div class="playlist-toolbar">
        <input id="rangeInput" type="text" placeholder="Parts: 1-5,8,20-25" ${probe.entries.length ? "" : "disabled"}>
        <button type="button" class="toolbar-btn" id="apply-range">Apply parts</button>
        <span class="muted">${probe.truncated ? `first ${probe.entries.length} entries shown` : "checkboxes take priority"}</span>
      </div>
      <div class="entries" id="entries"></div>`;
  } else {
    const qualities = (probe.qualities || []).slice(0, 10).map((quality) => `
      <button type="button" class="qbadge" data-set-quality="${quality.height}"
              title="${quality.estimated_bytes ? "≈ " + bytes(quality.estimated_bytes) : "available"}">${esc(quality.label)}</button>`).join("");
    body = `<div class="media-qualities">${qualities || '<span class="muted">No video formats listed</span>'}</div>`;
  }

  container.innerHTML = `
    <div class="media">
      <div class="media-thumb">${thumb}${durationBadge}${live}</div>
      <div class="media-body">
        <span class="media-kind ${isPlaylist ? "" : "video"}">${isPlaylist ? "playlist" : "video"}</span>
        <h3 class="media-title">${esc(probe.title)}</h3>
        <div class="media-meta">${metaParts.map((part) => `<span>${part}</span>`).join("")}</div>
        ${isPlaylist ? "" : body}
      </div>
    </div>
    ${isPlaylist ? body : ""}
    ${probe.truncated ? '<p class="note">The playlist is longer than the server cap — adjust <code>TT_PROBE_MAX_ENTRIES</code> if you need every entry.</p>' : ""}`;
  container.classList.remove("hidden");

  if (isPlaylist) renderEntries();
}

function filteredEntries() {
  const entries = state.probe ? state.probe.entries : [];
  const needle = state.entryFilter.trim().toLowerCase();
  return needle ? entries.filter((entry) => entry.title.toLowerCase().includes(needle)) : entries;
}

function renderEntries() {
  const container = $("#entries");
  if (!container || !state.probe) return;
  const filtered = filteredEntries();
  const limit = state.showAll ? filtered.length : MAX_ENTRIES;
  const visible = filtered.slice(0, limit);

  container.innerHTML = visible.map((entry) => `
    <label class="entry">
      <input type="checkbox" class="entry-check" data-url="${esc(entry.url)}" ${state.selected.has(entry.url) ? "checked" : ""}>
      <span class="entry-index">${entry.index}</span>
      <span class="entry-title" title="${esc(entry.title)}">${esc(entry.title)}</span>
      <span class="entry-duration">${duration(entry.duration) || "—"}</span>
    </label>`).join("") || `<div class="entries-more muted">No titles match the filter.</div>`;

  if (filtered.length > visible.length) {
    container.insertAdjacentHTML("beforeend",
      `<div class="entries-more"><button type="button" class="toolbar-btn" id="show-all">Show all ${filtered.length}</button></div>`);
  }

  const count = $("#sel-count");
  if (count) count.textContent = `${state.selected.size} of ${state.probe.entries.length} selected`;
}

function parseSpec(text, total) {
  const value = text.trim();
  if (!value || value.toLowerCase() === "all" || value === "*") return null;
  const parts = value.split(/[,\s]+/).filter(Boolean);
  const output = [];
  for (const part of parts) {
    const match = part.match(/^(\d+)(?:-(\d*))?$/);
    if (!match) throw new Error(`Invalid part "${part}" — use 1-5,8,10-12`);
    const start = Number(match[1]);
    const isRange = part.includes("-");
    const end = isRange ? (match[2] ? Number(match[2]) : total) : start;
    if (start < 1) throw new Error("Parts start at 1");
    if (end < start) throw new Error(`Part "${part}" is backwards`);
    if (end > total) throw new Error(`Part "${part}" exceeds the playlist length (${total})`);
    for (let index = start; index <= end; index += 1) output.push(index);
  }
  return [...new Set(output)];
}

async function analyze() {
  const url = $("#url").value.trim();
  if (!url) {
    toast("Paste a media URL first", "warn");
    $("#url").focus();
    return;
  }
  const button = $("#analyze");
  setLoading(button, true, "Analyzing…");
  renderProbeLoading();
  const options = collectOptions();
  try {
    const probe = await api("/api/probe", {
      method: "POST",
      body: JSON.stringify({
        url,
        mode: options.mode,
        cookies_file: options.cookies_file,
        cookies_from_browser: options.cookies_from_browser,
        proxy: options.proxy,
      }),
    });
    state.probe = probe;
    state.probeUrl = url;
    state.entryFilter = "";
    state.showAll = false;
    state.selected = new Set(probe.kind === "playlist" ? probe.entries.map((entry) => entry.url) : []);
    renderProbe();
    if (probe.kind === "playlist" && probe.truncated) {
      toast(`Playlist truncated to ${probe.entries.length} entries`, "warn");
    }
  } catch (error) {
    state.probe = null;
    state.probeUrl = "";
    renderProbeError(error.message);
  } finally {
    setLoading(button, false);
  }
}

function selectAll(entries) {
  state.selected = new Set(entries.map((entry) => entry.url));
  renderEntries();
}

/* --------------------------------------------------------------- submit */
async function submitDownload(event) {
  event.preventDefault();
  const url = $("#url").value.trim();
  if (!url) {
    toast("Paste a media URL first", "warn");
    $("#url").focus();
    return;
  }

  const options = collectOptions();
  let entries = null;
  if (state.probe && state.probeUrl === url && state.probe.kind === "playlist") {
    if (!state.selected.size) {
      toast("Select at least one playlist video", "warn");
      return;
    }
    entries = state.probe.entries
      .filter((entry) => state.selected.has(entry.url))
      .map((entry) => ({
        url: entry.url,
        title: entry.title,
        index: entry.index,
        duration: entry.duration,
      }));
  }

  const button = $("#submit");
  setLoading(button, true, "Adding…");
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        url,
        options,
        entries,
        title: state.probeUrl === url && state.probe ? state.probe.title : "",
      }),
    });
    toast(`Queued ${job.total_items} video${job.total_items === 1 ? "" : "s"}`, "ok");
    upsertJob(job);
    $("#url").value = "";
    state.probe = null;
    state.probeUrl = "";
    state.selected.clear();
    renderProbe();
    $("#url").focus();
    savePrefs();
  } catch (error) {
    toast(error.message, "err");
  } finally {
    setLoading(button, false);
  }
}

/* ------------------------------------------------------------------ jobs */
function statusBadge(status) {
  const label = STATUS_LABELS[status] || status;
  return `<span class="status st-${esc(status)}">${esc(label)}</span>`;
}

function jobClass(job) {
  if (["running", "queued"].includes(job.status)) return "is-active";
  if (job.status === "completed") return "is-done";
  if (["failed", "partial", "interrupted"].includes(job.status)) return "is-failed";
  return "";
}

function retryableJob(job) {
  return (
    (job.failed_items || 0) > 0 ||
    (job.cancelled_items || 0) > 0 ||
    ["failed", "partial", "cancelled", "interrupted"].includes(job.status)
  );
}

function jobMeta(job) {
  const parts = [`${job.completed_items}/${job.total_items} done`];
  if (job.failed_items) parts.push(`<span class="bad">${job.failed_items} failed</span>`);
  if (job.active_items) parts.push(`${job.active_items} active`);
  if (job.speed > 0) parts.push(`<span class="hot">${bytes(job.speed)}/s</span>`);
  if (job.eta) parts.push(`ETA ${duration(job.eta)}`);
  if (job.total_bytes) parts.push(bytes(job.total_bytes));
  return parts.join('<span class="dotsep">·</span>');
}

function jobStats(job) {
  const percent = Math.max(0, Math.min(100, job.percent || 0));
  const size = job.total_bytes
    ? `${bytes(job.downloaded)} / ${bytes(job.total_bytes)}`
    : bytes(job.downloaded);
  return `<span class="pct">${percent.toFixed(percent < 10 ? 1 : 0)}%</span><span>${size}</span>`;
}

function jobBarClass(job) {
  if (job.status === "completed") return "done";
  if (["failed", "partial", "interrupted"].includes(job.status)) return "fail";
  return "";
}

function jobActions(job) {
  const canCancel = ["queued", "running"].includes(job.status);
  const canRetry = retryableJob(job);
  return `
    <button type="button" class="icon-btn cancel-btn" data-action="cancel" data-job="${job.id}" title="Cancel job" ${canCancel ? "" : "disabled"}>${icon("x")}</button>
    <button type="button" class="icon-btn" data-action="retry" data-job="${job.id}" title="Retry failed parts" ${canRetry ? "" : "disabled"}>${icon("retry")}</button>
    <button type="button" class="icon-btn always" data-action="open" data-job="${job.id}" title="Open download folder">${icon("folder")}</button>
    <button type="button" class="icon-btn danger always" data-action="delete" data-job="${job.id}" title="Remove from list (Shift+click also deletes files)">${icon("trash")}</button>`;
}

function jobCardHTML(job) {
  const items = (job.items || []).map(itemRowHTML).join("");
  const kindIcon = job.kind === "playlist" ? "list" : "film";
  const percent = Math.max(0, Math.min(100, job.percent || 0));
  return `
    <article class="job ${jobClass(job)}" data-job="${job.id}">
      <div class="job-head">
        <div class="job-top">
          <span class="job-kind">${icon(kindIcon)}</span>
          <div class="job-titles">
            <h3 class="job-title" title="${esc(job.title)}">${esc(job.title)}</h3>
            <div class="job-meta">
              <span data-role="status">${statusBadge(job.status)}</span>
              <span data-role="meta">${jobMeta(job)}</span>
            </div>
          </div>
          <div class="job-actions" data-role="actions">${jobActions(job)}</div>
        </div>
        <div class="job-progress">
          <div class="bar ${jobBarClass(job)}" data-role="bar"><span style="width:${percent}%"></span></div>
          <div class="job-stats" data-role="stats">${jobStats(job)}</div>
        </div>
      </div>
      <div class="items" data-role="items">${items}</div>
    </article>`;
}

function itemStatusClass(status) {
  if (status === "completed") return "is-done";
  if (["failed", "cancelled", "interrupted"].includes(status)) return "is-fail";
  if (["running", "downloading"].includes(status)) return "is-run";
  if (status === "processing") return "is-proc";
  return "";
}

function itemActions(item) {
  const active = ["queued", "running", "downloading", "processing"].includes(item.status);
  const retryable = ["failed", "cancelled", "interrupted"].includes(item.status);
  const hasFile = Boolean(item.filepath);
  return `
    ${retryable ? `<button type="button" class="icon-btn" data-action="retry-item" data-job="${item.job_id}" data-item="${item.id}" title="Retry this video">${icon("retry")}</button>` : ""}
    ${active ? `<button type="button" class="icon-btn danger" data-action="cancel-item" data-job="${item.job_id}" data-item="${item.id}" title="Cancel this video">${icon("x")}</button>` : ""}
    ${hasFile ? `<button type="button" class="icon-btn" data-action="open-item" data-job="${item.job_id}" data-item="${item.id}" title="Show in folder">${icon("folder")}</button>` : ""}
    ${hasFile ? `<button type="button" class="icon-btn" data-action="copy" data-job="${item.job_id}" data-item="${item.id}" title="Copy file path">${icon("copy")}</button>` : ""}`;
}

function itemStats(item) {
  const percent = item.status === "completed" ? 100 : Math.max(0, Math.min(100, item.percent || 0));
  if (item.status === "completed") {
    return `<span class="pct">100%</span><span>${bytes(item.total)}</span>`;
  }
  if (item.status === "failed") return `<span class="bad">download failed</span>`;
  if (item.status === "cancelled") return `<span>cancelled</span>`;
  if (item.status === "interrupted") return `<span>interrupted by restart</span>`;
  if (item.status === "queued") return `<span>waiting for a worker</span>`;
  if (item.status === "processing") return `<span class="pct">${percent.toFixed(0)}%</span><span>merging / post-processing…</span>`;

  const parts = [`<span class="pct">${percent.toFixed(percent < 10 ? 1 : 0)}%</span>`];
  if (item.speed > 0) parts.push(`<span>${bytes(item.speed)}/s</span>`);
  if (item.total) parts.push(`<span>${bytes(item.downloaded)} / ${bytes(item.total)}</span>`);
  else if (item.downloaded) parts.push(`<span>${bytes(item.downloaded)}</span>`);
  if (item.eta) parts.push(`<span>ETA ${duration(item.eta)}</span>`);
  return parts.join("");
}

function itemRowHTML(item) {
  const percent = item.status === "completed" ? 100 : Math.max(0, Math.min(100, item.percent || 0));
  const error = ["failed", "cancelled", "interrupted"].includes(item.status) ? item.error : "";
  return `
    <div class="item ${item.status}" data-item="${item.id}" data-state="${item.status}">
      <div class="item-top">
        <span class="item-index">${String(item.index || 0).padStart(2, "0")}</span>
        <span class="item-title" title="${esc(item.title)}">${esc(item.title)}</span>
        <span class="item-status ${itemStatusClass(item.status)}">${esc(STATUS_LABELS[item.status] || item.status)}</span>
        <div class="item-actions">${itemActions(item)}</div>
      </div>
      <div class="item-bar">
        <div class="bar ${item.status === "completed" ? "done" : item.status === "failed" ? "fail" : ""}">
          <span style="width:${percent}%"></span>
        </div>
      </div>
      <div class="item-stats">${itemStats(item)}</div>
      <div class="item-error" title="${esc(error)}">${esc(error)}</div>
    </div>`;
}

function renderJobs() {
  const container = $("#jobs");
  const jobs = Array.from(state.jobs.values()).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  container.innerHTML = jobs.map(jobCardHTML).join("");
  $("#empty").classList.toggle("hidden", jobs.length > 0);
  updateQueueMeta();
}

function updateQueueMeta() {
  const jobs = Array.from(state.jobs.values());
  const active = jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
  $("#queue-count").textContent = String(jobs.length);
  $("#queue-sub").textContent = jobs.length
    ? `${active} active · ${jobs.length} job${jobs.length === 1 ? "" : "s"} in this session`
    : "Live progress streamed from the download workers.";
  const clear = $("#clear-finished");
  clear.disabled = !jobs.some((job) => ["completed", "failed", "cancelled", "partial", "interrupted"].includes(job.status));
}

function updateJobCard(job) {
  const card = document.querySelector(`.job[data-job="${job.id}"]`);
  if (!card) {
    renderJobs();
    return;
  }
  card.className = `job ${jobClass(job)}`;
  const status = card.querySelector('[data-role="status"]');
  if (status) status.innerHTML = statusBadge(job.status);
  const meta = card.querySelector('[data-role="meta"]');
  if (meta) meta.innerHTML = jobMeta(job);
  const actions = card.querySelector('[data-role="actions"]');
  if (actions) actions.innerHTML = jobActions(job);
  const bar = card.querySelector('[data-role="bar"]');
  if (bar) {
    bar.className = `bar ${jobBarClass(job)}`;
    bar.firstElementChild.style.width = `${Math.max(0, Math.min(100, job.percent || 0))}%`;
  }
  const stats = card.querySelector('[data-role="stats"]');
  if (stats) stats.innerHTML = jobStats(job);
  updateQueueMeta();
}

function upsertJob(job) {
  if (!job || !job.id) return;
  const previous = state.jobs.get(job.id) || {};
  const merged = { ...previous, ...job };
  if (!job.items && previous.items) merged.items = previous.items;
  state.jobs.set(job.id, merged);
  if (!job.items && !previous.id) {
    syncJobs();
    return;
  }
  if (document.querySelector(`.job[data-job="${job.id}"]`)) updateJobCard(merged);
  else renderJobs();
}

function updateItemRow(item) {
  const job = state.jobs.get(item.job_id);
  if (!job) {
    syncJobs();
    return;
  }
  if (!job.items) job.items = [];
  const index = job.items.findIndex((candidate) => candidate.id === item.id);
  if (index === -1) {
    job.items.push(item);
    renderJobs();
    return;
  }
  job.items[index] = item;

  const row = document.querySelector(`.item[data-item="${item.id}"]`);
  if (!row) return;

  const statusCell = row.querySelector(".item-status");
  statusCell.className = `item-status ${itemStatusClass(item.status)}`;
  statusCell.textContent = STATUS_LABELS[item.status] || item.status;

  const bar = row.querySelector(".item-bar .bar");
  bar.className = `bar ${item.status === "completed" ? "done" : item.status === "failed" ? "fail" : ""}`;
  bar.firstElementChild.style.width = `${item.status === "completed" ? 100 : Math.max(0, Math.min(100, item.percent || 0))}%`;
  row.querySelector(".item-stats").innerHTML = itemStats(item);

  const error = ["failed", "cancelled", "interrupted"].includes(item.status) ? item.error : "";
  const errorCell = row.querySelector(".item-error");
  errorCell.textContent = error || "";
  errorCell.title = error || "";

  if (row.dataset.state !== item.status) {
    row.dataset.state = item.status;
    row.className = `item ${item.status}`;
    row.querySelector(".item-actions").innerHTML = itemActions(item);
  }
}

async function syncJobs() {
  if (state.syncPromise) return state.syncPromise;
  state.syncPromise = (async () => {
    try {
      const data = await api("/api/jobs?limit=100");
      state.jobs = new Map((data.jobs || []).map((job) => [job.id, job]));
      renderJobs();
    } catch (error) {
      /* transient network errors are handled by the SSE reconnect */
    } finally {
      state.syncPromise = null;
    }
  })();
  return state.syncPromise;
}

/* --------------------------------------------------------------- actions */
async function handleJobAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const { action, job: jobId, item: itemId } = button.dataset;
  if (!action || !jobId) return;

  try {
    if (action === "cancel") {
      button.disabled = true;
      await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      toast("Cancelling job…", "warn");
    } else if (action === "retry") {
      const result = await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
      toast(`Retrying ${result.retried} video${result.retried === 1 ? "" : "s"}…`, "ok");
    } else if (action === "cancel-item") {
      button.disabled = true;
      await api(`/api/jobs/${jobId}/items/${itemId}/cancel`, { method: "POST" });
    } else if (action === "retry-item") {
      await api(`/api/jobs/${jobId}/items/${itemId}/retry`, { method: "POST" });
      toast("Retrying video…", "ok");
    } else if (action === "open") {
      await api(`/api/jobs/${jobId}/open`, { method: "POST" });
    } else if (action === "open-item") {
      await api(`/api/jobs/${jobId}/open?item_id=${encodeURIComponent(itemId)}`, { method: "POST" });
    } else if (action === "copy") {
      const job = state.jobs.get(jobId);
      const item = job && job.items ? job.items.find((candidate) => candidate.id === itemId) : null;
      if (item && item.filepath) await copyText(item.filepath);
    } else if (action === "delete") {
      const deleteFiles = event.shiftKey;
      const message = deleteFiles
        ? "Delete this job AND its downloaded files? This cannot be undone."
        : "Remove this job from the list? Downloaded files stay on disk.";
      if (!window.confirm(message)) return;
      await api(`/api/jobs/${jobId}?delete_files=${deleteFiles}`, { method: "DELETE" });
      state.jobs.delete(jobId);
      renderJobs();
      toast(deleteFiles ? "Job and files deleted" : "Job removed from list", "ok");
      return;
    }
    setTimeout(syncJobs, 250);
  } catch (error) {
    toast(error.message, "err");
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("File path copied", "ok");
}

async function clearFinished() {
  const finished = Array.from(state.jobs.values()).filter((job) =>
    ["completed", "failed", "cancelled", "partial", "interrupted"].includes(job.status));
  if (!finished.length) return;
  if (!window.confirm(`Remove ${finished.length} finished job${finished.length === 1 ? "" : "s"} from the list? Files stay on disk.`)) return;
  const button = $("#clear-finished");
  setLoading(button, true, "Clearing…");
  try {
    await Promise.all(finished.map((job) => api(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(() => null)));
    await syncJobs();
    toast("Finished jobs cleared", "ok");
  } finally {
    setLoading(button, false);
  }
}

async function uploadCookies(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    toast("Uploading cookies…", "warn");
    const result = await api("/api/cookies", { method: "POST", body: form });
    $("#cookieFile").value = result.id;
    savePrefs();
    toast("Cookie file stored", "ok");
  } catch (error) {
    toast(error.message, "err");
  } finally {
    $("#cookieUpload").value = "";
  }
}

/* ------------------------------------------------------------------- sse */
function connectEvents() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  const query = state.apiKey ? `?key=${encodeURIComponent(state.apiKey)}` : "";
  const source = new EventSource(`/api/events${query}`);
  state.es = source;

  source.onopen = () => {
    state.reconnect = 0;
    setConnection(true);
    stopPolling();
    syncJobs();
  };

  source.onerror = () => {
    setConnection(false);
    source.close();
    if (state.es === source) state.es = null;
    startPolling();
    const delay = Math.min(15000, 1000 * Math.pow(1.6, Math.min(state.reconnect, 6)));
    state.reconnect += 1;
    setTimeout(connectEvents, delay);
  };

  source.onmessage = (event) => {
    try {
      handleEvent(JSON.parse(event.data));
    } catch {
      /* ignore malformed frame */
    }
  };
}

function handleEvent(event) {
  if (event.type === "snapshot") {
    state.jobs = new Map((event.jobs || []).map((job) => [job.id, job]));
    renderJobs();
    return;
  }
  if (event.type === "job") {
    upsertJob(event.job);
    return;
  }
  if (event.type === "item") {
    updateItemRow(event.item);
  }
}

function setConnection(online) {
  const node = $("#conn");
  node.classList.toggle("on", online);
  node.classList.toggle("off", !online);
  node.title = online ? "Live progress connected" : "Reconnecting to live progress…";
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(syncJobs, 12000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/* ------------------------------------------------------------------ init */
function bindEvents() {
  $("#form").addEventListener("submit", submitDownload);
  $("#analyze").addEventListener("click", analyze);

  $("#url").addEventListener("input", () => {
    const current = $("#url").value.trim();
    if (state.probe && current !== state.probeUrl) {
      state.probe = null;
      state.probeUrl = "";
      state.selected.clear();
      renderProbe();
    }
  });

  $("#url").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      analyze();
    }
  });

  $("#paste").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text.trim()) return;
      $("#url").value = text.trim();
      analyze();
    } catch {
      toast("Clipboard access was blocked — paste manually", "warn");
      $("#url").focus();
    }
  });

  $("#quality-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    setQuality(chip.dataset.quality);
  });

  $("#audioOnly").addEventListener("change", syncAudioUI);
  $("#cookieUpload").addEventListener("change", (event) => uploadCookies(event.target.files[0]));
  $("#clear-finished").addEventListener("click", clearFinished);

  $("#form").addEventListener("input", savePrefs);
  $("#form").addEventListener("change", savePrefs);

  $("#probe").addEventListener("click", (event) => {
    const quality = event.target.closest("[data-set-quality]");
    if (quality) {
      const height = quality.dataset.setQuality;
      const known = ["best", "2160", "1440", "1080", "720", "480", "360", "worst"];
      setQuality(known.includes(height) ? height : "best");
      return;
    }
    const showAll = event.target.closest("#show-all");
    if (showAll) {
      state.showAll = true;
      renderEntries();
      return;
    }
    const button = event.target.closest("button");
    if (!button || !state.probe) return;
    if (button.id === "sel-all") selectAll(state.probe.entries);
    else if (button.id === "sel-none") {
      state.selected.clear();
      renderEntries();
    } else if (button.id === "sel-invert") {
      const next = new Set();
      for (const entry of state.probe.entries) {
        if (!state.selected.has(entry.url)) next.add(entry.url);
      }
      state.selected = next;
      renderEntries();
    } else if (button.id === "apply-range") {
      const input = $("#rangeInput");
      try {
        const indexes = parseSpec(input.value, state.probe.entries.length);
        if (indexes === null) selectAll(state.probe.entries);
        else {
          state.selected = new Set(indexes.map((index) => state.probe.entries[index - 1].url));
          renderEntries();
        }
        toast(`${state.selected.size} part${state.selected.size === 1 ? "" : "s"} selected`, "ok");
      } catch (error) {
        toast(error.message, "err");
      }
    }
  });

  $("#probe").addEventListener("change", (event) => {
    if (!event.target.classList.contains("entry-check")) return;
    const url = event.target.dataset.url;
    if (event.target.checked) state.selected.add(url);
    else state.selected.delete(url);
    const count = $("#sel-count");
    if (count && state.probe) {
      count.textContent = `${state.selected.size} of ${state.probe.entries.length} selected`;
    }
  });

  $("#probe").addEventListener("input", (event) => {
    if (event.target.id !== "entryFilter") return;
    state.entryFilter = event.target.value;
    renderEntries();
  });

  $("#probe").addEventListener("keydown", (event) => {
    if (event.target.id === "rangeInput" && event.key === "Enter") {
      event.preventDefault();
      const apply = $("#apply-range");
      if (apply) apply.click();
    }
  });

  $("#jobs").addEventListener("click", handleJobAction);

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      $("#form").requestSubmit();
    }
  });
}

async function init() {
  loadPrefs();
  bindEvents();
  renderProbe();
  renderJobs();
  setConnection(false);
  await loadSystem();
  connectEvents();
  startPolling();
  // Refresh system info occasionally so transport/ffmpeg badges stay honest.
  setInterval(loadSystem, 60000);
}

init();
