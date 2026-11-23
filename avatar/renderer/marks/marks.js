// One monitor's marks: lays them out (layout.js) and draws them as SVG built from DOM nodes, every word a text
// node (nothing from Octavia becomes markup). A mark already up from the last draw keeps still instead of unfolding
// again, so updating the marks while she talks only animates what's new.

import { layout, BADGE_R, LABEL_PAD_X } from './layout.js';

const NS = 'http://www.w3.org/2000/svg';
const FONT = "600 15px 'Segoe UI', system-ui, sans-serif";
const LEAVE_MS = 160;

const host = window.marksHost;
const root = document.getElementById('marks');
let shownKeys = new Set();
let leaving = null;

const canvas = document.createElement('canvas').getContext('2d');
function measure(text) {
  canvas.font = FONT;
  return canvas.measureText(text).width;
}

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (parent) parent.appendChild(node);
  return node;
}

// Draws `shape` (ellipse / rect) as the bloom line, its white core and a faint outer halo.
function ring(g, shape) {
  if (shape.type === 'ellipse') {
    const { cx, cy, rx, ry } = shape;
    el('ellipse', { class: 'halo', cx, cy, rx: rx + 6, ry: ry + 6 }, g);
    el('ellipse', { class: 'glow draw', cx, cy, rx, ry, pathLength: 1 }, g);
    el('ellipse', { class: 'core draw', cx, cy, rx, ry, pathLength: 1 }, g);
  }
}

function brackets(g, r, size) {
  const s = Math.min(size, r.w / 2, r.h / 2);
  const d = [
    `M${r.x},${r.y + s}V${r.y}H${r.x + s}`,
    `M${r.x + r.w - s},${r.y}H${r.x + r.w}V${r.y + s}`,
    `M${r.x + r.w},${r.y + r.h - s}V${r.y + r.h}H${r.x + r.w - s}`,
    `M${r.x + s},${r.y + r.h}H${r.x}V${r.y + r.h - s}`,
  ].join('');
  el('path', { class: 'bracket', d }, g);
}

function badge(g, cx, cy, n) {
  el('circle', { class: 'badge', cx, cy, r: BADGE_R }, g);
  el('text', { class: 'badge-n', x: cx, y: cy + 0.5 }, g).textContent = String(n);
}

function label(g, lab, step) {
  const { x, y, w, h, text } = lab;
  el('rect', { class: 'pill-glow', x, y, width: w, height: h, rx: 7 }, g);
  el('rect', { class: 'pill', x, y, width: w, height: h, rx: 7 }, g);
  let tx = x + LABEL_PAD_X;
  if (step) {
    badge(g, x + 6 + BADGE_R, y + h / 2, step);
    tx += 2 * BADGE_R + 6;
  }
  el('text', { class: 'text', x: tx, y: y + h / 2 }, g).textContent = text;
}

function arrow(g, a) {
  const d = `M${a.x1},${a.y1}Q${a.cx},${a.cy} ${a.x2},${a.y2}`;
  el('path', { class: 'glow draw', d, pathLength: 1, 'stroke-width': 3.4 }, g);
  el('path', { class: 'core draw', d, pathLength: 1 }, g);
  // The head: a chevron whose tip is on the spot.
  const len = 18;
  const wing = 0.45;
  const p = (ang) => `${a.x2 - len * Math.cos(ang)},${a.y2 - len * Math.sin(ang)}`;
  el('path', { class: 'head', d: `M${a.x2},${a.y2}L${p(a.angle - wing)}L${p(a.angle + wing)}Z` }, g);
}

function key(m) {
  return `${m.kind}|${Math.round(m.x)}|${Math.round(m.y)}|${m.label}|${m.step || ''}`;
}

function draw(payload) {
  if (!payload || !Array.isArray(payload.marks)) return;
  clearTimeout(leaving);
  leaving = null;
  root.classList.remove('leaving');
  const view = { width: window.innerWidth, height: window.innerHeight };
  const shapes = layout(payload.marks, view, measure);
  const keys = new Set();
  root.replaceChildren();
  payload.marks.forEach((m, i) => {
    const s = shapes[i];
    const k = key(m);
    keys.add(k);
    const g = el('g', { class: `mark ${s.kind}${s.done ? ' done' : ''}${shownKeys.has(k) ? '' : ' new'}` }, root);
    const sh = s.shape;
    if (sh.type === 'ellipse') ring(g, sh);
    else if (sh.type === 'rect') {
      if (s.kind === 'highlight') {
        el('rect', { class: 'wash', x: sh.x, y: sh.y, width: sh.w, height: sh.h, rx: 3 }, g);
        el('rect', { class: 'scan', x: sh.x, y: sh.y, width: sh.w, height: sh.h, rx: 3 }, g);
        el('rect', { class: 'halo', x: sh.x, y: sh.y, width: sh.w, height: sh.h, rx: 3 }, g);
      } else {
        el('rect', { class: 'halo', x: sh.x - 4, y: sh.y - 4, width: sh.w + 8, height: sh.h + 8, rx: 4 }, g);
        el('rect', { class: 'core draw', x: sh.x, y: sh.y, width: sh.w, height: sh.h, rx: 2, pathLength: 1 }, g);
        brackets(g, sh, 14);
      }
    } else {
      el('circle', { class: 'dot', cx: sh.cx, cy: sh.cy, r: sh.r }, g);
    }
    if (s.leader) el('line', { class: 'leader', ...s.leader }, g);
    if (s.arrow) arrow(g, s.arrow);
    if (s.label) label(g, s.label, s.step);
    if (s.badge) badge(g, s.badge.cx, s.badge.cy, s.badge.n);
  });
  shownKeys = keys;
  // Laid out and in the page: the window can be shown.
  void root.getBoundingClientRect();
  host.painted(payload.id);
}

function clear() {
  shownKeys = new Set();
  if (!root.childNodes.length) return;
  root.classList.add('leaving');
  clearTimeout(leaving);
  leaving = setTimeout(() => {
    leaving = null;
    root.replaceChildren();
    root.classList.remove('leaving');
  }, LEAVE_MS);
}

host.onDraw(draw);
host.onClear(clear);
host.hello().then((payload) => payload && draw(payload));
