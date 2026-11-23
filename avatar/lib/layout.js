'use strict';
// Where she stands when she points at something on the screen, and how her window is laid out around her.
// Pure geometry with no Electron in it, so it can be unit-tested. Everything is in DIP: screen rectangles are
// Electron's {x, y, width, height}; the layout sent to the renderer uses {x, y, w, h} in window coordinates.

const RING_RADIUS = 26; // the marker's ring around a spot (renderer/marker.js)
const BOX_RING_PAD = 8; // ...or its rounded rectangle, this far outside the thing's box
const MARKER_PAD = 12; // room around the marker for its glow and pulse
const LIFT = 0.06; // her pointing shoulder sits this much of her height above the spot
const REACH_USED = 0.85; // the fingertip stops short of the thing: the arm is never locked straight
const MIN_GAP = 0.05; // ...and at least this much of her height away from it
const BOX_GAP = 0.02; // ...or this much past the edge of the thing's box

// Used until the renderer has measured the model and sent its 'metrics': shoulderY (from the top) and reach
// (shoulder to index fingertip) as fractions of the full-framing viewport's height, shoulderX (the shoulder
// joint's offset from her centre line) likewise, bodyWidth (the viewport's width over its height) and seatY (what
// she sits on when she sits: just under her hip joints, from the top). These are what a VRoid figure measures,
// Octavia's included.
const DEFAULT_METRICS = Object.freeze({ shoulderY: 0.27, shoulderX: 0.064, reach: 0.33, bodyWidth: 0.55, seatY: 0.52 });
// A measurement outside these ranges is a bug somewhere, not a strange model: keep the default instead.
const METRIC_RANGES = { shoulderY: [0.05, 0.6], shoulderX: [0, 0.3], reach: [0.1, 0.8], bodyWidth: [0.2, 2], seatY: [0.3, 0.8] };
const PERCH_MIN = 0.45; // sitting on a window, she's at least this much of her pointing height, or she doesn't
const PERCH_SINK = 0.004; // ...and sits this much of her height into its edge, rather than on air
const PERCH_ALONG = 0.62; // ...this far along it from the left
const CAPTION = 150; // DIP at the right end of a window's top edge: its minimise, maximise and close buttons

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function cleanMetrics(metrics) {
  const out = {};
  for (const [key, [lo, hi]] of Object.entries(METRIC_RANGES)) {
    const v = metrics ? metrics[key] : undefined;
    out[key] = Number.isFinite(v) && v >= lo && v <= hi ? v : DEFAULT_METRICS[key];
  }
  return out;
}

// {x, y, width, height} <-> edges {l, t, r, b} (right and bottom exclusive).
const edgesOf = (rect) => ({ l: rect.x, t: rect.y, r: rect.x + rect.width, b: rect.y + rect.height });
const rectOf = (e) => ({ x: e.l, y: e.t, width: e.r - e.l, height: e.b - e.t });
const grow = (e, by) => ({ l: e.l - by, t: e.t - by, r: e.r + by, b: e.b + by });
const union = (a, b) => ({ l: Math.min(a.l, b.l), t: Math.min(a.t, b.t), r: Math.max(a.r, b.r), b: Math.max(a.b, b.b) });

function intersect(a, b) {
  const e = { l: Math.max(a.l, b.l), t: Math.max(a.t, b.t), r: Math.min(a.r, b.r), b: Math.min(a.b, b.b) };
  return e.r > e.l && e.b > e.t ? e : null;
}

function centreOf(rect) {
  return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
}

// The renderer's layout for her home window: the framing viewport is the whole window, as it always was.
function homeLayout(bounds, framing) {
  const { width, height } = bounds;
  return { mode: 'home', framing, width, height, body: { x: 0, y: 0, w: width, h: height }, target: null, box: null, arm: null, from: null };
}

// Where she stands to point at `target` (a point, DIP screen) on `display` ({bounds, workArea}), with `box`
// ({x, y, width, height} or null) the thing's extent when it has a clear one. `bodyHeight` is her full-body
// framing height, `metrics` the renderer's measurements, `home` her home window's bounds and `from` (optional,
// default: the centre of home) the screen point she flies in from.
//
// She stands beside the thing on the side with more room, full body (smaller when that side is too narrow for
// her), inside the display's work area (she can float mid-screen: she flies), with her pointing shoulder a
// little above the spot and far enough to the side that her fingertip stops just short of it. Her body's
// rectangle never contains the spot or the box. The window covers her body and the marker, clamped to the
// display (the spot may be on the taskbar, outside the work area).
//
// Returns {bounds (the window, DIP screen), layout (for the renderer), side ('left'|'right' of the thing),
// body (her framing viewport, DIP screen)}.
function pointLayout({ display, target, box = null, bodyHeight, metrics = null, home = null, from = null }) {
  const area = edgesOf(display.workArea);
  const screen = edgesOf(display.bounds);
  const m = cleanMetrics(metrics);
  // Whole pixels, so "clear of the thing" survives rounding the window to whole pixels.
  const spot = { x: Math.round(clamp(target.x, screen.l, screen.r - 1)), y: Math.round(clamp(target.y, screen.t, screen.b - 1)) };
  // All of her has to fit in the work area, and beside the spot on its roomier side: on a portrait or square
  // monitor a big point_height would otherwise leave no side clear, and she'd stand on the thing.
  const fits = Math.min(area.b - area.t, (area.r - area.l) / m.bodyWidth);
  const beside = Math.max(area.r - (spot.x + 1), spot.x - area.l) / m.bodyWidth;
  const h = Math.max(1, Math.floor(Math.min(Number.isFinite(bodyHeight) ? Math.min(bodyHeight, fits) : fits, beside)));
  const w = Math.max(1, Math.round(h * m.bodyWidth));
  const boxEdges = box ? edgesOf(box) : null;
  const thing = boxEdges && intersect({ l: Math.floor(boxEdges.l), t: Math.floor(boxEdges.t), r: Math.ceil(boxEdges.r), b: Math.ceil(boxEdges.b) }, screen);

  // Places her on one side of the spot (and of the box, when `withBox`), kept inside `room`. `clear` says
  // whether her body's rectangle stays off the thing.
  const place = (side, withBox, room) => {
    const dir = side === 'right' ? 1 : -1;
    // The thing's edge on her side (the spot counts as one pixel wide).
    const near = dir > 0 ? Math.max(spot.x + 1, withBox ? thing.r : -Infinity) : Math.min(spot.x, withBox ? thing.l : Infinity);
    const extent = withBox ? Math.abs(near - spot.x) : 0;
    const gap = Math.max(MIN_GAP * h, extent + BOX_GAP * h);
    const shoulder = spot.x + dir * (REACH_USED * m.reach * h + gap);
    // She faces the viewer, so her pointing shoulder is the one nearer the thing.
    let x = shoulder + dir * m.shoulderX * h - w / 2;
    // Never over the thing, even with odd measurements.
    x = dir > 0 ? Math.max(x, near) : Math.min(x, near - w);
    x = Math.round(clamp(x, room.l, room.r - w));
    const clear = dir > 0 ? x >= near : x + w <= near;
    const y = Math.round(clamp(spot.y - LIFT * h - m.shoulderY * h, room.t, room.b - h));
    return { side, withBox, x, y, clear };
  };
  // More room on the right than on the left of [l, r) (within the work area) => right first.
  const sidesFor = (l, r) => (area.r - r >= l - area.l ? ['right', 'left'] : ['left', 'right']);

  const plans = [];
  if (thing) for (const side of sidesFor(Math.min(spot.x, thing.l), Math.max(spot.x + 1, thing.r))) plans.push([side, true]);
  // A box too wide to stand beside (a whole window, say): point at the spot and forget the box.
  const spotSides = sidesFor(spot.x, spot.x + 1);
  for (const side of spotSides) plans.push([side, false]);
  let placed = null;
  for (const [side, withBox] of plans) {
    const p = place(side, withBox, area);
    if (p.clear) {
      placed = p;
      break;
    }
  }
  // Still nothing (her height is capped so the spot alone always leaves a side clear; this is a guard against
  // odd numbers): hang over the taskbar if that helps; if not, just stand as close as she can.
  if (!placed) {
    for (const side of spotSides) {
      const p = place(side, false, screen);
      if (p.clear) {
        placed = p;
        break;
      }
    }
  }
  if (!placed) placed = place(spotSides[0], false, area);

  const keptBox = placed.withBox ? thing : null;
  const body = { l: placed.x, t: placed.y, r: placed.x + w, b: placed.y + h };
  const marker = keptBox ? grow(keptBox, BOX_RING_PAD + MARKER_PAD) : grow({ l: spot.x, t: spot.y, r: spot.x, b: spot.y }, RING_RADIUS + MARKER_PAD);
  const clipped = intersect(union(body, marker), screen) || body;
  const win = { l: Math.floor(clipped.l), t: Math.floor(clipped.t), r: Math.ceil(clipped.r), b: Math.ceil(clipped.b) };
  const origin = from || (home ? centreOf(home) : null);

  const layout = {
    mode: 'point',
    framing: 'full',
    width: win.r - win.l,
    height: win.b - win.t,
    body: { x: body.l - win.l, y: body.t - win.t, w, h },
    target: { x: spot.x - win.l, y: spot.y - win.t },
    box: keptBox ? { x: keptBox.l - win.l, y: keptBox.t - win.t, w: keptBox.r - keptBox.l, h: keptBox.b - keptBox.t } : null,
    // Standing screen-right of the thing puts it on her right side.
    arm: placed.side,
    from: origin ? { x: Math.round(origin.x - win.l), y: Math.round(origin.y - win.t) } : null,
  };
  return { bounds: rectOf(win), layout, side: placed.side, body: rectOf(body) };
}

// Her window while she flies somewhere with nothing to point at (a victory lap): just her, full body, at most
// `bodyHeight` tall (and no taller than the work area allows), centred on `at` (DIP screen) but kept inside
// `display`'s work area. Returns {bounds (the window, DIP screen), layout (for the renderer), body}.
function flyLayout({ display, at, bodyHeight, metrics = null }) {
  const area = edgesOf(display.workArea);
  const m = cleanMetrics(metrics);
  const fits = Math.min(area.b - area.t, (area.r - area.l) / m.bodyWidth);
  const h = Math.max(1, Math.floor(Number.isFinite(bodyHeight) && bodyHeight > 0 ? Math.min(bodyHeight, fits) : fits));
  const w = Math.max(1, Math.round(h * m.bodyWidth));
  const x = Math.round(clamp(at.x - w / 2, area.l, area.r - w));
  const y = Math.round(clamp(at.y - h / 2, area.t, area.b - h));
  const bounds = { x, y, width: w, height: h };
  const layout = { mode: 'point', framing: 'full', width: w, height: h, body: { x: 0, y: 0, w, h }, target: null, box: null, arm: null, from: null };
  return { bounds, layout, body: { ...bounds } };
}

// A victory lap's stops: three corners of the work area of `display`, clockwise round the screen from the one
// after the corner nearest `home` (her home window's bounds), each a flyLayout; then she flies home.
function lapStops({ display, home, bodyHeight, metrics = null }) {
  const a = display.workArea;
  // Clockwise on screen: top-left, top-right, bottom-right, bottom-left.
  const corners = [
    { x: a.x, y: a.y },
    { x: a.x + a.width, y: a.y },
    { x: a.x + a.width, y: a.y + a.height },
    { x: a.x, y: a.y + a.height },
  ];
  const c = centreOf(home);
  const far = (p) => Math.hypot(p.x - c.x, p.y - c.y);
  const nearest = corners.reduce((best, p, i) => (far(p) < far(corners[best]) ? i : best), 0);
  return [1, 2, 3].map((k) => flyLayout({ display, at: corners[(nearest + k) % 4], bodyHeight, metrics }));
}

// Where she sits on top of a window whose top edge is `edge` ({l, t, r}, DIP screen) on `display`: full body, at
// most `bodyHeight` tall, her seat (metrics.seatY) on the edge and her legs over it, smaller when the room above it
// in the work area (or below it on the screen, for her legs) is short, over the window and clear of its caption
// buttons. Returns {bounds, layout, body}, or null when there's no room for her at PERCH_MIN of her size.
function perchLayout({ display, edge, bodyHeight, metrics = null }) {
  const area = edgesOf(display.workArea);
  const screen = edgesOf(display.bounds);
  const m = cleanMetrics(metrics);
  const full = Number.isFinite(bodyHeight) && bodyHeight > 0 ? bodyHeight : area.b - area.t;
  const below = 1 - m.seatY + PERCH_SINK; // of her height, under the edge: her legs
  const fits = Math.min(area.b - area.t, (area.r - area.l) / m.bodyWidth, (edge.t - area.t) / m.seatY, (screen.b - edge.t) / below);
  const h = Math.floor(Math.min(full, fits));
  if (!(h >= PERCH_MIN * full)) return null;
  const w = Math.max(1, Math.round(h * m.bodyWidth));
  const lo = Math.max(edge.l, area.l);
  const hi = Math.min(edge.r - CAPTION, area.r);
  if (hi - lo < w) return null; // too narrow to sit on
  const x = Math.round(clamp(edge.l + (edge.r - edge.l) * PERCH_ALONG - w / 2, lo, hi - w));
  const y = clamp(Math.round(edge.t - (m.seatY - PERCH_SINK) * h), area.t, screen.b - h); // rounding kept on the screen
  const bounds = { x, y, width: w, height: h };
  const layout = { mode: 'perch', framing: 'full', width: w, height: h, body: { x: 0, y: 0, w, h }, target: null, box: null, arm: null, from: null };
  return { bounds, layout, body: { ...bounds } };
}

// Whether the segment a -> b passes within `pad` of `rect` ({x, y, width, height}); Liang-Barsky clipping.
function segmentCrossesRect(a, b, rect, pad = 0) {
  const e = grow(edgesOf(rect), pad);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  let t0 = 0;
  let t1 = 1;
  const clip = (p, q) => {
    if (p === 0) return q >= 0;
    const u = q / p;
    if (p < 0) t0 = Math.max(t0, u);
    else t1 = Math.min(t1, u);
    return t0 <= t1;
  };
  return clip(-dx, a.x - e.l) && clip(dx, e.r - a.x) && clip(-dy, a.y - e.t) && clip(dy, e.b - a.y);
}

// A window at `pos` ({x, y}) of `size` ({width, height}) moved the least it takes to lie wholly inside `area` (a
// display's work area): the orb's home window, which may be placed by a spot saved for the character (whose
// window is mostly empty space and can hang off the screen's edge on purpose).
function keepInside(pos, size, area) {
  const x = Math.min(Math.max(pos.x, area.x), area.x + Math.max(0, area.width - size.width));
  const y = Math.min(Math.max(pos.y, area.y), area.y + Math.max(0, area.height - size.height));
  return { x: Math.round(x), y: Math.round(y) };
}

module.exports = {
  keepInside,
  DEFAULT_METRICS,
  RING_RADIUS,
  centreOf,
  cleanMetrics,
  flyLayout,
  homeLayout,
  lapStops,
  perchLayout,
  pointLayout,
  segmentCrossesRect,
};
