// Popping in and out. At home she rises into view from below the window with a little overshoot, or now and
// then peeks in from the side instead (half way in, a peek, then the rest of the way with a little bounce), and on
// leaving gives a short wave, then sinks and fades away. When she flies somewhere (to point at a thing on
// the screen, and back home) she zips instead: in from the direction she comes from with a short rainbow
// streak and a little overshoot, and out the same way toward where she's going.
//
// A rise moves her own root (the animator's rootY), so her hair and clothes bounce as they always have. A
// zip moves `flight`, a group around her that her spring bones simulate relative to: flying a body length
// in a fraction of a second would otherwise fling her hair straight up.

import { clamp, easeOutBack, lerp, smoothstep } from './math.js';

const ENTER = 0.55;
export const WAVE_OUT = 0.85; // how long the goodbye wave plays before she sinks
export const LEAVE = 0.6;
const LONG_ABSENCE = 60; // s hidden before a return deserves a hello wave
const ZIP_IN = 0.45;
const ZIP_OUT = 0.25;
const ZIP_DISTANCE = 1.2; // m: where a zip starts or ends, about a body length away (outside her window)
// A peek: sliding in half way (s), peeking, then sliding the rest of the way in; from this far to the side (m,
// mostly out of her window) to this far.
export const PEEK = [0.35, 0.5, 0.45];
const PEEK_OUT = 0.45;
const PEEK_HALF = 0.22;

const now = () => performance.now() / 1000;

export class Presence {
  // `sink` is how far below her standing position (m) she starts and ends a rise; `flight` is the group
  // a zip moves. `events` gets hidden() when she's gone after a hide, arrived() when a zip-in finished and
  // zippedOut() when a zip-out did.
  constructor({ animator, canvas, flight, sink, events }) {
    this.animator = animator;
    this.canvas = canvas;
    this.flight = flight;
    this.sink = sink;
    this.events = events;
    // hidden | entering | peeking | shown | waving | leaving (at home), zip-in | zip-out | away (flying)
    this.phase = 'hidden';
    this.t = 0;
    this.lift = -sink; // rise and sink (m, on her root)
    this.at = { x: 0, y: 0 }; // zip offset from her spot (m, on the flight group)
    this.opacity = 0;
    this.zip = null; // { dir: {x, y} unit, start: {x, y}, startLift } while zipping
    this.hideWhenOut = false; // a hide came during a zip-out: it ends hidden, not away
    this.hiddenSince = -Infinity; // never shown yet counts as a long absence
    this.helloWave = false;
  }

  get visible() {
    return this.phase !== 'hidden' && this.phase !== 'away';
  }

  // Standing still on screen (not arriving or leaving).
  get settled() {
    return this.phase === 'shown';
  }

  // Comes into view at home: rising from below, or with `peek` -1 or 1 (her right or left), peeking in from that
  // side, if she's out of sight.
  show(reason, { peek = 0 } = {}) {
    if (this.phase === 'entering' || this.phase === 'shown' || this.phase === 'zip-in' || this.phase === 'peeking') return;
    const gone = !this.visible;
    // Wall-clock time: no frames (and no update calls) happen while she is hidden.
    const away = gone ? now() - this.hiddenSince : 0;
    this.helloWave = (reason === 'wake' || reason === 'talk') && away > LONG_ABSENCE;
    if (gone && (peek === 1 || peek === -1)) {
      this.peekSide = peek;
      this.at = { x: peek * PEEK_OUT, y: 0 };
      this.lift = 0;
      this.opacity = 0;
      this.startOpacity = 0;
      this.zip = null;
      this.hideWhenOut = false;
      this.phase = 'peeking';
      this.t = 0;
      this.animator.playGesture('peek', { lean: peek });
      return;
    }
    if (gone || this.phase === 'zip-out') {
      // Out of sight (or on her way out sideways): come up from below at home.
      this.at = { x: 0, y: 0 };
      this.lift = -this.sink;
      if (gone) this.opacity = 0;
    }
    // Start from wherever she is, so a show during the exit turns around smoothly.
    this.startLift = this.lift;
    this.startOpacity = this.opacity;
    this.zip = null;
    this.hideWhenOut = false;
    this.phase = 'entering';
    this.t = 0;
  }

  hide() {
    if (!this.visible) {
      this.phase = 'hidden';
      this.events.hidden();
      return;
    }
    if (this.phase === 'zip-out') {
      // Already on her way out: no wave, the zip is the exit.
      this.hideWhenOut = true;
      return;
    }
    if (this.phase === 'waving' || this.phase === 'leaving') return;
    this.animator.playGesture('wave', { duration: 1.2 });
    this.phase = 'waving';
    this.t = 0;
  }

  // Flies in: `dir` (world, x right and y up) points from her spot toward where she comes from. Returns
  // false if she's already here.
  zipIn(dir) {
    if (this.phase === 'shown' || this.phase === 'zip-in') return false;
    const d = unit(dir) || { x: 0, y: 1 };
    // Caught mid-way out, she turns around from where she is; otherwise she comes from out of sight.
    const turning = this.phase === 'zip-out';
    this.zip = { dir: d, start: turning ? { ...this.at } : { x: d.x * ZIP_DISTANCE, y: d.y * ZIP_DISTANCE }, startLift: this.lift };
    if (!turning) this.opacity = 0;
    this.startOpacity = this.opacity;
    this.helloWave = false;
    this.hideWhenOut = false;
    this.phase = 'zip-in';
    this.t = 0;
    return true;
  }

  // Flies off toward `dir` (world, x right and y up) and ends invisible ('away'). Returns false when she
  // isn't there to leave.
  zipOut(dir) {
    if (!this.visible) return false;
    const d = unit(dir) || { x: 0, y: 1 };
    this.zip = { dir: d, start: { ...this.at }, startLift: this.lift };
    this.startOpacity = this.opacity;
    this.hideWhenOut = false;
    this.phase = 'zip-out';
    this.t = 0;
    return true;
  }

  // The in-window rainbow streak while zipping, as offsets (m) from her spot, or null.
  get streak() {
    const z = this.zip;
    if (!z || (this.phase !== 'zip-in' && this.phase !== 'zip-out')) return null;
    const head = { ...this.at };
    if (this.phase === 'zip-in') {
      const p = clamp(this.t / ZIP_IN, 0, 1);
      // The tail catches up with her as she settles.
      return { tail: mix(z.start, head, smoothstep((p - 0.3) / 0.7)), head, alpha: 1 - smoothstep((p - 0.55) / 0.45) };
    }
    const p = clamp(this.t / ZIP_OUT, 0, 1);
    return { tail: mix(z.start, head, smoothstep((p - 0.35) / 0.65)), head, alpha: 1 - smoothstep((p - 0.6) / 0.4) };
  }

  update(dt) {
    this.t += dt;
    if (this.phase === 'entering') {
      const p = this.t / ENTER;
      this.lift = lerp(this.startLift, 0, easeOutBack(p, 1.2));
      this.opacity = lerp(this.startOpacity, 1, smoothstep(p / 0.6));
      if (this.helloWave && this.t >= 0.25) {
        this.helloWave = false;
        this.animator.playGesture('wave', { duration: 1.8 });
      }
      if (p >= 1) this.phase = 'shown';
    } else if (this.phase === 'peeking') {
      const [slide, hold, settle] = PEEK;
      let x = PEEK_HALF;
      if (this.t < slide) x = lerp(PEEK_OUT, PEEK_HALF, smoothstep(this.t / slide));
      else if (this.t >= slide + hold) x = PEEK_HALF * (1 - easeOutBack((this.t - slide - hold) / settle, 1.3));
      this.at = { x: this.peekSide * x, y: 0 };
      this.opacity = lerp(this.startOpacity, 1, smoothstep(this.t / 0.2));
      if (this.t >= slide + hold + settle) {
        this.at = { x: 0, y: 0 };
        this.phase = 'shown';
        if (this.helloWave) {
          this.helloWave = false;
          this.animator.playGesture('wave', { duration: 1.8 });
        }
      }
    } else if (this.phase === 'waving') {
      if (this.t >= WAVE_OUT) {
        this.phase = 'leaving';
        this.t = 0;
      }
    } else if (this.phase === 'leaving') {
      const p = clamp(this.t / LEAVE, 0, 1);
      // A small lift before the drop, like crouching to jump down.
      this.lift = -this.sink * (p < 0.2 ? -0.05 * smoothstep(p / 0.2) : lerp(-0.05, 1, smoothstep((p - 0.2) / 0.8)));
      this.opacity = 1 - smoothstep((p - 0.25) / 0.75);
      if (p >= 1) {
        this.phase = 'hidden';
        this.hiddenSince = now();
        this.events.hidden();
      }
    } else if (this.phase === 'zip-in') {
      const p = clamp(this.t / ZIP_IN, 0, 1);
      // Fast, then a small overshoot past her spot and back: she brakes hard.
      const k = 1 - easeOutBack(p, 1.1);
      this.at = { x: this.zip.start.x * k, y: this.zip.start.y * k };
      this.lift = this.zip.startLift * (1 - smoothstep(p));
      this.opacity = lerp(this.startOpacity, 1, smoothstep(p / 0.35));
      if (p >= 1) {
        this.at = { x: 0, y: 0 };
        this.lift = 0;
        this.zip = null;
        this.phase = 'shown';
        this.events.arrived();
      }
    } else if (this.phase === 'zip-out') {
      const p = clamp(this.t / ZIP_OUT, 0, 1);
      // Off at once and speeding up; she's mostly gone before she fades.
      const k = p * p * ZIP_DISTANCE;
      this.at = { x: this.zip.start.x + this.zip.dir.x * k, y: this.zip.start.y + this.zip.dir.y * k };
      this.opacity = this.startOpacity * (1 - smoothstep((p - 0.3) / 0.7));
      if (p >= 1) {
        this.opacity = 0;
        this.phase = this.hideWhenOut ? 'hidden' : 'away';
        this.hiddenSince = now();
        this.events.zippedOut();
        if (this.hideWhenOut) this.events.hidden();
        this.hideWhenOut = false;
      }
    }
    this.animator.rootY = this.lift;
    this.flight.position.set(this.at.x, this.at.y, 0);
    this.canvas.style.opacity = this.opacity.toFixed(3);
  }
}

function unit(v) {
  const len = v ? Math.hypot(v.x, v.y) : 0;
  return len > 1e-6 && Number.isFinite(len) ? { x: v.x / len, y: v.y / len } : null;
}

function mix(a, b, t) {
  return { x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) };
}
