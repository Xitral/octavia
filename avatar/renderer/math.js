// Small numeric helpers shared by the animation modules.

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const rand = (lo, hi) => lo + Math.random() * (hi - lo);
export const pick = (items) => items[Math.floor(Math.random() * items.length)];

// Frame-rate independent exponential approach: after `tau` seconds, ~63% of the way to the target.
export const damp = (current, target, tau, dt) => (tau <= 0 ? target : target + (current - target) * Math.exp(-dt / tau));
export const dampFactor = (tau, dt) => (tau <= 0 ? 1 : 1 - Math.exp(-dt / tau));

export const smoothstep = (t) => {
  const x = clamp(t, 0, 1);
  return x * x * (3 - 2 * x);
};

export const easeInOut = (t) => {
  const x = clamp(t, 0, 1);
  return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2;
};

// Ends a little past 1 and settles back: the "pop" in pop-in.
export const easeOutBack = (t, overshoot = 1.4) => {
  const x = clamp(t, 0, 1) - 1;
  return 1 + (overshoot + 1) * x * x * x + overshoot * x * x;
};

// Rises from 0 to 1 over `attack` seconds, holds, and falls back to 0 over the last `release` seconds.
export function envelope(t, duration, attack, release) {
  if (t <= 0 || t >= duration) return 0;
  return Math.min(smoothstep(t / attack), smoothstep((duration - t) / release));
}

// A bump that rises and falls once over [start, end] (0 outside).
export function bump(t, start, end) {
  if (t <= start || t >= end) return 0;
  return Math.sin(Math.PI * ((t - start) / (end - start)));
}

// Like bump, but it also eases into and out of the rise (sin²): no kick at either end, so pulses that follow one
// another read as one smooth motion rather than a jolt each.
export function softBump(t, start, end) {
  return bump(t, start, end) ** 2;
}

// 1D gradient noise, summed over two octaves: smooth, non-repeating wobble in about [-1, 1]. Each
// instance gets its own random gradients so different body parts don't move in lockstep.
export class Noise {
  constructor(size = 256) {
    this.grad = Float32Array.from({ length: size }, () => Math.random() * 2 - 1);
    this.size = size;
  }

  sample(x) {
    const i = Math.floor(x);
    const f = x - i;
    const g0 = this.grad[((i % this.size) + this.size) % this.size];
    const g1 = this.grad[(((i + 1) % this.size) + this.size) % this.size];
    const u = f * f * f * (f * (f * 6 - 15) + 10);
    return 2 * lerp(g0 * f, g1 * (f - 1), u);
  }

  at(t) {
    return clamp(this.sample(t) * 0.8 + this.sample(t * 2.3 + 17.1) * 0.35, -1, 1);
  }
}
