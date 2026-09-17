/* The floor renderer.
 *
 * Deterministic layout: rooms arrive from /v1/floor grouped by wing, are
 * sorted by name, and fill each wing's grid block — so the same node always
 * draws the same floor, and a NEW service simply takes the next slot in its
 * wing (apps wing grows by rows, without limit). Projection matches the
 * sprite kit exactly (see assets/ASSETS.md): one tile = 96x48 screen px at
 * zoom 1, sprites anchored by their footprint.
 *
 * Motion: /v1/activity events spawn workers that walk an L-shaped corridor
 * path between rooms; a low hum of ambient couriers walks the declared
 * edges so a healthy-but-quiet floor still feels alive; running rooms get
 * a breathing glow, the reactor throws sparks.
 */
"use strict";

const TILE_W = 96, TILE_H = 48;            // one floor tile at zoom 1
const PITCH = 2.6;                         // room center spacing, in tiles
const SIZE_SCALE = { small: 0.72, medium: 0.95, large: 1.22 };
const WORKER_FOR = { llm: "spark", git: "courier", pr: "scroll", issue: "scroll",
                     lifecycle: "inspector", ingress: "courier", watch: "inspector",
                     data: "courier", egress: "courier", calendar: "scroll",
                     feeds: "courier", search: "inspector", notes: "scroll" };
const KIND_COLOR = { llm: "#ffd166", git: "#ef6461", pr: "#ef6461", issue: "#9b5de5",
                     ingress: "#d9a441", watch: "#4ea8de", data: "#8d99ae",
                     egress: "#4ea8de", lifecycle: "#4ea8de" };

// Wing blocks: grid origin (in tiles) + columns. The apps wing has no row
// cap — the floor extends north as the node grows. A wing name the backend
// invents later falls into `overflow` west of the gate rather than vanishing.
// `level` lifts a wing onto its own visual deck (z, in "floors"): the task
// yard sits a level above the main floor so ephemeral runs — which come and
// go far faster than everything else — never crowd or overlap permanent
// rooms. A pitch tighter than the main floor is fine there: yard rooms are
// small, uniform, and capped (YARD_CAP server-side).
const WINGS = {
  outside: { origin: [-8.6, 2.2], cols: 1, label: "OUTSIDE", level: 0 },
  gate:    { origin: [-4.6, 0.0], cols: 2, label: "GATE", level: 0 },
  core:    { origin: [0.0, 0.0],  cols: 2, label: "CORE PLANE", level: 0 },
  ops:     { origin: [-3.0, 5.0], cols: 3, label: "OPERATIONS", level: 0 },
  bay:     { origin: [5.2, -0.8], cols: 2, label: "AGENT BAY", level: 0 },
  apps:    { origin: [-1.2, -5.4], cols: 5, label: "APPS", level: 0 },
  overflow:{ origin: [-8.6, -2.6], cols: 2, label: "ANNEX", level: 0 },
  yard:    { origin: [4.4, -2.6], cols: 4, pitch: 2.0, label: "TASK YARD (level above)", level: 1 },
};
// Screen-px vertical lift per level at zoom 1: the deck above feels like a
// separate floor, not just a half-tile offset.  Tuned for the projected
// isometry; bigger than half a tile means yard rooms never visually intersect
// the rooms below at any zoom.
const LEVEL_HEIGHT = 132;

const canvas = document.getElementById("floor");
const ctx = canvas.getContext("2d");
const tip = document.getElementById("tip");
const logPanel = document.getElementById("logpanel");
const logTitle = document.getElementById("logpanel-title");
const logBody = document.getElementById("logpanel-body");
const logStatus = document.getElementById("logpanel-status");
const logClose = document.getElementById("logpanel-close");
const logFullscreen = document.getElementById("logpanel-fullscreen");
const logResizeHandle = document.getElementById("logpanel-resize");
const logHeader = document.getElementById("logpanel-header");
const levelToggle = document.getElementById("level-toggle");

let cam = { x: 0, y: 0, zoom: 1 };
let rooms = new Map();                     // name -> room (+layout gx,gy)
let edges = [];
let workers = [];                          // {sprite, path[[sx,sy]..], t, len, color, label}
let sparks = [];
let floorMeta = { node: "…", running: 0, total: 0, sources: {} };
let sinceId = 0;
let hovered = null;
let focused = null;
let paused = false;
let motion = 0;
const inspector = document.getElementById("inspector");
const pulseCopy = document.getElementById("pulsecopy");

// A small deterministic field gives the floor depth before any data arrives.
// It is deliberately subtle: this is a map of real boundaries, not a game HUD.
const stars = Array.from({ length: 92 }, (_, i) => ({
  x: (Math.sin(i * 97.13) * .5 + .5), y: (Math.sin(i * 31.71 + 2) * .5 + .5),
  r: .35 + (i % 4) * .2, a: .12 + (i % 5) * .035,
}));

// --- sprite cache -------------------------------------------------------------
const sprites = {};
function sprite(kind, name) {
  const key = kind + "/" + name;
  if (!sprites[key]) {
    const img = new Image();
    img.src = `assets/${kind}/${name}.svg`;
    img.onerror = () => { img.failed = true; };
    sprites[key] = img;
  }
  return sprites[key];
}

// --- level focus --------------------------------------------------------------
// Which level is the user studying?  "all" shows every wing at full opacity;
// "ground" and "yard" dim the other level so the focused one reads cleanly.
// Stored here so the draw routines read it without a DOM lookup each frame.
let levelFocus = "all";   // "all" | "ground" | "yard"

function isLevelFocused(level) {
  if (levelFocus === "all") return true;
  if (levelFocus === "ground") return level === 0;
  if (levelFocus === "yard") return level === 1;
  return true;
}

function setLevelFocus(v) {
  if (v !== "all" && v !== "ground" && v !== "yard") return;
  levelFocus = v;
  if (levelToggle) {
    for (const btn of levelToggle.querySelectorAll("button[data-level]")) {
      btn.setAttribute("aria-pressed", btn.dataset.level === v ? "true" : "false");
    }
  }
}

if (levelToggle) {
  levelToggle.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-level]");
    if (btn) setLevelFocus(btn.dataset.level);
  });
}

// Dimming alpha for the non-focused level.  Not zero — the user still sees
// that something exists on the other floor; just clearly subordinate.
const DIM_ALPHA = 0.18;

// --- projection ---------------------------------------------------------------
function proj(gx, gy, level = 0) {
  return [(gx - gy) * (TILE_W / 2), (gx + gy) * (TILE_H / 2) - level * LEVEL_HEIGHT];
}
function toScreen(sx, sy) {
  return [canvas.width / 2 + (sx - cam.x) * cam.zoom,
          canvas.height / 2 + (sy - cam.y) * cam.zoom];
}

// --- layout -------------------------------------------------------------------
function layout(snapshot) {
  const byWing = {};
  for (const r of snapshot.rooms) {
    const wing = WINGS[r.wing] ? r.wing : "overflow";
    (byWing[wing] = byWing[wing] || []).push(r);
  }
  const placed = new Map();
  for (const [wing, list] of Object.entries(byWing)) {
    const w = WINGS[wing];
    const pitch = w.pitch || PITCH;
    list.sort((a, b) => a.name.localeCompare(b.name));
    list.forEach((r, i) => {
      r.gx = w.origin[0] + (i % w.cols) * pitch;
      r.gy = w.origin[1] + Math.floor(i / w.cols) * pitch * (wing === "apps" ? -1 : 1);
      r.level = w.level || 0;
      placed.set(r.name, r);
    });
  }
  rooms = placed;
  edges = snapshot.edges;
  floorMeta = { node: snapshot.node, running: snapshot.running, total: snapshot.total,
                sources: snapshot.sources, yardOverflow: snapshot.yard_overflow || 0 };
  renderPulse();
  if (focused) {
    focused = rooms.get(focused.name) || null;
    renderInspector();
  }
}

function renderPulse() {
  if (!floorMeta.total) return;
  const activeEdges = edges.filter(e => rooms.get(e.from)?.state === "running"
    || rooms.get(e.to)?.state === "running").length;
  pulseCopy.innerHTML = `<span class="metric">${floorMeta.running}/${floorMeta.total}</span> rooms lit`
    + ` · <span class="metric">${activeEdges}</span> declared pathways`;
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function renderInspector() {
  if (!focused) {
    inspector.classList.remove("open");
    inspector.innerHTML = "";
    return;
  }
  const linked = edges.filter(e => e.from === focused.name || e.to === focused.name);
  const inbound = linked.filter(e => e.to === focused.name).map(e => rooms.get(e.from)?.label || e.from);
  const outbound = linked.filter(e => e.from === focused.name).map(e => rooms.get(e.to)?.label || e.to);
  const state = focused.state === "running" ? "lit / active" : focused.state || "unpolled";
  inspector.innerHTML = `<button type="button" aria-label="Close room details">×</button>`
    + `<div class="kicker">${escapeHtml(WINGS[focused.wing]?.label || focused.wing)} · ${escapeHtml(state)}</div>`
    + `<h2>${escapeHtml(focused.label)}</h2>`
    + `<p>${escapeHtml(focused.blurb || "No description declared.")}</p>`
    + `<div class="line"><span>signals in</span><span>${escapeHtml(inbound.join(", ") || "—")}</span></div>`
    + `<div class="line"><span>signals out</span><span>${escapeHtml(outbound.join(", ") || "—")}</span></div>`
    + `<div class="line"><span>declared paths</span><span>${linked.length}</span></div>`;
  inspector.classList.add("open");
  const close = inspector.querySelector("button");
  close.addEventListener("click", () => { focused = null; renderInspector(); });
}

// --- workers ------------------------------------------------------------------
function roomPath(a, b) {
  const A = rooms.get(a), B = rooms.get(b);
  if (!A || !B) return null;
  // Same-level edges are a flat L corridor; cross-level ones (yard <-> core)
  // ride a middle waypoint at A's level, then rise/drop to B's — reads as a
  // lift shaft rather than a room floating mid-air.
  const pts = [
    proj(A.gx, A.gy, A.level),
    proj(B.gx, A.gy, A.level),
    proj(B.gx, B.gy, B.level),
  ];
  let len = 0;
  for (let i = 1; i < pts.length; i++)
    len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return { pts, len };
}

function spawnWorker(kind, from, to, label) {
  const p = roomPath(from, to);
  if (!p || p.len < 1) return;
  workers.push({
    sprite: sprite("workers", WORKER_FOR[kind] || "courier"),
    color: KIND_COLOR[kind] || "#d9a441",
    path: p.pts, len: p.len, t: 0,
    speed: 55 + Math.random() * 25,        // px/s at zoom 1
    label, bob: Math.random() * Math.PI * 2,
    roomA: from, roomB: to,
  });
  if (workers.length > 24) workers.shift();
}

function workerPos(w) {
  let d = w.t;
  for (let i = 1; i < w.path.length; i++) {
    const seg = Math.hypot(w.path[i][0] - w.path[i - 1][0],
                           w.path[i][1] - w.path[i - 1][1]);
    if (d <= seg || i === w.path.length - 1) {
      const f = seg ? Math.min(1, d / seg) : 1;
      return [w.path[i - 1][0] + (w.path[i][0] - w.path[i - 1][0]) * f,
              w.path[i - 1][1] + (w.path[i][1] - w.path[i - 1][1]) * f];
    }
    d -= seg;
  }
  return w.path[w.path.length - 1];
}

function pointOnPath(pts, progress) {
  let total = 0;
  for (let i = 1; i < pts.length; i++) total += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  let d = total * progress;
  for (let i = 1; i < pts.length; i++) {
    const seg = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (d <= seg) return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * d / seg,
                         pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * d / seg];
    d -= seg;
  }
  return pts[pts.length - 1];
}

// A quiet floor still breathes: ambient couriers walk declared edges at a
// rate tied to how much of the node is actually running.
let ambientTimer = 0;
function ambient(dt) {
  ambientTimer -= dt;
  if (ambientTimer > 0 || !edges.length) return;
  ambientTimer = 2.8 + Math.random() * 3.5;
  const live = edges.filter(e => {
    const a = rooms.get(e.from), b = rooms.get(e.to);
    return a && b && (a.state === "running" || b.state === "running");
  });
  if (!live.length || workers.length > 14) return;
  const e = live[(Math.random() * live.length) | 0];
  spawnWorker(e.kind, e.from, e.to, null);
}

// --- drawing ------------------------------------------------------------------
function diamond(cx, cy, hw, hh) {
  ctx.beginPath();
  ctx.moveTo(cx, cy - hh); ctx.lineTo(cx + hw, cy);
  ctx.lineTo(cx, cy + hh); ctx.lineTo(cx - hw, cy);
  ctx.closePath();
}

function drawBackdrop(now) {
  const drift = now / 26000;
  for (const star of stars) {
    const x = (star.x * canvas.width + Math.sin(drift + star.y * 9) * 16) % canvas.width;
    const y = star.y * canvas.height;
    ctx.fillStyle = `rgba(153, 196, 219, ${star.a})`;
    ctx.fillRect(x, y, star.r * devicePixelRatio, star.r * devicePixelRatio);
  }
  const horizon = ctx.createLinearGradient(0, canvas.height * .52, 0, canvas.height);
  horizon.addColorStop(0, "rgba(19,24,37,0)");
  horizon.addColorStop(1, "rgba(10,13,24,.28)");
  ctx.fillStyle = horizon;
  ctx.fillRect(0, canvas.height * .5, canvas.width, canvas.height * .5);
}

function drawWingAura(wing, w, members, now) {
  if (!members.length) return;
  // A wing whose level is not in focus fades into the backdrop so the focused
  // floor reads cleanly.  Auras, outlines, and the wing label all follow.
  const dim = !isLevelFocused(w.level || 0);
  if (dim) ctx.globalAlpha = DIM_ALPHA;
  const minGx = Math.min(...members.map(r => r.gx)) - .82;
  const maxGx = Math.max(...members.map(r => r.gx)) + .82;
  const minGy = Math.min(...members.map(r => r.gy)) - .82;
  const maxGy = Math.max(...members.map(r => r.gy)) + .82;
  const corners = [[minGx, minGy], [maxGx, minGy], [maxGx, maxGy], [minGx, maxGy]]
    .map(([gx, gy]) => toScreen(...proj(gx, gy, w.level || 0)));
  const energy = members.filter(r => r.state === "running").length / members.length;
  const selected = focused && (WINGS[focused.wing] ? focused.wing : "overflow") === wing;
  const alpha = .025 + energy * .035 + (selected ? .08 : 0);
  ctx.beginPath();
  corners.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.closePath();
  ctx.fillStyle = `rgba(124, 224, 211, ${alpha})`;
  ctx.fill();
  ctx.strokeStyle = selected ? "rgba(217,164,65,.72)" : `rgba(124, 224, 211, ${.08 + energy * .12})`;
  ctx.lineWidth = selected ? 1.35 : 1;
  ctx.setLineDash([3 * cam.zoom, 8 * cam.zoom]);
  ctx.lineDashOffset = -(now / 45) % (11 * cam.zoom);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineDashOffset = 0;
  if (dim) ctx.globalAlpha = 1;
}

function draw(now, dt) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const z = cam.zoom;
  drawBackdrop(now);

  // Wing auras make the system's trust zones visible before one reads a label.
  for (const [wing, w] of Object.entries(WINGS)) {
    const members = [...rooms.values()].filter(r =>
      (WINGS[r.wing] ? r.wing : "overflow") === wing);
    drawWingAura(wing, w, members, now);
  }

  // wing labels + floor pads
  ctx.textAlign = "center";
  for (const [wing, w] of Object.entries(WINGS)) {
    const members = [...rooms.values()].filter(r =>
      (WINGS[r.wing] ? r.wing : "overflow") === wing);
    if (!members.length) continue;
    const minGx = Math.min(...members.map(r => r.gx));
    const maxGx = Math.max(...members.map(r => r.gx));
    const minGy = Math.min(...members.map(r => r.gy));
    const pitch = w.pitch || PITCH;
    const [sx, sy] = proj((minGx + maxGx) / 2, minGy - pitch * 0.62, w.level || 0);
    const [x, y] = toScreen(sx, sy);
    ctx.font = `${Math.max(9, 11 * z)}px "Avenir Next", system-ui, sans-serif`;
    ctx.fillStyle = "rgba(244, 237, 228, 0.32)";
    ctx.fillText(w.label, x, y);
  }
  if (floorMeta.yardOverflow > 0) {
    const w = WINGS.yard;
    const [sx, sy] = proj(w.origin[0] + 1.5, w.origin[1] - w.pitch * 2.2, w.level);
    const [x, y] = toScreen(sx, sy);
    ctx.font = `${Math.max(9, 10 * z)}px "Avenir Next", system-ui, sans-serif`;
    ctx.fillStyle = "rgba(217, 164, 65, 0.85)";
    ctx.fillText(`+${floorMeta.yardOverflow} more task${floorMeta.yardOverflow === 1 ? "" : "s"} off-deck`, x, y);
  }

  // a translucent deck under the raised level, so "floor above" reads clearly
  const yardMembers = [...rooms.values()].filter(r => r.level > 0);
  if (yardMembers.length) {
    const minGx = Math.min(...yardMembers.map(r => r.gx)) - 1;
    const maxGx = Math.max(...yardMembers.map(r => r.gx)) + 1;
    const minGy = Math.min(...yardMembers.map(r => r.gy)) - 1;
    const maxGy = Math.max(...yardMembers.map(r => r.gy)) + 1;
    const corners = [[minGx, minGy], [maxGx, minGy], [maxGx, maxGy], [minGx, maxGy]]
      .map(([gx, gy]) => toScreen(...proj(gx, gy, yardMembers[0].level)));
    ctx.beginPath();
    corners.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.closePath();
    ctx.fillStyle = "rgba(126, 140, 181, 0.06)";
    ctx.fill();
    ctx.strokeStyle = "rgba(126, 140, 181, 0.25)";
    ctx.setLineDash([3 * z, 5 * z]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  for (const r of rooms.values()) {
    const dim = !isLevelFocused(r.level || 0);
    if (dim) ctx.globalAlpha = DIM_ALPHA;
    const [sx, sy] = proj(r.gx, r.gy, r.level);
    const [x, y] = toScreen(sx, sy);
    diamond(x, y, (TILE_W / 2 + 8) * z, (TILE_H / 2 + 4) * z);
    ctx.fillStyle = r.state === "running" ? "rgba(124, 224, 211, 0.10)"
      : r.state === "exited" ? "rgba(239, 100, 97, 0.07)"
      : "rgba(244, 237, 228, 0.04)";
    ctx.fill();
    ctx.strokeStyle = hovered === r || focused === r ? "#d9a441" : "rgba(244, 237, 228, 0.14)";
    ctx.lineWidth = hovered === r || focused === r ? 1.6 : 1;
    ctx.stroke();
    if (dim) ctx.globalAlpha = 1;
  }

  // Conveyor edges are the declared, reviewable capability graph. A slow
  // moving signal dot makes direction legible without pretending to show
  // unobserved traffic; real observed events remain the worker sprites.
  for (const e of edges) {
    const p = roomPath(e.from, e.to);
    if (!p) continue;
    const a = rooms.get(e.from), b = rooms.get(e.to);
    // An edge that only connects rooms on the dimmed level fades with them.
    // Cross-level edges (yard <-> core) stay legible — they're the lift
    // shaft between decks, exactly what you want to see while focused.
    const dim = a && b && !isLevelFocused(a.level || 0) && !isLevelFocused(b.level || 0);
    if (dim) ctx.globalAlpha = DIM_ALPHA;
    const hot = (hovered && (e.from === hovered.name || e.to === hovered.name))
      || (focused && (e.from === focused.name || e.to === focused.name));
    const live = rooms.get(e.from)?.state === "running" || rooms.get(e.to)?.state === "running";
    ctx.beginPath();
    p.pts.forEach(([px, py], i) => {
      const [x, y] = toScreen(px, py);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = hot ? (KIND_COLOR[e.kind] || "#d9a441")
      : live ? "rgba(124,224,211,0.13)" : "rgba(244,237,228,0.045)";
    ctx.lineWidth = hot ? 2 : live ? 1.15 : 1;
    ctx.setLineDash(hot ? [] : [4 * z, 6 * z]);
    ctx.stroke();
    ctx.setLineDash([]);
    if (live && (hot || Math.sin((motion + p.len) / 700) > .25)) {
      const [px, py] = pointOnPath(p.pts, ((motion / 8500) + p.len / 1000) % 1);
      const [x, y] = toScreen(px, py);
      ctx.beginPath();
      ctx.arc(x, y, (hot ? 3 : 2) * z, 0, Math.PI * 2);
      ctx.fillStyle = hot ? (KIND_COLOR[e.kind] || "#d9a441") : "rgba(124,224,211,.72)";
      ctx.shadowColor = ctx.fillStyle;
      ctx.shadowBlur = 9 * z;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    if (dim) ctx.globalAlpha = 1;
  }

  // depth-sorted rooms + workers. A raised level is its own deck in front of
  // everything below it, so bias its depth well past the main floor's range
  // rather than interleaving by gx+gy (which would z-fight across decks).
  const drawables = [];
  for (const r of rooms.values())
    drawables.push({ depth: r.gx + r.gy + r.level * 1000, room: r });
  for (const w of workers) {
    const [px, py] = workerPos(w);
    const level = Math.max(rooms.get(w.roomA)?.level || 0, rooms.get(w.roomB)?.level || 0);
    drawables.push({ depth: py / (TILE_H / 2) + 0.6 + level * 1000, worker: w, px, py });
  }
  drawables.sort((a, b) => a.depth - b.depth);

  for (const d of drawables) {
    if (d.room) {
      const r = d.room;
      const dim = !isLevelFocused(r.level || 0);
      if (dim) ctx.globalAlpha = DIM_ALPHA;
      const [sx, sy] = proj(r.gx, r.gy, r.level);
      const [x, y] = toScreen(sx, sy);
      const s = (SIZE_SCALE[r.size] || 1) * z;
      // An unpolled room is an honest absence of telemetry, not evidence the
      // service is stopped. Keep it legible while reserving the blackout for
      // a confirmed exited container.
      const dark = r.state === "exited";
      if (r.state === "running") {          // breathing glow
        const pulse = 0.55 + 0.45 * Math.sin(now / 900 + r.gx * 3.1);
        diamond(x, y, (TILE_W / 2 + 2) * z, (TILE_H / 2 + 1) * z);
        ctx.fillStyle = `rgba(255, 209, 102, ${0.05 + 0.05 * pulse})`;
        ctx.fill();
      }
      if (dark) {
        // Fully desaturated + darkened: a stopped room should read as
        // "off" at a glance, not just slightly dimmer than a running one.
        ctx.save();
        ctx.filter = "grayscale(1) brightness(0.5) contrast(0.9)";
        ctx.globalAlpha = 0.72;
      }
      const img = sprite("decor", r.archetype);
      const ok = img.complete && !img.failed && img.naturalWidth;
      const fb = ok ? img : sprite("decor", "workshop");
      if (fb.complete && fb.naturalWidth)
        ctx.drawImage(fb, x - 64 * s, y - 80 * s, 128 * s, 128 * s);
      if (dark) {
        ctx.restore();                       // drop the grayscale filter…
        diamond(x, y, (TILE_W / 2 + 8) * z, (TILE_H / 2 + 4) * z);
        ctx.fillStyle = "rgba(15, 17, 26, 0.55)";   // …then a flat shade on top
        ctx.fill();
      }
      ctx.font = `${Math.max(8, 10.5 * z)}px "Avenir Next", system-ui, sans-serif`;
      ctx.fillStyle = hovered === r || focused === r ? "#d9a441"
        : dark ? "rgba(180, 186, 204, 0.45)" : r.state === "unpolled"
          ? "rgba(244, 237, 228, 0.63)" : "rgba(244, 237, 228, 0.78)";
      const suffix = r.state === "running" ? "" : r.state === "exited" ? " · stopped"
        : r.state === "unpolled" ? "" : ` · ${r.state}`;
      const cap = r.wing === "yard" ? 12 : 22;
      const shortLabel = r.label.length > cap ? r.label.slice(0, cap - 1) + "…" : r.label;
      ctx.fillText(shortLabel + suffix, x, y + (TILE_H / 2 + 16) * z);
      if (dim) ctx.globalAlpha = 1;
      if (r.archetype === "reactor" && r.state === "running" && Math.random() < dt * 3)
        sparks.push({ x: sx, y: sy - 52, vx: (Math.random() - 0.5) * 30,
                      vy: -35 - Math.random() * 25, life: 1 });
    } else {
      const w = d.worker;
      const a = rooms.get(w.roomA), b = rooms.get(w.roomB);
      const dim = a && b && !isLevelFocused(a.level || 0) && !isLevelFocused(b.level || 0);
      if (dim) ctx.globalAlpha = DIM_ALPHA;
      const [x, y] = toScreen(d.px, d.py);
      const s = 0.92 * z;
      const bob = Math.sin(now / 110 + w.bob) * 1.6 * z;
      const img = w.sprite;
      if (img.complete && img.naturalWidth)
        ctx.drawImage(img, x - 16 * s, y - 38 * s + bob, 32 * s, 40 * s);
      ctx.fillStyle = w.color;
      ctx.beginPath();
      ctx.arc(x, y - 46 * s + bob, 1.6 * z, 0, 7);
      ctx.fill();
      if (dim) ctx.globalAlpha = 1;
    }
  }

  // reactor sparks
  for (const p of sparks) {
    p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 60 * dt; p.life -= dt * 1.3;
    const [x, y] = toScreen(p.x, p.y);
    ctx.fillStyle = `rgba(255, 209, 102, ${Math.max(0, p.life)})`;
    ctx.fillRect(x, y, 2.2 * z, 2.2 * z);
  }
  sparks = sparks.filter(p => p.life > 0);
}

// --- main loop ----------------------------------------------------------------
let last = performance.now();
function frame(now) {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  if (!paused) {
    motion += dt * 1000;
    for (const w of workers) w.t += w.speed * dt;
    workers = workers.filter(w => w.t < w.len);
    ambient(dt);
  }
  draw(now, paused ? 0 : dt);
  requestAnimationFrame(frame);
}

// --- data ---------------------------------------------------------------------
async function pollFloor() {
  try {
    const snap = await (await fetch("/v1/floor")).json();
    if (snap.rooms && snap.rooms.length) {
      layout(snap);
      document.getElementById("nodeline").textContent =
        `${snap.node} · ${snap.running}/${snap.total} rooms lit`;
    }
  } catch (e) { /* keep the last good floor */ }
}

const tickerEl = document.getElementById("ticker");
async function pollActivity() {
  try {
    const act = await (await fetch(`/v1/activity?since=${sinceId}`)).json();
    sinceId = act.latest_id || sinceId;
    for (const r of rooms.values())
      if (act.states && act.states[r.name]) r.state = act.states[r.name];
    for (const ev of act.events || []) {
      spawnWorker(ev.kind, ev.from, ev.to, ev.label);
      const div = document.createElement("div");
      div.className = `ev ${ev.kind}`;
      div.textContent = ev.label;
      tickerEl.prepend(div);
      while (tickerEl.children.length > 6) tickerEl.lastChild.remove();
    }
    renderSources(act.sources || floorMeta.sources);
  } catch (e) { /* transient */ }
}

function renderSources(sources) {
  const el = document.getElementById("sources");
  el.innerHTML = Object.entries(sources).map(([name, status]) => {
    const cls = status === "ok" ? "" : status.startsWith("no credential") ? "off" : "err";
    const note = status === "ok" ? "" : status.startsWith("no credential")
      ? " · not wired" : " · unreachable";
    return `<div class="src"><span class="dot ${cls}"></span>${name}${note}</div>`;
  }).join("");
  el.title = Object.entries(sources).map(([n, s]) => `${n}: ${s}`).join("\n");
}

// --- input --------------------------------------------------------------------
function resize() {
  canvas.width = innerWidth * devicePixelRatio;
  canvas.height = innerHeight * devicePixelRatio;
  canvas.style.width = innerWidth + "px";
  canvas.style.height = innerHeight + "px";
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}
addEventListener("resize", () => { resize(); });

let drag = null;
canvas.addEventListener("pointerdown", e => {
  drag = { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y };
  canvas.classList.add("dragging");
  canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener("pointermove", e => {
  const px = e.clientX * devicePixelRatio, py = e.clientY * devicePixelRatio;
  if (drag) {
    cam.x = drag.cx - (e.clientX - drag.x) * devicePixelRatio / cam.zoom;
    cam.y = drag.cy - (e.clientY - drag.y) * devicePixelRatio / cam.zoom;
    return;
  }
  hovered = null;
  for (const r of rooms.values()) {
    const [sx, sy] = proj(r.gx, r.gy, r.level);
    const [x, y] = toScreen(sx, sy);
    if (Math.abs(px - x) < 55 * cam.zoom && Math.abs(py - (y - 25 * cam.zoom)) < 55 * cam.zoom)
      hovered = r;
  }
  if (hovered) {
    const dot = hovered.state === "running" ? "#8ac926"
      : hovered.state === "exited" ? "#ef6461" : "#5b667e";
    const stateLabel = hovered.state === "running" ? "running"
      : hovered.state === "exited" ? "stopped" : hovered.state;
    tip.style.display = "block";
    tip.style.left = Math.min(e.clientX + 16, innerWidth - 280) + "px";
    tip.style.top = (e.clientY + 12) + "px";
    tip.innerHTML = `<b>${hovered.label}</b>`
      + `<div class="state"><span class="dot" style="background:${dot}"></span>`
      + `${stateLabel}${hovered.status ? " · " + hovered.status : ""}</div>`
      + `<div class="blurb">${hovered.blurb || ""}</div>`
      + (hovered.ring != null ? `<div class="blurb">ring ${hovered.ring}</div>` : "")
      + (hovered.level > 0 ? `<div class="blurb">sub-level: task yard</div>` : "")
      + (isLoggable(hovered) ? `<div class="blurb" style="color:#d9a441;margin-top:4px">click to view logs</div>` : "");
  } else tip.style.display = "none";
});
canvas.addEventListener("pointerup", e => {
  const wasClick = drag && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 7;
  drag = null; canvas.classList.remove("dragging");
  canvas.releasePointerCapture(e.pointerId);
  if (wasClick) {
    const px = e.clientX * devicePixelRatio, py = e.clientY * devicePixelRatio;
    let hit = null;
    for (const r of rooms.values()) {
      const [sx, sy] = proj(r.gx, r.gy, r.level);
      const [x, y] = toScreen(sx, sy);
      if (Math.abs(px - x) < 55 * cam.zoom && Math.abs(py - (y - 25 * cam.zoom)) < 55 * cam.zoom)
        hit = r;
    }
    focused = hit && focused !== hit ? hit : null;
    renderInspector();
    // The topology inspector applies to every room. Runnable agent/task rooms
    // additionally retain main's live-log drill-down without replacing it.
    if (hit && isLoggable(hit)) openLogPanel(hit);
  }
});
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  cam.zoom = Math.min(2.4, Math.max(0.45, cam.zoom * (e.deltaY < 0 ? 1.1 : 0.9)));
}, { passive: false });
addEventListener("keydown", e => {
  if (e.key === "Escape") { focused = null; renderInspector(); closeLogPanel(); }
  if (e.key === " ") { e.preventDefault(); paused = !paused; }
  if (e.key === "0") { cam = { x: 0, y: 0, zoom: Math.min(1.45, Math.max(.7, canvas.width / 1600)) }; }
});

// --- log panel ----------------------------------------------------------------
// Rooms that can show logs: agent-dev instances, task yard containers, and
// any bay or ops container (things that run code the operator cares about).
const LOGGABLE_WINGS = new Set(["bay", "yard"]);
const LOGGABLE_NAMES = new Set(["agent", "doorbell-runner"]);

// --- terminal ----------------------------------------------------------------
// The panel is a real terminal: xterm.js, the emulator VS Code's terminal is
// built on. It is staged into site/vendor/ by scripts/fetch-vendor.sh from the
// version+integrity pins in manifest/floor.toml — see that manifest for why it
// comes from this node's own registry rather than npmjs.com.
//
// What we hand it is the container's bytes, unchanged. Everything that used to
// live here — the CSI parser, the SGR state machine, the line buffer, the
// erase and cursor handling — is xterm's problem now, and it handles a great
// deal this panel never got right on its own: wide characters, combining
// marks, scroll regions, the alternate screen.
//
// Scrollback is a memory ceiling, not a UI choice: the panel holds a grid line
// per retained row. Sessions needing more history read the container log
// directly — this is a live view, not an archive.
const TERM_SCROLLBACK = 5000;

let term = null;            // the xterm.js Terminal, built on first open
let termFit = null;         // FitAddon — recomputes cols/rows from the panel

// xterm wants concrete colours, and the stylesheet is where this node's
// palette lives. Read the sixteen ANSI slots back out of CSS so there is one
// source of truth and the panel follows the floor's theme for free.
function termTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
  const slot = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
  const theme = {
    background: v("--term-bg", "#0b0e17"),
    foreground: v("--term-fg", "#e2dcd2"),
    cursor: "rgba(0,0,0,0)",            // nothing is typing; a caret would lie
    selectionBackground: "rgba(217,164,65,.32)",
  };
  slot.forEach((name, i) => {
    theme[name] = v(`--ansi-${i}`, "#cccccc");
    theme["bright" + name[0].toUpperCase() + name.slice(1)] = v(`--ansi-${i + 8}`, "#ffffff");
  });
  return theme;
}

function ensureTerminal() {
  if (term) return term;
  if (typeof Terminal === "undefined") {
    logStatus.textContent =
      "terminal unavailable — site/vendor is empty; run scripts/fetch-vendor.sh";
    return null;
  }
  term = new Terminal({
    // The stream carries real CRLFs; converting bare LFs as well would only
    // paper over a container that is genuinely writing a stair-stepped line.
    convertEol: false,
    disableStdin: true,             // a log view never sends keystrokes back
    cursorBlink: false,
    scrollback: TERM_SCROLLBACK,
    fontFamily: '"SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace',
    fontSize: 12.5,
    lineHeight: 1.25,
    theme: termTheme(),
  });
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open(logBody);
  termFit.fit();
  return term;
}

// Resizing the panel resizes the terminal, which reflows its scrollback —
// the thing a fixed-width buffer could never do.
function fitTerminal() {
  if (!term || !termFit || !logPanel.classList.contains("open")) return;
  try { termFit.fit(); } catch (_) { /* panel mid-layout; the next one lands */ }
}

if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(fitTerminal).observe(logBody);
}

let logReader = null;   // active ReadableStreamDefaultReader, if any

function isLoggable(room) {
  return LOGGABLE_WINGS.has(room.wing) || LOGGABLE_NAMES.has(room.name);
}

function closeLogPanel() {
  logPanel.classList.remove("open");
  if (logReader) { try { logReader.cancel(); } catch (_) {} logReader = null; }
}

async function openLogPanel(room) {
  closeLogPanel();              // cancel any previous stream first
  logTitle.textContent = `logs · ${room.label} (${room.name})`;
  logStatus.textContent = "connecting…";
  logPanel.classList.add("open");

  // The panel was display:none until now, so the terminal could not have
  // measured itself; build or refit it once it is actually laid out.
  const t = ensureTerminal();
  if (!t) return;               // vendor assets missing — status already says so
  t.reset();
  fitTerminal();

  try {
    const resp = await fetch(`/v1/logs/${encodeURIComponent(room.name)}`);
    if (!resp.ok) {
      logStatus.textContent = `error ${resp.status}`;
      return;
    }
    logStatus.textContent = "streaming";
    logReader = resp.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await logReader.read();
      if (done) { logStatus.textContent = "stream ended"; break; }
      t.write(decoder.decode(value, { stream: true }));
    }
  } catch (err) {
    if (err.name !== "AbortError") logStatus.textContent = `disconnected: ${err.message}`;
  }
}

// --- log panel interactions ---------------------------------------------------
// Fullscreen toggle
logFullscreen.addEventListener("click", () => {
  logPanel.classList.toggle("fullscreen");
  fitTerminal();
});

// Resizing: drag the bottom-right corner
let resizeStart = null;
logResizeHandle.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  e.stopPropagation();
  const rect = logPanel.getBoundingClientRect();
  resizeStart = {
    startX: e.clientX,
    startY: e.clientY,
    startW: rect.width,
    startH: rect.height,
  };
  logResizeHandle.setPointerCapture(e.pointerId);
});

logResizeHandle.addEventListener("pointermove", (e) => {
  if (!resizeStart) return;
  const deltaX = e.clientX - resizeStart.startX;
  const deltaY = e.clientY - resizeStart.startY;
  const newW = Math.max(280, resizeStart.startW + deltaX);
  const newH = Math.max(200, resizeStart.startH + deltaY);
  logPanel.style.width = newW + "px";
  logPanel.style.height = newH + "px";
  fitTerminal();
});

logResizeHandle.addEventListener("pointerup", () => {
  resizeStart = null;
});

// Dragging: drag the header to move
let dragStart = null;
logHeader.addEventListener("pointerdown", (e) => {
  // Don't drag if clicking a button
  if (e.target.tagName === "BUTTON") return;
  const rect = logPanel.getBoundingClientRect();
  dragStart = {
    startX: e.clientX,
    startY: e.clientY,
    startL: rect.left,
    startT: rect.top,
  };
  logHeader.setPointerCapture(e.pointerId);
});

logHeader.addEventListener("pointermove", (e) => {
  if (!dragStart) return;
  const deltaX = e.clientX - dragStart.startX;
  const deltaY = e.clientY - dragStart.startY;
  const newL = dragStart.startL + deltaX;
  const newT = dragStart.startT + deltaY;
  // Clamp to viewport
  const maxL = window.innerWidth - 100;
  const maxT = window.innerHeight - 100;
  logPanel.style.left = Math.max(0, Math.min(newL, maxL)) + "px";
  logPanel.style.top = Math.max(0, Math.min(newT, maxT)) + "px";
  logPanel.style.right = "auto";
  logPanel.style.bottom = "auto";
});

logHeader.addEventListener("pointerup", () => {
  dragStart = null;
});

// Keyboard shortcuts
addEventListener("keydown", (e) => {
  if (logPanel.classList.contains("open")) {
    if (e.key === "f" || e.key === "F") {
      e.preventDefault();
      logFullscreen.click();
    }
  }
});

logClose.addEventListener("click", closeLogPanel);

// --- go -----------------------------------------------------------------------
resize();
cam.zoom = Math.min(1.45, Math.max(0.7, canvas.width / 1600));
pollFloor().then(pollActivity);
setInterval(pollFloor, 15000);
setInterval(pollActivity, 5000);
requestAnimationFrame(frame);