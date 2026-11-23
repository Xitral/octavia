'use strict';
// Where her marks go. Octavia names places in physical desktop pixels (what her screenshots and UI Automation use);
// each overlay window covers one monitor in DIPs, which differ from physical pixels on a scaled monitor. A mark is
// turned into DIPs once, then each window gets the marks that reach into it, in its own coordinates.

const KINDS = new Set(['circle', 'box', 'highlight', 'arrow', 'label']);
const MAX_MARKS = 24;
const LABEL_CHARS = 80;
const LIMIT = 1e6; // no desktop is this big: anything past it is a bad number

const finite = (v) => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) < LIMIT;

// The marks Octavia sent, cleaned: unknown kinds, bad numbers and extras dropped. Returns {marks, dropped}: each
// mark keeps its place in her list (`src`), and `dropped` lists the places of the ones left out, so what she's told
// about a mark is about the right one.
function sanitize(raw) {
  const marks = [];
  const dropped = [];
  (Array.isArray(raw) ? raw : []).forEach((m, src) => {
    if (marks.length >= MAX_MARKS || !m || typeof m !== 'object' || !KINDS.has(m.kind) || !finite(m.x) || !finite(m.y)) {
      dropped.push(src);
      return;
    }
    let box = null;
    if (Array.isArray(m.box) && m.box.length === 4 && m.box.every(finite)) {
      const [x1, y1, x2, y2] = m.box;
      if (x2 > x1 && y2 > y1) box = [x1, y1, x2, y2];
    }
    const label = typeof m.label === 'string' ? m.label.replace(/\s+/g, ' ').trim().slice(0, LABEL_CHARS) : '';
    if (m.kind === 'label' && !label) {
      dropped.push(src);
      return;
    }
    const step = Number.isInteger(m.step) && m.step >= 1 && m.step <= 99 ? m.step : null;
    marks.push({ kind: m.kind, x: m.x, y: m.y, box, label, step, done: m.done === true, src });
  });
  return { marks, dropped };
}

function dipPoint(screen, p) {
  return typeof screen.screenToDipPoint === 'function' ? screen.screenToDipPoint(p) : { x: p.x, y: p.y };
}

// One mark in physical pixels -> DIPs (screen-wide). A box's corners are converted one by one, each by the monitor
// it's on; its far corner is the last pixel inside it, so a box ending at a monitor's edge stays on that monitor.
function toDip(screen, mark) {
  const at = dipPoint(screen, { x: mark.x, y: mark.y });
  let box = null;
  if (mark.box) {
    const [x1, y1, x2, y2] = mark.box;
    const a = dipPoint(screen, { x: x1, y: y1 });
    const b = dipPoint(screen, { x: x2 - 1, y: y2 - 1 });
    const left = Math.min(a.x, b.x);
    const top = Math.min(a.y, b.y);
    box = { x: left, y: top, width: Math.max(1, Math.abs(b.x - a.x) + 1), height: Math.max(1, Math.abs(b.y - a.y) + 1) };
  }
  return { ...mark, x: at.x, y: at.y, box };
}

// The part of the screen a mark (in DIPs) is about: its box, or its point.
function anchor(mark) {
  return mark.box || { x: mark.x, y: mark.y, width: 1, height: 1 };
}

function intersects(a, b) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

function contains(b, x, y) {
  return x >= b.x && x < b.x + b.width && y >= b.y && y < b.y + b.height;
}

// Which monitor (an index into `boundsList`) draws a mark: the one its point is on, else the first its box reaches
// into; -1 when it's on none. Each mark is drawn whole by one window, its arrow and label kept on that monitor, so
// nothing is cut in two at a monitor's edge.
function home(mark, boundsList) {
  const at = boundsList.findIndex((b) => contains(b, mark.x, mark.y));
  return at >= 0 ? at : boundsList.findIndex((b) => intersects(anchor(mark), b));
}

// Each window's marks, in its own coordinates (a list per entry of `boundsList`), and the marks (by index into
// `marks`) on no monitor at all, which can't be drawn. `index` is each mark's place in the list Octavia sent (`src`,
// from sanitize), else in `marks`.
function assign(marks, boundsList) {
  const perWindow = boundsList.map(() => []);
  const missing = [];
  marks.forEach((m, i) => {
    const index = Number.isInteger(m.src) ? m.src : i;
    const at = home(m, boundsList);
    if (at < 0) {
      missing.push(index);
      return;
    }
    const b = boundsList[at];
    const box = m.box ? { ...m.box, x: m.box.x - b.x, y: m.box.y - b.y } : null;
    perWindow[at].push({ ...m, index, x: m.x - b.x, y: m.y - b.y, box });
  });
  return { perWindow, missing };
}

module.exports = { KINDS, MAX_MARKS, sanitize, toDip, home, assign };
