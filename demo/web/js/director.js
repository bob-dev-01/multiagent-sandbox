// Plays a run's events, live or recorded, as one sequence: the robot walks, the
// rooms react, the console logs, the inspector fills in. Events are queued, so
// a burst (static analysis takes milliseconds) still reads at human speed and a
// slow step (the sandbox takes seconds) simply holds the robot in its room.

const VERDICT_STATE = { PASS: "pass", FAIL_UNSAFE: "unsafe", FAIL_BOTH: "unsafe", FAIL_INCORRECT: "wrong" };
const PROFILE_NAMES = ["permissive", "balanced", "strict", "paranoid"];

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function money(v) { return v == null ? "" : `$${Number(v).toFixed(3)}`; }

export class Console {
  constructor(list, clock) { this.list = list; this.clock = clock; }
  clear() { this.list.innerHTML = ""; this.clock.textContent = "0.0 s"; }
  add(t, mark, text, cls = "") {
    const li = document.createElement("li");
    li.className = cls;
    li.innerHTML = `<span class="t">${(t / 1000).toFixed(1)}s</span><span class="k">${mark}</span><span>${text}</span>`;
    this.list.appendChild(li);
    this.list.scrollTop = this.list.scrollHeight;
    this.clock.textContent = `${(t / 1000).toFixed(1)} s`;
  }
}

export class Inspector {
  constructor(root) {
    this.root = root;
    this.code = root.querySelector("#code");
    this.contract = root.querySelector("#contract");
    this.verdict = root.querySelector("#verdict");
    this.output = root.querySelector("#output");
    root.querySelectorAll(".pane-head button").forEach((b) => b.addEventListener("click", () => this.show(b.dataset.pane)));
  }
  show(pane) {
    this.root.querySelectorAll(".pane-head button").forEach((b) => b.setAttribute("aria-selected", b.dataset.pane === pane));
    this.root.querySelectorAll(".pane").forEach((p) => { p.hidden = p.dataset.pane !== pane; });
  }
  clear() {
    this.code.innerHTML = '<span class="empty">The agent\'s source appears here once it is written.</span>';
    this.contract.innerHTML = '<p class="empty">The agent\'s contract appears here.</p>';
    this.verdict.innerHTML = '<p class="empty">The verdict appears here.</p>';
    this.output.innerHTML = '<p class="empty">The agent\'s output appears here.</p>';
    this.flags = new Set();
    this.source = "";
  }
  setCode(source) {
    this.source = source;
    this.renderCode();
  }
  flagLines(findings) {
    for (const f of findings) {
      const m = f.match(/:(\d+) /);
      if (m) this.flags.add(Number(m[1]));
    }
    this.renderCode();
  }
  renderCode() {
    const lines = this.source.replace(/\n$/, "").split("\n");
    this.code.innerHTML = lines.map((l, i) =>
      `<span class="ln${this.flags.has(i + 1) ? " flag" : ""}">${esc(l) || " "}</span>`).join("");
  }
  setContract(c, meta) {
    this.contract.innerHTML = `<dl class="kv">
      <dt>Tools it may call</dt><dd>${c.authorized_tools.length ? c.authorized_tools.map(esc).join(", ") : "none declared"}</dd>
      <dt>CPU</dt><dd>${c.cpu_cores} core${c.cpu_cores === 1 ? "" : "s"}</dd>
      <dt>Memory</dt><dd>${c.memory_mb} MB</dd>
      <dt>Time limit</dt><dd>${c.max_duration_sec} s</dd>
      <dt>Network</dt><dd>${c.network_access ? "allowed" : "none"}</dd>
      <dt>Evidence</dt><dd>${c.evidence.map(esc).join(", ")}</dd>
      <dt>Written by</dt><dd>${esc(meta.model)}${meta.cost_usd ? `, ${money(meta.cost_usd)}, ${meta.seconds} s` : ""}</dd>
    </dl>`;
  }
  setVerdict(v, current) {
    const rows = PROFILE_NAMES.map((p) => `<tr class="${p === current ? "current" : ""}"><td>${p}</td>
      <td><span class="tag ${v.by_profile[p]}">${v.by_profile[p]}</span></td></tr>`).join("");
    const reasons = [...v.safety_reasons, ...v.correctness_reasons];
    this.verdict.innerHTML = `<p><span class="tag ${v.verdict}">${v.verdict}</span> under the ${esc(v.profile)} profile${
      v.correctness_score != null ? `, judge score ${v.correctness_score}` : ""}</p>
      ${reasons.length ? `<ul class="reasons">${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <table class="profiles"><caption class="hint" style="text-align:left">The same evidence under every strictness profile</caption>${rows}</table>`;
  }
  setOutput(got, expected) {
    this.output.innerHTML = `<p class="hint">What the agent returned</p><pre class="json">${esc(JSON.stringify(got, null, 2))}</pre>
      <p class="hint">What the task expects</p><pre class="json">${esc(JSON.stringify(expected, null, 2))}</pre>`;
  }
}

export class Director {
  constructor(floor, consoleView, inspector, ui) {
    this.floor = floor;
    this.console = consoleView;
    this.inspector = inspector;
    this.ui = ui;                      // { banner(text, state), taskcard(html), done(summary) }
    this.queue = [];
    this.playing = false;
    this.ctx = {};
  }

  reset(speed = 1) {
    this.queue = [];
    this.floor.speed = speed;
    this.floor.reset();
    this.console.clear();
    this.inspector.clear();
    this.ctx = { registered: 0 };
    this.ui.banner(null);
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
  }

  log(e, mark, text, cls) { this.console.add(e.t, mark, text, cls); }

  async handle(e) {
    const d = e.data;
    const f = this.floor;
    switch (e.type) {
      case "run_started": {
        this.ctx = { ...this.ctx, level: d.level, profile: d.profile, expected: d.expected_output };
        this.ctx.registered = d.registered ?? this.ctx.registered;
        f.shelf(this.ctx.registered);
        this.ui.taskcard(`<strong>${esc(d.task_id)}: ${esc(d.title)}</strong>${esc(d.description)}`);
        this.ui.banner(d.replay ? "Recorded run, replayed" : "Live run", "active");
        this.log(e, "▸", `${esc(d.task_id)} (${d.wording} wording), ${d.level}, ${d.profile} profile${d.replay ? ", recorded" : ""}`, "active");
        await f.wait(700);
        break;
      }
      case "scripted_agent":
        this.log(e, "•", `the generator will serve a stored ${d.label.replace("_", " ")} agent (${esc(d.failure_mode || "correct")})`);
        break;
      case "autogen_message":
        this.log(e, "⇄", `AutoGen: ${esc(d.message)} to ${esc(d.to)}`, "autogen");
        break;
      case "registry_lookup":
        await f.walk("archive");
        f.gauge(d.best_similarity, d.tau);
        this.log(e, "?", d.best_similarity == null ? "registry is empty: generate a new agent"
          : `closest registered agent: similarity ${d.best_similarity} against threshold ${d.tau}, so ${d.decision}`, "active");
        await f.wait(1300);
        break;
      case "reused":
        f.setLamp("pass");
        this.ui.banner(`Reused a registered agent (similarity ${Number(d.similarity).toFixed(2)}): no generation, no validation`, "pass");
        this.log(e, "↺", `reused the agent written for ${esc(d.source_task)}`, "pass");
        await f.walk("dispatch");
        break;
      case "generation_started":
        await f.walk("workshop");
        f.printing(true, d.regeneration ? `rewriting, attempt ${d.attempt}` : "writing the agent");
        this.log(e, "✎", d.regeneration ? `attempt ${d.attempt}: the model rewrites the agent from the validator's feedback`
          : "the generator writes an agent", "active");
        break;
      case "generation_finished":
        f.printing(false);
        this.inspector.setCode(d.source);
        this.inspector.setContract(d.contract, d);
        this.inspector.show("code");
        this.log(e, "✓", `${esc(d.model)}: ${d.source.split("\n").length} lines${d.cost_usd ? `, ${money(d.cost_usd)}, ${d.seconds} s` : ""}; declares ${
          d.contract.authorized_tools.length ? d.contract.authorized_tools.map(esc).join(", ") : "no tools"}`);
        break;
      case "validation_started":
        f.setLamp("active");
        break;
      case "layer_finished":
        if (d.layer === "static") {
          await f.walk("scanner");
          await f.scan(d.findings);
          this.inspector.flagLines(d.findings.filter((x) => x.startsWith("[HIGH]")));
          const high = d.findings.filter((x) => x.startsWith("[HIGH]"));
          this.log(e, d.passed ? "✓" : "✗", d.passed ? `static analysis: no high-severity finding (${d.ms} ms)`
            : `static analysis: ${high.map((x) => esc(x.replace(/^\[HIGH\] /, ""))).slice(0, 2).join("; ")}`, d.passed ? "pass" : "unsafe");
          if (!d.passed) f.setLamp("unsafe");
        } else if (d.layer === "policy") {
          await f.walk("desk");
          await f.stamp(d.passed, d.passed ? "contract allowed" : "contract refused");
          this.log(e, d.passed ? "✓" : "✗", d.passed ? "policy: the contract asks only for what the policy allows"
            : `policy: ${d.findings.map(esc).slice(0, 2).join("; ")}`, d.passed ? "pass" : "unsafe");
        }
        break;
      case "sandbox_started":
        if (d.phase === "validation") {
          await f.walk("lab");
          f.chamber(d.level, "running");
          this.log(e, "▣", `${d.level} sandbox: a pod starts and the agent runs inside it`, "active");
        } else {
          await f.walk("dispatch");
          this.log(e, "▣", `executing the approved agent in a fresh ${d.level} sandbox`, "active");
        }
        break;
      case "sandbox_finished": {
        if (d.phase === "validation") {
          f.labResult(d.level, d);
          const v = [...d.network_attempts, ...d.fs_violations, ...d.unauthorized_tool_calls, ...d.runtime_violations];
          if (v.length) f.setLamp("unsafe");
          this.log(e, v.length ? "✗" : "✓", v.length ? `sandbox recorded and blocked: ${v.slice(0, 2).map(esc).join("; ")}`
            : `sandbox: ran in ${(d.wall_ms / 1000).toFixed(1)} s on kernel ${esc(d.kernel)}, output match ${d.match}`, v.length ? "unsafe" : "pass");
          this.inspector.setOutput(d.output, this.ctx.expected);
          await f.wait(1600);
        } else {
          this.ctx.execOutput = d.output;
        }
        break;
      }
      case "judge_started":
        await f.walk("judge");
        this.log(e, "⚖", "the calibrated judge (Claude Sonnet 5) scores correctness", "active");
        break;
      case "judge_finished":
        await f.gavel(d.score);
        this.log(e, "⚖", `judge: ${d.score}/3. ${esc(d.justification).slice(0, 160)}`, d.score === 3 ? "pass" : "wrong");
        break;
      case "verdict": {
        const state = VERDICT_STATE[d.verdict];
        await f.walk("gate");
        f.gateLight(state, d.verdict.replace("_", " "));
        f.setLamp(state);
        this.inspector.setVerdict(d, d.profile);
        this.log(e, "■", `verdict ${d.verdict} under ${d.profile}; the other profiles: ${
          Object.entries(d.by_profile).map(([p, v]) => `${p} ${v}`).join(", ")}`, state);
        await f.wait(900);
        break;
      }
      case "regeneration":
        this.ui.banner("Wrong output: back to the workshop with the validator's feedback", "wrong");
        this.log(e, "↻", "regenerate: the feedback goes back to the model", "wrong");
        await f.walk("workshop");
        break;
      case "escalated":
        this.ui.banner("Unsafe: stopped and sent to a person, never back to the generator", "unsafe");
        await f.walk("review");
        f.quarantine("waiting for a person");
        this.log(e, "⛔", `escalated to human review: ${esc(d.reason).slice(0, 200)}`, "unsafe");
        break;
      case "refused":
        this.log(e, "⛔", `deployment refused: ${esc(d.reason)}`, "unsafe");
        break;
      case "deployed":
        await f.walk("dispatch");
        f.delivered(d.passed, d.passed ? "result delivered" : "execution failed");
        this.log(e, d.passed ? "✓" : "✗", d.passed ? `deployed: the task ran and its result was checked (${(d.ms / 1000).toFixed(1)} s)`
          : "deployed, but the execution failed", d.passed ? "pass" : "unsafe");
        break;
      case "registered":
        this.ctx.registered += 1;
        f.shelf(this.ctx.registered);
        this.log(e, "＋", "registered in the archive for reuse", "pass");
        break;
      case "error":
        this.ui.banner(`Problem: ${d.message}`, "unsafe");
        this.log(e, "!", `${esc(d.stage)}: ${esc(d.message)}`, "unsafe");
        break;
      case "run_finished": {
        const ok = d.succeeded;
        if (d.output !== undefined && d.output !== null) this.inspector.setOutput(d.output, this.ctx.expected);
        const outcome = (d.outcome || "").replaceAll("_", " ");
        this.ui.banner(ok ? `Done: ${outcome}${d.time_to_deploy_sec != null ? ` in ${d.time_to_deploy_sec.toFixed(1)} s` : ""}`
          : `Ended: ${outcome}`, ok ? "pass" : (d.outcome === "escalated_unsafe" ? "unsafe" : "wrong"));
        this.log(e, "▪", `finished: ${outcome}${d.cost_usd ? `, model cost ${money(d.cost_usd)}` : ""}`, ok ? "pass" : "wrong");
        this.ui.done(d);
        break;
      }
      default:
        break;
    }
  }
}
