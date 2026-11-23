// Gestures: short movements layered over the state's base pose.
//
// Each gesture returns, for a time t since it started, an optional absolute `pose` (bones blended from
// the base pose toward it by the gesture's weight), optional `add` offsets (Euler angles added on top,
// also scaled by the weight) and an optional `look` (yaw/pitch in radians the gaze should turn toward).
// The player fades every gesture in and out so nothing ever snaps.

import { ARM, HAND, hand, limb, merge, pointingHand } from './poses.js';
import { bump, clamp, damp, envelope, lerp, pick, rand, smoothstep, softBump } from './math.js';

const other = (side) => (side === 'left' ? 'right' : 'left');

// Adds Euler offsets for a bone into an `add` map.
function addTo(add, bone, x, y, z) {
  const a = add[bone] || (add[bone] = [0, 0, 0]);
  a[0] += x;
  a[1] += y;
  a[2] += z;
}

// Shoulders (clavicles) up by `amount` on both sides.
function shouldersUp(add, amount) {
  addTo(add, 'leftShoulder', 0, 0, amount);
  addTo(add, 'rightShoulder', 0, 0, -amount);
}

// Blends two left-side arm poses: t = 0 gives `a`, 1 gives `b`.
function mixArm(a, b, t) {
  const out = {};
  for (const part of Object.keys(a)) out[part] = a[part].map((v, i) => lerp(v, b[part][i], t));
  return out;
}

// Interpolates between keyed arm poses [[time, armPose], ...] with eased segments.
function keyedArm(keys, t) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [t1, p1] = keys[i];
    if (t <= t1) {
      const [t0, p0] = keys[i - 1];
      return mixArm(p0, p1, smoothstep((t - t0) / (t1 - t0)));
    }
  }
  return keys[keys.length - 1][1];
}

// A wave with the given hand: arm up, forearm rocking side to side from the elbow.
function wave(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, { ...ARM.wave, Shoulder: [0, 0, 0.1] }), hand(s, HAND.open));
  const add = {};
  const rock = Math.sin(2 * Math.PI * g.rate * t);
  addTo(add, `${s}LowerArm`, 0, -0.32 * rock * sign, 0);
  addTo(add, `${s}Hand`, 0, 0, 0.12 * Math.sin(2 * Math.PI * g.rate * t - 0.8) * sign);
  // A friendly tilt of the head toward the waving hand and a little lean into it.
  addTo(add, 'head', 0, 0.04 * sign, -0.1 * sign);
  addTo(add, 'spine', 0, 0, -0.03 * sign);
  return { pose, add };
}

function nod(g, t) {
  const add = {};
  // Two dips, the second smaller, like a natural "mm-hm", each easing in and out so the head doesn't bounce
  // off the top between them.
  const d = softBump(t, 0.02, 0.5) + 0.6 * softBump(t, 0.4, 0.86);
  addTo(add, 'head', 0.2 * g.amount * d, 0, 0);
  addTo(add, 'neck', 0.07 * g.amount * d, 0, 0);
  return { add };
}

function shake(g, t) {
  const add = {};
  const decay = 1 - smoothstep(t / g.duration);
  const y = 0.2 * g.amount * Math.sin(2 * Math.PI * 2.4 * t) * (0.4 + 0.6 * decay);
  addTo(add, 'head', 0.03, y, 0);
  addTo(add, 'neck', 0, y * 0.3, 0);
  return { add };
}

function shrug(g, t) {
  const pose = merge(limb('left', ARM.shrug), limb('right', ARM.shrug), hand('left', HAND.open), hand('right', HAND.open));
  const add = {};
  const up = bump(t, 0.1, 1.2);
  shouldersUp(add, 0.2 * up);
  addTo(add, 'head', 0.03, 0, 0.12 * g.tilt * up);
  addTo(add, 'neck', -0.04 * up, 0, 0);
  return { pose, add };
}

// A laugh's "ha"s as [start, length, strength]: each a soft pulse, overlapping the next a little so they run
// together, coming slower and weaker as the laugh runs out, the way a real one does.
const HAS = [
  [0.12, 0.27, 1],
  [0.36, 0.28, 0.85],
  [0.61, 0.3, 0.68],
  [0.88, 0.33, 0.5],
  [1.18, 0.36, 0.3],
];

// A laugh: it takes her with a lift of the chest and her head tipping back and to one side, her shoulders
// shaking softly with each "ha", then she comes forward out of it with a little nod as it settles. Every part
// eases in and out, so it reads as one laugh rather than a shudder. One hand comes up loosely under her chin
// (`side`), the other stays down at her side.
function laugh(g, t) {
  const pose = merge(limb(g.side, ARM.giggle), hand(g.side, HAND.soft));
  const add = {};
  const d = g.duration;
  const back = smoothstep(t / 0.35) * (1 - smoothstep((t - (d - 0.95)) / 0.6));
  const settle = softBump(t, d - 0.85, d - 0.1);
  let ha = 0;
  for (const [start, length, strength] of HAS) ha += strength * softBump(t, start, start + length);
  // Each "ha" squeezes the chest a little forward and lifts the shoulders; the head dips with it, a touch.
  shouldersUp(add, 0.02 * back + 0.055 * ha);
  addTo(add, 'spine', -0.03 * back + 0.015 * ha, 0, 0);
  addTo(add, 'chest', -0.04 * back + 0.03 * ha, 0, 0);
  addTo(add, 'neck', -0.03 * back, 0, 0);
  addTo(add, 'head', -0.12 * back + 0.025 * ha + 0.06 * settle, 0, 0.07 * g.tilt * back);
  addTo(add, 'leftUpperArm', 0, 0, 0.03 * ha);
  addTo(add, 'rightUpperArm', 0, 0, -0.03 * ha);
  return { pose, add };
}

function fistPump(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const arm = keyedArm(
    [
      [0, ARM.fistDown],
      [0.3, ARM.fistUp],
      [0.55, ARM.fistDown],
      [0.8, ARM.fistUp],
      [1.1, ARM.fistDown],
    ],
    t,
  );
  const pose = merge(limb(s, { ...arm, Shoulder: [0, 0, 0.05] }), hand(s, HAND.fist));
  const add = {};
  const pump = softBump(t, 0.25, 0.65) + softBump(t, 0.75, 1.2);
  addTo(add, 'spine', 0.04 * pump, 0.05 * sign, 0);
  addTo(add, 'head', 0.06 * pump, 0, 0);
  return { pose, add };
}

function handsOnHips() {
  const pose = merge(limb('left', ARM.hip), limb('right', ARM.hip), hand('left', HAND.relaxed), hand('right', HAND.relaxed));
  const add = {};
  addTo(add, 'chest', -0.05, 0, 0);
  addTo(add, 'head', -0.03, 0, 0.06);
  return { pose, add };
}

function surprisedJolt(g, t) {
  const add = {};
  // A fast start, then a slow settle: shock first, then relaxing out of it.
  const jolt = t < 0.12 ? smoothstep(t / 0.12) : 1 - 0.55 * smoothstep((t - 0.12) / 0.9);
  shouldersUp(add, 0.14 * jolt);
  addTo(add, 'spine', -0.07 * jolt, 0, 0);
  addTo(add, 'head', -0.12 * jolt, 0, 0);
  addTo(add, 'leftUpperArm', 0, 0, 0.12 * jolt);
  addTo(add, 'rightUpperArm', 0, 0, -0.12 * jolt);
  addTo(add, 'leftLowerArm', 0, -0.25 * jolt, 0);
  addTo(add, 'rightLowerArm', 0, 0.25 * jolt, 0);
  return { add };
}

function sigh(g, t) {
  const add = {};
  const inhale = bump(t, 0, 0.8);
  const exhale = smoothstep((t - 0.6) / 0.9);
  shouldersUp(add, 0.1 * inhale - 0.08 * exhale);
  addTo(add, 'chest', -0.05 * inhale + 0.07 * exhale, 0, 0);
  addTo(add, 'head', 0.14 * exhale, 0, 0.05 * g.tilt * exhale);
  return { add };
}

function thinkChin(g) {
  const pose = merge(limb(other(g.side), ARM.across), limb(g.side, ARM.chin), hand('left', HAND.soft), hand('right', HAND.soft));
  const add = {};
  addTo(add, 'head', -0.05, 0, 0.1 * (g.side === 'right' ? 1 : -1));
  return { pose, add, look: { yaw: 0.35 * (g.side === 'right' ? 1 : -1), pitch: 0.3 } };
}

// When the camera gesture presses the shutter (s after it starts): the flash goes off then.
export const CAMERA_SHUTTER_S = 0.3;

// Taking a picture. With a camera in her hand (`held`: the prop's grip closes that hand) she holds it up
// in front of her face, lens toward the screen, and steadies it from below with the other palm; without
// one, both hands frame an imaginary camera. The holding hand's index finger presses the shutter (with a
// small recoil of the head), then the camera comes down.
function camera(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = g.held
    ? merge(limb(s, ARM.cameraHold), limb(other(s), ARM.cameraSupport), hand(s, HAND.soft), hand(other(s), HAND.cradle))
    : merge(limb('left', ARM.camera), limb('right', ARM.camera), hand('left', HAND.soft), hand('right', HAND.soft));
  const add = {};
  const press = bump(t, g.shutter - 0.08, g.shutter + 0.12);
  // Curling toward the palm is -z on the left, +z on the mirrored right.
  addTo(add, `${s}IndexProximal`, 0, 0, -0.5 * press * sign);
  addTo(add, `${s}IndexIntermediate`, 0, 0, -0.4 * press * sign);
  // The camera's grip leaves the thumb sticking up in front of her face: fold it down onto the top.
  if (g.held) for (const [bone, a] of Object.entries(hand(s, THUMB_DOWN))) if (bone.includes('Thumb')) addTo(add, bone, ...a);
  addTo(add, 'head', 0.06 - 0.03 * press, -0.04 * sign, -0.06 * sign);
  addTo(add, 'spine', 0.03, 0, 0);
  // A real camera: she frames the shot on its screen; an imaginary one: she looks through it at you.
  return { pose, add, look: g.held ? CAMERA_LOOK : { yaw: 0, pitch: 0 } };
}
const CAMERA_LOOK = { yaw: 0, pitch: -0.2 };
const THUMB_DOWN = { curl: 0, thumb: 0.6 }; // only its thumb is used: a fold added on top of the grip

// The `search` activity: studying the screen through a magnifying glass held up in front of her eye (the
// holding hand's side), peering out through it with her head tilted to it, slowly looking across the
// screen, the other hand cupping the raised elbow. Without a magnifier (`held` false) she rests her chin
// on that hand instead. Held until the activity ends.
function inspect(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = g.held
    ? merge(limb(s, ARM.magnifier), limb(other(s), ARM.underElbow), hand(s, HAND.soft), hand(other(s), HAND.soft))
    : merge(limb(s, ARM.chin), limb(other(s), ARM.across), hand(s, HAND.soft), hand(other(s), HAND.soft));
  const add = {};
  // Looking across the screen: head and eyes drift together (y > 0 turns both to her left), now and then
  // leaning in closer; the head tilts toward the glass. The activity keeps the glass in front of her eye
  // as the head moves (Activities.apply).
  const scan = Math.sin((2 * Math.PI * t) / g.period + g.phase);
  const closer = smoothstep((Math.sin((2 * Math.PI * t) / (g.period * 1.7) + 1.3) - 0.3) / 0.7);
  addTo(add, 'head', 0.03 + 0.04 * closer, 0.1 * scan, -0.06 * sign);
  addTo(add, 'neck', 0.02 * closer, 0.04 * scan, 0);
  addTo(add, 'spine', 0.04 * closer, 0, 0);
  return { pose, add, look: { yaw: 0.22 * scan, pitch: 0.02 } };
}

// ---- your tasks, timers and reminders, with their props (activities.js) ----

// The `tasks` activity: reading a clipboard held up in front of her chest in the holding hand (`side`), facing
// out so you can read it too, the other hand on her hip. Her eyes run down the list, and every `period`
// seconds come up to you for a moment. Held until the activity ends.
function clipboardRead(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.clipboard), limb(other(s), ARM.hip), hand(s, HAND.soft), hand(other(s), HAND.relaxed));
  const add = {};
  const cycle = (t + g.phase) % g.period;
  const glance = bump(cycle, g.period - 1.4, g.period);
  const down = smoothstep(cycle / (g.period - 1.4));
  const reading = 1 - glance;
  addTo(add, 'head', (0.08 + 0.05 * down) * reading, 0.1 * sign * reading, 0.06 * sign);
  addTo(add, 'neck', 0.03 * reading, 0, 0);
  return { pose, add, look: { yaw: 0.3 * sign * reading, pitch: lerp(-0.18, -0.36, down) * reading } };
}

// When the tick moment starts drawing its tick in the air, and how long each stroke takes: the clipboard's own
// tick draws in over the same time (activities.js acts on the prop then).
export const TICK_AT_S = 0.4;
const TICK_STROKES = [0.12, 0.2];
// The free hand's path while drawing, as offsets to that upper arm, [y, z] for the left arm (mirrored for the
// right): short and down toward her middle, then long and up past where it started, the way a tick looks to you.
// Small: her fingertip starts at a box on the clipboard (ARM.airTick), and the tick is the size of one.
const TICK_PATH = [[0, 0], [-0.04, -0.05], [-0.1, 0.07]];

// A task done: her free hand comes over, a finger out, and draws a tick in the next box of the clipboard (still held up
// in `side`) as it appears there, then she nods, pleased.
function tick(g, t) {
  const s = g.side;
  const free = other(s);
  const fs = free === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.clipboard), limb(free, ARM.airTick), hand(s, HAND.soft), pointingHand(free));
  const add = {};
  const [first, second] = TICK_STROKES;
  const p1 = smoothstep((t - TICK_AT_S) / first);
  const p2 = smoothstep((t - TICK_AT_S - first) / second);
  const [a, b, c] = TICK_PATH;
  const y = lerp(lerp(a[0], b[0], p1), c[0], p2);
  const z = lerp(lerp(a[1], b[1], p1), c[1], p2);
  addTo(add, `${free}UpperArm`, 0, fs * y, fs * z);
  const done = TICK_AT_S + first + second;
  const nod = softBump(t, done, done + 0.5);
  addTo(add, 'head', 0.16 * nod, 0, 0.05 * fs);
  addTo(add, 'neck', 0.05 * nod, 0, 0);
  // Watching the clipboard while she draws, then back to you.
  const watch = 1 - smoothstep((t - done) / 0.25);
  return { pose, add, look: { yaw: -0.25 * fs * watch, pitch: -0.2 * watch } };
}

// When the stopwatch moments press the crown (s after they start): the prop acts then.
export const STOPWATCH_PRESS_S = 0.45;
const THUMB_PRESS = { curl: 0, thumb: 0.9 }; // only its thumb: a press added on top of the grip

// A timer set: she holds a stopwatch up by her shoulder, face to you, glances at it as her thumb clicks it on,
// and nods at you. `side`: the hand with the stopwatch (without one she mimes it).
function stopwatchStart(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.stopwatch), limb(other(s), ARM.hip), hand(s, HAND.soft), hand(other(s), HAND.relaxed));
  const add = {};
  const press = bump(t, STOPWATCH_PRESS_S - 0.08, STOPWATCH_PRESS_S + 0.14);
  for (const [bone, [x, y, z]] of Object.entries(hand(s, THUMB_PRESS))) {
    if (bone.includes('Thumb')) addTo(add, bone, x * press, y * press, z * press);
  }
  const nod = softBump(t, STOPWATCH_PRESS_S + 0.15, STOPWATCH_PRESS_S + 0.65);
  addTo(add, 'head', 0.12 * nod, 0, -0.08 * sign);
  // At the watch for the click, then back to you.
  const watch = bump(t, 0.15, STOPWATCH_PRESS_S + 0.35);
  return { pose, add, look: { yaw: 0.35 * sign * watch, pitch: 0.12 * watch } };
}

// A timer going off: the stopwatch held up high beside her head, ringing and shaking in her hand, her arm
// jiggling with it. `side`: the hand with the stopwatch.
function stopwatchRing(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const arm = mixArm(ARM.stopwatch, ARM.stopwatchUp, smoothstep(t / 0.35));
  const pose = merge(limb(s, arm), limb(other(s), ARM.hip), hand(s, HAND.soft), hand(other(s), HAND.relaxed));
  const add = {};
  // The stopwatch itself rattles (its rig); her hand only trembles with it, and her arm sways gently, rather
  // than the whole arm buzzing.
  const ringing = smoothstep((t - STOPWATCH_PRESS_S) / 0.15) * (1 - smoothstep((t - STOPWATCH_PRESS_S) / 1.3));
  addTo(add, `${s}UpperArm`, 0, 0, 0.03 * sign * ringing * Math.sin(2 * Math.PI * 2.2 * t));
  addTo(add, `${s}Hand`, 0, 0.05 * ringing * Math.sin(2 * Math.PI * 7 * t), 0);
  addTo(add, 'head', -0.04, 0.05 * sign, -0.06 * sign);
  return { pose, add };
}

// When the bell starts ringing (s after the moment starts), and how fast it swings.
export const BELL_AT_S = 0.35;
const BELL_HZ = 3.4;

// A reminder: she lifts a hand bell to her shoulder and rings it, the bell swinging side to side from her
// wrist while its clapper (the prop's rig) swings the other way. `side`: the hand with the bell.
function ringBell(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.bell), limb(other(s), ARM.hip), hand(s, HAND.fist), hand(other(s), HAND.relaxed));
  const add = {};
  const ringing = t > BELL_AT_S ? 1 - smoothstep((t - BELL_AT_S) / 1.2) : 0;
  const swing = ringing && ringing * Math.sin(2 * Math.PI * BELL_HZ * (t - BELL_AT_S));
  addTo(add, `${s}Hand`, 0, 0.32 * swing * sign, 0);
  addTo(add, `${s}LowerArm`, 0.12 * swing, 0, 0);
  addTo(add, 'head', 0.03, 0.06 * sign, -0.08 * sign);
  return { pose, add };
}

// When the sonic rainboom goes off (s after the moment starts): at the top of her burst.
export const RAINBOOM_AT_S = 0.32;

// Something big got done: a quick crouch, arms pulled in, then she bursts open, fists flung up and out, chin up,
// as the rainboom goes off.
function rainboom(g, t) {
  const burst = smoothstep((t - RAINBOOM_AT_S + 0.08) / 0.16);
  const arm = mixArm(ARM.fistDown, ARM.burst, burst);
  const pose = merge(limb('left', arm), limb('right', arm), hand('left', HAND.fist), hand('right', HAND.fist));
  const add = {};
  const crouch = smoothstep(t / 0.2) * (1 - burst);
  shouldersUp(add, 0.1 * crouch - 0.06 * burst);
  addTo(add, 'spine', 0.12 * crouch - 0.08 * burst, 0, 0);
  addTo(add, 'chest', 0.06 * crouch - 0.08 * burst, 0, 0);
  addTo(add, 'head', 0.1 * crouch - 0.16 * burst, 0, 0);
  return { pose, add, look: { yaw: 0, pitch: 0.15 * burst } };
}

// ---- the web, your files, the morning, builds, music, shouting and the weather ----

// The `web` activity: a little globe on her upturned palm (in `side`'s hand), turning, and the other hand reaching
// over to spin it every `every` seconds, her eyes on it and now and then on you. Held until the activity ends.
function globeSpin(g, t) {
  const s = g.side;
  const free = other(s);
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.globe), limb(free, ARM.globeSpin), hand(s, HAND.open), pointingHand(free));
  const add = {};
  // The flick: the finger sweeps across the globe, then eases back.
  const cycle = (t + g.phase) % g.every;
  const flick = bump(cycle, 0, 0.35);
  addTo(add, `${free}UpperArm`, 0, 0.1 * flick * (free === 'left' ? 1 : -1), 0);
  addTo(add, `${free}Hand`, 0, 0, 0.25 * flick * (free === 'left' ? -1 : 1));
  const glance = bump(cycle, g.every - 1.4, g.every);
  return { pose, add, look: { yaw: 0.3 * sign * (1 - glance), pitch: 0.05 * (1 - glance) } };
}

// The `morning` activity: a mug of hot chocolate held in front of her chest by its handle (in `side`'s hand), the
// other hand on her hip; whenever she's been quiet a moment (`quiet()`, from the lip sync), she lifts it for a
// sip, and it comes down the moment she talks. Held until the activity ends.
function mugHold(g, t) {
  const s = g.side;
  const dt = Math.max(0, t - (g.last ?? t));
  g.last = t;
  g.quietFor = (g.quiet ? g.quiet() : true) ? (g.quietFor ?? 0) + dt : 0;
  const sipping = g.quietFor > 0.6 && (t + g.phase) % g.every < 1.9;
  g.sip = damp(g.sip ?? 0, sipping ? 1 : 0, sipping ? 0.28 : 0.1, dt);
  const sip = smoothstep(g.sip);
  const pose = merge(limb(s, mixArm(ARM.mug, ARM.mugSip, sip)), limb(other(s), ARM.hip), hand(s, HAND.soft), hand(other(s), HAND.relaxed));
  const add = {};
  addTo(add, 'head', -0.06 * sip, 0, 0);
  return { pose, add, look: sip > 0.5 ? { yaw: 0, pitch: -0.1 } : undefined };
}

// When the wrench twirls (s after its moment starts): the prop's rig spins it then.
export const WRENCH_AT_S = 0.3;

// A build of hers under way: she brings a wrench up, twirls it like a baton (the prop's rig), catches it and holds
// it up, pleased with herself, chin up. `side`: the hand with the wrench.
function wrenchTwirl(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.wrench), limb(other(s), ARM.hip), hand(s, HAND.fist), hand(other(s), HAND.relaxed));
  const add = {};
  const proud = smoothstep((t - WRENCH_AT_S - 0.7) / 0.3);
  const catchIt = bump(t, WRENCH_AT_S + 0.55, WRENCH_AT_S + 0.95);
  addTo(add, `${s}UpperArm`, 0, 0, -0.06 * sign * catchIt);
  addTo(add, 'head', -0.06 * proud, 0.05 * sign * proud, -0.05 * sign * proud);
  const watch = 1 - proud;
  return { pose, add, look: { yaw: 0.3 * sign * watch, pitch: 0.2 * watch } };
}

// When the headphones go on (s after the music moment starts: her hands are at her ears) and come off again.
export const HEADPHONES_ON_S = 0.35;
const HEADPHONES_OFF_S = 3.6;

// Music: both hands up to her ears to slide headphones on (they're worn: the prop sits on her head), then a
// laid-back groove, cool rather than bouncy: chin a little up, a small soft nod on each beat (sin², so it eases in
// and out of the dip instead of bouncing off it), and a slow sway of her head, side to side and tilting, once a bar,
// until her hands go up again to take them off.
function headphonesOn(g, t) {
  const up = Math.max(1 - smoothstep((t - HEADPHONES_ON_S) / 0.35), smoothstep((t - HEADPHONES_OFF_S) / 0.3));
  const pose = merge(limb('left', mixArm(ARM.relaxed, ARM.ears, up)), limb('right', mixArm(ARM.relaxed, ARM.ears, up)), hand('left', HAND.soft), hand('right', HAND.soft));
  const add = {};
  const grooving = smoothstep((t - HEADPHONES_ON_S - 0.2) / 0.3) * (1 - smoothstep((t - HEADPHONES_OFF_S + 0.2) / 0.3));
  const beat = Math.sin((Math.PI * g.bpm) / 60 * t) ** 2;
  const bar = (2 * Math.PI * g.bpm) / 240 * t; // four beats to a sway
  addTo(add, 'head', (0.06 * beat - 0.05) * grooving, 0.035 * Math.sin(bar) * grooving, 0.04 * Math.sin(bar + 0.6) * grooving);
  addTo(add, 'neck', 0.02 * beat * grooving, 0, 0);
  shouldersUp(add, 0.012 * beat * grooving);
  addTo(add, 'spine', 0, 0.025 * Math.sin(bar - 0.8) * grooving, 0); // her shoulders half a beat behind her head
  return { pose, add };
}

// When the sunglasses appear (s after the moment starts): her hand is at her temple.
export const SHADES_ON_S = 0.3;

// A sunny day: her hand comes up to her temple and slides sunglasses on (worn), pushes them up her nose with a
// finger, and the hand drops to her hip: deal with it. `side`: the hand she uses.
function shades(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const up = 1 - smoothstep((t - SHADES_ON_S - 0.35) / 0.35);
  const pose = merge(limb(s, mixArm(ARM.hip, ARM.temple, up)), limb(other(s), ARM.hip), up > 0.5 ? pointingHand(s) : hand(s, HAND.relaxed), hand(other(s), HAND.relaxed));
  const add = {};
  const push = bump(t, SHADES_ON_S + 0.05, SHADES_ON_S + 0.3);
  addTo(add, `${s}UpperArm`, 0, 0, 0.08 * push * sign);
  addTo(add, 'head', -0.08 * (1 - up), -0.04 * sign * (1 - up), 0.05 * sign * (1 - up));
  return { pose, add };
}

// A shout: a megaphone up to her mouth, leaning into it, the other hand on her hip. `side`: the hand with it.
function megaphoneShout(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.megaphone), limb(other(s), ARM.hip), hand(s, HAND.fist), hand(other(s), HAND.relaxed));
  const add = {};
  const lean = smoothstep(t / 0.4);
  addTo(add, 'spine', 0.05 * lean, 0.06 * sign * lean, 0);
  addTo(add, 'head', -0.05 * lean, 0.05 * sign * lean, 0);
  return { pose, add };
}

// When the umbrella pops open (s after its moment starts): up over her shoulder by then.
export const UMBRELLA_AT_S = 0.35;

// Rain: an umbrella up over her shoulder, popping open (the prop's rig), a little bounce, head tilted under it.
// `side`: the hand with it.
function umbrellaUp(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.umbrella), limb(other(s), ARM.hip), hand(s, HAND.fist), hand(other(s), HAND.relaxed));
  const add = {};
  const bounce = softBump(t, UMBRELLA_AT_S - 0.05, UMBRELLA_AT_S + 0.45);
  shouldersUp(add, 0.05 * bounce);
  addTo(add, 'head', 0.03, 0.04 * sign, -0.1 * sign);
  return { pose, add, look: { yaw: 0.15 * sign, pitch: 0.15 * bump(t, 0.2, 1.4) } };
}

// Sitting on top of a window (the 'perch' layout, app.js): thighs forward over its edge, lower legs hanging down in
// front of it, swinging her feet a little, one then the other, leaning back a touch. Legs only: her arms are free
// for anything else. Held until she flies off.
const PERCH_SWING_HZ = 0.4;
function perch(g, t) {
  const pose = merge(
    limb('left', { UpperLeg: [-1.5, -0.1, 0.12], LowerLeg: [1.3, 0, 0], Foot: [0.4, 0, 0] }),
    limb('right', { UpperLeg: [-1.4, -0.1, 0.1], LowerLeg: [1.55, 0, 0], Foot: [0.3, 0, 0] }),
  );
  const add = {};
  const swing = 2 * Math.PI * PERCH_SWING_HZ * t + g.phase;
  addTo(add, 'leftLowerLeg', 0.16 * Math.sin(swing), 0, 0);
  addTo(add, 'rightLowerLeg', 0.16 * Math.sin(swing + 2.5), 0, 0);
  addTo(add, 'spine', -0.04, 0, 0);
  return { pose, add };
}

// Napping on her cloud (nap.js): leaning forward, arms folded on the cloud in front of her, her head resting on
// them, turned a little your way; slow, deep breaths. Held until she wakes.
function nap(g, t) {
  const pose = merge(limb('left', ARM.fold), limb('right', ARM.foldUnder), hand('left', HAND.relaxed), hand('right', HAND.relaxed));
  const add = {};
  const breath = Math.sin(2 * Math.PI * 0.2 * t);
  addTo(add, 'spine', 0.24, 0, 0);
  addTo(add, 'chest', 0.12 - 0.025 * breath, 0, 0);
  addTo(add, 'neck', 0.1, 0, 0);
  addTo(add, 'head', 0.12, 0.2, 0.42);
  shouldersUp(add, 0.03 * breath);
  return { pose, add, look: { yaw: 0.1, pitch: -0.1 } };
}

// ---- showing off ----

// A show-off spin: arms tucked in, a full turn on the spot (the whole body, hair flying), and a finger gun at you
// to finish, the other hand on her hip.
export const SPIN_S = [0.12, 0.72]; // when the turn starts and how long it takes
function spin(g, t) {
  const [start, length] = SPIN_S;
  const p = clamp((t - start) / length, 0, 1);
  const done = p >= 1;
  const s = g.side;
  const finish = smoothstep((t - start - length) / 0.2);
  const pose = merge(
    limb(s, mixArm(ARM.fistDown, ARM.engaged, finish)),
    limb(other(s), mixArm(ARM.fistDown, ARM.hip, finish)),
    finish > 0.5 ? pointingHand(s) : hand(s, HAND.fist),
    hand(other(s), finish > 0.5 ? HAND.relaxed : HAND.fist),
  );
  const add = {};
  // A whole turn is no turn at all: once it's done it's back to 0, so the fade-out doesn't unwind it.
  const turn = done ? 0 : 2 * Math.PI * (p < 0.5 ? 2 * p * p : 1 - 2 * (1 - p) * (1 - p));
  addTo(add, 'hips', 0, turn * (g.side === 'left' ? -1 : 1), 0);
  addTo(add, 'head', -0.05 * finish, 0, 0.08 * finish * (s === 'left' ? -1 : 1));
  return { pose, add };
}

// Waiting on something slow: arms crossed, a finger drumming on her arm, a foot tapping, a glance up and away.
function impatient(g, t) {
  const pose = merge(limb('left', ARM.crossed), limb('right', ARM.crossed), hand('left', HAND.soft), hand('right', HAND.soft));
  const add = {};
  // Squared, so finger and foot ease off and back down rather than snapping at each turn.
  const drum = Math.max(0, Math.sin(2 * Math.PI * 3.2 * t)) ** 2;
  addTo(add, 'rightIndexProximal', 0, 0, 0.5 * drum);
  addTo(add, 'rightIndexIntermediate', 0, 0, 0.3 * drum);
  const tap = Math.max(0, Math.sin(2 * Math.PI * 1.8 * t)) ** 2;
  addTo(add, 'leftFoot', -0.35 * tap, 0, 0);
  addTo(add, 'leftLowerLeg', 0.12 * tap, 0, 0);
  addTo(add, 'head', 0.04, 0.12 * g.away, 0.06 * g.away);
  return { pose, add, look: { yaw: 0.4 * g.away, pitch: 0.25 } };
}

// Peeking in from the side of her window (presence.js): leaning in toward the middle, head tilted, then
// straightening up as she slides the rest of the way in. `lean` 1: she's come from her left, so leans to her right.
function peek(g, t) {
  const add = {};
  const lean = smoothstep(t / 0.3) * (1 - smoothstep((t - 0.9) / 0.4));
  addTo(add, 'spine', 0, 0, 0.12 * g.lean * lean);
  addTo(add, 'chest', 0, 0, 0.08 * g.lean * lean);
  addTo(add, 'head', 0, 0, 0.2 * g.lean * lean);
  return { add };
}

// ---- idle actions: the small things people do when nothing is happening ----

function lookAround(g, t) {
  const i = Math.min(g.glances.length - 1, Math.floor(t / (g.duration / g.glances.length)));
  return { look: g.glances[i] };
}

// Tucking her hair behind her ear: the hand comes up by her cheek and sweeps back, her head tilting into it.
function hairTuck(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const up = bump(t, 0, g.duration);
  const pose = merge(limb(s, ARM.tuck), hand(s, HAND.soft));
  const add = {};
  const sweep = smoothstep((t - 0.45) / 0.5);
  addTo(add, `${s}UpperArm`, 0, -0.25 * sweep * sign, 0);
  addTo(add, `${s}Hand`, 0, 0, 0.3 * sweep * sign);
  addTo(add, 'head', 0, 0, -0.1 * sign * up);
  return { pose, add, look: { yaw: -0.15 * sign, pitch: -0.05 } };
}

// Checking her nails: a hand up in front of her, fingers curled toward her, a look at them, then a little brush on
// her collar.
function checkNails(g, t) {
  const s = g.side;
  const sign = s === 'left' ? 1 : -1;
  const pose = merge(limb(s, ARM.nails), hand(s, HAND.soft));
  const add = {};
  const brush = bump(t, 1.2, 1.7);
  addTo(add, `${s}UpperArm`, 0, 0.2 * brush * sign, 0.1 * brush * sign);
  return { pose, add, look: { yaw: 0.25 * sign, pitch: -0.3 } };
}

// Rolling her shoulders back, then a stretch of the neck to one side.
function shoulderRoll(g, t) {
  const add = {};
  const a = (2 * Math.PI * t) / 0.9;
  const rolling = bump(t, 0, 1.8);
  shouldersUp(add, 0.1 * Math.max(0, Math.sin(a)) ** 2 * rolling);
  addTo(add, 'chest', -0.05 * Math.cos(a) * rolling, 0, 0);
  addTo(add, 'upperChest', -0.03 * Math.cos(a) * rolling, 0, 0);
  const neck = bump(t, 1.2, g.duration);
  addTo(add, 'neck', 0, 0, 0.18 * g.tilt * neck);
  addTo(add, 'head', 0.05 * neck, 0, 0.12 * g.tilt * neck);
  return { add };
}

// A yawn behind her hand: head back, shoulders up, eyes squeezed shut.
function yawn(g, t) {
  const s = g.side;
  const pose = merge(limb(s, ARM.yawn), hand(s, HAND.soft));
  const add = {};
  const open = bump(t, 0.2, g.duration - 0.3);
  shouldersUp(add, 0.1 * open);
  addTo(add, 'head', -0.18 * open, 0, 0.06 * (s === 'left' ? -1 : 1) * open);
  addTo(add, 'neck', -0.06 * open, 0, 0);
  addTo(add, 'chest', -0.04 * open, 0, 0);
  return { pose, add };
}

function stretch(g, t) {
  const up = bump(t, 0, g.duration);
  const arm = { UpperArm: [-0.4, 0.45, -1.3], LowerArm: [-0.3, -0.05, 0], Hand: [0, 0, 0.35] };
  const pose = merge(limb('left', arm), limb('right', arm), hand('left', HAND.open), hand('right', HAND.open));
  const add = {};
  addTo(add, 'chest', -0.08 * up, 0, 0);
  addTo(add, 'head', -0.12 * up, 0, 0.04);
  shouldersUp(add, 0.05 * up);
  return { pose, add };
}

// ---- talking hands ----

// An open-palm beat with one or both hands: the "you see?" of conversation. Every beat picks its own
// hands, reach and timing so a long answer never loops the same motion.
function beat(g) {
  // From her sides up toward palm up (not from hands held out in front, which looked stiff).
  const reach = mixArm(ARM.talk, ARM.palmUp, g.reach);
  const open = { ...HAND.open, curl: lerp(0.3, 0.08, g.reach) };
  let pose;
  if (g.side === 'both') {
    const wide = mixArm(reach, ARM.shrug, g.spread);
    pose = merge(limb('left', wide), limb('right', wide), hand('left', open), hand('right', open));
  } else {
    pose = merge(limb(g.side, reach), hand(g.side, open));
  }
  return { pose };
}

// name -> { duration, fadeIn, fadeOut (s), arms: whether it needs the arms (talking beats and other arm
// gestures make way), bothHands: needs both hands (a pointing arm comes down meanwhile), sustain: held
// until the sentence that asked for it ends (with its duration as a safety cap), calm: slow enough for the
// renderer's half frame rate (a nap), under: an activity's
// held pose that other arm gestures play over instead of ending it (reading a clipboard: a tick or a wave
// on top, and back to reading with nothing in between), make: per-instance parameters, sample }
const DEFS = {
  wave: { duration: 2.2, fadeIn: 0.35, fadeOut: 0.5, arms: true, make: () => ({ side: 'right', rate: rand(2.0, 2.4) }), sample: wave },
  nod: { duration: 0.9, fadeIn: 0.05, fadeOut: 0.1, arms: false, make: () => ({ amount: rand(0.8, 1.1) }), sample: nod },
  shake: { duration: 1.2, fadeIn: 0.15, fadeOut: 0.3, arms: false, make: () => ({ amount: rand(0.8, 1.1) }), sample: shake },
  shrug: { duration: 1.5, fadeIn: 0.3, fadeOut: 0.45, arms: true, make: () => ({ tilt: pick([-1, 1]) }), sample: shrug },
  laugh: {
    duration: 2.3,
    fadeIn: 0.4,
    fadeOut: 0.6,
    arms: true,
    make: () => ({ tilt: pick([-1, 1]), side: pick(['left', 'right']) }),
    sample: laugh,
  },
  fist_pump: { duration: 1.6, fadeIn: 0.3, fadeOut: 0.45, arms: true, make: () => ({ side: 'right' }), sample: fistPump },
  // Held for as long as the sentence plays; released by the animator (with a safety cap).
  hands_on_hips: { duration: 8, fadeIn: 0.5, fadeOut: 0.5, arms: true, sustain: true, make: () => ({}), sample: handsOnHips },
  surprised_jolt: { duration: 1.3, fadeIn: 0.04, fadeOut: 0.6, arms: false, make: () => ({}), sample: surprisedJolt },
  sigh: { duration: 2.2, fadeIn: 0.3, fadeOut: 0.6, arms: false, make: () => ({ tilt: pick([-1, 1]) }), sample: sigh },
  think_chin: { duration: 3.2, fadeIn: 0.55, fadeOut: 0.6, arms: true, make: () => ({ side: 'right' }), sample: thinkChin },
  // Played by the "snapshot" message (the assistant takes a screenshot), not by speech; `side` is the hand
  // with the camera, `held` whether there's a real one in it.
  camera: {
    duration: 1.3,
    fadeIn: 0.2,
    fadeOut: 0.45,
    arms: true,
    bothHands: true,
    make: () => ({ shutter: CAMERA_SHUTTER_S, side: 'right', held: false }),
    sample: camera,
  },
  // The `search` activity's pose; `side` is the hand with the magnifier. Held until the activity releases
  // it (activities.js), through sentences and state changes; the long duration is only a safety cap.
  inspect: {
    duration: 90,
    fadeIn: 0.5,
    fadeOut: 0.5,
    arms: true,
    bothHands: true,
    make: () => ({ side: 'right', held: false, period: rand(5, 7), phase: rand(0, 2 * Math.PI) }),
    sample: inspect,
  },
  // The `tasks` activity's pose; `side` is the hand with the clipboard. Held until the activity releases it,
  // like inspect.
  clipboard: {
    duration: 90,
    fadeIn: 0.45,
    fadeOut: 0.45,
    arms: true,
    bothHands: true,
    under: true,
    make: () => ({ side: 'left', period: rand(4.5, 6), phase: rand(0, 3) }),
    sample: clipboardRead,
  },
  // Moments (activities.js): one go each, with their prop in `side`'s hand.
  tick: { duration: 1.5, fadeIn: 0.28, fadeOut: 0.4, arms: true, bothHands: true, make: () => ({ side: 'left' }), sample: tick },
  stopwatch_start: {
    duration: 1.6,
    fadeIn: 0.3,
    fadeOut: 0.45,
    arms: true,
    bothHands: true,
    make: () => ({ side: 'right' }),
    sample: stopwatchStart,
  },
  stopwatch_ring: {
    duration: 2.0,
    fadeIn: 0.25,
    fadeOut: 0.5,
    arms: true,
    bothHands: true,
    make: () => ({ side: 'right' }),
    sample: stopwatchRing,
  },
  ring_bell: { duration: 1.9, fadeIn: 0.3, fadeOut: 0.5, arms: true, bothHands: true, make: () => ({ side: 'right' }), sample: ringBell },
  rainboom: { duration: 1.7, fadeIn: 0.12, fadeOut: 0.55, arms: true, bothHands: true, make: () => ({}), sample: rainboom },
  // The `web` and `morning` activities' poses (the `files` one reads like the clipboard): held until released.
  globe: {
    duration: 90,
    fadeIn: 0.45,
    fadeOut: 0.45,
    arms: true,
    bothHands: true,
    under: true,
    make: () => ({ side: 'left', every: rand(2.8, 3.6), phase: rand(0, 2) }),
    sample: globeSpin,
  },
  mug: {
    duration: 90,
    fadeIn: 0.45,
    fadeOut: 0.45,
    arms: true,
    bothHands: true,
    under: true,
    make: () => ({ side: 'right', every: rand(4.5, 5.5), phase: rand(1, 2), quiet: null }),
    sample: mugHold,
  },
  wrench: { duration: 1.9, fadeIn: 0.25, fadeOut: 0.45, arms: true, bothHands: true, make: () => ({ side: 'right' }), sample: wrenchTwirl },
  headphones: {
    duration: 4.3,
    fadeIn: 0.25,
    fadeOut: 0.4,
    arms: true,
    bothHands: true,
    make: () => ({ side: 'right', bpm: rand(100, 124) }),
    sample: headphonesOn,
  },
  shades: { duration: 2.8, fadeIn: 0.2, fadeOut: 0.5, arms: true, bothHands: true, make: () => ({ side: 'right' }), sample: shades },
  megaphone: { duration: 2.4, fadeIn: 0.2, fadeOut: 0.4, arms: true, bothHands: true, make: () => ({ side: 'right' }), sample: megaphoneShout },
  umbrella: { duration: 3.0, fadeIn: 0.3, fadeOut: 0.5, arms: true, bothHands: true, make: () => ({ side: 'right' }), sample: umbrellaUp },
  // Held until she wakes (nap.js); the long duration is only a safety cap. Slow enough for the calm frame rate.
  nap: { duration: 3 * 3600, fadeIn: 1.2, fadeOut: 0.35, arms: true, bothHands: true, calm: true, make: () => ({}), sample: nap },
  perch: { duration: 3 * 3600, fadeIn: 0.3, fadeOut: 0.3, arms: false, calm: true, make: () => ({ phase: rand(0, 2 * Math.PI) }), sample: perch },
  look_around: {
    duration: 4.5,
    fadeIn: 0.4,
    fadeOut: 0.6,
    arms: false,
    make: () => {
      const first = pick([-1, 1]);
      return { glances: [first, -first, first * 0.3].map((s) => ({ yaw: s * rand(0.45, 0.8), pitch: rand(-0.12, 0.15) })) };
    },
    sample: lookAround,
  },
  stretch: { duration: 2.6, fadeIn: 0.7, fadeOut: 0.8, arms: true, make: () => ({}), sample: stretch },
  hair_tuck: { duration: 1.8, fadeIn: 0.35, fadeOut: 0.45, arms: true, make: () => ({ side: pick(['left', 'right']) }), sample: hairTuck },
  check_nails: { duration: 2.4, fadeIn: 0.4, fadeOut: 0.5, arms: true, make: () => ({ side: pick(['left', 'right']) }), sample: checkNails },
  shoulder_roll: { duration: 2.4, fadeIn: 0.2, fadeOut: 0.4, arms: false, make: () => ({ tilt: pick([-1, 1]) }), sample: shoulderRoll },
  yawn: { duration: 2.6, fadeIn: 0.35, fadeOut: 0.6, arms: true, make: () => ({ side: pick(['left', 'right']) }), sample: yawn },
  peek: { duration: 1.4, fadeIn: 0.1, fadeOut: 0.3, arms: false, make: () => ({ lean: 1 }), sample: peek },
  // A show-off spin (public: the smug lines ask for it), and waiting on something slow (Octavia sends it).
  // Long enough after the turn for her hair to settle, so the finger gun is seen.
  spin: { duration: 1.9, fadeIn: 0.1, fadeOut: 0.4, arms: true, make: () => ({ side: 'right' }), sample: spin },
  impatient: { duration: 3.2, fadeIn: 0.4, fadeOut: 0.6, arms: true, make: () => ({ away: pick([-1, 1]) }), sample: impatient },
  beat: {
    duration: 1.5,
    fadeIn: 0.4,
    fadeOut: 0.55,
    arms: true,
    make: () => ({ side: pick(['left', 'right', 'right', 'both']), reach: rand(0.35, 1), spread: rand(0, 0.6), duration: rand(1.1, 2.1) }),
    sample: beat,
  },
};

// Debug, for tools/drive.mjs: holds a pose given in a message (bone -> [x, y, z]), with optional additive
// offsets and gaze, so new poses can be tuned by eye without restarting the app.
DEFS.custom = {
  duration: 600,
  fadeIn: 0.2,
  fadeOut: 0.3,
  arms: true,
  bothHands: true,
  make: () => ({ pose: {}, add: {}, look: null }),
  sample: (g) => ({ pose: g.pose, add: g.add, look: g.look ?? undefined }),
};

export const PUBLIC_GESTURES = [
  'wave',
  'nod',
  'shake',
  'shrug',
  'laugh',
  'fist_pump',
  'hands_on_hips',
  'surprised_jolt',
  'sigh',
  'think_chin',
  'spin',
  'impatient',
];
// Her fidgets while idle (Animator.idleActions picks among them, a yawn more often when she's sleepy).
export const FIDGETS = ['hair_tuck', 'check_nails', 'shoulder_roll'];

export class GesturePlayer {
  constructor() {
    this.active = [];
  }

  // Starts a gesture and returns it (a handle for live(), releaseOne() and drop()), or null for unknown
  // names. `options` can override the per-instance parameters (e.g. { side: 'left' } or { duration: 1.2 }
  // for a quick wave).
  play(name, options = {}) {
    const def = Object.hasOwn(DEFS, name) ? DEFS[name] : null;
    if (!def) return null;
    const g = { def, t: 0, ...def.make(), ...options };
    g.duration ??= def.duration;
    // A sustained gesture holds until released; its duration is only a safety cap.
    g.releaseAt = def.sustain ? Infinity : g.duration;
    // One gesture per pair of arms: a new arm gesture politely ends the old one. A held pose that others play
    // over (`under`) is a layer of its own beneath them: only another such pose ends it.
    if (def.arms) for (const running of this.active) if (running.def.arms && !running.def.under === !def.under) this.releaseOne(running);
    this.active.push(g);
    return g;
  }

  releaseOne(g) {
    g.releaseAt = Math.min(g.releaseAt, g.t + g.def.fadeOut);
    g.duration = Math.min(g.duration, g.releaseAt);
  }

  // Ends a gesture at once, with no fade (she's out of sight, so there's nothing to see).
  drop(g) {
    this.active = this.active.filter((running) => running !== g);
  }

  // Ends sustained gestures (hands on hips) when the sentence that asked for them is over.
  releaseSustained() {
    for (const g of this.active) if (g.def.sustain) this.releaseOne(g);
  }

  // Whether `g` is playing and not yet on its way out (not released, and short of its fade-out).
  live(g) {
    return !!g && g.t < g.releaseAt - g.def.fadeOut && this.active.includes(g);
  }

  get armsBusy() {
    return this.active.some((g) => g.def.arms && g.t < g.releaseAt);
  }

  // An arm gesture other than `except` has the arms and isn't yet fading out, so a gesture that wants
  // them back (an activity's pose after a picture) can crossfade in as soon as that one starts to go.
  armsHeld(except = null) {
    return this.active.some((g) => g !== except && g.def.arms && g.t < g.releaseAt - g.def.fadeOut);
  }

  // Until its fade-out starts, so whatever it displaced can come back as it goes.
  get bothHandsBusy() {
    return this.active.some((g) => g.def.bothHands && g.t < g.releaseAt - g.def.fadeOut);
  }

  weight(g) {
    const { fadeIn, fadeOut } = g.def;
    if (g.releaseAt === Infinity) return smoothstep(g.t / fadeIn);
    return envelope(g.t, g.releaseAt, fadeIn, fadeOut);
  }

  update(dt) {
    for (const g of this.active) {
      g.t += dt;
      if (g.releaseAt === Infinity && g.t > g.duration) this.releaseOne(g);
    }
    this.active = this.active.filter((g) => g.t < g.releaseAt);
  }

  // Calls visit(weight, sample) for every active gesture: the held poses others play over (`under`) first, then
  // the rest; oldest first in each.
  forEach(visit) {
    for (const under of [true, false]) {
      for (const g of this.active) {
        if (!g.def.under !== !under) continue;
        const w = this.weight(g);
        if (w > 0) visit(clamp(w, 0, 1), g.def.sample(g, Math.min(g.t, g.duration)));
      }
    }
  }
}

