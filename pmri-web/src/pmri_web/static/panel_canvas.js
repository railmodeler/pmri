/**
 * Panel Canvas editor -- renders the panel model on a <canvas> and
 * handles pan/zoom + selection. Edit operations (add/remove/move)
 * dispatch to the server over the existing `/json` WebSocket as
 * `panel-edit` ops. The read-side `/panels/{name}/model` endpoint
 * ships the current panel state; the client re-fetches after each
 * mutation so the view is always in sync with disk.
 *
 * Design: deliberately framework-free. One module, one canvas
 * context, one state object. The server owns the source of truth
 * (the panel XML file); this script is a thin view + input
 * dispatcher.
 */

const HIT_POS_POINT = 1;
const HIT_TURNOUT_A = 2;
const HIT_TURNOUT_B = 3;
const HIT_TURNOUT_C = 4;
const HIT_TURNOUT_D = 5;

const COLORS = {
  track: "#444",
  trackMain: "#222",
  point: "#888",
  pointSelected: "#2a6",
  turnoutStock: "#444",
  turnoutClosed: "#3a3",
  turnoutThrown: "#c33",
  turnoutIdle: "#888",
  sensor: "#aaa",
  signal: "#aaa",
  memoryBg: "#ffd",
  memoryBorder: "#776",
  label: "#222",
  selection: "#2a6",
  grid: "#eee",
  background: "#ffffff",
};

// ---- state --------------------------------------------------------------

const shell = document.querySelector(".panel-editor-shell");
if (!shell) throw new Error("panel-editor shell missing");
const panelName = shell.dataset.panel;
const panelWidth = parseInt(shell.dataset.width || "1200", 10);
const panelHeight = parseInt(shell.dataset.height || "800", 10);

const canvas = shell.querySelector('[data-role="canvas"]');
const status = shell.querySelector('[data-role="status"]');
const zoomReadout = shell.querySelector('[data-role="zoom-readout"]');
const drawerBody = shell.querySelector('[data-role="drawer-body"]');
const panelWidthInput = shell.querySelector('[data-role="panel-width"]');
const panelHeightInput = shell.querySelector('[data-role="panel-height"]');
const applySizeBtn = shell.querySelector('[data-role="apply-size"]');

const state = {
  model: null,            // last loaded panel model (JSON)
  tool: "select",         // active tool id
  view: {
    scale: 1.0,
    offsetX: 0,
    offsetY: 0,
  },
  drag: null,             // {kind: "pan"|"draw-segment", startX, startY, ...}
  selection: null,        // {kind: "point"|"segment"|"turnout", id: "..."}
  pendingSegment: null,   // first-endpoint pick for the add-segment tool
};

// ---- websocket client ---------------------------------------------------

let ws = null;
let wsReady = false;
let wsOutbox = [];

function openWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/json`);
  ws.addEventListener("open", () => {
    wsReady = true;
    setStatus("connected");
    while (wsOutbox.length) ws.send(wsOutbox.shift());
  });
  ws.addEventListener("close", () => {
    wsReady = false;
    setStatus("disconnected -- reconnecting...");
    setTimeout(openWs, 1500);
  });
  ws.addEventListener("message", (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handleWsMessage(msg);
    } catch { /* ignore bad frames */ }
  });
}

function wsSend(obj) {
  const raw = JSON.stringify(obj);
  if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(raw);
  } else {
    wsOutbox.push(raw);
  }
}

function handleWsMessage(msg) {
  if (msg && msg.type === "error") {
    setStatus(`error: ${msg.data && msg.data.message || "unknown"}`);
    return;
  }
  if (msg && msg.type === "panel-edit") {
    // Server echoed our mutation or a peer client's mutation --
    // reload the model to pick up the change.
    void loadModel();
  }
}

// ---- model fetch --------------------------------------------------------

async function loadModel() {
  setStatus("loading...");
  const resp = await fetch(`/panels/${encodeURIComponent(panelName)}/model`);
  if (!resp.ok) {
    setStatus(`error loading panel (${resp.status})`);
    return;
  }
  state.model = await resp.json();
  setStatus(`${state.model.points.length} pts, `
          + `${state.model.segments.length} segs, `
          + `${state.model.turnouts.length} turnouts`);
  // Reflect any size change from the server back into the inputs so
  // the operator sees the authoritative dimensions.
  if (panelWidthInput) panelWidthInput.value = state.model.width;
  if (panelHeightInput) panelHeightInput.value = state.model.height;
  render();
}

function setStatus(msg) {
  if (status) status.textContent = msg;
}

// ---- rendering ----------------------------------------------------------

function resizeCanvas() {
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const cssW = wrap.clientWidth;
  const cssH = wrap.clientHeight;
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  render();
}

function worldToScreen(x, y) {
  return {
    x: x * state.view.scale + state.view.offsetX,
    y: y * state.view.scale + state.view.offsetY,
  };
}

function screenToWorld(px, py) {
  return {
    x: (px - state.view.offsetX) / state.view.scale,
    y: (py - state.view.offsetY) / state.view.scale,
  };
}

function resolveEndpoint(ref, type) {
  if (!state.model) return null;
  if (type === HIT_POS_POINT) {
    const p = state.model.points.find((p) => p.ident === ref);
    return p ? { x: p.x, y: p.y } : null;
  }
  const t = state.model.turnouts.find((t) => t.ident === ref);
  if (!t) return null;
  if (type === HIT_TURNOUT_A) return { x: t.a[0], y: t.a[1] };
  if (type === HIT_TURNOUT_B) return { x: t.b[0], y: t.b[1] };
  if (type === HIT_TURNOUT_C) return { x: t.c[0], y: t.c[1] };
  if (type === HIT_TURNOUT_D) return { x: t.cen[0], y: t.cen[1] };
  return null;
}

function render() {
  const ctx = canvas.getContext("2d");
  const cssW = parseFloat(canvas.style.width);
  const cssH = parseFloat(canvas.style.height);
  ctx.save();
  ctx.fillStyle = COLORS.background;
  ctx.fillRect(0, 0, cssW, cssH);

  // Panel bounds outline so the user can see the panel dimensions
  // even while panning off-canvas. Use the LIVE model width/height
  // so resize operations are reflected the moment the server replies.
  const w = (state.model && state.model.width) || panelWidth;
  const h = (state.model && state.model.height) || panelHeight;
  const tl = worldToScreen(0, 0);
  const br = worldToScreen(w, h);
  ctx.strokeStyle = "#ccc";
  ctx.setLineDash([4, 3]);
  ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
  ctx.setLineDash([]);

  if (!state.model) { ctx.restore(); return; }

  drawSegments(ctx);
  drawPoints(ctx);
  drawTurnouts(ctx);
  drawIcons(ctx);
  drawLabels(ctx);
  drawSelection(ctx);
  drawPendingSegment(ctx);
  ctx.restore();

  updateZoomReadout();
}

function drawSegments(ctx) {
  for (const seg of state.model.segments) {
    const a = resolveEndpoint(seg.c1.ref, seg.c1.type);
    const b = resolveEndpoint(seg.c2.ref, seg.c2.type);
    if (!a || !b) continue;
    const sa = worldToScreen(a.x, a.y);
    const sb = worldToScreen(b.x, b.y);
    ctx.strokeStyle = seg.mainline ? COLORS.trackMain : COLORS.track;
    ctx.lineWidth = (seg.mainline ? 4 : 2) * state.view.scale;
    if (seg.dashed) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(sa.x, sa.y);
    ctx.lineTo(sb.x, sb.y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

function drawPoints(ctx) {
  for (const p of state.model.points) {
    const sp = worldToScreen(p.x, p.y);
    const selected = state.selection && state.selection.kind === "point"
                    && state.selection.id === p.ident;
    ctx.fillStyle = selected ? COLORS.pointSelected : COLORS.point;
    ctx.beginPath();
    ctx.arc(sp.x, sp.y, selected ? 5 : 3, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawTurnouts(ctx) {
  const labelFontPx = Math.max(9, 11 * state.view.scale);
  for (const t of state.model.turnouts) {
    const cen = worldToScreen(t.cen[0], t.cen[1]);
    const a = worldToScreen(t.a[0], t.a[1]);
    const b = worldToScreen(t.b[0], t.b[1]);
    const c = worldToScreen(t.c[0], t.c[1]);
    ctx.lineWidth = 3 * state.view.scale;
    ctx.strokeStyle = COLORS.turnoutStock;
    ctx.beginPath(); ctx.moveTo(cen.x, cen.y); ctx.lineTo(a.x, a.y); ctx.stroke();
    ctx.strokeStyle = COLORS.turnoutIdle;
    ctx.beginPath(); ctx.moveTo(cen.x, cen.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cen.x, cen.y); ctx.lineTo(c.x, c.y); ctx.stroke();
    // Pivot handle.
    ctx.strokeStyle = "#888";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cen.x, cen.y, 6, 0, Math.PI * 2); ctx.stroke();
    // Leg endpoint dots. These are the click targets the Segment
    // tool snaps to -- drawing them makes the connection points
    // visible so the operator doesn't have to guess where to click.
    // Styled to match PositionablePoint dots (muted grey) so they
    // read as "attach point", not an independent entity.
    ctx.fillStyle = COLORS.point;
    for (const leg of [a, b, c]) {
      ctx.beginPath();
      ctx.arc(leg.x, leg.y, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    // Label: bean sysname when assigned, else the layout ident so
    // there's always SOMETHING visible to tell turnouts apart.
    ctx.font = `${labelFontPx}px monospace`;
    ctx.textBaseline = "top";
    drawIconLabel(ctx, t.turnout || t.ident, t.cen[0] + 10, t.cen[1] + 8);
  }
}

function drawIcons(ctx) {
  // A small text label beside each icon so the operator can see the
  // bean system name without having to click. Font size tracks the
  // view scale so the label stays legible when zoomed out but doesn't
  // overwhelm the icon when zoomed in.
  const labelFontPx = Math.max(9, 11 * state.view.scale);
  ctx.font = `${labelFontPx}px monospace`;
  ctx.textBaseline = "top";

  for (const s of state.model.sensor_icons) {
    const c = worldToScreen(s.x + 8, s.y + 8);
    ctx.fillStyle = COLORS.sensor;
    ctx.beginPath();
    ctx.arc(c.x, c.y, 7 * state.view.scale, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#222";
    ctx.lineWidth = 1;
    ctx.stroke();
    drawIconLabel(ctx, s.sensor, s.x + 20, s.y);
  }
  for (const t of state.model.turnout_icons) {
    const p = worldToScreen(t.x, t.y);
    const w = 16 * state.view.scale;
    ctx.strokeStyle = "#222";
    ctx.lineWidth = 1;
    ctx.strokeRect(p.x, p.y, w, w);
    drawIconLabel(ctx, t.turnout, t.x + 20, t.y);
  }
  for (const s of state.model.signal_icons) {
    const c = worldToScreen(s.x + 6, s.y + 6);
    ctx.fillStyle = COLORS.signal;
    ctx.beginPath();
    ctx.arc(c.x, c.y, 6 * state.view.scale, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#222";
    ctx.stroke();
    drawIconLabel(ctx, s.sysname, s.x + 16, s.y);
  }
  for (const m of state.model.memory_icons) {
    const p = worldToScreen(m.x, m.y);
    const w = 48 * state.view.scale;
    const h = 16 * state.view.scale;
    ctx.fillStyle = COLORS.memoryBg;
    ctx.strokeStyle = COLORS.memoryBorder;
    ctx.fillRect(p.x, p.y, w, h);
    ctx.strokeRect(p.x, p.y, w, h);
    ctx.fillStyle = "#333";
    ctx.font = `${Math.max(8, 10 * state.view.scale)}px monospace`;
    ctx.textBaseline = "top";
    ctx.fillText(m.memory, p.x + 2, p.y + 2);
  }
  // Restore the label font for drawTurnouts' potential labels (none
  // today, but defensive -- the next draw pass starts clean).
  ctx.font = `${labelFontPx}px monospace`;
}

function drawIconLabel(ctx, text, worldX, worldY) {
  if (!text) return;
  const sp = worldToScreen(worldX, worldY);
  // Backing fill so the label stays readable over segments.
  const metrics = ctx.measureText(text);
  const pad = 2;
  ctx.fillStyle = "rgba(255,255,255,0.75)";
  ctx.fillRect(sp.x - pad, sp.y - pad, metrics.width + pad * 2,
               parseFloat(ctx.font) + pad);
  ctx.fillStyle = "#222";
  ctx.fillText(text, sp.x, sp.y);
}

function drawLabels(ctx) {
  ctx.fillStyle = COLORS.label;
  ctx.font = `${Math.max(9, 12 * state.view.scale)}px sans-serif`;
  ctx.textBaseline = "top";
  for (const l of state.model.text_labels) {
    const p = worldToScreen(l.x, l.y);
    ctx.fillText(l.text, p.x, p.y);
  }
}

function drawPendingSegment(ctx) {
  if (!state.pendingSegment) return;
  const xy = state.pendingSegment.type === HIT_POS_POINT
    ? (() => {
        const p = state.model.points.find((p) => p.ident === state.pendingSegment.ref);
        return p ? [p.x, p.y] : null;
      })()
    : turnoutLegXy(state.pendingSegment.ref, state.pendingSegment.type);
  if (!xy) return;
  const sp = worldToScreen(xy[0], xy[1]);
  ctx.strokeStyle = COLORS.selection;
  ctx.lineWidth = 2;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.arc(sp.x, sp.y, 10, 0, Math.PI * 2);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawSelection(ctx) {
  if (!state.selection) return;
  if (state.selection.kind === "segment") {
    const seg = state.model.segments.find((s) => s.ident === state.selection.id);
    if (!seg) return;
    const a = resolveEndpoint(seg.c1.ref, seg.c1.type);
    const b = resolveEndpoint(seg.c2.ref, seg.c2.type);
    if (!a || !b) return;
    const sa = worldToScreen(a.x, a.y);
    const sb = worldToScreen(b.x, b.y);
    ctx.strokeStyle = COLORS.selection;
    ctx.lineWidth = 5 * state.view.scale;
    ctx.beginPath();
    ctx.moveTo(sa.x, sa.y); ctx.lineTo(sb.x, sb.y); ctx.stroke();
  } else if (state.selection.kind === "turnout") {
    const t = state.model.turnouts.find((t) => t.ident === state.selection.id);
    if (!t) return;
    const cen = worldToScreen(t.cen[0], t.cen[1]);
    ctx.strokeStyle = COLORS.selection;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cen.x, cen.y, 9, 0, Math.PI * 2);
    ctx.stroke();
  } else if (state.selection.kind === "icon") {
    const fieldByKind = {
      sensoricon: "sensor_icons", turnouticon: "turnout_icons",
      signalicon: "signal_icons", memoryicon: "memory_icons",
    };
    const keyAttr = {
      sensoricon: "sensor", turnouticon: "turnout",
      signalicon: "sysname", memoryicon: "memory",
    };
    const field = fieldByKind[state.selection.iconKind];
    const key = keyAttr[state.selection.iconKind];
    if (!field) return;
    const icon = state.model[field].find((x) => x[key] === state.selection.key);
    if (!icon) return;
    const box = iconBox(state.selection.iconKind, icon);
    if (!box) return;
    const tl = worldToScreen(box.x - 2, box.y - 2);
    const br = worldToScreen(box.x + box.w + 2, box.y + box.h + 2);
    ctx.strokeStyle = COLORS.selection;
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 2]);
    ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
    ctx.setLineDash([]);
  }
}

// ---- hit testing --------------------------------------------------------

// Icon bounding boxes in world-coords (unscaled). Sensor is a 16x16
// square centered on (x+8, y+8); turnout icon the same; signal the
// same but offset; memory a 48x16 strip anchored at (x, y). Picking
// uses these boxes so the whole glyph is clickable, not just the
// 1px center.
function iconBox(kind, icon) {
  if (kind === "sensoricon")  return { x: icon.x, y: icon.y, w: 16, h: 16 };
  if (kind === "turnouticon") return { x: icon.x, y: icon.y, w: 16, h: 16 };
  if (kind === "signalicon")  return { x: icon.x, y: icon.y, w: 12, h: 12 };
  if (kind === "memoryicon")  return { x: icon.x, y: icon.y, w: 48, h: 16 };
  return null;
}

function hitTest(worldX, worldY) {
  if (!state.model) return null;
  // Icons first: a sensor icon overlaid on a segment should pick the
  // icon, not the track. Walk kinds from most-to-least common.
  const iconKinds = [
    ["sensor_icons", "sensoricon", "sensor"],
    ["turnout_icons", "turnouticon", "turnout"],
    ["signal_icons", "signalicon", "sysname"],
    ["memory_icons", "memoryicon", "memory"],
  ];
  for (const [field, kind, keyAttr] of iconKinds) {
    for (const icon of state.model[field]) {
      const box = iconBox(kind, icon);
      if (!box) continue;
      if (worldX >= box.x && worldX <= box.x + box.w
          && worldY >= box.y && worldY <= box.y + box.h) {
        return { kind: "icon", iconKind: kind, key: icon[keyAttr], id: icon[keyAttr], obj: icon };
      }
    }
  }
  // Points next -- small targets.
  for (const p of state.model.points) {
    if (Math.hypot(worldX - p.x, worldY - p.y) <= 6 / state.view.scale) {
      return { kind: "point", id: p.ident, obj: p };
    }
  }
  // Turnout pivots.
  for (const t of state.model.turnouts) {
    if (Math.hypot(worldX - t.cen[0], worldY - t.cen[1])
        <= 8 / state.view.scale) {
      return { kind: "turnout", id: t.ident, obj: t };
    }
  }
  // Segments -- distance to the line is cheap.
  for (const seg of state.model.segments) {
    const a = resolveEndpoint(seg.c1.ref, seg.c1.type);
    const b = resolveEndpoint(seg.c2.ref, seg.c2.type);
    if (!a || !b) continue;
    const d = pointToSegmentDistance(worldX, worldY, a.x, a.y, b.x, b.y);
    if (d <= 4 / state.view.scale) {
      return { kind: "segment", id: seg.ident, obj: seg };
    }
  }
  return null;
}

function pointToSegmentDistance(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx;
  const cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

// ---- input --------------------------------------------------------------

function canvasPos(ev) {
  const rect = canvas.getBoundingClientRect();
  return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
}

function onPointerDown(ev) {
  canvas.focus();
  const { x: px, y: py } = canvasPos(ev);
  const world = screenToWorld(px, py);

  if (state.tool === "pan" || ev.button === 1 /* middle */) {
    state.drag = {
      kind: "pan",
      startX: px, startY: py,
      origOffsetX: state.view.offsetX,
      origOffsetY: state.view.offsetY,
    };
    return;
  }

  const hit = hitTest(world.x, world.y);

  if (state.tool === "select") {
    state.selection = hit
      ? (hit.kind === "icon"
          ? { kind: "icon", iconKind: hit.iconKind, key: hit.key, id: hit.key }
          : { kind: hit.kind, id: hit.id })
      : null;
    if (hit && hit.kind === "point") {
      // Start a drag-to-move on the point. Final XY is applied in
      // onPointerUp with a single movePoint op.
      state.drag = {
        kind: "move-point",
        pointId: hit.id,
        origX: hit.obj.x, origY: hit.obj.y,
        startWorld: world,
      };
    } else if (hit && hit.kind === "icon") {
      state.drag = {
        kind: "move-icon",
        iconKind: hit.iconKind,
        key: hit.key,
        obj: hit.obj,
        origX: hit.obj.x, origY: hit.obj.y,
        startWorld: world,
      };
    } else if (hit && hit.kind === "turnout") {
      // Identify Points sitting on (or essentially on) a leg of this
      // turnout so they ride along during the drag and segments
      // through them stay visually connected. 2-world-px tolerance
      // forgives floating-point drift from the XML round trip AND
      // catches points the operator placed by hand very close to a
      // leg -- those read as "connected" visually so they should
      // behave that way.
      const t = hit.obj;
      const sticky = [];
      const STICK_PX = 2;
      for (const p of state.model.points) {
        for (const leg of [t.a, t.b, t.c]) {
          if (Math.hypot(p.x - leg[0], p.y - leg[1]) <= STICK_PX) {
            sticky.push({ p, origX: p.x, origY: p.y });
            break;
          }
        }
      }
      state.drag = {
        kind: "move-turnout",
        ident: hit.id,
        obj: hit.obj,
        startWorld: world,
        // Snapshot original coords so pointer-up can diff and decide
        // whether the gesture was a real drag vs a click.
        orig: {
          cen: [...hit.obj.cen],
          a: [...hit.obj.a], b: [...hit.obj.b], c: [...hit.obj.c],
        },
        sticky,
      };
    }
    updateDrawer();
    render();
    return;
  }

  // Icon placement tools. Prompt once for the bean system name; the
  // user types e.g. "IS1" for sensor or leaves blank to cancel.
  const iconTool = {
    "add-sensor-icon":  { kind: "sensoricon", label: "Sensor system name (e.g. IS1)" },
    "add-turnout-icon": { kind: "turnouticon", label: "Turnout system name (e.g. IT1)" },
    "add-signal-icon":  { kind: "signalmasticon", label: "Signal-mast system name" },
    "add-memory-icon":  { kind: "memoryicon", label: "Memory system name (e.g. IM1)" },
  }[state.tool];
  if (iconTool) {
    const key = window.prompt(iconTool.label, "");
    if (!key || !key.trim()) {
      setStatus("icon placement cancelled");
      return;
    }
    sendPanelEdit({
      op: "addIcon",
      kind: iconTool.kind, key: key.trim(),
      x: round(world.x), y: round(world.y),
    });
    return;
  }

  if (state.tool === "add-point") {
    // Snap to a nearby turnout leg so the new point sits ON the end of
    // existing track -- the user can then draw a segment from that
    // point outward without first having to fiddle with coordinates.
    // Snapping to an existing point is a no-op (would be a duplicate).
    const snap = findAnchorNear(world.x, world.y);
    let x = round(world.x);
    let y = round(world.y);
    if (snap && snap.type === HIT_POS_POINT) {
      setStatus(`already a point here (${snap.ref})`);
      return;
    }
    if (snap) {
      const legXy = turnoutLegXy(snap.ref, snap.type);
      if (legXy) { x = round(legXy[0]); y = round(legXy[1]); }
    }
    const ident = nextIdent("P");
    locallyAddPoint(ident, x, y);
    sendPanelEdit({ op: "addPoint", ident, x, y });
    return;
  }

  if (state.tool === "add-segment") {
    // Two-click sequence. First click MUST land on an existing
    // point or turnout leg -- there's no anchor to hang a segment
    // off otherwise. Second click can be:
    //   * an existing anchor -> draw segment between the two anchors;
    //   * empty space -> auto-create a new Point at the click AND draw
    //     a segment from the first anchor to the new Point. This
    //     collapses the common "turnout leg out to a new endpoint"
    //     flow into one gesture.
    const anchor = findAnchorNear(world.x, world.y);
    if (!state.pendingSegment) {
      if (!anchor) {
        setStatus("segment: click an existing point or turnout leg to start");
        return;
      }
      state.pendingSegment = anchor;
      setStatus(`segment: picked ${anchor.ref}, click second endpoint`
              + ` (empty spot auto-creates a point)`);
      render();
      return;
    }
    let c2ref, c2type;
    if (anchor) {
      c2ref = anchor.ref;
      c2type = anchor.type;
    } else {
      const newPointIdent = nextIdent("P");
      const px = round(world.x);
      const py = round(world.y);
      locallyAddPoint(newPointIdent, px, py);
      sendPanelEdit({ op: "addPoint", ident: newPointIdent, x: px, y: py });
      c2ref = newPointIdent;
      c2type = HIT_POS_POINT;
    }
    const segIdent = nextIdent("S");
    locallyAddSegment(segIdent, state.pendingSegment, { ref: c2ref, type: c2type });
    sendPanelEdit({
      op: "addSegment", ident: segIdent,
      connect1_name: state.pendingSegment.ref,
      type1: state.pendingSegment.type,
      connect2_name: c2ref,
      type2: c2type,
    });
    state.pendingSegment = null;
    return;
  }

  if (state.tool === "add-turnout") {
    // Drop a right-hand turnout centered on the click. Geometry is
    // a 60x60 box around the center -- the operator can drag legs
    // later once the edit-leg tool lands.
    const ident = nextIdent("T");
    const cx = round(world.x);
    const cy = round(world.y);
    sendPanelEdit({
      op: "addTurnout", ident,
      turnout_name: "",
      xcen: cx, ycen: cy,
      xa: cx - 30, ya: cy,
      xb: cx + 30, yb: cy,
      xc: cx + 15, yc: cy + 25,
    });
    return;
  }
}

function onPointerMove(ev) {
  if (!state.drag) return;
  const { x: px, y: py } = canvasPos(ev);
  if (state.drag.kind === "pan") {
    state.view.offsetX = state.drag.origOffsetX + (px - state.drag.startX);
    state.view.offsetY = state.drag.origOffsetY + (py - state.drag.startY);
    render();
  } else if (state.drag.kind === "move-point") {
    // Live-preview the move by mutating the in-memory model; the
    // authoritative write happens on pointerUp.
    const world = screenToWorld(px, py);
    const p = state.model.points.find((p) => p.ident === state.drag.pointId);
    if (p) { p.x = round(world.x); p.y = round(world.y); }
    render();
  } else if (state.drag.kind === "move-icon") {
    const world = screenToWorld(px, py);
    state.drag.obj.x = round(world.x);
    state.drag.obj.y = round(world.y);
    render();
  } else if (state.drag.kind === "move-turnout") {
    // Translate the whole turnout by the drag delta: cen moves to the
    // pointer, A/B/C shift by the same vector so leg geometry stays
    // intact. Sticky points (ones that started co-located with a leg)
    // ride along so segments through them stay connected. The
    // authoritative save goes out on pointer-up as one updateTurnout
    // plus one movePoint per sticky point.
    const world = screenToWorld(px, py);
    const dx = round(world.x) - state.drag.orig.cen[0];
    const dy = round(world.y) - state.drag.orig.cen[1];
    const o = state.drag.orig;
    const t = state.drag.obj;
    t.cen = [o.cen[0] + dx, o.cen[1] + dy];
    t.a   = [o.a[0]   + dx, o.a[1]   + dy];
    t.b   = [o.b[0]   + dx, o.b[1]   + dy];
    t.c   = [o.c[0]   + dx, o.c[1]   + dy];
    for (const s of state.drag.sticky) {
      s.p.x = s.origX + dx;
      s.p.y = s.origY + dy;
    }
    render();
  }
}

function onPointerUp(ev) {
  if (!state.drag) return;
  const drag = state.drag;
  state.drag = null;
  if (drag.kind === "move-point") {
    const p = state.model.points.find((p) => p.ident === drag.pointId);
    if (!p) return;
    if (p.x === drag.origX && p.y === drag.origY) return;   // click without drag
    sendPanelEdit({
      op: "movePoint", ident: drag.pointId, x: p.x, y: p.y,
    });
  } else if (drag.kind === "move-icon") {
    if (drag.obj.x === drag.origX && drag.obj.y === drag.origY) return;
    sendPanelEdit({
      op: "moveIcon", kind: drag.iconKind, key: drag.key,
      x: drag.obj.x, y: drag.obj.y,
    });
  } else if (drag.kind === "move-turnout") {
    const t = drag.obj;
    if (t.cen[0] === drag.orig.cen[0] && t.cen[1] === drag.orig.cen[1]) {
      return;   // click without drag
    }
    sendPanelEdit({
      op: "updateTurnout", ident: drag.ident,
      xcen: t.cen[0], ycen: t.cen[1],
      xa: t.a[0], ya: t.a[1],
      xb: t.b[0], yb: t.b[1],
      xc: t.c[0], yc: t.c[1],
    });
    // Persist each sticky point's new position so segments routed
    // through them stay visually connected after the server reload.
    for (const s of drag.sticky) {
      sendPanelEdit({
        op: "movePoint", ident: s.p.ident, x: s.p.x, y: s.p.y,
      });
    }
  }
}

// ---- edit tool helpers --------------------------------------------------

function sendPanelEdit(data) {
  wsSend({
    type: "panel-edit", method: "post",
    data: { panel: panelName, ...data },
  });
}

function nextIdent(prefix) {
  // Simple client-generated ident: biggest existing-number + 1.
  if (!state.model) return `${prefix}1`;
  const pool = [];
  if (prefix === "P") pool.push(...state.model.points.map((x) => x.ident));
  else if (prefix === "S") pool.push(...state.model.segments.map((x) => x.ident));
  else if (prefix === "T") pool.push(...state.model.turnouts.map((x) => x.ident));
  let max = 0;
  for (const id of pool) {
    const m = String(id).match(/^(\D*)(\d+)$/);
    if (m && m[1] === prefix) max = Math.max(max, parseInt(m[2], 10));
  }
  return `${prefix}${max + 1}`;
}

function round(x) {
  // JMRI coordinates are usually integer pixels; round to keep
  // the file diff clean.
  return Math.round(x);
}

function turnoutLegXy(turnoutIdent, hitType) {
  const t = state.model && state.model.turnouts.find(
    (t) => t.ident === turnoutIdent,
  );
  if (!t) return null;
  if (hitType === HIT_TURNOUT_A) return t.a;
  if (hitType === HIT_TURNOUT_B) return t.b;
  if (hitType === HIT_TURNOUT_C) return t.c;
  return null;
}

// Optimistic local-model updates. We mutate state.model so subsequent
// nextIdent() calls in the same gesture pick unique names; the server's
// echo of the panel-edit reply triggers a full loadModel() that
// overwrites these with the authoritative version.
function locallyAddPoint(ident, x, y) {
  if (!state.model) return;
  state.model.points.push({ ident, x, y });
  render();
}

function locallyAddSegment(ident, a, b) {
  if (!state.model) return;
  state.model.segments.push({
    ident,
    c1: { ref: a.ref, type: a.type },
    c2: { ref: b.ref, type: b.type },
    mainline: false, dashed: false, block: null,
  });
  render();
}

function findAnchorNear(worldX, worldY) {
  if (!state.model) return null;
  // Points first.
  for (const p of state.model.points) {
    if (Math.hypot(worldX - p.x, worldY - p.y) <= 12 / state.view.scale) {
      return { ref: p.ident, type: HIT_POS_POINT };
    }
  }
  // Turnout legs.
  for (const t of state.model.turnouts) {
    const candidates = [
      { ref: t.ident, type: HIT_TURNOUT_A, xy: t.a },
      { ref: t.ident, type: HIT_TURNOUT_B, xy: t.b },
      { ref: t.ident, type: HIT_TURNOUT_C, xy: t.c },
    ];
    for (const cand of candidates) {
      if (Math.hypot(worldX - cand.xy[0], worldY - cand.xy[1])
          <= 12 / state.view.scale) {
        return { ref: cand.ref, type: cand.type };
      }
    }
  }
  return null;
}

function transformTurnout(action) {
  if (!state.selection || state.selection.kind !== "turnout") {
    setStatus("select a turnout first");
    return;
  }
  const t = state.model.turnouts.find((t) => t.ident === state.selection.id);
  if (!t) return;
  const cx = t.cen[0], cy = t.cen[1];
  const xform = (p) => {
    const [x, y] = p;
    const rx = x - cx, ry = y - cy;
    switch (action) {
      case "rot-cw":  return [round(cx - ry), round(cy + rx)];
      case "rot-ccw": return [round(cx + ry), round(cy - rx)];
      case "rot-180": return [round(cx - rx), round(cy - ry)];
      case "flip-h":  return [round(cx - rx), round(cy + ry)];
      case "flip-v":  return [round(cx + rx), round(cy - ry)];
      default:        return [x, y];
    }
  };
  // Sticky points: any Point sitting on (or very close to) a leg
  // follows the same transform so segments through those points
  // stay connected. See the drag path for the matching rationale.
  const sticky = [];
  const STICK_PX = 2;
  for (const p of state.model.points) {
    for (const leg of [t.a, t.b, t.c]) {
      if (Math.hypot(p.x - leg[0], p.y - leg[1]) <= STICK_PX) {
        sticky.push(p);
        break;
      }
    }
  }
  t.a = xform(t.a);
  t.b = xform(t.b);
  t.c = xform(t.c);
  for (const p of sticky) {
    const [nx, ny] = xform([p.x, p.y]);
    p.x = nx;
    p.y = ny;
  }
  render();
  sendPanelEdit({
    op: "updateTurnout", ident: t.ident,
    xcen: t.cen[0], ycen: t.cen[1],
    xa: t.a[0], ya: t.a[1],
    xb: t.b[0], yb: t.b[1],
    xc: t.c[0], yc: t.c[1],
  });
  for (const p of sticky) {
    sendPanelEdit({
      op: "movePoint", ident: p.ident, x: p.x, y: p.y,
    });
  }
}

function deleteSelection() {
  const sel = state.selection;
  if (!sel) { setStatus("nothing to delete"); return; }
  if (sel.kind === "icon") {
    sendPanelEdit({ op: "removeIcon", kind: sel.iconKind, key: sel.key });
    state.selection = null;
    updateDrawer();
    return;
  }
  const opByKind = {
    point: "removePoint",
    segment: "removeSegment",
    turnout: "removeTurnout",
  };
  const op = opByKind[sel.kind];
  if (!op) return;
  sendPanelEdit({ op, ident: sel.id });
  state.selection = null;
  updateDrawer();
}

function onWheel(ev) {
  ev.preventDefault();
  const { x: px, y: py } = canvasPos(ev);
  const worldBefore = screenToWorld(px, py);
  const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  setZoom(state.view.scale * factor, worldBefore, px, py);
}

function setZoom(newScale, worldAnchor, screenPx, screenPy) {
  newScale = Math.max(0.1, Math.min(8, newScale));
  if (worldAnchor === undefined) {
    worldAnchor = screenToWorld(canvas.clientWidth / 2, canvas.clientHeight / 2);
    screenPx = canvas.clientWidth / 2;
    screenPy = canvas.clientHeight / 2;
  }
  state.view.scale = newScale;
  state.view.offsetX = screenPx - worldAnchor.x * newScale;
  state.view.offsetY = screenPy - worldAnchor.y * newScale;
  render();
}

function resetView() {
  const wrap = canvas.parentElement;
  const w = (state.model && state.model.width) || panelWidth;
  const h = (state.model && state.model.height) || panelHeight;
  const sx = (wrap.clientWidth - 40) / w;
  const sy = (wrap.clientHeight - 40) / h;
  const s = Math.max(0.1, Math.min(sx, sy));
  state.view.scale = s;
  state.view.offsetX = 20;
  state.view.offsetY = 20;
  render();
}

function updateZoomReadout() {
  if (!zoomReadout) return;
  zoomReadout.textContent = `${Math.round(state.view.scale * 100)}%`;
}

// ---- drawer -------------------------------------------------------------

function updateDrawer() {
  if (!drawerBody) return;
  const sel = state.selection;
  if (!sel || !state.model) {
    drawerBody.innerHTML = '<span class="muted small">Nothing selected.</span>';
    return;
  }
  if (sel.kind === "point") {
    const p = state.model.points.find((p) => p.ident === sel.id);
    if (!p) return;
    drawerBody.innerHTML = `
      <div><strong>Point</strong> <code>${escape(p.ident)}</code></div>
      <div class="small muted">(${p.x}, ${p.y})</div>`;
    return;
  }
  if (sel.kind === "segment") {
    const s = state.model.segments.find((s) => s.ident === sel.id);
    if (!s) return;
    drawerBody.innerHTML = `
      <div><strong>Segment</strong> <code>${escape(s.ident)}</code></div>
      <div class="small">block: <code>${escape(s.block || "-")}</code></div>
      <div class="small">mainline: ${s.mainline ? "yes" : "no"}</div>`;
    return;
  }
  if (sel.kind === "turnout") {
    const t = state.model.turnouts.find((t) => t.ident === sel.id);
    if (!t) return;
    drawerBody.innerHTML = `
      <div><strong>Turnout</strong> <code>${escape(t.ident)}</code></div>
      <div class="small">bean: <code>${escape(t.turnout || "-")}</code></div>
      <div class="small">block: <code>${escape(t.block || "-")}</code></div>
      <div class="muted small" style="margin-top:0.4rem">
        drag pivot to move
      </div>
      <div class="row" style="gap:0.25rem;flex-wrap:wrap;margin-top:0.3rem">
        <button type="button" data-turnout-action="rot-cw"
                title="Rotate 90 clockwise">&#8635;</button>
        <button type="button" data-turnout-action="rot-ccw"
                title="Rotate 90 counter-clockwise">&#8634;</button>
        <button type="button" data-turnout-action="rot-180"
                title="Rotate 180">180&deg;</button>
        <button type="button" data-turnout-action="flip-h"
                title="Flip horizontally">&harr;</button>
        <button type="button" data-turnout-action="flip-v"
                title="Flip vertically">&#8597;</button>
      </div>`;
    return;
  }
  if (sel.kind === "icon") {
    drawerBody.innerHTML = `
      <div><strong>${escape(sel.iconKind)}</strong></div>
      <div class="small">bean: <code>${escape(sel.key)}</code></div>
      <div class="muted small">drag to move; Delete to remove</div>`;
    return;
  }
}

function escape(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// ---- toolbar ------------------------------------------------------------

shell.querySelectorAll("[data-tool]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tool = btn.dataset.tool;
    shell.querySelectorAll("[data-tool]").forEach((b) => {
      b.classList.toggle("is-active", b === btn);
    });
    state.pendingSegment = null;
  });
});

shell.querySelectorAll("[data-action]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.action === "delete") deleteSelection();
  });
});

// Turnout rotate/flip buttons live inside the drawer and are
// re-rendered on every selection change, so delegate from the drawer
// root rather than per-button attach.
if (drawerBody) {
  drawerBody.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-turnout-action]");
    if (!btn) return;
    transformTurnout(btn.dataset.turnoutAction);
  });
}

canvas.addEventListener("keydown", (ev) => {
  if (ev.key === "Delete" || ev.key === "Backspace") {
    ev.preventDefault();
    deleteSelection();
  } else if (ev.key === "Escape") {
    state.pendingSegment = null;
    state.selection = null;
    updateDrawer();
    render();
  }
});

shell.querySelector('[data-role="zoom-in"]').addEventListener("click", () => {
  setZoom(state.view.scale * 1.25);
});
shell.querySelector('[data-role="zoom-out"]').addEventListener("click", () => {
  setZoom(state.view.scale / 1.25);
});
shell.querySelector('[data-role="zoom-reset"]').addEventListener("click", () => {
  resetView();
});

if (applySizeBtn) {
  applySizeBtn.addEventListener("click", () => {
    const w = parseInt(panelWidthInput.value, 10);
    const h = parseInt(panelHeightInput.value, 10);
    if (!Number.isFinite(w) || !Number.isFinite(h)) {
      setStatus("size must be integers");
      return;
    }
    sendPanelEdit({ op: "setPanelSize", width: w, height: h });
  });
  // Apply on Enter in either input.
  for (const inp of [panelWidthInput, panelHeightInput]) {
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); applySizeBtn.click(); }
    });
  }
}

// ---- bootstrap ----------------------------------------------------------

canvas.addEventListener("pointerdown", onPointerDown);
canvas.addEventListener("pointermove", onPointerMove);
canvas.addEventListener("pointerup", onPointerUp);
canvas.addEventListener("pointerleave", onPointerUp);
canvas.addEventListener("wheel", onWheel, { passive: false });
window.addEventListener("resize", resizeCanvas);

resizeCanvas();
resetView();
openWs();
void loadModel();
