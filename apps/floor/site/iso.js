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
  ops:     { origin: [0.8, 5.0],  cols: 3, label: "OPERATIONS", level: 0 },
  bay:     { origin: [5.2, -0.8], cols: 2, label: "AGENT BAY", level: 0 },
  apps:    { origin: [-1.2, -5.4], cols: 5, label: "APPS", level: 0 },
  overflow:{ origin: [-8.6, -2.6], cols: 2, label: "ANNEX", level: 0 },
  yard:    { origin: [4.4, -2.6], cols: 4, pitch: 2.0, label: "TASK YARD (level above)", level: 1 },
};
const LEVEL_HEIGHT = 78;   // screen px per level at zoom 1 — the "floor above"

const canvas = document.getElementById("floor");
const ctx = canvas.getContext("2d");
const tip = document.getElementById("tip");
const logPanel = document.getElementById("logpanel");
const logTitle = document.getElementById("logpanel-title");
const logBody = document.getElementById("logpanel-body");
const logStatus = document.getElementById("logpanel-status");
const logClose = document.getElementById("logpanel-close");

let cam = { x: 0, y: 0, zoom: 1 };
let rooms = new Map();                     // name -> room (+layout gx,gy)
let edges = [];
let workers = [];                          // {sprite, path[[sx,sy]..], t, len, color, label}
let sparks = [];
let floorMeta = { node: "…", running: 0, total: 0, sources: {} };
let sinceId = 0;
let hovered = null;

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

function draw(now, dt) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const z = cam.zoom;

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
    const [sx, sy] = proj(r.gx, r.gy, r.level);
    const [x, y] = toScreen(sx, sy);
    diamond(x, y, (TILE_W / 2 + 8) * z, (TILE_H / 2 + 4) * z);
    ctx.fillStyle = r.state === "running" ? "rgba(124, 224, 211, 0.10)"
      : r.state === "exited" ? "rgba(239, 100, 97, 0.07)"
      : "rgba(244, 237, 228, 0.04)";
    ctx.fill();
    ctx.strokeStyle = hovered === r ? "#d9a441" : "rgba(244, 237, 228, 0.14)";
    ctx.lineWidth = hovered === r ? 1.6 : 1;
    ctx.stroke();
  }

  // conveyor edges, faint; the hovered room's edges light up
  for (const e of edges) {
    const p = roomPath(e.from, e.to);
    if (!p) continue;
    const hot = hovered && (e.from === hovered.name || e.to === hovered.name);
    ctx.beginPath();
    p.pts.forEach(([px, py], i) => {
      const [x, y] = toScreen(px, py);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = hot ? (KIND_COLOR[e.kind] || "#d9a441") : "rgba(244,237,228,0.055)";
    ctx.lineWidth = hot ? 1.8 : 1;
    ctx.setLineDash(hot ? [] : [4 * z, 6 * z]);
    ctx.stroke();
    ctx.setLineDash([]);
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
      const [sx, sy] = proj(r.gx, r.gy, r.level);
      const [x, y] = toScreen(sx, sy);
      const s = (SIZE_SCALE[r.size] || 1) * z;
      const dark = r.state !== "running";
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
      ctx.fillStyle = hovered === r ? "#d9a441"
        : dark ? "rgba(180, 186, 204, 0.45)" : "rgba(244, 237, 228, 0.78)";
      const suffix = r.state === "running" ? "" : r.state === "exited" ? " · stopped"
        : r.state === "unpolled" ? "" : ` · ${r.state}`;
      const cap = r.wing === "yard" ? 12 : 22;
      const shortLabel = r.label.length > cap ? r.label.slice(0, cap - 1) + "…" : r.label;
      ctx.fillText(shortLabel + suffix, x, y + (TILE_H / 2 + 16) * z);
      if (r.archetype === "reactor" && r.state === "running" && Math.random() < dt * 3)
        sparks.push({ x: sx, y: sy - 52, vx: (Math.random() - 0.5) * 30,
                      vy: -35 - Math.random() * 25, life: 1 });
    } else {
      const w = d.worker;
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
  for (const w of workers) w.t += w.speed * dt;
  workers = workers.filter(w => w.t < w.len);
  ambient(dt);
  draw(now, dt);
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
  const wasDrag = drag && (Math.abs(e.clientX - drag.x) > 5 || Math.abs(e.clientY - drag.y) > 5);
  drag = null; canvas.classList.remove("dragging");
  canvas.releasePointerCapture(e.pointerId);
  // A click (not a pan drag) on a loggable room opens the log panel.
  if (!wasDrag && hovered && isLoggable(hovered)) openLogPanel(hovered);
});
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  cam.zoom = Math.min(2.4, Math.max(0.45, cam.zoom * (e.deltaY < 0 ? 1.1 : 0.9)));
}, { passive: false });

// --- log panel ----------------------------------------------------------------
// Rooms that can show logs: agent-dev instances, task yard containers, and
// any bay or ops container (things that run code the operator cares about).
const LOGGABLE_WINGS = new Set(["bay", "yard"]);
const LOGGABLE_NAMES = new Set(["agent", "doorbell-runner"]);

// Spinner runes (Braille + ASCII) — matches the server-side _SPINNER_CHARS.
// Used to visually dim spinner frames in the log panel.
const _SPINNER_RE = /^[⠁-⠿\-\\|\/]/;

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
  logBody.innerHTML = "";
  logStatus.textContent = "connecting…";
  logPanel.classList.add("open");

  try {
    const resp = await fetch(`/v1/logs/${encodeURIComponent(room.name)}`);
    if (!resp.ok) {
      logStatus.textContent = `error ${resp.status}`;
      return;
    }
    logStatus.textContent = "streaming · spinner frames deduplicated";
    logReader = resp.body.getReader();
    const decoder = new TextDecoder();
    let partial = "";
    const MAX_LINES = 500;

    while (true) {
      const { done, value } = await logReader.read();
      if (done) { logStatus.textContent = "stream ended"; break; }
      partial += decoder.decode(value, { stream: true });
      const lines = partial.split("\n");
      partial = lines.pop();          // last fragment, may be incomplete
      for (const line of lines) {
        const span = document.createElement("span");
        span.className = "logline" + (_SPINNER_RE.test(line) ? " spinner" : "");
        span.textContent = line;
        logBody.appendChild(span);
        // keep the body from growing forever
        while (logBody.children.length > MAX_LINES) logBody.firstChild.remove();
      }
      // auto-scroll to bottom
      logBody.scrollTop = logBody.scrollHeight;
    }
  } catch (err) {
    if (err.name !== "AbortError") logStatus.textContent = `disconnected: ${err.message}`;
  }
}

logClose.addEventListener("click", closeLogPanel);

// --- go -----------------------------------------------------------------------
resize();
cam.zoom = Math.min(1.15, Math.max(0.6, canvas.width / 2100));
pollFloor().then(pollActivity);
setInterval(pollFloor, 15000);
setInterval(pollActivity, 5000);
requestAnimationFrame(frame);
