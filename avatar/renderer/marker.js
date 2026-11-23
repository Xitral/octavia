// The marker: a 2D canvas layered with the WebGL canvas (behind it: she's in front of the screen, so her
// finger may cross the ring and the streak runs behind her). It draws a glowing, pulsing rainbow ring
// around the spot she points at (or a rounded rectangle around the thing, when its extent is known), the
// part of her rainbow streak that falls inside the window when she zips in or out, a sonic rainboom
// bursting out from behind her, the z's drifting up from her head while she naps, a puff of dust where she
// takes off or brakes, and a flash of the ring when the spot she points at is clicked.
//
// It never takes the mouse (pointer-events: none), and the click-through hit test only reads the WebGL
// canvas, so the ring can't keep a click from reaching what it marks.

import { clamp, damp, smoothstep } from './math.js';

export const RING_RADIUS = 26; // px around a point
export const BOX_PAD = 8; // px around a box
// Everything drawn (pulse, ripple, glow) stays within this many px outside the ring or box outline: the
// main process leaves that much room in the window (MARKER_PAD in lib/layout.js).
const REACH = 12;

// Rainbow Dash's mane, red to violet.
const RAINBOW = ['#ee4144', '#f37033', '#fdf6af', '#62bc4d', '#1e98d3', '#672f89'];
const FADE_IN = 0.12; // s (time constants)
const FADE_OUT = 0.1;
const PULSE_HZ = 1.1;
const PULSE = 1.5; // px the ring breathes in and out
const RIPPLE_S = 1.4; // a faint ring spreads out from the marker this often...
const RIPPLE = [3, REACH - 3]; // ...from this far outside the ring to this far (px; its line is 2 px)
const GLOW = REACH - 5; // px of soft light around the ring (its line reaches out 4 px with the pulse)
const SPIN = 0.9; // rad/s the colours travel around the ring
// While pointing is about to end (a countdown), the ring fades to this much of itself and draws in this many px.
const ENDING_ALPHA = 0.3;
const ENDING_SHRINK = 8;
// A sonic rainboom: how long its ring takes to race out and fade (s), and its bands' width (px, all six).
export const BOOM_S = 0.9;
const BOOM_WIDTH = 36;
// Napping: a z floats up from beside her head this often (s), each drifting up for Z_S s as it grows and fades.
const Z_EVERY = 1.3;
const Z_S = 2.8;
const Z_COLOURS = ['#1e98d3', '#672f89'];
// A press: the ring flashes and a bright ring bursts out from it this fast (s) and this far (px).
export const PRESS_FLASH_S = 0.4;
const PRESS_BURST = 34;
// A dust puff: how long it lasts (s), and its balls, as [angle (rad, 0 = right, clockwise on screen), speed]:
// mostly out to the sides and down, like dust kicked up.
export const PUFF_S = 0.55;
const PUFF_BALLS = [[0.2, 1], [0.7, 0.8], [1.3, 0.6], [1.85, 0.8], [2.45, 1], [2.95, 0.7], [3.5, 0.5], [5.9, 0.6]];

export class Marker {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.time = 0;
    this.alpha = 0;
    this.want = 0;
    this.spot = null; // { x, y, box: {x, y, w, h} | null } in window CSS px
    this.streak = null; // { tail, head, width, alpha }
    this.ending = null; // { total, left } s while counting down to the end of pointing
    this.pressed = Infinity; // s since the spot was last clicked
    this.booms = []; // { x, y, reach, t } rainbooms going off
    this.snore = null; // { x, y } where her z's come from while she naps
    this.zs = []; // { x, y, t, i } z's drifting up
    this.puffs = []; // { x, y, size, t } dust puffs
    this.nextZ = 0;
    this.zCount = 0;
    this.drawn = false; // something is on the canvas that a clear would remove
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
  }

  resize() {
    const ratio = Math.min(window.devicePixelRatio, 2);
    this.ratio = ratio;
    this.canvas.width = Math.max(1, Math.round((this.canvas.clientWidth || window.innerWidth) * ratio));
    this.canvas.height = Math.max(1, Math.round((this.canvas.clientHeight || window.innerHeight) * ratio));
    this.drawn = true; // resizing clears it; make sure the next update repaints
  }

  // Marks a spot (window px), with an optional box around the thing. Fades in.
  show(x, y, box = null) {
    this.spot = { x, y, box };
    this.want = 1;
    this.ending = null;
  }

  // Pointing ends in `seconds`: the ring slowly fades and draws in until then, so it's clear it's about to go.
  // A new spot starts it afresh.
  countdown(seconds) {
    if (!this.spot || this.want === 0 || !(seconds > 0)) return;
    this.ending = { total: seconds, left: seconds };
  }

  // The spot was clicked: the ring flashes and a bright ring bursts out of it. Only while a spot is marked.
  press() {
    if (this.spot && this.want > 0) this.pressed = 0;
  }

  // Fades the ring out.
  hide() {
    this.want = 0;
  }

  // Gone at once: she's gone, and the window may be moved or hidden next.
  clear() {
    this.want = 0;
    this.alpha = 0;
    this.spot = null;
    this.streak = null;
    this.ending = null;
    this.booms = [];
    this.snore = null;
    this.zs = [];
    this.puffs = [];
  }

  // The in-window streak for this frame (window px), or null.
  setStreak(streak) {
    this.streak = streak;
  }

  // A sonic rainboom going off at (x, y) (window px): a white flash, then a ring of her six colours with a white
  // shock front racing out `reach` px in BOOM_S, fading as it goes.
  boom(x, y, reach) {
    if (![x, y, reach].every(Number.isFinite) || reach <= 0) return;
    this.booms.push({ x, y, reach, t: 0 });
  }

  // A puff of dust at (x, y) (window px), `size` px across when it's spread out.
  puff(x, y, size) {
    if (![x, y, size].every(Number.isFinite) || size <= 0) return;
    this.puffs.push({ x, y, size, t: 0 });
  }

  // While she naps: where her z's come from (window px), or null when she's awake (the last ones drift off).
  setSnore(p) {
    this.snore = p && Number.isFinite(p.x) && Number.isFinite(p.y) ? { x: p.x, y: p.y } : null;
  }

  // Something is (or is about to be) on screen.
  get busy() {
    const drawing = this.streak !== null || this.booms.length > 0 || this.zs.length > 0 || this.snore !== null || this.puffs.length > 0;
    return this.alpha > 0.002 || this.want > 0 || drawing;
  }

  update(dt) {
    this.time += dt;
    // Moved onto a monitor with other scaling: no resize event comes, but the backing store must follow.
    if (Math.min(window.devicePixelRatio, 2) !== this.ratio) this.resize();
    this.alpha = damp(this.alpha, this.want, this.want > this.alpha ? FADE_IN : FADE_OUT, dt);
    if (this.want === 0 && this.alpha < 0.002) {
      this.alpha = 0;
      this.spot = null;
      this.ending = null;
    }
    if (this.ending) this.ending.left = Math.max(0, this.ending.left - dt);
    this.pressed += dt;
    for (const b of this.booms) b.t += dt;
    this.booms = this.booms.filter((b) => b.t < BOOM_S);
    this.nextZ -= dt;
    if (this.snore && this.nextZ <= 0) {
      this.zs.push({ ...this.snore, t: 0, i: this.zCount++ });
      this.nextZ = Z_EVERY;
    }
    for (const z of this.zs) z.t += dt;
    this.zs = this.zs.filter((z) => z.t < Z_S);
    for (const p of this.puffs) p.t += dt;
    this.puffs = this.puffs.filter((p) => p.t < PUFF_S);
    if (!this.busy && !this.drawn) return;
    const { ctx } = this;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.drawn = false;
    if (!this.busy) return;
    ctx.setTransform(this.ratio, 0, 0, this.ratio, 0, 0);
    for (const b of this.booms) this.drawBoom(b);
    for (const z of this.zs) this.drawZ(z);
    for (const p of this.puffs) this.drawPuff(p);
    if (this.streak) this.drawStreak(this.streak);
    if (this.spot && this.alpha > 0.002) this.drawRing(this.spot);
    this.drawn = true;
  }

  // A conic rainbow centred on (x, y) that slowly turns, so the ring shimmers.
  rainbow(x, y) {
    const g = this.ctx.createConicGradient(this.time * SPIN, x, y);
    RAINBOW.forEach((c, i) => g.addColorStop(i / RAINBOW.length, c));
    g.addColorStop(1, RAINBOW[0]);
    return g;
  }

  // The marker's outline, grown by `grow` px.
  outline(spot, grow) {
    const { ctx } = this;
    ctx.beginPath();
    if (spot.box) {
      const b = spot.box;
      const pad = BOX_PAD + grow;
      ctx.roundRect(b.x - pad, b.y - pad, b.w + 2 * pad, b.h + 2 * pad, 10 + grow * 0.5);
    } else {
      ctx.arc(spot.x, spot.y, Math.max(1, RING_RADIUS + grow), 0, 2 * Math.PI);
    }
  }

  drawRing(spot) {
    const { ctx } = this;
    const pulse = Math.sin(2 * Math.PI * PULSE_HZ * this.time);
    const ended = this.ending ? smoothstep(1 - this.ending.left / this.ending.total) : 0;
    const a = this.alpha * (1 - (1 - ENDING_ALPHA) * ended);
    const inset = ENDING_SHRINK * ended;
    const cx = spot.box ? spot.box.x + spot.box.w / 2 : spot.x;
    const cy = spot.box ? spot.box.y + spot.box.h / 2 : spot.y;
    ctx.save();

    // A ripple that spreads out and fades, like a sonar ping.
    const r = (this.time % RIPPLE_S) / RIPPLE_S;
    ctx.globalAlpha = a * 0.6 * (1 - smoothstep(r));
    ctx.lineWidth = 2;
    ctx.strokeStyle = this.rainbow(cx, cy);
    this.outline(spot, RIPPLE[0] + (RIPPLE[1] - RIPPLE[0]) * r - inset);
    ctx.stroke();

    // Dark and light halos keep the ring readable on any background, then the rainbow on top.
    const grow = PULSE * pulse - inset;
    ctx.globalAlpha = a * 0.45;
    ctx.lineWidth = 8;
    ctx.strokeStyle = 'rgba(20, 16, 40, 0.8)';
    this.outline(spot, grow);
    ctx.stroke();
    ctx.globalAlpha = a;
    ctx.shadowColor = 'rgba(255, 255, 255, 0.9)';
    const flash = this.pressed < PRESS_FLASH_S ? 1 - this.pressed / PRESS_FLASH_S : 0;
    ctx.shadowBlur = GLOW + pulse + 10 * flash;
    ctx.lineWidth = 4.5 + 2.5 * flash;
    ctx.strokeStyle = this.rainbow(cx, cy);
    this.outline(spot, grow);
    ctx.stroke();
    if (flash > 0) {
      // The burst: a white ring racing out from the marker as it fades.
      const p = 1 - flash;
      ctx.globalAlpha = a * flash;
      ctx.shadowBlur = 8;
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
      this.outline(spot, grow + PRESS_BURST * (1 - (1 - p) ** 2));
      ctx.stroke();
    }
    ctx.restore();
  }

  // Soft grey-white wisps bursting out and swelling as they fade: dust kicked up. Each is a ball of dust thinning out
  // to nothing at its edge, a little warmer and darker underneath (outlined balls read as soap bubbles).
  drawPuff({ x, y, size, t }) {
    const { ctx } = this;
    const p = clamp(t / PUFF_S, 0, 1);
    const out = 1 - (1 - p) ** 3;
    ctx.save();
    ctx.globalAlpha = 0.8 * (1 - p) ** 1.5;
    for (const [angle, speed] of PUFF_BALLS) {
      const bx = x + Math.cos(angle) * size * 0.45 * speed * out;
      const by = y + Math.sin(angle) * size * 0.3 * speed * out;
      const r = size * (0.1 + 0.14 * out) * (0.7 + 0.3 * speed);
      const dust = ctx.createRadialGradient(bx - r * 0.2, by - r * 0.25, r * 0.05, bx, by, r);
      dust.addColorStop(0, 'rgba(246, 244, 250, 0.95)');
      dust.addColorStop(0.55, 'rgba(222, 218, 232, 0.7)');
      dust.addColorStop(1, 'rgba(200, 194, 214, 0)');
      ctx.fillStyle = dust;
      ctx.beginPath();
      ctx.arc(bx, by, r, 0, 2 * Math.PI);
      ctx.fill();
    }
    ctx.restore();
  }

  // A z drifting up and away from her head, swaying a little, growing, fading in and out; blue and violet by
  // turns, outlined in white so it reads on any background.
  drawZ({ x, y, t, i }) {
    const { ctx } = this;
    const p = clamp(t / Z_S, 0, 1);
    const size = 12 + 14 * p;
    const alpha = Math.min(1, p * 6) * (1 - smoothstep((p - 0.55) / 0.45));
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.font = `bold ${size.toFixed(1)}px "Segoe UI", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const zx = x + 30 * p + 5 * Math.sin(p * 7 + i);
    const zy = y - 58 * p;
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
    ctx.strokeText('z', zx, zy);
    ctx.fillStyle = Z_COLOURS[i % Z_COLOURS.length];
    ctx.fillText('z', zx, zy);
    ctx.restore();
  }

  // A white flash at the heart, then six rainbow bands with a white shock front racing out and fading.
  drawBoom({ x, y, reach, t }) {
    const { ctx } = this;
    const p = clamp(t / BOOM_S, 0, 1);
    const r = reach * (1 - (1 - p) ** 3); // off like a shot, slowing as it spreads
    const fade = 1 - smoothstep((p - 0.3) / 0.7);
    ctx.save();
    if (p < 0.35) {
      const flash = ctx.createRadialGradient(x, y, 0, x, y, Math.max(1, reach * 0.25));
      const a = 0.85 * (1 - smoothstep(p / 0.35));
      flash.addColorStop(0, `rgba(255, 255, 255, ${a})`);
      flash.addColorStop(1, 'rgba(255, 255, 255, 0)');
      ctx.fillStyle = flash;
      ctx.fillRect(x - reach, y - reach, 2 * reach, 2 * reach);
    }
    const band = BOOM_WIDTH / RAINBOW.length;
    ctx.globalAlpha = fade;
    RAINBOW.forEach((colour, i) => {
      const radius = r - (i + 0.5) * band;
      if (radius <= 0) return;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.lineWidth = band + 0.5; // a hair of overlap, so no seam shows between bands
      ctx.strokeStyle = colour;
      ctx.stroke();
    });
    ctx.beginPath();
    ctx.arc(x, y, r + 2, 0, 2 * Math.PI);
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
    ctx.shadowColor = 'rgba(255, 255, 255, 0.9)';
    ctx.shadowBlur = 12;
    ctx.stroke();
    ctx.restore();
  }

  // Six stripes side by side from tail to head, narrowing and fading toward the tail.
  drawStreak({ tail, head, width, alpha }) {
    const dx = head.x - tail.x;
    const dy = head.y - tail.y;
    const len = Math.hypot(dx, dy);
    if (len < 1 || alpha <= 0.002) return;
    const nx = -dy / len;
    const ny = dx / len;
    const { ctx } = this;
    ctx.save();
    ctx.globalAlpha = clamp(alpha, 0, 1);
    const stripe = width / RAINBOW.length;
    RAINBOW.forEach((colour, i) => {
      const off = -width / 2 + i * stripe;
      const g = ctx.createLinearGradient(tail.x, tail.y, head.x, head.y);
      g.addColorStop(0, `${colour}00`);
      g.addColorStop(0.6, `${colour}cc`);
      g.addColorStop(1, colour);
      ctx.fillStyle = g;
      ctx.beginPath();
      // Full width at the head, a third of it at the tail.
      ctx.moveTo(head.x + nx * off, head.y + ny * off);
      ctx.lineTo(head.x + nx * (off + stripe), head.y + ny * (off + stripe));
      ctx.lineTo(tail.x + nx * (off + stripe) * 0.33, tail.y + ny * (off + stripe) * 0.33);
      ctx.lineTo(tail.x + nx * off * 0.33, tail.y + ny * off * 0.33);
      ctx.closePath();
      ctx.fill();
    });
    // A rounded, bright head.
    ctx.globalAlpha = clamp(alpha, 0, 1) * 0.9;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.shadowColor = 'rgba(255, 255, 255, 0.9)';
    ctx.shadowBlur = width * 0.3;
    ctx.beginPath();
    ctx.arc(head.x, head.y, width * 0.28, 0, 2 * Math.PI);
    ctx.fill();
    ctx.restore();
  }
}
