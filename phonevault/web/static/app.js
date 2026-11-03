"use strict";

/* Phonevault web interface.
 *
 * Plain DOM, no framework: the whole app is a handful of views over a small
 * JSON API, and a build step would cost more than it saves.
 *
 * Every value that came from the API is inserted as text, never as HTML --
 * file names come off a phone and are not trustworthy markup.
 */

const state = {
  profile: null,
  status: null,
  snapshots: [],
  browse: { snapshot: "latest", path: "/", entries: [], searching: false },
  selection: new Set(),
  pollTimer: null,
  device: null,   // what the attached phone reports now, once we have asked it
};

/* ---------- small helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function bytes(n) {
  if (n === null || n === undefined) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = Number(n), i = 0;
  while (Math.abs(value) >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return i === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[i]}`;
}

function duration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function when(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}

function toast(message, kind = "") {
  const node = el("div", { class: `toast ${kind}`.trim(), text: message });
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), kind === "bad" ? 9000 : 4500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error(`${response.status} ${response.statusText}`); }
  if (!response.ok || payload.error) throw new Error(payload.error || `${response.status}`);
  return payload;
}

function withProfile(path) {
  const sep = path.includes("?") ? "&" : "?";
  return state.profile ? `${path}${sep}profile=${encodeURIComponent(state.profile)}` : path;
}

function post(path, body = {}) {
  return api(path, { method: "POST", body: JSON.stringify({ profile: state.profile, ...body }) });
}

/* ---------- tabs ---------- */

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const name = tab.dataset.tab;
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    if (name === "snapshots") loadSnapshots();
    if (name === "browse") initBrowse();
    if (name === "maintenance") loadRetention();
    if (name === "settings") { loadSettings(); loadDevices(); }
  });
});

/* ---------- overview ---------- */

async function loadStatus() {
  let status;
  try {
    status = await api(withProfile("/api/status"));
  } catch (err) {
    $("#repo-line").textContent = err.message;
    return;
  }
  state.status = status;
  if (!state.profile) state.profile = status.profile;

  const select = $("#profile-select");
  if (select.options.length !== status.profiles.length) {
    clear(select);
    status.profiles.forEach((name) =>
      select.append(el("option", { value: name, selected: name === status.profile }, name)));
  }

  $("#repo-line").textContent = status.repository || "no repository configured";

  const cards = $("#summary-cards");
  clear(cards);
  const latest = status.latest;
  cards.append(
    stat("Snapshots", status.snapshot_count ?? "-", status.repo_error ? "repository unreachable" : ""),
    stat("Last backup", latest ? latest.when.split(" ")[0] : "never",
         latest ? latest.when.split(" ")[1] : "run one below"),
    stat("Files", latest ? latest.files.toLocaleString() : "-", latest ? bytes(latest.size) : ""),
    stat("Phone", ...phoneStat(status, latest)),
  );

  const banner = [];
  if (status.repo_error) banner.push(["bad", status.repo_error]);
  (status.problems || []).forEach((p) => banner.push(["warn", p]));
  if ((status.locks || []).length) {
    banner.push(["warn", `${status.locks.length} lock(s) held on the repository`]);
  }
  banner.forEach(([kind, text]) => cards.parentElement.insertBefore(
    el("div", { class: `banner ${kind}`, text }), cards.nextSibling));

  const paths = $("#backup-paths");
  clear(paths);
  renderRoots(paths, status);

  renderJob(status.job);
  updateDevicePill();
}

/* The list of paths the backup covers, editable in place.
 *
 * This used to be display-only, so changing what was covered meant finding
 * the comma-separated field on the settings tab -- easy to miss, and easy to
 * mistype into one long string. Editing is disabled while a job runs because
 * the running backup already holds its own copy of the list: a change made
 * now would appear to take effect but would not reach it. */
function renderRoots(box, status) {
  // A lock counts as busy too: backups also run from the CLI and the
  // scheduler, and those hold the repository without this page having a job.
  const busy = !!(status.job && status.job.state === "running")
            || !!(status.locks || []).length;
  const roots = status.roots || [];

  box.append(el("span", { class: "hint" }, "Backing up: "));
  roots.forEach((r) => box.append(
    el("span", { class: "root" },
      el("code", {}, r),
      el("button", {
        class: "rootx", title: `Stop backing up ${r}`, "aria-label": `Remove ${r}`,
        disabled: busy,
        onclick: () => setRoots(roots.filter((x) => x !== r), `No longer backing up ${r}`),
      }, "×"))));

  if (!roots.length) {
    box.append(el("span", { class: "warnrow", text: "no paths set — a backup would cover nothing" }));
  }

  const input = el("input", {
    type: "text", id: "root-add", class: "rootadd",
    placeholder: "add a path, e.g. /data/vendor", disabled: busy,
  });
  const submit = () => {
    const value = input.value.trim();
    if (!value) return;
    if (roots.includes(value)) { toast(`${value} is already in the list.`, "bad"); return; }
    // The server rejects PC paths outright; catch the obvious slip here so the
    // message arrives before a round trip.
    if (!value.startsWith("/")) {
      toast("Paths are on the phone and start with /, such as /sdcard.", "bad");
      return;
    }
    input.value = "";
    setRoots(roots.concat([value]), `Now backing up ${value}`);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  });
  box.append(el("span", { class: "addroot" },
    input, el("button", { disabled: busy, onclick: submit }, "Add")));

  if (busy) box.append(el("span", { class: "hint", text: "locked while a backup is running" }));
}

async function setRoots(roots, note) {
  if (!roots.length) {
    toast("Keep at least one path, or the backup would cover nothing.", "bad");
    return;
  }
  try {
    await post("/api/config", { settings: { roots } });
    toast(note, "good");
    loadStatus();
  } catch (err) { toast(err.message, "bad"); }
}

/* What to show for "Phone", as [value, note].
 *
 * A snapshot records the phone as it was when that backup ran, so after an OS
 * upgrade it keeps describing the old system until the next backup completes
 * -- which reads as though the attached phone were still on the old version.
 * Prefer what the phone says about itself now, and when only the snapshot's
 * value is available, label it as historical rather than letting it pass for
 * the current state. */
function phoneStat(status, latest) {
  const live = state.device;
  if (live) return [live, status.source];
  const recorded = latest ? latest.device.split(" [")[0] : null;
  if (recorded) return [recorded, `${status.source} · as of last backup`];
  return [status.source === "adb" ? "not backed up" : "local", status.source];
}

function stat(label, value, note) {
  return el("div", { class: "stat" },
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: String(value) }),
    note ? el("div", { class: "note", text: note }) : null);
}

function renderJob(job) {
  const area = $("#job-area");
  clear(area);
  if (!job) return;

  const p = job.progress || {};
  const running = job.state === "running";
  const total = p.total_bytes || 0;
  const done = p.done_bytes || 0;
  const pct = total ? Math.min(100, (done / total) * 100) : 0;

  const headline = running
    ? `${p.phase || job.kind}${p.total_files ? ` — ${p.done_files || 0} of ${p.total_files} files` : ""}`
    : job.state === "done" ? `${job.kind} finished`
    : job.state === "paused" ? `${job.kind} paused`
    : `${job.kind} failed`;

  area.append(el("div", { class: "progress-wrap" },
    el("div", { class: "progress-line" },
      el("strong", { text: headline }),
      el("span", { class: "mono", text: total
        ? `${bytes(done)} / ${bytes(total)} · ${duration(job.elapsed)}`
        : duration(job.elapsed) })),
    el("div", { class: `bar${running && !total ? " indeterminate" : ""}` },
      el("i", { style: `width:${running ? (total ? pct : 35) : 100}%` })),
    p.current ? el("div", { class: "progress-current", text: p.current }) : null));

  if (job.error) area.append(el("div", { class: "banner bad", text: job.error }));
  if (job.result && job.result.paused) {
    area.append(el("div", { class: "banner warn", text: job.result.message }));
  } else if (job.result) {
    area.append(renderResult(job.kind, job.result));
  }

  const lines = [...(p.messages || []), ...(p.warnings || []).map((w) => `warning: ${w}`)];
  if (lines.length) {
    const log = el("div", { class: "log" });
    lines.slice(-60).forEach((line) =>
      log.append(el("div", { class: line.startsWith("warning") ? "warn" : "", text: line })));
    area.append(log);
  }
}

function renderResult(kind, result) {
  const box = el("div", { class: "banner good" });
  if (kind === "backup" && result.stats) {
    const s = result.stats;
    const saved = s.bytes_before_compression
      ? ` Compressed ${bytes(s.bytes_before_compression)} to ${bytes(s.bytes_after_compression)} ` +
        `(${(100 * (1 - s.bytes_after_compression / s.bytes_before_compression)).toFixed(1)}% saved).`
      : "";
    box.textContent =
      `Snapshot ${result.snapshot}${result.dry_run ? " (dry run)" : ""}: ` +
      `${s.files_total} files, ${s.files_new} new, ${s.files_changed} modified, ` +
      `${s.files_unchanged} unchanged. Read ${bytes(s.bytes_read)}, ` +
      `stored ${bytes(s.bytes_stored)} in ${duration(s.duration)}.` + saved +
      (s.files_resumed ? ` ${s.files_resumed} file(s) recovered from an interrupted run.` : "") +
      (s.files_failed ? ` ${s.files_failed} file(s) unreadable.` : "");
  } else if (kind === "restore" && result.stats) {
    const s = result.stats;
    box.textContent =
      `Restored ${s.files} file(s) (${bytes(s.bytes_written)}) to ${result.target}. ` +
      `${s.skipped} skipped, ${s.failed} failed.`;
  } else if (result.reindexed !== undefined) {
    box.className = `banner ${result.healthy ? "good" : "bad"}`;
    const did = [];
    if (result.reindexed.length) {
      did.push(`rebuilt ${result.reindexed.length} index entr(y/ies) from pack files`);
    }
    if (result.removed.length) did.push(`removed ${result.removed.length} unusable object(s)`);
    const scale = `${result.packs_in_repo} pack(s), ${result.local_index_entries} blob(s) indexed.`;
    box.textContent = result.healthy
      ? (did.length ? `Repaired: ${did.join(", ")}. ${scale}`
                    : `Nothing to repair. ${scale}`)
      : `${did.length ? "Repaired " + did.join(", ") + ", but " : ""}` +
        `${result.problems.length} problem(s) remain. ${scale}` +
        (result.damaged_file_count
          ? ` ${result.damaged_file_count} file(s) lost data — run a backup to store them again.`
          : "");
  } else if (result.problems !== undefined) {
    box.className = `banner ${result.healthy ? "good" : "bad"}`;
    box.textContent = result.healthy
      ? "Repository is healthy."
      : `${result.problems.length} problem(s): ${result.problems.slice(0, 3).join("; ")}`;
  } else if (result.stats && result.stats.bytes_freed !== undefined) {
    const s = result.stats;
    box.textContent = `Freed ${bytes(s.bytes_freed)} — deleted ${s.packs_deleted} pack(s), ` +
      `rewrote ${s.packs_rewritten}, ${s.blobs_live} blobs still live.`;
  } else if (result.removed) {
    box.textContent = `Kept ${result.kept}, removed ${result.removed.length} snapshot(s). ` +
      `Policy: ${result.policy}.` +
      (result.stats ? ` Freed ${bytes(result.stats.bytes_freed)}.` : " Run Prune to reclaim space.");
  } else {
    box.textContent = JSON.stringify(result);
  }
  return box;
}

async function loadHistory() {
  let data;
  try { data = await api("/api/job"); } catch { return; }
  const box = $("#history");
  clear(box);
  if (!data.history.length) { box.append(el("div", { class: "empty", text: "Nothing has run yet." })); return; }
  const table = el("table", {}, el("thead", {}, el("tr", {},
    el("th", { text: "What" }), el("th", { text: "Started" }),
    el("th", { text: "Took" }), el("th", { text: "Outcome" }))));
  const body = el("tbody");
  data.history.forEach((job) => body.append(el("tr", {},
    el("td", { text: job.kind }),
    el("td", { class: "mono", text: when(job.started) }),
    el("td", { class: "num", text: duration(job.elapsed) }),
    el("td", { class: job.state === "done" ? "good" : "bad",
               text: job.state === "done" ? "ok" : (job.error || job.state) }))));
  table.append(body);
  box.append(table);
}

/* ---------- polling ---------- */

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api("/api/job");
      renderJob(data.current);
      const running = data.current && data.current.state === "running";
      $("#btn-backup").disabled = running;
      $("#btn-pause").hidden = !(data.current && data.current.pausable);
      if (!running && state.lastRunning) { loadStatus(); loadHistory(); loadSnapshots(); }
      state.lastRunning = running;
    } catch { /* the server may be restarting; the next tick retries */ }
  }, 900);
}

/* ---------- snapshots ---------- */

async function loadSnapshots() {
  const box = $("#snapshot-table");
  try {
    const data = await api(withProfile("/api/snapshots"));
    state.snapshots = data.snapshots;
  } catch (err) {
    clear(box); box.append(el("div", { class: "banner bad", text: err.message })); return;
  }
  clear(box);
  if (!state.snapshots.length) {
    box.append(el("div", { class: "empty", text: "No snapshots yet." })); return;
  }
  const table = el("table", {}, el("thead", {}, el("tr", {},
    el("th", { text: "ID" }), el("th", { text: "When" }),
    el("th", { class: "num", text: "Files" }), el("th", { class: "num", text: "Size" }),
    el("th", { text: "Paths" }), el("th", { text: "Tags" }), el("th", {}))));
  const body = el("tbody");
  state.snapshots.forEach((s) => {
    body.append(el("tr", {},
      el("td", { class: "mono", text: s.short_id }),
      el("td", { text: s.when }),
      el("td", { class: "num", text: (s.stats.files_total || 0).toLocaleString() }),
      el("td", { class: "num", text: bytes(s.stats.bytes_total || 0) }),
      el("td", { class: "mono", text: s.roots.join(", ") }),
      el("td", { text: s.tags.join(", ") || "-" }),
      el("td", {},
        el("button", { onclick: () => browseSnapshot(s.id) }, "Browse"),
        " ",
        el("button", { class: "danger", onclick: () => deleteSnapshot(s) }, "Delete"))));
  });
  table.append(body);
  box.append(table);
  fillSnapshotPicker();
}

async function deleteSnapshot(snapshot) {
  if (!confirm(`Delete snapshot ${snapshot.short_id} from ${snapshot.when}?\n\n` +
               `Space is only reclaimed when you run Prune.`)) return;
  try {
    const result = await post("/api/snapshot/delete", { snapshot: snapshot.id });
    toast(`Deleted ${result.deleted}. ${result.note}`, "good");
    loadSnapshots(); loadStatus();
  } catch (err) { toast(err.message, "bad"); }
}

/* ---------- browse ---------- */

function fillSnapshotPicker() {
  const select = $("#browse-snapshot");
  const current = select.value;
  clear(select);
  state.snapshots.forEach((s) =>
    select.append(el("option", { value: s.id }, `${s.short_id}  ·  ${s.when}`)));
  if (current && state.snapshots.some((s) => s.id === current)) select.value = current;
  else if (state.snapshots.length) state.browse.snapshot = state.snapshots[0].id;
}

async function initBrowse() {
  if (!state.snapshots.length) await loadSnapshots();
  fillSnapshotPicker();
  const select = $("#browse-snapshot");
  if (select.value) state.browse.snapshot = select.value;
  if (!state.browse.loadedOnce) {
    // Start at "/" and let the server point us at the snapshot's root; the
    // configured roots may be written with different separators.
    state.browse.path = "/";
    state.browse.loadedOnce = true;
  }
  loadTree();
}

function browseSnapshot(id) {
  state.browse.snapshot = id;
  $$(".tab").find((t) => t.dataset.tab === "browse").click();
  $("#browse-snapshot").value = id;
  loadTree();
}

async function loadTree() {
  const list = $("#browse-list");
  clear(list);
  list.append(el("div", { class: "empty", text: "Loading…" }));
  let data;
  try {
    data = await api(withProfile(
      `/api/tree?snapshot=${encodeURIComponent(state.browse.snapshot)}` +
      `&path=${encodeURIComponent(state.browse.path)}`));
  } catch (err) {
    clear(list); list.append(el("div", { class: "banner bad", text: err.message })); return;
  }
  // The server may redirect an empty "/" to the snapshot's own root.
  state.browse.path = data.path || state.browse.path;
  state.browse.entries = data.entries;
  state.browse.searching = false;
  renderCrumbs();
  renderList(data.entries);
}

async function runSearch(query) {
  const list = $("#browse-list");
  clear(list);
  list.append(el("div", { class: "empty", text: "Searching…" }));
  try {
    const data = await api(withProfile(
      `/api/search?snapshot=${encodeURIComponent(state.browse.snapshot)}&q=${encodeURIComponent(query)}`));
    state.browse.searching = true;
    renderCrumbs(`${data.entries.length} match(es) for “${query}”`);
    renderList(data.entries, true);
  } catch (err) {
    clear(list); list.append(el("div", { class: "banner bad", text: err.message }));
  }
}

function renderCrumbs(note) {
  const box = $("#browse-crumbs");
  clear(box);
  if (note) { box.append(el("span", { text: note })); return; }
  const parts = state.browse.path.split("/").filter(Boolean);
  box.append(el("button", { onclick: () => { state.browse.path = "/"; loadTree(); } }, "/"));
  let acc = "";
  parts.forEach((part, i) => {
    acc += "/" + part;
    const target = acc;
    box.append(el("span", { text: "/" }));
    if (i === parts.length - 1) box.append(el("span", { text: part }));
    else box.append(el("button", { onclick: () => { state.browse.path = target; loadTree(); } }, part));
  });
}

function renderList(entries, flat = false) {
  const list = $("#browse-list");
  clear(list);
  if (!entries.length) { list.append(el("div", { class: "empty", text: "Empty." })); return; }

  if (!flat && state.browse.path !== "/") {
    const parent = state.browse.path.replace(/\/[^/]+$/, "") || "/";
    list.append(el("div", { class: "frow" },
      el("span", { text: "↩" }),
      el("span", { class: "name dir", onclick: () => { state.browse.path = parent; loadTree(); } }, ".."),
      el("span", {}), el("span", {})));
  }

  entries.forEach((entry) => {
    const isDir = entry.kind === "d";
    const box = el("input", {
      type: "checkbox",
      checked: state.selection.has(entry.path),
      onchange: (ev) => {
        if (ev.target.checked) state.selection.add(entry.path);
        else state.selection.delete(entry.path);
        renderSelection();
      },
    });
    const name = el("span", {
      class: `name${isDir ? " dir" : ""}`,
      title: entry.path,
      onclick: isDir ? () => { state.browse.path = entry.path; loadTree(); } : null,
    }, (isDir ? "📁 " : entry.kind === "l" ? "🔗 " : "📄 ") + (flat ? entry.path : entry.name));
    list.append(el("div", { class: "frow" }, box, name,
      el("span", { class: "size", text: isDir ? "" : bytes(entry.size) }),
      el("span", { class: "when", text: entry.mtime ? when(entry.mtime).split(",")[0] : "" })));
  });
}

function renderSelection() {
  const box = $("#restore-selection");
  clear(box);
  if (!state.selection.size) {
    box.append(document.createTextNode("Nothing selected — the whole snapshot will be restored."));
    return;
  }
  box.append(el("span", { text: `${state.selection.size} selected: ` }));
  Array.from(state.selection).slice(0, 40).forEach((path) => {
    box.append(el("span", { class: "chip" }, path,
      el("button", { title: "remove", onclick: () => { state.selection.delete(path); renderSelection(); loadTreeSoft(); } }, "×")));
  });
  if (state.selection.size > 40) box.append(el("span", { text: ` …and ${state.selection.size - 40} more` }));
}

function loadTreeSoft() {
  if (!state.browse.searching) loadTree();
}

/* ---------- settings ---------- */

const SETTING_FIELDS = [
  ["repository", "Repository (NAS path)", "text"],
  ["source", "Source", "select", ["adb", "local"]],
  ["roots", "Paths to back up (comma separated)", "text"],
  ["excludes", "Extra excludes (comma separated)", "text"],
  ["use_default_excludes", "Skip caches and thumbnails", "bool"],
  ["tags", "Tags for new snapshots", "text"],
  ["device", "adb device (serial or host:port)", "text"],
  ["use_root", "Use root (su) on the phone", "bool"],
  ["batch_read", "Batch file reads (much faster)", "bool"],
  ["smb_username", "SMB username", "text"],
  ["smb_password", "SMB password", "text"],
  ["prune_after_forget", "Prune automatically after retention", "bool"],
];

async function loadSettings() {
  let data;
  try { data = await api(withProfile("/api/config")); }
  catch (err) { toast(err.message, "bad"); return; }

  const form = $("#settings-form");
  clear(form);
  SETTING_FIELDS.forEach(([key, label, type, options]) => {
    let input;
    const value = data.settings[key];
    if (type === "bool") {
      input = el("input", { type: "checkbox", id: `set-${key}`, checked: !!value });
      form.append(el("label", { class: "check" }, input, label));
      return;
    }
    if (type === "select") {
      input = el("select", { id: `set-${key}` });
      options.forEach((o) => input.append(el("option", { value: o, selected: o === value }, o)));
    } else {
      input = el("input", {
        type: key === "smb_password" ? "password" : "text",
        id: `set-${key}`,
        value: Array.isArray(value) ? value.join(", ") : (value ?? ""),
      });
    }
    form.append(el("label", { class: "field" }, el("span", { text: label }), input));
  });

  $("#settings-path").textContent = `Stored in ${data.path}`;
  $("#schedule-spec").value = data.settings.schedule || "";

  renderRetention(data.settings.retention);
}

async function loadRetention() {
  try {
    const data = await api(withProfile("/api/config"));
    renderRetention(data.settings.retention);
  } catch (err) { toast(err.message, "bad"); }
}

function renderRetention(retention) {
  const keep = $("#keep-grid");
  clear(keep);
  [["last", "Last"], ["hourly", "Hourly"], ["daily", "Daily"],
   ["weekly", "Weekly"], ["monthly", "Monthly"], ["yearly", "Yearly"]].forEach(([key, label]) => {
    keep.append(el("label", { class: "field" }, el("span", { text: label }),
      el("input", { type: "number", min: "0", id: `keep-${key}`,
                    value: (retention && retention[key]) ?? "" })));
  });
}

async function saveSettings() {
  const settings = {};
  SETTING_FIELDS.forEach(([key, , type]) => {
    const node = $(`#set-${key}`);
    if (!node) return;
    settings[key] = type === "bool" ? String(node.checked) : node.value;
  });
  try {
    const result = await post("/api/config", { settings });
    toast(`Saved ${result.changed.length} setting(s).`, "good");
    loadStatus();
  } catch (err) { toast(err.message, "bad"); }
}

async function loadDevices() {
  const box = $("#device-list");
  clear(box);
  box.append(el("div", { class: "empty", text: "Looking for devices…" }));
  let data;
  try { data = await api(withProfile("/api/devices")); }
  catch (err) { clear(box); box.append(el("div", { class: "banner bad", text: err.message })); return; }

  clear(box);
  if (data.error) { box.append(el("div", { class: "banner bad", text: data.error })); return; }
  box.append(el("p", { class: "hint", text: `adb: ${data.adb}` }));
  if (!data.devices.length) {
    box.append(el("div", { class: "banner warn", text:
      "No devices. On the phone: enable Developer options, turn on USB debugging, " +
      "connect by USB, and accept the authorisation prompt." }));
    return;
  }
  data.devices.forEach((device) => {
    const lines = [el("strong", { text: device.description || device.serial }),
                   el("div", { class: "hint", text: `${device.serial} · ${device.state}` })];
    if (device.state === "unauthorized") {
      lines.push(el("div", { class: "banner warn", text:
        "Unlock the phone and accept “Allow USB debugging”." }));
    }
    if (device.root !== undefined) {
      lines.push(el("div", { class: device.root ? "good" : "warnrow",
        text: device.root ? "root available" : "no root — /data will not be readable" }));
    }
    (device.storage || []).forEach((row) => lines.push(el("div", { class: "hint",
      text: `${row.mounted}: ${row.used} used of ${row.size} (${row.available} free)` })));
    box.append(el("div", { class: "card" }, ...lines));
  });
  updateDevicePill(data);
}

function updateDevicePill(data) {
  const pill = $("#device-pill");
  if (!data) {
    if (state.status && state.status.source !== "adb") {
      pill.textContent = "source: local filesystem";
      pill.className = "pill";
    }
    return;
  }
  const ready = (data.devices || []).find((d) => d.state === "device");
  // Remember what the phone says about itself so the summary can show the
  // current device rather than the one recorded in the last snapshot.
  const seen = ready && ready.description ? ready.description.split(" [")[0] : null;
  if (seen !== state.device) {
    state.device = seen;
    loadStatus();   // re-renders the summary; takes the no-data path back here
  }
  if (ready) {
    pill.textContent = `phone: ${ready.description || ready.serial}${ready.root ? " · root" : ""}`;
    pill.className = "pill ok";
  } else {
    pill.textContent = data.devices.length ? `phone: ${data.devices[0].state}` : "phone: not connected";
    pill.className = "pill bad";
  }
}

/* ---------- actions ---------- */

$("#btn-backup").addEventListener("click", async () => {
  try {
    await post("/api/backup", {
      dry_run: $("#opt-dry-run").checked,
      rescan: $("#opt-rescan").checked,
      forget: $("#opt-forget").checked,
    });
    toast("Backup started.", "good");
    state.lastRunning = true;
    $("#btn-pause").disabled = false;
  } catch (err) { toast(err.message, "bad"); }
});

$("#btn-pause").addEventListener("click", async () => {
  try {
    const result = await post("/api/job/pause");
    toast(result.note, "good");
    $("#btn-pause").disabled = true;
  } catch (err) { toast(err.message, "bad"); }
});

$("#btn-restore").addEventListener("click", async () => {
  const target = $("#restore-target").value.trim();
  if (!target) { toast("Choose a destination folder first.", "bad"); return; }
  const toPhone = $("#restore-to-phone").checked;
  const count = state.selection.size;
  const what = count ? `${count} selected item(s)` : "the entire snapshot";
  if (!confirm(`Restore ${what} to ${toPhone ? "the phone at " : ""}${target}?`)) return;
  try {
    await post("/api/restore", {
      snapshot: state.browse.snapshot,
      include: Array.from(state.selection),
      target,
      to_phone: toPhone,
      overwrite: $("#restore-overwrite").value,
    });
    toast("Restore started — see progress on the Overview tab.", "good");
    state.lastRunning = true;
  } catch (err) { toast(err.message, "bad"); }
});

$("#btn-forget").addEventListener("click", async () => {
  const body = { action: "forget", dry_run: $("#forget-dry").checked, prune: $("#forget-prune").checked };
  ["last", "hourly", "daily", "weekly", "monthly", "yearly"].forEach((key) => {
    const value = $(`#keep-${key}`).value.trim();
    if (value !== "") body[`keep_${key}`] = value;
  });
  await runMaintenance(body);
});

async function runMaintenance(body, target = "#maint-result") {
  try {
    await post("/api/maintenance", body);
    toast(`${body.action} started.`, "good");
    state.lastRunning = true;
    pollMaintenance(target);
  } catch (err) { toast(err.message, "bad"); }
}

function pollMaintenance(target = "#maint-result") {
  const box = $(target);
  clear(box);
  box.append(el("div", { class: "empty", text: "Running…" }));
  const timer = setInterval(async () => {
    let data;
    try { data = await api("/api/job"); } catch { return; }
    const job = data.current;
    if (!job || job.state === "running") return;
    clear(box);
    if (job.error) box.append(el("div", { class: "banner bad", text: job.error }));
    else if (job.result) box.append(renderResult(job.kind, job.result));
    if (job.result && job.result.problems && job.result.problems.length) {
      const log = el("div", { class: "log" });
      job.result.problems.slice(0, 200).forEach((p) => log.append(el("div", { text: p })));
      box.append(log);
    }
    clearInterval(timer);
    loadStatus(); loadSnapshots();
  }, 900);
}

$("#btn-repair").addEventListener("click", () => runMaintenance({ action: "repair" }, "#repair-result"));
$("#btn-repair-discard").addEventListener("click", () => {
  if (confirm(
    "This removes things that cannot be recovered:\n" +
    "  • index entries whose pack file is gone from the NAS\n" +
    "  • pack files whose contents cannot be located\n" +
    "  • leftovers from an interrupted write\n\n" +
    "It does not bring any data back. Run Repair first to see what would go.\n\nContinue?")) {
    runMaintenance({ action: "repair", discard_damaged: true }, "#repair-result");
  }
});

$("#btn-check").addEventListener("click", () => runMaintenance({ action: "check" }));
$("#btn-check-deep").addEventListener("click", () => runMaintenance({ action: "check", read_data: true }));
$("#btn-prune-dry").addEventListener("click", () => runMaintenance({ action: "prune", dry_run: true }));
$("#btn-prune").addEventListener("click", () => {
  if (confirm("Prune permanently deletes data that no snapshot references.\n\nContinue?")) {
    runMaintenance({ action: "prune" });
  }
});
$("#btn-unlock").addEventListener("click", () => {
  if (confirm("Break repository locks?\n\nOnly do this if no backup or prune is running.")) {
    runMaintenance({ action: "unlock" });
  }
});

$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-refresh-devices").addEventListener("click", loadDevices);
$("#btn-refresh-snapshots").addEventListener("click", loadSnapshots);

$("#btn-schedule").addEventListener("click", async () => {
  try {
    const result = await post("/api/schedule", { schedule: $("#schedule-spec").value.trim() });
    toast(result.description ? `Scheduled: ${result.description}` : "Schedule updated.", "good");
    $("#schedule-result").textContent = result.message || "";
  } catch (err) {
    toast(err.message, "bad");
    $("#schedule-result").textContent = err.message;
  }
});

$("#btn-schedule-off").addEventListener("click", async () => {
  $("#schedule-spec").value = "";
  try {
    const result = await post("/api/schedule", { schedule: "" });
    toast("Schedule removed.", "good");
    $("#schedule-result").textContent = result.message || "";
  } catch (err) { $("#schedule-result").textContent = err.message; }
});

$("#profile-select").addEventListener("change", (ev) => {
  state.profile = ev.target.value;
  state.selection.clear();
  state.browse.loadedOnce = false;
  loadStatus(); loadSnapshots(); renderSelection();
});

$("#browse-snapshot").addEventListener("change", (ev) => {
  state.browse.snapshot = ev.target.value;
  loadTree();
});

let searchTimer = null;
$("#browse-search").addEventListener("input", (ev) => {
  const query = ev.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => (query.length >= 2 ? runSearch(query) : loadTree()), 320);
});

/* ---------- boot ---------- */

loadStatus();
loadHistory();
renderSelection();
startPolling();
