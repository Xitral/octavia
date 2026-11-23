// Where each part of her marks goes on one monitor: the ring, box or highlight round the thing, an arrow coming in
// from the side with room, its label and its step number. Plain geometry, no DOM, so it's tested in node.
//
// Marks come in the window's own DIPs: {kind, x, y, box: {x, y, width, height} | null, label, step, done, index}.
// Arrows come in from the side facing the middle of the monitor (where there's usually room), and a label goes on
// the first side where it fits on the monitor without covering another mark or label.

export const POINT_R = 16; // the size of a bare point's target
export const PAD = 6;
export const MIN_R = 20;
export const ARROW = 120;
export const GAP = 8;
export const LABEL_H = 28;
export const LABEL_PAD_X = 12;
export const BADGE_R = 11;
export const MARGIN = 10;

const SQRT1_2 = Math.SQRT1_2;

function target(m) {
  if (m.box) return { x: m.box.x, y: m.box.y, w: m.box.width, h: m.box.height };
  // An arrow to a bare point ends right at it; a ring round one needs room to be seen.
  const r = m.kind === 'arrow' ? 2 : POINT_R;
  return { x: m.x - r, y: m.y - r, w: 2 * r, h: 2 * r };
}

function grow(r, by) {
  return { x: r.x - by, y: r.y - by, w: r.w + 2 * by, h: r.h + 2 * by };
}

function centre(r) {
  return { x: r.x + r.w / 2, y: r.y + r.h / 2 };
}

function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function inside(r, view) {
  return r.x >= MARGIN && r.y >= MARGIN && r.x + r.w <= view.width - MARGIN && r.y + r.h <= view.height - MARGIN;
}

function clampRect(r, view) {
  const x = Math.min(Math.max(r.x, MARGIN), Math.max(MARGIN, view.width - MARGIN - r.w));
  const y = Math.min(Math.max(r.y, MARGIN), Math.max(MARGIN, view.height - MARGIN - r.h));
  return { ...r, x, y };
}

// The shape drawn round what's marked, and the rectangle it covers.
function outline(kind, t) {
  if (kind === 'circle') {
    // An ellipse through the target's corners (so a wide button gets a wide ring), a little outside them.
    const c = centre(t);
    const rx = Math.max(MIN_R, (t.w / 2) / SQRT1_2 * 0.92 + PAD);
    const ry = Math.max(MIN_R, (t.h / 2) / SQRT1_2 * 0.92 + PAD);
    return { shape: { type: 'ellipse', cx: c.x, cy: c.y, rx, ry }, covers: { x: c.x - rx, y: c.y - ry, w: 2 * rx, h: 2 * ry } };
  }
  if (kind === 'box' || kind === 'highlight') {
    const r = grow(t, kind === 'box' ? PAD : 3);
    return { shape: { type: 'rect', ...r }, covers: r };
  }
  // An arrow or a label: a small dot on the spot itself.
  const c = centre(t);
  return { shape: { type: 'dot', cx: c.x, cy: c.y, r: 4 }, covers: grow({ x: c.x, y: c.y, w: 0, h: 0 }, 6) };
}

function labelSize(text, step, measure) {
  const badge = step ? 2 * BADGE_R + 6 : 0;
  return { w: Math.ceil(measure(text)) + 2 * LABEL_PAD_X + badge, h: LABEL_H };
}

// Eight directions, the ones nearest `prefer` (radians) first.
function directions(prefer) {
  const dirs = [];
  for (let k = 0; k < 8; k++) dirs.push((k * Math.PI) / 4);
  const off = (a) => Math.abs(Math.atan2(Math.sin(a - prefer), Math.cos(a - prefer)));
  return dirs.sort((a, b) => off(a) - off(b)).map((a) => ({ x: Math.cos(a), y: Math.sin(a) }));
}

// Where a ray from the middle of `r` in direction `d` leaves it.
function edge(r, d) {
  const c = centre(r);
  const tx = Math.abs(d.x) > 1e-6 ? r.w / 2 / Math.abs(d.x) : Infinity;
  const ty = Math.abs(d.y) > 1e-6 ? r.h / 2 / Math.abs(d.y) : Infinity;
  const t = Math.min(tx, ty);
  return { x: c.x + d.x * t, y: c.y + d.y * t };
}

// A label's rectangle hanging off `at`, away from the target along `d`.
function labelAt(at, d, size) {
  let x = at.x - size.w / 2;
  let y = at.y - size.h / 2;
  if (d.x > 0.3) x = at.x + 6;
  else if (d.x < -0.3) x = at.x - 6 - size.w;
  if (d.y > 0.3) y = at.y + 4;
  else if (d.y < -0.3) y = at.y - 4 - size.h;
  return { x, y, w: size.w, h: size.h };
}

function arrowFrom(head, tail) {
  // A gentle bend: the control point sits off the straight line, to its left.
  const dx = head.x - tail.x;
  const dy = head.y - tail.y;
  const len = Math.hypot(dx, dy) || 1;
  const bend = 0.16 * len;
  const cx = (head.x + tail.x) / 2 + (-dy / len) * bend;
  const cy = (head.y + tail.y) / 2 + (dx / len) * bend;
  const angle = Math.atan2(head.y - cy, head.x - cx); // the way the head points
  return { x1: tail.x, y1: tail.y, cx, cy, x2: head.x, y2: head.y, angle };
}

// Lays out every mark for a monitor `view` ({width, height}); `measure(text)` is a label's text width in DIPs.
export function layout(marks, view, measure) {
  const mid = { x: view.width / 2, y: view.height / 2 };
  const parts = marks.map((m) => {
    const t = target(m);
    return { m, t, ...outline(m.kind, t) };
  });
  const taken = parts.map((p) => p.covers);
  return parts.map(({ m, t, shape, covers }) => {
    const out = { index: m.index, kind: m.kind, done: m.done === true, step: m.step || null, shape, label: null, arrow: null, badge: null, leader: null };
    const text = m.label || '';
    const size = text ? labelSize(text, out.step, measure) : null;
    const c = centre(t);
    const prefer = Math.atan2(mid.y - c.y, mid.x - c.x);
    if (m.kind === 'arrow') {
      let best = null;
      for (const d of directions(prefer)) {
        const head = edge(grow(t, m.box ? GAP : 4), d);
        const tail = { x: head.x + d.x * ARROW, y: head.y + d.y * ARROW };
        const lab = size ? labelAt(tail, d, size) : null;
        const tailBox = { x: tail.x - 2, y: tail.y - 2, w: 4, h: 4 };
        const fits = inside(tailBox, view) && (!lab || (inside(lab, view) && !taken.some((r) => overlaps(r, lab))));
        if (fits || !best) best = { head, tail, lab, d };
        if (fits) break;
      }
      out.arrow = arrowFrom(best.head, best.tail);
      if (best.lab) out.label = { ...clampRect(best.lab, view), text };
      else if (out.step) out.badge = { cx: best.tail.x - best.d.x * (BADGE_R + 2), cy: best.tail.y - best.d.y * (BADGE_R + 2), r: BADGE_R, n: out.step };
    } else if (size) {
      const away = m.kind === 'label' ? 22 : GAP;
      const box = m.kind === 'label' && m.box ? t : covers;
      const bc = centre(box);
      const spots = [
        { x: box.x + box.w + away, y: bc.y - size.h / 2 }, // right
        { x: box.x - away - size.w, y: bc.y - size.h / 2 }, // left
        { x: bc.x - size.w / 2, y: box.y + box.h + away }, // below
        { x: bc.x - size.w / 2, y: box.y - away - size.h }, // above
        { x: box.x + box.w + away, y: box.y + box.h + away }, // below right
        { x: box.x - away - size.w, y: box.y - away - size.h }, // above left
      ].map((p) => ({ ...p, w: size.w, h: size.h }));
      // Facing the middle of the monitor first, like an arrow.
      if (c.x > mid.x) [spots[0], spots[1]] = [spots[1], spots[0]];
      const fit = spots.find((r) => inside(r, view) && !taken.some((o) => o !== covers && overlaps(o, r)));
      const lab = clampRect(fit || spots[0], view);
      out.label = { ...lab, text };
      if (m.kind === 'label') {
        // A short line from the dot to the nearest point of the label.
        const nx = Math.min(Math.max(c.x, lab.x), lab.x + lab.w);
        const ny = Math.min(Math.max(c.y, lab.y), lab.y + lab.h);
        out.leader = { x1: c.x, y1: c.y, x2: nx, y2: ny };
      }
    }
    if (out.label) taken.push(out.label);
    if (out.step && !out.label && !out.badge) {
      // The number on the outline's top-left.
      const at = shape.type === 'ellipse' ? { x: shape.cx - shape.rx * SQRT1_2, y: shape.cy - shape.ry * SQRT1_2 } : shape.type === 'rect' ? { x: shape.x, y: shape.y } : { x: shape.cx - 12, y: shape.cy - 12 };
      out.badge = { cx: at.x, cy: at.y, r: BADGE_R, n: out.step };
    }
    return out;
  });
}
