// Scratch ink on a panel: strokes the user draws over it with the pen, in the content's own coordinates (so they
// scroll with it), and the eraser that takes away whole strokes it touches. Pure; panel.js draws them on a canvas.

export const MAX_STROKES = 400;
export const MAX_POINTS = 4000; // per stroke

// Points closer than this (px) to the last one add nothing: a still pen doesn't pile up points.
const MIN_STEP = 1.5;

export function addPoint(stroke, p) {
  const last = stroke.points[stroke.points.length - 1];
  if (last && Math.hypot(p.x - last.x, p.y - last.y) < MIN_STEP) return false;
  if (stroke.points.length >= MAX_POINTS) return false;
  stroke.points.push({ x: p.x, y: p.y });
  return true;
}

function distToSegment(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len)) : 0;
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

export function touches(stroke, p, radius) {
  const pts = stroke.points;
  if (pts.length === 1) return Math.hypot(p.x - pts[0].x, p.y - pts[0].y) <= radius;
  for (let k = 1; k < pts.length; k++) if (distToSegment(p, pts[k - 1], pts[k]) <= radius) return true;
  return false;
}

// The strokes left once the eraser at `p` has passed.
export function erase(strokes, p, radius = 8) {
  return strokes.filter((s) => !touches(s, p, radius));
}

// A new stroke, from `p` (past MAX_STROKES the oldest goes).
export function startStroke(strokes, p) {
  if (strokes.length >= MAX_STROKES) strokes.shift();
  const stroke = { points: [{ x: p.x, y: p.y }] };
  strokes.push(stroke);
  return stroke;
}
