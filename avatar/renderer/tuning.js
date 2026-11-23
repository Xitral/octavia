// Debug, for tools/drive.mjs: fits an arm pose (poses.js's ARM angles) that puts a held prop where it's wanted,
// so a new pose starts from numbers solved on the real model instead of guesses tuned by eye.
//
// solveArm() runs forward kinematics on the normalized arm bones with the prop in that hand (the prop rack's
// mount, which follows the hand bone), scoring each try against goals, and searches the arm's eight angles
// (upper arm x/y/z, forearm twist and bend, wrist x/y/z) with Nelder-Mead from a starting pose. It restores the
// bones before it returns: the next frame is the animator's as usual.
//
// Goals, each with an optional `weight`:
//   { point: 'face', at: { bone: 'head', offset: [x, y, z] } }  the middle of the prop's part `point` (or of the
//       whole prop, with no point) at a bone's position (or 'eyes': the middle of her eyes) plus an offset in
//       metres (world axes: +X her left, +Y up, +Z toward the viewer); `at: { world: [x, y, z] }` is a spot in the world
//   { point: 'face', axis: [0, 0, 1], toward: 'camera' | [x, y, z] }  that part's local axis `axis` turned toward
//       the viewer, or along a world direction
//   { joint: 'LowerArm' | 'Hand', at: {...} }  the elbow or wrist at a spot; with `axis` and `toward`, an axis of
//       that bone turned that way, in the shared hand frame (props.js: for the left hand +X runs along the fingers,
//       -Y is the palm side)
// `fixed` skips the search and just reports on `start`; `measure` names prop parts (or her bones) whose world positions
// to report.
// Every solve also keeps the elbow, the wrist and the prop out of her torso, and the wrist within what a wrist
// can do.

import * as THREE from 'three';
import { eyesWorld } from './anatomy.js';
import { eulerOrder } from './poses.js';

const PARTS = ['UpperArm', 'LowerArm', 'Hand'];
// Soft limits per angle (left-side conventions, poses.js): past them the score climbs steeply.
const LIMITS = [
  [-2.2, 1.7], [-2.3, 0.9], [-1.5, 0.35], // upper arm
  [-2.3, 1.2], [-2.7, -0.05], // forearm twist, elbow bend
  [-0.45, 0.45], [-0.55, 0.55], [-1.2, 1.2], // wrist twist (a wrist hardly twists), toward the thumb, toward the palm
];
const TORSO_RADIUS = 0.11; // m round the line from her spine to her neck that an arm stays out of

const _v = new THREE.Vector3();
const _w = new THREE.Vector3();
const _e = new THREE.Euler();

const toParams = (pose) => [...pose.UpperArm, pose.LowerArm[0], pose.LowerArm[1], ...pose.Hand];
const toPose = (p) => ({ UpperArm: p.slice(0, 3), LowerArm: [p[3], p[4], 0], Hand: p.slice(5, 8) });
const round = (p) => p.map((v) => Math.round(v * 100) / 100);

// Nelder-Mead over `f` from `x0`, with a first step of `step` per coordinate.
function minimize(f, x0, { step = 0.25, iterations = 1500 } = {}) {
  const n = x0.length;
  let simplex = [x0.slice()];
  for (let i = 0; i < n; i++) {
    const x = x0.slice();
    x[i] += step;
    simplex.push(x);
  }
  let values = simplex.map(f);
  const order = () => {
    const idx = values.map((_, i) => i).sort((a, b) => values[a] - values[b]);
    simplex = idx.map((i) => simplex[i]);
    values = idx.map((i) => values[i]);
  };
  const along = (c, x, t) => c.map((ci, i) => ci + t * (x[i] - ci));
  for (let k = 0; k < iterations; k++) {
    order();
    if (values[n] - values[0] < 1e-9) break;
    const centroid = new Array(n).fill(0);
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) centroid[j] += simplex[i][j] / n;
    const worst = simplex[n];
    const r = along(centroid, worst, -1);
    const fr = f(r);
    if (fr < values[0]) {
      const e = along(centroid, worst, -2);
      const fe = f(e);
      [simplex[n], values[n]] = fe < fr ? [e, fe] : [r, fr];
    } else if (fr < values[n - 1]) {
      [simplex[n], values[n]] = [r, fr];
    } else {
      const c = along(centroid, worst, fr < values[n] ? -0.5 : 0.5);
      const fc = f(c);
      if (fc < Math.min(fr, values[n])) [simplex[n], values[n]] = [c, fc];
      else {
        for (let i = 1; i <= n; i++) {
          simplex[i] = along(simplex[0], simplex[i], 0.5);
          values[i] = f(simplex[i]);
        }
      }
    }
  }
  order();
  return { x: simplex[0], value: values[0] };
}

// Distance from p to the segment a-b.
function segmentDistance(p, a, b) {
  const ab = _v.subVectors(b, a);
  const t = Math.max(0, Math.min(1, _w.subVectors(p, a).dot(ab) / Math.max(ab.lengthSq(), 1e-9)));
  return p.distanceTo(_w.copy(a).addScaledVector(ab, t));
}

// Solves `side`'s arm for `goals` with the rack's current prop in that hand, from `start` (left-side angles:
// { UpperArm, LowerArm, Hand }). Returns { pose, score, report } with the pose in poses.js terms (left side).
export function solveArm({ vrm, rack, camera, side, start, goals, restarts = 4, measure = [], fixed: keepStart = false }) {
  const humanoid = vrm.humanoid;
  const flip = vrm.meta?.metaVersion === '0';
  const bones = PARTS.map((part) => humanoid.getNormalizedBoneNode(`${side}${part}`));
  if (bones.some((b) => !b)) throw new Error(`no ${side} arm`);
  const saved = bones.map((b) => b.quaternion.clone());
  const spine = humanoid.getNormalizedBoneNode('spine');
  const neck = humanoid.getNormalizedBoneNode('neck');
  const mount = rack?.hand === side ? rack.object(rack.holding) : null;
  const savedScale = mount ? mount.scale.x : 1;
  if (mount) mount.scale.setScalar(1); // measure the prop at its full size, not part way through its pop

  // Where each goal's point is in its part's own frame, measured once.
  const parts = new Map();
  const partOf = (name) => {
    if (!mount) throw new Error('no prop in that hand');
    if (!parts.has(name ?? '')) {
      const node = name ? mount.getObjectByName(name) : mount;
      if (!node) throw new Error(`the prop has no part ${name}`);
      vrm.scene.updateMatrixWorld(true);
      const center = new THREE.Box3().setFromObject(node).getCenter(new THREE.Vector3());
      parts.set(name ?? '', { node, local: node.worldToLocal(center) });
    }
    return parts.get(name ?? '');
  };
  const bonePos = (name) => {
    if (name === 'eyes') return eyesWorld(vrm) ?? bonePos('head');
    const node = humanoid.getNormalizedBoneNode(name);
    if (!node) throw new Error(`no bone ${name}`);
    return node.getWorldPosition(new THREE.Vector3());
  };
  // Targets are fixed in the world: the body doesn't move while the arm is solved.
  vrm.scene.updateMatrixWorld(true);
  const goalsAt = goals.map((g) => ({
    ...g,
    weight: Number.isFinite(g.weight) ? g.weight : 1,
    target: g.at ? (g.at.world ? new THREE.Vector3(...g.at.world) : bonePos(g.at.bone)).add(new THREE.Vector3(...(g.at.offset || [0, 0, 0]))) : null,
    part: g.point !== undefined || !g.joint ? partOf(g.point) : null,
    // An arm part of this side (LowerArm: the elbow, Hand: the wrist), or any bone below the wrist by its full name
    // (rightIndexDistal: the last joint of her index finger, as her fingers are posed right now).
    bone: g.joint ? (PARTS.includes(g.joint) ? bones[PARTS.indexOf(g.joint)] : humanoid.getNormalizedBoneNode(g.joint)) : null,
  }));
  const torso = [spine.getWorldPosition(new THREE.Vector3()), neck.getWorldPosition(new THREE.Vector3())];

  const set = (p) => {
    const pose = toPose(p);
    PARTS.forEach((part, i) => {
      let [x, y, z] = pose[part];
      if (side === 'right') [y, z] = [-y, -z];
      const q = bones[i].quaternion.setFromEuler(_e.set(x, y, z, eulerOrder(part)));
      if (flip) q.set(-q.x, q.y, -q.z, q.w);
    });
    bones[0].updateWorldMatrix(false, true);
  };
  const where = (g) => (g.bone ? g.bone.getWorldPosition(new THREE.Vector3()) : g.part.node.localToWorld(g.part.local.clone()));
  const terms = (p) => {
    set(p);
    const out = [];
    for (const g of goalsAt) {
      const at = where(g);
      if (g.target) out.push(['at', g, g.weight * at.distanceTo(g.target) * 100]); // cm
      if (g.axis) {
        // A joint's axis is in the shared hand frame (props.js), which VRM 0.x bones have turned half round.
        const [x, y, z] = g.axis;
        const local = g.joint && flip ? new THREE.Vector3(-x, y, -z) : new THREE.Vector3(x, y, z);
        const dir = local.transformDirection((g.bone ?? g.part.node).matrixWorld);
        const want = g.toward === 'camera' ? _v.subVectors(camera.position, at).normalize().clone() : new THREE.Vector3(...g.toward).normalize();
        out.push(['axis', g, g.weight * (1 - dir.dot(want)) * 20]);
      }
    }
    // Out of her torso: the elbow, the wrist and the prop's middle.
    const keep = [bones[1].getWorldPosition(new THREE.Vector3()), bones[2].getWorldPosition(new THREE.Vector3())];
    if (mount) keep.push(partOf(undefined).node.localToWorld(partOf(undefined).local.clone()));
    let inside = 0;
    for (const k of keep) inside += Math.max(0, TORSO_RADIUS - segmentDistance(k, torso[0], torso[1])) * 100;
    out.push(['torso', null, 3 * inside * inside]);
    let over = 0;
    p.forEach((v, i) => {
      const [lo, hi] = LIMITS[i];
      over += Math.max(0, lo - v, v - hi);
    });
    out.push(['limits', null, 400 * over * over]);
    // A little pull toward the start, so of two fits the one nearer the hand-made pose wins.
    out.push(['start', null, 0.3 * p.reduce((s, v, i) => s + (v - x0[i]) ** 2, 0)]);
    return out;
  };
  const score = (p) => terms(p).reduce((s, [kind, g, v]) => s + (kind === 'at' ? v * v : v), 0);

  const x0 = toParams(start);
  let best = keepStart ? { x: x0, value: score(x0) } : minimize(score, x0);
  for (let i = 0; !keepStart && i < restarts; i++) {
    const jitter = best.x.map((v, k) => v + (((i * 7 + k * 3) % 5) - 2) * 0.12);
    const tried = minimize(score, jitter, { step: 0.15 });
    if (tried.value < best.value) best = tried;
  }
  // Where the prop's parts named in `measure` end up (world, cm), e.g. to aim the other hand at one.
  const measured = {};
  set(best.x);
  for (const name of measure) {
    const part = mount?.getObjectByName(name);
    const bone = part ? null : humanoid.getNormalizedBoneNode(name); // or a bone of hers, by its full name
    const at = part ? new THREE.Box3().setFromObject(part).getCenter(new THREE.Vector3()) : bone?.getWorldPosition(new THREE.Vector3());
    if (at) measured[name] = round(at.toArray().map((v) => v * 100));
  }
  const report = terms(best.x).map(([kind, g, v]) => `${kind}${g?.point ? `:${g.point}` : g?.joint ? `:${g.joint}` : ''} ${v.toFixed(2)}`);
  bones.forEach((b, i) => b.quaternion.copy(saved[i]));
  bones[0].updateWorldMatrix(false, true);
  if (mount) mount.scale.setScalar(savedScale);
  const pose = toPose(round(best.x));
  // Where her landmarks are (world, cm), for writing the next goals' offsets.
  const landmarks = {};
  for (const name of ['hips', 'chest', 'neck', 'head', 'eyes', 'leftUpperArm', 'rightUpperArm']) {
    const at = name === 'eyes' ? eyesWorld(vrm) : humanoid.getNormalizedBoneNode(name)?.getWorldPosition(new THREE.Vector3());
    if (at) landmarks[name] = round(at.toArray().map((v) => v * 100));
  }
  landmarks.camera = round(camera.position.toArray().map((v) => v * 100));
  return { pose, score: best.value, report, landmarks, measured };
}
