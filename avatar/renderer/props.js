// Props she holds: a magnifying glass while she studies your screen, a camera while she takes a
// screenshot, a clipboard for your tasks, a stopwatch for timers, a hand bell for reminders and more
// (builtins.js), any .glb/.gltf dropped into models/avatar/props, or one Octavia puts together from simple shapes
// while she runs (made props, below). Some she wears on her head instead: headphones, sunglasses.
//
// Usage (the renderer wires it; everything here is safe to call at any time):
//
//   const rack = new PropRack(vrm, cfg.props, host.loadProp, { onProblem: (name, reason) => ... });
//   rack.preload();                    // loads every configured prop; never rejects, failures are logged
//                                      // and handed to onProblem, once per prop
//   rack.compile(stage.renderer, stage.camera, stage.scene); // after preload: no shader hitch on first hold
//   rack.hold('magnifier');            // pops into her right hand (or the hand the prop's config names)
//   rack.hold('camera', 'left');       // another prop replaces the current one (it pops out)
//   rack.act('ring');                  // the held prop does something with its own parts, if it can
//   rack.release();                    // pops out
//   rack.define('sword', { parts });   // a prop made of simple shapes, ready at once (or replaced, even in hand)
//   rack.remove('sword');              // and gone again (only made props)
//   rack.update(dt);                  // every frame (the pop animation; POP_IN / POP_OUT seconds)
//   rack.dispose();                    // frees geometry, materials and textures
//
// Holding a prop only places it in the hand: the hand's shape is the animator's job. `rack.grip` says
// which hand should close how far ({ hand, shape, weight }, or null): blend `hand(grip.hand, grip.shape)`
// from poses.js over the pose by `grip.weight`, which eases in as the prop pops in and out as it pops out.
// `rack.holding` is the held prop's name and `rack.hand` its hand (null when her hands are empty).
//
// Where a prop sits. Each hand has a hand frame: the axes of the VRM *normalized* hand bone, which are
// the same for every model (identity in the T-pose). For the LEFT hand +X runs along the fingers, -Y is
// the palm side (+Y the back of the hand) and +Z the thumb side (the thumb points forward in the T-pose).
// The right hand mirrors X, so the same numbers put a prop in the same place in either hand, mirrored,
// like poses.js mirrors limb angles. The frame's origin is the grip point, measured from the model's own
// finger bones: the middle of the hole a closed fist makes, where a handle would run through along Z.
//
// A prop's model follows glTF conventions: +Y is up, +Z its front. It's scaled so its longest side is
// `size` metres (default 0.18; a built-in keeps its real size unless given a different one) and its
// bounding-box centre sits on the grip point, turned so it's held like a handle:
// its up (+Y) comes out of the thumb side of her fist, its front (+Z) faces where her fingers point, and
// its +X faces the back of her left hand (the palm of her right). Then `grip` [x, y, z] (m, along the
// hand frame's axes) slides it in her hand, and `rotation` [x, y, z] (degrees, Euler XYZ about the hand
// frame's axes) turns it, grip offset included, about the grip point: set `grip` so the right part of the
// prop is in her fist, then tilt it with `rotation`. The built-ins are modelled the same way and bring
// their own grip and size, so zeros fit them too; their `grip`/`rotation` add to the tuned placement. One
// that isn't held like a handle (a stopwatch lies in her palm, face out) also brings its own turn, made
// before the config's.
//
// Worn props (headphones, sunglasses, or a file of the same name) go on her head rather than in a hand, in the
// head frame: the axes of the normalized head bone (+X her left, +Y up, +Z where she faces), its origin the
// middle of her eyes as they show (anatomy.js). The model sits upright facing forward, its bounding-box centre
// (a built-in's holdAt) between her eyes; `grip` and `rotation` move it in the head frame, and it's never
// mirrored. A worn prop leaves her hands empty: no grip.
//
// A built-in may move its own parts (its `rig`): a stopwatch's hand sweeps once she clicks it on, a bell's
// clapper swings as she rings it, a tick draws itself onto the clipboard. `act(name)` starts one of those on
// the held prop; a file prop has none, and just stays as it is.
//
// style 'toon' redraws lit materials as MeshToonMaterial (keeping map, colour, transparency and side)
// with a shared 3-step shading ramp, so props match her cel shading; 'original' keeps them. Unlit
// materials stay as they are: they're already flat on purpose.

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { eyesWorld } from './anatomy.js';
import { BUILTINS, WORN_BUILTINS, defaultHand } from './builtins.js';
import { easeOutBack, smoothstep } from './math.js';

export const DEFAULT_SIZE = 0.18; // m: a file prop's longest side when its config doesn't say
export const POP_IN = 0.15; // s: the scale-pop into her hand
export const POP_OUT = 0.12;
const LOAD_TIMEOUT_MS = 15000; // a prop that never arrives must not keep a hold() waiting forever
const TOON_STEPS = [0.5, 0.78, 1]; // shade, terminator, lit: fractions of the light, close to her MToon shading
// A hand closed round a handle, in poses.js hand() terms (radians of curl; thumb folds it in).
const HANDLE_GRIP = { curl: 0.95, spread: 0, thumb: 0.7 };

// Model axes -> hand frame for a prop held like a handle (left hand): +Y (up) -> +Z (thumb side),
// +Z (front) -> +X (along the fingers), +X -> +Y (back of the hand). A 120 degree turn about (1, 1, 1).
const HOLD = Object.freeze(new THREE.Quaternion(0.5, 0.5, 0.5, 0.5));

const FINGERS = ['Index', 'Middle', 'Ring', 'Little'];
const SIDES = ['left', 'right'];

// Mirrors a left-hand rotation for the right hand (across the body's midplane): same as poses.js.
const mirrorQuat = (q) => q.set(q.x, -q.y, -q.z, q.w);
// [x, y, z] degrees (Euler XYZ) as a quaternion.
const eulerDegrees = (angles) => {
  const [x, y, z] = angles.map(THREE.MathUtils.degToRad);
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z, 'XYZ'));
};

// ---- config ----

const vec3 = (v) => (Array.isArray(v) && v.length === 3 && v.every(Number.isFinite) ? v.slice() : [0, 0, 0]);

// One prop's config with every field checked: a bad value falls back to its default rather than
// breaking the avatar. `file` only says whether there is one: the renderer asks for the bytes by name.
// `parts` is only there for a prop made of simple shapes (see "made props" below).
export function normalizeDef(name, raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  let builtin = null;
  if (typeof d.builtin === 'string' && Object.hasOwn(BUILTINS, d.builtin)) builtin = d.builtin;
  else if (Object.hasOwn(BUILTINS, name)) builtin = name; // a broken replacement file falls back to it
  const parts = builtin === null ? normalizeParts(d.parts) : null;
  return {
    ...(parts ? { parts } : {}),
    name,
    file: typeof d.file === 'string' && d.file.length > 0,
    builtin,
    hand: d.hand === 'left' || d.hand === 'right' ? d.hand : defaultHand(builtin),
    worn: builtin !== null && WORN_BUILTINS.has(builtin), // on her head, not in a hand
    size: Number.isFinite(d.size) && d.size > 0 ? d.size : null,
    grip: vec3(d.grip),
    rotation: vec3(d.rotation),
    style: d.style === 'original' ? 'original' : 'toon',
  };
}

// ---- materials ----

// The shading ramp every toon prop shares: a few texels read with nearest filtering give hard steps.
export function toonGradient(steps = TOON_STEPS) {
  const data = Uint8Array.from(steps, (s) => Math.round(Math.min(1, Math.max(0, s)) * 255));
  const texture = new THREE.DataTexture(data, data.length, 1, THREE.RedFormat);
  texture.minFilter = THREE.NearestFilter;
  texture.magFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

// Everything MeshToonMaterial can draw that a lit glTF material may carry. Textures are shared, not copied.
const TOON_KEYS = [
  'name', 'map', 'alphaMap', 'transparent', 'opacity', 'alphaTest', 'alphaHash', 'side', 'vertexColors', 'emissiveMap',
  'emissiveIntensity', 'normalMap', 'normalMapType', 'bumpMap', 'bumpScale', 'aoMap', 'aoMapIntensity', 'lightMap',
  'lightMapIntensity', 'displacementMap', 'displacementScale', 'displacementBias', 'depthWrite', 'depthTest', 'blending',
  'premultipliedAlpha', 'flatShading', 'visible', 'toneMapped', 'polygonOffset', 'polygonOffsetFactor', 'polygonOffsetUnits',
  'alphaToCoverage', 'wireframe',
];

// A toon version of a lit material, or the material itself when it isn't one (unlit, already toon,
// custom shaders, or marked `userData.keepStyle` like the built-ins' glass and highlights).
export function toonMaterial(material, gradientMap) {
  const lit = material && (material.isMeshStandardMaterial || material.isMeshPhongMaterial || material.isMeshLambertMaterial);
  if (!lit || material.userData?.keepStyle) return material;
  const toon = new THREE.MeshToonMaterial({ gradientMap });
  for (const key of TOON_KEYS) if (material[key] !== undefined) toon[key] = material[key];
  toon.color.copy(material.color);
  toon.emissive.copy(material.emissive);
  if (material.normalScale) toon.normalScale.copy(material.normalScale);
  return toon;
}

// Swaps every lit material under `root` for its toon version (a material shared by several meshes
// stays shared) and frees the old ones. Returns how many were converted.
export function toonify(root, gradientMap) {
  const swapped = new Map();
  const swap = (m) => {
    if (!swapped.has(m)) swapped.set(m, toonMaterial(m, gradientMap));
    return swapped.get(m);
  };
  root.traverse((o) => {
    if (!o.isMesh || !o.material) return;
    o.material = Array.isArray(o.material) ? o.material.map(swap) : swap(o.material);
  });
  let count = 0;
  for (const [old, toon] of swapped) {
    if (old === toon) continue;
    old.dispose();
    count++;
  }
  return count;
}

// ---- built-ins (builtins.js) ----

export {
  BELL_RING_S,
  BUILTINS,
  CLIPBOARD_ROWS,
  FOLDER_FLIP_EVERY_S,
  GLOBE_TURN,
  HEADPHONE_NOTES,
  HEADPHONE_NOTE_S,
  STOPWATCH_LAP_S,
  STOPWATCH_RING_S,
  TICK_DRAW_S,
  UMBRELLA_OPEN_S,
  WORN_BUILTINS,
  WRENCH_TWIRL_S,
  defaultHand,
} from './builtins.js';

// ---- made props: simple shapes Octavia puts together at runtime (her make_avatar_prop tool) ----
//
// A made prop's `parts` is a list of { shape, size, position, rotation, color }, in metres and degrees, in the
// prop's own glTF axes (+Y up, +Z its front). Its origin is what goes in her fist (its hold point, like a built-in's
// holdAt), and it keeps its own size. `size` [x, y, z] is the part's width, height and depth: a cylinder, cone or
// capsule stands along Y; a torus is a ring in the XY plane round Z, `z` its tube's thickness; a plane lies in XY
// facing +Z (`z` unused) and shows from both sides. Octavia checks all of it; anything off is clamped or the part
// dropped here too.

export const SHAPES = Object.freeze(['box', 'sphere', 'cylinder', 'cone', 'torus', 'capsule', 'plane']);
export const MAX_PARTS = 24;
const PART_MIN = 0.002; // m: thinner than this doesn't show
const PART_MAX = 1; // m: a part's side, and how far from her fist it may sit
const SEGMENTS = 24;
const COLOR = /^#[0-9a-f]{6}$/i;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function normalizePart(raw) {
  if (!raw || typeof raw !== 'object' || !SHAPES.includes(raw.shape)) return null;
  const size = Array.isArray(raw.size) && raw.size.length === 3 && raw.size.every(Number.isFinite) ? raw.size : null;
  if (!size) return null;
  return {
    shape: raw.shape,
    size: size.map((s) => clamp(s, PART_MIN, PART_MAX)),
    position: vec3(raw.position).map((p) => clamp(p, -PART_MAX, PART_MAX)),
    rotation: vec3(raw.rotation),
    color: typeof raw.color === 'string' && COLOR.test(raw.color) ? raw.color.toLowerCase() : '#cccccc',
  };
}

// A made prop's parts, checked; null when there are none it can use.
export function normalizeParts(raw) {
  if (!Array.isArray(raw)) return null;
  const parts = raw.slice(0, MAX_PARTS).map(normalizePart).filter(Boolean);
  return parts.length > 0 ? parts : null;
}

function partGeometry({ shape, size: [x, y, z] }) {
  switch (shape) {
    case 'box':
      return new THREE.BoxGeometry(x, y, z);
    case 'sphere':
      return new THREE.SphereGeometry(0.5, SEGMENTS, SEGMENTS / 2).scale(x, y, z);
    case 'cylinder':
      return new THREE.CylinderGeometry(0.5, 0.5, 1, SEGMENTS).scale(x, y, z);
    case 'cone':
      return new THREE.ConeGeometry(0.5, 1, SEGMENTS).scale(x, y, z);
    case 'capsule': {
      // Its ends stay round: their radius from the smallest side, the straight part whatever height is left.
      const r = Math.min(x, y, z) / 2;
      return new THREE.CapsuleGeometry(r, y - 2 * r, 6, SEGMENTS).scale(x / (2 * r), 1, z / (2 * r));
    }
    case 'torus': {
      // Made 1 across, then stretched to x by y; the tube is never so fat the hole closes.
      const tube = Math.min(z / (2 * Math.max(x, y)), 0.25);
      return new THREE.TorusGeometry(0.5 - tube, tube, 12, SEGMENTS).scale(x, y, z / (2 * tube));
    }
    default:
      return new THREE.PlaneGeometry(1, 1).scale(x, y, 1);
  }
}

// The model of a made prop from its (normalized) parts, as a built-in hands it over: { model, holdAt, shape }.
export function buildParts(parts) {
  const model = new THREE.Group();
  model.name = 'parts';
  parts.forEach((part, i) => {
    const material = new THREE.MeshStandardMaterial({
      color: part.color,
      roughness: 0.6,
      side: part.shape === 'plane' ? THREE.DoubleSide : THREE.FrontSide,
    });
    const mesh = new THREE.Mesh(partGeometry(part), material);
    mesh.name = `${part.shape}${i}`;
    mesh.position.set(...part.position);
    mesh.quaternion.copy(eulerDegrees(part.rotation));
    model.add(mesh);
  });
  return { model, holdAt: new THREE.Vector3(), shape: { ...HANDLE_GRIP } };
}

// ---- the rack ----

// Where the grip point is in each hand frame, from the model's finger bones (see the header).
function measureGrip(humanoid, side, flip) {
  const hand = humanoid.getNormalizedBoneNode(`${side}Hand`);
  if (!hand) return null;
  hand.updateWorldMatrix(true, true);
  const toHand = hand.matrixWorld.clone().invert();
  const knuckles = new THREE.Vector3();
  const p = new THREE.Vector3();
  let found = 0;
  for (const finger of FINGERS) {
    const node = humanoid.getNormalizedBoneNode(`${side}${finger}Proximal`);
    if (!node) continue;
    knuckles.add(node.getWorldPosition(p).applyMatrix4(toHand));
    found++;
  }
  const out = side === 'left' ? 1 : -1; // which way the fingers run in this hand's frame
  if (found > 0) {
    knuckles.divideScalar(found);
    // VRM 0.x rigs are turned half round (see the frame in attach()); measure in the shared hand frame.
    if (flip) knuckles.set(-knuckles.x, knuckles.y, -knuckles.z);
  } else {
    // No finger bones: knuckles about a third of a forearm past the wrist.
    const lower = humanoid.getNormalizedBoneNode(`${side}LowerArm`);
    const forearm = lower ? lower.getWorldPosition(p).distanceTo(hand.getWorldPosition(new THREE.Vector3())) : 0.25;
    knuckles.set(out * 0.33 * forearm, 0, 0);
  }
  // The fist's hole sits under the knuckles, a finger's thickness plus a handle's radius toward the palm.
  const reach = knuckles.length() || 0.075;
  return new THREE.Vector3(knuckles.x, knuckles.y - 0.38 * reach, knuckles.z);
}

// Where a worn prop goes, in the head frame (see the header): the middle of her eyes as they show, or, on a model
// without eye bones, about where eyes would be: a little up and forward of the head bone.
function measureEyes(vrm, flip) {
  const head = vrm?.humanoid?.getNormalizedBoneNode('head');
  if (!head) return null;
  const eyes = eyesWorld(vrm);
  if (!eyes) return new THREE.Vector3(0, 0.06, 0.08);
  head.updateWorldMatrix(true, false);
  const at = head.worldToLocal(eyes);
  if (flip) at.set(-at.x, at.y, -at.z); // VRM 0.x: into the shared frame, like the hands
  return at;
}

// Lights and cameras inside a prop file would light her or do nothing useful: drop them.
function stripExtras(root) {
  const extras = [];
  root.traverse((o) => {
    if (o.isLight || o.isCamera) extras.push(o);
  });
  for (const o of extras) o.removeFromParent();
}

// Props are part of her: never culled by stale bounds (skinned props keep bind-pose ones), and no shadows.
function prepare(root) {
  root.traverse((o) => {
    o.frustumCulled = false;
    o.castShadow = false;
    o.receiveShadow = false;
  });
}

function disposeTree(root, keep) {
  const textures = new Set();
  root.traverse((o) => {
    o.geometry?.dispose();
    const materials = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
    for (const m of materials) {
      for (const v of Object.values(m)) if (v && v.isTexture && v !== keep) textures.add(v);
      m.dispose();
    }
  });
  for (const t of textures) t.dispose();
}

const withTimeout = (promise, ms, what) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${what} took longer than ${ms / 1000} s`)), ms);
    promise.then(
      (v) => (clearTimeout(timer), resolve(v)),
      (e) => (clearTimeout(timer), reject(e)),
    );
  });

// Why a file didn't load, in a few words the user can act on. Compressed GLBs (a common export option) need
// decoders the page isn't allowed to run, so name the kind and what to do about it. A reason main.js refused the
// file with (it's too big) arrives wrapped by Electron as "Error invoking remote method 'avatar:load-prop': Error:
// <reason>": only the reason is for the user.
function explainLoadError(err) {
  const message = String(err?.message || err)
    .replace(/^Error invoking remote method '[^']*': (?:\w*Error: )?/, '')
    .replace(/^THREE\.GLTFLoader: /, '');
  const packed = /draco/i.test(message) ? 'Draco' : /meshopt/i.test(message) ? 'meshopt' : /basisu|ktx2/i.test(message) ? 'KTX2 textures' : null;
  if (packed) return `it's compressed (${packed}); export it again without compression`;
  return message;
}

export class PropRack {
  // `vrm`: the loaded VRM. `defs`: cfg.props, { name: { file, builtin, hand, size, grip, rotation, style } }
  // (built-ins not listed are added with defaults). `loadBytes(name)`: resolves to the file's bytes
  // (Uint8Array or ArrayBuffer) or null, or rejects saying why, i.e. host.loadProp. `onProblem(name, reason)`:
  // called once for a prop that can't be held as configured (it's left out, or its file is and the built-in
  // stands in), with a short reason in plain words, so the app can tell the user why rather than only log it.
  constructor(vrm, defs = {}, loadBytes = null, { onProblem = null } = {}) {
    this.vrm = vrm;
    this.loadBytes = typeof loadBytes === 'function' ? loadBytes : null;
    this.onProblem = typeof onProblem === 'function' ? onProblem : null;
    this.reported = new Set(); // props whose problem onProblem has heard
    this.defs = new Map();
    const given = defs && typeof defs === 'object' ? defs : {};
    for (const [name, raw] of Object.entries(given)) this.defs.set(name, normalizeDef(name, raw));
    for (const name of Object.keys(BUILTINS)) if (!this.defs.has(name)) this.defs.set(name, normalizeDef(name, { builtin: name }));

    this.gradient = toonGradient();
    this.props = new Map(); // name -> loaded prop
    this.pending = new Map(); // name -> promise, while loading
    this.loading = null;
    this.current = null; // the prop in her hand, or popping in
    this.leaving = new Set(); // props popping out
    this.wanted = null; // { name, side }: asked for before it finished loading
    this.disposed = false;

    // VRM 0.x normalized bones (after VRMUtils.rotateVRM0) have their X and Z axes flipped: a frame
    // turned half round about Y undoes that, so the hand frame is the same for every model.
    this.flip = vrm?.meta?.metaVersion === '0';
    this.frames = { left: null, right: null, head: null };
    this.gripPoints = { left: null, right: null, head: null };
    const humanoid = vrm?.humanoid;
    if (humanoid) for (const side of SIDES) this.gripPoints[side] = measureGrip(humanoid, side, this.flip);
    this.gripPoints.head = measureEyes(vrm, this.flip);
  }

  // Names of the props that are loaded and ready to hold.
  get names() {
    return [...this.props.keys()];
  }

  has(name) {
    return this.props.has(name);
  }

  // The loaded prop's root object (in her hand while held), e.g. to find where its parts are; null if
  // it isn't loaded. Read it, don't move it: hold() and update() own its transform.
  object(name) {
    return this.props.get(name)?.mount ?? null;
  }

  // Loads every configured prop (files through loadBytes, built-ins from primitives). Resolves to
  // { loaded: [names], failed: [names] }; a prop that fails is logged and left out (a file that fails
  // falls back to the built-in of the same name, if any). Safe to call more than once.
  preload() {
    if (!this.loading) {
      const names = [...this.defs.keys()];
      this.loading = Promise.all(names.map((name) => this.loadOne(name))).then((ok) => ({
        loaded: names.filter((_, i) => ok[i]),
        failed: names.filter((_, i) => !ok[i]),
      }));
    }
    return this.loading;
  }

  loadOne(name) {
    const run = async () => {
      const def = this.defs.get(name);
      let model = null;
      let spec = null;
      if (def.file) {
        try {
          model = await this.loadFile(name);
        } catch (err) {
          const reason = explainLoadError(err);
          const fallback = def.builtin ? `; using the built-in ${def.builtin}` : '';
          console.warn(`prop ${name}: couldn't load its file: ${reason}${fallback}`);
          this.problem(name, fallback ? `${reason}${fallback} instead` : reason);
        }
      }
      if (!model && def.builtin) {
        spec = BUILTINS[def.builtin]();
        model = spec.model;
      }
      if (!model && def.parts) {
        spec = buildParts(def.parts);
        model = spec.model;
      }
      if (!model) {
        if (!def.file) {
          console.warn(`prop ${name}: no file and no built-in of that name`);
          this.problem(name, 'there is no file or built-in of that name');
        }
        return false;
      }
      if (this.disposed) {
        disposeTree(model);
        return false;
      }
      try {
        this.props.set(name, this.makeProp(def, model, spec));
      } catch (err) {
        console.warn(`prop ${name}: ${err.message}`);
        this.problem(name, err.message);
        disposeTree(model, this.gradient);
        return false;
      }
      const wanted = this.wanted;
      if (wanted && wanted.name === name) {
        this.wanted = null;
        this.hold(name, wanted.side);
      }
      return true;
    };
    const promise = run()
      .catch((err) => {
        console.warn(`prop ${name}: ${err.message}`); // preload() must never reject
        this.problem(name, err.message);
        return false;
      })
      .finally(() => this.pending.delete(name));
    this.pending.set(name, promise);
    return promise;
  }

  // Hands a prop's problem to onProblem, once per prop (the first reason is the one that matters). Not once
  // the rack is disposed: the page is going away, and nobody needs to hear about it.
  problem(name, reason) {
    if (!this.onProblem || this.disposed || this.reported.has(name)) return;
    this.reported.add(name);
    try {
      this.onProblem(name, reason);
    } catch (err) {
      console.warn(`prop ${name}: couldn't report its problem: ${err.message}`); // loading goes on regardless
    }
  }

  async loadFile(name) {
    if (!this.loadBytes) throw new Error('no way to read prop files here');
    const bytes = await withTimeout(Promise.resolve(this.loadBytes(name)), LOAD_TIMEOUT_MS, 'reading it');
    if (!bytes || !bytes.byteLength) throw new Error('the file could not be read');
    const buffer = bytes instanceof ArrayBuffer ? bytes : bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    // No base path: a prop must carry everything inside it (a .glb, or a .gltf with embedded data).
    const gltf = await withTimeout(new GLTFLoader().parseAsync(buffer, ''), LOAD_TIMEOUT_MS, 'decoding it');
    const root = gltf.scene || gltf.scenes?.[0];
    if (!root) throw new Error('it has no scene');
    stripExtras(root);
    return root;
  }

  // Wraps a model in the transforms that put it in a hand, or on her head (see the header):
  //   frame (per hand, or the head) -> mount: grip point, rotation, pop scale -> inner: grip offset, handle turn
  //   -> content: size scale, centred -> model
  makeProp(def, model, spec) {
    prepare(model);
    if (def.style === 'toon') toonify(model, this.gradient);
    const box = new THREE.Box3().setFromObject(model, true);
    if (box.isEmpty()) throw new Error('it has nothing to show');
    const extent = box.getSize(new THREE.Vector3());
    const longest = Math.max(extent.x, extent.y, extent.z);
    if (!(longest > 1e-6)) throw new Error('it has no size');
    // A built-in keeps its own true-to-life size unless the config asks for another one. The config
    // can't tell "not set" from the file-prop default, so the default counts as not set for built-ins.
    const size = def.size !== null && !(spec && def.size === DEFAULT_SIZE) ? def.size : spec ? longest : DEFAULT_SIZE;
    const scale = size / longest;
    const center = box.getCenter(new THREE.Vector3());

    const content = new THREE.Group();
    content.name = 'content';
    content.add(model);
    content.scale.setScalar(scale);
    content.position.copy(center).multiplyScalar(-scale);
    const inner = new THREE.Group();
    inner.name = 'grip';
    inner.add(content);
    const mount = new THREE.Group();
    mount.name = `prop:${def.name}`;
    mount.add(inner);
    mount.visible = false;

    // How the model sits in the hand frame: held like a handle, then turned the built-in's own way, if any. Worn,
    // it sits upright facing forward.
    const hold = def.worn ? new THREE.Quaternion() : HOLD.clone();
    if (spec?.turn) hold.premultiply(eulerDegrees(spec.turn));
    // A built-in's hold point goes on the grip point: turn its offset from the centre into the hand frame.
    const offset = new THREE.Vector3();
    if (spec) offset.copy(center).sub(spec.holdAt).multiplyScalar(scale).applyQuaternion(hold);
    offset.add(new THREE.Vector3(...def.grip));
    return {
      name: def.name,
      hand: def.hand,
      worn: def.worn,
      mount,
      inner,
      offset, // left-hand frame; mirrored for the right
      hold,
      turn: eulerDegrees(def.rotation),
      shape: { ...(spec ? spec.shape : HANDLE_GRIP) },
      rig: spec?.rig ?? null, // a built-in's moving parts
      side: null, // the hand it's attached to
      phase: 'idle', // idle | in | held | out
      t: 0,
      from: 0,
      scale: 0,
    };
  }

  // Puts together a made prop (`raw`: its config, with `parts`; see "made props" above) and has it ready to hold at
  // once, replacing an earlier one of that name; if she's holding that one, the new one takes its place in her hand.
  // Returns null when it's ready, else why not: a file or built-in prop of that name stays as it is.
  define(name, raw) {
    if (this.disposed) return 'the avatar is closing';
    if (typeof name !== 'string' || !name) return 'it has no name';
    const old = this.defs.get(name);
    if (old && !old.parts) return 'there is already a prop of that name';
    const def = normalizeDef(name, raw);
    if (!def.parts) return 'it has no shapes it can use';
    const spec = buildParts(def.parts);
    let prop;
    try {
      prop = this.makeProp(def, spec.model, spec);
    } catch (err) {
      disposeTree(spec.model, this.gradient);
      return err.message;
    }
    const held = this.current?.name === name ? this.current.side : null;
    this.drop(name);
    this.defs.set(name, def);
    this.props.set(name, prop);
    this.reported.delete(name);
    if (held) this.hold(name, held);
    return null;
  }

  // Throws a made prop away (out of her hand at once, if she holds it). False for any other prop: those come
  // from the config.
  remove(name) {
    if (this.disposed || !this.defs.get(name)?.parts) return false;
    this.drop(name);
    this.defs.delete(name);
    return true;
  }

  // Frees prop `name`'s model, gone from her hand without a pop.
  drop(name) {
    const prop = this.props.get(name);
    if (this.wanted?.name === name) this.wanted = null;
    if (!prop) return;
    if (this.current === prop) this.current = null;
    this.detach(prop);
    disposeTree(prop.mount, this.gradient);
    this.props.delete(name);
  }

  // Warms up the props' shaders against the real scene's lights, so the first hold doesn't stutter. `names`: only
  // those props (one just made), else all of them.
  compile(renderer, camera, scene, names = null) {
    for (const prop of this.props.values()) {
      if (names && !names.includes(prop.name)) continue;
      try {
        renderer.compile(prop.mount, camera, scene);
      } catch (err) {
        console.warn(`prop ${prop.name}: shader warm-up failed: ${err.message}`);
      }
    }
  }

  // Puts the named prop in a hand ('left' | 'right', default: the prop's configured hand) with a quick
  // pop, or on her head if it's worn (whatever hand is asked for); whatever she held before pops out. Returns
  // false (and does nothing) for unknown props. A prop that is still loading is shown as soon as it's ready,
  // unless something else was asked for meanwhile.
  hold(name, hand) {
    if (this.disposed) return false;
    const def = this.defs.get(name);
    if (!def) return false;
    const side = def.worn ? 'head' : hand === 'left' || hand === 'right' ? hand : def.hand;
    if (!this.gripPoints[side]) return false;
    if (!this.loading) this.preload(); // nobody preloaded: start now (built-ins are ready at once)
    const prop = this.props.get(name);
    if (!prop) {
      if (!this.pending.has(name)) return false; // it failed to load
      this.letGo();
      this.wanted = { name, side };
      return true;
    }
    this.wanted = null;
    if (prop === this.current && prop.side === side) return true;
    this.letGo();
    // Picked up again while it was vanishing: grow back from where it is instead of blinking.
    const resume = this.leaving.has(prop) && prop.side === side;
    this.leaving.delete(prop);
    if (!resume) prop.rig?.reset(); // a fresh one: the stopwatch at twelve, a clean list on the clipboard
    this.attach(prop, side, resume ? prop.scale : 0);
    prop.phase = 'in';
    prop.t = 0;
    prop.from = prop.scale;
    this.current = prop;
    return true;
  }

  // Pops out whatever she holds.
  release() {
    this.wanted = null;
    this.letGo();
  }

  // The held prop does `action` with its own parts ('tick', 'start', 'ring'...; see the built-ins). False when
  // she holds nothing (or it's still loading), or it has no such action: a file prop has none.
  act(action) {
    const rig = this.current?.rig;
    return !!rig && rig.act(action) === true;
  }

  letGo() {
    const prop = this.current;
    if (!prop) return;
    this.current = null;
    prop.phase = 'out';
    prop.t = 0;
    prop.from = prop.scale;
    this.leaving.add(prop);
  }

  // The held prop's name (or the one about to be, while it loads); null when her hands are empty.
  get holding() {
    return this.current?.name ?? this.wanted?.name ?? null;
  }

  // Which hand holds it: 'left' | 'right', 'head' for a worn one, or null.
  get hand() {
    return this.current?.side ?? this.wanted?.side ?? null;
  }

  // Hand-shape hint for a prop (default: the held one) in poses.js hand() terms: { curl, spread, thumb }.
  gripShape(name = this.current?.name) {
    const prop = name != null ? this.props.get(name) : null;
    return prop ? { ...prop.shape } : null;
  }

  // What the holding hand should do right now: { hand, shape, weight } (weight 0..1 follows the pop,
  // including the pop-out after a release), or null. A worn prop leaves her hands free.
  get grip() {
    const prop = [this.current, ...[...this.leaving].reverse()].find((p) => p && p.side !== 'head');
    if (!prop) return null;
    const weight = prop.phase === 'in' ? smoothstep(prop.t / POP_IN) : prop.phase === 'held' ? 1 : 1 - smoothstep(prop.t / POP_OUT);
    return { hand: prop.side, shape: { ...prop.shape }, weight };
  }

  update(dt) {
    if (this.disposed || !(dt > 0)) return;
    const prop = this.current;
    if (prop && prop.phase === 'in') {
      prop.t += dt;
      const p = prop.t / POP_IN;
      if (p >= 1) {
        prop.phase = 'held';
        this.setScale(prop, 1);
      } else {
        this.setScale(prop, prop.from + (1 - prop.from) * easeOutBack(p, 2));
      }
    }
    for (const leaving of this.leaving) {
      leaving.t += dt;
      const p = leaving.t / POP_OUT;
      if (p >= 1) {
        this.detach(leaving);
        continue;
      }
      // A little swell before it vanishes, like a soap bubble.
      const s = p < 0.25 ? 1 + 0.1 * smoothstep(p / 0.25) : 1.1 * (1 - smoothstep((p - 0.25) / 0.75));
      this.setScale(leaving, leaving.from * s);
    }
    // Moving parts keep moving while it's shown, popping out included.
    if (prop && prop.phase !== 'idle') prop.rig?.update(dt);
    for (const leaving of this.leaving) leaving.rig?.update(dt);
  }

  setScale(prop, s) {
    prop.scale = s;
    // A zero scale makes a singular matrix; hide it instead.
    prop.mount.visible = s > 1e-3;
    prop.mount.scale.setScalar(Math.max(s, 1e-3));
  }

  frame(side) {
    if (this.frames[side]) return this.frames[side];
    const bone = this.vrm.humanoid.getNormalizedBoneNode(side === 'head' ? 'head' : `${side}Hand`);
    const frame = new THREE.Object3D();
    frame.name = `props:${side}`;
    if (this.flip) frame.rotation.y = Math.PI;
    bone.add(frame);
    this.frames[side] = frame;
    return frame;
  }

  attach(prop, side, scale) {
    const { mount, inner } = prop;
    this.frame(side).add(mount);
    prop.side = side;
    mount.position.copy(this.gripPoints[side]);
    mount.quaternion.copy(prop.turn);
    inner.position.copy(prop.offset);
    inner.quaternion.copy(prop.hold);
    if (side === 'right') {
      mirrorQuat(mount.quaternion);
      inner.position.x = -inner.position.x;
      mirrorQuat(inner.quaternion);
    }
    this.setScale(prop, scale);
  }

  detach(prop) {
    this.leaving.delete(prop);
    prop.mount.removeFromParent();
    prop.side = null;
    prop.phase = 'idle';
    this.setScale(prop, 0);
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.current = null;
    this.wanted = null;
    this.leaving.clear();
    for (const prop of this.props.values()) {
      prop.mount.removeFromParent();
      disposeTree(prop.mount, this.gradient);
    }
    this.props.clear();
    for (const side of [...SIDES, 'head']) {
      this.frames[side]?.removeFromParent();
      this.frames[side] = null;
    }
    this.gradient.dispose();
  }
}
