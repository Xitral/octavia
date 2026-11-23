// Pointing at a spot on the screen with the index finger.
//
// The spot is a window pixel, and the camera ray through it is what she aims at. Every frame, after the
// animator has written its pose, the pointing arm is solved on the normalized bones so that the line from
// her shoulder through her index fingertip, and the finger itself, both run through a point on that ray a
// little in front of her body. On screen both lines then pass through the spot, whatever the perspective.
// Solving from the current chest transform every frame keeps the finger locked on target while she
// breathes, sways and hovers. The solution is blended over the animator's arm by a weight that eases in
// and out.
//
// Before the pose is written, pointing also turns her torso a little toward the spot, puts the other hand
// on her hip (a gesture that needs that hand still takes it) and turns her gaze to the spot, glancing back
// at the viewer now and then while she talks.
//
// A press (the assistant clicking the spot) jabs the arm forward, toward the glass, and back: the aim point
// slides out along the spot's ray, so on screen the finger stays on the spot's line and, as ever, short of it.
//
// Local axes of the normalized bones (VRM 1.0; poses.js has the Euler conventions) are measured from the
// model where they matter (the arm's own axis, the elbow's bend direction), so nothing assumes a T-pose
// arm lies exactly along X.

import * as THREE from 'three';
import { BOX_PAD, RING_RADIUS } from './marker.js';
import { ARM, HAND, eulerOrder, hand, limb, merge, pointingHand } from './poses.js';
import { clamp, damp, rand, smoothstep } from './math.js';

const RAISE = 0.35; // s to raise the arm
const LOWER = 0.4; // s to lower it
const RETARGET_TAU = 0.12; // s: a new spot while pointing sweeps the arm across
const DEPTH = 0.3; // m in front of her body where the aim point sits on the spot's ray: a slight reach
const MAX_DEPTH = 0.55; // m: reaching further forward keeps the fingertip off a spot that's close
export const PRESS_S = 0.32; // a press's jab, out and back
const PRESS_DEPTH = 0.14; // m further forward at its deepest
const CLEARANCE = 10; // px between the fingertip and the marker's edge
const GAZE_DEPTH = 0.9; // m: she looks at the spot's ray this far out, toward the screen, not sideways
const ELBOW_BEND = 0.16; // rad: straight but not locked
const MAX_BEND = 1.7; // rad: pointing at something very close bends the elbow up to this
const MAX_TWIST = 1.6; // rad of forearm twist, as far as the hand-made poses go
const MAX_WRIST = 0.7; // rad the wrist may bend to line the finger up with the spot (a close one needs most)
const SOLVE_PASSES = 3;
const TORSO_YAW = 0.09; // rad for each of spine and chest, toward the spot
const TORSO_LEAN = 0.035; // rad the chest leans toward the spot's side
const LOW_LEAN = 0.12; // rad more (and half that in the spine) for a spot far below her shoulder
// Where the forearm bends toward, so the elbow drops down and back like a relaxed reach.
const ELBOW_TOWARD = new THREE.Vector3(0, 1, 0.9).normalize();
const DOWN = new THREE.Vector3(0, -1, 0);
const FORWARD = new THREE.Vector3(0, 0, 1);
const PALM_INWARD = 0.6; // how much the palm turns in from facing down

const SIDES = ['left', 'right'];
const other = (side) => (side === 'left' ? 'right' : 'left');

const _euler = new THREE.Euler();
const _ray = new THREE.Ray();
const _m1 = new THREE.Matrix4();
const _m2 = new THREE.Matrix4();
const _q = new THREE.Quaternion();
const _qa = new THREE.Quaternion();
const _qb = new THREE.Quaternion();
const _U = new THREE.Quaternion();
const _Pinv = new THREE.Quaternion();
const _S = new THREE.Vector3();
const _A = new THREE.Vector3();
const _T = new THREE.Vector3();
const _K = new THREE.Vector3();
const _d = new THREE.Vector3();
const _v = new THREE.Vector3();
const _w = new THREE.Vector3();
const _x = new THREE.Vector3();
const _y = new THREE.Vector3();

function addAngles(add, bone, x, y, z) {
  const a = add[bone] || (add[bone] = [0, 0, 0]);
  a[0] += x;
  a[1] += y;
  a[2] += z;
}

// `v` minus its component along unit `axis`, normalized; `fallback` when that leaves nothing.
function orthogonal(v, axis, fallback, out) {
  out.copy(v).addScaledVector(axis, -v.dot(axis));
  if (out.lengthSq() < 1e-6) out.copy(fallback).addScaledVector(axis, -fallback.dot(axis));
  return out.normalize();
}

// Rotation taking the frame (a1, b1) to (a2, b2); each pair unit and perpendicular.
function frameRotation(a1, b1, a2, b2, out) {
  _m1.makeBasis(a1, b1, _v.crossVectors(a1, b1));
  _m2.makeBasis(a2, b2, _w.crossVectors(a2, b2));
  return out.setFromRotationMatrix(_m2.multiply(_m1.transpose()));
}

// Distance (px) from p to the line through a and b.
function lineDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy);
  if (len < 1e-6) return Math.hypot(p.x - a.x, p.y - a.y);
  return Math.abs((p.x - a.x) * dy - (p.y - a.y) * dx) / len;
}

// How far the fingertip reaches past the distal joint along `dir`, from the vertices skinned to the raw
// distal bone (finger bones end at the last joint, not at the tip). Null if the mesh doesn't say.
function fingertipReach(vrm, side, joint, dir) {
  const raw = vrm.humanoid.getRawBoneNode(`${side}IndexDistal`);
  if (!raw) return null;
  let best = 0;
  const v = new THREE.Vector3();
  vrm.scene.traverse((mesh) => {
    if (!mesh.isSkinnedMesh) return;
    const bone = mesh.skeleton.bones.indexOf(raw);
    if (bone < 0) return;
    const { skinIndex, skinWeight } = mesh.geometry.attributes;
    for (let i = 0; i < skinIndex.count; i++) {
      let weight = 0;
      for (let k = 0; k < 4; k++) if (skinIndex.getComponent(i, k) === bone) weight += skinWeight.getComponent(i, k);
      if (weight < 0.5) continue;
      mesh.getVertexPosition(i, v).applyMatrix4(mesh.matrixWorld);
      best = Math.max(best, v.sub(joint).dot(dir));
    }
  });
  return best > 0.002 ? best : null;
}

export class Pointing {
  // Must be built while the model is still in its rest pose (it measures the arms).
  constructor(vrm, stage, flipAxes) {
    this.vrm = vrm;
    this.stage = stage;
    this.flipAxes = flipAxes;
    this.torso = ['upperChest', 'chest', 'spine', 'hips'].map((n) => vrm.humanoid.getNormalizedBoneNode(n)).find(Boolean);

    this.on = false; // wants to point (the weight follows)
    this.side = null; // the pointing arm while pointing or lowering it
    this.target = null; // the spot, window px
    this.aim = { x: 0, y: 0 }; // where the arm aims now: follows the target, so a new spot sweeps it over
    this.box = null;
    this.ramp = 0;
    this.weight = 0;
    this.time = 0;
    this.glance = 0; // 1 = looking back at the viewer
    this.glanceWant = 0;
    this.nextGlance = 0;
    this.elevation = 0; // of the last aim above level, rad (the torso leans with it)
    this.pressT = Infinity; // s since the last press
    this.push = 0; // m the aim point is pushed out for a press, now

    vrm.scene.updateMatrixWorld(true);
    this.chains = Object.fromEntries(SIDES.map((side) => [side, this.chain(side)]));
    // The other hand on the hip, as authored pose quaternions for the animator's working pose.
    this.hipQuats = Object.fromEntries(
      SIDES.map((side) => {
        const pose = merge(limb(side, ARM.hip), hand(side, HAND.relaxed));
        return [side, Object.entries(pose).map(([bone, a]) => [bone, this.authored(a, bone, false)])];
      }),
    );
  }

  // An authored Euler pose angle as a quaternion; `actual` converts it to the node's own convention.
  authored(angles, bone, actual, out = new THREE.Quaternion()) {
    out.setFromEuler(_euler.set(angles[0], angles[1], angles[2], eulerOrder(bone)));
    // VRM 0.x normalized bones (after VRMUtils.rotateVRM0) have their X and Z axes flipped.
    if (actual && this.flipAxes) out.set(-out.x, out.y, -out.z, out.w);
    return out;
  }

  // One arm's bones and rest measurements, or null when the model lacks the arm.
  chain(side) {
    const get = (name) => this.vrm.humanoid.getNormalizedBoneNode(side + name);
    const c = { side, clavicle: get('Shoulder'), upper: get('UpperArm'), lower: get('LowerArm'), hand: get('Hand') };
    if (!c.upper || !c.lower || !c.hand) return null;
    c.knuckle = get('IndexProximal');
    const inter = get('IndexIntermediate');
    const distal = get('IndexDistal');

    const shoulder = c.upper.getWorldPosition(new THREE.Vector3());
    const elbow = c.lower.getWorldPosition(new THREE.Vector3());
    const wrist = c.hand.getWorldPosition(new THREE.Vector3());
    const tip = new THREE.Vector3();
    if (c.knuckle && inter && distal) {
      const joint = distal.getWorldPosition(new THREE.Vector3());
      const dir = joint.clone().sub(inter.getWorldPosition(new THREE.Vector3())).normalize();
      const reach = fingertipReach(this.vrm, side, joint, dir) ?? joint.distanceTo(inter.getWorldPosition(_v)) * 0.9;
      tip.copy(joint).addScaledVector(dir, reach);
      c.tipNode = distal;
    } else {
      // No finger bones: aim with the far end of the hand.
      c.knuckle = null;
      tip.copy(wrist).addScaledVector(_v.subVectors(wrist, elbow).normalize(), wrist.distanceTo(elbow) * 0.7);
      c.tipNode = c.hand;
    }
    c.tipLocal = c.tipNode.worldToLocal(tip.clone());
    c.shoulder = shoulder; // at rest, for the metrics
    c.reach = shoulder.distanceTo(tip);
    c.upperLength = shoulder.distanceTo(elbow);
    c.foreLength = elbow.distanceTo(tip); // elbow to fingertip
    // Axes in the bones' own frames: along the upper arm, along the forearm, and the palm's normal.
    c.armAxis = c.lower.position.clone().normalize();
    c.foreAxis = c.hand.position.clone().normalize();
    // The palm faces -y at rest (flipping x and z for VRM 0.x leaves that alone).
    c.palmAxis = orthogonal(DOWN, c.foreAxis, FORWARD, new THREE.Vector3());
    // Which way the forearm swings when the elbow bends, in the upper arm's frame.
    const bent = c.foreAxis.clone().applyQuaternion(this.elbowQuat(side, ELBOW_BEND, new THREE.Quaternion()));
    c.bendAxis = orthogonal(bent, c.armAxis, FORWARD, new THREE.Vector3());

    c.fingers = Object.entries(pointingHand(side))
      .map(([bone, a]) => ({ node: this.vrm.humanoid.getNormalizedBoneNode(bone), q: this.authored(a, bone, true) }))
      .filter((f) => f.node);
    c.nodes = [c.clavicle, c.upper, c.lower, c.hand, ...c.fingers.map((f) => f.node)].filter(Boolean);
    c.saved = c.nodes.map(() => new THREE.Quaternion());
    return c;
  }

  // The elbow bent by `angle` (rad), as the lower arm's local rotation.
  elbowQuat(side, angle, out) {
    return this.authored(limb(side, { LowerArm: [0, -angle, 0] })[`${side}LowerArm`], `${side}LowerArm`, true, out);
  }

  get active() {
    return this.side !== null;
  }

  // Not moving on its own: fully up or down, aimed where it should be.
  get settled() {
    const still = this.ramp === 0 || this.ramp === 1;
    return still && (!this.target || Math.hypot(this.aim.x - this.target.x, this.aim.y - this.target.y) < 0.5);
  }

  // Starts pointing at window pixel `target` (with an optional box {x, y, w, h} around the thing), with
  // `arm` ('left' or 'right'; her right is screen-left) or whichever suits. Returns false if she can't.
  start(target, box, arm) {
    const side = this.pickSide(target, arm);
    if (!side) return false;
    // Sweep from the old spot when she's already pointing with this arm; otherwise aim straight there.
    if (this.side !== side || this.weight === 0) {
      this.aim = { x: target.x, y: target.y };
      if (this.side !== side) this.ramp = 0;
    }
    this.side = side;
    this.target = { x: target.x, y: target.y };
    this.box = box;
    this.on = true;
    this.nextGlance = this.time + rand(2, 4);
    return true;
  }

  // Lowers the arm.
  stop() {
    this.on = false;
  }

  // Drops everything at once (she's out of sight).
  cancel() {
    this.on = false;
    this.side = null;
    this.target = null;
    this.ramp = 0;
    this.weight = 0;
    this.glance = 0;
    this.glanceWant = 0;
  }

  // Her right is screen-left: she faces the viewer. An arm that would have to reach across her body to get
  // there points with the other one instead.
  pickSide(target, arm) {
    const centre = this.stage.toWindow(this.torso.getWorldPosition(_v));
    const natural = target.x < centre.x ? 'right' : 'left';
    let side = arm === 'left' || arm === 'right' ? arm : natural;
    if (side !== natural && Math.abs(target.x - centre.x) > 0.05 * this.stage.bodyRect.h) side = natural;
    if (!this.chains[side]) side = other(side);
    return this.chains[side] ? side : null;
  }

  // `talking`: she's saying something (glances at the viewer). `yielding`: a gesture needs both hands
  // (taking a picture), so the arm comes down until it's done.
  // The assistant clicks the spot: a quick jab toward it (see the header). Only while pointing.
  press() {
    if (this.on && this.target) this.pressT = 0;
  }

  update(dt, talking, yielding) {
    this.time += dt;
    this.pressT += dt;
    this.push = this.pressT < PRESS_S ? PRESS_DEPTH * Math.sin((Math.PI * this.pressT) / PRESS_S) : 0;
    const up = this.on && !yielding;
    this.ramp = clamp(this.ramp + (up ? dt / RAISE : -dt / LOWER), 0, 1);
    this.weight = smoothstep(this.ramp);
    if (!this.on && this.ramp === 0) {
      this.side = null;
      this.target = null;
    }
    if (this.target) {
      this.aim.x = damp(this.aim.x, this.target.x, RETARGET_TAU, dt);
      this.aim.y = damp(this.aim.y, this.target.y, RETARGET_TAU, dt);
    }
    // While she talks about it, she looks at the viewer every few seconds, then back at the thing.
    if (talking && this.weight > 0.5) {
      if (this.time >= this.nextGlance) {
        this.glanceWant = 1 - this.glanceWant;
        this.nextGlance = this.time + (this.glanceWant ? rand(0.8, 1.5) : rand(2.5, 5));
      }
    } else {
      this.glanceWant = 0;
      this.nextGlance = Math.max(this.nextGlance, this.time + 1.5);
    }
    this.glance = damp(this.glance, this.glanceWant, 0.15, dt);
  }

  // Before the pose is written: the other hand to the hip and the torso toward the spot, into the
  // animator's working pose (`work`, authored quaternions) and additive offsets (`add`).
  prePose(work, add) {
    const w = this.weight;
    if (w <= 0) return;
    for (const [bone, q] of this.hipQuats[other(this.side)]) work[bone]?.slerp(q, w);
    // Spine and neck: y > 0 turns to her left, z > 0 tilts the top to her right, x > 0 bends forward.
    const toward = this.side === 'left' ? 1 : -1;
    // Something far below (on the taskbar, say) gets a real lean, so the arm reaches out from her side
    // instead of hanging down along it.
    const lean = (TORSO_LEAN + LOW_LEAN * clamp((-this.elevation - 0.6) / 0.7, 0, 1)) * toward * w;
    const bow = clamp(-0.08 * this.elevation, -0.04, 0.06) * w;
    addAngles(add, 'spine', 0, TORSO_YAW * toward * w, -0.5 * lean);
    addAngles(add, 'chest', bow, TORSO_YAW * toward * w, -lean);
  }

  // The gaze override { yaw, pitch, weight } toward the spot from `headPos`, or null.
  look(headPos) {
    const weight = this.weight * (1 - this.glance);
    if (weight <= 0.001 || !this.target) return null;
    const g = this.rayPoint(this.aim, GAZE_DEPTH, _v);
    const dx = g.x - headPos.x;
    const dz = g.z - headPos.z;
    return { yaw: Math.atan2(dx, dz), pitch: Math.atan2(g.y - headPos.y, Math.hypot(dx, dz)), weight };
  }

  // The point on the camera ray through window pixel `px` that lies `depth` m in front of her body.
  rayPoint(px, depth, out) {
    const ray = this.stage.rayAt(px.x, px.y, _ray);
    const z = this.torso.getWorldPosition(out).z + depth;
    const dz = Math.min(ray.direction.z, -1e-3); // the camera looks along -z, toward her
    return ray.at((z - ray.origin.z) / dz, out);
  }

  // How much room (px) the fingertip needs before the spot along screen direction (ux, uy) from it:
  // outside the ring, or outside the box's padded outline.
  room(ux, uy) {
    if (!this.box) return RING_RADIUS + CLEARANCE;
    const b = this.box;
    const pad = BOX_PAD + CLEARANCE;
    const tx = ux > 1e-6 ? (b.x + b.w + pad - this.aim.x) / ux : ux < -1e-6 ? (b.x - pad - this.aim.x) / ux : Infinity;
    const ty = uy > 1e-6 ? (b.y + b.h + pad - this.aim.y) / uy : uy < -1e-6 ? (b.y - pad - this.aim.y) / uy : Infinity;
    // A huge box (a whole window) would fold her arm away to nothing; past a point she just points at it.
    return clamp(Math.min(tx, ty), RING_RADIUS * 0.5, 8 * RING_RADIUS);
  }

  // Room left (px) between the fingertip and the marker if the arm reaches `length` m from shoulder `S`
  // toward aim point `A` (negative: the finger would be over it).
  spare(S, A, length) {
    _T.subVectors(A, S).normalize().multiplyScalar(length).add(S);
    const tip = this.stage.toWindow(_T, _p1);
    const shoulder = this.stage.toWindow(S, _p2);
    const ux = shoulder.x - this.aim.x;
    const uy = shoulder.y - this.aim.y;
    const len = Math.hypot(ux, uy) || 1;
    // Distance from the spot back toward the shoulder, along the screen line the arm makes.
    const along = ((tip.x - this.aim.x) * ux + (tip.y - this.aim.y) * uy) / len;
    return along - this.room(ux / len, uy / len);
  }

  // Picks how far forward to reach and how much to bend the elbow, so the fingertip stops just short of
  // the marker. Sets `A` to the aim point and returns the elbow bend.
  reachFor(c, S, A) {
    const straight = Math.sqrt(c.upperLength ** 2 + c.foreLength ** 2 + 2 * c.upperLength * c.foreLength * Math.cos(ELBOW_BEND));
    const depth = Math.min(DEPTH + this.push, MAX_DEPTH);
    this.rayPoint(this.aim, depth, A);
    if (this.spare(S, A, straight) >= 0) return ELBOW_BEND;
    // Reach further forward: the arm foreshortens on screen.
    let lo = depth;
    let hi = MAX_DEPTH;
    this.rayPoint(this.aim, hi, A);
    if (this.spare(S, A, straight) >= 0) {
      for (let i = 0; i < 10; i++) {
        const mid = (lo + hi) / 2;
        if (this.spare(S, this.rayPoint(this.aim, mid, A), straight) >= 0) hi = mid;
        else lo = mid;
      }
      this.rayPoint(this.aim, hi, A);
      return ELBOW_BEND;
    }
    // Still too close: bend the elbow so the arm is short enough (law of cosines on upper arm + forearm).
    let short = straight * 0.35;
    let long = straight;
    for (let i = 0; i < 12; i++) {
      const mid = (short + long) / 2;
      if (this.spare(S, A, mid) >= 0) short = mid;
      else long = mid;
    }
    const a = c.upperLength;
    const b = c.foreLength;
    return clamp(Math.acos(clamp((short * short - a * a - b * b) / (2 * a * b), -1, 1)), ELBOW_BEND, MAX_BEND);
  }

  // After the pose is written: solve the pointing arm and blend it over the animator's.
  apply() {
    const c = this.side && this.chains[this.side];
    const w = this.weight;
    if (!c || w <= 0 || !this.target) return;
    c.nodes.forEach((n, i) => c.saved[i].copy(n.quaternion));
    const side = this.side;

    const S = c.upper.getWorldPosition(_S);
    const A = this.rayPoint(this.aim, Math.min(DEPTH + this.push, MAX_DEPTH), _A);
    // The clavicle comes up with an arm that points high, and forward with one that reaches forward.
    const d = _d.subVectors(A, S).normalize();
    this.elevation = Math.asin(clamp(d.y, -1, 1));
    if (c.clavicle) {
      const up = clamp(this.elevation / 1.2, 0, 1);
      const angles = limb(side, { Shoulder: [0, -0.12 * clamp(d.z, 0, 1), 0.2 * up] })[`${side}Shoulder`];
      c.clavicle.quaternion.multiply(this.authored(angles, `${side}Shoulder`, true, _q));
      c.upper.getWorldPosition(S);
    }
    const bend = this.reachFor(c, S, A);
    d.subVectors(A, S).normalize();

    // Upper arm along the aim, turned so the elbow drops down and back.
    const parent = c.upper.parent.getWorldQuaternion(_qa);
    _Pinv.copy(parent).invert();
    const U = frameRotation(c.armAxis, c.bendAxis, d, orthogonal(ELBOW_TOWARD, d, FORWARD, _x), _U);
    c.upper.quaternion.copy(_Pinv).multiply(U);

    // Forearm: the elbow bend, then a twist so the palm faces down and a little inward.
    const elbow = this.elbowQuat(side, bend, c.lower.quaternion);
    const fore = _qb.copy(U).multiply(elbow); // forearm's world rotation before the twist
    const axisWorld = _v.copy(c.foreAxis).applyQuaternion(fore);
    const inward = _w.set(side === 'left' ? -PALM_INWARD : PALM_INWARD, 0, 0).add(DOWN);
    const palm = orthogonal(inward, axisWorld, FORWARD, _y).applyQuaternion(fore.invert());
    orthogonal(palm, c.foreAxis, c.palmAxis, palm);
    const twist = Math.atan2(_x.crossVectors(c.palmAxis, palm).dot(c.foreAxis), c.palmAxis.dot(palm));
    c.lower.quaternion.multiply(_q.setFromAxisAngle(c.foreAxis, clamp(twist, -MAX_TWIST, MAX_TWIST)));

    // Straight wrist and the pointing hand.
    c.hand.quaternion.identity();
    for (const f of c.fingers) f.node.quaternion.copy(f.q);

    // The bend and the finger's offset from the arm's axis leave the fingertip off the line: turn the
    // whole arm about the shoulder to put it on, then the wrist so the finger points along it too, and
    // repeat (each step barely disturbs the other), ending on the shoulder line.
    for (let pass = 0; pass <= SOLVE_PASSES; pass++) {
      this.swingOnto(c, S, A, U);
      if (pass === SOLVE_PASSES || !c.knuckle) continue;
      c.upper.updateWorldMatrix(false, true);
      const T = c.tipNode.localToWorld(_T.copy(c.tipLocal));
      const K = c.knuckle.getWorldPosition(_K);
      // Only while the tip stops short of the aim point; past it the wanted direction flips.
      if (_v.subVectors(A, T).dot(d) <= 0.01) continue;
      _q.setFromUnitVectors(_w.subVectors(T, K).normalize(), _v.normalize());
      const handWorld = c.hand.getWorldQuaternion(_qa).premultiply(_q);
      const lowerWorld = c.lower.getWorldQuaternion(_qb);
      c.hand.quaternion.copy(lowerWorld.invert().multiply(handWorld));
      const bent = 2 * Math.acos(clamp(Math.abs(c.hand.quaternion.w), 0, 1));
      if (bent > MAX_WRIST) c.hand.quaternion.slerp(_q.identity(), 1 - MAX_WRIST / bent);
    }

    // Blend over the animator's arm (and hand).
    if (w < 1) c.nodes.forEach((n, i) => n.quaternion.copy(c.saved[i].slerp(n.quaternion, w)));
  }

  // Turns the upper arm (world rotation `U`, updated) about shoulder `S` so the fingertip lies on the line
  // from the shoulder to aim point `A`. The rest of the arm is rigid meanwhile, so one turn is exact.
  swingOnto(c, S, A, U) {
    c.upper.updateWorldMatrix(false, true);
    const T = c.tipNode.localToWorld(_T.copy(c.tipLocal));
    _q.setFromUnitVectors(_v.subVectors(T, S).normalize(), _w.subVectors(A, S).normalize());
    U.premultiply(_q);
    c.upper.quaternion.copy(_Pinv).multiply(U);
  }

  // How far (px) the spot is from the shoulder-to-fingertip line and from the finger's own line, as drawn
  // now. For the debug log; null unless fully pointing.
  measure() {
    const c = this.side && this.chains[this.side];
    if (!c || this.weight < 1 || !this.target) return null;
    c.upper.updateWorldMatrix(true, true);
    const S = this.stage.toWindow(c.upper.getWorldPosition(_S), { x: 0, y: 0 });
    const T = this.stage.toWindow(c.tipNode.localToWorld(_T.copy(c.tipLocal)), { x: 0, y: 0 });
    const out = { side: this.side, target: this.target, shoulderLine: lineDistance(this.target, S, T), tip: T };
    if (c.knuckle) out.fingerLine = lineDistance(this.target, this.stage.toWindow(c.knuckle.getWorldPosition(_K)), T);
    out.tipGap = Math.hypot(T.x - this.target.x, T.y - this.target.y);
    return out;
  }
}

const _p1 = { x: 0, y: 0 };
const _p2 = { x: 0, y: 0 };
