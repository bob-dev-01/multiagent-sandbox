// The results tab: each finding as one sentence, one chart, and its numbers.
// Charts are plain SVG. Series colours are the two validated categorical slots;
// green, red and amber stay reserved for verdicts.

const NS = "http://www.w3.org/2000/svg";
const C1 = "#2a78d6";     // series 1
const C2 = "#eb6834";     // series 2
const INK = "#1d2b36", INK2 = "#4a5967", GRID = "#dfe5ea";

function el(name, attrs = {}, parent) {
  const n = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
function text(parent, x, y, s, attrs = {}) {
  const t = el("text", { x, y, fill: attrs.fill || INK2, "font-size": attrs.size || 12,
    "text-anchor": attrs.anchor || "start", "font-weight": attrs.weight || 400, ...attrs.extra }, parent);
  t.textContent = s;
  return t;
}
const pct = (v, d = 1) => `${(v * 100).toFixed(d)}%`;
const ms = (v) => (v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(0)} ms`);

// One tooltip for the whole tab.
let tip;
function tooltip(node, html) {
  node.style.cursor = "default";
  node.addEventListener("pointerenter", () => { tip.innerHTML = html; tip.hidden = false; });
  node.addEventListener("pointermove", (e) => {
    tip.style.left = `${e.clientX + 14}px`; tip.style.top = `${e.clientY + 10}px`;
  });
  node.addEventListener("pointerleave", () => { tip.hidden = true; });
}

function svg(w, h) { return el("svg", { viewBox: `0 0 ${w} ${h}`, class: "chart", role: "img" }); }

// Horizontal bars, one series.
function hbars(rows, { max, fmt, colour = C1, width = 640, label = 190 }) {
  const rowH = 34, h = rows.length * rowH + 10;
  const s = svg(width, h);
  const W = width - label - 90;
  rows.forEach((r, i) => {
    const y = 6 + i * rowH;
    text(s, label - 10, y + 17, r.name, { anchor: "end", fill: INK });
    el("rect", { x: label, y: y + 4, width: W, height: 18, rx: 4, fill: "#eef2f5" }, s);
    const bw = Math.max(r.value > 0 ? 3 : 0, (W * r.value) / max);
    const bar = el("rect", { x: label, y: y + 4, width: bw, height: 18, rx: 4, fill: r.colour || colour }, s);
    // Long bars carry their label inside, so it never runs off the chart.
    const inside = bw > W * 0.62;
    text(s, inside ? label + bw - 8 : label + bw + 8, y + 17, fmt(r.value, r),
      { fill: inside ? "#fff" : INK, weight: 600, anchor: inside ? "end" : "start" });
    tooltip(bar, r.tip || `${r.name}: ${fmt(r.value, r)}`);
  });
  return s;
}

// Vertical grouped bars: categories x two series.
function grouped(cats, series, { max, fmt, width = 640, height = 240, yfmt }) {
  const s = svg(width, height + 40);
  const left = 54, bottom = height, top = 12, W = width - left - 10;
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4, y = bottom - ((bottom - top) * i) / 4;
    el("line", { x1: left, x2: width - 10, y1: y, y2: y, stroke: GRID }, s);
    text(s, left - 8, y + 4, (yfmt || fmt)(v), { anchor: "end", size: 11 });
  }
  const gw = W / cats.length, bw = Math.min(34, (gw - 18) / series.length);
  cats.forEach((c, i) => {
    const gx = left + i * gw + (gw - bw * series.length - 2 * (series.length - 1)) / 2;
    series.forEach((sr, j) => {
      const v = sr.values[i];
      const bh = ((bottom - top) * v) / max;
      const bar = el("rect", { x: gx + j * (bw + 2), y: bottom - bh, width: bw, height: Math.max(0, bh),
        rx: 4, fill: sr.colour }, s);
      tooltip(bar, `${c}<br>${sr.name}: <strong>${fmt(v)}</strong>`);
    });
    text(s, left + i * gw + gw / 2, bottom + 18, c, { anchor: "middle", fill: INK, size: 12 });
  });
  return s;
}

function legend(items) {
  const div = document.createElement("div");
  div.className = "legend";
  div.innerHTML = items.map((i) => `<span><i style="background:${i.colour}"></i>${i.name}</span>`).join("");
  return div;
}

function table(head, rows) {
  const d = document.createElement("details");
  d.innerHTML = `<summary>Show the numbers</summary><table class="numbers"><thead><tr>${
    head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  return d;
}

function finding(root, { tag, title, body, charts = [], extra = [] }) {
  const sec = document.createElement("section");
  sec.className = "finding";
  sec.innerHTML = `<p class="finding-tag">${tag}</p><h2>${title}</h2><p class="finding-body">${body}</p>`;
  for (const c of charts) sec.appendChild(c);
  for (const x of extra) sec.appendChild(x);
  root.appendChild(sec);
}

export async function renderResults(root) {
  const R = await (await fetch("data/results.json")).json();
  root.innerHTML = "";
  tip = document.createElement("div");
  tip.className = "viz-tip"; tip.hidden = true;
  document.body.appendChild(tip);

  const intro = document.createElement("header");
  intro.className = "results-intro";
  intro.innerHTML = `<h1>What the experiments found</h1>
    <p>30 enterprise tasks, 300 labelled agents and 2,700 validations at three isolation levels on Azure,
    then about 600 agents written live by Claude Opus 5. Every number below comes from the recorded runs.</p>`;
  root.appendChild(intro);

  // H1
  finding(root, {
    tag: "H1, RQ1",
    title: "No unsafe agent got through, and no safe one was turned away",
    body: `All ${R.headline.validations.toLocaleString()} verdicts matched their ground-truth labels at L1, L2 and L3. Of the
      ${R.headline.unsafe_cells} runs of unsafe agents, a pipeline without validation would have deployed every one; this one deployed
      none (95% upper bound ${pct(R.matrix.fnr_upper, 2)}). Among the ${R.headline.generated_agents} agents the model wrote, none ran unsafely.`,
    charts: [hbars([
      { name: "No validation", value: R.headline.unsafe_cells, colour: "#9aa7b2" },
      { name: "Three-layer pipeline", value: R.headline.unsafe_deployed },
    ], { max: R.headline.unsafe_cells, fmt: (v) => `${v} of ${R.headline.unsafe_cells} unsafe runs deployed` })],
  });

  // Ablation
  const ab = R.ablation;
  finding(root, {
    tag: "Layer ablation",
    title: "The layers catch different things; together they catch everything",
    body: `With every unsafe agent forced through the sandbox, static analysis caught all 90 and the sandbox alone caught
      ${ab[0].sandbox} at first. A run-time monitor added in the study raised that to ${ab[ab.length - 1].sandbox}. The rest carry
      risks with no run-time effect, such as an unused hard-coded key, which only reading the code can find. Identical at L2 and L3.`,
    charts: [hbars(ab.map((a) => ({ name: a.run, value: a.sandbox, tip: `${a.run}: sandbox alone ${a.sandbox} of ${a.n}` })),
      { max: 90, fmt: (v) => `${v} of 90 detected by the sandbox alone` })],
    extra: [table(["Round", "Sandbox alone", "Static analysis"], ab.map((a) => [a.run, `${a.sandbox} / ${a.n}`, `${a.static} / ${a.n}`]))],
  });

  // H2 latency
  const L = R.latency;
  finding(root, {
    tag: "H2, RQ2",
    title: "Stronger isolation costs time, not detection",
    body: `The sandbox stage takes ${ms(L.L1.p50)} as a subprocess (L1) and about ${ms(L.L2.p50)} in a container, almost all of it
      starting the pod. gVisor (L3) adds ${pct(R.gvisor_ratio.geometric_ratio_l3_l2 - 1)} over runc (L2) for these light agents,
      but its cost is paid per system call: file-heavy work ran up to ${R.workload.file_io.ratio_l3_l2}× slower.`,
    charts: [hbars(["L1", "L2", "L3"].map((k) => ({ name: { L1: "L1 subprocess", L2: "L2 runc", L3: "L3 gVisor" }[k],
      value: L[k].p50, colour: { L1: "#7a8794", L2: C1, L3: C2 }[k],
      tip: `${k}: median ${ms(L[k].p50)}, 95th percentile ${ms(L[k].p95)}` })),
      { max: 6000, fmt: (v) => `median ${ms(v)}` }),
      legend([{ name: "L2 runc", colour: C1 }, { name: "L3 gVisor", colour: C2 }]),
      grouped(["cpu", "alloc", "file_io", "stat"].map((k) => ({ cpu: "Computation", alloc: "Memory", file_io: "File I/O", stat: "stat calls" }[k])),
        [{ name: "L2 runc", colour: C1, values: ["cpu", "alloc", "file_io", "stat"].map((k) => R.workload[k].median_l2_ms) },
         { name: "L3 gVisor", colour: C2, values: ["cpu", "alloc", "file_io", "stat"].map((k) => R.workload[k].median_l3_ms) }],
        { max: 1100, fmt: (v) => `${v.toFixed(0)} ms` })],
    extra: [table(["Workload", "L2 median", "L3 median", "L3 / L2"], Object.entries(R.workload).map(([k, w]) =>
      [k, `${w.median_l2_ms} ms`, `${w.median_l3_ms} ms`, `${w.ratio_l3_l2}×`]))],
  });

  // H3
  const h = R.h3;
  finding(root, {
    tag: "H3",
    title: "Checking correctness as well as safety predicts success far better",
    body: `On the ${R.h3_n} model-written agents that passed every safety check, safety alone would have approved all of them;
      ${pct(1 - h.safety_only.precision)} then failed their task. Adding the calibrated judge raised the accuracy of the
      prediction to ${pct(h.combined_judge_3.accuracy)} and rejected no agent that would have succeeded (McNemar p < 10⁻¹³).`,
    charts: [hbars([
      { name: "Safety only", value: h.safety_only.accuracy, tip: `Safety only: ${pct(h.safety_only.accuracy)} [${pct(h.safety_only.ci[0])}, ${pct(h.safety_only.ci[1])}]` },
      { name: "Safety + judge (score 3)", value: h.combined_judge_3.accuracy, tip: `Combined: ${pct(h.combined_judge_3.accuracy)} [${pct(h.combined_judge_3.ci[0])}, ${pct(h.combined_judge_3.ci[1])}]` },
    ], { max: 1, fmt: (v) => `accuracy ${pct(v)}` })],
  });

  // Judge calibration heatmap
  const conf = R.confusion;
  const hm = svg(360, 250);
  const max = Math.max(...conf.flat());
  text(hm, 205, 14, "judge score", { anchor: "middle", fill: INK, size: 12 });
  conf.forEach((row, hi) => row.forEach((v, ji) => {
    const x = 80 + ji * 62, y = 26 + hi * 52;
    const t = v / max;
    const cell = el("rect", { x, y, width: 58, height: 48, rx: 4,
      fill: v ? `rgba(42,120,214,${0.12 + 0.88 * t})` : "#f1f4f6" }, hm);
    text(hm, x + 29, y + 30, String(v), { anchor: "middle", fill: t > 0.5 ? "#fff" : INK, weight: 700, size: 15 });
    tooltip(cell, `human ${hi}, judge ${ji}: ${v} item${v === 1 ? "" : "s"}`);
  }));
  [0, 1, 2, 3].forEach((i) => {
    text(hm, 109 + i * 62, 244, String(i), { anchor: "middle", fill: INK });
    text(hm, 66, 56 + i * 52, String(i), { anchor: "end", fill: INK });
  });
  text(hm, 18, 140, "human", { fill: INK, extra: { transform: "rotate(-90 18 140)" }, anchor: "middle" });
  finding(root, {
    tag: "Judge calibration",
    title: `The AI judge agrees with a blind human reviewer at κ = ${R.headline.kappa.toFixed(3)}`,
    body: `A human scored 60 agents blind, the judge (Claude Sonnet 5) scored the same evidence; 58 of 60 scores are identical,
      and the two disagreements are one point apart. The threshold for letting the judge score unattended was 0.80.`,
    charts: [hm],
  });

  // H4
  const ip = R.incorrect_passed;
  const profiles = ["permissive", "balanced", "strict", "paranoid"];
  const gp = R.generated_pass;
  const gk = Object.keys(gp);
  finding(root, {
    tag: "H4, RQ4",
    title: "The strictness threshold decides whether wrong agents get through",
    body: `Re-judging every run under four profiles: ${pct(ip.permissive.k / ip.permissive.n, 0)} of the incorrect corpus agents pass
      under the loosest and none under exact matching. On model-written agents, strictness rejected 31 more than the loosest profile
      while task wording was ambiguous, and only 3 once it was repaired.`,
    charts: [hbars(profiles.map((p) => ({ name: p, value: ip[p].k / ip[p].n })),
      { max: 1, fmt: (v, r) => `${pct(v)} of incorrect agents pass` }),
      legend([{ name: gk[0], colour: C1 }, { name: gk[1], colour: C2 }]),
      grouped(profiles, gk.map((k, i) => ({ name: k, colour: i ? C2 : C1,
        values: profiles.map((p) => (gp[k][p].PASS || 0) / Object.values(gp[k][p]).reduce((a, b) => a + b, 0)) })),
        { max: 1, fmt: (v) => pct(v), yfmt: (v) => pct(v, 0) })],
  });

  // Integration
  const it = R.integration;
  const names = Object.keys(it);
  finding(root, {
    tag: "RQ3",
    title: "End to end, with a real model, the loop completes almost every task",
    body: `The whole loop, generation to execution, on 30 tasks and their rewordings. Test 1 replays the labelled corpus, so a
      third of its agents are unsafe by design; tests 2 to 4 use Claude Opus 5. Test 3 followed the fixes the live run exposed;
      test 4 ran the orchestrator and executor as AutoGen agents.`,
    charts: [legend([{ name: "original wording", colour: C1 }, { name: "reworded", colour: C2 }]),
      grouped(names.map((n) => n.split(":")[0]), [
        { name: "original wording", colour: C1, values: names.map((n) => it[n].cold_tsr) },
        { name: "reworded", colour: C2, values: names.map((n) => it[n].warm_tsr) }],
        { max: 1, fmt: (v) => pct(v), yfmt: (v) => pct(v, 0) })],
    extra: [table(["Run", "Original", "Reworded", "Reuse", "Model cost", "Unsafe executions"], names.map((n) => [n,
      pct(it[n].cold_tsr), pct(it[n].warm_tsr), pct(it[n].arr), it[n].cost ? `$${it[n].cost.toFixed(2)}` : "–", it[n].unsafe_executions]))],
  });

  // Reuse curve
  const curve = R.reuse_curve;
  const rc = svg(640, 250);
  const X = (tau) => 60 + ((tau - 0.6) / 0.35) * 560, Y = (v) => 210 - v * 190;
  for (let i = 0; i <= 4; i++) {
    el("line", { x1: 60, x2: 620, y1: Y(i / 4), y2: Y(i / 4), stroke: GRID }, rc);
    text(rc, 52, Y(i / 4) + 4, pct(i / 4, 0), { anchor: "end", size: 11 });
  }
  el("path", { fill: "rgba(42,120,214,.15)", d: `M${curve.map((c) => `${X(c.tau)},${Y(c.ci[1])}`).join("L")}L${
    [...curve].reverse().map((c) => `${X(c.tau)},${Y(c.ci[0])}`).join("L")}Z` }, rc);
  el("polyline", { fill: "none", stroke: C1, "stroke-width": 2,
    points: curve.map((c) => `${X(c.tau)},${Y(c.reuse_eligible)}`).join(" ") }, rc);
  curve.forEach((c) => {
    const dot = el("circle", { cx: X(c.tau), cy: Y(c.reuse_eligible), r: 5, fill: C1, stroke: "#fff", "stroke-width": 2 }, rc);
    tooltip(dot, `threshold ${c.tau}: ${pct(c.reuse_eligible, 0)} of reworded tasks reuse an agent [${pct(c.ci[0], 0)}, ${pct(c.ci[1], 0)}]`);
    text(rc, X(c.tau), 230, c.tau.toFixed(2), { anchor: "middle", size: 11 });
  });
  el("line", { x1: X(0.85), x2: X(0.85), y1: Y(1), y2: Y(0), stroke: INK, "stroke-dasharray": "4 4" }, rc);
  text(rc, X(0.85) + 6, Y(0.95), "threshold used, 0.85", { fill: INK, size: 12 });
  finding(root, {
    tag: "RQ3",
    title: "Reuse depends on how far a task is reworded",
    body: `A reworded task reuses a registered agent when its meaning is close enough. At the threshold used, about half of the
      rewordings qualify, which is what the live runs measured (${R.reuse_observed.map((v) => pct(v, 0)).join(" and ")}); every reused
      agent succeeded, so a lower threshold of about 0.75 looks safe on this evidence.`,
    charts: [rc],
  });
}
