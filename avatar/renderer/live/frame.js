// A live block's page: runs the program Octavia wrote for it (a 3D scene to turn, a graph to drag, a setup with
// sliders) with three.js and a few helpers of hers (`oct`). It sits in a sandboxed iframe on her panel, with an
// opaque origin: it can't reach the panel's page or its bridge to her, store anything, or ask anything of the
// network (its CSP has no connect-src, see avatar/lib/scheme.js), so whatever the program does stays drawing on
// this canvas. The panel's page talks to it only through postMessage:
//
//   panel -> here: {live: 1, type: 'run', code, params}   once, after our 'hello'
//                  {live: 1, type: 'params', values}      a slider moved, or she changed a value
//                  {live: 1, type: 'ping', n}             are you still there? (a program stuck in a loop can't answer)
//   here -> panel: hello | ok (ran ~0.6 s without an error) | error {message, line} | state {state} (oct.report)
//                  set {id, value} (the program moved one of its own sliders) | pong {n}
//
// Nothing loops unless the program asks (oct.loop), at most 30 frames a second, and a loop pauses after two
// minutes with nobody touching it (a see-through window is redrawn whole for every frame): a click goes on.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { ICONS } from '../icons/tabler-filled.js';
import { iconSvg } from '../icons/icons.js';
import { makeGraph } from './graph.js';

const root = document.getElementById('root');
const pausedEl = document.getElementById('paused');
const faultEl = document.getElementById('fault');

// Nothing in here needs to talk to anyone: the CSP already refuses requests; WebRTC isn't covered by it.
for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel', 'WebTransport']) {
  try {
    Object.defineProperty(window, name, { value: undefined, configurable: false, writable: false });
  } catch {
    // not there in this build
  }
}

const post = (msg) => window.parent.postMessage({ live: 1, ...msg }, '*');

const COLORS = Object.freeze({
  blue: '#4c9cf5', // her line colour
  glow: '#7cc0ff',
  bright: '#e2f0ff',
  text: '#d8e6f7',
  muted: '#7f97b8',
  haze: '#2b4a73',
  deep: '#0b1424', // the panel's glass
  rose: '#e08f9a',
  mint: '#86d6b4',
  violet: '#a99cf0',
  ice: '#bfe3ff',
});
const BACKDROP = 0x0b1424;
const IDLE_PAUSE_MS = 120_000;
const MAX_FPS = 30;
const STATE_GAP_MS = 250;

const values = {}; // the sliders' values now, by id
const paramListeners = [];
const resizeListeners = [];
const loops = new Set();
let ran = false;
let failed = false;
let lastTouch = performance.now();

// ---------------------------------------------------------------- errors: the first one goes back to her

const OFFSET = 2; // an AsyncFunction's body starts on its third line
function whereIn(stack) {
  const m = /<anonymous>:(\d+):(\d+)/.exec(String(stack || ''));
  return m ? { line: Math.max(1, Number(m[1]) - OFFSET), col: Number(m[2]) } : null;
}

function fail(err) {
  if (failed) return;
  failed = true;
  for (const loop of loops) loop.stop();
  const message = String((err && err.message) || err || 'unknown error').slice(0, 400);
  const name = err && err.name && !message.startsWith(err.name) ? `${err.name}: ` : '';
  const at = whereIn(err && err.stack);
  post({ type: 'error', message: `${name}${message}`, line: at ? at.line : null, col: at ? at.col : null });
  faultEl.textContent = `${name}${message}${at ? ` (line ${at.line})` : ''}`;
  faultEl.hidden = false;
}
window.addEventListener('error', (e) => fail(e.error || e.message));
window.addEventListener('unhandledrejection', (e) => fail(e.reason));

// ---------------------------------------------------------------- frames, paused when nobody's looking

function touched() {
  lastTouch = performance.now();
  if (!pausedEl.hidden) {
    pausedEl.hidden = true;
    for (const loop of loops) loop.wake();
  }
}
for (const type of ['pointerdown', 'pointermove', 'wheel', 'keydown']) window.addEventListener(type, touched, { passive: true, capture: true });

const renderers = new Set(); // three() scenes: drawn after each loop frame, and when their camera moves

function loop(fn) {
  if (typeof fn !== 'function') throw new TypeError('oct.loop needs a function (dt, t) => {...}');
  let raf = 0;
  let last = 0;
  let t = 0;
  let stopped = false;
  const step = (now) => {
    raf = 0;
    if (stopped || failed) return;
    if (now - lastTouch > IDLE_PAUSE_MS) {
      pausedEl.hidden = false;
      last = 0;
      return; // touched() wakes it
    }
    const gap = 1000 / Math.min(MAX_FPS, Math.max(1, Number(oct.fps) || MAX_FPS));
    if (last && now - last < gap - 2) {
      raf = requestAnimationFrame(step);
      return;
    }
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 1 / MAX_FPS;
    last = now;
    t += dt;
    try {
      fn(dt, t);
      for (const r of renderers) r.draw();
    } catch (err) {
      fail(err);
      return;
    }
    raf = requestAnimationFrame(step);
  };
  const handle = {
    stop() {
      stopped = true;
      if (raf) cancelAnimationFrame(raf);
      loops.delete(handle);
    },
    wake() {
      if (!stopped && !raf) raf = requestAnimationFrame(step);
    },
  };
  loops.add(handle);
  handle.wake();
  return handle.stop;
}

// ---------------------------------------------------------------- 3D: a scene in her look, turned with the mouse

const v3 = (p, fallback = [0, 0, 0]) => {
  if (p && p.isVector3) return p.clone();
  const a = Array.isArray(p) ? p : fallback;
  return new THREE.Vector3(Number(a[0]) || 0, Number(a[1]) || 0, Number(a[2]) || 0);
};

function three(opts = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, preserveDrawingBuffer: true });
  // Drawn a little under the screen's resolution and smoothed up: the PS2's soft picture, and cheaper.
  const res = Math.min(1, Math.max(0.35, Number(opts.res) || 0.8));
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2) * res);
  renderer.setClearColor(BACKDROP, opts.backdrop === false ? 0 : 0.55);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  root.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(Number(opts.fov) || 45, 1, 0.05, 2000);
  camera.position.copy(v3(opts.camera, [4, 3, 6]));
  const target = v3(opts.target);
  if (opts.fog !== false) {
    const d = camera.position.distanceTo(target);
    scene.fog = new THREE.Fog(BACKDROP, d * 1.2, d * 3.2);
  }
  scene.add(new THREE.HemisphereLight(0xcfe6ff, 0x0b1424, 1.6));
  const sun = new THREE.DirectionalLight(0xe2f0ff, 1.8);
  sun.position.set(5, 8, 4);
  scene.add(sun);
  if (opts.grid !== false) {
    const size = Number(opts.grid) > 0 ? Number(opts.grid) : 10;
    const grid = new THREE.GridHelper(size, size, 0x4c9cf5, 0x22406a);
    grid.material.transparent = true;
    grid.material.opacity = 0.45;
    scene.add(grid);
  }
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(target);
  controls.enableDamping = false; // drawn on demand: nothing runs between moves
  controls.enablePan = opts.pan === true;
  let queued = false;
  const view = {
    scene,
    camera,
    renderer,
    controls,
    draw() {
      queued = false;
      renderer.render(scene, camera);
      placeLabels(scene, camera);
    },
    render() {
      // Several changes in one go draw once, on the next frame.
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => queued && view.draw());
    },
  };
  // Frames an object (or the whole scene): the camera keeps its direction and comes as close as it can with every
  // corner of the object (and its labels' points) in view, `pad` (a share of the view) from the edges. It used to
  // fit a sphere round the object, which left most of a flat map's view empty (the CT3 map, 2026-11-22).
  view.fit = (object = scene, opts) => {
    if (!opts || typeof opts !== 'object') return fitSphere(object, Number(opts) || 1.05); // as before: holds when it turns
    const pad = Math.min(0.4, Math.max(0, Number(opts.pad ?? 0.05)));
    const box = new THREE.Box3().setFromObject(object);
    object.traverse((o) => {
      if (o.isOctLabel) box.expandByPoint(o.getWorldPosition(new THREE.Vector3()));
    });
    if (box.isEmpty()) return view;
    const centre = box.getCenter(new THREE.Vector3());
    const dir = camera.position.clone().sub(controls.target);
    if (dir.lengthSq() < 1e-9) dir.set(1, 0.8, 1.2);
    dir.normalize();
    const corners = [];
    for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) corners.push(new THREE.Vector3(x, y, z));
    // `area`: pixels kept clear at each side ({top, bottom, left, right}, for a legend or buttons); the picture is
    // fitted to what's left and moved over to its middle (setViewOffset).
    const W = root.clientWidth || 1;
    const H = root.clientHeight || 1;
    const a = { top: 0, bottom: 0, left: 0, right: 0, ...(typeof opts === 'object' && opts.area ? opts.area : {}) };
    const fx = Math.max(0.2, (W - a.left - a.right) / W) * (1 - 2 * pad);
    const fy = Math.max(0.2, (H - a.top - a.bottom) / H) * (1 - 2 * pad);
    camera.clearViewOffset();
    const fits = (d) => {
      camera.position.copy(centre).addScaledVector(dir, d);
      camera.lookAt(centre);
      camera.updateMatrixWorld();
      return corners.every((c) => {
        const p = c.clone().project(camera);
        return p.z < 1 && Math.abs(p.x) <= fx && Math.abs(p.y) <= fy;
      });
    };
    let lo = 1e-3;
    let hi = Math.max(1, box.getSize(new THREE.Vector3()).length() * 4);
    while (!fits(hi) && hi < 1e6) hi *= 2;
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      if (fits(mid)) hi = mid;
      else lo = mid;
    }
    const dist = hi;
    controls.target.copy(centre);
    camera.position.copy(centre).addScaledVector(dir, dist);
    camera.near = dist / 100;
    camera.far = dist * 100;
    const dx = (a.left - a.right) / 2;
    const dy = (a.top - a.bottom) / 2;
    if (dx || dy) camera.setViewOffset(W, H, -dx, -dy, W, H);
    view.offsetArea = dx || dy ? a : null; // kept through a resize
    camera.updateProjectionMatrix();
    if (scene.fog) {
      scene.fog.near = dist * 1.2;
      scene.fog.far = dist * 3.2;
    }
    controls.update();
    view.render();
    return view;
  };
  // The fit programs had first: a sphere round the object, so it stays in view however it turns.
  const fitSphere = (object, margin) => {
    const box = new THREE.Box3().setFromObject(object);
    if (box.isEmpty()) return view;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const half = (camera.fov * Math.PI) / 360;
    const across = Math.atan(Math.tan(half) * camera.aspect);
    const dist = (Math.max(sphere.radius, 1e-3) * margin) / Math.sin(Math.min(half, across));
    const dir = camera.position.clone().sub(controls.target);
    if (dir.lengthSq() < 1e-9) dir.set(1, 0.8, 1.2);
    camera.clearViewOffset();
    view.offsetArea = null;
    controls.target.copy(sphere.center);
    camera.position.copy(sphere.center).addScaledVector(dir.normalize(), dist);
    camera.near = dist / 100;
    camera.far = dist * 100;
    camera.updateProjectionMatrix();
    if (scene.fog) {
      scene.fog.near = dist * 1.2;
      scene.fog.far = dist * 3.2;
    }
    controls.update();
    view.render();
    return view;
  };
  controls.addEventListener('change', () => view.render());
  const fit = () => {
    const w = root.clientWidth || 1;
    const h = root.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    const a = view.offsetArea;
    if (a) camera.setViewOffset(w, h, -(a.left - a.right) / 2, -(a.top - a.bottom) / 2, w, h);
    camera.updateProjectionMatrix();
    view.render();
  };
  resizeListeners.push(fit);
  fit();
  controls.update();
  renderers.add(view);
  latestScene = scene;
  for (const anchor of pendingLabels.splice(0)) if (!anchor.parent) scene.add(anchor);
  view.render();
  return view;
}

// A word or number at a point of the 3D scene: crisp text on the page, kept over that point as the scene turns
// (always readable, never hidden behind things). It's an Object3D: it goes into the latest oct.three() scene by
// itself, and moves with whatever it's added to. `size`: its text size in pixels (14).
const labelLayer = document.createElement('div');
labelLayer.className = 'labels';
// The labels whose text is on the page now. One that's no longer in any scene (removed, its group cleared or taken
// out) has its text taken off at the next draw, and put back if it's ever in a scene again: a program that rebuilds
// its labels on every change left ghosts of the old ones behind (2026-11-22, the MRV map).
const labels = new Set();
let latestScene = null;
const pendingLabels = [];

// The filled Tabler icon called `name` (tabler.io/icons, filled set only, bundled: nothing is fetched), as its
// paths' data. 'star', 'star-filled', 'IconStarFilled' and 'icon-star' all find the star. A name the filled set
// doesn't have draws a plain dot instead of stopping the program, and is reported (list_visuals shows it).
const missingIcons = new Set();
function iconPaths(name) {
  const raw = String(name || '').trim();
  const key = raw
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .toLowerCase()
    .replace(/[\s_]+/g, '-')
    .replace(/-filled$/, '');
  for (const k of [key, key.replace(/^icon-/, '')]) if (Object.hasOwn(ICONS, k)) return ICONS[k];
  if (!missingIcons.has(raw)) {
    missingIcons.add(raw);
    sendState();
  }
  return ICONS.point;
}

// An icon as an <svg> element, for the program's own page: {size: pixels (24), color}.
function icon(name, opts = {}) {
  const size = Number(opts.size);
  return iconSvg(iconPaths(name), { size: Number.isFinite(size) && size > 0 ? size : 24, color: opts.color || COLORS.glow });
}

function label(text, opts = {}) {
  const anchor = new THREE.Object3D();
  const el = document.createElement('div');
  el.className = 'label';
  const glyph = opts.icon ? iconSvg(iconPaths(opts.icon), { className: 'label-icon' }) : null;
  const raw = Number(opts.size);
  const px = Number.isFinite(raw) && raw >= 4 ? Math.min(48, Math.max(9, raw)) : 14; // a scene-unit size (old) reads as the default
  el.style.fontSize = `${px}px`;
  if (opts.plate !== false) el.classList.add('plate'); // on a backing plate: readable on anything
  // Labels given a priority give way to each other when they'd overlap on screen: the lower one is hidden.
  anchor.priority = opts.priority != null && Number.isFinite(Number(opts.priority)) ? Number(opts.priority) : null;
  el.style.color = opts.color || COLORS.bright;
  anchor.el = el;
  anchor.isOctLabel = true;
  // Done with it for good: out of its scene, and its text off the page.
  anchor.dispose = () => {
    anchor.removeFromParent();
    el.remove();
    labels.delete(anchor);
    return anchor;
  };
  anchor.setText = (value) => {
    el.textContent = value == null ? '' : String(value);
    if (glyph) el.prepend(glyph); // an icon before the words (or alone)
    return anchor;
  };
  anchor.setColor = (color) => {
    el.style.color = String(color);
    return anchor;
  };
  anchor.setText(text);
  if (opts.at) anchor.position.copy(v3(opts.at));
  if (!labelLayer.isConnected) root.append(labelLayer);
  labelLayer.append(el);
  labels.add(anchor);
  if (latestScene) latestScene.add(anchor);
  else pendingLabels.push(anchor);
  for (const r of renderers) r.render();
  return anchor;
}

const seen = new THREE.Vector3();
// Puts each label of `scene` over its point as `camera` sees it; ones behind the camera or invisible are hidden.
// Labels in no scene at all lose their text from the page (another view's scene keeps its own).
function placeLabels(scene, camera) {
  const w = root.clientWidth;
  const h = root.clientHeight;
  scene.traverse((o) => {
    if (o.isOctLabel && !labels.has(o)) {
      labels.add(o); // back in a scene after it left one
      if (!labelLayer.isConnected) root.append(labelLayer);
      labelLayer.append(o.el);
    }
  });
  for (const anchor of [...labels]) {
    let shown = Boolean(anchor.el.textContent || anchor.el.querySelector('.label-icon')); // words, or an icon alone
    let top = anchor;
    for (let o = anchor; o; o = o.parent) {
      if (!o.visible) shown = false;
      top = o;
    }
    if (top !== scene) {
      if (!top.isScene && !pendingLabels.includes(anchor)) {
        anchor.el.remove(); // in no scene now: no ghost of it stays
        labels.delete(anchor);
      }
      continue; // another view's: it places its own
    }
    if (shown) {
      anchor.getWorldPosition(seen).project(camera);
      shown = seen.z > -1 && seen.z < 1;
    }
    anchor.el.style.display = shown ? '' : 'none';
    if (shown) {
      anchor.screen = [((seen.x + 1) / 2) * w, ((1 - seen.y) / 2) * h];
      anchor.el.style.transform = `translate(${anchor.screen[0]}px, ${anchor.screen[1]}px) translate(-50%, -50%)`;
      if (anchor.priority != null) placed.push(anchor);
    }
  }
  declutter(placed);
  placed.length = 0;
}

// Labels that would sit on each other: the more important stays, the other is hidden until there's room (turn the
// map, zoom in, or hover its topic). Sizes are read once per text, so this costs little per frame.
const placed = [];
function declutter(shown) {
  const kept = [];
  shown.sort((a, b) => b.priority - a.priority);
  for (const anchor of shown) {
    // Every size read before any class is set: reading after a change forces a layout per label.
    const text = anchor.el.textContent;
    if (anchor.sizedFor !== text || !anchor.box) {
      anchor.box = [anchor.el.offsetWidth, anchor.el.offsetHeight];
      anchor.sizedFor = text;
    }
  }
  const hidden = new Set();
  for (const anchor of shown) {
    const [x, y] = anchor.screen;
    const [bw, bh] = anchor.box;
    const r = { l: x - bw / 2 - 2, r: x + bw / 2 + 2, t: y - bh / 2 - 1, b: y + bh / 2 + 1 };
    const hit = kept.some((k) => r.l < k.r && r.r > k.l && r.t < k.b && r.b > k.t);
    if (hit) hidden.add(anchor);
    else kept.push(r);
  }
  for (const anchor of shown) anchor.el.classList.toggle('crowded', hidden.has(anchor));
}

// A direction in the world (gravity, a wind) as `object` sees it, in its own turned frame: for forces on things
// inside a group you rotate. Returns [x, y, z].
function local(object, direction) {
  object.updateWorldMatrix(true, false);
  const q = object.getWorldQuaternion(new THREE.Quaternion()).invert();
  const v = v3(direction).applyQuaternion(q);
  return [v.x, v.y, v.z];
}

// A thick arrow from `from` to `to` (arrays or vectors); .set(from, to) moves it.
function arrow(from, to, opts = {}) {
  const color = new THREE.Color(opts.color || COLORS.glow);
  const material = new THREE.MeshBasicMaterial({ color });
  const radius = Number(opts.radius) || 0.03;
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, 1, 10), material);
  const head = new THREE.Mesh(new THREE.ConeGeometry(radius * 3, 1, 14), material);
  const group = new THREE.Group();
  group.add(shaft, head);
  const up = new THREE.Vector3(0, 1, 0);
  group.set = (a, b) => {
    const start = v3(a);
    const dir = v3(b).sub(start);
    const length = dir.length();
    group.visible = length > 1e-6;
    if (!group.visible) return group;
    const headLength = Math.min(length * 0.35, radius * 8);
    group.position.copy(start);
    group.quaternion.setFromUnitVectors(up, dir.clone().normalize());
    shaft.scale.set(1, Math.max(1e-4, length - headLength), 1);
    shaft.position.set(0, (length - headLength) / 2, 0);
    head.scale.set(1, headLength, 1);
    head.position.set(0, length - headLength / 2, 0);
    return group;
  };
  group.material = material;
  return group.set(from, to);
}

// x, y and z arrows from the origin, labelled. `names`: their labels (['x', 'y', 'z']).
function axes(length = 2, opts = {}) {
  const group = new THREE.Group();
  const names = opts.names || ['x', 'y', 'z'];
  const tones = [COLORS.rose, COLORS.mint, COLORS.glow];
  [[1, 0, 0], [0, 1, 0], [0, 0, 1]].forEach((d, k) => {
    const end = d.map((c) => c * length);
    group.add(arrow([0, 0, 0], end, { color: tones[k], radius: 0.015 }));
    if (names[k]) group.add(label(names[k], { at: end.map((c) => c * 1.08), size: 13, color: tones[k] }));
  });
  return group;
}

// ---------------------------------------------------------------- 2D: a canvas, and a graph plane on it

// `sharp` (oct.plane's own): drawn at the screen's resolution, in CSS pixels. A bare one (oct.canvas) is exactly
// oct.width x oct.height pixels, so a program that sizes things by canvas.width gets what it expects.
function canvas2d(sharp = false) {
  const el = document.createElement('canvas');
  root.append(el);
  const ctx = el.getContext('2d');
  ctx.getContext = () => ctx; // oct.canvas().getContext('2d') works too
  const fit = () => {
    const ratio = sharp ? Math.min(window.devicePixelRatio || 1, 2) : 1;
    const w = Math.max(1, Math.round(root.clientWidth * ratio));
    const h = Math.max(1, Math.round(root.clientHeight * ratio));
    if (el.width === w && el.height === h) return; // sizing a canvas wipes it: only when it really changed
    el.width = w;
    el.height = h;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  };
  fit();
  resizeListeners.push(fit);
  return Object.assign(ctx, { width: () => root.clientWidth, height: () => root.clientHeight });
}

// A plane with x and y in its own units: p.draw(fn) draws with fn (again on every slider move and resize); p.X / p.Y
// turn units into pixels; axes, curve, point, vector, segment, text, fill draw in units; p.handle makes a point they
// can drag.
function plane(opts = {}) {
  const ctx = canvas2d(true);
  const range = { x: (opts.x || [-5, 5]).map(Number), y: (opts.y || [-3, 3]).map(Number) };
  const margin = 18;
  let view = { ...range };
  const handles = [];
  let drawer = null;
  const p = { ctx };
  const W = () => root.clientWidth;
  const H = () => root.clientHeight;
  const sync = () => {
    view = { x: [...range.x], y: [...range.y] };
    if (opts.equal) {
      // One unit is as long across as up: the narrower range grows round its middle.
      const sx = (W() - 2 * margin) / (view.x[1] - view.x[0]);
      const sy = (H() - 2 * margin) / (view.y[1] - view.y[0]);
      if (sx > sy) {
        const half = ((W() - 2 * margin) / sy) / 2;
        const mid = (view.x[0] + view.x[1]) / 2;
        view.x = [mid - half, mid + half];
      } else {
        const half = ((H() - 2 * margin) / sx) / 2;
        const mid = (view.y[0] + view.y[1]) / 2;
        view.y = [mid - half, mid + half];
      }
    }
  };
  p.X = (x) => margin + ((x - view.x[0]) / (view.x[1] - view.x[0])) * (W() - 2 * margin);
  p.Y = (y) => H() - margin - ((y - view.y[0]) / (view.y[1] - view.y[0])) * (H() - 2 * margin);
  p.unX = (px) => view.x[0] + ((px - margin) / (W() - 2 * margin)) * (view.x[1] - view.x[0]);
  p.unY = (py) => view.y[0] + ((H() - margin - py) / (H() - 2 * margin)) * (view.y[1] - view.y[0]);
  p.range = () => ({ x: [...view.x], y: [...view.y] });
  const stroke = (color, width = 2, dash = null) => {
    ctx.strokeStyle = color || COLORS.glow;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.shadowColor = 'rgba(76, 156, 245, 0.55)';
    ctx.shadowBlur = 6;
  };
  p.axes = (o = {}) => {
    const step = (span) => {
      const raw = span / 8;
      const mag = 10 ** Math.floor(Math.log10(raw));
      return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    };
    ctx.save();
    ctx.shadowBlur = 0;
    ctx.font = "11px 'Segoe UI', system-ui, sans-serif";
    ctx.fillStyle = COLORS.muted;
    for (const [axis, lo, hi] of [['x', ...view.x], ['y', ...view.y]]) {
      const s = o.step || step(hi - lo);
      for (let v = Math.ceil(lo / s) * s; v <= hi + 1e-9; v += s) {
        const at = Math.abs(v) < 1e-9 ? 0 : v;
        ctx.strokeStyle = 'rgba(76, 156, 245, 0.13)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (axis === 'x') {
          ctx.moveTo(p.X(at), p.Y(view.y[0]));
          ctx.lineTo(p.X(at), p.Y(view.y[1]));
        } else {
          ctx.moveTo(p.X(view.x[0]), p.Y(at));
          ctx.lineTo(p.X(view.x[1]), p.Y(at));
        }
        if (o.grid !== false) ctx.stroke();
        if (at !== 0 && o.numbers !== false) {
          const text = String(Number(at.toPrecision(6)));
          if (axis === 'x') ctx.fillText(text, p.X(at) + 2, Math.min(H() - 4, Math.max(12, p.Y(0) + 12)));
          else ctx.fillText(text, Math.min(W() - 24, Math.max(2, p.X(0) + 4)), p.Y(at) - 2);
        }
      }
    }
    ctx.strokeStyle = 'rgba(124, 192, 255, 0.6)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(p.X(view.x[0]), p.Y(0));
    ctx.lineTo(p.X(view.x[1]), p.Y(0));
    ctx.moveTo(p.X(0), p.Y(view.y[0]));
    ctx.lineTo(p.X(0), p.Y(view.y[1]));
    ctx.stroke();
    if (o.labels) {
      ctx.fillStyle = COLORS.text;
      ctx.fillText(String(o.labels[0] || ''), W() - margin - 30, p.Y(0) - 6);
      ctx.fillText(String(o.labels[1] || ''), p.X(0) + 6, margin);
    }
    ctx.restore();
  };
  p.curve = (f, o = {}) => {
    ctx.save();
    stroke(o.color, o.width || 2.2, o.dash);
    ctx.beginPath();
    let pen = false;
    const n = Math.max(50, Math.round(W()));
    for (let i = 0; i <= n; i++) {
      const x = view.x[0] + ((view.x[1] - view.x[0]) * i) / n;
      const y = f(x);
      if (!Number.isFinite(y) || Math.abs(p.Y(y)) > 1e5) {
        pen = false;
        continue;
      }
      if (pen) ctx.lineTo(p.X(x), p.Y(y));
      else ctx.moveTo(p.X(x), p.Y(y));
      pen = true;
    }
    ctx.stroke();
    ctx.restore();
  };
  // A path through points [[x, y], ...] (a parametric curve, a trajectory).
  p.path = (points, o = {}) => {
    ctx.save();
    stroke(o.color, o.width || 2, o.dash);
    ctx.beginPath();
    points.forEach(([x, y], k) => (k ? ctx.lineTo(p.X(x), p.Y(y)) : ctx.moveTo(p.X(x), p.Y(y))));
    if (o.close) ctx.closePath();
    if (o.fill) {
      ctx.fillStyle = o.fill;
      ctx.fill();
    }
    ctx.stroke();
    ctx.restore();
  };
  p.segment = (x0, y0, x1, y1, o = {}) => p.path([[x0, y0], [x1, y1]], o);
  p.point = (x, y, o = {}) => {
    ctx.save();
    ctx.fillStyle = o.color || COLORS.bright;
    ctx.shadowColor = 'rgba(124, 192, 255, 0.9)';
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.arc(p.X(x), p.Y(y), o.r || 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    if (o.label) p.text(x, y, o.label, { dx: 8, dy: -8, color: o.color });
  };
  p.vector = (x0, y0, x1, y1, o = {}) => {
    const [ax, ay, bx, by] = [p.X(x0), p.Y(y0), p.X(x1), p.Y(y1)];
    const angle = Math.atan2(by - ay, bx - ax);
    const head = o.head || 10;
    ctx.save();
    stroke(o.color, o.width || 2.4);
    ctx.fillStyle = o.color || COLORS.glow;
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx - Math.cos(angle) * head * 0.6, by - Math.sin(angle) * head * 0.6);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(bx, by);
    ctx.lineTo(bx - head * Math.cos(angle - 0.4), by - head * Math.sin(angle - 0.4));
    ctx.lineTo(bx - head * Math.cos(angle + 0.4), by - head * Math.sin(angle + 0.4));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
    if (o.label) p.text(x1, y1, o.label, { dx: 6, dy: -6, color: o.color });
  };
  // A filled Tabler icon centred on (x, y): {size: pixels (20), color}.
  p.icon = (x, y, name, o = {}) => {
    const paths = iconPaths(name);
    const size = Number(o.size) > 0 ? Number(o.size) : 20;
    ctx.save();
    ctx.translate(p.X(x) - size / 2 + (o.dx || 0), p.Y(y) - size / 2 + (o.dy || 0));
    ctx.scale(size / 24, size / 24);
    ctx.fillStyle = o.color || COLORS.glow;
    ctx.shadowColor = 'rgba(76, 156, 245, 0.6)';
    ctx.shadowBlur = 6;
    for (const d of paths) ctx.fill(new Path2D(d));
    ctx.restore();
  };
  p.text = (x, y, words, o = {}) => {
    ctx.save();
    ctx.font = `${o.size || 13}px 'Segoe UI', system-ui, sans-serif`;
    ctx.fillStyle = o.color || COLORS.text;
    ctx.textAlign = o.align || 'left';
    ctx.shadowColor = 'rgba(5, 8, 17, 0.9)';
    ctx.shadowBlur = 4;
    ctx.fillText(String(words), p.X(x) + (o.dx || 0), p.Y(y) + (o.dy || 0));
    ctx.restore();
  };
  // A point they can drag: {x, y, color, label}; onMove(handle) after each move. Drawn on top of everything.
  p.handle = (spec, onMove) => {
    const handle = { x: Number(spec.x) || 0, y: Number(spec.y) || 0, color: spec.color || COLORS.ice, label: spec.label || '', onMove };
    handles.push(handle);
    p.redraw();
    return handle;
  };
  // p.draw(fn) sets what it draws and draws it; p.draw() with nothing draws it again (each frame of a loop).
  p.draw = (fn) => {
    if (typeof fn === 'function') drawer = fn;
    p.redraw();
  };
  p.redraw = () => {
    sync();
    ctx.clearRect(0, 0, W(), H());
    if (drawer) drawer(p);
    else p.axes();
    for (const hd of handles) {
      p.point(hd.x, hd.y, { color: hd.color, r: 6.5, label: hd.label });
      ctx.save();
      ctx.strokeStyle = 'rgba(226, 240, 255, 0.7)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(p.X(hd.x), p.Y(hd.y), 10, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
  };
  let held = null;
  const el = ctx.canvas;
  el.addEventListener('pointerdown', (e) => {
    const near = handles
      .map((hd) => [hd, Math.hypot(p.X(hd.x) - e.offsetX, p.Y(hd.y) - e.offsetY)])
      .filter(([, d]) => d < 16)
      .sort((a, b) => a[1] - b[1])[0];
    if (!near) return;
    held = near[0];
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', (e) => {
    el.style.cursor = held || handles.some((hd) => Math.hypot(p.X(hd.x) - e.offsetX, p.Y(hd.y) - e.offsetY) < 16) ? 'grab' : '';
    if (!held) return;
    held.x = Math.min(view.x[1], Math.max(view.x[0], p.unX(e.offsetX)));
    held.y = Math.min(view.y[1], Math.max(view.y[0], p.unY(e.offsetY)));
    try {
      if (held.onMove) held.onMove(held);
      p.redraw();
    } catch (err) {
      fail(err);
    }
  });
  const letGo = () => {
    held = null;
  };
  el.addEventListener('pointerup', letGo);
  el.addEventListener('pointercancel', letGo);
  resizeListeners.push(() => p.redraw());
  paramListeners.push(() => p.redraw());
  return p;
}

// ---------------------------------------------------------------- what the program sees

let stateTimer = 0;
let stateWaiting = null;
let reported; // what the program last reported (undefined: nothing yet)
function report(obj) {
  try {
    JSON.stringify(obj);
  } catch {
    throw new TypeError('oct.report takes plain values (numbers, words, lists)');
  }
  reported = obj;
  sendState();
}

// The program's report, with the icon names it asked for that don't exist (drawn as dots) beside it.
function sendState() {
  let body = reported;
  if (missingIcons.size) {
    const missing = [...missingIcons].slice(0, 10).join(', ');
    body = reported && typeof reported === 'object' && !Array.isArray(reported) ? { ...reported, missing_icons: missing } : { report: reported, missing_icons: missing };
  }
  stateWaiting = (JSON.stringify(body) || '').slice(0, 1500);
  if (stateTimer) return;
  stateTimer = setTimeout(() => {
    stateTimer = 0;
    post({ type: 'state', state: stateWaiting });
  }, STATE_GAP_MS);
}

const oct = {
  THREE,
  OrbitControls,
  colors: COLORS,
  root,
  params: values,
  fps: MAX_FPS,
  get width() {
    return root.clientWidth;
  },
  get height() {
    return root.clientHeight;
  },
  three,
  // A knowledge map or any node graph, laid out and labelled for reading (renderer/live/graph.js).
  graph: (spec) =>
    makeGraph({ THREE, COLORS, root, three, label, report, touched, onResize: (fn) => resizeListeners.push(fn) }, spec),
  label,
  icon,
  local,
  arrow,
  axes,
  canvas: () => canvas2d(false),
  plane,
  loop,
  report,
  onParams(fn) {
    if (typeof fn === 'function') paramListeners.push(fn);
  },
  onResize(fn) {
    if (typeof fn === 'function') resizeListeners.push(fn);
  },
  // Moves one of the block's sliders from the program (the user dragged the thing it stands for).
  set(id, value) {
    if (!(id in values)) throw new RangeError(`oct.set: no slider ${id} (sliders: ${Object.keys(values).join(', ') || 'none'})`);
    values[id] = value;
    post({ type: 'set', id: String(id), value });
    notify(String(id));
  },
};
Object.freeze(oct);

function notify(changed) {
  for (const fn of paramListeners) {
    try {
      fn(values, changed);
    } catch (err) {
      fail(err);
    }
  }
  for (const r of renderers) r.render();
}

let resizeQueued = false;
new ResizeObserver(() => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => {
    resizeQueued = false;
    for (const fn of resizeListeners) {
      try {
        fn();
      } catch (err) {
        fail(err);
      }
    }
  });
}).observe(root);

const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;

function run(code, params) {
  if (ran) return;
  ran = true;
  Object.assign(values, params && typeof params === 'object' ? params : {});
  let program;
  try {
    program = new AsyncFunction('oct', 'THREE', String(code));
  } catch (err) {
    fail(err);
    return;
  }
  Promise.resolve()
    .then(() => program(oct, THREE))
    .catch(fail);
  setTimeout(() => {
    if (!failed) post({ type: 'ok' });
  }, 600);
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent) return;
  const msg = event.data;
  if (!msg || typeof msg !== 'object' || msg.live !== 1) return;
  if (msg.type === 'run') run(msg.code, msg.params);
  else if (msg.type === 'ping') post({ type: 'pong', n: msg.n });
  else if (msg.type === 'params' && msg.values && typeof msg.values === 'object') {
    let changed = null;
    for (const [k, v] of Object.entries(msg.values)) {
      if (k in values && values[k] !== v) changed = changed === null ? k : '*';
      values[k] = v;
    }
    touched();
    if (changed !== null) notify(changed);
  }
});

post({ type: 'hello' });
