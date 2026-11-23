// oct.graph: a knowledge map (or any node graph) laid out, framed and labelled for reading, that every program of
// hers gets whole. She gives what it is (nodes with a kind, a group, a state and a line about them; edges with a
// kind), and the map does the rest: clusters by group, room for every label (the layout runs in screen pixels, so a
// label's size is known), crisp labels on plates that give way to more important ones instead of overlapping,
// typed and directed edges, shape by kind and colour by state or kind with a legend that says so, hover and
// selection with an inspector, reset/zoom/focus controls (and keys, when the frame has them), a hint that goes away,
// and no spinning. From the user's 45-point critique of her "CT3 concepts in 3D" panel (2026-11-22): scattered
// spheres in a far-off camera with labels piled on each other.
//
// makeGraph(deps, spec) -> handle; deps are the frame's own helpers (frame.js), so this file has no state of its own.

const SHAPES = ['sphere', 'box', 'octahedron', 'cone', 'dodecahedron', 'torus', 'cylinder', 'tetrahedron'];
const GLYPH = { sphere: '●', box: '■', octahedron: '◆', cone: '▲', dodecahedron: '⬢', torus: '◎', cylinder: '▮', tetrahedron: '▴' };
const PX = 40; // layout pixels per scene unit
const STATE_COLORS = { weak: 'rose', shaky: 'violet', learning: 'ice', solid: 'mint', new: 'muted', done: 'mint' };
const STATE_WORDS = { weak: 'needs study', shaky: 'shaky', learning: 'learning', solid: 'solid', new: 'not started', done: 'done' };

const hash = (s) => {
  let h = 2166136261;
  for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return ((h >>> 0) % 10000) / 10000;
};

function measureText(text, px, weight = 600) {
  const ctx = measureText.ctx || (measureText.ctx = document.createElement('canvas').getContext('2d'));
  ctx.font = `${weight} ${px}px 'Segoe UI', system-ui, sans-serif`;
  return ctx.measureText(String(text)).width;
}

// Positions in layout pixels: groups round an ellipse the shape of the view, each node pulled to its group, pushed
// off every other node's box (its shape and its label together), drawn along its edges, kept inside the view, with a
// little depth by importance. `layers`: prerequisites above what builds on them.
export function layout(nodes, edges, opts) {
  const W = Math.max(200, opts.width) * 0.9;
  const H = Math.max(150, opts.height) * 0.86;
  const groups = [...new Set(nodes.map((n) => n.group))];
  const centre = new Map();
  groups.forEach((g, k) => {
    if (groups.length === 1) return centre.set(g, [0, 0]);
    const a = -Math.PI / 2 + (k / groups.length) * Math.PI * 2;
    centre.set(g, [Math.cos(a) * W * 0.32, Math.sin(a) * H * 0.32]);
  });
  const depth = new Map(); // layers: longest chain of prerequisites above it
  if (opts.layers) {
    const into = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) if (into.has(e.to)) into.get(e.to).push(e.from);
    const d = (id, seen = new Set()) => {
      if (depth.has(id)) return depth.get(id);
      if (seen.has(id)) return 0;
      seen.add(id);
      const v = Math.max(0, ...(into.get(id) || []).map((p) => d(p, seen) + 1));
      depth.set(id, v);
      return v;
    };
    nodes.forEach((n) => d(n.id));
  }
  const maxDepth = Math.max(1, ...depth.values());
  const P = nodes.map((n) => {
    const [cx, cy] = centre.get(n.group);
    return { n, x: cx + (hash(n.id) - 0.5) * W * 0.2, y: cy + (hash(`${n.id}y`) - 0.5) * H * 0.2, z: 0, vx: 0, vy: 0 };
  });
  const at = new Map(P.map((p) => [p.n.id, p]));
  const box = (p) => ({ w: Math.max(p.n.size * 2, p.n.labelW) + 14, h: p.n.size * 2 + p.n.labelH + 10 });
  for (let it = 0; it < 360; it++) {
    const cool = 1 - it / 400;
    for (const p of P) {
      const [cx, cy] = centre.get(p.n.group);
      p.vx += (cx - p.x) * 0.02;
      p.vy += (cy - p.y) * 0.02;
      if (opts.layers) p.vy += ((H / 2 - (depth.get(p.n.id) / maxDepth) * H) - p.y) * 0.03;
    }
    for (const e of edges) {
      const a = at.get(e.from);
      const b = at.get(e.to);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;
      const want = a.n.group === b.n.group ? 110 : 190;
      const f = (d - want) * 0.006;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (let i = 0; i < P.length; i++) {
      for (let j = i + 1; j < P.length; j++) {
        const a = P[i];
        const b = P[j];
        const ba = box(a);
        const bb = box(b);
        const dx = b.x - a.x || 0.01;
        const dy = b.y - a.y || 0.01;
        const ox = (ba.w + bb.w) / 2 - Math.abs(dx); // overlap of their boxes, if any
        const oy = (ba.h + bb.h) / 2 - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          // apart along the shorter way out
          if (ox / ba.w < oy / ba.h) {
            const s = Math.sign(dx) * ox * 0.5;
            a.x -= s;
            b.x += s;
          } else {
            const s = Math.sign(dy) * oy * 0.5;
            a.y -= s;
            b.y += s;
          }
        } else {
          const d2 = dx * dx + dy * dy;
          const f = Math.min(4, 1800 / d2);
          a.vx -= (dx / Math.sqrt(d2)) * f;
          a.vy -= (dy / Math.sqrt(d2)) * f;
          b.vx += (dx / Math.sqrt(d2)) * f;
          b.vy += (dy / Math.sqrt(d2)) * f;
        }
      }
    }
    for (const p of P) {
      p.x += p.vx * cool;
      p.y += p.vy * cool;
      p.vx *= 0.6;
      p.vy *= 0.6;
      const b = box(p);
      p.x = Math.min(W / 2 - b.w / 2, Math.max(-W / 2 + b.w / 2, p.x));
      p.y = Math.min(H / 2 - b.h / 2, Math.max(-H / 2 + b.h / 2, p.y));
    }
  }
  // Layers: each depth a band (prerequisites on top); a band with more than fits across one line is split into
  // staggered lines inside it, left to right as the layout had them.
  const lineOf = new Map(); // node -> its line's y (layers)
  if (opts.layers) {
    const bands = Math.max(1, ...depth.values()) + 1;
    const bandH = H / bands;
    for (let d = 0; d < bands; d++) {
      const row = P.filter((q) => depth.get(q.n.id) === d).sort((a, b) => a.x - b.x);
      if (!row.length) continue;
      const across = row.reduce((sum, q) => sum + box(q).w + 4, 0);
      const lines = Math.max(1, Math.min(row.length, Math.ceil(across / (W * 0.94))));
      const top = H / 2 - d * bandH;
      row.forEach((q, k) => {
        const line = k % lines;
        const y = top - bandH * ((line + 0.5) / lines);
        const b = box(q);
        q.y = Math.min(H / 2 - b.h / 2, Math.max(-H / 2 + b.h / 2, y));
        lineOf.set(q, q.y);
      });
    }
  }
  // Then nothing overlaps where there's room for it: boxes pushed apart (only sideways in layers) until clear.
  for (let pass = 0; pass < 300; pass++) {
    let moved = false;
    for (let i = 0; i < P.length; i++) {
      for (let j = i + 1; j < P.length; j++) {
        const a = P[i];
        const b = P[j];
        const ba = box(a);
        const bb = box(b);
        const dx = b.x - a.x || (hash(a.n.id + b.n.id) - 0.5) * 0.1;
        const dy = b.y - a.y || (hash(b.n.id + a.n.id) - 0.5) * 0.1;
        const ox = (ba.w + bb.w) / 2 - Math.abs(dx) + 2;
        const oy = (ba.h + bb.h) / 2 - Math.abs(dy) + 2;
        if (ox <= 0 || oy <= 0) continue;
        if (opts.layers && lineOf.get(a) !== lineOf.get(b) && oy < 6) continue; // neighbouring lines just touch
        moved = true;
        // The shorter way out first; later the other way too (two pressed against a side can't go sideways).
        if (opts.layers || (pass < 120 ? ox / (ba.w + bb.w) < oy / (ba.h + bb.h) : (pass >> 3) % 2 === 0)) {
          a.x -= (Math.sign(dx) * ox) / 2;
          b.x += (Math.sign(dx) * ox) / 2;
        } else {
          a.y -= (Math.sign(dy) * oy) / 2;
          b.y += (Math.sign(dy) * oy) / 2;
        }
      }
    }
    for (const p of P) {
      const b = box(p);
      p.x = Math.min(W / 2 - b.w / 2, Math.max(-W / 2 + b.w / 2, p.x));
      if (!opts.layers) p.y = Math.min(H / 2 - b.h / 2, Math.max(-H / 2 + b.h / 2, p.y));
    }
    if (!moved) break;
  }
  // Depth: the important come forward, the rest sit back a little (the camera is framed on it all afterwards).
  for (const p of P) {
    p.z = (p.n.importance - 2) * 26 + (hash(`${p.n.id}z`) - 0.5) * 30;
  }
  return new Map(P.map((p) => [p.n.id, [p.x, p.y, p.z]]));
}

export function makeGraph(deps, spec = {}) {
  const { THREE, COLORS, root, three, label, report, touched, onResize } = deps;
  const seenIds = new Set();
  const nodes = (spec.nodes || [])
    .filter((n) => n && n.id != null && !seenIds.has(String(n.id)) && seenIds.add(String(n.id)))
    .map((n) => ({ ...n, id: String(n.id) })); // an id given twice: the first one
  if (!nodes.length) throw new TypeError('oct.graph needs nodes: [{id, label, group, kind, state, detail, importance}]');
  const ids = new Set(nodes.map((n) => n.id));
  const edges = (spec.edges || []).filter((e) => e && ids.has(String(e.from)) && ids.has(String(e.to))).map((e) => ({ ...e, from: String(e.from), to: String(e.to) }));
  const palette = [COLORS.glow, COLORS.mint, COLORS.rose, COLORS.violet, COLORS.ice, '#9fb7d6', '#c9a7e8', '#7fd6d0'];

  // ---- what each look means
  const kindNames = [...new Set(nodes.map((n) => n.kind || 'topic'))];
  const kinds = {};
  kindNames.forEach((k, i) => {
    const given = (spec.kinds || {})[k] || {};
    kinds[k] = { shape: SHAPES.includes(given.shape) ? given.shape : SHAPES[i % SHAPES.length], color: given.color || palette[i % palette.length], label: given.label || k };
  });
  const byState = nodes.some((n) => n.state);
  const states = {};
  if (byState) {
    for (const n of nodes) {
      const s = n.state || 'new';
      if (states[s]) continue;
      const given = (spec.states || {})[s] || {};
      states[s] = { color: given.color || COLORS[STATE_COLORS[s]] || palette[Object.keys(states).length % palette.length], label: given.label || STATE_WORDS[s] || s };
    }
  }
  const edgeKindNames = [...new Set(edges.map((e) => e.kind || 'link'))];
  const edgeKinds = {};
  edgeKindNames.forEach((k, i) => {
    const given = (spec.edgeKinds || {})[k] || {};
    edgeKinds[k] = { color: given.color || [COLORS.ice, COLORS.violet, COLORS.mint, COLORS.rose, COLORS.glow][i % 5], dashed: given.dashed === true, label: given.label || k };
  });

  const el = (tag, cls, text) => {
    const x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text != null) x.textContent = String(text);
    return x;
  };

  // ---- the legend: a strip along the bottom, so the map is laid out and framed above it, never under it
  const legend = el('div', 'g-legend');
  const row = (glyph, color, words, dashed = false) => {
    const r = el('div', 'g-row');
    const g = el('span', dashed ? 'g-glyph g-dash' : 'g-glyph', glyph);
    g.style.color = color;
    r.append(g, el('span', null, words));
    return r;
  };
  if (kindNames.length > 1 || spec.kinds) {
    legend.append(el('div', 'g-head', 'shape'));
    for (const k of kindNames) legend.append(row(GLYPH[kinds[k].shape], byState ? COLORS.text : kinds[k].color, kinds[k].label));
  }
  if (byState) {
    legend.append(el('div', 'g-head', 'colour'));
    for (const s of Object.keys(states)) legend.append(row('●', states[s].color, states[s].label));
  }
  if (edges.length) {
    legend.append(el('div', 'g-head', 'arrows'));
    for (const k of edgeKindNames) legend.append(row('→', edgeKinds[k].color, edgeKinds[k].label, edgeKinds[k].dashed));
  }
  if (legend.childElementCount) root.append(legend);

  // ---- sizes, then where everything goes
  const view = spec.view || three({ grid: false, camera: [0, 0, 14], fog: true });
  // Room kept clear of the map: the buttons and hint along the top, the legend along the bottom.
  const area = { top: 42, bottom: (legend.isConnected ? legend.offsetHeight : 0) + 12, left: 10, right: 10 };
  const W = (root.clientWidth || 600) - area.left - area.right;
  const H = (root.clientHeight || 400) - area.top - area.bottom;
  // Sizes: a map with more than fits at full size comes smaller (down to 72 %), so everything still has its room
  // rather than labels hiding each other; past that, the less important labels give way on screen.
  const sizeAll = (k) => {
    for (const n of nodes) {
      n.importance = Math.min(3, Math.max(1, Number(n.importance) || 2));
      n.size = (7 + n.importance * 4) * k; // radius, layout px
      n.labelPx = Math.max(10, (11 + n.importance * 1.5) * k);
      n.labelW = measureText(n.label ?? n.id, n.labelPx) + 14;
      n.labelH = n.labelPx + 8;
    }
  };
  sizeAll(1);
  const needed = nodes.reduce((sum, n) => sum + (Math.max(n.size * 2, n.labelW) + 18) * (n.size * 2 + n.labelH + 14), 0) * 1.8;
  if (needed > W * H) sizeAll(Math.max(0.72, Math.sqrt((W * H) / needed)));
  const where = layout(nodes, edges, { width: W, height: H, layers: spec.layout === 'layers' });
  const world = (id) => {
    const [x, y, z] = where.get(id);
    return new THREE.Vector3(x / PX, y / PX, z / PX);
  };

  // ---- the scene
  const map = new THREE.Group();
  view.scene.add(map);
  const geometryFor = (shape, r) => {
    switch (shape) {
      case 'box': return new THREE.BoxGeometry(r * 1.6, r * 1.6, r * 1.6);
      case 'octahedron': return new THREE.OctahedronGeometry(r * 1.2);
      case 'cone': return new THREE.ConeGeometry(r, r * 1.9, 20);
      case 'dodecahedron': return new THREE.DodecahedronGeometry(r * 1.1);
      case 'torus': return new THREE.TorusGeometry(r * 0.8, r * 0.32, 12, 28);
      case 'cylinder': return new THREE.CylinderGeometry(r * 0.8, r * 0.8, r * 1.6, 20);
      case 'tetrahedron': return new THREE.TetrahedronGeometry(r * 1.3);
      default: return new THREE.SphereGeometry(r, 28, 18);
    }
  }; // fmt: skip
  const colorOf = (n) => (byState ? states[n.state || 'new'].color : kinds[n.kind || 'topic'].color);
  const items = new Map(); // id -> {n, mesh, material, tag, edges: []}
  for (const n of nodes) {
    const material = new THREE.MeshStandardMaterial({ color: colorOf(n), roughness: 0.55, metalness: 0.05, emissive: colorOf(n), emissiveIntensity: 0.12, transparent: true });
    const mesh = new THREE.Mesh(geometryFor(kinds[n.kind || 'topic'].shape, n.size / PX), material);
    mesh.position.copy(world(n.id));
    mesh.userData.node = n.id;
    map.add(mesh);
    const tag = label(n.label ?? n.id, { at: mesh.position.clone().add(new THREE.Vector3(0, -(n.size + 4 + n.labelH / 2) / PX, 0)), size: n.labelPx, priority: n.importance * 10 + (n.state === 'weak' ? 5 : 0), plate: true });
    map.add(tag);
    if (n.state === 'weak') tag.el.classList.add('label-alert');
    items.set(n.id, { n, mesh, material, tag, edges: [] });
  }
  const hitEdges = [];
  for (const e of edges) {
    const look = edgeKinds[e.kind || 'link'];
    const a = items.get(e.from);
    const b = items.get(e.to);
    const from = a.mesh.position;
    const to = b.mesh.position;
    const dir = to.clone().sub(from);
    const len = dir.length();
    if (len < 1e-6) continue;
    const unit = dir.clone().normalize();
    const start = from.clone().addScaledVector(unit, a.n.size / PX + 0.05);
    const end = to.clone().addScaledVector(unit, -(b.n.size / PX + 0.08));
    const material = new THREE.MeshBasicMaterial({ color: look.color, transparent: true, opacity: look.dashed ? 0.55 : 0.8 });
    const g = new THREE.Group();
    const shaftLen = Math.max(0.01, start.distanceTo(end) - 0.28);
    const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, shaftLen, 8), material);
    const head = new THREE.Mesh(new THREE.ConeGeometry(0.12, 0.28, 14), material);
    const hit = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, start.distanceTo(end), 6), new THREE.MeshBasicMaterial({ visible: false }));
    const up = new THREE.Vector3(0, 1, 0);
    const q = new THREE.Quaternion().setFromUnitVectors(up, unit);
    shaft.quaternion.copy(q);
    shaft.position.copy(start).addScaledVector(unit, shaftLen / 2);
    head.quaternion.copy(q);
    head.position.copy(end).addScaledVector(unit, -0.14);
    hit.quaternion.copy(q);
    hit.position.copy(start).lerp(end, 0.5);
    hit.userData.edge = e;
    g.add(shaft, head, hit);
    map.add(g);
    const item = { e, group: g, material, base: material.opacity };
    a.edges.push(item);
    b.edges.push(item);
    hitEdges.push(hit);
  }

  // ---- overlays: legend, controls, inspector, hint, tooltip

  const tools = el('div', 'g-tools');
  const button = (glyph, title, fn) => {
    const b = el('button', 'g-btn', glyph);
    b.type = 'button';
    b.title = title;
    b.setAttribute('aria-label', title);
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      fn();
    });
    tools.append(b);
    return b;
  };
  root.append(tools);

  const inspector = el('div', 'g-inspector');
  inspector.hidden = true;
  root.append(inspector);
  const tip = el('div', 'g-tip');
  tip.hidden = true;
  root.append(tip);
  const hint = el('div', 'g-hint', 'drag to turn · scroll to zoom · click a topic');
  root.append(hint);
  let hinted = false;
  const dropHint = () => {
    if (hinted) return;
    hinted = true;
    hint.classList.add('gone');
    setTimeout(() => hint.remove(), 600);
  };
  setTimeout(dropHint, 6000);

  // ---- the camera: framed on the map, moved smoothly
  const home = { pos: null, target: null };
  let gliding = 0; // a newer move takes over from the one under way
  const glide = (toPos, toTarget, ms = 420) => {
    const fromPos = view.camera.position.clone();
    const fromTarget = view.controls.target.clone();
    const t0 = performance.now();
    const mine = ++gliding;
    const step = (now) => {
      if (mine !== gliding) return;
      const t = Math.min(1, (now - t0) / ms);
      const k = 1 - (1 - t) ** 3;
      view.camera.position.lerpVectors(fromPos, toPos, k);
      view.controls.target.lerpVectors(fromTarget, toTarget, k);
      view.controls.update();
      view.draw();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  const frame = () => {
    view.camera.position.set(0, 0, 14);
    view.controls.target.set(0, 0, 0);
    view.fit(map, { pad: 0.04, area });
    home.pos = view.camera.position.clone();
    home.target = view.controls.target.clone();
  };
  frame();
  // A resized panel: framed again for its new shape (home too), keeping a selection's inspector as it was.
  if (onResize) onResize(() => frame());
  const zoom = (k) => {
    const t = view.controls.target;
    glide(t.clone().add(view.camera.position.clone().sub(t).multiplyScalar(k)), t.clone(), 260);
  };
  const focus = (id) => {
    const it = items.get(id);
    if (!it) return;
    const p = it.mesh.position.clone();
    map.localToWorld(p);
    const dist = Math.max(3, home.pos.distanceTo(home.target) * 0.62);
    const dir = view.camera.position.clone().sub(view.controls.target).normalize();
    glide(p.clone().addScaledVector(dir, dist), p);
  };
  button('⟲', 'Reset view (R)', () => glide(home.pos, home.target));
  button('+', 'Zoom in (+)', () => zoom(0.75));
  button('−', 'Zoom out (−)', () => zoom(1.33));
  const focusBtn = button('◎', 'Focus the selected topic (F)', () => selected && focus(selected));
  focusBtn.disabled = true;

  // ---- hover and selection
  let hovered = null;
  let selected = null;
  const neighbours = (id) => {
    const out = new Set([id]);
    for (const e of items.get(id).edges) out.add(e.e.from).add(e.e.to);
    return out;
  };
  const paint = () => {
    const focusId = hovered || selected;
    const near = focusId ? neighbours(focusId) : null;
    for (const [id, it] of items) {
      const lit = !near || near.has(id);
      it.material.opacity = lit ? 1 : 0.22;
      it.material.emissiveIntensity = id === focusId ? 0.45 : 0.12;
      it.mesh.scale.setScalar(id === selected ? 1.3 : id === hovered ? 1.15 : 1);
      it.tag.el.classList.toggle('label-dim', !lit);
      it.tag.el.classList.toggle('label-on', id === focusId);
      it.tag.priority = id === focusId ? 1000 : it.tag.basePriority;
    }
    for (const it of items.values()) {
      for (const e of it.edges) {
        const lit = !focusId || e.e.from === focusId || e.e.to === focusId;
        e.material.opacity = lit ? e.base : 0.08;
      }
    }
    view.render();
  };
  for (const it of items.values()) it.tag.basePriority = it.tag.priority;

  const relation = (e, out) => {
    const k = edgeKinds[e.kind || 'link'];
    const other = items.get(out ? e.to : e.from).n;
    const li = el('li');
    const a = el('button', 'g-link', other.label ?? other.id);
    a.type = 'button';
    a.addEventListener('click', () => select(other.id, true));
    const what = el('span', 'g-rel', `${k.label}${e.label ? `: ${e.label}` : ''}`);
    what.style.color = k.color;
    li.append(a, what);
    return li;
  };
  const inspect = (id) => {
    inspector.replaceChildren();
    if (!id) {
      inspector.hidden = true;
      return;
    }
    const { n } = items.get(id);
    const close = el('button', 'g-close', '×');
    close.type = 'button';
    close.title = 'Close (Esc)';
    close.addEventListener('click', () => select(null));
    const chips = el('div', 'g-chips');
    const chip = (words, color) => {
      const c = el('span', 'g-chip', words);
      c.style.borderColor = color;
      c.style.color = color;
      chips.append(c);
    };
    chip(kinds[n.kind || 'topic'].label, kinds[n.kind || 'topic'].color);
    if (byState) chip(states[n.state || 'new'].label, states[n.state || 'new'].color);
    if (n.group != null && n.group !== '') chip(String(n.group), COLORS.muted);
    inspector.append(close, el('div', 'g-title', n.label ?? n.id), chips);
    if (n.detail) inspector.append(el('p', 'g-detail', n.detail));
    const ins = items.get(id).edges.filter((e) => e.e.to === id);
    const outs = items.get(id).edges.filter((e) => e.e.from === id);
    if (ins.length) {
      inspector.append(el('div', 'g-head', 'comes from'));
      const ul = el('ul');
      ins.forEach((e) => ul.append(relation(e.e, false)));
      inspector.append(ul);
    }
    if (outs.length) {
      inspector.append(el('div', 'g-head', 'leads to'));
      const ul = el('ul');
      outs.forEach((e) => ul.append(relation(e.e, true)));
      inspector.append(ul);
    }
    const seenAt = items.get(id).mesh.getWorldPosition(new THREE.Vector3()).project(view.camera);
    inspector.classList.toggle('left', seenAt.x > 0.1); // away from the topic: on the left when it's right of centre
    inspector.hidden = false;
  };
  const listeners = [];
  const select = (id, move = false) => {
    selected = id && items.has(id) ? id : null;
    focusBtn.disabled = !selected;
    inspect(selected);
    paint();
    if (move && selected) focus(selected);
    report({ selected: selected ? items.get(selected).n.label ?? selected : null });
    for (const fn of listeners) fn(selected);
  };

  const ray = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const canvas = view.renderer.domElement;
  const pick = (ev) => {
    const r = canvas.getBoundingClientRect();
    pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(pointer, view.camera);
    const meshes = [...items.values()].map((it) => it.mesh);
    const hitNode = ray.intersectObjects(meshes, false)[0];
    if (hitNode) return { node: hitNode.object.userData.node };
    const hitEdge = ray.intersectObjects(hitEdges, false)[0];
    return hitEdge ? { edge: hitEdge.object.userData.edge } : {};
  };
  let downAt = null;
  canvas.addEventListener('pointerdown', (ev) => {
    downAt = [ev.clientX, ev.clientY];
    dropHint();
    touched();
  });
  canvas.addEventListener('pointermove', (ev) => {
    const got = pick(ev);
    const id = got.node || null;
    canvas.style.cursor = got.node || got.edge ? 'pointer' : '';
    if (id !== hovered) {
      hovered = id;
      paint();
    }
    if (got.edge) {
      const k = edgeKinds[got.edge.kind || 'link'];
      const a = items.get(got.edge.from).n;
      const b = items.get(got.edge.to).n;
      tip.textContent = `${a.label ?? a.id} → ${b.label ?? b.id} · ${k.label}${got.edge.label ? `: ${got.edge.label}` : ''}`;
    } else if (id) {
      const n = items.get(id).n;
      tip.textContent = n.detail ? `${n.label ?? n.id}: ${n.detail}` : String(n.label ?? n.id);
    }
    tip.hidden = !(got.edge || id);
    if (!tip.hidden) {
      const r = root.getBoundingClientRect();
      tip.style.left = `${Math.min(ev.clientX - r.left + 14, r.width - tip.offsetWidth - 6)}px`;
      tip.style.top = `${Math.max(6, ev.clientY - r.top - tip.offsetHeight - 10)}px`;
    }
  });
  canvas.addEventListener('pointerleave', () => {
    tip.hidden = true;
    if (hovered) {
      hovered = null;
      paint();
    }
  });
  canvas.addEventListener('click', (ev) => {
    if (downAt && Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5) return; // that was a turn
    const got = pick(ev);
    select(got.node || null);
  });
  window.addEventListener('keydown', (ev) => {
    // Not while typing into the program's own boxes, nor with a modifier (Ctrl+- is the page's), and Tab only
    // when nothing in particular has the keyboard.
    const t = ev.target;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName || ''))) return;
    if (ev.key === 'Tab' && t && t !== document.body && t !== document.documentElement) return;
    const order = [...items.keys()];
    if (ev.key === 'r' || ev.key === 'R') glide(home.pos, home.target);
    else if (ev.key === '+' || ev.key === '=') zoom(0.75);
    else if (ev.key === '-' || ev.key === '_') zoom(1.33);
    else if ((ev.key === 'f' || ev.key === 'F') && selected) focus(selected);
    else if (ev.key === 'Escape') select(null);
    else if (ev.key === 'Tab') {
      ev.preventDefault();
      const k = selected ? order.indexOf(selected) : -1;
      select(order[(k + (ev.shiftKey ? order.length - 1 : 1)) % order.length], false);
    } else return;
    dropHint();
  });
  paint();

  return {
    view,
    group: map,
    select: (id) => select(id == null ? null : String(id)), // lit, with its inspector; the view stays (focus moves it)
    focus: (id) => focus(String(id)),
    reset: () => glide(home.pos, home.target),
    onSelect(fn) {
      if (typeof fn === 'function') listeners.push(fn);
    },
    // A node's state (and colour) changed: "SA is now solid".
    setState(id, state) {
      const it = items.get(String(id));
      if (!it || !byState) return;
      it.n.state = state;
      if (!states[state]) states[state] = { color: COLORS[STATE_COLORS[state]] || COLORS.text, label: STATE_WORDS[state] || state };
      it.material.color.set(states[state].color);
      it.material.emissive.set(states[state].color);
      it.tag.el.classList.toggle('label-alert', state === 'weak');
      if (selected === String(id)) inspect(selected);
      view.render();
    },
    position: (id) => (items.has(String(id)) ? items.get(String(id)).mesh.position.clone() : null),
  };
}
