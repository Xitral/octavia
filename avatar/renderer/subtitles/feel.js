// Her subtitles' share of her state, with no DOM in it (tested in node): the feeling a sentence carries (the orb's own
// FEELS, so the words lean the way she does) and her voice as she says it, eased slowly into a steady glow (the user
// found light pulsing with each syllable distracting, 2026-10-27), with the haze drifting only as she speaks.

import { FEELS } from '../orb-motion.js';
import { clamp, damp } from '../math.js';

const VOICE_RISE = 0.8; // s: her glow comes up over her first words and holds through the syllables between...
const VOICE_FALL = 0.6; // ...and fades softly after, so nothing flickers in the corner of the eye
const SILENCE_S = 0.15; // no mouth message this long: her voice has stopped
export const HAZE_WRAP = 96; // px: the haze's noise tile, so its drift wraps without a seam
const LEAN = 0.4; // the most of her glow that leans cyan (light) or violet (heavy)

// A feeling by name at `intensity` 0..1: {lift, energy} as the orb takes it (orb-motion.js feelFor). An unknown one, or
// none, is no feeling.
export function feelOf(emotion, intensity) {
  const f = FEELS[emotion];
  const k = Number.isFinite(intensity) ? clamp(intensity, 0, 1) : 0.5;
  if (!f || k <= 0) return { lift: 0, energy: 0 };
  const scale = 0.35 + 0.65 * k; // even a slight feeling shows a little
  return { lift: f.lift * scale, energy: f.energy * scale };
}

// How far her glow leans cyan or violet for a lift: never a colour of its own, only a lean of her hazy blue.
export function leanOf(lift) {
  return { cyan: LEAN * clamp(lift, 0, 1), violet: LEAN * clamp(-lift, 0, 1) };
}

export class Voice {
  constructor() {
    this.level = 0; // the last level she sent
    this.voice = 0; // eased slowly: a steady glow while she talks, never a flicker per syllable
    this.sinceMouth = Infinity;
    this.haze = 0; // px the haze has drifted (HAZE_WRAP)
  }

  set(level) {
    this.level = Number.isFinite(level) ? clamp(level, 0, 1) : 0;
    this.sinceMouth = 0;
  }

  // Nothing left to move: the page stops drawing until she speaks again.
  get calm() {
    return this.sinceMouth > SILENCE_S && this.voice < 0.004;
  }

  // Advance `dt` s.
  tick(dt) {
    this.sinceMouth += dt;
    const level = this.sinceMouth > SILENCE_S ? 0 : this.level;
    this.voice = damp(this.voice, level, level > this.voice ? VOICE_RISE : VOICE_FALL, dt);
    // Integrated, never time times a changing rate: it drifts as she talks and stops when she does.
    this.haze = (this.haze + 26 * this.voice * dt) % HAZE_WRAP;
  }
}
