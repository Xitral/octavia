// Lip sync from the "mouth" messages: five vowel weights (aa ih ou ee oh) about 50 times a second.

import { clamp, damp } from './math.js';

export const VOWELS = ['aa', 'ih', 'ou', 'ee', 'oh'];

// Opening fast and closing a bit slower reads as speech; symmetric smoothing looks like chewing.
const ATTACK = 0.04;
const RELEASE = 0.08;
// Audio messages arrive every ~20 ms; if they stop for this long the audio has stopped.
const SILENCE_AFTER = 0.15;

export class LipSync {
  constructor() {
    this.target = [0, 0, 0, 0, 0];
    this.weights = [0, 0, 0, 0, 0];
    this.targetLevel = 0;
    this.level = 0; // smoothed loudness 0..1, used by the body and expressions
    this.sinceMessage = Infinity;
  }

  push(level, v) {
    if (!Array.isArray(v)) return;
    for (let i = 0; i < VOWELS.length; i++) this.target[i] = clamp(Number(v[i]) || 0, 0, 1);
    // The protocol promises a sum <= 1; enforce it so a bad sender can't over-drive the morphs.
    const sum = this.target.reduce((a, b) => a + b, 0);
    if (sum > 1) this.target.forEach((w, i) => (this.target[i] = w / sum));
    this.targetLevel = clamp(Number(level) || 0, 0, 1);
    // Silence is no sign of talking: only sound keeps "talking" (and its gestures) going.
    if (this.targetLevel > 0 || sum > 0) this.sinceMessage = 0;
  }

  update(dt) {
    this.sinceMessage += dt;
    if (this.sinceMessage > SILENCE_AFTER) {
      this.target.fill(0);
      this.targetLevel = 0;
    }
    for (let i = 0; i < VOWELS.length; i++) {
      const t = this.target[i];
      this.weights[i] = damp(this.weights[i], t, t > this.weights[i] ? ATTACK : RELEASE, dt);
    }
    this.level = damp(this.level, this.targetLevel, this.targetLevel > this.level ? ATTACK : RELEASE * 2, dt);
  }

  get talking() {
    return this.sinceMessage < SILENCE_AFTER;
  }
}
