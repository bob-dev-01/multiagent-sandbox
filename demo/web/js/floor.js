// The factory floor: isometric rooms, the robot, and what happens in each room.
// Grid units are projected onto the 1200x640 SVG; every effect is anchored to a room.

const NS = "http://www.w3.org/2000/svg";
const S = 42;                      // one grid unit in pixels
const O = { x: 360, y: 58 };       // where grid (0, 0) lands
const C30 = Math.cos(Math.PI / 6);

export function iso(x, y, z = 0) {
  return [O.x + (x - y) * S * C30, O.y + (x + y) * S * 0.5 - z * S];
}

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function pts(list) {
  return list.map(([x, y, z]) => iso(x, y, z).join(",")).join(" ");
}

// The route, in the order a new agent walks it.
export const ROOMS = {
  intake:   { x: 0.0,  y: 0.0,  w: 2.4, d: 2.4, sign: "Intake",        sub: "a task arrives" },
  archive:  { x: 3.4,  y: 0.0,  w: 2.8, d: 2.4, sign: "Archive",       sub: "registered agents" },
  workshop: { x: 7.2,  y: 0.0,  w: 2.8, d: 2.4, sign: "Workshop",      sub: "Claude writes the agent" },
  scanner:  { x: 11.0, y: 0.0,  w: 2.4, d: 2.4, sign: "Scanner",       sub: "static analysis" },
  desk:     { x: 14.4, y: 0.0,  w: 2.4, d: 2.4, sign: "Contract desk", sub: "policy check" },
  lab:      { x: 13.8, y: 5.0,  w: 3.0, d: 2.8, sign: "Isolation lab", sub: "the agent runs, watched" },
  judge:    { x: 10.2, y: 5.2,  w: 2.6, d: 2.4, sign: "Judge",         sub: "correctness, 0 to 3" },
  gate:     { x: 7.0,  y: 5.2,  w: 2.4, d: 2.4, sign: "Verdict gate",  sub: "" },
  dispatch: { x: 2.6,  y: 5.2,  w: 3.0, d: 2.4, sign: "Dispatch",      sub: "runs the task for real" },
  review:   { x: 7.0,  y: 9.2,  w: 2.4, d: 2.0, sign: "Human review",  sub: "unsafe agents stop here" },
};

export function centre(key) {
  const r = ROOMS[key];
  return [r.x + r.w / 2, r.y + r.d / 2];
}

export class Floor {
  constructor(svg) {
    this.svg = svg;
    this.speed = 1;
    this.at = "intake";
    this.layers = {};
    this.draw();
  }

  // ------------------------------------------------------------ static drawing
  draw() {
    const svg = this.svg;
    svg.innerHTML = "";
    const defs = el("defs", {}, svg);
    const glow = el("filter", { id: "glow", x: "-50%", y: "-50%", width: "200%", height: "200%" }, defs);
    el("feGaussianBlur", { stdDeviation: "3", result: "b" }, glow);
    const merge = el("feMerge", {}, glow);
    el("feMergeNode", { in: "b" }, merge);
    el("feMergeNode", { in: "SourceGraphic" }, merge);

    this.layers.paths = el("g", {}, svg);
    this.layers.rooms = el("g", {}, svg);
    this.layers.fx = el("g", {}, svg);
    this.layers.robot = el("g", {}, svg);

    // Corridors and the three special routes.
    const main = ["intake", "archive", "workshop", "scanner", "desk", "lab", "judge", "gate"];
    const line = (keys, cls) => el("polyline", {
      class: `path ${cls || ""}`, points: pts(keys.map((k) => [...centre(k), 0])),
    }, this.layers.paths);
    line(main);
    line(["gate", "dispatch"], "fast");
    line(["archive", "dispatch"], "fast");
    line(["gate", "workshop"], "back");
    line(["gate", "review"], "red");

    this.roomNodes = {};
    for (const [key, r] of Object.entries(ROOMS)) this.roomNodes[key] = this.drawRoom(key, r);
    this.drawRobot();
    this.placeRobot(centre("intake"));
    // Frame the drawing: whatever the room layout, the floor fills the stage.
    const box = this.layers.rooms.getBBox();
    const pad = 36;
    svg.setAttribute("viewBox", `${box.x - pad} ${box.y - pad} ${box.width + 2 * pad} ${box.height + 2 * pad + 40}`);
  }

  drawRoom(key, r) {
    const g = el("g", { class: "room", "data-room": key }, this.layers.rooms);
    const h = 0.55;
    const { x, y, w, d } = r;
    const tint = { lab: "#e3ecf6", dispatch: "#e2efe6", review: "#f4e3e1" }[key] || "#e9eef2";
    el("polygon", { class: "room-floor", fill: tint,
      points: pts([[x, y, 0], [x + w, y, 0], [x + w, y + d, 0], [x, y + d, 0]]) }, g);
    el("polygon", { class: "room-wall", fill: "#f3f6f8",
      points: pts([[x, y, 0], [x + w, y, 0], [x + w, y, h], [x, y, h]]) }, g);
    el("polygon", { class: "room-wall", fill: "#e3e9ee",
      points: pts([[x, y, 0], [x, y + d, 0], [x, y + d, h], [x, y, h]]) }, g);
    const [sx, sy] = iso(x + w / 2, y, h + 0.95);
    el("text", { class: "room-sign", x: sx, y: sy, "text-anchor": "middle" }, g).textContent = r.sign;
    if (r.sub) {
      el("text", { class: "room-sub", x: sx, y: sy + 14, "text-anchor": "middle" }, g).textContent = r.sub;
    }
    this.furnish(key, r, g);
    return g;
  }

  // A few solid blocks per room, so each room's job reads at a glance.
  block(g, x, y, w, d, h, top = "#cfd8df", left = "#b8c3cc", right = "#c4ced6") {
    el("polygon", { fill: left, points: pts([[x, y + d, 0], [x + w, y + d, 0], [x + w, y + d, h], [x, y + d, h]]) }, g);
    el("polygon", { fill: right, points: pts([[x + w, y, 0], [x + w, y + d, 0], [x + w, y + d, h], [x + w, y, h]]) }, g);
    el("polygon", { fill: top, points: pts([[x, y, h], [x + w, y, h], [x + w, y + d, h], [x, y + d, h]]) }, g);
  }

  furnish(key, r, g) {
    const { x, y, w, d } = r;
    if (key === "intake") this.block(g, x + 0.3, y + 0.3, 0.9, 0.5, 0.35);
    if (key === "archive") {
      this.block(g, x + 0.15, y + 0.1, w - 0.3, 0.12, 0.05, "#c3ccd4");
      this.block(g, x + 0.15, y + 0.6, w - 0.3, 0.12, 0.05, "#c3ccd4");
    }
    if (key === "workshop") {
      this.block(g, x + 0.3, y + 0.3, 1.3, 0.6, 0.35);
      this.block(g, x + 0.55, y + 0.35, 0.6, 0.08, 0.75, "#2b3c4a", "#1d2b36", "#2b3c4a");
    }
    if (key === "desk") this.block(g, x + 0.3, y + 0.4, 1.6, 0.45, 0.4, "#d9cfb9", "#c2b69c", "#cdc1a6");
    if (key === "judge") this.block(g, x + 0.3, y + 0.3, 1.8, 0.55, 0.5, "#c9b28d", "#a88f68", "#b89f78");
    if (key === "dispatch") {
      for (let i = 0; i < 4; i++) this.block(g, x + 0.3 + i * 0.6, y + 0.9, 0.5, 0.6, 0.12, "#a9b7c2");
    }
    if (key === "review") this.block(g, x + 0.3, y + 0.3, 1.2, 0.5, 0.38);
    if (key === "gate") {
      this.block(g, x + 0.2, y + 0.9, 0.18, 0.18, 1.0, "#4a5967", "#3a4854", "#4a5967");
      this.block(g, x + w - 0.38, y + 0.9, 0.18, 0.18, 1.0, "#4a5967", "#3a4854", "#4a5967");
    }
  }

  drawRobot() {
    const g = el("g", { id: "robot" }, this.layers.robot);
    el("ellipse", { cx: 0, cy: 2, rx: 13, ry: 5, fill: "rgba(29,43,54,.18)" }, g);
    el("rect", { x: -9, y: -30, width: 18, height: 22, rx: 5, fill: "#1d2b36" }, g);
    el("rect", { x: -11, y: -46, width: 22, height: 15, rx: 4, fill: "#2b3c4a" }, g);
    el("circle", { cx: -4, cy: -39, r: 2.3, fill: "#9fe0bb" }, g);
    el("circle", { cx: 4, cy: -39, r: 2.3, fill: "#9fe0bb" }, g);
    el("line", { x1: 0, y1: -46, x2: 0, y2: -53, stroke: "#2b3c4a", "stroke-width": 2 }, g);
    this.lamp = el("circle", { cx: 0, cy: -55, r: 3.2, fill: "#2364aa" }, g);
    this.passport = el("rect", { x: 6, y: -24, width: 11, height: 14, rx: 1.5, fill: "#f7f9fa",
      stroke: "#2364aa", "stroke-width": 1.5 }, g);
    this.robot = g;
  }

  placeRobot([gx, gy]) {
    const [px, py] = iso(gx, gy);
    this.pos = [gx, gy];
    this.robot.setAttribute("transform", `translate(${px},${py})`);
  }

  // ------------------------------------------------------------ motion
  wait(ms) { return new Promise((r) => setTimeout(r, ms / this.speed)); }

  async walk(key) {
    if (this.at === key && this.pos) return;
    const to = centre(key);
    const from = this.pos;
    const dist = Math.hypot(to[0] - from[0], to[1] - from[1]);
    const ms = Math.max(350, dist * 130) / this.speed;
    const start = performance.now();
    this.light(key);
    // A timer rather than requestAnimationFrame: in a background tab frames
    // stop and the whole run would wait for the page to be shown again.
    await new Promise((resolve) => {
      const step = () => {
        const k = Math.min(1, (performance.now() - start) / ms);
        const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        this.placeRobot([from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e]);
        if (k < 1) setTimeout(step, 16); else resolve();
      };
      step();
    });
    this.at = key;
  }

  light(key) {
    for (const [k, node] of Object.entries(this.roomNodes)) node.classList.toggle("lit", k === key);
  }

  setLamp(state) {
    const colour = { active: "#2364aa", pass: "#2e7d4f", unsafe: "#c8322b", wrong: "#d99a00" }[state];
    this.lamp.setAttribute("fill", colour);
    this.passport.setAttribute("stroke", colour);
  }

  // ------------------------------------------------------------ effects
  fxGroup(key) {
    let g = this.layers.fx.querySelector(`[data-fx="${key}"]`);
    if (!g) g = el("g", { "data-fx": key }, this.layers.fx);
    return g;
  }

  clear(key) {
    if (key) this.fxGroup(key).innerHTML = "";
    else this.layers.fx.innerHTML = "";
  }

  text(key, dx, dy, value, attrs = {}) {
    const r = ROOMS[key];
    const [x, y0] = iso(r.x + r.w / 2 + dx, r.y + r.d / 2 + dy, attrs.z || 0);
    const y = y0 + (attrs.dyPx || 0);
    const t = el("text", { class: attrs.cls || "fx-text", x, y, "text-anchor": attrs.anchor || "middle",
      fill: attrs.fill || "#1d2b36" }, this.fxGroup(key));
    t.textContent = value;
    return t;
  }

  reset() {
    this.clear();
    this.setLamp("active");
    this.at = null;
    this.placeRobot(centre("intake"));
    this.at = "intake";
    this.light("intake");
  }

  // Archive: one box per registered agent, and the similarity gauge.
  shelf(count) {
    const key = "shelf";
    const g = this.fxGroup(key);
    g.innerHTML = "";
    const r = ROOMS.archive;
    for (let i = 0; i < Math.min(count, 10); i++) {
      const bx = r.x + 0.25 + (i % 5) * 0.45;
      const by = r.y + 0.25 + Math.floor(i / 5) * 0.45;
      el("polygon", { fill: "#2e7d4f", opacity: .85, points: pts([
        [bx, by, 0.25], [bx + 0.32, by, 0.25], [bx + 0.32, by + 0.32, 0.25], [bx, by + 0.32, 0.25]]) }, g);
    }
    if (count > 10) this.text(key, 0, -0.2, `+${count - 10}`, { z: 0.4 });
  }

  gauge(similarity, tau) {
    const key = "archive";
    this.clear(key);
    const r = ROOMS.archive;
    const [x0, y0] = iso(r.x + 0.1, r.y + r.d + 0.35);
    const g = this.fxGroup(key);
    const W = 120;
    el("rect", { x: x0 - 10, y: y0 + 6, width: W, height: 8, rx: 4, fill: "#d5dbe0" }, g);
    if (similarity != null) {
      el("rect", { x: x0 - 10, y: y0 + 6, width: Math.max(2, W * similarity), height: 8, rx: 4,
        fill: similarity >= tau ? "#2e7d4f" : "#2364aa" }, g);
    }
    el("line", { x1: x0 - 10 + W * tau, x2: x0 - 10 + W * tau, y1: y0 + 2, y2: y0 + 18,
      stroke: "#1d2b36", "stroke-width": 2 }, g);
    const label = similarity == null ? "no agent registered yet"
      : `best match ${similarity.toFixed(2)} (threshold ${tau})`;
    el("text", { class: "fx-text", x: x0 - 10, y: y0 + 32 }, g).textContent = label;
  }

  printing(on, label) {
    const key = "workshop";
    this.clear(key);
    if (!on) return;
    const t = this.text(key, 0, 0.9, label || "writing the agent", { fill: "#2364aa" });
    t.animate([{ opacity: 1 }, { opacity: .35 }, { opacity: 1 }], { duration: 1200, iterations: Infinity });
  }

  async scan(findings) {
    const key = "scanner";
    this.clear(key);
    const r = ROOMS.scanner;
    const g = this.fxGroup(key);
    const posts = [[r.x + 0.5, r.y + 0.6], [r.x + 0.5, r.y + 1.6]];
    for (const [px, py] of posts) {
      el("polyline", { points: pts([[px, py, 0], [px, py, 1.1]]), stroke: "#4a5967", "stroke-width": 4 }, g);
    }
    el("polyline", { points: pts([[posts[0][0], posts[0][1], 1.1], [posts[1][0], posts[1][1], 1.1]]),
      stroke: "#4a5967", "stroke-width": 4 }, g);
    const beam = el("polyline", { points: pts([[r.x + 0.5, r.y + 0.6, 0.1], [r.x + 0.5, r.y + 1.6, 0.1]]),
      stroke: "#2364aa", "stroke-width": 3, filter: "url(#glow)" }, g);
    beam.animate([{ transform: "translate(0,0)" }, { transform: "translate(0,-34px)" }, { transform: "translate(0,0)" }],
      { duration: 900 / this.speed, iterations: 2 });
    await this.wait(1500);
    const high = findings.filter((f) => f.startsWith("[HIGH]"));
    if (high.length) {
      high.slice(0, 3).forEach((f, i) => {
        const rule = (f.match(/\] (\S+?)(:|\s)/) || [])[1] || "finding";
        this.text(key, 1.3, -0.2 + i * 0.55, rule, { fill: "#c8322b", anchor: "start" });
      });
      if (high.length > 3) this.text(key, 1.3, 1.45, `+${high.length - 3} more`, { fill: "#c8322b", anchor: "start" });
    } else {
      this.text(key, 1.2, 0.2, findings.length ? `${findings.length} minor` : "clean",
        { fill: "#2e7d4f", anchor: "start" });
    }
  }

  async stamp(ok, label) {
    const key = "desk";
    this.clear(key);
    await this.wait(500);
    const r = ROOMS.desk;
    const [x, y] = iso(r.x + r.w / 2, r.y + r.d / 2, 0.5);
    const g = this.fxGroup(key);
    const ring = el("circle", { cx: x + 34, cy: y - 6, r: 17, fill: "none",
      stroke: ok ? "#2e7d4f" : "#c8322b", "stroke-width": 3 }, g);
    el("text", { class: "fx-text", x: x + 34, y: y - 2, "text-anchor": "middle",
      fill: ok ? "#2e7d4f" : "#c8322b" }, g).textContent = ok ? "OK" : "NO";
    ring.animate([{ transform: "scale(1.6)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }],
      { duration: 250 / this.speed, easing: "ease-out" });
    if (label) this.text(key, 0, 1.25, label, { fill: ok ? "#2e7d4f" : "#c8322b" });
  }

  chamber(level, state) {
    const key = "lab";
    this.clear(key);
    const r = ROOMS.lab;
    const g = this.fxGroup(key);
    const x0 = r.x + 0.4, y0 = r.y + 0.4, w = r.w - 0.8, d = r.d - 0.8, h = 1.3;
    const box = (inset, colour, width) => {
      const a = x0 + inset, b = y0 + inset, ww = w - 2 * inset, dd = d - 2 * inset;
      el("polygon", { fill: "rgba(35,100,170,.06)", stroke: colour, "stroke-width": width,
        points: pts([[a, b, h], [a + ww, b, h], [a + ww, b + dd, h], [a, b + dd, h]]) }, g);
      for (const [px, py] of [[a + ww, b], [a + ww, b + dd], [a, b + dd]]) {
        el("polyline", { points: pts([[px, py, 0], [px, py, h]]), stroke: colour, "stroke-width": width }, g);
      }
    };
    box(0, "#2364aa", 1.5);
    if (level === "L3") box(0.18, "#2364aa", 1.5);
    const tag = level === "L3" ? "L3: gVisor, its own kernel" : "L2: runc container";
    this.text(key, 0, 1.75, tag, { fill: "#2364aa" });
    if (state === "running") {
      this.alarm = null;
      const t = this.text(key, 0, -0.15, "running", { fill: "#2364aa", z: 1.6 });
      t.animate([{ opacity: 1 }, { opacity: .3 }, { opacity: 1 }], { duration: 1000, iterations: Infinity });
    }
  }

  labResult(level, sandbox) {
    this.chamber(level, "done");
    const key = "lab";
    const lines = [];
    const add = (list, label) => { if (list && list.length) lines.push(`${label}: ${list.length}`); };
    add(sandbox.network_attempts, "network blocked");
    add(sandbox.fs_violations, "file access blocked");
    add(sandbox.unauthorized_tool_calls, "undeclared tool");
    add(sandbox.runtime_violations, "runtime violation");
    if (sandbox.kernel) this.text(key, 0, 1.75, `kernel ${sandbox.kernel}`, { fill: "#4a5967", dyPx: 15 });
    const secs = sandbox.wall_ms ? `${(sandbox.wall_ms / 1000).toFixed(1)} s` : "";
    if (lines.length) {
      const g = this.fxGroup(key);
      const r = ROOMS.lab;
      const [x, y] = iso(r.x + r.w - 0.3, r.y + 0.3, 1.6);
      const lamp = el("circle", { cx: x, cy: y, r: 7, fill: "#c8322b", filter: "url(#glow)" }, g);
      lamp.animate([{ opacity: 1 }, { opacity: .2 }], { duration: 400, iterations: Infinity, direction: "alternate" });
      lines.forEach((l, i) => this.text(key, 0, -0.35 + i * 0.5, l, { fill: "#c8322b", z: 1.4 }));
    } else {
      const ok = sandbox.match === 1;
      this.text(key, 0, -0.1, ok ? "no violations, output matches" : `no violations, output match ${(sandbox.match ?? 0).toFixed(2)}`,
        { fill: ok ? "#2e7d4f" : "#8a6200", z: 1.4 });
    }
    if (secs) this.text(key, 0, 0.45, secs, { fill: "#4a5967", z: 1.4 });
  }

  async gavel(score) {
    const key = "judge";
    this.clear(key);
    const r = ROOMS.judge;
    const [x, y] = iso(r.x + r.w / 2 + 0.6, r.y + r.d / 2, 0.6);
    const g = this.fxGroup(key);
    const hammer = el("g", { transform: `translate(${x},${y})` }, g);
    el("rect", { x: -3, y: -26, width: 6, height: 26, fill: "#6b4f2a", rx: 2 }, hammer);
    el("rect", { x: -11, y: -34, width: 22, height: 10, fill: "#8a6a3c", rx: 2 }, hammer);
    hammer.animate([{ transform: `translate(${x}px,${y}px) rotate(-35deg)` },
      { transform: `translate(${x}px,${y}px) rotate(10deg)` }], { duration: 300 / this.speed, iterations: 2, direction: "alternate" });
    await this.wait(600);
    if (score != null) {
      const colour = score === 3 ? "#2e7d4f" : score === 2 ? "#8a6200" : "#c8322b";
      this.text(key, -0.7, 0.2, `${score}`, { cls: "big-score", fill: colour, z: 0.7 });
    }
  }

  gateLight(state, label) {
    const key = "gate";
    this.clear(key);
    const r = ROOMS.gate;
    const colours = { pass: "#2e7d4f", unsafe: "#c8322b", wrong: "#d99a00" };
    const g = this.fxGroup(key);
    ["pass", "wrong", "unsafe"].forEach((s, i) => {
      const [x, y] = iso(r.x + 0.5 + i * 0.6, r.y + 0.15, 1.05);
      el("circle", { cx: x, cy: y, r: 6, fill: s === state ? colours[s] : "#cfd6dc",
        filter: s === state ? "url(#glow)" : "" }, g);
    });
    if (label) this.text(key, 0, 1.25, label, { fill: colours[state] || "#1d2b36" });
  }

  delivered(ok, label) {
    const key = "dispatch";
    this.clear(key);
    this.text(key, 0, 1.3, label, { fill: ok ? "#2e7d4f" : "#c8322b" });
  }

  quarantine(reason) {
    const key = "review";
    this.clear(key);
    const r = ROOMS.review;
    const g = this.fxGroup(key);
    const [x, y] = iso(r.x + r.w / 2 + 0.55, r.y + r.d / 2 - 0.2);
    el("circle", { cx: x, cy: y - 26, r: 6, fill: "#4a5967" }, g);
    el("rect", { x: x - 7, y: y - 19, width: 14, height: 18, rx: 4, fill: "#4a5967" }, g);
    if (reason) this.text(key, 0, 1.2, reason, { fill: "#c8322b" });
  }
}
