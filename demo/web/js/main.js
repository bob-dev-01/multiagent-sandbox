import { Floor, ROOMS } from "./floor.js?v=4";
import { Fleet, Inspector, ROOM_TEXT, esc } from "./director.js?v=4";

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// ?backend=http://localhost:8080 points a local copy of the page at a local backend.
const override = new URLSearchParams(location.search).get("backend");
const BACKEND = ((override && /^http:\/\/localhost(:\d+)?$/.test(override) ? override : window.AF_BACKEND) || "")
  .replace(/\/$/, "");

const SCENARIOS = [
  { id: "live", mode: "live", name: "A new agent, written live", cost: "about 40 s, a few cents",
    hint: "Claude Opus 5 writes an agent for the task; watch it pass every room.", button: "Run live" },
  { id: "unsafe", mode: "unsafe_u1", name: "An unsafe agent is stopped", cost: "free",
    hint: "A labelled unsafe agent from the corpus. The checks walk it to quarantine.", button: "Send the unsafe agent" },
  { id: "repair", mode: "wrong_i1", name: "A wrong agent is repaired", cost: "a few cents",
    hint: "A labelled wrong agent fails; the findings go back to Claude to fix.", button: "Run the repair" },
  { id: "reuse", mode: "safe_a1", name: "Reuse from the archive", cost: "free",
    hint: "The same task twice: the second agent is served from the archive.", button: "Run it twice" },
  { id: "batch", mode: null, name: "Several tasks at once", cost: "a few cents per live task",
    hint: "Up to 15 random tasks walk the floor side by side. Click any agent to follow it.", button: "Run the batch" },
];
const OUTCOME_STATE = {
  generated_and_deployed: "pass", reused: "reused", escalated_unsafe: "unsafe",
  escalated_incorrect: "wrong", deployment_refused: "wrong", demo_error: "wrong",
};
const STATE_COLOUR = { pass: "var(--pass)", reused: "#5fae82", unsafe: "var(--unsafe)", wrong: "var(--wrong)",
  running: "var(--active)", queued: "#dfe5e3", skipped: "#dfe5e3" };

// ------------------------------------------------------------------ state
const state = {
  tasks: [], scenario: "live", level: "L3", profile: "strict",
  code: (() => { try { return localStorage.getItem("af-code") || ""; } catch { return ""; } })(),
  running: false, replays: [], batch: null, cluster: null, starting: false, backendDone: true,
};

const inspector = new Inspector($(".inspector"));
const floor = new Floor($("#floor"), { onPick: (key) => fleet.follow(key) });
const fleet = new Fleet(floor, inspector, {
  header: renderHeader,
  detail: renderDetail,
  onFinished: () => refreshSession(),
  onIdle: () => { if (state.backendDone) setRunning(false); },
  followChanged: () => { if (state.batch?.view) renderBatch(state.batch.view); },
});

function status(text, error = false) {
  const s = $("#status"); s.textContent = text || ""; s.classList.toggle("error", error);
}
function setRunning(on) {
  state.running = on;
  $("#play").disabled = on;
  $("#reset").disabled = on;
  $("#batch-stop").hidden = !(on && state.batch);
  updateRunButton();
}

// ------------------------------------------------------------------ backend
async function api(path, opts = {}) {
  if (!BACKEND) throw new Error("no live backend is configured for this page");
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.code) headers["X-Demo-Code"] = state.code;
  const res = await fetch(`${BACKEND}${path}`, { ...opts, headers });
  if (res.status === 401) { const e = new Error("access code needed"); e.auth = true; throw e; }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* plain text */ }
    const e = new Error(detail); e.status = res.status; throw e;
  }
  return res.json();
}

// The dialog is driven by its form's submit event rather than the dialog's
// close event, which some embedded browsers never deliver.
function askCode() {
  return new Promise((resolve) => {
    const dlg = $("#code-dialog");
    const form = dlg.querySelector("form");
    $("#code-input").value = "";
    $("#code-status").textContent = "";
    dlg.showModal();
    const onSubmit = async (event) => {
      event.preventDefault();
      form.removeEventListener("submit", onSubmit);
      const ok = event.submitter && event.submitter.value === "ok";
      dlg.close();
      if (!ok) return resolve(false);
      state.code = $("#code-input").value.trim();
      try {
        await api("/api/auth", { method: "POST" });
        try { localStorage.setItem("af-code", state.code); } catch { /* storage may be blocked */ }
        resolve(true);
      } catch { state.code = ""; status("That access code was not accepted.", true); resolve(false); }
    };
    form.addEventListener("submit", onSubmit);
  });
}

async function withCode(fn) {
  try { return await fn(); } catch (e) {
    if (e.auth && await askCode()) return fn();
    throw e;
  }
}

function stream(runId, key, label) {
  return new Promise((resolve) => {
    const source = new EventSource(`${BACKEND}/api/runs/${runId}/stream`);
    source.onmessage = (m) => fleet.push(key, label, JSON.parse(m.data));
    source.addEventListener("end", () => { source.close(); resolve(); });
    source.onerror = () => { source.close(); resolve(); };
  });
}

// ------------------------------------------------------------------ header and detail
function renderHeader(track) {
  if (!track) {
    $("#run-over").textContent = state.batch ? "Batch starting" : "Nothing running yet";
    $("#run-title").textContent = state.batch ? "Agents are lining up at the intake" : "Pick a scenario and press Run";
    setPill("idle", "Idle");
    $("#clock").textContent = "0.0 s";
    return;
  }
  const i = track.info;
  const many = fleet.tracks.size > 1;
  $("#run-over").innerHTML = many
    ? `Following <b>${esc(track.label)}</b>; click any agent on the floor to follow it`
    : `<b>${esc(i.taskId || track.label)}</b>${i.replay ? ", recorded run" : ""}`;
  $("#run-title").textContent = i.title || "Waiting at the intake";
  const labels = { queued: "Waiting", working: many ? `${countWorking()} at work` : (i.replay ? "Recorded run" : "Live run"),
    pass: "Passed", unsafe: "Unsafe", wrong: "Wrong" };
  const kind = track.state === "working" ? "active" : track.state === "queued" ? "idle" : track.state;
  setPill(kind, track.done ? `${labels[track.state]}: ${(i.outcome || "").replaceAll("_", " ")}` : labels[track.state]);
  if (i.level) $("#run-level").textContent = i.level === "L3" ? "L3 gVisor" : "L2 runc";
  if (i.profile) $("#run-profile").textContent = `${i.profile} profile`;
  $("#clock").textContent = `${(track.t / 1000).toFixed(1)} s`;
}
function countWorking() { return [...fleet.tracks.values()].filter((t) => t.state === "working").length; }
function setPill(kind, text) {
  $("#run-state").className = `pill state ${kind}`;
  $("#run-state-text").textContent = text;
}

function renderDetail(track) {
  const box = $("#detail");
  if (!track) {
    box.className = "card detail";
    $("#detail-over").textContent = "The floor";
    $("#detail-title").textContent = "Ten rooms, walked in order";
    $("#detail-text").textContent = "Each task becomes an agent that walks from the intake through every check. An agent found in the archive skips straight to dispatch; an unsafe one is walked to quarantine and never runs for real.";
    $("#detail-extra").innerHTML = "";
    return;
  }
  const room = floor.roomOf(track.key) || "intake";
  const final = track.done ? track.state : null;
  box.className = `card detail ${final || ""}`;
  $("#detail-over").textContent = `${track.label} is in the ${ROOMS[room].name.toLowerCase()}`;
  $("#detail-title").textContent = ROOMS[room].name;
  $("#detail-text").textContent = ROOM_TEXT[room];
  const facts = Object.entries(track.info.facts)
    .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
  const stamp = final ? `<span class="stamp ${final}">${final === "pass" ? "PASSED" : final === "unsafe" ? "UNSAFE" : "WRONG"}</span>` : "";
  $("#detail-extra").innerHTML = `${stamp}${facts ? `<dl class="facts">${facts}</dl>` : ""}`;
}

// ------------------------------------------------------------------ scenarios and controls
function renderScenarios() {
  $("#scenarios").innerHTML = SCENARIOS.map((s) => `<button class="scenario" role="radio" aria-checked="${s.id === state.scenario}" data-id="${s.id}">
      <span class="scenario-top"><span class="scenario-name">${s.name}</span><span class="scenario-cost">${s.cost}</span></span>
      <span class="scenario-hint">${s.hint}</span></button>`).join("");
  $("#scenarios").querySelectorAll(".scenario").forEach((b) => b.addEventListener("click", () => {
    state.scenario = b.dataset.id;
    renderScenarios();
  }));
  const batch = state.scenario === "batch";
  $("#batch-opts").hidden = !batch;
  $("#task-field").hidden = batch;
  updateRunButton();
}

function updateRunButton() {
  const s = SCENARIOS.find((x) => x.id === state.scenario);
  const n = $("#batch-size").value;
  $("#run").textContent = s.id === "batch" ? `Run ${n} tasks` : s.button;
  // A local backend (?backend=) runs its sandboxes locally, not on the cluster.
  const ready = !BACKEND || BACKEND.startsWith("http://localhost") || !state.cluster || state.cluster === "Running";
  $("#run").disabled = state.running || !ready;
}

function request(mode, taskId) {
  return {
    task_id: taskId || $("#task").value,
    wording: $("#wording").value,
    custom_text: $("#custom").value,
    mode,
    level: state.level,
    profile: state.profile,
    sandbox_always: $("#always").checked,
  };
}

async function run() {
  const s = SCENARIOS.find((x) => x.id === state.scenario);
  status("");
  try {
    if (s.id === "batch") {
      const size = Number($("#batch-size").value);
      const ids = pickTasks(size);
      await startBatch(ids, Number($("#batch-parallel").value), $("#batch-mode").value);
    } else if (s.id === "reuse") {
      const id = $("#task").value;
      await startBatch([id, id], 1, "safe_a1");
    } else {
      await startSingle(request(s.mode));
    }
  } catch (e) {
    setRunning(false);
    state.batch = null;
    if (e.status === 409) {
      status("Someone else's run is in progress; you are now watching it.");
      joinActive();
      return;
    }
    const replay = state.replays.find((r) => r.mode === s.mode) || state.replays[0];
    status(`Live run not possible (${e.message}).${replay ? " Showing a recorded run of the same kind instead." : ""}`, true);
    if (replay) playReplay(replay.id, 2);
  }
}

async function startSingle(req) {
  setRunning(true);
  const { run_id } = await withCode(() => api("/api/runs", { method: "POST", body: JSON.stringify(req) }));
  watchSingle(run_id, req.task_id);
}

function watchSingle(runId, label) {
  state.batch = null;
  state.backendDone = false;
  setRunning(true);
  fleet.reset(1);
  $("#batchstrip").hidden = true;
  stream(runId, runId, label || "agent").then(() => { state.backendDone = true; if (!fleet.busy()) setRunning(false); });
}

function pickTasks(n) {
  const ids = state.tasks.map((t) => t.task_id);
  for (let i = ids.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [ids[i], ids[j]] = [ids[j], ids[i]];
  }
  return ids.slice(0, Math.min(n, ids.length));
}

async function startBatch(taskIds, parallel, mode) {
  setRunning(true);
  const req = request(mode, taskIds[0]);
  if (req.wording === "custom") req.wording = "original";
  const { batch_id } = await withCode(() => api("/api/batches", {
    method: "POST", body: JSON.stringify({ ...req, task_ids: taskIds, parallel }),
  }));
  watchBatch(batch_id);
}

// Polls the batch for the tally and tiles, and opens a stream for every task
// as soon as it starts, so running tasks walk the floor side by side.
async function watchBatch(batchId) {
  const b = { id: batchId, view: null, streams: new Set() };
  state.batch = b;
  state.backendDone = false;
  setRunning(true);
  fleet.reset(1.4);
  $("#batchstrip").hidden = false;
  inspectorPane("batch");
  const labels = [];
  const pending = [];
  while (state.batch === b) {
    try { b.view = await api(`/api/batches/${batchId}`); } catch { await sleep(2000); continue; }
    if (!labels.length) {
      const seen = {};
      b.view.items.forEach((it, i) => {
        seen[it.task_id] = (seen[it.task_id] || 0) + 1;
        labels[i] = seen[it.task_id] > 1 ? `${it.task_id} #${seen[it.task_id]}` : it.task_id;
        fleet.track(`item-${i}`, labels[i]);
      });
    }
    b.view.items.forEach((it, i) => {
      if (it.run_id && !b.streams.has(i)) {
        b.streams.add(i);
        pending.push(stream(it.run_id, `item-${i}`, labels[i]));
      }
    });
    renderBatch(b.view);
    if (b.view.done) break;
    await sleep(1500);
  }
  await Promise.all(pending);
  state.backendDone = true;
  refreshSession();
  if (!fleet.busy()) setRunning(false);
}

function renderBatch(view) {
  const titles = Object.fromEntries(state.tasks.map((t) => [t.task_id, t.title]));
  const kinds = view.items.map((it) => it.status === "done" ? (OUTCOME_STATE[it.outcome] || "wrong") : it.status);
  const count = (k) => kinds.filter((x) => x === k).length;
  const order = ["pass", "reused", "wrong", "unsafe", "running", "queued", "skipped"];
  const names = { pass: "deployed", reused: "reused", wrong: "wrong or refused", unsafe: "unsafe, quarantined",
    running: "running", queued: "queued", skipped: "skipped" };
  $("#batchbar").innerHTML = order.filter((k) => count(k)).map((k) =>
    `<span style="flex: ${count(k)} 1 0; background: ${STATE_COLOUR[k]}"></span>`).join("");
  $("#batchbar").setAttribute("aria-label", order.filter((k) => count(k)).map((k) => `${count(k)} ${names[k]}`).join(", "));
  $("#tally").innerHTML = order.filter((k) => count(k)).map((k) =>
    `<li><i style="background: ${STATE_COLOUR[k]}"></i><b>${count(k)}</b> ${names[k]}</li>`).join("")
    + `<li>${view.parallel} at a time${view.cost_usd ? `, $${view.cost_usd.toFixed(2)} so far` : ""}${view.stopped ? `, ${esc(view.stopped)}` : ""}</li>`;
  const tiles = view.items.map((it, i) => {
    const k = kinds[i];
    const out = it.status === "done" ? it.outcome.replaceAll("_", " ") : it.status;
    const cls = k === "pass" ? "pass" : k;
    return `<li><button class="tile ${cls}${fleet.followed === `item-${i}` ? " followed" : ""}" data-key="item-${i}"
      title="${esc(titles[it.task_id] || "")}${it.reason ? `: ${esc(it.reason)}` : ""}">
      <span class="top"><span>${String(i + 1).padStart(2, "0")}</span><span>${it.seconds != null ? `${it.seconds.toFixed(0)} s` : ""}</span></span>
      <span class="id">${esc(it.task_id)}</span><span class="out">${esc(out)}</span></button></li>`;
  }).join("");
  $("#batch").innerHTML = `<p class="hint">${view.finished} of ${view.total} finished. Select a task to follow it on the floor.</p><ul class="tiles">${tiles}</ul>`;
  $("#batch").querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => fleet.follow(t.dataset.key)));
}

function inspectorPane(name) { inspector.show(name); }

async function joinActive() {
  try {
    const s = await api("/api/session");
    if (!s.active) return false;
    if (s.active.batch_id) watchBatch(s.active.batch_id);
    else if (s.active.run_id) watchSingle(s.active.run_id);
    return true;
  } catch { return false; }
}

// ------------------------------------------------------------------ recordings
async function loadReplays() {
  try { state.replays = await (await fetch("data/replays/index.json")).json(); } catch { state.replays = []; }
  $("#replay").innerHTML = state.replays.map((r) => `<option value="${r.id}">${esc(r.title)}</option>`).join("");
}

async function playReplay(id, speed = 2) {
  const entry = state.replays.find((r) => r.id === id);
  if (!entry) return;
  state.batch = null;
  state.backendDone = false;
  setRunning(true);
  $("#batchstrip").hidden = true;
  const events = await (await fetch(`data/replays/${entry.file}`)).json();
  fleet.reset(speed);
  const label = events.find((e) => e.type === "run_started")?.data.task_id || "agent";
  let last = 0;
  for (const event of events) {
    const gap = Math.max(0, (event.t - last) / speed);
    last = event.t;
    if (gap > 30) await sleep(Math.min(gap, 8000));
    if (event.type === "run_started") event.data = { ...event.data, replay: true };
    fleet.push("replay", label, event);
  }
  state.backendDone = true;
  if (!fleet.busy()) setRunning(false);
}

// ------------------------------------------------------------------ tasks, budget, cluster
async function loadTasks() {
  let tasks = [];
  try { tasks = (await api("/api/session")).tasks; } catch {
    tasks = await (await fetch("data/tasks.json")).json();
  }
  state.tasks = tasks;
  $("#task").innerHTML = tasks.map((t) => `<option value="${t.task_id}">${t.task_id} ${esc(t.title)}</option>`).join("");
  $("#task").value = "T017";
}

async function refreshSession() {
  if (!BACKEND) return null;
  try {
    const s = await api("/api/session");
    const { left_usd: left, daily_usd: daily } = s.budget;
    $("#budget-left").textContent = `$${left.toFixed(2)} left of $${daily.toFixed(0)}`;
    $("#budget-fill").style.width = `${Math.max(0, Math.min(100, (left / daily) * 100))}%`;
    fleet.registered = s.registered;
    floor.shelf(s.registered);
    return s;
  } catch { return null; }
}

async function refreshInfra() {
  const label = $("#cluster-state");
  const dot = $("#cluster .dot");
  if (!BACKEND) {
    label.textContent = "Recordings only on this copy";
    $("#ready-note").textContent = "This copy of the page plays recorded runs; live runs need the backend.";
    return;
  }
  try {
    const s = await api("/api/infra");
    const power = s.aks.power;
    const prov = s.aks.provisioning;
    const busy = prov && !["Succeeded", "Failed"].includes(prov);
    state.cluster = busy ? "Changing" : power;
    if (power === "Running") state.starting = false;
    dot.className = `dot ${power === "Running" && !busy ? "on" : busy || state.starting ? "busy" : ""}`;
    label.textContent = busy || state.starting ? `Sandbox cluster ${power === "Running" ? "stopping" : "starting"}, about 10 minutes`
      : power === "Running" ? "Sandbox cluster running" : "Sandbox cluster stopped";
    $("#cluster-start").hidden = power === "Running" || busy || state.starting;
    $("#cluster-stop").hidden = power !== "Running" || busy;
    $("#ready-note").textContent = power === "Running" && !busy ? ""
      : "Live runs need the sandbox cluster. Start it above (about 10 minutes) or play a recording meanwhile.";
    if (s.cost && s.cost.month_to_date != null) $("#cost-mtd").textContent = `Azure this month $${s.cost.month_to_date.toFixed(2)}`;
  } catch {
    state.cluster = null;
    dot.className = "dot";
    label.textContent = "Backend waking up";
  }
  updateRunButton();
}

async function cluster(action) {
  $(`#cluster-${action}`).disabled = true;
  try {
    await withCode(() => api(`/api/infra/aks/${action}`, { method: "POST" }));
    if (action === "start") state.starting = true;
    status(action === "start" ? "Cluster starting. It takes about ten minutes; play a recording meanwhile." : "Cluster stopping.");
  } catch (e) { status(e.message, true); }
  $(`#cluster-${action}`).disabled = false;
  setTimeout(refreshInfra, 3000);
}

// ------------------------------------------------------------------ wiring
$("#cluster-start").addEventListener("click", () => cluster("start"));
$("#cluster-stop").addEventListener("click", () => cluster("stop"));
$("#run").addEventListener("click", run);
$("#batch-size").addEventListener("change", updateRunButton);
$("#play").addEventListener("click", () => playReplay($("#replay").value, 2));
$("#batch-stop").addEventListener("click", async () => {
  if (!state.batch) return;
  try {
    await withCode(() => api(`/api/batches/${state.batch.id}/stop`, { method: "POST" }));
    status("No new tasks will start; the running ones finish.");
  } catch (e) { status(e.message, true); }
});
$("#reset").addEventListener("click", async () => {
  try {
    await withCode(() => api("/api/reset", { method: "POST" }));
    fleet.registered = 0; floor.shelf(0);
    status("The archive is empty; the next task will be written afresh.");
  } catch (e) { status(e.message, true); }
});
$("#settings-toggle").addEventListener("click", () => {
  const open = $("#settings-toggle").getAttribute("aria-expanded") !== "true";
  $("#settings-toggle").setAttribute("aria-expanded", open);
  $("#settings-body").hidden = !open;
});
$("#level").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
  state.level = b.dataset.level;
  $("#level").querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", x === b));
  $("#chip-level").textContent = state.level === "L3" ? "L3 gVisor" : "L2 runc";
}));
$("#profile").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
  state.profile = b.dataset.profile;
  $("#profile").querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", x === b));
  $("#chip-profile").textContent = state.profile;
}));
$("#wording").addEventListener("change", () => { $("#custom").hidden = $("#wording").value !== "custom"; });

document.querySelectorAll(".tabs button").forEach((tab) => tab.addEventListener("click", async () => {
  document.querySelectorAll(".tabs button").forEach((t) => t.setAttribute("aria-selected", t === tab));
  const results = tab.id === "tab-results";
  $("#view-floor").hidden = results;
  $("#view-results").hidden = !results;
  if (results && !state.resultsLoaded) {
    state.resultsLoaded = true;
    const { renderResults } = await import("./results.js?v=4");
    renderResults($("#results"));
  }
}));

// ------------------------------------------------------------------ start
renderScenarios();
fleet.reset(1);
await Promise.all([loadTasks(), loadReplays()]);
refreshInfra();
const session = await refreshSession();
if (session && session.active) {
  status("A run is already in progress; you are watching it.");
  joinActive();
}
setInterval(() => { if (!document.hidden) refreshInfra(); }, 20000);
