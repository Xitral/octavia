// Facial expressions: emotions, blinking and the mouth, all written through the VRM expression manager.
//
// VRoid models carry raw face morphs (Fcl_BRW_* brows, Fcl_EYE_* eyes, Fcl_MTH_* mouth) besides the VRM
// presets. Building emotions from those parts lets the mouth part step back while she talks, so vowels stay
// readable, and allows one-sided looks like a smug half-wink. Any other VRM falls back to the presets.

import { VRMExpression, VRMExpressionMorphTargetBind } from '@pixiv/three-vrm';
import { VOWELS } from './lipsync.js';
import { Noise, clamp, damp, rand } from './math.js';

// Raw recipes use morph names without the "Fcl_" prefix. Weights stay below full strength (full VRoid
// morphs look cartoonish on a desktop companion), and bangs often hide the brows, so the eyes and mouth
// carry most of each look. `eyesClosed` says how far the emotion already narrows the eyes, so
// a blink on top doesn't push the lids through the face.
const EMOTIONS = {
  neutral: { raw: {}, preset: {}, eyesClosed: 0 },
  happy: { raw: { BRW_Joy: 0.7, EYE_Joy: 0.55, MTH_Fun: 0.8, MTH_Joy: 0.25 }, preset: { happy: 0.85 }, eyesClosed: 0.55 },
  angry: { raw: { BRW_Angry: 1, EYE_Angry: 0.8, MTH_Angry: 0.7 }, preset: { angry: 0.85 }, eyesClosed: 0.3 },
  sad: { raw: { BRW_Sorrow: 1, EYE_Sorrow: 0.85, MTH_Sorrow: 0.75 }, preset: { sad: 0.85 }, eyesClosed: 0.4 },
  relaxed: { raw: { BRW_Fun: 0.5, EYE_Fun: 0.5, MTH_Fun: 0.55 }, preset: { relaxed: 0.75 }, eyesClosed: 0.3 },
  surprised: { raw: { BRW_Surprised: 1, EYE_Surprised: 0.8, MTH_Surprised: 0.7 }, preset: { surprised: 0.85 }, eyesClosed: 0 },
  excited: {
    raw: { BRW_Surprised: 0.5, BRW_Joy: 0.4, EYE_Spread: 0.45, MTH_Joy: 0.75 },
    preset: { happy: 0.7, surprised: 0.3 },
    eyesClosed: 0,
  },
  smug: {
    raw: { BRW_Fun: 0.4, BRW_Angry: 0.15, EYE_Joy_R: 0.75, EYE_Fun: 0.35, MTH_Fun: 0.7, MTH_SkinFung_L: 0.7 },
    preset: { happy: 0.45, relaxed: 0.3, blinkRight: 0.35 },
    eyesClosed: 0.45,
  },
  laughing: { raw: { BRW_Joy: 0.9, EYE_Joy: 0.95, MTH_Joy: 0.9 }, preset: { happy: 1 }, eyesClosed: 0.95 },
  curious: { raw: { BRW_Surprised: 0.6, EYE_Spread: 0.3, MTH_Small: 0.5 }, preset: { surprised: 0.4 }, eyesClosed: 0 },
  playful: { raw: { BRW_Joy: 0.5, EYE_Fun: 0.4, MTH_Fun: 0.75, MTH_SkinFung: 0.8 }, preset: { happy: 0.65 }, eyesClosed: 0.25 },
  thinking: { raw: { BRW_Angry: 0.25, BRW_Sorrow: 0.3, EYE_Fun: 0.25, MTH_Small: 0.5, MTH_Down: 0.35 }, preset: { relaxed: 0.3 }, eyesClosed: 0.15 },
};

// The resting face for each state, used whenever no message asked for something else.
const STATE_EMOTION = {
  idle: ['relaxed', 0.35],
  listening: ['curious', 0.35],
  transcribing: ['thinking', 0.6],
  thinking: ['thinking', 0.8],
  speaking: ['happy', 0.15],
  confirming: ['curious', 0.7],
  stopped: ['neutral', 0],
};

const ATTACK_TAU = 0.15;
const RELEASE_TAU = 0.5; // eases back to the state's face in about 1.5 s

// Blink timing: closing is quicker than opening, like real eyelids.
const BLINK_CLOSE = 0.06;
const BLINK_HOLD = 0.03;
const BLINK_OPEN = 0.12;

function collectMorphNames(root) {
  const names = new Set();
  root.traverse((o) => {
    if (o.isMesh && o.morphTargetDictionary) Object.keys(o.morphTargetDictionary).forEach((n) => names.add(n));
  });
  return names;
}

// Registers an expression that drives one raw morph target on every mesh that has it.
function registerRawExpression(vrm, morph) {
  const expression = new VRMExpression(`raw:${morph}`);
  vrm.scene.traverse((o) => {
    if (o.isMesh && o.morphTargetDictionary && morph in o.morphTargetDictionary) {
      expression.addBind(new VRMExpressionMorphTargetBind({ primitives: [o], index: o.morphTargetDictionary[morph], weight: 1 }));
    }
  });
  vrm.expressionManager.registerExpression(expression);
}

export class Expressions {
  constructor(vrm) {
    this.manager = vrm.expressionManager;
    this.noise = new Noise();
    this.time = 0;

    // Per emotion: the channels to drive, as { channel: [weight, isMouth] }.
    const available = collectMorphNames(vrm.scene);
    this.recipes = {};
    const registered = new Set();
    for (const [name, def] of Object.entries(EMOTIONS)) {
      const morphs = Object.keys(def.raw).map((k) => `Fcl_${k}`);
      const useRaw = morphs.length > 0 && morphs.every((m) => available.has(m));
      const channels = {};
      if (useRaw) {
        for (const [k, w] of Object.entries(def.raw)) {
          const morph = `Fcl_${k}`;
          if (!registered.has(morph)) {
            registerRawExpression(vrm, morph);
            registered.add(morph);
          }
          channels[`raw:${morph}`] = [w, k.startsWith('MTH_')];
        }
      } else {
        // Presets move the whole face, mouth included.
        for (const [k, w] of Object.entries(def.preset)) if (this.manager.getExpression(k)) channels[k] = [w, true];
      }
      this.recipes[name] = { channels, eyesClosed: def.eyesClosed };
    }
    this.usesRawMorphs = registered.size > 0;
    this.channels = [...new Set(Object.values(this.recipes).flatMap((r) => Object.keys(r.channels)))];
    this.weights = Object.fromEntries(this.channels.map((c) => [c, 0]));
    this.eyesClosed = 0;

    this.stateEmotion = STATE_EMOTION.idle;
    this.active = null; // { name, intensity, until } from a segment or an emotion message

    this.nextBlink = rand(1, 3);
    this.blinkStart = -1;
    this.doubleBlink = false;
    this.nextWink = rand(2, 4);
    this.winkStart = -1;
    this.winkSide = 'blinkRight';
    this.asleep = false; // napping (nap.js): eyes shut
    this.sleep = 0; // how shut they are for it
    this.state = 'idle';
    this.energy = { sleepy: 0, perky: 0 }; // from the clock (daytime.js)
    this.mood = null; // { name, intensity, until }: a resting face that carries over (setMood)
  }

  // How awake she is (daytime.js): sleepy, her resting eyelids droop and her blinks slow down; perky, her resting
  // face is brighter.
  setEnergy({ sleepy = 0, perky = 0 } = {}) {
    this.energy = { sleepy: clamp(sleepy, 0, 1), perky: clamp(perky, 0, 1) };
  }

  // A mood that carries over after a conversation: her resting face (idle or listening, nothing else asking for a
  // face) shows it until `seconds` from now: a little happy after a thank-you, smug after a tease.
  setMood(name, intensity, seconds) {
    if (!Object.hasOwn(EMOTIONS, name) || !(seconds > 0)) {
      this.mood = null;
      return;
    }
    const i = Number.isFinite(intensity) ? clamp(intensity, 0, 1) : 0.4;
    this.mood = { name, intensity: i, until: this.time + seconds };
  }

  // The face when nothing asks for one: a mood carried over, or the state's, brighter when she's perky.
  get restingFace() {
    const resting = this.state === 'idle' || this.state === 'listening';
    if (resting && this.mood && this.time < this.mood.until) return [this.mood.name, this.mood.intensity];
    if (this.state === 'idle' && this.energy.perky > 0.5) return ['happy', 0.2 + 0.15 * this.energy.perky];
    return this.stateEmotion;
  }

  // Asleep: her eyes close (slowly, or at once with `now`) and stay shut, no blinking; awake, they open at once.
  setAsleep(asleep, now = false) {
    this.asleep = asleep === true;
    if (!this.asleep || now) this.sleep = this.asleep ? 1 : 0;
  }

  setState(state) {
    this.state = Object.hasOwn(STATE_EMOTION, state) ? state : 'idle';
    this.stateEmotion = STATE_EMOTION[this.state];
  }

  // Shows an emotion until `seconds` from now. Unknown names count as neutral, per the protocol.
  show(name, intensity, seconds) {
    const known = Object.hasOwn(EMOTIONS, name) ? name : 'neutral';
    const i = Number.isFinite(intensity) ? clamp(intensity, 0, 1) : 0.8;
    this.active = { name: known, intensity: i, until: this.time + Math.max(0, seconds) };
  }

  // The sentence that set the emotion is over; ease back to the state's face.
  release() {
    if (this.active) this.active.until = Math.min(this.active.until, this.time);
  }

  get currentEmotion() {
    return this.active ? this.active.name : this.restingFace[0];
  }

  update(dt, lipsync) {
    this.time += dt;
    if (this.active && this.time >= this.active.until) this.active = null;
    if (this.mood && this.time >= this.mood.until) this.mood = null;
    const [name, intensity] = this.active ? [this.active.name, this.active.intensity] : this.restingFace;
    const recipe = this.recipes[name];

    // A slow breath of variation so a held expression never looks frozen.
    const alive = 1 + 0.07 * this.noise.at(this.time * 0.35);
    // While the mouth is busy with vowels, emotions keep only a hint of their own mouth shape.
    const talk = clamp(lipsync.level * 1.8, 0, 1);
    const mouthScale = this.usesRawMorphs ? 1 - 0.75 * talk : 1 - 0.35 * talk;
    const tau = this.active ? ATTACK_TAU : RELEASE_TAU;

    for (const channel of this.channels) {
      const entry = recipe.channels[channel];
      let target = 0;
      if (entry) {
        const [w, isMouth] = entry;
        target = clamp(w * intensity * alive * (isMouth ? mouthScale : 1), 0, 1);
      }
      this.weights[channel] = damp(this.weights[channel], target, tau, dt);
      this.manager.setValue(channel, this.weights[channel]);
    }
    this.eyesClosed = damp(this.eyesClosed, recipe.eyesClosed * intensity, tau, dt);

    lipsync.weights.forEach((w, i) => this.manager.setValue(VOWELS[i], w));
    if (this.asleep) this.sleep = damp(this.sleep, 1, 0.45, dt);
    this.updateBlink();
    this.updateWink(name === 'playful');
  }

  // A blink's lid, 0 open to 1 shut, `t` s in: slower when she's sleepy (by `slow`).
  blinkCurve(t, slow = 1) {
    if (t <= 0) return 0;
    if (t < BLINK_CLOSE * slow) return t / (BLINK_CLOSE * slow);
    if (t < (BLINK_CLOSE + BLINK_HOLD) * slow) return 1;
    return clamp(1 - (t - (BLINK_CLOSE + BLINK_HOLD) * slow) / (BLINK_OPEN * slow), 0, 1);
  }

  // Also used by the animator: people tend to blink when their gaze jumps.
  blinkNow() {
    if (this.blinkStart < 0) this.nextBlink = this.time;
  }

  updateBlink() {
    if (this.blinkStart < 0 && this.time >= this.nextBlink && !this.asleep) {
      this.blinkStart = this.time;
      this.doubleBlink = Math.random() < 0.15;
    }
    let value = 0;
    const { sleepy } = this.energy;
    const slow = 1 + 1.5 * sleepy; // heavy, slow blinks late at night
    if (this.blinkStart >= 0) {
      const t = this.time - this.blinkStart;
      const one = (BLINK_CLOSE + BLINK_HOLD + BLINK_OPEN) * slow;
      value = this.blinkCurve(t, slow);
      if (this.doubleBlink && t >= one) value = this.blinkCurve(t - one - 0.05, slow);
      if (t >= (this.doubleBlink ? 2 * one + 0.05 : one)) {
        this.blinkStart = -1;
        this.nextBlink = this.time + rand(2.5, 6);
      }
    }
    // Drowsy: the lids hang a little lower while she rests (not while she talks or listens hard).
    const drowse = this.state === 'idle' || this.state === 'stopped' ? 0.3 * sleepy : 0.12 * sleepy;
    this.manager.setValue('blink', Math.max(value, this.sleep, drowse) * (1 - this.eyesClosed));
  }

  updateWink(playful) {
    let value = 0;
    if (this.winkStart >= 0) {
      const t = this.time - this.winkStart;
      value = Math.min(clamp(t / 0.08, 0, 1), clamp((0.45 - t) / 0.15, 0, 1));
      if (t > 0.45) {
        this.winkStart = -1;
        this.nextWink = this.time + rand(3.5, 7);
      }
    } else if (playful && this.time >= this.nextWink) {
      this.winkStart = this.time;
      this.winkSide = Math.random() < 0.5 ? 'blinkLeft' : 'blinkRight';
    }
    // Added on top: an emotion recipe may already use one eye (the preset smug does).
    for (const side of ['blinkLeft', 'blinkRight']) {
      const base = this.weights[side] || 0;
      this.manager.setValue(side, clamp(base + (this.winkSide === side ? value : 0), 0, 1));
    }
  }
}
