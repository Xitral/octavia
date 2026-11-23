// Elements she draws, in a scene block or over the whole panel (overlays): shapes, lines and arrows, text, emoji,
// paths and cards, each its own SVG group carrying its id, so she can change any one of them later. Pure: element in,
// a {tag, attrs, text, kids} tree out (panel.js makes it real), and where an overlay goes from the boxes of what it's
// placed against.

const COLOUR = /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9., %]{3,40}\)|[a-zA-Z]{3,20})$/;
const PATH = /^[MLHVCSQTAZmlhvcsqtaz0-9 ,.\-eE]{1,4000}$/;
const EMOJI_FONT = '"Segoe UI Emoji", "Segoe UI Symbol", sans-serif';

// A colour only when it's one (Octavia checks them too): nothing that could be url() or anything else.
export const colour = (c) => (typeof c === 'string' && COLOUR.test(c) ? c : null);
const num = (v, d = 0) => (Number.isFinite(Number(v)) ? Number(v) : d);
const el = (tag, attrs = {}, kids = [], text) => ({ tag, attrs, kids, text });

// Text lines for an element: its own line breaks, and wrapped to `width` when it has one (`measure` as diagram.js).
function lines(text, size, width, measure) {
  const out = [];
  for (const part of String(text || '').split('\n')) {
    if (width > 0 && measure) out.push(...measure(part, size, width).lines);
    else out.push(part);
  }
  return out;
}

function paint(attrs, e, { fill = true } = {}) {
  const f = colour(e.fill);
  const s = colour(e.stroke);
  if (fill && f) attrs.fill = f;
  if (s) attrs.stroke = s;
  if (e.stroke_width !== undefined) attrs['stroke-width'] = num(e.stroke_width, 1.5);
  if (e.dashed) attrs['stroke-dasharray'] = '5 4';
  return attrs;
}

function textNodes(e, cx, top, width, measure, cls) {
  const size = num(e.size, 14);
  const lh = size * 1.25;
  const rows = lines(e.text, size, width, measure);
  const start = top - ((rows.length - 1) * lh) / 2;
  const attrs = { 'font-size': size, 'text-anchor': e.anchor || 'middle', 'dominant-baseline': 'central', class: cls };
  if (colour(e.color)) attrs.fill = colour(e.color);
  if (e.bold) attrs['font-weight'] = '700';
  return rows.map((row, k) => el('text', { ...attrs, x: cx.toFixed(1), y: (start + k * lh).toFixed(1) }, [], row));
}

// One element as its SVG group, drawn at `at` ({x, y}: where its own x, y go; overlays are moved there once placed).
// `marker` is the id of this picture's arrowhead.
export function element(e, { marker = 'scene-arrow', measure = null } = {}) {
  const x = num(e.x);
  const y = num(e.y);
  const w = num(e.w);
  const h = num(e.h);
  const kids = [];
  const tone = e.tone ? ` tone-${e.tone}` : '';
  if (e.type === 'rect' || e.type === 'card') {
    // A card is at least as tall as its words (and note) need, so new words never spill out of it.
    const noteH = e.note ? 13 : 0;
    const rows = e.type === 'card' || e.text ? lines(e.text, num(e.size, 14), w - 12, measure).length : 0;
    const tall = e.type === 'card' ? Math.max(h, rows * num(e.size, 14) * 1.25 + noteH + 14) : h;
    kids.push(el('rect', paint({ x, y, width: w, height: tall, rx: num(e.round, 6), class: `shape${tone}` }, e)));
    if (e.type === 'card' || e.text) {
      kids.push(...textNodes(e, x + w / 2, y + (tall - noteH) / 2, w - 12, measure, 'scene-text'));
      if (e.note) {
        kids.push(el('text', { x: (x + w / 2).toFixed(1), y: (y + tall - 9).toFixed(1), 'font-size': 10, 'text-anchor': 'middle', class: 'scene-note' }, [], String(e.note)));
      }
    }
  } else if (e.type === 'ellipse') {
    kids.push(el('ellipse', paint({ cx: x + w / 2, cy: y + h / 2, rx: w / 2, ry: h / 2, class: `shape${tone}` }, e)));
    if (e.text) kids.push(...textNodes(e, x + w / 2, y + h / 2, w - 8, measure, 'scene-text'));
  } else if (e.type === 'line' || e.type === 'arrow') {
    const attrs = paint({ x1: x, y1: y, x2: num(e.x2), y2: num(e.y2), class: `stroke${tone}` }, e, { fill: false });
    if (e.type === 'arrow') attrs['marker-end'] = `url(#${marker})`;
    kids.push(el('line', attrs));
  } else if (e.type === 'path') {
    if (PATH.test(String(e.d || ''))) {
      const attrs = paint({ d: e.d, class: `stroke${tone}` }, e);
      if (!colour(e.fill)) attrs.fill = 'none';
      if (x || y) attrs.transform = `translate(${x} ${y})`; // a path's x, y shift it (so it can be nudged)
      if (e.arrow) attrs['marker-end'] = `url(#${marker})`;
      kids.push(el('path', attrs));
    }
  } else if (e.type === 'text') {
    kids.push(...textNodes(e, x, y, w, measure, 'scene-text'));
  } else if (e.type === 'emoji') {
    const attrs = { x, y, 'font-size': num(e.size, 28), 'text-anchor': 'middle', 'dominant-baseline': 'central', class: 'scene-emoji', style: { 'font-family': EMOJI_FONT } };
    kids.push(el('text', attrs, [], String(e.text || '').slice(0, 16)));
  }
  const attrs = { class: 'el pop', 'data-eid': String(e.id) };
  const turn = num(e.rotate);
  if (turn) {
    const [cx, cy] = e.type === 'line' || e.type === 'arrow' ? [(x + num(e.x2)) / 2, (y + num(e.y2)) / 2] : e.type === 'text' || e.type === 'emoji' ? [x, y] : [x + w / 2, y + h / 2];
    attrs.transform = `rotate(${turn} ${cx.toFixed(1)} ${cy.toFixed(1)})`;
  }
  if (num(e.opacity, 1) < 1) attrs.opacity = Math.max(0, num(e.opacity, 1));
  return el('g', attrs, kids);
}

// The arrowhead a picture's arrows share.
export function arrowhead(id) {
  return el('marker', { id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, [
    el('path', { d: 'M0,0 L10,5 L0,10 L2.5,5 Z', class: 'arrowhead' }),
  ]);
}

// An element's own centre, before it's moved anywhere.
export function ownCentre(e) {
  const x = num(e.x);
  const y = num(e.y);
  if (e.type === 'text' || e.type === 'emoji') return { x, y };
  if (e.type === 'line' || e.type === 'arrow') return { x: (x + num(e.x2)) / 2, y: (y + num(e.y2)) / 2 };
  return { x: x + num(e.w) / 2, y: y + num(e.h) / 2 };
}

// How big an overlay is, for placing it beside something: its box, or an estimate for text and emoji.
export function extent(e) {
  if (e.type === 'emoji') return { w: num(e.size, 28), h: num(e.size, 28) };
  if (e.type === 'text') {
    const size = num(e.size, 14);
    const rows = String(e.text || '').split('\n');
    return { w: num(e.w) || Math.max(...rows.map((r) => r.length)) * size * 0.55, h: rows.length * size * 1.25 };
  }
  return { w: num(e.w), h: num(e.h) };
}

// Where an overlay's centre goes on the panel (its pixels, origin at the content's top left), from `box(address)`
// (an element's or block's box there, or null) and the content's size. Null when what it's placed against isn't
// there (it's then left out, and Octavia told).
export function placeOverlay(o, box, sheet) {
  const { w, h } = extent(o);
  const gap = num(o.gap, 8);
  let c;
  if (o.type === 'line' || o.type === 'arrow') return { x: num(o.dx), y: num(o.dy), absolute: true };
  if (o.place) {
    const [v, hz] = o.place.includes('-') ? o.place.split('-') : ['top', 'bottom'].includes(o.place) ? [o.place, 'center'] : ['middle', o.place];
    const cx = hz === 'left' ? gap + w / 2 : hz === 'right' ? sheet.w - gap - w / 2 : sheet.w / 2;
    const cy = v === 'top' ? gap + h / 2 : v === 'bottom' ? sheet.h - gap - h / 2 : sheet.h / 2;
    c = { x: cx, y: cy };
  } else if (o.near) {
    const b = box(o.near);
    if (!b) return null;
    const side = o.side || 'right';
    const mid = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
    c =
      side === 'above' ? { x: mid.x, y: b.y - gap - h / 2 }
      : side === 'below' ? { x: mid.x, y: b.y + b.h + gap + h / 2 }
      : side === 'left' ? { x: b.x - gap - w / 2, y: mid.y }
      : side === 'right' ? { x: b.x + b.w + gap + w / 2, y: mid.y }
      : mid; // center, over
  } else if (o.between && o.between.length === 2) {
    const [a, b] = o.between.map(box);
    if (!a || !b) return null;
    c = { x: (a.x + a.w / 2 + b.x + b.w / 2) / 2, y: (a.y + a.h / 2 + b.y + b.h / 2) / 2 };
  } else {
    return { x: num(o.dx), y: num(o.dy), absolute: true }; // at its own point, nudged by dx, dy
  }
  return { x: c.x + num(o.dx), y: c.y + num(o.dy) };
}
