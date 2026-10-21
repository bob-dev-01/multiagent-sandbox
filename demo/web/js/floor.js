// The factory floor: ten rooms on a 4 x 3 plan, walked as a snake from the
// intake, and any number of agents walking between them at once. Rooms are
// plain DOM; an agent is a button positioned in floor pixels, so a move is a
// CSS transition of left/top and the browser does the animation.

export const ROOMS = {
  intake:     { name: "Intake",        sub: "a task arrives",                       c: 1, r: 1 },
  archive:    { name: "Archive",       sub: "registered agents",                    c: 2, r: 1 },
  workshop:   { name: "Workshop",      sub: "Claude writes the agent",              c: 3, r: 1 },
  scanner:    { name: "Scanner",       sub: "static analysis",                      c: 4, r: 1 },
  judge:      { name: "Judge",         sub: "correctness, 0 to 3",                  c: 1, r: 2 },
  lab:        { name: "Isolation lab", sub: "sandbox pods, no network, watched",    c: 2, r: 2, span: 2 },
  desk:       { name: "Contract desk", sub: "policy check",                         c: 4, r: 2 },
  gate:       { name: "Verdict gate",  sub: "pass, unsafe or wrong",                c: 1, r: 3 },
  dispatch:   { name: "Dispatch",      sub: "runs the task for real, in a fresh sandbox", c: 2, r: 3, span: 2 },
  quarantine: { name: "Quarantine",    sub: "waits for human review",               c: 4, r: 3 },
};
// The order an agent walks; neighbours in this list share a door.
const SNAKE = ["intake", "archive", "workshop", "scanner", "desk", "lab", "judge", "gate", "dispatch"];

const PROPS = {
  intake: [["desk", 10, 76, 80, 11]],
  archive: [["shelf", 8, 25, 84, 8], ["shelf", 8, 86, 84, 8]],
  workshop: [["desk", 8, 78, 26, 10], ["desk", 37, 78, 26, 10], ["desk", 66, 78, 26, 10]],
  scanner: [["post", 22, 32, 5, 56], ["post", 73, 32, 5, 56], ["beam", 22, 28, 56, 6]],
  judge: [["desk", 12, 80, 76, 10]],
  lab: Array.from({ length: 7 }, (_, i) => ["pod", 4 + i * 13.3, 26, 11.5, 68]),
  desk: [["desk", 20, 78, 60, 10]],
  gate: [["gate", 10, 30, 80, 4]],
  dispatch: [["belt", 5, 70, 90, 14]],
  quarantine: [["cage", 10, 30, 80, 62]],
};

const ROBOT = '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="6" width="12" height="10" rx="3" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="8" cy="11" r="1.3" fill="#fff"/><circle cx="12" cy="11" r="1.3" fill="#fff"/><path d="M10 6V3" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>';

function el(tag, cls, parent, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  if (parent) parent.appendChild(n);
  return n;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Floor {
  constructor(root, { onPick } = {}) {
    this.root = root;
    this.onPick = onPick || (() => {});
    this.speed = 1;
    this.agents = new Map();      // key -> { el, room, walking, label }
    this.members = {};            // room -> [keys], in order of arrival
    this.rooms = {};
    this.followed = null;
    this.build();
    new ResizeObserver(() => this.relayoutAll()).observe(root);
  }

  build() {
    this.root.innerHTML = "";
    for (const [key, r] of Object.entries(ROOMS)) {
      const cls = ["room", key === "lab" ? "lab" : "", key === "quarantine" ? "quarantine" : ""].join(" ");
      const room = el("section", cls, this.root);
      room.style.gridColumn = `${r.c} / span ${r.span || 1}`;
      room.style.gridRow = r.r;
      room.setAttribute("aria-label", r.name);
      for (const [kind, l, t, w, h] of PROPS[key]) {
        const p = el("div", `prop ${kind}`, room);
        Object.assign(p.style, { left: `${l}%`, top: `${t}%`, width: `${w}%`, height: `${h}%` });
        p.setAttribute("aria-hidden", "true");
      }
      el("div", "room-head", room, `<span class="room-name">${r.name}</span><span class="room-sub">${r.sub}</span>`);
      const count = el("span", "room-count", room);
      this.rooms[key] = { el: room, count };
      this.members[key] = [];
    }
    this.corridor = el("div", "corridor", this.root,
      "<span>Red corridor: agents the verdict gate rejects as unsafe are walked to quarantine</span><span aria-hidden=\"true\">→</span>");
    this.doorEls = [];
    requestAnimationFrame(() => this.placeDoors());
  }

  // ------------------------------------------------------------- geometry

  box(key) {
    const e = this.rooms[key].el;
    return { x: e.offsetLeft, y: e.offsetTop, w: e.offsetWidth, h: e.offsetHeight };
  }
  centre(key) { const b = this.box(key); return { x: b.x + b.w / 2, y: b.y + b.h / 2 }; }

  // Midpoints of the gaps between rooms, where the doors are.
  gapX(leftKey, rightKey) { const a = this.box(leftKey), b = this.box(rightKey); return (a.x + a.w + b.x) / 2; }
  gapY(topKey, bottomKey) {
    const a = this.box(topKey);
    const bTop = bottomKey === "corridor" ? this.corridor.offsetTop : this.box(bottomKey).y;
    return (a.y + a.h + bTop) / 2;
  }

  // Where two neighbouring rooms meet. Agents walk door to door, so they
  // cross each room in a straight line and never cut through a wall.
  door(a, b) {
    const cx = (k) => this.centre(k).x, cy = (k) => this.centre(k).y;
    const key = [a, b].sort().join("|");
    const D = {
      "archive|intake": () => ({ x: this.gapX("intake", "archive"), y: cy("intake") }),
      "archive|workshop": () => ({ x: this.gapX("archive", "workshop"), y: cy("archive") }),
      "scanner|workshop": () => ({ x: this.gapX("workshop", "scanner"), y: cy("workshop") }),
      "desk|scanner": () => ({ x: cx("scanner"), y: this.gapY("scanner", "desk") }),
      "desk|lab": () => ({ x: this.gapX("lab", "desk"), y: cy("desk") }),
      "judge|lab": () => ({ x: this.gapX("judge", "lab"), y: cy("judge") }),
      "gate|judge": () => ({ x: cx("judge"), y: this.gapY("judge", "gate") }),
      "dispatch|gate": () => ({ x: this.gapX("gate", "dispatch"), y: cy("gate") }),
      "desk|quarantine": () => ({ x: cx("desk"), y: this.gapY("desk", "quarantine") }),
    };
    return D[key] ? D[key]() : null;
  }

  // The reuse shortcut: out of the archive's floor, along the corridor between
  // the judge and the lab, and into dispatch, past every check.
  reusePath() {
    const xGap = this.gapX("judge", "lab");
    const y1 = this.gapY("archive", "lab"), y2 = this.gapY("lab", "dispatch");
    const xIn = this.box("dispatch").x + 70;
    return [{ x: this.centre("archive").x, y: y1 }, { x: xGap, y: y1 }, { x: xGap, y: y2 }, { x: xIn, y: y2 }];
  }

  placeDoors() {
    for (const d of this.doorEls) d.remove();
    this.doorEls = [];
    const add = (cls, style, html = "") => {
      const d = el("span", cls, this.root, html);
      d.setAttribute("aria-hidden", "true");
      Object.assign(d.style, style);
      this.doorEls.push(d);
    };
    const door = (a, b, orient, arrow, red) => {
      const p = this.door(a, b);
      add(`door ${orient}${red ? " red" : ""}`, { left: `${p.x}px`, top: `${p.y}px` }, arrow);
    };
    // The green reuse corridor, drawn under the agents.
    const pts = this.reusePath();
    for (let i = 0; i < pts.length - 1; i++) {
      const p = pts[i], q = pts[i + 1];
      add("reuse-line", {
        left: `${Math.min(p.x, q.x) - 2}px`, top: `${Math.min(p.y, q.y) - 2}px`,
        width: `${Math.abs(q.x - p.x) + 4}px`, height: `${Math.abs(q.y - p.y) + 4}px`,
      });
    }
    add("door v green", { left: `${pts[0].x}px`, top: `${pts[0].y}px` }, "⌄");
    add("door v green", { left: `${pts[3].x}px`, top: `${pts[3].y}px` }, "⌄");
    door("intake", "archive", "h", "›");
    door("archive", "workshop", "h", "›");
    door("workshop", "scanner", "h", "›");
    door("scanner", "desk", "v", "⌄");
    door("desk", "lab", "h", "‹");
    door("lab", "judge", "h", "‹");
    door("judge", "gate", "v", "⌄");
    door("gate", "dispatch", "h", "›");
    door("desk", "quarantine", "v", "⌄", true);
    const cy = this.corridor.offsetTop + this.corridor.offsetHeight / 2;
    add("door v red", { left: `${this.centre("gate").x}px`, top: `${this.gapY("gate", "corridor")}px` }, "⌄");
    add("door v red", { left: `${this.centre("quarantine").x}px`, top: `${this.gapY("quarantine", "corridor")}px` }, "⌃");
    this.corridorY = cy;
  }

  // Waypoints from one room to another, each tagged with the room it leads into.
  route(from, to) {
    if (from === to) return [];
    const i = SNAKE.indexOf(from), j = SNAKE.indexOf(to);
    if (i >= 0 && j >= 0) {
      if (from === "archive" && to === "dispatch") {
        const pts = this.reusePath();
        return pts.map((p, k) => ({ ...p, room: k === pts.length - 1 ? "dispatch" : null }));
      }
      const step = j > i ? 1 : -1;
      const pts = [];
      for (let k = i; k !== j; k += step) pts.push({ ...this.door(SNAKE[k], SNAKE[k + step]), room: SNAKE[k + step] });
      return pts;
    }
    if (to === "quarantine") {
      if (from === "gate") {
        const y = this.corridor.offsetTop + this.corridor.offsetHeight / 2;
        const gx = this.centre("gate").x, qx = this.centre("quarantine").x;
        return [{ x: gx, y: this.gapY("gate", "corridor"), room: null }, { x: gx, y, room: null },
          { x: qx, y, room: null }, { x: qx, y: this.gapY("quarantine", "corridor"), room: "quarantine" }];
      }
      // From anywhere before the gate: along the snake to the contract desk, then down.
      return [...(from === "desk" ? [] : this.route(from, "desk")),
        { ...this.door("desk", "quarantine"), room: "quarantine" }];
    }
    return [];
  }

  // Free spots in a room: a grid inside it, below the sign.
  slot(key, index) {
    const b = this.box(key);
    const cols = key === "lab" ? 7 : (ROOMS[key].span || 1) * 3;
    const rows = 3;
    const c = index % cols, r = Math.floor(index / cols) % rows, layer = Math.floor(index / (cols * rows));
    return {
      x: b.x + b.w * (0.04 + 0.92 * (c + 0.5) / cols) + layer * 8,
      y: b.y + b.h * (0.36 + 0.5 * (r + 0.5) / rows) + layer * 8,
    };
  }

  relayout(room) {
    this.members[room].forEach((key, i) => {
      const a = this.agents.get(key);
      if (a && !a.walking) { a.el.style.setProperty("--walk", "400ms"); this.place(a, this.slot(room, i)); }
    });
    const n = this.members[room].length;
    this.rooms[room].count.textContent = n ? `${n} agent${n === 1 ? "" : "s"}` : "";
    this.rooms[room].el.classList.toggle("busy", n > 0);
  }
  relayoutAll() {
    this.placeDoors();
    for (const room of Object.keys(ROOMS)) this.relayout(room);
  }

  place(a, p) { a.el.style.left = `${p.x}px`; a.el.style.top = `${p.y}px`; }

  // --------------------------------------------------------------- agents

  add(key, label) {
    if (this.agents.has(key)) return;
    const b = el("button", "agent queued", this.root,
      `<span class="body">${ROBOT}</span><span class="tag"></span><span class="say" hidden></span>`);
    b.querySelector(".tag").textContent = label;
    b.setAttribute("aria-label", `${label}, waiting`);
    b.addEventListener("click", () => this.onPick(key));
    b.style.setProperty("--walk", "0s");
    const a = { el: b, room: "intake", walking: false, label };
    this.agents.set(key, a);
    this.members.intake.push(key);
    this.relayout("intake");
    requestAnimationFrame(() => requestAnimationFrame(() => b.style.removeProperty("--walk")));
  }

  async walk(key, to) {
    const a = this.agents.get(key);
    if (!a || a.room === to) return;
    const from = a.room;
    this.members[from] = this.members[from].filter((k) => k !== key);
    this.relayout(from);
    a.walking = true;
    a.el.classList.remove("idle-in-room");
    // Constant walking speed: a segment's time follows its length.
    let at = { x: parseFloat(a.el.style.left) || 0, y: parseFloat(a.el.style.top) || 0 };
    const stepTo = async (p) => {
      const ms = Math.max(120, Math.min(1100, Math.hypot(p.x - at.x, p.y - at.y) / (0.5 * this.speed)));
      a.el.style.setProperty("--walk", `${ms}ms`);
      this.place(a, p);
      at = p;
      await sleep(ms);
    };
    for (const p of this.route(from, to)) {
      await stepTo(p);
      if (p.room && key === this.followed) this.markFollowedRoom(p.room);
    }
    a.walking = false;
    a.room = to;
    this.members[to].push(key);
    const i = this.members[to].length - 1;
    await stepTo(this.slot(to, i));
    this.relayout(to);
    a.el.classList.add("idle-in-room");
    if (key === this.followed) this.markFollowedRoom(to);
  }

  markFollowedRoom(room) {
    const r = room === undefined ? this.agents.get(this.followed)?.room : room;
    for (const [k, v] of Object.entries(this.rooms)) v.el.classList.toggle("followed", k === r);
  }

  setState(key, state) {
    const a = this.agents.get(key);
    if (!a) return;
    a.el.classList.remove("queued", "working", "pass", "unsafe", "wrong");
    a.el.classList.add(state);
    a.el.setAttribute("aria-label", `${a.label}, ${state === "working" ? "at work" : state}`);
  }

  say(key, text) {
    const a = this.agents.get(key);
    if (!a) return;
    const s = a.el.querySelector(".say");
    s.textContent = text || "";
    s.hidden = !text || key !== this.followed;
  }

  follow(key) {
    this.followed = key;
    for (const [k, a] of this.agents) {
      a.el.classList.toggle("followed", k === key);
      const s = a.el.querySelector(".say");
      s.hidden = k !== key || !s.textContent;
    }
    this.markFollowedRoom();
  }

  roomOf(key) { return this.agents.get(key)?.room; }

  // Room fittings that react to what happens in them.
  shelf(n) {
    this.rooms.archive.el.querySelectorAll(".shelf").forEach((s, i) => s.classList.toggle("filled", n > i * 4));
    this.rooms.archive.el.querySelector(".room-sub").textContent =
      n ? `${n} registered agent${n === 1 ? "" : "s"}` : "registered agents";
  }
  alarm(on) { this.rooms.scanner.el.querySelector(".beam").classList.toggle("alarm", on); }

  clear() {
    for (const a of this.agents.values()) a.el.remove();
    this.agents.clear();
    for (const k of Object.keys(this.members)) { this.members[k] = []; this.relayout(k); }
    this.followed = null;
    this.alarm(false);
    this.markFollowedRoom(null);
  }

  wait(ms) { return sleep(ms / this.speed); }
}
