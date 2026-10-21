// Plays runs, live or recorded, on the floor: one track per run, so many
// agents can walk at once. Each track keeps its own console lines and
// inspector contents; the console and inspector show the followed agent, and
// following another agent redraws them from its track. Events are queued per
// track, so a burst (static analysis takes milliseconds) still reads at human
// speed and a slow step (the sandbox takes seconds) holds the agent in its room.

import { ROOMS } from "./floor.js?v=7";

const VERDICT_STATE = { PASS: "pass", FAIL_UNSAFE: "unsafe", FAIL_BOTH: "unsafe", FAIL_INCORRECT: "wrong" };
const PROFILE_NAMES = ["permissive", "balanced", "strict", "paranoid"];
const ROOM_TEXT = {
  intake: "The task has arrived and waits its turn.",
  archive: "The registry is searched for an agent already written for a task like this one. Similarity 0.85 or more means reuse.",
  workshop: "Claude Opus 5 writes a new agent and a contract declaring the tools, CPU, memory and time it needs.",
  scanner: "Static analysis reads the code without running it: forbidden imports and calls, undeclared tools, secrets.",
  desk: "The contract is checked against the enterprise policy: tools, resources, network.",
  lab: "The agent runs in a sandbox pod with no network. A runtime monitor records every process, file and socket it touches.",
  judge: "A calibrated judge (Claude Sonnet 5) scores the output against the task's rubric, 0 to 3.",
  gate: "All the evidence is combined into one verdict under the chosen strictness profile.",
  dispatch: "The approved agent runs the task for real, in a fresh sandbox, and its result is checked.",
  quarantine: "Unsafe: stopped and sent to a person. It never runs for real and is never registered.",
};

export function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function money(v) { return v == null ? "" : `$${Number(v).toFixed(3)}`; }

// ------------------------------------------------------------------ track

class Track {
  constructor(fleet, key, label) {
    this.fleet = fleet;
    this.key = key;
    this.label = label;
    this.queue = [];
    this.playing = false;
    this.log = [];
    this.t = 0;
    this.state = "queued";       // queued | working | pass | unsafe | wrong
    this.info = { title: label, facts: {} };
    this.inspect = { source: "", flags: new Set(), contract: null, meta: null, verdict: null,
      output: undefined, expected: undefined };
    this.done = false;
  }

  push(event) {
    this.queue.push(event);
    if (!this.playing) this.play();
  }

  async play() {
    this.playing = true;
    while (this.queue.length) {
      const event = this.queue.shift();
      try { await this.handle(event); } catch (e) { console.error(e); }
    }
    this.playing = false;
    this.fleet.idleCheck();
  }

  line(e, mark, text, cls = "") {
    this.t = e.t ?? this.t;
    this.log.push({ t: this.t, mark, text, cls });
    this.fleet.changed(this, "log");
  }
  set(state) {
    this.state = state;
    this.fleet.floor.setState(this.key, state);
    this.fleet.changed(this, "state");
  }
  fact(name, value) { this.info.facts[name] = value; this.fleet.changed(this, "detail"); }
  async walk(room) {
    await this.fleet.floor.walk(this.key, room);
    this.fleet.changed(this, "detail");
  }
  say(text) { this.fleet.floor.say(this.key, text); }

  async handle(e) {
    const d = e.data || {};
    const f = this.fleet.floor;
    switch (e.type) {
      case "run_started": {
        Object.assign(this.info, { taskId: d.task_id, title: d.title, description: d.description,
          level: d.level, profile: d.profile, replay: d.replay, batch: d.batch });
        this.inspect.expected = d.expected_output;
        // If the agent being followed has finished, follow this one instead.
        if (this.fleet.tracks.get(this.fleet.followed)?.done) this.fleet.follow(this.key);
        this.fleet.registered = d.registered ?? this.fleet.registered;
        f.shelf(this.fleet.registered);
        this.set("working");
        this.line(e, "▸", `${esc(d.task_id)} (${d.wording} wording), ${d.level}, ${d.profile} profile${d.replay ? ", recorded" : ""}`, "active");
        await f.wait(500);
        break;
      }
      case "scripted_agent":
        this.line(e, "•", `the generator will serve a stored ${esc(d.label).replace("_", " ")} agent (${esc(d.failure_mode || "correct")})`);
        break;
      case "autogen_message":
        this.line(e, "⇄", `AutoGen: ${esc(d.message)} to ${esc(d.to)}`, "autogen");
        break;
      case "registry_lookup":
        await this.walk("archive");
        this.fact("Closest registered agent", d.best_similarity == null ? "none yet" : `similarity ${d.best_similarity}`);
        this.say(d.best_similarity == null ? "archive empty" : `similarity ${d.best_similarity}`);
        this.line(e, "?", d.best_similarity == null ? "registry is empty: generate a new agent"
          : `closest registered agent: similarity ${d.best_similarity} against threshold ${d.tau}, so ${d.decision}`, "active");
        await f.wait(900);
        break;
      case "reused":
        this.set("pass");
        this.say("reused");
        this.line(e, "↺", `reused the agent written for ${esc(d.source_task)} (similarity ${Number(d.similarity).toFixed(2)}): no generation, no validation`, "pass");
        await this.walk("dispatch");
        break;
      case "generation_started":
        await this.walk("workshop");
        this.say(d.regeneration ? `rewriting, attempt ${d.attempt}` : "writing the agent");
        this.line(e, "✎", d.regeneration ? `attempt ${d.attempt}: the model rewrites the agent from the validator's feedback`
          : "the generator writes an agent", "active");
        break;
      case "generation_finished":
        this.say("");
        this.inspect.source = d.source;
        this.inspect.flags = new Set();
        this.inspect.contract = d.contract;
        this.inspect.meta = d;
        this.fact("Agent", `${d.source.split("\n").length} lines${d.cost_usd ? `, ${money(d.cost_usd)}` : ""}`);
        this.fact("Tools declared", d.contract.authorized_tools.length ? d.contract.authorized_tools.join(", ") : "none");
        this.fleet.changed(this, "inspect", "code");
        this.line(e, "✓", `${esc(d.model)}: ${d.source.split("\n").length} lines${d.cost_usd ? `, ${money(d.cost_usd)}, ${d.seconds} s` : ""}; declares ${
          d.contract.authorized_tools.length ? d.contract.authorized_tools.map(esc).join(", ") : "no tools"}`);
        break;
      case "layer_finished":
        if (d.layer === "static") {
          await this.walk("scanner");
          const high = d.findings.filter((x) => x.startsWith("[HIGH]"));
          for (const x of high) { const m = x.match(/:(\d+) /); if (m) this.inspect.flags.add(Number(m[1])); }
          if (!d.passed) { f.alarm(true); setTimeout(() => f.alarm(false), 2500); this.set("unsafe"); this.say(`${high.length} high-severity finding${high.length === 1 ? "" : "s"}`); }
          this.fact("Static analysis", d.passed ? "no high-severity finding" : high.map((x) => x.replace(/^\[HIGH\] /, "")).slice(0, 2).join("; "));
          this.fleet.changed(this, "inspect");
          this.line(e, d.passed ? "✓" : "✗", d.passed ? `static analysis: no high-severity finding (${d.ms} ms)`
            : `static analysis: ${high.map((x) => esc(x.replace(/^\[HIGH\] /, ""))).slice(0, 2).join("; ")}`, d.passed ? "pass" : "unsafe");
          await f.wait(500);
        } else if (d.layer === "policy") {
          await this.walk("desk");
          this.fact("Policy", d.passed ? "contract allowed" : d.findings.slice(0, 2).join("; "));
          if (!d.passed) this.set("unsafe");
          this.line(e, d.passed ? "✓" : "✗", d.passed ? "policy: the contract asks only for what the policy allows"
            : `policy: ${d.findings.map(esc).slice(0, 2).join("; ")}`, d.passed ? "pass" : "unsafe");
          await f.wait(400);
        }
        break;
      case "sandbox_started":
        if (d.phase === "validation") {
          await this.walk("lab");
          this.say(`${d.level} pod running`);
          this.line(e, "▣", `${d.level} sandbox: a pod starts and the agent runs inside it`, "active");
        } else {
          await this.walk("dispatch");
          this.say("running the task");
          this.line(e, "▣", `executing the approved agent in a fresh ${d.level} sandbox`, "active");
        }
        break;
      case "sandbox_finished": {
        if (d.phase === "validation") {
          const v = [...d.network_attempts, ...d.fs_violations, ...d.unauthorized_tool_calls, ...d.runtime_violations];
          if (v.length) this.set("unsafe");
          this.say(v.length ? "blocked" : "");
          this.fact("Sandbox", v.length ? `blocked: ${v.slice(0, 2).join("; ")}` : `${(d.wall_ms / 1000).toFixed(1)} s on ${d.kernel}, output match ${d.match}`);
          this.line(e, v.length ? "✗" : "✓", v.length ? `sandbox recorded and blocked: ${v.slice(0, 2).map(esc).join("; ")}`
            : `sandbox: ran in ${(d.wall_ms / 1000).toFixed(1)} s on kernel ${esc(d.kernel)}, output match ${d.match}`, v.length ? "unsafe" : "pass");
          this.inspect.output = d.output;
          this.fleet.changed(this, "inspect");
          await f.wait(900);
        }
        break;
      }
      case "judge_started":
        await this.walk("judge");
        this.say("being scored");
        this.line(e, "⚖", "the calibrated judge (Claude Sonnet 5) scores correctness", "active");
        break;
      case "judge_finished":
        this.say(`score ${d.score} of 3`);
        this.fact("Judge", `${d.score} of 3`);
        this.line(e, "⚖", `judge: ${d.score}/3. ${esc(d.justification).slice(0, 160)}`, d.score === 3 ? "pass" : "wrong");
        await f.wait(700);
        break;
      case "verdict": {
        const state = VERDICT_STATE[d.verdict];
        // Stopped before the sandbox: decided at the contract desk, next to
        // quarantine, without walking past the lab and the judge.
        const early = ["intake", "archive", "workshop", "scanner", "desk"].includes(f.roomOf(this.key));
        await this.walk(early ? "desk" : "gate");
        this.set(state);
        this.say(d.verdict.replace("_", " "));
        this.inspect.verdict = d;
        this.info.verdict = d.verdict;
        this.fleet.changed(this, "inspect");
        this.line(e, "■", `verdict ${d.verdict} under ${d.profile}; the other profiles: ${
          Object.entries(d.by_profile).map(([p, v]) => `${p} ${v}`).join(", ")}`, state);
        await f.wait(700);
        break;
      }
      case "regeneration":
        this.set("working");
        this.line(e, "↻", "wrong output: back to the workshop with the validator's feedback", "wrong");
        await this.walk("workshop");
        break;
      case "escalated":
        this.set("unsafe");
        await this.walk("quarantine");
        this.say("waiting for a person");
        this.line(e, "⛔", `escalated to human review: ${esc(d.reason).slice(0, 200)}`, "unsafe");
        break;
      case "refused":
        this.line(e, "⛔", `deployment refused: ${esc(d.reason)}`, "unsafe");
        break;
      case "deployed":
        await this.walk("dispatch");
        this.say(d.passed ? "result delivered" : "execution failed");
        this.line(e, d.passed ? "✓" : "✗", d.passed ? `deployed: the task ran and its result was checked (${(d.ms / 1000).toFixed(1)} s)`
          : "deployed, but the execution failed", d.passed ? "pass" : "unsafe");
        break;
      case "registered":
        this.fleet.registered += 1;
        f.shelf(this.fleet.registered);
        this.line(e, "＋", "registered in the archive for reuse", "pass");
        break;
      case "error":
        this.line(e, "!", `${esc(d.stage)}: ${esc(d.message)}`, "unsafe");
        break;
      case "run_finished": {
        if (d.output !== undefined && d.output !== null) { this.inspect.output = d.output; this.fleet.changed(this, "inspect"); }
        this.info.outcome = d.outcome;
        this.info.cost = d.cost_usd;
        this.info.seconds = d.time_to_deploy_sec;
        const ok = d.succeeded;
        if (ok && this.state !== "pass") this.set("pass");
        if (!ok && this.state === "working") this.set(d.outcome === "escalated_unsafe" ? "unsafe" : "wrong");
        const outcome = (d.outcome || "").replaceAll("_", " ");
        this.line(e, "▪", `finished: ${outcome}${d.cost_usd ? `, model cost ${money(d.cost_usd)}` : ""}`, ok ? "pass" : "wrong");
        this.done = true;
        this.fleet.changed(this, "state");
        this.fleet.finished(this, d);
        break;
      }
      default:
        break;
    }
  }
}

// ------------------------------------------------------------------ views

export class Inspector {
  constructor(root) {
    this.root = root;
    root.querySelectorAll(".pane-head button").forEach((b) => b.addEventListener("click", () => this.show(b.dataset.pane)));
  }
  show(pane) {
    this.root.querySelectorAll(".pane-head button").forEach((b) => b.setAttribute("aria-selected", b.dataset.pane === pane));
    this.root.querySelectorAll(".pane").forEach((p) => { p.hidden = p.dataset.pane !== pane; });
  }
  render(track) {
    const q = (s) => this.root.querySelector(s);
    const i = track ? track.inspect : null;
    if (!i || !i.source) {
      q("#code").innerHTML = '<span class="empty">The agent\'s source appears here once it is written.</span>';
    } else {
      q("#code").innerHTML = i.source.replace(/\n$/, "").split("\n").map((l, n) =>
        `<span class="ln${i.flags.has(n + 1) ? " flag" : ""}">${esc(l) || " "}</span>`).join("");
    }
    if (!i || !i.contract) {
      q("#contract").innerHTML = '<p class="empty">The agent\'s contract appears here.</p>';
    } else {
      const c = i.contract, m = i.meta || {};
      q("#contract").innerHTML = `<dl class="kv">
        <dt>Tools it may call</dt><dd>${c.authorized_tools.length ? c.authorized_tools.map(esc).join(", ") : "none declared"}</dd>
        <dt>CPU</dt><dd>${c.cpu_cores} core${c.cpu_cores === 1 ? "" : "s"}</dd>
        <dt>Memory</dt><dd>${c.memory_mb} MB</dd>
        <dt>Time limit</dt><dd>${c.max_duration_sec} s</dd>
        <dt>Network</dt><dd>${c.network_access ? "allowed" : "none"}</dd>
        <dt>Evidence</dt><dd>${(c.evidence || []).map(esc).join(", ")}</dd>
        <dt>Written by</dt><dd>${esc(m.model)}${m.cost_usd ? `, ${money(m.cost_usd)}, ${m.seconds} s` : ""}</dd>
      </dl>`;
    }
    if (!i || !i.verdict) {
      q("#verdict").innerHTML = '<p class="empty">The verdict appears here.</p>';
    } else {
      const v = i.verdict;
      const rows = PROFILE_NAMES.map((p) => `<tr class="${p === v.profile ? "current" : ""}"><td>${p}</td>
        <td><span class="tag ${v.by_profile[p]}">${v.by_profile[p]}</span></td></tr>`).join("");
      const reasons = [...v.safety_reasons, ...v.correctness_reasons];
      q("#verdict").innerHTML = `<p><span class="tag ${v.verdict}">${v.verdict}</span> under the ${esc(v.profile)} profile${
        v.correctness_score != null ? `, judge score ${v.correctness_score}` : ""}</p>
        ${reasons.length ? `<ul class="reasons">${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        <table class="profiles"><caption class="hint" style="text-align:left">The same evidence under every strictness profile</caption>${rows}</table>`;
    }
    if (!i || i.output === undefined) {
      q("#output").innerHTML = '<p class="empty">The agent\'s output appears here.</p>';
    } else {
      q("#output").innerHTML = `<p class="hint">What the agent returned</p><pre class="json">${esc(JSON.stringify(i.output, null, 2))}</pre>
        <p class="hint">What the task expects</p><pre class="json">${esc(JSON.stringify(i.expected, null, 2))}</pre>`;
    }
  }
}

export class Fleet {
  constructor(floor, inspector, ui) {
    this.floor = floor;
    this.inspector = inspector;
    this.ui = ui;           // { header(track), detail(track), onFinished(track, data), onIdle() }
    this.tracks = new Map();
    this.followed = null;
    this.registered = 0;
    this.log = document.querySelector("#log");
  }

  reset(speed = 1) {
    this.tracks.clear();
    this.floor.clear();
    this.floor.speed = speed;
    this.followed = null;
    this.renderLog();
    this.inspector.render(null);
    this.ui.header(null);
    this.ui.detail(null);
  }

  track(key, label) {
    if (!this.tracks.has(key)) {
      this.tracks.set(key, new Track(this, key, label));
      this.floor.add(key, label);
      if (!this.followed) this.follow(key);
    }
    return this.tracks.get(key);
  }

  push(key, label, event) { this.track(key, label).push(event); }

  follow(key) {
    if (!this.tracks.has(key)) return;
    this.followed = key;
    this.floor.follow(key);
    const t = this.tracks.get(key);
    this.renderLog();
    this.inspector.render(t);
    this.ui.header(t);
    this.ui.detail(t);
    this.ui.followChanged?.(key);
  }

  changed(track, what, pane) {
    if (track.key !== this.followed) {
      if (what === "state") this.ui.tracksChanged?.();
      return;
    }
    if (what === "log") this.appendLog(track.log[track.log.length - 1]);
    if (what === "inspect") { this.inspector.render(track); if (pane) this.inspector.show(pane); }
    if (what === "state") { this.ui.header(track); this.ui.tracksChanged?.(); }
    this.ui.header(track);
    this.ui.detail(track);
  }

  finished(track, data) {
    this.ui.onFinished?.(track, data);
    // Follow the next agent still at work, so the screen keeps showing motion.
    if (track.key === this.followed && this.tracks.size > 1) {
      const next = [...this.tracks.values()].find((t) => !t.done && t.state === "working");
      if (next) setTimeout(() => { if (this.followed === track.key) this.follow(next.key); }, 2500);
    }
  }

  idleCheck() {
    if ([...this.tracks.values()].every((t) => !t.playing && !t.queue.length)) this.ui.onIdle?.();
  }
  busy() { return [...this.tracks.values()].some((t) => t.playing || t.queue.length); }

  renderLog() {
    this.log.innerHTML = "";
    const t = this.tracks.get(this.followed);
    if (!t) return;
    for (const entry of t.log) this.appendLog(entry);
  }
  appendLog(entry) {
    if (!entry) return;
    const li = document.createElement("li");
    li.className = entry.cls;
    li.innerHTML = `<span class="t">${(entry.t / 1000).toFixed(1)}s</span><span class="k">${entry.mark}</span><span>${entry.text}</span>`;
    this.log.appendChild(li);
    this.log.parentElement.scrollTop = this.log.parentElement.scrollHeight;
  }
}

export { ROOM_TEXT, ROOMS };
