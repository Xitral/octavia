// Lays out a diagram (boxes and arrows) for a panel: a flowchart, a cycle, a concept map. Layered, the way Graphviz's
// dot does it but much smaller: cycles are broken by turning their back edges around, each node goes one layer
// below the longest chain into it, a few barycentre sweeps untangle each layer's order, and nodes then shift
// toward their parents without overlapping. Pure: text is measured by the `measure` passed in (the panel's uses
// the real font; tests use the estimate here).
//
// Input: {nodes:[{id, label, note?, shape?, tone?}], edges:[{from, to, label?, dashed?}], direction?}
//   shape: box | round | diamond | circle; tone: plain | key | glow | dim; direction: down | right | auto
// Output: {width, height, direction, nodes:[...with x, y, w, h, lines, noteLines], edges:[{d, back, dashed, label,
//   lx, ly}], notes:[what was left out, and why]}

const FONT = 14;
const NOTE_FONT = 11.5;
const LINE = 1.3;
const PAD_X = 12;
const PAD_Y = 8;
const CROSS_GAP = 22; // between neighbours in a layer
const LAYER_GAP = 44;
const LABEL_GAP = 18; // extra between layers when some edge there has a label
const MARGIN = 10;
const SHAPES = new Set(['box', 'round', 'diamond', 'circle']);
const TONES = new Set(['plain', 'key', 'glow', 'dim']);

// Words wrapped to `maxWidth`, by an average glyph width: good enough for tests and a fallback.
export function estimateMeasure(text, size, maxWidth) {
  const per = size * 0.55;
  const lines = [];
  let line = '';
  for (const word of String(text).split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && next.length * per > maxWidth) {
      lines.push(line);
      line = word;
    } else line = next;
  }
  if (line) lines.push(line);
  return { lines: lines.length ? lines : [''], width: Math.max(...(lines.length ? lines : ['']).map((l) => l.length * per)) };
}

function sizeNode(node, measure, maxText) {
  const label = measure(node.label, FONT, maxText);
  const note = node.note ? measure(node.note, NOTE_FONT, maxText) : { lines: [], width: 0 };
  const textW = Math.max(label.width, note.width, 24);
  const textH = label.lines.length * FONT * LINE + note.lines.length * NOTE_FONT * LINE + (note.lines.length ? 3 : 0);
  let w = textW + 2 * PAD_X;
  let h = textH + 2 * PAD_Y;
  if (node.shape === 'diamond') {
    w *= 1.45;
    h *= 1.6;
  } else if (node.shape === 'circle') {
    w = h = Math.max(w, h) * 1.08;
  }
  return { ...node, w: Math.ceil(w), h: Math.ceil(h), lines: label.lines, noteLines: note.lines };
}

// Ranks and orders the nodes: returns layers (arrays of indexes) and which edges were turned round.
function layer(count, edges) {
  const out = Array.from({ length: count }, () => []);
  edges.forEach((e, k) => out[e.from].push(k));
  // Depth-first from each node in the order given: an edge to a node still on the path closes a cycle.
  const state = new Array(count).fill(0); // 0 new, 1 on the path, 2 done
  const back = new Set();
  for (let root = 0; root < count; root++) {
    if (state[root]) continue;
    const stack = [[root, 0]];
    state[root] = 1;
    while (stack.length) {
      const top = stack[stack.length - 1];
      const [v, at] = top;
      if (at >= out[v].length) {
        state[v] = 2;
        stack.pop();
        continue;
      }
      top[1]++;
      const k = out[v][at];
      const w = edges[k].to;
      if (state[w] === 1) back.add(k);
      else if (state[w] === 0) {
        state[w] = 1;
        stack.push([w, 0]);
      }
    }
  }
  const forward = edges.map((e, k) => (back.has(k) ? { from: e.to, to: e.from } : e));
  // Longest path: Kahn's order over the edges as they now point.
  const indeg = new Array(count).fill(0);
  forward.forEach((e) => indeg[e.to]++);
  const rank = new Array(count).fill(0);
  const queue = [];
  for (let v = 0; v < count; v++) if (!indeg[v]) queue.push(v);
  for (let q = 0; q < queue.length; q++) {
    const v = queue[q];
    forward.forEach((e) => {
      if (e.from !== v) return;
      rank[e.to] = Math.max(rank[e.to], rank[v] + 1);
      if (--indeg[e.to] === 0) queue.push(e.to);
    });
  }
  // A node a source points at directly but sits several layers down is fine; a source with only far children is
  // pulled down next to them, so the top layer isn't crowded with things that belong lower.
  for (let v = 0; v < count; v++) {
    const ins = forward.filter((e) => e.to === v).length;
    const outs = forward.filter((e) => e.from === v).map((e) => rank[e.to]);
    if (!ins && outs.length) rank[v] = Math.max(rank[v], Math.min(...outs) - 1);
  }
  const layers = [];
  rank.forEach((r, v) => (layers[r] ||= []).push(v));
  return { layers: layers.filter(Boolean), rank, forward, back };
}

// Barycentre sweeps: each node moves to the average position of its neighbours in the layer before (then after).
function order(layers, forward) {
  const pos = new Map();
  const index = () => layers.forEach((l) => l.forEach((v, k) => pos.set(v, k / Math.max(1, l.length - 1))));
  index();
  const near = (v, up) => forward.filter((e) => (up ? e.to === v : e.from === v)).map((e) => pos.get(up ? e.from : e.to));
  for (let sweep = 0; sweep < 6; sweep++) {
    const up = sweep % 2 === 0;
    const seq = up ? layers.slice(1) : layers.slice(0, -1).reverse();
    for (const l of seq) {
      const keyed = l.map((v, k) => {
        const n = near(v, up);
        return { v, key: n.length ? n.reduce((a, b) => a + b, 0) / n.length : k / Math.max(1, l.length - 1) };
      });
      keyed.sort((a, b) => a.key - b.key);
      l.splice(0, l.length, ...keyed.map((x) => x.v));
      index();
    }
  }
}

// Within each layer, centres along the cross axis: packed, then pulled toward their parents' centres without
// overlapping or changing order.
function crossPositions(layers, nodes, forward, size) {
  const centre = new Array(nodes.length).fill(0);
  const widths = layers.map((l) => l.reduce((s, v) => s + size(nodes[v]), 0) + CROSS_GAP * (l.length - 1));
  const widest = Math.max(...widths);
  layers.forEach((l, r) => {
    let at = (widest - widths[r]) / 2;
    for (const v of l) {
      centre[v] = at + size(nodes[v]) / 2;
      at += size(nodes[v]) + CROSS_GAP;
    }
  });
  for (let pass = 0; pass < 2; pass++) {
    for (const l of pass ? [...layers].reverse() : layers) {
      const want = l.map((v) => {
        const ps = forward.filter((e) => (pass ? e.from === v : e.to === v)).map((e) => centre[pass ? e.to : e.from]);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : centre[v];
      });
      // Left to right, nothing may sit closer than the gap to the one before; then right to left the same.
      const got = want.slice();
      for (let k = 1; k < l.length; k++) {
        const min = got[k - 1] + (size(nodes[l[k - 1]]) + size(nodes[l[k]])) / 2 + CROSS_GAP;
        got[k] = Math.max(got[k], min);
      }
      for (let k = l.length - 2; k >= 0; k--) {
        const max = got[k + 1] - (size(nodes[l[k + 1]]) + size(nodes[l[k]])) / 2 - CROSS_GAP;
        got[k] = Math.min(got[k], max);
      }
      l.forEach((v, k) => (centre[v] = got[k]));
    }
  }
  const lo = Math.min(...nodes.map((n, v) => centre[v] - size(n) / 2));
  return centre.map((c) => c - lo);
}

function layoutOne(spec, direction, measure, maxText) {
  const notes = [];
  const ids = new Map();
  const nodes = [];
  for (const raw of spec.nodes || []) {
    const id = String(raw.id ?? '');
    if (!id || ids.has(id)) {
      notes.push(id ? `node "${id}" was listed twice` : 'a node with no id was left out');
      continue;
    }
    ids.set(id, nodes.length);
    const shape = SHAPES.has(raw.shape) ? raw.shape : 'box';
    const tone = TONES.has(raw.tone) ? raw.tone : 'plain';
    nodes.push(sizeNode({ id, label: String(raw.label ?? id), note: raw.note ? String(raw.note) : '', shape, tone }, measure, maxText));
  }
  const edges = [];
  for (const raw of spec.edges || []) {
    const from = ids.get(String(raw.from));
    const to = ids.get(String(raw.to));
    if (from === undefined || to === undefined) {
      notes.push(`an arrow from "${raw.from}" to "${raw.to}" names a node that isn't there`);
      continue;
    }
    if (from === to) {
      notes.push(`an arrow from "${raw.from}" to itself was left out`);
      continue;
    }
    edges.push({ from, to, label: raw.label ? String(raw.label) : '', dashed: raw.dashed === true });
  }
  if (!nodes.length) return { width: 0, height: 0, direction, nodes: [], edges: [], notes };
  const { layers, forward, back } = layer(nodes.length, edges);
  order(layers, forward);
  const down = direction === 'down';
  const cross = (n) => (down ? n.w : n.h);
  const main = (n) => (down ? n.h : n.w);
  const crossAt = crossPositions(layers, nodes, forward, cross);
  // Along the main axis: each layer as thick as its thickest node, a gap (wider where an arrow carries a label).
  const layerOf = new Array(nodes.length);
  layers.forEach((l, r) => l.forEach((v) => (layerOf[v] = r)));
  // (an arrow going back up runs down the side, so its label doesn't need room between layers)
  const labelled = new Set(
    edges.filter((e, k) => e.label && !back.has(k) && layerOf[e.from] !== layerOf[e.to]).map((e) => Math.min(layerOf[e.from], layerOf[e.to])),
  );
  const mainAt = [];
  let at = 0;
  layers.forEach((l, r) => {
    const thick = Math.max(...l.map((v) => main(nodes[v])));
    mainAt[r] = at + thick / 2;
    at += thick + LAYER_GAP + (labelled.has(r) ? LABEL_GAP : 0);
  });
  const placed = nodes.map((n, v) => {
    const c = crossAt[v];
    const m = mainAt[layerOf[v]];
    const cx = MARGIN + (down ? c : m);
    const cy = MARGIN + (down ? m : c);
    return { ...n, cx, cy, x: cx - n.w / 2, y: cy - n.h / 2 };
  });
  const routed = edges.map((e, k) => route(placed[e.from], placed[e.to], e, back.has(k), down));
  // A back arrow bows out to the side, its label on the bow: both inside the picture.
  const reach = routed.map((r) => {
    if (!r.back) return 0;
    const label = r.label ? measure(r.label, 11.5, 130) : { width: 0, lines: [] };
    return r.side + (down ? label.width / 2 + 8 : label.lines.length * 7 + 8);
  });
  const width = Math.ceil(Math.max(...placed.map((n) => n.x + n.w), ...(down ? reach : [0])) + MARGIN);
  const height = Math.ceil(Math.max(...placed.map((n) => n.y + n.h), ...(down ? [0] : reach)) + MARGIN);
  return { width, height, direction, nodes: placed, edges: routed, notes };
}

// An arrow: out of the source's far side and into the target's near side along the main axis, as a smooth curve.
// One that goes back up (a cycle's last arrow, or between nodes of one layer) bows out to the side instead.
function route(a, b, e, back, down) {
  const f = (n) => n.toFixed(1);
  let d;
  let lx;
  let ly;
  let side = 0;
  const forward = down ? b.cy > a.cy + 1 : b.cx > a.cx + 1;
  if (forward) {
    const [x1, y1] = down ? [a.cx, a.y + a.h] : [a.x + a.w, a.cy];
    const [x2, y2] = down ? [b.cx, b.y - 2] : [b.x - 2, b.cy];
    const bend = (down ? y2 - y1 : x2 - x1) * 0.5;
    const [c1x, c1y, c2x, c2y] = down ? [x1, y1 + bend, x2, y2 - bend] : [x1 + bend, y1, x2 - bend, y2];
    d = `M${f(x1)},${f(y1)} C${f(c1x)},${f(c1y)} ${f(c2x)},${f(c2y)} ${f(x2)},${f(y2)}`;
    [lx, ly] = [(x1 + 3 * c1x + 3 * c2x + x2) / 8, (y1 + 3 * c1y + 3 * c2y + y2) / 8];
  } else {
    // Out of the source's side and into the target's same side, bowing outward past both.
    side = down ? Math.max(a.x + a.w, b.x + b.w) + 26 : Math.max(a.y + a.h, b.y + b.h) + 26;
    const [x1, y1] = down ? [a.x + a.w, a.cy] : [a.cx, a.y + a.h];
    const [x2, y2] = down ? [b.x + b.w + 2, b.cy] : [b.cx, b.y + b.h + 2];
    const [c1x, c1y, c2x, c2y] = down ? [side, y1, side, y2] : [x1, side, x2, side];
    d = `M${f(x1)},${f(y1)} C${f(c1x)},${f(c1y)} ${f(c2x)},${f(c2y)} ${f(x2)},${f(y2)}`;
    [lx, ly] = [(x1 + 3 * c1x + 3 * c2x + x2) / 8, (y1 + 3 * c1y + 3 * c2y + y2) / 8];
  }
  return { from: a.id, to: b.id, d, back: back || !forward, dashed: e.dashed, label: e.label, lx, ly, side };
}

// The whole layout. `direction` auto picks whichever of down and right needs less shrinking to fit `fit`
// ({width, height}).
export function layoutDiagram(spec, { measure = estimateMeasure, fit = { width: 400, height: 480 } } = {}) {
  const maxText = 150;
  const wanted = spec.direction === 'right' || spec.direction === 'down' ? spec.direction : 'auto';
  if (wanted !== 'auto') return layoutOne(spec, wanted, measure, maxText);
  const scale = (l) => Math.min(1, fit.width / Math.max(1, l.width), fit.height / Math.max(1, l.height));
  const down = layoutOne(spec, 'down', measure, maxText);
  const right = layoutOne(spec, 'right', measure, maxText);
  return scale(right) > scale(down) + 0.05 ? right : down;
}

export const DIAGRAM_FONT = { label: FONT, note: NOTE_FONT, line: LINE };
