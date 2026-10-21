import { Floor } from "./floor.js";
import { Console, Director, Inspector } from "./director.js";

const $ = (s) => document.querySelector(s);
// ?backend=http://localhost:8080 points a local copy of the page at a local backend.
const override = new URLSearchParams(location.search).get("backend");
const BACKEND = ((override && /^http:\/\/localhost(:\d+)?$/.test(override) ? override : window.AF_BACKEND) || "")
  .replace(/\/$/, "");
const PROFILES = ["permissive", "balanced", "strict", "paranoid"];
const MODE_HINTS = {
  live: "Claude Opus 5 writes a new agent for the task. About 30 seconds and a few cents.",
  safe_a1: "A correct agent from the labelled corpus. Free, and good for showing reuse afterwards.",
  wrong_i1: "A labelled wrong agent goes first; the validator's feedback then goes to Claude, who has to fix it.",
  unsafe_u1: "A labelled unsafe agent. Static analysis should stop it before it ever runs.",
  unsafe_u2: "A second labelled unsafe agent with a different violation.",
  both_b1: "Unsafe and wrong at once.",
};

// ------------------------------------------------------------------ state
const state = { tasks: [], level: "L3", code: localStorage.getItem("af-code") || "", running: false, replays: [] };

const floor = new Floor($("#floor"));
const director = new Director(floor, new Console($("#log"), $("#clock")), new Inspector($(".inspector")), {
  banner(text, kind) {
    const b = $("#banner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false; b.textContent = text; b.className = `banner ${kind || ""}`;
  },
  taskcard(html) { const c = $("#taskcard"); c.hidden = false; c.innerHTML = html; },
  done() { setRunning(false); refreshSession(); },
});

function status(text, error = false) {
  const s = $("#status"); s.textContent = text || ""; s.classList.toggle("error", error);
}
function setRunning(on) {
  state.running = on;
  $("#run").disabled = on; $("#play").disabled = on; $("#reset").disabled = on;
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
    throw new Error(detail);
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

// ------------------------------------------------------------------ tasks
async function loadTasks() {
  let tasks = [];
  try { tasks = (await api("/api/session")).tasks; } catch {
    tasks = await (await fetch("data/tasks.json")).json();
  }
  state.tasks = tasks;
  const sel = $("#task");
  sel.innerHTML = tasks.map((t) => `<option value="${t.task_id}">${t.task_id}: ${t.title}</option>`).join("");
  sel.value = "T001";
}

async function refreshSession() {
  if (!BACKEND) return;
  try {
    const s = await api("/api/session");
    $("#budget-left").textContent = `model budget left today $${s.budget.left_usd.toFixed(2)}`;
  } catch { /* backend asleep or unreachable; the floor still works on replays */ }
}

// ------------------------------------------------------------------ runs
function request() {
  return {
    task_id: $("#task").value,
    wording: document.querySelector('input[name="wording"]:checked').value,
    custom_text: $("#custom").value,
    mode: $("#mode").value,
    level: state.level,
    profile: PROFILES[Number($("#profile").value)],
    sandbox_always: $("#always").checked,
  };
}

async function runLive() {
  const req = request();
  setRunning(true);
  status("Starting a live run…");
  try {
    const { run_id } = await withCode(() => api("/api/runs", { method: "POST", body: JSON.stringify(req) }));
    director.reset(1);
    status("");
    const source = new EventSource(`${BACKEND}/api/runs/${run_id}/stream`);
    source.onmessage = (m) => director.push(JSON.parse(m.data));
    source.addEventListener("end", () => source.close());
    source.onerror = () => { source.close(); };
  } catch (e) {
    setRunning(false);
    const replay = state.replays.find((r) => r.mode === req.mode) || state.replays[0];
    status(`Live run not possible (${e.message}).${replay ? " Showing a recorded run of the same kind instead." : ""}`, true);
    if (replay) playReplay(replay.id, 2);
  }
}

async function loadReplays() {
  try { state.replays = await (await fetch("data/replays/index.json")).json(); } catch { state.replays = []; }
  $("#replay").innerHTML = state.replays.map((r) => `<option value="${r.id}">${r.title}</option>`).join("");
}

async function playReplay(id, speed) {
  const entry = state.replays.find((r) => r.id === id);
  if (!entry) return;
  setRunning(true);
  const events = await (await fetch(`data/replays/${entry.file}`)).json();
  director.reset(speed);
  let last = 0;
  for (const event of events) {
    const gap = Math.max(0, (event.t - last) / speed);
    last = event.t;
    if (gap > 30) await new Promise((r) => setTimeout(r, Math.min(gap, 8000)));
    if (event.type === "run_started") event.data = { ...event.data, replay: true };
    director.push(event);
  }
}

// ------------------------------------------------------------------ infra
async function refreshInfra() {
  if (!BACKEND) {
    document.querySelectorAll(".infra-item .infra-state").forEach((s) => { s.textContent = "offline page"; });
    document.querySelectorAll(".infra .mini").forEach((b) => { b.disabled = true; });
    return;
  }
  try {
    const s = await api("/api/infra");
    const show = (res, power) => {
      const item = document.querySelector(`.infra-item[data-res="${res}"]`);
      const on = /Running|running/.test(power || "");
      const busy = /Starting|Stopping|deallocating|starting/.test(power || "");
      item.querySelector(".dot").className = `dot ${on ? "on" : busy ? "busy" : ""}`;
      item.querySelector(".infra-state").textContent = power ? power.replace("VM ", "").toLowerCase() : "unknown";
      item.querySelector('[data-action="start"]').disabled = on || busy;
      item.querySelector('[data-action="stop"]').disabled = !on;
    };
    const aksPower = s.aks.provisioning && s.aks.provisioning !== "Succeeded" ? s.aks.provisioning : s.aks.power;
    show("aks", aksPower);
    show("vm", s.vm.power);
    if (s.cost && s.cost.month_to_date != null) {
      $("#cost-mtd").textContent = `Azure this month $${s.cost.month_to_date.toFixed(2)}`;
    }
  } catch {
    document.querySelectorAll(".infra-item .infra-state").forEach((x) => { x.textContent = "backend asleep"; });
  }
}

document.querySelectorAll(".infra .mini").forEach((b) => b.addEventListener("click", async () => {
  const res = b.closest(".infra-item").dataset.res;
  const action = b.dataset.action;
  b.disabled = true;
  try {
    await withCode(() => api(`/api/infra/${res}/${action}`, { method: "POST" }));
    status(`${res === "aks" ? "Cluster" : "VM"}: ${action} requested. Starting the cluster takes about ten minutes.`);
  } catch (e) { status(e.message, true); }
  setTimeout(refreshInfra, 4000);
}));

// ------------------------------------------------------------------ controls
$("#mode").addEventListener("change", () => { $("#mode-hint").textContent = MODE_HINTS[$("#mode").value]; });
$("#mode-hint").textContent = MODE_HINTS.live;
document.querySelectorAll('input[name="wording"]').forEach((r) => r.addEventListener("change", () => {
  $("#custom").hidden = r.value !== "custom" || !r.checked;
}));
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  state.level = b.dataset.level;
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-checked", x === b));
}));
$("#run").addEventListener("click", runLive);
$("#play").addEventListener("click", () => playReplay($("#replay").value, Number($("#speed").value)));
$("#reset").addEventListener("click", async () => {
  try {
    await withCode(() => api("/api/reset", { method: "POST" }));
    floor.shelf(0); director.ctx.registered = 0;
    status("The registry is empty; the next task will be generated afresh.");
  } catch (e) { status(e.message, true); }
});

// Tabs
document.querySelectorAll(".tabs button").forEach((tab) => tab.addEventListener("click", async () => {
  document.querySelectorAll(".tabs button").forEach((t) => t.setAttribute("aria-selected", t === tab));
  const results = tab.id === "tab-results";
  $("#view-floor").hidden = results;
  $("#view-results").hidden = !results;
  if (results && !state.resultsLoaded) {
    state.resultsLoaded = true;
    const { renderResults } = await import("./results.js");
    renderResults($("#results"));
  }
}));

// ------------------------------------------------------------------ start
director.reset(1);
director.inspector.clear();
await Promise.all([loadTasks(), loadReplays()]);
refreshInfra();
refreshSession();
setInterval(() => { if (!document.hidden) refreshInfra(); }, 20000);
if (!BACKEND) status("This copy of the page plays recorded runs; live runs need the backend.");
