// The orb's life, apart from drawing it (orb.js draws whatever this says, once a frame): how she comes and goes,
// how each of the assistant's states looks, the pulse and ripples of her voice, and flying out of orb form into a
// pointer that rings what she shows you, and back. Everything is in window CSS pixels, y down. No DOM or WebGL in
// here, so it's unit-tested.
//
// Every look is a handful of numbers eased toward the state's own (MOODS), never switched, so no change of state
// can make her jump.

import { clamp, damp, lerp, smoothstep } from './math.js';

export const ORB_RADIUS = 0.18; // of the smaller side of her body's box; the rest is room for her glow, which fades out before the edge
export const RING_RADIUS = 26; // px: the ring round a spot (lib/layout.js's RING_RADIUS)
export const BOX_PAD = 8; // px: the highlight's gap round a thing's box (lib/layout.js's BOX_RING_PAD)
export const BOX_ROUND = 10; // px: the highlight's rounded corners
export const POINTER_HEAD = 9; // px: the orb's radius once it's a pointer
const POINTER_GAP = 5; // px between the ring and the pointer's tip
const POINTER_LENGTH = 30; // px from the pointer's tip to the middle of its head

// Seconds.
// She condenses in (her wisps drawing in from wide, her heart kindling) and dissolves out the same way back, both
// along one `form` (0 gone .. 1 whole) that a show or hide just turns round: called back as she leaves, she
// gathers again from exactly where she was.
export const APPEAR_S = 0.8;
export const LEAVE_S = 0.6;
// Crossing the screen she leaves her window as a bright point and the streak across the screen (lib/trail.js,
// drawn over every monitor) carries her; at the other end she blooms out of a point again. She never slides out of
// her window or in through its side: her window only covers her, so sliding she was cut off at its edge (the user:
// "it clips on the invisible windows she's moving towards", 2026-10-28). She leans a little toward where she's
// going (ZIP_LEAN of her radius) and gathers, so the streak's head takes off from where she is.
export const ZIP_IN_S = 0.45;
export const ZIP_OUT_S = 0.22;
export const ZIP_POINT = 0.22; // her size as a point, of her own
const ZIP_LEAN = 0.35;
export const FLY_S = 0.8; // from her spot to the thing, turning into a pointer
export const RETURN_S = 0.7; // ...and back into an orb (at 0.45 s she swelled from pointer to orb in a few frames)
const BREATH_S = 4.6;
// A change of state eases in and out over about a second: two smoothing stages in a row, so it sets off gently
// instead of jumping into motion (one stage starts at full speed, which the user found hard on the eyes).
const MOOD_TAU = 0.22;
const LOOK_TAU = 0.25;
const VOICE_ATTACK = 0.07; // quick enough to follow syllables, slow enough not to snap
const VOICE_RELEASE = 0.12;
const VOICE_SLOW = 0.35; // the slow envelope a syllable's onset stands out from
// What's drawn of her voice: a glow that rises as she talks and settles as she stops, in two smoothing stages so
// syllables don't show through it. Following each syllable she pumped about four times a second, which the user
// found distracting on her subtitles (2026-10-27) and which is the same thing here.
const VOICE_GLOW_RISE = 0.16;
const VOICE_GLOW_FALL = 0.4;
export const RIPPLE_S = 0.9;
export const MAX_RIPPLES = 4;
const ONSET = 0.15; // the voice this far above its slow envelope starts a ripple...
const RIPPLE_GAP_S = 1.2; // ...at most this often: one soft ripple as a phrase begins, not one a syllable
const PHRASE_QUIET = 0.12; // ...and only out of a pause (the slow envelope this low), or after RIPPLE_LONG_S of talk
const RIPPLE_LONG_S = 3;
// Drowsy: idle and untouched this long she settles, over DROWSE_RAMP_S more, into a dimmer, slower calm (always
// moving at full life in the corner of the screen all day is a distraction); anything at all wakes her at once.
export const DROWSE_AFTER_S = 45;
const DROWSE_RAMP_S = 20;
const DROWSE_WAKE_TAU = 0.2;
const DROWSE_SINK_TAU = 2;
const STRETCH_TAU = 0.06;
const KICK_TAU = 0.5;
const SILENCE_S = 0.15; // no mouth message this long: her voice has stopped (lipsync.js's SILENCE_AFTER)
// Her clock wraps here (and whenever she's out of sight), so float32 in the shader never gets coarse; at most a
// tiny jump every four and a half hours of `always` mode.
export const TIME_WRAP_S = 16384;
// The spin wraps here: the shader turns things by it times 2 (the swirl) or times a multiple of 1/4 (the wisps),
// all whole turns at 8 pi, so the wrap never shows. (At pi, the wisps jumped a quarter turn every few seconds.)
export const SPIN_WRAP = 8 * Math.PI;
const FLASH_TAU = 0.18;
const WORKING_FLASH_S = 1.6; // a snapshot or a moment shows as work this long
// Her motes' flow (how far along their drift out of her they are, integrated here: see the spin) wraps here. The
// shader gives each mote a rate of a whole number of 32nds, so the wrap moves each one by whole lives.
export const FLOW_WRAP = 32;
// The phase her surface's ripple of noise travels round (the shader samples noise round a circle by it), one turn.
export const SURFACE_WRAP = 2 * Math.PI;
const FEEL_TAU = 0.4; // a feeling eases in and out like a change of state (two stages), a little slower
const FEEL_HOLD_S = 2.5; // a feeling without a length of its own stays this long
// Her flight's trail: where she's been in the last TRAIL_S, a point every TRAIL_STEP_S while she moves faster than
// TRAIL_SPEED px/s.
// Her ghosts (orb.js): now and then faint geometry (a ring, a polygon, an arc) surfaces in her glass for GHOST_S and
// sinks away again. Only while she rests at home, a random GHOST_GAP_S apart, so they stay rare and never land on cue.
export const GHOST_S = 4.5;
export const GHOST_GAP_S = Object.freeze([40, 100]);
export const TRAIL_POINTS = 10;
export const TRAIL_S = 0.45;
const TRAIL_STEP_S = 1 / 40;
const TRAIL_SPEED = 160;

// What a feeling does to her: lift (-1 sinking, heavy and dim .. 1 light, her motes rising) and energy (0 calm .. 1
// lively: quicker, brighter motes and runs of light). The bridge's emotions (avatar/emotion.py); anything else is
// none. Never a colour of its own: she stays her hazy blue and only leans a little cyan or violet.
export const FEELS = Object.freeze({
  happy: { lift: 0.6, energy: 0.55 },
  excited: { lift: 0.7, energy: 1 },
  laughing: { lift: 0.8, energy: 0.9 },
  laugh: { lift: 0.8, energy: 0.9 },
  chuckling: { lift: 0.5, energy: 0.5 },
  playful: { lift: 0.5, energy: 0.7 },
  proud: { lift: 0.5, energy: 0.5 },
  confident: { lift: 0.3, energy: 0.4 },
  smug: { lift: 0.25, energy: 0.3 },
  relaxed: { lift: 0.3, energy: 0 },
  curious: { lift: 0.2, energy: 0.6 },
  surprised: { lift: 0.1, energy: 1 },
  sarcastic: { lift: 0, energy: 0.3 },
  thinking: { lift: 0, energy: 0.3 },
  whispering: { lift: 0, energy: 0 },
  sigh: { lift: -0.45, energy: 0 },
  sad: { lift: -0.85, energy: 0 },
});

// How each state looks: glow (bloom), swirl (the light turning inside her), shimmer (the edge flickering), spin
// (how fast it all turns), halo (the listening ring), orbit (work: beads racing along the wisps) and hue (0 electric
// blue, toward 1 cyan, toward -1 violet).
export const MOODS = Object.freeze({
  idle: { glow: 0.55, swirl: 0.12, shimmer: 0.08, spin: 0.2, halo: 0, orbit: 0, hue: 0 },
  listening: { glow: 1, swirl: 0.2, shimmer: 0.15, spin: 0.45, halo: 1, orbit: 0, hue: 0.12 },
  thinking: { glow: 0.75, swirl: 1, shimmer: 0.5, spin: 1.2, halo: 0, orbit: 0, hue: 0.25 },
  working: { glow: 0.8, swirl: 0.45, shimmer: 0.3, spin: 0.9, halo: 0, orbit: 1, hue: -0.18 },
  speaking: { glow: 0.85, swirl: 0.25, shimmer: 0.2, spin: 0.5, halo: 0, orbit: 0, hue: 0.05 },
  confirming: { glow: 0.9, swirl: 0.2, shimmer: 0.2, spin: 0.35, halo: 0.6, orbit: 0, hue: -0.15 },
});
const LOOKS = Object.keys(MOODS.idle);

// What the orb shows for the assistant's `state`, with `busy` while a tool, an activity or a snapshot is under way.
// Her voice and your voice come first; work shows over thinking.
export function moodFor(state, busy) {
  if (state === 'speaking') return 'speaking';
  if (state === 'listening' || state === 'transcribing') return 'listening';
  if (busy) return 'working';
  if (state === 'thinking') return 'thinking';
  if (state === 'confirming') return 'confirming';
  return 'idle';
}

// Where the metrics put her when she flies out: lib/layout.js stands her body's box (square, the orb in its middle)
// just beside the thing, and she flies the rest of the way herself.
export const ORB_METRICS = Object.freeze({ type: 'metrics', shoulderY: 0.5, shoulderX: 0, reach: 0.1, bodyWidth: 1, seatY: 0.78 });

const unit = (x, y) => {
  const n = Math.hypot(x, y);
  return n > 1e-6 ? { x: x / n, y: y / n } : null;
};
const mix = (a, b, t) => ({ x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) });
const easeIn = (t) => clamp(t, 0, 1) ** 2;
const easeOut = (t) => 1 - (1 - clamp(t, 0, 1)) ** 3;
// Slow off the mark, slow into the arrival.
const glide = (t) => {
  const x = clamp(t, 0, 1);
  return x * x * x * (x * (x * 6 - 15) + 10);
};

// The pointer for a thing at `target` (with `box` its extent, or null), coming from `from`: the ring (a rounded
// rectangle {x, y, w, h, round}), the pointer's tip just outside it on `from`'s side and its head further out, kept
// inside `size` ({w, h}, the window) when given.
export function pointerGeometry(target, box, from, size = null) {
  const ring = box
    ? { x: box.x - BOX_PAD, y: box.y - BOX_PAD, w: box.w + 2 * BOX_PAD, h: box.h + 2 * BOX_PAD, round: BOX_ROUND }
    : { x: target.x - RING_RADIUS, y: target.y - RING_RADIUS, w: 2 * RING_RADIUS, h: 2 * RING_RADIUS, round: RING_RADIUS };
  const c = { x: ring.x + ring.w / 2, y: ring.y + ring.h / 2 };
  // From below right, like a mouse pointer, when there's no telling where she comes from.
  const d = (from && unit(from.x - c.x, from.y - c.y)) || { x: Math.SQRT1_2, y: Math.SQRT1_2 };
  const toEdge = Math.min(d.x ? ring.w / 2 / Math.abs(d.x) : Infinity, d.y ? ring.h / 2 / Math.abs(d.y) : Infinity);
  const at = (k) => {
    const p = { x: c.x + d.x * k, y: c.y + d.y * k };
    if (!size) return p;
    return { x: clamp(p.x, POINTER_HEAD, size.w - POINTER_HEAD), y: clamp(p.y, POINTER_HEAD, size.h - POINTER_HEAD) };
  };
  return { ring, tip: at(toEdge + POINTER_GAP), head: at(toEdge + POINTER_GAP + POINTER_LENGTH) };
}

export class OrbMotion {
  constructor() {
    this.phase = 'hidden'; // hidden, entering, shown, leaving, zip-in, zip-out or away (zipped off, out of sight)
    this.t = 0; // seconds into the phase
    this.time = 0;
    this.size = { w: 1, h: 1 }; // the window
    this.body = { x: 0, y: 0, w: 1, h: 1 }; // her box in it
    this.state = 'idle';
    this.working = false; // the assistant says a tool is running
    this.activity = false; // an activity (a search, the web...) is under way
    this.workFlash = 0; // seconds a snapshot or a moment still shows as work
    this.look = { ...MOODS.idle };
    this.lookMid = { ...MOODS.idle }; // the first smoothing stage, between the state's look and what's drawn
    this.spinPhase = 0;
    this.level = 0; // her voice, as the last mouth message said
    this.sinceMouth = Infinity; // s since that message
    this.voice = 0;
    this.slowVoice = 0;
    this.voiceMid = 0;
    this.voiceGlow = 0; // what's drawn of her voice (VOICE_GLOW_*)
    this.form = 0; // 0 gone .. 1 whole (APPEAR_S, LEAVE_S)
    this.breath = 0; // her breath's phase, integrated (it slows as she drowses)
    this.calmFor = 0; // s idle and untouched
    this.drowseMid = 0;
    this.drowse = 0; // 0 awake .. 1 drowsy
    this.stretchAmount = 0;
    this.sinceRipple = Infinity;
    this.ripples = []; // {age, strength}
    this.kick = 0; // a burst of brightness: a feeling, a moment
    this.kickTarget = 0; // what it swells toward (it rises over a moment rather than flashing on)
    this.flash = 0; // the ring flashing as the thing is clicked
    this.gaze = { x: 0, y: 0 }; // where her inner light leans: toward the pointer, or a window you switched to
    this.gazeTarget = { x: 0, y: 0 };
    this.glanceFor = 0;
    this.zip = null; // {dir}: where she's off to, or where she comes from
    this.hideWhenOut = false;
    this.arriveOnShow = false;
    this.pointing = null; // {target, box, geometry, from, p (0 orb .. 1 pointer), returning, countdown, left}
    this.prev = null;
    this.velocity = { x: 0, y: 0 };
    this.events = [];
    this.feel = { lift: 0, energy: 0 }; // what's drawn
    this.feelMid = { lift: 0, energy: 0 };
    this.feelTarget = { lift: 0, energy: 0 };
    this.feelLeft = 0; // s the feeling still holds
    this.flow = 0; // her motes' drift (FLOW_WRAP)
    this.surface = 0; // her surface's ripple (SURFACE_WRAP)
    this.trail = []; // {x, y, age}, newest first
    this.sinceTrail = 0;
    this.random = Math.random; // (tests swap in their own)
    this.ghost = null; // {age, kind (0 ring, 1 polygon, 2 arc), sides, seed}
    this.untilGhost = this.ghostGap();
  }

  ghostGap() {
    return GHOST_GAP_S[0] + (GHOST_GAP_S[1] - GHOST_GAP_S[0]) * this.random();
  }

  // A ghost now (of `kind`, or any), however long it's been since the last one: the next is a full gap after it.
  // One already showing plays out (starting over, it vanished in a frame).
  summonGhost(kind = null) {
    if (this.ghost) return;
    const k = [0, 1, 2].includes(kind) ? kind : Math.min(2, Math.floor(3 * this.random()));
    this.ghost = { age: 0, kind: k, sides: 3 + Math.min(3, Math.floor(4 * this.random())), seed: this.random() };
    this.untilGhost = this.ghostGap();
  }

  // Her ghost's age, 0 new .. 1 gone, or -1 for none; then its kind, sides and seed (the shader's uGhost).
  get ghostUniform() {
    const g = this.ghost;
    return g ? [g.age / GHOST_S, g.kind, g.sides, g.seed] : [-1, 0, 0, 0];
  }

  updateGhost(dt) {
    // Only at rest at home, idle or thinking: flying, pointing or coming and going she has other things to show, and
    // while you talk or she does her halo and her voice should read clearly.
    const atRest = this.settled && !this.pointing && this.form >= 1;
    if (this.ghost) {
      // One caught by a flight or a hide sinks out quickly rather than riding along (or popping off).
      this.ghost.age += atRest ? dt : 4 * dt;
      if (this.ghost.age >= GHOST_S || !this.visible) this.ghost = null;
      return;
    }
    if (!atRest || (this.mood !== 'idle' && this.mood !== 'thinking')) return;
    this.untilGhost -= dt;
    if (this.untilGhost <= 0) this.summonGhost();
  }

  // The window's size and where her body sits in it (window px).
  layout(size, body) {
    this.size = { w: Math.max(1, size.w), h: Math.max(1, size.h) };
    this.body = body || { x: 0, y: 0, w: this.size.w, h: this.size.h };
    this.prev = null; // the window moved under her: that's no velocity of hers
    this.stretchAmount = 0;
    this.trail = []; // ...nor a path of hers
  }

  get visible() {
    return this.phase !== 'hidden' && this.phase !== 'away';
  }

  get settled() {
    return this.phase === 'shown';
  }

  get busy() {
    return this.working || this.activity || this.workFlash > 0;
  }

  get mood() {
    return moodFor(this.state, this.busy);
  }

  // Nothing changing but the breath, the swirl and the sparks.
  get calm() {
    return this.settled && this.mood === 'idle' && !this.pointing && !this.trail.length && !this.ripples.length && this.voice < 0.01 && this.kick < 0.02 && this.kickTarget < 0.02;
  }

  get rest() {
    return { x: this.body.x + this.body.w / 2, y: this.body.y + this.body.h / 2 };
  }

  get baseRadius() {
    return ORB_RADIUS * Math.min(this.body.w, this.body.h);
  }

  // ---- what she's told ----

  setState(state) {
    this.state = typeof state === 'string' ? state : 'idle';
  }

  setVoice(level) {
    this.level = Number.isFinite(level) ? clamp(level, 0, 1) : 0;
    this.sinceMouth = 0;
  }

  // Out of sight, or pointing: whatever she was busy with is over (the bridge ends activities without a word then).
  endWork() {
    this.activity = false;
    this.workFlash = 0;
  }

  // A feeling or a moment: a burst of light, `amount` 0..1.
  spark(amount) {
    if (Number.isFinite(amount)) this.kickTarget = Math.max(this.kickTarget, clamp(amount, 0, 1));
  }

  // A feeling (one of FEELS, by name) at `intensity` 0..1 for `seconds` (FEEL_HOLD_S when not given); then it
  // eases away again. An unknown one is no feeling, so a new emotion from the bridge can't break her.
  feelFor(emotion, intensity, seconds) {
    const f = FEELS[emotion];
    const k = Number.isFinite(intensity) ? clamp(intensity, 0, 1) : 0.5;
    if (!f || k <= 0) return;
    // Even a slight feeling shows a little; the strongest are as FEELS says.
    const scale = 0.35 + 0.65 * k;
    this.feelTarget = { lift: f.lift * scale, energy: f.energy * scale };
    this.feelLeft = Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds, 30) : FEEL_HOLD_S;
  }

  // A snapshot or a moment: busy for a little while.
  briefWork() {
    this.workFlash = WORKING_FLASH_S;
  }

  // Window px her inner light leans toward: the pointer (`glance` false) or, for a moment, a window (`glance`).
  lookAt(x, y, glance = false) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (!glance && this.glanceFor > 0) return;
    const r = this.rest;
    const d = unit(x - r.x, y - r.y);
    const near = clamp(Math.hypot(x - r.x, y - r.y) / 300, 0, 1);
    this.gazeTarget = d ? { x: d.x * near, y: d.y * near } : { x: 0, y: 0 };
    if (glance) this.glanceFor = 1.2;
  }

  show({ arrive = false } = {}) {
    if (this.phase === 'leaving') {
      // Back before she's gone: she gathers again from exactly where she is (her form just turns round).
      this.phase = 'entering';
      this.t = 0;
      this.arriveOnShow = arrive;
      return;
    }
    if (this.phase === 'entering' || this.phase === 'shown' || this.phase === 'zip-in') {
      if (arrive && this.phase === 'shown') this.events.push('arrived');
      else if (arrive) this.arriveOnShow = true;
      return;
    }
    this.arriveOnShow = arrive;
    this.zip = null;
    this.hideWhenOut = false;
    this.phase = 'entering';
    this.t = 0;
    this.form = 0;
  }

  hide() {
    if (!this.visible) {
      this.phase = 'hidden';
      this.pointing = null;
      this.events.push('hidden');
      return;
    }
    if (this.phase === 'zip-out') {
      this.hideWhenOut = true;
      return;
    }
    if (this.phase === 'leaving') return;
    this.phase = 'leaving';
    this.t = 0;
  }

  // Flies in from `dir` (window px, from her spot toward where she comes from). False when she's already here.
  zipIn(dir) {
    if (this.phase === 'shown' || this.phase === 'zip-in') return false;
    const d = (dir && unit(dir.x, dir.y)) || { x: 0, y: 1 };
    this.zip = { dir: d };
    this.hideWhenOut = false;
    this.arriveOnShow = false;
    this.phase = 'zip-in';
    this.t = 0;
    return true;
  }

  // Flies off toward `dir` (window px) and ends out of sight. False when she isn't there to leave.
  zipOut(dir) {
    if (!this.visible) return false;
    const d = (dir && unit(dir.x, dir.y)) || { x: 0, y: 1 };
    this.zip = { dir: d };
    this.hideWhenOut = false;
    if (this.pointing) this.pointing.returning = true;
    this.phase = 'zip-out';
    this.t = 0;
    return true;
  }

  // Flies from her spot to `target` (window px; `box` the thing's extent or null) and turns into a pointer there.
  point(target, box = null) {
    // Already out pointing in this window: she sets off again from wherever she is.
    const from = this.pointing ? this.position() : this.rest;
    const geometry = pointerGeometry(target, box, this.rest, this.size);
    this.pointing = { target, box, geometry, from, p: 0, returning: false, countdown: null, left: 0 };
  }

  unpoint() {
    if (this.pointing) this.pointing.returning = true;
  }

  // Pointing ends in `seconds`: the ring fades and draws in until then.
  countdown(seconds) {
    if (!this.pointing || !Number.isFinite(seconds) || seconds <= 0) return;
    this.pointing.countdown = seconds;
    this.pointing.left = seconds;
  }

  press() {
    if (this.pointing && !this.pointing.returning) this.flash = 1;
  }

  // Events since the last call ('arrived', 'zipped-out', 'hidden'), oldest first.
  takeEvents() {
    return this.events.splice(0);
  }

  // ---- every frame ----

  update(dt) {
    this.time += dt;
    if (this.time >= TIME_WRAP_S || (!this.visible && this.time >= 600)) this.time %= 600;
    this.t += dt;
    this.workFlash = Math.max(0, this.workFlash - dt);
    this.glanceFor = Math.max(0, this.glanceFor - dt);
    const target = MOODS[this.mood];
    for (const key of LOOKS) {
      this.lookMid[key] = damp(this.lookMid[key], target[key], MOOD_TAU, dt);
      this.look[key] = damp(this.look[key], this.lookMid[key], MOOD_TAU, dt);
    }
    this.updateForm(dt);
    this.updateDrowse(dt);
    this.spinPhase = (this.spinPhase + this.look.spin * (1 - 0.5 * this.drowse) * dt) % SPIN_WRAP;
    this.breath = (this.breath + ((2 * Math.PI) / BREATH_S) * (1 - 0.3 * this.drowse) * dt) % (2 * Math.PI);
    this.updateVoice(dt);
    this.updateFeel(dt);
    this.kickTarget = damp(this.kickTarget, 0, KICK_TAU, dt);
    this.kick = damp(this.kick, this.kickTarget, 0.12, dt);
    this.flash = damp(this.flash, 0, FLASH_TAU, dt);
    this.gaze = { x: damp(this.gaze.x, this.gazeTarget.x, LOOK_TAU, dt), y: damp(this.gaze.y, this.gazeTarget.y, LOOK_TAU, dt) };
    this.updatePhase();
    this.updatePointing(dt);
    const at = this.position();
    if (this.prev && dt > 0) {
      const v = { x: (at.x - this.prev.x) / dt, y: (at.y - this.prev.y) / dt };
      this.velocity = { x: damp(this.velocity.x, v.x, 0.05, dt), y: damp(this.velocity.y, v.y, 0.05, dt) };
    } else {
      this.velocity = { x: 0, y: 0 };
    }
    this.prev = at;
    const speed = Math.hypot(this.velocity.x, this.velocity.y);
    // Her stretch along the flight eases: following her raw speed it flicked as she set off and arrived.
    this.stretchAmount = damp(this.stretchAmount, clamp(speed / 2800, 0, 0.8), STRETCH_TAU, dt);
    this.updateTrail(at, dt);
    this.updateGhost(dt);
  }

  // Her form grows while she comes in and is here, and shrinks while she leaves, at one steady pace each way.
  updateForm(dt) {
    if (this.phase === 'leaving') this.form = Math.max(0, this.form - dt / LEAVE_S);
    else if (this.phase === 'hidden' || this.phase === 'away') this.form = 0;
    else if (this.phase === 'zip-in' || this.phase === 'zip-out') this.form = 1; // a zip is a flight: she's whole
    else this.form = Math.min(1, this.form + dt / APPEAR_S);
  }

  updateDrowse(dt) {
    const quiet = this.settled && this.mood === 'idle' && !this.pointing && this.level < 0.01 && this.voiceGlow < 0.01 && this.kickTarget < 0.05;
    this.calmFor = quiet ? this.calmFor + dt : 0;
    const target = smoothstep((this.calmFor - DROWSE_AFTER_S) / DROWSE_RAMP_S);
    const tau = target < this.drowse ? DROWSE_WAKE_TAU : DROWSE_SINK_TAU;
    this.drowseMid = damp(this.drowseMid, target, tau, dt);
    this.drowse = damp(this.drowse, this.drowseMid, tau, dt);
  }

  updateFeel(dt) {
    this.feelLeft = Math.max(0, this.feelLeft - dt);
    if (this.feelLeft === 0) this.feelTarget = { lift: 0, energy: 0 };
    for (const key of ['lift', 'energy']) {
      this.feelMid[key] = damp(this.feelMid[key], this.feelTarget[key], FEEL_TAU, dt);
      this.feel[key] = damp(this.feel[key], this.feelMid[key], FEEL_TAU, dt);
    }
    // Her motes drift out of her, quicker when she's lively and as she speaks, and while she listens they stream
    // into her instead (the halo turns the flow round). Integrated, as the spin is: the shader never multiplies
    // time by a rate that changes.
    this.flow = (((this.flow + this.flowRate * dt) % FLOW_WRAP) + FLOW_WRAP) % FLOW_WRAP;
    // Her surface churns with her voice: slow at rest, quick while she talks.
    this.surface = (this.surface + (0.4 + 2.2 * this.voiceGlow + 0.6 * this.feel.energy) * dt) % SURFACE_WRAP;
  }

  // How fast her motes drift outward (negative: inward), in lives a second times the shader's per-mote 32nds.
  get flowRate() {
    return 1 + 0.8 * this.feel.energy + 2 * this.voiceGlow - 2.4 * this.look.halo;
  }

  // Where she's just been, while she flies: points of her path that age out over TRAIL_S. Only real flight: the
  // window moving under her (layout) clears it, as it clears her velocity.
  updateTrail(at, dt) {
    for (const p of this.trail) p.age += dt;
    this.trail = this.trail.filter((p) => p.age < TRAIL_S);
    this.sinceTrail += dt;
    const speed = Math.hypot(this.velocity.x, this.velocity.y);
    if (!this.visible || speed < TRAIL_SPEED || this.sinceTrail < TRAIL_STEP_S) return;
    this.sinceTrail = 0;
    this.trail.unshift({ x: at.x, y: at.y, age: 0 });
    if (this.trail.length > TRAIL_POINTS) this.trail.length = TRAIL_POINTS;
  }

  updateVoice(dt) {
    this.sinceMouth += dt;
    // A level nobody followed up is a voice that stopped (cut off mid-word, or a phrase with no closing 0).
    const level = this.sinceMouth > SILENCE_S ? 0 : this.level;
    this.voice = damp(this.voice, level, level > this.voice ? VOICE_ATTACK : VOICE_RELEASE, dt);
    const glowTau = level > this.voiceMid ? VOICE_GLOW_RISE : VOICE_GLOW_FALL;
    this.voiceMid = damp(this.voiceMid, level, glowTau, dt);
    this.voiceGlow = damp(this.voiceGlow, this.voiceMid, glowTau, dt);
    const quietBefore = this.slowVoice < PHRASE_QUIET;
    this.slowVoice = damp(this.slowVoice, level, VOICE_SLOW, dt);
    this.sinceRipple += dt;
    for (const r of this.ripples) r.age += dt;
    this.ripples = this.ripples.filter((r) => r.age < RIPPLE_S);
    const phrase = quietBefore || this.sinceRipple > RIPPLE_LONG_S;
    if (level - this.slowVoice > ONSET && this.sinceRipple > RIPPLE_GAP_S && phrase) {
      this.sinceRipple = 0;
      this.ripples.push({ age: 0, strength: clamp(level, 0.3, 0.7) });
      if (this.ripples.length > MAX_RIPPLES) this.ripples.shift();
    }
  }

  updatePhase() {
    if (this.phase === 'entering' && this.form >= 1) {
      this.phase = 'shown';
      if (this.arriveOnShow) this.events.push('arrived');
      this.arriveOnShow = false;
    } else if (this.phase === 'zip-in' && this.t >= ZIP_IN_S) {
      this.phase = 'shown';
      this.zip = null;
      this.events.push('arrived');
    } else if (this.phase === 'zip-out' && this.t >= ZIP_OUT_S) {
      this.phase = this.hideWhenOut ? 'hidden' : 'away';
      this.zip = null;
      this.pointing = null;
      this.events.push(this.hideWhenOut ? 'hidden' : 'zipped-out');
      this.hideWhenOut = false;
    } else if (this.phase === 'leaving' && this.form <= 0) {
      this.phase = 'hidden';
      this.pointing = null;
      this.events.push('hidden');
    }
  }

  updatePointing(dt) {
    const pt = this.pointing;
    if (!pt) return;
    if (pt.returning) {
      // Zipping off, she gathers back into an orb on the way.
      const speed = this.phase === 'zip-out' ? 1 / ZIP_OUT_S : 1 / RETURN_S;
      pt.p = Math.max(0, pt.p - speed * dt);
      if (pt.p === 0) this.pointing = null;
      return;
    }
    // She only sets off once she's here.
    if (this.phase === 'shown') pt.p = Math.min(1, pt.p + dt / FLY_S);
    if (pt.countdown) pt.left = Math.max(0, pt.left - dt);
  }

  // ---- what to draw ----

  // How far into pointer form: 0 an orb, 1 a pointer.
  get morph() {
    const pt = this.pointing;
    return pt ? glide((pt.p - 0.3) / 0.7) : 0;
  }

  // Her centre (window px), with the flight to the thing and any zip.
  position() {
    let at = this.rest;
    const pt = this.pointing;
    if (pt && pt.p > 0) {
      const to = pt.geometry.head;
      const from = pt.returning ? this.rest : pt.from;
      const k = glide(pt.p);
      at = mix(from, to, k);
      // A gentle arc, not a straight line.
      const d = unit(to.x - from.x, to.y - from.y);
      if (d) {
        const bow = Math.sin(Math.PI * k) * 0.12 * Math.hypot(to.x - from.x, to.y - from.y);
        at = { x: at.x - d.y * bow, y: at.y + d.x * bow };
      }
    }
    if (this.zip) {
      const lean = ZIP_LEAN * this.baseRadius * this.gathered;
      at = { x: at.x + this.zip.dir.x * lean, y: at.y + this.zip.dir.y * lean };
    }
    return at;
  }

  // How far she's gathered into a point to cross the screen: 0 herself .. 1 a point.
  get gathered() {
    if (this.phase === 'zip-out') return easeIn(this.t / ZIP_OUT_S);
    if (this.phase === 'zip-in') return 1 - easeOut(this.t / ZIP_IN_S);
    return 0;
  }

  // Her size (1 = at rest) as she condenses and dissolves, and breathes. No overshoot: a pop is the one thing that
  // reads as a cartoon in her.
  get scale() {
    const s = this.visible ? lerp(0.72, 1, glide(this.form)) * lerp(1, ZIP_POINT, this.gathered) : 0;
    const breathing = 0.035 * (1 - 0.6 * this.voiceGlow) * (1 - this.morph);
    return Math.max(0, s * (1 + breathing * Math.sin(this.breath) + 0.05 * this.voiceGlow + 0.05 * this.kick));
  }

  get alpha() {
    if (!this.visible) return 0;
    if (this.phase === 'entering' || this.phase === 'leaving') return smoothstep(this.form / 0.7);
    // As a point she's bright, and goes out only as the streak's head takes over (and comes in as it arrives).
    // (A beat late: the streak's head is still a little short of her when her window shows her.)
    if (this.phase === 'zip-in') return smoothstep((this.t - ZIP_IN_S * 0.12) / (ZIP_IN_S * 0.25));
    if (this.phase === 'zip-out') return 1 - smoothstep((this.t - ZIP_OUT_S * 0.75) / (ZIP_OUT_S * 0.25));
    return 1;
  }

  get radius() {
    return lerp(this.baseRadius, POINTER_HEAD, this.morph) * this.scale;
  }

  // The highlight round the thing: {ring, tip, alpha, flash, drawIn (0 .. 1 as the countdown runs out)}, or null.
  get highlight() {
    const pt = this.pointing;
    if (!pt) return null;
    const drawIn = pt.countdown ? 1 - pt.left / pt.countdown : 0;
    const alpha = smoothstep((pt.p - 0.6) / 0.4) * (1 - 0.85 * smoothstep(drawIn)) * this.alpha;
    if (alpha <= 0.001) return null;
    return { ring: pt.geometry.ring, tip: pt.geometry.tip, alpha, flash: this.flash, drawIn };
  }

  // How much she stretches along her flight (0 round), and which way: {x, y, amount}.
  get stretch() {
    const d = unit(this.velocity.x, this.velocity.y) || { x: 1, y: 0 };
    return { x: d.x, y: d.y, amount: this.stretchAmount };
  }

  // Whether window px (x, y) is on her: only the orb itself, never a pointer (that mustn't stand in a click's way).
  contains(x, y) {
    if (!this.visible || this.morph > 0.2) return false;
    const at = this.position();
    return Math.hypot(x - at.x, y - at.y) <= this.radius * 1.1;
  }
}
