// The procedural animator: everything that moves her body.
//
// Every frame builds each bone's rotation from layers:
//   1. the state's base pose, eased from the previous state's pose on every state change, with pointing's
//      and flying's changes blended in (the free hand on her hip, legs tucked up);
//   2. gestures, each blended over the base by its own fade-in/out weight, then the grip of a hand that
//      holds a prop (see setGrip);
//   3. additive life: breathing, sway and weight shifts, head micro-motion, talking motion, emotion
//      posture, and turning the head toward whatever she is looking at;
//   4. post-pose layers that override bones outright once the pose is written: the pointing arm, solved
//      against the current body so it stays locked on target, then any added with addPostPose (a
//      magnifier kept in front of her eye).
// Nothing is keyframed against wall-clock loops, so no two moments look quite the same.

import * as THREE from 'three';
import { FIDGETS, GesturePlayer } from './gestures.js';
import { Pointing } from './pointing.js';
import { HOVER_LEGS, POSED_BONES, STATE_POSES, eulerOrder, hand } from './poses.js';
import { Noise, clamp, damp, dampFactor, easeInOut, pick, rand } from './math.js';

const TRANSITION = 0.6; // seconds to ease between state poses
const LOOK_DISTANCE = 2.5; // m: the cursor is treated as a point this far in front of her face
const CURSOR_IDLE = 6; // s without cursor movement before she stops following it
export const GLANCE_S = 1.3; // a glance at the window the user switched to: how long she looks its way
const GLANCE_IN = 0.1; // s to turn her gaze there (her eyes and head then follow at their own pace)
const GLANCE_OUT = 0.4; // s to turn it back

// How much of the gaze turns the head (the eyes cover the rest) and how it splits over neck and head.
const HEAD_FOLLOW_YAW = 0.5;
const HEAD_FOLLOW_PITCH = 0.4;
const NOD_PUSH_S = 0.08; // a talking head bob's push is spread over this long

// Small posture changes that go with an emotion (additive Euler offsets, scaled by the emotion's weight).
const EMOTION_POSTURE = {
  happy: { chest: [-0.03, 0, 0], head: [-0.03, 0, 0.03] },
  excited: { chest: [-0.05, 0, 0], head: [-0.05, 0, 0] },
  sad: { chest: [0.05, 0, 0], neck: [0.04, 0, 0], head: [0.1, 0, 0.03], leftShoulder: [0, 0, -0.04], rightShoulder: [0, 0, 0.04] },
  angry: { spine: [0.03, 0, 0], head: [0.06, 0, 0], leftShoulder: [0, 0, 0.04], rightShoulder: [0, 0, -0.04] },
  surprised: { spine: [-0.03, 0, 0], head: [-0.06, 0, 0] },
  curious: { head: [0.02, 0.05, 0.14] },
  smug: { chest: [-0.04, 0, 0], head: [-0.07, -0.05, 0.08] },
  playful: { head: [0, 0.04, -0.1] },
  thinking: { head: [-0.03, 0, 0.06] },
  laughing: { head: [-0.05, 0, 0] },
  relaxed: { head: [0, 0, 0.03] },
};

// Where the eyes go while thinking: up and to one side, as if searching memory.
const THINK_LOOK = { yaw: 0.4, pitch: 0.32 };

// Hovering while she's away from home: a slow bob (m, Hz), and how fast the tucked legs blend (s). At home
// (floating while you talk to her, if cfg.hover, or napping) she doesn't bob: she lifts a little off her spot and
// stays there.
const HOVER_BOB = 0.012;
const HOVER_HZ = 0.45;
const HOVER_TAU = 0.3;
export const HOVER_LIFT = 0.025; // m
const LIFT_TAU = 0.45;

const _euler = new THREE.Euler();
const _q = new THREE.Quaternion();
const _v = new THREE.Vector3();

function quatOf(angles, order, out = new THREE.Quaternion()) {
  return out.setFromEuler(_euler.set(angles[0], angles[1], angles[2], order));
}

export class Animator {
  constructor(vrm, stage, expressions, lipsync) {
    this.vrm = vrm;
    this.stage = stage;
    this.expressions = expressions;
    this.lipsync = lipsync;
    this.gestures = new GesturePlayer();
    this.time = 0;

    this.bones = {};
    for (const name of POSED_BONES) {
      const node = vrm.humanoid.getNormalizedBoneNode(name);
      if (node) this.bones[name] = node;
    }
    this.names = Object.keys(this.bones);
    // Arm bones take additive offsets in the parent's frame, so a "raise" stays a raise however the arm
    // is twisted; the spine, head and legs take them in their own frame.
    this.preMultiply = Object.fromEntries(this.names.map((n) => [n, eulerOrder(n) !== 'XYZ']));
    // VRM 0.x normalized bones (after VRMUtils.rotateVRM0) have their X and Z axes flipped.
    this.flipAxes = vrm.meta?.metaVersion === '0';
    // Measures her arms, so it must come before the first pose below.
    this.pointing = new Pointing(vrm, stage, this.flipAxes);
    this.postPose = [this.pointing];
    this.gripHolder = null; // see setGrip
    this.gripCache = new Map();

    // Pose quaternions per state, converted once.
    this.stateQuats = {};
    for (const [state, pose] of Object.entries(STATE_POSES)) this.stateQuats[state] = this.poseQuats(pose);

    this.state = 'idle';
    this.base = this.poseQuats(STATE_POSES.idle); // current base pose
    this.from = this.poseQuats(STATE_POSES.idle); // snapshot at the start of a transition
    this.transition = 1;
    this.work = Object.fromEntries(this.names.map((n) => [n, new THREE.Quaternion()]));

    this.noise = Object.fromEntries(['breath', 'hipsZ', 'hipsY', 'headX', 'headY', 'headZ', 'arms', 'talkY', 'talkZ', 'lookX', 'lookY'].map((k) => [k, new Noise()]));
    this.breathPhase = 0;
    this.breathRate = rand(0.22, 0.27);
    this.weightSide = 1;
    this.weightShift = 0;
    this.nextIdleAction = rand(15, 30);
    this.nextBeat = rand(1.5, 3);
    this.posture = {}; // smoothed emotion posture offsets
    this.life = 1; // how much idle motion: "stopped" keeps her very still
    this.lifeSmoothed = 1;
    this.energy = { sleepy: 0, perky: 0 }; // from the clock (daytime.js): see setEnergy
    this.talkSway = 0;

    // Talking head bob: a damped spring kicked by loudness peaks.
    this.nodPos = 0;
    this.nodVel = 0;
    this.levelSlow = 0;
    this.nodCooldown = 0;
    this.nodPush = 0;
    this.nodPushLeft = 0;
    this.levelHead = 0;

    // Gaze, as yaw/pitch (radians) relative to facing straight ahead.
    this.lookTarget = new THREE.Object3D();
    stage.scene.add(this.lookTarget);
    vrm.lookAt.target = this.lookTarget;
    vrm.lookAt.autoUpdate = true;
    this.gaze = { yaw: 0, pitch: 0 }; // where she wants to look
    this.eyes = { yaw: 0, pitch: 0 }; // eyes follow fast
    this.head = { yaw: 0, pitch: 0 }; // the head follows slower
    this.cursor = null;
    this.cursorMovedAt = -Infinity;
    this.glance = null; // { x, y, t }: a quick look toward a spot (glanceAt)
    this.thinkSide = 1;

    this.rootY = 0; // set by the pop-in/out animation
    this.flying = false; // away from home (pointing at something): she hovers with her legs tucked
    this.hovering = false; // at home, floating like that anyway (setHovering)
    this.hover = 0;
    this.flyingWeight = 0; // how much of the hover is flying: only that bobs
    this.lift = 0; // m she's floated up off her spot at home
    this.hoverLegs = Object.entries(HOVER_LEGS)
      .filter(([bone]) => this.bones[bone])
      .map(([bone, angles]) => [bone, quatOf(angles, eulerOrder(bone))]);
    this.applyPose(this.base, {});
  }

  // Away from home (pointing at something) she flies: hovers with her legs tucked. `now` skips the blend,
  // for when she's out of sight, so she never arrives half-way between standing and flying.
  setFlying(flying, now = false) {
    this.flying = flying;
    if (now) this.hover = flying ? 1 : 0;
  }

  // At home she can float too, legs tucked and bobbing, lifted a little off her spot: while you talk to her,
  // or napping on her cloud.
  setHovering(hovering) {
    this.hovering = hovering === true;
  }

  // How awake she is (daytime.js): sleepy, she breathes slower, moves less and yawns; perky, she moves a little
  // more and fidgets more often.
  setEnergy({ sleepy = 0, perky = 0 } = {}) {
    this.energy = { sleepy: clamp(sleepy, 0, 1), perky: clamp(perky, 0, 1) };
  }

  // Adds a layer that runs after the pose is written to the normalized bones each frame, in order after
  // the pointing arm: an object with apply(dt) that may overwrite bone rotations outright.
  addPostPose(layer) {
    this.postPose.push(layer);
  }

  // What holds her props (props.js's PropRack): every frame its `grip`, { hand, shape, weight } or null,
  // closes that hand round the prop (see applyGrip).
  setGrip(holder) {
    this.gripHolder = holder;
  }

  poseQuats(pose) {
    const out = {};
    for (const name of this.names) out[name] = pose[name] ? quatOf(pose[name], eulerOrder(name)) : new THREE.Quaternion();
    return out;
  }

  setState(state) {
    if (!STATE_POSES[state] || state === this.state) return;
    this.state = state;
    for (const n of this.names) this.from[n].copy(this.base[n]);
    this.transition = 0;
    this.life = state === 'stopped' ? 0.3 : 1;
    if (state === 'thinking' || state === 'transcribing') this.thinkSide = Math.random() < 0.5 ? -1 : 1;
    if (state === 'idle') this.nextIdleAction = this.time + rand(15, 40);
    if (state !== 'speaking') this.gestures.releaseSustained();
  }

  // Nothing fast is happening: only breathing, sway and gaze drift.
  get calm() {
    const quietState = this.state === 'idle' || this.state === 'thinking' || this.state === 'transcribing' || this.state === 'stopped';
    const settledHover = (this.hover === 0 || this.hover === 1) && (this.flyingWeight === 0 || this.flyingWeight === 1);
    const still = this.pointing.settled && settledHover && (this.lift === 0 || this.lift === HOVER_LIFT);
    const slow = this.gestures.active.every((g) => g.def.calm);
    return quietState && still && this.transition >= 1 && slow && !this.lipsync.talking;
  }

  playGesture(name, options) {
    return this.gestures.play(name, options);
  }

  endSegment() {
    this.gestures.releaseSustained();
  }

  // Cursor position in window CSS pixels (it may be outside the window).
  setCursor(x, y) {
    if (!this.cursor || Math.hypot(x - this.cursor.x, y - this.cursor.y) > 2) this.cursorMovedAt = this.time;
    this.cursor = { x, y };
  }

  // A quick look toward a spot (window CSS pixels; it may be outside the window): the window the user just
  // switched to. Over after GLANCE_S.
  glanceAt(x, y) {
    if (Number.isFinite(x) && Number.isFinite(y)) this.glance = { x, y, t: 0 };
  }

  // How much the glance has her gaze now: turned there quickly, held, turned back; 0 without one.
  glanceWeight() {
    const g = this.glance;
    return g ? clamp(Math.min(g.t / GLANCE_IN, (GLANCE_S - g.t) / GLANCE_OUT), 0, 1) : 0;
  }

  update(dt) {
    this.time += dt;
    if (this.glance && (this.glance.t += dt) >= GLANCE_S) this.glance = null;
    this.gestures.update(dt);
    this.pointing.update(dt, this.state === 'speaking' && this.lipsync.talking, this.gestures.bothHandsBusy);
    this.updateBase(dt);
    const add = {};
    this.pointing.prePose(this.work, add);
    const bob = this.updateHover(dt);
    const lookOverride = this.applyGestures(add);
    this.applyGrip();
    this.addLife(add, dt);
    this.addTalking(add, dt);
    this.addPosture(add, dt);
    this.updateGaze(add, dt, lookOverride);
    this.idleActions();
    this.applyPose(this.work, add);
    this.vrm.scene.position.y = this.rootY + bob;
    for (const layer of this.postPose) layer.apply(dt);
  }

  // Flying or hovering: tucks the legs up (blended into the working pose) and returns the bob's height while
  // flying, plus the lift at home (m).
  updateHover(dt) {
    const want = this.flying || this.hovering ? 1 : 0;
    this.hover = damp(this.hover, want, HOVER_TAU, dt);
    if (Math.abs(this.hover - want) < 1e-3) this.hover = want;
    const flying = this.flying ? 1 : 0;
    this.flyingWeight = damp(this.flyingWeight, flying, HOVER_TAU, dt);
    if (Math.abs(this.flyingWeight - flying) < 1e-3) this.flyingWeight = flying;
    const lift = this.hovering && !this.flying ? HOVER_LIFT : 0;
    this.lift = damp(this.lift, lift, LIFT_TAU, dt);
    if (Math.abs(this.lift - lift) < 1e-5) this.lift = lift;
    if (this.hover === 0) return this.lift;
    for (const [bone, q] of this.hoverLegs) this.work[bone].slerp(q, this.hover);
    return this.lift + HOVER_BOB * this.flyingWeight * Math.sin(2 * Math.PI * HOVER_HZ * this.time);
  }

  updateBase(dt) {
    const target = this.stateQuats[this.state];
    this.transition = Math.min(1, this.transition + dt / TRANSITION);
    const t = easeInOut(this.transition);
    for (const n of this.names) {
      this.base[n].slerpQuaternions(this.from[n], target[n], t);
      this.work[n].copy(this.base[n]);
    }
  }

  // Blends gesture poses into `work` and collects their additive offsets. Returns the blended gaze
  // override from gestures, if any.
  applyGestures(add) {
    let look = null;
    this.gestures.forEach((w, sample) => {
      if (sample.pose) {
        for (const [bone, angles] of Object.entries(sample.pose)) {
          if (this.work[bone]) this.work[bone].slerp(quatOf(angles, eulerOrder(bone), _q), w);
        }
      }
      if (sample.add) {
        for (const [bone, a] of Object.entries(sample.add)) addAngles(add, bone, a[0] * w, a[1] * w, a[2] * w);
      }
      if (sample.look) look = { yaw: sample.look.yaw, pitch: sample.look.pitch, weight: Math.max(w, look ? look.weight : 0) };
    });
    return look;
  }

  // The hand holding a prop closes round it, by the grip's weight (which follows the prop's pop in and
  // out). After the gestures, so it wins over their hand shapes, but before the additive offsets, so a
  // finger action on top (the camera's shutter press) still shows. A pointing hand, solved once the pose
  // is written, still wins over it.
  applyGrip() {
    const grip = this.gripHolder?.grip;
    if (!grip || !(grip.weight > 0)) return;
    const w = Math.min(1, grip.weight);
    for (const [bone, q] of this.gripQuats(grip.hand, grip.shape)) this.work[bone]?.slerp(q, w);
  }

  // A grip's hand shape as pose quaternions, made once per shape (each prop has one).
  gripQuats(side, shape) {
    const key = `${side} ${shape.curl} ${shape.spread} ${shape.thumb}`;
    let quats = this.gripCache.get(key);
    if (!quats) {
      quats = Object.entries(hand(side, shape)).map(([bone, angles]) => [bone, quatOf(angles, eulerOrder(bone))]);
      this.gripCache.set(key, quats);
    }
    return quats;
  }

  // Breathing, sway, weight shift and head micro-motion: the difference between alive and a statue.
  addLife(add, dt) {
    const n = this.noise;
    const t = this.time;
    const { sleepy, perky } = this.energy;
    const awake = 1 - 0.3 * sleepy + 0.15 * perky;
    const life = (this.lifeSmoothed = damp(this.lifeSmoothed, this.life * awake, 0.5, dt));

    // Breathing, at a rate that drifts a little from breath to breath.
    this.breathRate = clamp(this.breathRate + n.breath.at(t * 0.1) * 0.002, 0.2, 0.3);
    this.breathPhase += dt * this.breathRate * (1 - 0.25 * sleepy) * 2 * Math.PI;
    const b = Math.sin(this.breathPhase) * (0.6 + 0.4 * life);
    addAngles(add, 'chest', -0.014 * b, 0, 0);
    addAngles(add, 'upperChest', -0.01 * b, 0, 0);
    addAngles(add, 'neck', 0.014 * b, 0, 0); // keeps the head level while the chest rises
    addAngles(add, 'leftShoulder', 0, 0, 0.012 * b);
    addAngles(add, 'rightShoulder', 0, 0, -0.012 * b);
    addAngles(add, 'leftUpperArm', 0, 0, 0.01 * b);
    addAngles(add, 'rightUpperArm', 0, 0, -0.01 * b);

    // Weight shift: the hips drop toward one side, the spine counters to keep the head over the feet,
    // and the legs counter-rotate so the feet stay planted. The unloaded knee softens.
    this.weightShift = damp(this.weightShift, this.weightSide, 0.9, dt);
    const hipsZ = (0.03 * this.weightShift + 0.012 * n.hipsZ.at(t * 0.13)) * life;
    const hipsY = 0.025 * n.hipsY.at(t * 0.11) * life;
    addAngles(add, 'hips', 0, hipsY, hipsZ);
    addAngles(add, 'spine', 0, -hipsY * 0.5, -hipsZ * 0.7);
    addAngles(add, 'chest', 0, -hipsY * 0.3, -hipsZ * 0.3);
    addAngles(add, 'leftUpperLeg', 0, -hipsY, -hipsZ);
    addAngles(add, 'rightUpperLeg', 0, -hipsY, -hipsZ);
    const relaxedKnee = this.weightShift > 0 ? 'right' : 'left';
    const soft = Math.abs(this.weightShift) * 0.1 * life;
    addAngles(add, `${relaxedKnee}UpperLeg`, -soft * 0.5, 0, 0);
    addAngles(add, `${relaxedKnee}LowerLeg`, soft, 0, 0);
    addAngles(add, `${relaxedKnee}Foot`, -soft * 0.5, 0, 0);

    addAngles(add, 'head', 0.025 * n.headX.at(t * 0.23) * life, 0.035 * n.headY.at(t * 0.19) * life, 0.02 * n.headZ.at(t * 0.21) * life);
    const arms = n.arms.at(t * 0.17) * life;
    addAngles(add, 'leftUpperArm', 0.02 * arms, 0, 0.015 * arms);
    addAngles(add, 'rightUpperArm', -0.02 * arms, 0, 0.015 * arms);
    addAngles(add, 'leftLowerArm', 0, -0.04 * arms, 0);
    addAngles(add, 'rightLowerArm', 0, -0.04 * arms, 0);
  }

  // Talking: the head bobs on stressed syllables, the torso sways, and the hands make open beats.
  addTalking(add, dt) {
    const level = this.lipsync.level;
    const talking = this.state === 'speaking' && this.lipsync.talking;
    this.levelSlow = damp(this.levelSlow, level, 0.4, dt);
    this.nodCooldown -= dt;
    if (talking && this.nodCooldown <= 0 && level - this.levelSlow > 0.16) {
      // The push is spread over NOD_PUSH_S rather than given at once, so the head eases into each bob instead of
      // twitching (a laugh's loud "ha"s would otherwise jerk it every few frames).
      this.nodPush = rand(0.5, 1.1) / NOD_PUSH_S;
      this.nodPushLeft = NOD_PUSH_S;
      this.nodCooldown = rand(0.3, 0.6);
    }
    if (this.nodPushLeft > 0) {
      this.nodVel += this.nodPush * Math.min(dt, this.nodPushLeft);
      this.nodPushLeft -= dt;
    }
    // Critically-damped-ish spring back to rest.
    this.nodVel += (-90 * this.nodPos - 13 * this.nodVel) * dt;
    this.nodPos += this.nodVel * dt;
    // Loudness lifts the chin a touch, smoothed so it follows the voice's swell rather than every syllable.
    this.levelHead = damp(this.levelHead, level, 0.12, dt);
    addAngles(add, 'head', this.nodPos * 0.9 + this.levelHead * 0.03, 0, 0);
    addAngles(add, 'neck', this.nodPos * 0.3, 0, 0);

    const sway = (this.talkSway = damp(this.talkSway, talking ? 1 : 0, 0.6, dt));
    const t = this.time;
    addAngles(add, 'spine', 0, 0.035 * sway * this.noise.talkY.at(t * 0.45), 0.02 * sway * this.noise.talkZ.at(t * 0.38));
    addAngles(add, 'head', 0, 0.04 * sway * this.noise.talkY.at(t * 0.7 + 5), 0.03 * sway * this.noise.talkZ.at(t * 0.6 + 9));

    // While pointing, the free hand stays on her hip rather than beating.
    if (talking && this.time >= this.nextBeat && !this.gestures.armsBusy && !this.pointing.active) {
      this.gestures.play('beat');
      this.nextBeat = this.time + rand(2.2, 5);
    }
  }

  // Emotion posture follows the face: slumped when sad, chest up when happy, a tilt when curious.
  addPosture(add, dt) {
    const target = EMOTION_POSTURE[this.expressions.currentEmotion] || {};
    const k = dampFactor(0.5, dt);
    for (const bone of new Set([...Object.keys(this.posture), ...Object.keys(target)])) {
      const cur = this.posture[bone] || (this.posture[bone] = [0, 0, 0]);
      const tgt = target[bone] || [0, 0, 0];
      for (let i = 0; i < 3; i++) cur[i] += (tgt[i] - cur[i]) * k;
      addAngles(add, bone, cur[0], cur[1], cur[2]);
    }
  }

  // Decides where she looks, moves the VRM look-at target there, and turns the head part of the way.
  updateGaze(add, dt, override) {
    const head = this.vrm.humanoid.getNormalizedBoneNode('head');
    const headPos = head.getWorldPosition(_v);
    let want;
    const thinking = this.state === 'thinking' || this.state === 'transcribing';
    if (thinking) {
      want = { yaw: THINK_LOOK.yaw * this.thinkSide, pitch: THINK_LOOK.pitch };
    } else if (this.state === 'idle' && this.cursor && this.time - this.cursorMovedAt < CURSOR_IDLE) {
      want = this.anglesTo(this.cursor, headPos);
    } else {
      // The camera is where the user is.
      const cam = this.stage.camera.position;
      want = { yaw: Math.atan2(cam.x - headPos.x, cam.z - headPos.z), pitch: Math.atan2(cam.y - headPos.y, cam.z - headPos.z) };
    }
    const glance = this.glanceWeight();
    if (glance > 0) {
      const at = this.anglesTo(this.glance, headPos);
      want = { yaw: want.yaw + (at.yaw - want.yaw) * glance, pitch: want.pitch + (at.pitch - want.pitch) * glance };
    }
    const pointAt = this.pointing.look(headPos);
    if (pointAt) {
      want = {
        yaw: want.yaw + (pointAt.yaw - want.yaw) * pointAt.weight,
        pitch: want.pitch + (pointAt.pitch - want.pitch) * pointAt.weight,
      };
    }
    if (override) {
      want = {
        yaw: want.yaw + (override.yaw - want.yaw) * override.weight,
        pitch: want.pitch + (override.pitch - want.pitch) * override.weight,
      };
    }
    // A tiny drift so a held gaze isn't laser-locked.
    want.yaw += 0.03 * this.noise.lookY.at(this.time * 0.3);
    want.pitch += 0.02 * this.noise.lookX.at(this.time * 0.3);

    if (Math.hypot(want.yaw - this.gaze.yaw, want.pitch - this.gaze.pitch) > 0.35 && Math.random() < 0.5) this.expressions.blinkNow();
    this.gaze = want;
    this.eyes.yaw = damp(this.eyes.yaw, want.yaw, 0.06, dt);
    this.eyes.pitch = damp(this.eyes.pitch, want.pitch, 0.06, dt);
    this.head.yaw = damp(this.head.yaw, want.yaw, 0.35, dt);
    this.head.pitch = damp(this.head.pitch, want.pitch, 0.35, dt);

    const yaw = clamp(this.head.yaw * HEAD_FOLLOW_YAW, -0.55, 0.55);
    const pitch = clamp(this.head.pitch * HEAD_FOLLOW_PITCH, -0.3, 0.3);
    addAngles(add, 'neck', -pitch * 0.4, yaw * 0.4, 0);
    addAngles(add, 'head', -pitch * 0.6, yaw * 0.6, 0);

    const { yaw: ey, pitch: ep } = this.eyes;
    this.lookTarget.position.set(
      headPos.x + Math.sin(ey) * Math.cos(ep) * LOOK_DISTANCE,
      headPos.y + Math.sin(ep) * LOOK_DISTANCE,
      headPos.z + Math.cos(ey) * Math.cos(ep) * LOOK_DISTANCE,
    );
  }

  // A spot on the screen (window px: the cursor, a window) is somewhere in front of her: map its offset from her
  // face in the window to a point LOOK_DISTANCE ahead.
  anglesTo(p, headPos) {
    const face = this.stage.toWindow(headPos);
    const ppm = this.stage.pixelsPerMeter;
    const dx = (p.x - face.x) / ppm;
    const dy = -(p.y - face.y) / ppm;
    return {
      yaw: clamp(Math.atan2(dx, LOOK_DISTANCE), -1, 1),
      pitch: clamp(Math.atan2(dy, Math.hypot(dx, LOOK_DISTANCE)), -0.5, 0.6),
    };
  }

  // Every so often, while idle, she does something small on her own: a look around, a stretch, a fidget (her hair,
  // her nails, a shoulder roll) or a shift of her weight; late at night, often a yawn. Sooner when she's perky.
  idleActions() {
    if (this.state !== 'idle' || this.time < this.nextIdleAction || this.gestures.active.length || this.pointing.active) return;
    const { sleepy, perky } = this.energy;
    const roll = Math.random();
    if (Math.random() < 0.45 * sleepy) this.gestures.play('yawn');
    else if (roll < 0.25) this.gestures.play('look_around');
    else if (roll < 0.42) this.gestures.play('stretch');
    else if (roll < 0.75) this.gestures.play(pick(FIDGETS));
    else this.weightSide = -this.weightSide;
    this.nextIdleAction = this.time + rand(15, 40) * (1 - 0.35 * perky);
  }

  applyPose(quats, add) {
    for (const n of this.names) {
      const q = this.bones[n].quaternion.copy(quats[n]);
      const a = add[n];
      if (a) {
        quatOf(a, 'XYZ', _q);
        if (this.preMultiply[n]) q.premultiply(_q);
        else q.multiply(_q);
      }
      if (this.flipAxes) q.set(-q.x, q.y, -q.z, q.w);
    }
  }
}

function addAngles(add, bone, x, y, z) {
  const a = add[bone] || (add[bone] = [0, 0, 0]);
  a[0] += x;
  a[1] += y;
  a[2] += z;
}
