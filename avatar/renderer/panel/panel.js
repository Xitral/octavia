// A panel page: builds the spec's blocks (formatted text, a diagram, a chart, facts, steps, a comparison, a picture
// she drew), tells the main process how tall it came out, opens with a short unfold, counts down its time on screen
// (paused while the pointer rests on it or the pen is on), and folds away when closed. When she changes it, only
// what changed is redrawn (patch.js). It can be resized from its edges (the text zooms with it) and written on with
// a scratch pen (ink.js). Every word from the spec goes in as text, never as markup; a picture she drew is shown as
// an image, where nothing in it can run.

import { parseInline } from './markdown.js';
import { inline, markdown } from './rich.js';
import { layoutDiagram, DIAGRAM_FONT } from './diagram.js';
import { buildChart } from './chart.js';
import { planUpdate } from './patch.js';
import { addPoint, erase, startStroke } from './ink.js';
import { buildSim } from './sim.js';
import { element, arrowhead, placeOverlay, ownCentre, extent } from './scene.js';
import { createPlayer } from './player.js';
import { buildFilm } from './film.js';
import { buildLive } from './live.js';
import { CHROME } from '../icons/chrome.js';
import { iconSvg, setIcon } from '../icons/icons.js';

const host = window.panelHost;
const SVG = 'http://www.w3.org/2000/svg';
const SANS = '"Segoe UI", system-ui, sans-serif';
const panel = document.getElementById('panel');
const content = document.getElementById('content');
const titleEl = document.getElementById('title');
const timerEl = document.getElementById('timer');
const TICK_MS = 1000; // a step a second for the draining line: each frame redraws the whole transparent window
const MAX_ZOOM = 1.8; // the text grows with the panel's width up to this
const MIN_FIGURE_SCALE = 0.8; // a diagram shrinks to fit only this far; wider ones scroll sideways

let state = { stayS: 0, left: 0, hovered: false, ticker: null };
let notes = [];

// ---------------------------------------------------------------- small DOM helpers

function h(tag, cls, kids = []) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  for (const k of [].concat(kids)) if (k != null) node.append(k);
  return node;
}

// A {tag, attrs, text, kids} tree (chart.js) as real SVG. `style` holds custom properties, set through the CSSOM.
function svgFrom(desc) {
  const node = document.createElementNS(SVG, desc.tag);
  for (const [k, v] of Object.entries(desc.attrs || {})) {
    if (k === 'style') for (const [p, pv] of Object.entries(v)) node.style.setProperty(p, String(pv));
    else node.setAttribute(k, String(v));
  }
  if (desc.text != null) node.textContent = desc.text;
  for (const kid of desc.kids || []) node.append(svgFrom(kid));
  return node;
}

const measurer = document.createElement('canvas').getContext('2d');
function measure(text, size, maxWidth) {
  measurer.font = `${size}px ${SANS}`;
  const width = (s) => measurer.measureText(s).width;
  const lines = [];
  let line = '';
  for (const word of String(text).split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && width(next) > maxWidth) {
      lines.push(line);
      line = word;
    } else line = next;
  }
  if (line) lines.push(line);
  if (!lines.length) lines.push('');
  return { lines, width: Math.max(...lines.map(width)) };
}


// ---------------------------------------------------------------- the block kinds

function diagram(block, width) {
  const layout = layoutDiagram(block, { measure, fit: { width, height: 460 } });
  notes.push(...layout.notes);
  if (!layout.nodes.length) return null;
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`);
  // Shrunk to fit, but never so far its words get hard to read: past that it scrolls sideways.
  const scale = Math.max(MIN_FIGURE_SCALE, Math.min(1, width / layout.width));
  svg.setAttribute('width', String(Math.round(layout.width * scale)));
  svg.setAttribute('height', String(Math.round(layout.height * scale)));
  svg.setAttribute('class', 'diagram');
  svg.setAttribute('role', 'img');
  const defs = svgFrom({
    tag: 'defs',
    kids: [
      {
        tag: 'marker',
        attrs: { id: 'arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' },
        kids: [{ tag: 'path', attrs: { d: 'M0,0 L10,5 L0,10 L2.5,5 Z', class: 'arrowhead' } }],
      },
    ],
  });
  svg.append(defs);
  const order = new Map(layout.nodes.map((n, k) => [n.id, k]));
  layout.edges.forEach((e, k) => {
    const cls = ['edge', 'draw', e.dashed ? 'dashed' : '', e.back ? 'back' : ''].filter(Boolean).join(' ');
    svg.append(svgFrom({ tag: 'path', attrs: { d: e.d, class: cls, pathLength: 1, 'marker-end': 'url(#arrow)', style: { '--i': (order.get(e.to) ?? k) + 1 } } }));
  });
  layout.edges.forEach((e) => {
    if (!e.label) return;
    const m = measure(e.label, 11.5, 130);
    const w = m.width + 10;
    const hgt = m.lines.length * 14 + 4;
    svg.append(
      svgFrom({
        tag: 'g',
        attrs: { class: 'edge-label pop', transform: `translate(${(e.lx - w / 2).toFixed(1)},${(e.ly - hgt / 2).toFixed(1)})`, style: { '--i': (order.get(e.to) ?? 0) + 1 } },
        kids: [
          { tag: 'rect', attrs: { width: w.toFixed(1), height: hgt, rx: 3 } },
          ...m.lines.map((l, k) => ({ tag: 'text', attrs: { x: (w / 2).toFixed(1), y: 13 + k * 14, 'text-anchor': 'middle' }, text: l })),
        ],
      }),
    );
  });
  const lit = new Set((block.nodes || []).filter((n) => n.highlight).map((n) => String(n.id)));
  layout.nodes.forEach((n, k) => {
    let shape;
    if (n.shape === 'diamond') {
      const pts = [[n.w / 2, 0], [n.w, n.h / 2], [n.w / 2, n.h], [0, n.h / 2]].map((p) => p.map((v) => v.toFixed(1)).join(',')).join(' ');
      shape = { tag: 'polygon', attrs: { points: pts } };
    } else if (n.shape === 'circle') {
      shape = { tag: 'ellipse', attrs: { cx: n.w / 2, cy: n.h / 2, rx: n.w / 2, ry: n.h / 2 } };
    } else {
      shape = { tag: 'rect', attrs: { width: n.w, height: n.h, rx: n.shape === 'round' ? Math.min(n.h / 2, 18) : 4 } };
    }
    const lh = DIAGRAM_FONT.label * DIAGRAM_FONT.line;
    const nh = DIAGRAM_FONT.note * DIAGRAM_FONT.line;
    const textH = n.lines.length * lh + n.noteLines.length * nh + (n.noteLines.length ? 3 : 0);
    let y = n.h / 2 - textH / 2;
    const texts = [];
    for (const l of n.lines) {
      texts.push({ tag: 'text', attrs: { x: n.w / 2, y: (y + lh * 0.78).toFixed(1), 'text-anchor': 'middle', class: 'node-label' }, text: l });
      y += lh;
    }
    if (n.noteLines.length) y += 3;
    for (const l of n.noteLines) {
      texts.push({ tag: 'text', attrs: { x: n.w / 2, y: (y + nh * 0.78).toFixed(1), 'text-anchor': 'middle', class: 'node-note' }, text: l });
      y += nh;
    }
    const node = svgFrom({
      tag: 'g',
      attrs: { class: `node ${n.shape} tone-${n.tone} pop${lit.has(n.id) ? ' lit' : ''}`, transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`, style: { '--i': k } },
      kids: [{ tag: 'title', text: n.note ? `${n.label}: ${n.note}` : n.label }, shape, ...texts],
    });
    node.dataset.id = n.id;
    node.dataset.eid = n.id; // addressable: her edits and overlays find it by id
    svg.append(node);
  });
  return h('div', 'figure', [svg]);
}

// A diagram whose only change is which nodes glow: the nodes are relit where they are, with a soft flash.
function relight(section, block) {
  const lit = new Set((block.nodes || []).filter((n) => n.highlight).map((n) => String(n.id)));
  for (const node of section.querySelectorAll('.node')) {
    const on = lit.has(node.dataset.id);
    if (on && !node.classList.contains('lit')) {
      node.classList.remove('flash');
      void node.getBBox(); // restart the flash
      node.classList.add('flash');
    }
    node.classList.toggle('lit', on);
  }
}

function chart(block, width) {
  const desc = buildChart(block, width);
  if (!desc) {
    notes.push('a chart had no numbers to draw');
    return null;
  }
  return h('div', 'figure', [svgFrom(desc)]);
}

function facts(block) {
  return h(
    'div',
    'facts',
    (block.items || []).map((it, k) => {
      const tile = h('div', 'fact rise', [h('div', 'fact-value', inline(parseInline(String(it.value ?? '')))), h('div', 'fact-label', inline(parseInline(String(it.label ?? ''))))]);
      if (it.note) tile.append(h('div', 'fact-note', inline(parseInline(String(it.note)))));
      tile.style.setProperty('--i', String(k));
      return tile;
    }),
  );
}

function step(it, k, timed) {
  const li = h('li', `step rise${it.state ? ` ${it.state}` : ''}`, [
    timed ? h('span', 'step-time', inline(parseInline(String(it.time ?? '')))) : null,
    h('span', 'step-dot', String(k + 1)),
    h('div', 'step-body', [h('div', 'step-title', inline(parseInline(String(it.title ?? '')))), it.text ? h('div', 'step-text', markdown(String(it.text))) : null]),
  ]);
  li.style.setProperty('--i', String(k));
  return li;
}

function steps(block) {
  const timed = (block.items || []).some((it) => it.time);
  return h('ol', timed ? 'steps timed' : 'steps', (block.items || []).map((it, k) => step(it, k, timed)));
}

// A steps block whose steps changed state or got new ones at the end: the ones it had stay, restated (their colours
// ease over), and the new ones rise in.
function restep(section, block) {
  const list = section.querySelector('.steps');
  const items = block.items || [];
  const timed = items.some((it) => it.time);
  if (!list || list.classList.contains('timed') !== timed) return false;
  const have = [...list.children];
  items.forEach((it, k) => {
    if (k < have.length) {
      have[k].classList.remove('todo', 'current', 'done');
      if (it.state) have[k].classList.add(it.state);
    } else {
      const li = step(it, k, timed);
      li.style.setProperty('--i', '0');
      list.append(li);
    }
  });
  return true;
}

function compare(block) {
  const cols = block.columns || [];
  const grid = h(
    'div',
    'compare',
    cols.map((c, k) => {
      const col = h('div', 'compare-col rise', [
        h('div', 'compare-title', inline(parseInline(String(c.title ?? '')))),
        h('ul', null, (c.points || []).map((p) => h('li', null, inline(parseInline(String(p)))))),
      ]);
      col.style.setProperty('--i', String(k));
      return col;
    }),
  );
  grid.style.setProperty('--cols', String(Math.max(1, cols.length)));
  return grid;
}

// Boxes and choice buttons the user answers in. Clicking a box gives the panel the keyboard (and only then); Enter
// (Ctrl+Enter in a bigger box) or the send button sends what's in this block's boxes, a choice button sends itself,
// and Escape gives the keyboard back unsent. What's sent becomes the user's turn (src/octavia/visuals/host.py).
let typing = false;
let ending = false; // the page is ending the typing itself (sent, or Escape): the keyboard goes back
function inputs(block) {
  const form = h('div', 'inputs');
  const boxes = [];
  const sentMark = h('span', 'sent-mark', '');
  const send = (field) => {
    const values = {};
    const untouched = []; // sent just as she pre-filled them: her words, not theirs
    for (const box of boxes) {
      values[box.dataset.field] = box.value;
      if (box.value && box.value === box.defaultValue) untouched.push(box.dataset.field);
      box.dataset.sent = box.value;
    }
    host.sent(values, field || '', untouched, false);
    sentMark.replaceChildren('Sent ', iconSvg(CHROME.sent));
    sentMark.classList.remove('flash');
    void sentMark.offsetWidth;
    sentMark.classList.add('flash');
    stopTyping(true);
  };
  for (const f of block.fields || []) {
    const row = h('div', 'input-row');
    if (f.label) row.append(h('label', 'input-label', inline(parseInline(String(f.label)))));
    if (f.choices && f.choices.length) {
      const choices = h('div', 'choices');
      for (const c of f.choices) {
        const button = h('button', 'choice', String(c));
        button.type = 'button';
        button.addEventListener('click', () => {
          if (choices.classList.contains('resting')) return;
          for (const b of choices.children) b.classList.toggle('chosen', b === button);
          host.sent({ [f.id]: String(c) }, f.id, [], true);
          choices.classList.add('resting'); // one answer at a time: clicking through them doesn't queue several
          setTimeout(() => choices.classList.remove('resting'), 1500);
        });
        choices.append(button);
      }
      row.append(choices);
    } else {
      const box = document.createElement(f.lines > 1 ? 'textarea' : 'input');
      if (f.lines > 1) box.rows = f.lines;
      else box.type = 'text';
      box.className = 'box';
      box.dataset.field = f.id;
      box.maxLength = 2000;
      box.placeholder = String(f.placeholder || '');
      box.value = String(f.value || '');
      box.defaultValue = box.value;
      box.spellcheck = false;
      box.addEventListener('pointerdown', () => startTyping(box));
      box.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          stopTyping(true);
        } else if (e.key === 'Enter' && !e.isComposing && (f.lines === 1 || e.ctrlKey)) {
          e.preventDefault();
          send(f.id);
        }
      });
      boxes.push(box);
      row.append(box);
    }
    form.append(row);
  }
  if (boxes.length) {
    const button = h('button', 'send', String(block.submit || 'Send'));
    button.type = 'button';
    button.addEventListener('click', () => send(''));
    form.append(h('div', 'send-row', [sentMark, button]));
  }
  return form;
}

// The panel takes the keyboard for this box: the main process makes the window focusable and focuses it, then the
// box gets the caret.
function startTyping(box) {
  if (!typing) {
    typing = true;
    host.typing(true);
  }
  const focusBox = () => document.activeElement !== box && box.focus({ preventScroll: true });
  // The window gets the keyboard once Octavia has said where it was (at most a quarter second): try until then.
  for (const ms of [20, 80, 160, 320]) setTimeout(focusBox, ms);
}

// Done typing: from here (`back`: hand the keyboard back to where it was) or because they clicked elsewhere.
function stopTyping(back) {
  if (!typing) return;
  typing = false;
  ending = true;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  ending = false;
  host.typing(false, back);
}

// Clicking off the boxes but still on the panel ends the typing too; leaving for another window is the main
// process's to notice (the window's blur).
document.addEventListener('focusout', (e) => {
  if (!typing || ending) return;
  const next = e.relatedTarget;
  if (next && next.closest && next.closest('.box')) return;
  setTimeout(() => {
    if (typing && document.hasFocus() && !(document.activeElement && document.activeElement.closest('.box'))) stopTyping(true);
  }, 0);
});
window.addEventListener('blur', () => {
  typing = false; // the main process saw it go (it hands nothing back: they chose where the keyboard went)
});

// A picture she drew: already cleaned by Octavia (src/octavia/visuals/svg_clean.py), and shown as an image, so even
// what the cleaning missed can't run a script or load anything.
function picture(block) {
  const img = new Image();
  img.alt = String(block.alt || 'a drawing');
  img.className = 'drawing';
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(block.svg || ''))}`;
  return h('div', 'figure', [img]);
}

// A scene: her picture of separate elements (cards, shapes, arrows, text, emoji), each its own group with its id, so
// any one of them can be changed, moved or taken away without redrawing the rest (reconcileScene).
let pictures = 0; // each picture's arrowhead gets an id of its own
function scene(block, width) {
  const W = Number(block.width) || 400;
  const H = Number(block.height) || 240;
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const scale = Math.max(MIN_FIGURE_SCALE, Math.min(1, width / W));
  svg.setAttribute('width', String(Math.round(W * scale)));
  svg.setAttribute('height', String(Math.round(H * scale)));
  svg.setAttribute('class', 'scene');
  svg.setAttribute('role', 'img');
  const marker = `scene-arrow-${++pictures}`;
  svg.dataset.marker = marker;
  svg.append(svgFrom({ tag: 'defs', kids: [arrowhead(marker)] }));
  (block.elements || []).forEach((e, k) => {
    const node = svgFrom(element(e, { marker, measure }));
    node.style.setProperty('--i', String(Math.min(k, 30)));
    node.dataset.drawnAt = JSON.stringify({ x: e.x, y: e.y }); // where it was drawn: later moves are translations
    svg.append(node);
  });
  return h('div', 'figure', [svg]);
}

// Whether only an element's place changed (x, y, and a line's end by the same amount), and it isn't turned (a CSS
// move would take over its rotate attribute).
function movedOnly(a, b) {
  if (Number(a.rotate) || Number(b.rotate)) return false;
  const dx = Number(b.x) - Number(a.x);
  const dy = Number(b.y) - Number(a.y);
  const rest = (e) => JSON.stringify({ ...e, x: 0, y: 0, x2: e.x2 == null ? null : Number(e.x2) - Number(e.x), y2: e.y2 == null ? null : Number(e.y2) - Number(e.y) });
  return (dx || dy) && rest(a) === rest(b);
}

// A scene she changed: elements that are the same stay as they are, changed ones are redrawn where they are, new ones
// pop in, and gone ones fade out. Nothing else in the picture moves or replays.
function reconcileScene(sectionNode, before, after) {
  const svg = sectionNode.querySelector('svg.scene');
  if (!svg || before.width !== after.width || before.height !== after.height) return false;
  const marker = svg.dataset.marker;
  const old = new Map((before.elements || []).map((e) => [e.id, JSON.stringify(e)]));
  const nodes = new Map([...svg.querySelectorAll(':scope > g.el')].map((g) => [g.dataset.eid, g]));
  const wanted = new Set((after.elements || []).map((e) => e.id));
  for (const [id, g] of nodes) {
    if (wanted.has(id)) continue;
    g.classList.add('leaving');
    setTimeout(() => g.remove(), 220);
  }
  let cursor = svg.querySelector('defs');
  for (const e of after.elements || []) {
    let g = nodes.get(e.id);
    const was = g && old.get(e.id);
    if (g && was && was !== JSON.stringify(e) && movedOnly(JSON.parse(was), e)) {
      // Only its place changed: the node stays, and eases there (.el transitions transform).
      const from = JSON.parse(g.dataset.drawnAt || was);
      g.style.transform = `translate(${Number(e.x) - Number(from.x)}px, ${Number(e.y) - Number(from.y)}px)`;
      old.set(e.id, JSON.stringify(e));
    } else if (!g || old.get(e.id) !== JSON.stringify(e)) {
      const fresh = svgFrom(element(e, { marker, measure }));
      fresh.style.setProperty('--i', '0');
      fresh.dataset.drawnAt = JSON.stringify({ x: e.x, y: e.y });
      if (g) {
        fresh.classList.remove('pop');
        fresh.classList.add('morph');
        g.replaceWith(fresh);
      }
      g = fresh;
    }
    if (cursor.nextSibling !== g) cursor.after(g); // only what's out of order moves
    cursor = g;
  }
  const caption = sectionNode.querySelector(':scope > .caption');
  if ((after.caption || '') !== (before.caption || '')) {
    if (caption) caption.remove();
    if (after.caption) sectionNode.append(h('div', 'caption', inline(parseInline(String(after.caption)))));
  }
  return true;
}

// A worksheet: her picture (an exam's tree or graph) with blanks on it, placed by percentages so the picture and
// its blanks scale together. Number and text blanks are small boxes (the panel takes the keyboard only while one is
// clicked, as for an input block; Enter moves to the next one), a cut blank is a spot a click crosses out in red.
// The send button sends every blank at once. Her marks colour the blanks and put a tip beside each; what's typed
// stays through them (reconcileSheet).
// The picture's own frame (its viewBox: where blanks' coordinates are measured from), or the block's width and height.
function frameOf(block) {
  const box = /viewBox\s*=\s*["']([^"']+)["']/.exec(String(block.svg || ''));
  const nums = box ? box[1].trim().split(/[\s,]+/).map(Number) : [];
  if (nums.length === 4 && nums.every(Number.isFinite) && nums[2] > 0 && nums[3] > 0) return nums;
  return [0, 0, Number(block.width) || 400, Number(block.height) || 300];
}

function worksheet(block) {
  const frame = frameOf(block);
  const sheetNode = h('div', 'ws');
  sheetNode.style.setProperty('--ws-w', String(frame[2]));
  sheetNode.style.maxWidth = `${frame[2]}px`;
  // The picture in the flow at the sheet's full width: the sheet is exactly the picture's box, so a blank placed by
  // percentages of it lands on its point of the picture (no letterboxing to drift from).
  const img = new Image();
  img.alt = 'worksheet';
  img.className = 'ws-picture';
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(block.svg || ''))}`;
  sheetNode.append(img);
  for (const b of block.blanks || []) sheetNode.append(blankNode(b, frame));
  const message = h('div', 'ws-message text', block.message ? markdown(block.message) : []);
  const send = h('button', 'send', String(block.submit || 'Check'));
  send.type = 'button';
  send.addEventListener('click', () => sendSheet(sheetNode));
  return h('div', 'ws-wrap', [sheetNode, h('div', 'send-row', [h('span', 'sent-mark', ''), send]), message]);
}

function blankNode(b, frame) {
  const [x0, y0, W, H] = frame;
  const at = (node) => {
    node.style.left = `${((Number(b.x) - x0) / W) * 100}%`;
    node.style.top = `${((Number(b.y) - y0) / H) * 100}%`;
  };
  let node;
  if (b.kind === 'cut') {
    node = h('button', 'blank cut', '✕');
    node.type = 'button';
    node.title = String(b.label || 'cross out');
    node.dataset.value = b.value === 'x' ? 'x' : '';
    node.classList.toggle('on', node.dataset.value === 'x');
    node.addEventListener('click', () => {
      node.dataset.value = node.dataset.value === 'x' ? '' : 'x';
      node.classList.toggle('on', node.dataset.value === 'x');
    });
  } else {
    node = document.createElement('input');
    node.type = 'text';
    node.className = 'blank box';
    if (b.kind === 'number') node.inputMode = 'decimal';
    node.maxLength = 200;
    node.placeholder = String(b.label || '');
    node.value = String(b.value || '');
    node.defaultValue = node.value;
    node.spellcheck = false;
    node.dataset.field = b.id;
    if (Number(b.width) > 0) node.style.width = `${(Number(b.width) / W) * 100}cqw`; // of the sheet (a size container), not of the blank's own wrapper
    node.addEventListener('pointerdown', () => startTyping(node));
    node.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        stopTyping(true);
      } else if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault(); // the next blank, not a send: a worksheet goes in all at once
        const boxes = [...node.closest('.ws').querySelectorAll('input.blank')];
        const next = boxes[boxes.indexOf(node) + 1];
        if (next) next.focus({ preventScroll: true });
      }
    });
  }
  node.dataset.eid = b.id;
  at(node);
  const wrap = h('span', `blank-at${b.mark ? ` mark-${b.mark}` : ''}`, [node]);
  wrap.dataset.blank = b.id;
  at(wrap);
  node.style.left = '';
  node.style.top = '';
  if (b.tip) wrap.append(h('span', 'tip', String(b.tip)));
  return wrap;
}

function sheetValues(sheetNode) {
  const values = {};
  const untouched = [];
  for (const wrap of sheetNode.querySelectorAll('.blank-at')) {
    const node = wrap.firstChild;
    const id = wrap.dataset.blank;
    values[id] = node.classList.contains('cut') ? node.dataset.value || '' : node.value;
    if (!node.classList.contains('cut') && node.value && node.value === node.defaultValue) untouched.push(id);
  }
  return { values, untouched };
}

function sendSheet(sheetNode) {
  const { values, untouched } = sheetValues(sheetNode);
  host.sent(values, '', untouched, false);
  const mark = sheetNode.parentNode.querySelector('.sent-mark');
  if (mark) {
    mark.replaceChildren('Sent ', iconSvg(CHROME.sent));
    mark.classList.remove('flash');
    void mark.offsetWidth;
    mark.classList.add('flash');
  }
  stopTyping(true);
}

// Her marks, or the same sheet sent again with the same blanks: marks, tips, the message and the picture change in
// place; what's been typed or crossed out stays (unless she set a blank's value herself).
function reconcileSheet(sectionNode, before, after) {
  const sheetNode = sectionNode.querySelector('.ws');
  const ids = (b) => (b.blanks || []).map((x) => x.id).join('\u0000');
  if (!sheetNode || ids(before) !== ids(after)) return false;
  const frame = frameOf(after);
  if (before.svg !== after.svg || before.width !== after.width || before.height !== after.height) {
    sheetNode.querySelector('.ws-picture').src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(after.svg || ''))}`;
    sheetNode.style.maxWidth = `${frame[2]}px`;
    sheetNode.style.setProperty('--ws-w', String(frame[2]));
    for (const wrap of [...sheetNode.querySelectorAll('.blank-at')]) {
      const b = (after.blanks || []).find((x) => x.id === wrap.dataset.blank);
      if (b) {
        wrap.style.left = `${((Number(b.x) - frame[0]) / frame[2]) * 100}%`;
        wrap.style.top = `${((Number(b.y) - frame[1]) / frame[3]) * 100}%`;
      }
    }
  }
  const old = new Map((before.blanks || []).map((b) => [b.id, b]));
  for (const b of after.blanks || []) {
    const wrap = sheetNode.querySelector(`.blank-at[data-blank="${CSS.escape(b.id)}"]`);
    if (!wrap) continue;
    const was = old.get(b.id) || {};
    if (was.kind !== b.kind || was.x !== b.x || was.y !== b.y || was.width !== b.width || was.label !== b.label) {
      const node = wrap.firstChild;
      const typed = node.classList.contains('cut') ? node.dataset.value : node.value;
      const fresh = blankNode({ ...b, value: was.value !== b.value ? b.value : typed }, frame);
      wrap.replaceWith(fresh);
      continue;
    }
    const node = wrap.firstChild;
    if ((was.value || '') !== (b.value || '')) {
      if (node.classList.contains('cut')) {
        node.dataset.value = b.value === 'x' ? 'x' : '';
        node.classList.toggle('on', node.dataset.value === 'x');
      } else node.value = String(b.value || '');
    }
    for (const m of ['right', 'wrong', 'missed', 'extra']) wrap.classList.toggle(`mark-${m}`, b.mark === m);
    let tip = wrap.querySelector('.tip');
    if (b.tip) {
      if (!tip) wrap.append((tip = h('span', 'tip', '')));
      tip.textContent = String(b.tip);
    } else if (tip) tip.remove();
  }
  if ((before.message || '') !== (after.message || '')) {
    sectionNode.querySelector('.ws-message').replaceChildren(...(after.message ? markdown(after.message) : []));
  }
  if ((before.submit || '') !== (after.submit || '')) sectionNode.querySelector('.send').textContent = String(after.submit || 'Check');
  return true;
}

// ---------------------------------------------------------------- pictures and pages from the web

// Pictures from the web: Octavia downloaded, checked and redrew each one (src/octavia/visuals/web_images.py), and the
// app serves only those, by name, from images/. Each says where it's from; ↗ opens that page in the user's browser.
const IMAGE_NAME = /^img-[0-9a-f]{32}\.(jpg|png)$/;
function sourceButton(label, which, title) {
  const button = h('button', 'source', [h('span', 'source-mark', '◇'), h('span', 'source-site', label), h('span', 'source-go', [iconSvg(CHROME.open)])]);
  button.title = title;
  button.addEventListener('click', () => host.open(which));
  return button;
}

function gallery(block) {
  const pics = (block.images || []).filter((p) => IMAGE_NAME.test(String(p.src || '')));
  if (!pics.length) throw new Error('no picture it can show');
  const node = h('div', `gallery n${Math.min(pics.length, 4)}`);
  for (const p of pics) {
    const img = new Image();
    img.className = 'photo';
    img.alt = String(p.alt || 'a picture');
    if (p.width > 0 && p.height > 0) {
      img.width = p.width; // its shape is known before it decodes: the panel lays out once
      img.height = p.height;
    }
    img.src = `${location.origin}/images/${p.src}`;
    img.draggable = false;
    const frame = h('div', 'photo-frame', [img, h('span', 'photo-scan')]);
    const kids = [frame];
    if (p.source) kids.push(sourceButton(String(p.site || 'source'), String(p.source), `Open ${p.source} in your browser`));
    node.append(h('figure', 'pic', kids));
  }
  return node;
}

// A web page: its own sandboxed view in the main process sits exactly over this box (where it is goes up with
// host.web), so this box only shows while the page loads or when it couldn't.
let webBox = null;
let webState = {};
function webPage(block) {
  const site = (() => {
    try {
      return new URL(String(block.url)).hostname.replace(/^www\./, '');
    } catch {
      return 'the page';
    }
  })();
  const status = h('div', 'web-status', `Loading ${site}…`);
  const box = h('div', 'web-view', [h('span', 'web-glyph', [iconSvg(CHROME.star)]), status]);
  box.style.height = `${Math.max(240, Number(block.height) || 560)}px`;
  const titleText = h('span', 'web-title', site);
  const bar = h('div', 'web-bar', [titleText, sourceButton('open in browser', 'page', 'Open this page in your browser')]);
  webBox = { box, status, titleText, site };
  webObserver.disconnect();
  webObserver.observe(box);
  showWebState(webState);
  return h('div', 'web', [box, bar]);
}

function showWebState(st) {
  webState = st || {};
  if (!webBox) return;
  const { state, title, url, error } = webState;
  let site = webBox.site;
  try {
    if (url) site = new URL(url).hostname.replace(/^www\./, '');
  } catch {
    // keep the one it was given
  }
  webBox.titleText.textContent = title ? `${title} · ${site}` : site;
  const failed = state === 'failed' || state === 'blocked';
  webBox.status.textContent = failed ? `Couldn't load ${site}${error ? `: ${error}` : ''}` : state === 'loaded' ? '' : `Loading ${site}…`;
  webBox.box.classList.toggle('failed', failed);
}

// Where the page goes on the panel (window pixels), clipped to what of the content shows; null when none of it does.
function webRect() {
  if (!webBox || !webBox.box.isConnected || !panel.classList.contains('open') || panel.classList.contains('fold')) return null;
  const r = webBox.box.getBoundingClientRect();
  const c = content.getBoundingClientRect();
  const x = Math.max(r.left, c.left);
  const y = Math.max(r.top, c.top);
  const width = Math.min(r.right, c.right) - x;
  const height = Math.min(r.bottom, c.bottom) - y;
  return width > 8 && height > 8 ? { x, y, width, height } : null;
}

let webReport = null;
function reportWeb() {
  if (!webBox) return;
  cancelAnimationFrame(webReport);
  webReport = requestAnimationFrame(() => host.web(webRect()));
}
content.addEventListener('scroll', reportWeb, { passive: true });
new ResizeObserver(reportWeb).observe(content);
const webObserver = new ResizeObserver(reportWeb);
host.onPage(showWebState);
host.onHover((over) => {
  // The main process tells a page's panel where the pointer is (the page's own view has it): the count waits.
  if (state.hovered === over) return;
  state.hovered = over;
  panel.classList.toggle('hovered', over);
});

// What a live block says (it ran, it failed, what it reports, the values the user set) goes to Octavia, by its address.
function liveReporter(getSection) {
  return (event) => {
    const sec = getSection();
    if (!sec || !sec.isConnected) return;
    host.live(sec.dataset.bid ? sec.dataset.bid : `#${sec.dataset.block}`, event);
  };
}

function section(block, k, width, previous = null) {
  let node = null;
  let wrapRef = null;
  try {
    if (block.kind === 'text') node = h('div', 'text', markdown(block.markdown));
    else if (block.kind === 'diagram') node = diagram(block, width);
    else if (block.kind === 'chart') node = chart(block, width);
    else if (block.kind === 'facts') node = facts(block);
    else if (block.kind === 'steps') node = steps(block);
    else if (block.kind === 'compare') node = compare(block);
    else if (block.kind === 'svg') node = picture(block);
    else if (block.kind === 'input') node = inputs(block);
    else if (block.kind === 'sim') node = buildSim(block, { h, inline, parseInline });
    else if (block.kind === 'scene') node = scene(block, width);
    else if (block.kind === 'sheet') node = worksheet(block);
    else if (block.kind === 'image') node = gallery(block);
    else if (block.kind === 'page') node = webPage(block);
    else if (block.kind === 'film') node = buildFilm(block, { h, host });
    else if (block.kind === 'live') node = buildLive(block, { h, inline, parseInline, report: liveReporter(() => wrapRef) }, previous);
    else notes.push(`block ${k + 1} is a kind the panel doesn't know (${block.kind})`);
  } catch (err) {
    notes.push(`block ${k + 1} (${block.kind}) couldn't be drawn: ${err.message}`);
  }
  if (!node) return null;
  const wrap = h('section', `block block-${block.kind} rise`, [node]);
  wrapRef = wrap;
  if (block.caption) wrap.append(h('div', 'caption', inline(parseInline(String(block.caption)))));
  wrap.style.setProperty('--i', String(k));
  wrap.dataset.block = String(k); // addressable as #k (or #id)
  if (block.id) wrap.dataset.bid = String(block.id);
  if (block.kind === 'input') for (const row of wrap.querySelectorAll('.box')) row.dataset.eid = row.dataset.field;
  return wrap;
}

// A kept block's caption, as the block now has it.
function recaption(wrap, block) {
  const now = wrap.querySelector(':scope > .caption');
  const next = block.caption ? h('div', 'caption', inline(parseInline(String(block.caption)))) : null;
  if (now && next) now.replaceWith(next);
  else if (now) now.remove();
  else if (next) wrap.append(next);
}

// What's on the page now, block by block: each block's spec and its section (null where it couldn't be drawn).
let shown = [];

// The first build, or a change: only what changed is redrawn (patch.js), so a step marked done or a node lit doesn't
// make the rest flash or replay its entrance. Changed blocks come in quickly; new ones rise in.
function build(spec, width, first) {
  notes = [];
  titleEl.textContent = String(spec.title || '');
  const next = spec.blocks || [];
  const plan = first ? next.map((_, index) => ({ action: 'add', index })) : planUpdate(shown.map((s) => s.block), next);
  const kept = new Set();
  const out = plan.map(({ action, index, from }) => {
    const block = next[index];
    const old = from !== undefined ? shown[from] : null;
    if (old && old.node && action === 'keep') {
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'highlight') {
      relight(old.node, block);
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'steps' && restep(old.node, block)) {
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'sheet' && reconcileSheet(old.node, old.block, block)) {
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'scene' && reconcileScene(old.node, old.block, block)) {
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'live' && old.node.querySelector('.live').live.update(block)) {
      recaption(old.node, block);
      kept.add(old.node);
      return { block, node: old.node };
    }
    if (old && old.node && action === 'film') {
      old.node.querySelector('.film').film.update(block);
      kept.add(old.node);
      return { block, node: old.node };
    }
    // A live block whose program changed starts again with the values the user had set on its controls.
    const was = old && old.node && old.node.querySelector('.live');
    const previous = was && block.kind === 'live' ? { params: was.live.block().params, values: was.live.values() } : null;
    const node = section(block, index, width, previous);
    if (node && !first) node.classList.add(action === 'add' ? 'arrive' : 'swap-in');
    return { block, node };
  });
  // Into the page moving as little as possible: a node is only moved when it's out of order (a block she moved), and
  // entrances are one-shot (their classes go once played), so nothing kept replays or loses a flash it was just given.
  carryTyped(shown, out, kept);
  for (const s of shown) {
    if (!s.node || kept.has(s.node)) continue;
    const live = s.node.querySelector('.live');
    if (live) live.live.destroy();
    s.node.remove();
  }
  if (!overlayLayer.isConnected) sheet.append(overlayLayer);
  if (!inkCanvas.isConnected) sheet.append(inkCanvas);
  let cursor = null;
  out.forEach(({ node }, k) => {
    if (!node) return;
    node.dataset.block = String(k);
    const at = cursor ? cursor.nextSibling : sheet.firstChild;
    // A kept block is moved without being taken out (moveBefore): taking out a live block's frame reloads it.
    if (at !== node) {
      if (node.isConnected && typeof sheet.moveBefore === 'function') sheet.moveBefore(node, at);
      else sheet.insertBefore(node, at);
    }
    cursor = node;
  });
  shown = out;
  overlaysWanted = spec.overlays || [];
}

// ---------------------------------------------------------------- overlays: her elements over the whole panel

const overlayLayer = document.createElementNS(SVG, 'svg');
overlayLayer.setAttribute('class', 'overlays');
overlayLayer.append(svgFrom({ tag: 'defs', kids: [arrowhead('overlay-arrow')] }));
let overlaysWanted = [];
const PLACING = ['place', 'near', 'side', 'between', 'gap', 'dx', 'dy'];
// An overlay without where it goes: what it looks like.
function lookOf(o) {
  const look = { ...o };
  for (const key of PLACING) delete look[key];
  return look;
}
let overlaysDrawn = new Map(); // id -> [json, node]
const overlaySpots = new Map(); // id -> its box once placed (null: placed by its own coordinates)

// A box on the panel, in its own pixels (zoom taken out), for an address: '#2' or '#id' (a block), '2:x' or 'id:x'
// (something in that block), or a bare id (an overlay, a block id, then anything with that id).
function nodeOf(address) {
  const a = String(address || '').trim();
  let node = null;
  const blockNode = (key) => sheet.querySelector(`:scope > section[data-block="${CSS.escape(key)}"], :scope > section[data-bid="${CSS.escape(key)}"]`);
  if (a.startsWith('#')) node = blockNode(a.slice(1));
  else if (a.includes(':')) {
    const [where, id] = [a.slice(0, a.indexOf(':')), a.slice(a.indexOf(':') + 1)];
    const block = blockNode(where);
    node = block && block.querySelector(`[data-eid="${CSS.escape(id)}"]`);
  } else {
    node = overlayLayer.querySelector(`[data-oid="${CSS.escape(a)}"]`) || sheet.querySelector(`:scope > section[data-bid="${CSS.escape(a)}"]`) || sheet.querySelector(`:scope > section [data-eid="${CSS.escape(a)}"]`);
  }
  return node || null;
}

function boxOf(address) {
  const node = nodeOf(address);
  if (!node) return null;
  const r = node.getBoundingClientRect();
  const s = sheet.getBoundingClientRect();
  const k = s.width / (sheet.offsetWidth || 1) || 1;
  return { x: (r.left - s.left) / k, y: (r.top - s.top) / k, w: r.width / k, h: r.height / k };
}

// Draws (or changes) the overlays and puts each where it belongs; ones placed against something that isn't there
// are left out, with a note for her. Run after every layout (placements follow what they're placed against).
function placeOverlays(report) {
  const sheetBox = { w: sheet.offsetWidth, h: sheet.offsetHeight };
  const wanted = new Set(overlaysWanted.map((o) => o.id));
  for (const [id, [, node]] of overlaysDrawn) {
    if (!wanted.has(id)) {
      node.classList.add('leaving');
      setTimeout(() => node.remove(), 220);
      overlaysDrawn.delete(id);
    }
  }
  for (const o of overlaysWanted) {
    const json = JSON.stringify(lookOf(o)); // where it goes isn't part of its look: a move keeps it and eases
    let drawn = overlaysDrawn.get(o.id);
    if (!drawn || drawn[0] !== json) {
      const node = svgFrom(element(o, { marker: 'overlay-arrow', measure }));
      node.dataset.oid = o.id;
      if (drawn) {
        node.classList.remove('pop');
        node.classList.add('morph');
        drawn[1].replaceWith(node);
      } else overlayLayer.append(node);
      drawn = [json, node];
      overlaysDrawn.set(o.id, drawn);
    }
    const spot = placeOverlay(o, boxOf, sheetBox);
    const node = drawn[1];
    if (!spot) {
      node.style.display = 'none';
      overlaySpots.delete(o.id);
      if (report) report.push(`overlay ${o.id}: nothing called ${o.near || o.between.join(' or ')} to place it by`);
      continue;
    }
    node.style.display = '';
    const arriving = !node.dataset.placed;
    node.dataset.placed = '1';
    const { w: ow, h: oh } = extent(o);
    overlaySpots.set(o.id, spot.absolute ? null : { x: spot.x - ow / 2, y: spot.y - oh / 2, w: ow, h: oh });
    const own = ownCentre(o);
    const move = spot.absolute
      ? `translate(${spot.x.toFixed(1)}px, ${spot.y.toFixed(1)}px)`
      : `translate(${(spot.x - own.x).toFixed(1)}px, ${(spot.y - own.y).toFixed(1)}px)`;
    // A CSS transform, so a move eases (.el transitions it); it takes over from the group's own rotate attribute, so
    // the turn goes in it too, round the element's middle.
    const turn = Number(o.rotate) ? ` rotate(${Number(o.rotate)}deg)` : '';
    node.removeAttribute('transform');
    node.style.transformBox = 'fill-box';
    node.style.transformOrigin = 'center';
    if (arriving) node.style.transition = 'none'; // straight to its place, popping in there
    node.style.transform = `${move}${turn}`;
    if (arriving) {
      void node.getBoundingClientRect();
      node.style.transition = '';
    }
  }
}

// Where everything is, for her to edit by (list_visuals): each block, each element in one, each overlay, in the
// panel's own pixels.
function layoutReport() {
  const boxes = {};
  let count = 0;
  const add = (key, box) => {
    if (box && count++ < 400) boxes[key] = { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.w), h: Math.round(box.h) };
  };
  for (const sec of sheet.querySelectorAll(':scope > section[data-block]')) {
    const k = sec.dataset.block;
    add(`#${k}`, boxOf(`#${k}`));
    for (const el of sec.querySelectorAll('[data-eid]')) add(`${k}:${el.dataset.eid}`, boxOf(`${k}:${el.dataset.eid}`));
  }
  for (const id of overlaysDrawn.keys()) add(id, overlaySpots.get(id) || boxOf(id));
  return boxes;
}

// An input block she rebuilt (she filled in one box, or a block before it changed) keeps what the user has typed in
// the others, and the caret stays where it was; a box that's gone ends the typing. What was already sent stays behind:
// a quiz's next question in the same block starts with an empty box, not the last answer.
function carryTyped(before, after, kept) {
  const typed = new Map();
  let focused = null;
  for (const { node } of before) {
    if (!node || kept.has(node)) continue;
    for (const box of node.querySelectorAll('.box')) {
      if (box.value !== box.defaultValue && box.value !== box.dataset.sent) typed.set(box.dataset.field, box.value);
      if (box === document.activeElement) focused = box.dataset.field;
    }
  }
  if (!typed.size && !focused) return;
  let refocus = null;
  for (const { node } of after) {
    if (!node || kept.has(node)) continue;
    for (const box of node.querySelectorAll('.box')) {
      const id = box.dataset.field;
      if (typed.has(id) && box.value === box.defaultValue) box.value = typed.get(id); // unless she set this one
      if (id === focused) refocus = box;
    }
  }
  if (refocus) setTimeout(() => refocus.focus({ preventScroll: true }), 0);
  else if (focused) setTimeout(() => stopTyping(true), 0);
}

// The panel got wider or narrower (the user resized it): text reflows by itself; diagrams and charts are drawn again
// for the new width, in place and without their entrance.
function refit(width) {
  shown = shown.map(({ block, node }, k) => {
    if (!node || (block.kind !== 'diagram' && block.kind !== 'chart')) return { block, node };
    const fresh = section(block, k, width);
    if (!fresh) return { block, node };
    fresh.classList.add('static');
    node.replaceWith(fresh);
    return { block, node: fresh };
  });
}

// ---------------------------------------------------------------- size and scale

// The panel's frame width for the window's (DIP), the zoom that keeps its text in proportion as it's resized, and
// the width figures are laid out at.
let base = 440;
let zoom = 1;
function figureWidth() {
  return (window.innerWidth - 2 * pad) / zoom - 48; // the content's side padding (22 each) and scrollbar room
}
let pad = 18;
function applyScale() {
  const frame = window.innerWidth - 2 * pad;
  zoom = Math.min(MAX_ZOOM, Math.max(1, frame / base));
  // The frame's own width comes from the window's in CSS (100vw), so it fits the window the moment it's resized;
  // only the zoom waits for this.
  document.documentElement.style.setProperty('--zoom', zoom.toFixed(3));
}

function measureHeight() {
  panel.classList.add('measuring');
  const height = panel.getBoundingClientRect().height;
  panel.classList.remove('measuring');
  return height;
}

// ---------------------------------------------------------------- time on screen

function startTimer(stayS) {
  clearInterval(state.ticker);
  state = { ...state, stayS, left: stayS * 1000, ticker: null };
  panel.classList.toggle('timed', stayS > 0);
  timerEl.style.transform = 'scaleX(1)';
  if (!stayS) return;
  state.ticker = setInterval(() => {
    if (state.hovered || inking || typing) return; // it waits while they're looking at it or writing on it
    state.left -= TICK_MS;
    timerEl.style.transform = `scaleX(${Math.max(0, state.left / (state.stayS * 1000)).toFixed(4)})`;
    if (state.left <= 0) {
      clearInterval(state.ticker);
      host.close('timer');
    }
  }, TICK_MS);
}

let generation = 0; // a newer spec arriving mid-build wins; the older build reports nothing
let remeasure = null;
let applying = false; // a spec is being laid out: only apply() reports its size (with its notes)

async function apply(payload, first) {
  const mine = ++generation;
  applying = true;
  pad = payload.pad;
  base = payload.base || payload.width;
  document.documentElement.style.setProperty('--pad', `${payload.pad}px`);
  applyScale();
  panel.hidden = false;
  lastWidth = window.innerWidth; // laid out for this width now: no redraw for it when the window settles
  build(payload.spec, figureWidth(), first);
  await document.fonts.ready;
  // A drawing's height is only known once it's decoded.
  await Promise.all([...content.querySelectorAll('img')].map((img) => img.decode().catch(() => notes.push("a drawing couldn't be shown"))));
  if (mine !== generation) return;
  await new Promise((r) => requestAnimationFrame(() => r()));
  if (mine !== generation) return;
  applying = false;
  // Measured at full length: the window is resized to it (up to a cap), and the content scrolls past that.
  placeOverlays(notes);
  player.update(payload.spec);
  reportedWidth = window.innerWidth;
  host.size(measureHeight(), notes);
  sizeInk();
  host.layout(layoutReport());
  player.place();
  setTimeout(() => mine === generation && player.place(), 420); // again once moved things have eased there
  startTimer(payload.stayS);
  showPin(payload.pinned === true, payload.pinnable !== false);
  reportWeb(); // a page that came with this spec, or moved with it (a block above it changed)
  clearTimeout(remeasure);
  remeasure = setTimeout(() => {
    reportWeb();
    if (mine !== generation) return;
    placeOverlays(null);
    host.layout(layoutReport());
    player.place();
  }, 900); // once the entrances (which scale and shift things while they play) are over
}

// ---------------------------------------------------------------- ambient light: short smooth bursts

// A see-through window is redrawn whole for every frame anything in it moves, so nothing loops: now and then a
// twinkle or the star's flare plays once, smoothly, for under a second, and the panel is still in between.
let ambient = null;
function scheduleAmbient() {
  clearTimeout(ambient);
  ambient = setTimeout(() => {
    if (!panel.classList.contains('open') || panel.classList.contains('fold')) return;
    const pick = Math.random() < 0.3 ? glyph : twinkles[Math.floor(Math.random() * twinkles.length)];
    if (pick) {
      pick.classList.remove('on');
      void pick.offsetWidth; // restart its burst
      pick.classList.add('on');
    }
    scheduleAmbient();
  }, 4000 + Math.random() * 5000);
}
const glyph = document.querySelector('.glyph');
// The title bar's star and buttons: filled Tabler icons (bundled), tinted by the bar's own colours.
setIcon(glyph, CHROME.star);
setIcon(document.getElementById('pen'), CHROME.pen);
setIcon(document.getElementById('clear'), CHROME.clear);
setIcon(document.getElementById('close'), CHROME.close);
const twinkles = [...document.querySelectorAll('.twinkle')];
for (const el of [glyph, ...twinkles]) el && el.addEventListener('animationend', () => el.classList.remove('on'));

// ---------------------------------------------------------------- scratch ink

// The pen: scratch notes in ink over the panel, kept while it's up (through her changes too), never saved.
// Pen on, the left button draws and the right one erases; the timer waits.
let inking = false;
let erasing = false;
let strokes = [];
let drawing = null;
const inkCanvas = document.createElement('canvas');
inkCanvas.className = 'ink';
// The blocks and the ink sit on one sheet inside the scrolling content, so the ink covers exactly the blocks.
const sheet = document.createElement('div');
sheet.className = 'sheet';
content.append(sheet);
const ink = inkCanvas.getContext('2d');

// The canvas covers the sheet the blocks sit on (its whole length, scrolled or not). Its pixels are matched to the screen's (the zoom and the
// display's scaling both), and pointer positions are mapped through its own box, so ink lands under the pen
// whatever the zoom.
let inkScale = 1;
const MAX_INK_PX = 8192; // the canvas's longest side in pixels: a very long panel gets softer ink, not 150 MB of it
function sizeInk() {
  const cssW = inkCanvas.offsetWidth || 1;
  const cssH = inkCanvas.offsetHeight || 1;
  const scale = Math.min(
    (inkCanvas.getBoundingClientRect().width / cssW) * (window.devicePixelRatio || 1),
    MAX_INK_PX / Math.max(cssW, cssH),
  );
  const w = Math.round(cssW * scale);
  const hgt = Math.round(cssH * scale);
  if (w === inkCanvas.width && hgt === inkCanvas.height && scale === inkScale) return; // nothing to reallocate
  inkScale = scale;
  inkCanvas.width = w;
  inkCanvas.height = hgt;
  drawInk();
}

function drawInk() {
  ink.setTransform(inkScale, 0, 0, inkScale, 0, 0);
  ink.clearRect(0, 0, inkCanvas.width, inkCanvas.height);
  ink.lineCap = 'round';
  ink.lineJoin = 'round';
  ink.lineWidth = 2.2;
  ink.strokeStyle = '#e6f3ff';
  ink.shadowColor = 'rgba(124, 192, 255, 0.9)';
  ink.shadowBlur = 4;
  for (const s of strokes) {
    ink.beginPath();
    s.points.forEach((p, k) => (k ? ink.lineTo(p.x, p.y) : ink.moveTo(p.x, p.y)));
    if (s.points.length === 1) ink.lineTo(s.points[0].x + 0.1, s.points[0].y);
    ink.stroke();
  }
}

// A pointer event as a point in the canvas's own CSS pixels (so in the content's coordinates).
function inkPoint(e) {
  const r = inkCanvas.getBoundingClientRect();
  const k = (inkCanvas.offsetWidth || 1) / (r.width || 1);
  return { x: (e.clientX - r.left) * k, y: (e.clientY - r.top) * k };
}

function setPen(on) {
  inking = on;
  panel.classList.toggle('inking', on);
  penButton.classList.toggle('on', on);
  penButton.title = on ? 'Pen on: draw with the left button, erase with the right. Click to stop.' : 'Write on it';
}

const penButton = document.getElementById('pen');
penButton.addEventListener('click', () => setPen(!inking));
document.getElementById('clear').addEventListener('click', () => {
  strokes = [];
  drawInk();
  host.ink(0);
});
inkCanvas.addEventListener('pointerdown', (e) => {
  if (!inking || (e.button !== 0 && e.button !== 2)) return;
  inkCanvas.setPointerCapture(e.pointerId);
  erasing = e.button === 2;
  const p = inkPoint(e);
  if (erasing) strokes = erase(strokes, p, 9);
  else drawing = startStroke(strokes, p);
  drawInk();
  inkChanged = true;
});
inkCanvas.addEventListener('pointermove', (e) => {
  if (!inking || !inkCanvas.hasPointerCapture(e.pointerId)) return;
  // Every position since the last frame, not only the latest: a quick flick keeps its curve.
  const points = (e.getCoalescedEvents ? e.getCoalescedEvents() : []).map(inkPoint);
  if (!points.length) points.push(inkPoint(e));
  let changed = false;
  for (const p of points) {
    if (erasing) {
      const before = strokes.length;
      strokes = erase(strokes, p, 9);
      changed ||= strokes.length !== before;
    } else if (drawing) changed = addPoint(drawing, p) || changed;
  }
  if (changed) drawInk();
});
// Octavia keeps count of the strokes (so she knows there's something to look at), told as each one ends; when asked,
// she looks at the panel, ink and all (look_at_panel). The ink itself is never saved.
let inkChanged = false;
const penUp = () => {
  drawing = null;
  erasing = false;
  if (inkChanged) host.ink(strokes.length);
  inkChanged = false;
};
inkCanvas.addEventListener('pointerup', penUp);
inkCanvas.addEventListener('pointercancel', penUp);

// ---------------------------------------------------------------- wiring

document.getElementById('close').addEventListener('click', () => host.close('user'));

// The pin: Octavia says what it is now (`onPinned`), so it never shows a pin she doesn't know of.
const pinButton = document.getElementById('pin');
let pinnedNow = false;
function showPin(on, can = true) {
  if (pinnedNow !== on || !pinButton.firstChild || !pinButton.firstChild.tagName) setIcon(pinButton, on ? CHROME.pinned : CHROME.pin);
  pinnedNow = on;
  pinButton.hidden = !can;
  pinButton.setAttribute('aria-pressed', String(on));
  panel.classList.toggle('pinned', on);
  pinButton.title = on ? 'Unpin: back to closing by itself' : 'Pin: keep it up, even after a restart';
}
pinButton.addEventListener('click', () => host.pin(!pinnedNow));
host.onPinned(({ on, stayS }) => {
  showPin(on === true);
  startTimer(Number.isFinite(stayS) ? stayS : 0);
});
window.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  if (e.target.closest && e.target.closest('.inputs')) return; // a right-click in a box isn't a close
  if (!inking) host.close('user'); // with the pen on, the right button erases
});
function hover(on) {
  if (state.hovered === on) return;
  state.hovered = on;
  panel.classList.toggle('hovered', on);
  host.mouse(on); // the window takes the mouse only while the pointer is on the frame, not its glow room
}
panel.addEventListener('mouseenter', () => hover(true));
panel.addEventListener('mouseleave', (e) => {
  if (dragging || resizing || sliding) return;
  // Into a live block's frame (its own document): the pointer is still on the panel. The frame's page has the mouse
  // then, so this page hears nothing until it comes back out.
  const into = document.elementFromPoint(e.clientX, e.clientY);
  if (into && into.classList && into.classList.contains('live-frame')) return;
  hover(false);
});
// A simulation's slider keeps the mouse while it's held, even when the pointer slips off the panel mid-drag.
let sliding = false;
content.addEventListener('pointerdown', (e) => {
  if (e.button === 0 && e.target.closest('.sim-range, .film-strip')) sliding = true;
});
const letSlideGo = () => {
  if (!sliding) return;
  sliding = false;
  if (!panel.matches(':hover')) hover(false);
};
window.addEventListener('pointerup', letSlideGo);
window.addEventListener('pointercancel', letSlideGo);
const bar = document.getElementById('bar');
bar.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 || e.target.closest('button')) return;
  bar.setPointerCapture(e.pointerId);
  dragging = true;
  host.drag('start');
});
let dragging = false;
const letGo = () => {
  if (!dragging) return;
  dragging = false;
  host.drag('end');
  if (!panel.matches(':hover')) hover(false);
};
bar.addEventListener('pointerup', letGo);
bar.addEventListener('pointercancel', letGo);
bar.addEventListener('lostpointercapture', letGo);

// Resizing: the edges and corners of the frame. The main process moves the window's edges with the pointer.
let resizing = false;
for (const grip of document.querySelectorAll('.grip')) {
  grip.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    grip.setPointerCapture(e.pointerId);
    resizing = true;
    host.resize('start', grip.dataset.edge);
  });
  const done = () => {
    if (!resizing) return;
    resizing = false;
    host.resize('end');
    // Laid out for the new width now, not in a moment, with its figures redrawn: the last word on its height (a pull
    // from the side followed it all along; from the bottom, the height they set stays).
    clearTimeout(refitTimer);
    settle();
    if (!panel.matches(':hover')) hover(false);
  };
  grip.addEventListener('pointerup', done);
  grip.addEventListener('pointercancel', done);
  grip.addEventListener('lostpointercapture', done);
}

// The window's size changed (a resize, or her change made it grow): the text's zoom follows the width, figures are
// redrawn for it once it settles, and the main process hears how tall the content now is.
let lastWidth = 0;
let reportedWidth = 0; // the width the main process last heard a height for
let refitTimer = null;
window.addEventListener('resize', () => {
  if (!shown.length) return;
  applyScale();
  if (Math.abs(window.innerWidth - lastWidth) < 1) {
    sizeInk();
    return;
  }
  // A new width (they're pulling it from a side): the text rewraps, and the main process hears the new height now,
  // so the window grows or shrinks with it as they pull, not in one jump after they let go (figures are redrawn once
  // it settles). A height they set themselves is kept there. Once per width: the height changes this brings fire
  // resize too, and each measure lays out the whole content.
  if (!applying && Math.abs(window.innerWidth - reportedWidth) >= 1) {
    reportedWidth = window.innerWidth;
    host.size(measureHeight(), [], true);
  }
  clearTimeout(refitTimer);
  refitTimer = setTimeout(settle, 120);
});

function settle() {
  if (Math.abs(window.innerWidth - lastWidth) >= 1) {
    lastWidth = window.innerWidth;
    refit(figureWidth());
  }
  sizeInk();
  if (applying) return; // mid-apply, apply reports (with its notes) once it's laid out
  placeOverlays(null);
  reportedWidth = window.innerWidth;
  host.size(measureHeight(), [], true);
  host.layout(layoutReport());
}

// While the main process moves, resizes or eases the window, it steps it on this page's animation frames: the
// display's own rate, where its timers on Windows manage only about 32 a second.
let ticking = false;
function frame() {
  if (!ticking) return;
  host.tick();
  requestAnimationFrame(frame);
}
host.onTicks((on) => {
  const was = ticking;
  ticking = on;
  if (on && !was) requestAnimationFrame(frame);
});

host.onGo(() => {
  panel.classList.add('open');
  lastWidth = window.innerWidth;
  scheduleAmbient();
  if (panel.matches(':hover')) hover(true); // it came up under a pointer that hasn't moved: the count waits
  setTimeout(reportWeb, 340); // a page comes in once the unfold is over
});
host.onFold(() => {
  clearInterval(state.ticker);
  clearTimeout(ambient);
  panel.classList.add('fold');
  setTimeout(() => host.folded(), 200);
});
// An explainer's player bar, pointer and flashes (player.js); made before the first spec is applied.
const player = createPlayer({ host, h, sheet, footer: document.querySelector('footer.timer'), nodeOf, boxOf });
host.onSpec((payload) => apply(payload, false));
host.hello().then((payload) => payload && apply(payload, true));

// Entrances are one-shot: once one has played, its class goes, so a node moved later (a block she moved) doesn't play
// it again.
const ONE_SHOT = ['rise', 'pop', 'draw', 'grow', 'swap-in', 'arrive', 'morph', 'pulse'];
document.addEventListener('animationend', (e) => {
  const t = e.target;
  if (!t || !t.classList || t.classList.contains('leaving')) return;
  if (ONE_SHOT.some((c) => t.classList.contains(c)) && !t.classList.contains('flash')) t.classList.remove(...ONE_SHOT);
});
