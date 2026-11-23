// Napping on a cloud. Left idle on screen for `napAfterS` (`always` mode) she dozes off: a little cloud puffs up
// in front of her, she floats up to it (legs tucked), folds her arms on it and rests her head on them, eyes shut,
// breathing slow, z's drifting up. Anything that isn't idle wakes her: a jolt, eyes wide, and the cloud puffs
// away. Called back after a break at least that long (`conversation`
// mode hides her when idle, so that's the only nap she gets there), she's caught napping: she comes up asleep on
// her cloud and wakes up a moment later, whatever's going on (you're already talking to her), unless she has to
// do something first (a moment, pointing).
//
// Usage (the renderer wires it):
//   const nap = new Nap({ animator, expressions, vrm, napAfterS: cfg.napAfterS });
//   nap.shown(away);                    // she's shown after `away` s out of sight (Infinity: never shown yet)
//   nap.update(dt, { idle, visible, busy }); // every frame, before the animator (see update)
//   nap.snore                           // where her z's come from (world, by her head) while asleep, else null
//   nap.asleep, nap.dozing              // asleep (or falling asleep); the cloud is out

import * as THREE from 'three';
import { toonGradient } from './props.js';
import { clamp, easeOutBack, smoothstep } from './math.js';

export const NAP_WAKE_DELAY_S = 0.8; // caught napping: how long she sleeps on once she's back in view
const PUFF_IN_S = 0.7; // the cloud puffing up as she dozes off
const PUFF_OUT_S = 0.3; // ...and away when she wakes

// A puffy cloud about `width` m across: a cluster of toon-shaded balls, flatter than it's wide, flat-ish bottom.
function cloud(width, material, name) {
  const group = new THREE.Group();
  group.name = name;
  // [x, y, z, r] as fractions of the width
  const balls = [
    [0, 0.05, 0, 0.3],
    [-0.28, -0.02, 0.02, 0.22],
    [0.28, -0.01, -0.02, 0.23],
    [-0.12, 0.16, -0.04, 0.22],
    [0.15, 0.14, 0.03, 0.2],
    [-0.42, -0.07, -0.02, 0.14],
    [0.43, -0.06, 0.03, 0.15],
    [0.02, -0.06, 0.12, 0.2],
  ];
  const geometry = new THREE.SphereGeometry(1, 20, 14);
  for (const [x, y, z, r] of balls) {
    const ball = new THREE.Mesh(geometry, material);
    ball.position.set(x * width, y * width, z * width);
    ball.scale.set(r * width, r * width * 0.8, r * width * 0.9);
    ball.frustumCulled = false;
    group.add(ball);
  }
  return group;
}

export class Nap {
  // `animator`: plays the nap pose and floats her. `expressions`: shuts her eyes. `vrm`: where the clouds go (they
  // move with her). `napAfterS`: idle this long on screen before she dozes off, and away this long before she's
  // caught napping; 0 = never.
  constructor({ animator, expressions, vrm, napAfterS = 0 }) {
    this.animator = animator;
    this.expressions = expressions;
    this.napAfterS = Number.isFinite(napAfterS) && napAfterS > 0 ? napAfterS : 0;
    this.phase = 'awake'; // awake | dozing (falling asleep) | asleep | waking (clouds puffing away)
    this.idleFor = 0; // s of nothing going on, on screen
    this.t = 0; // s in this phase
    this.wakeIn = Infinity; // caught napping: s until she wakes by herself
    this.gesture = null; // the nap pose
    this.head = vrm?.humanoid?.getNormalizedBoneNode('head') ?? null;

    // The cloud she rests her arms on, just below her chest in front of her (it moves with her). Hidden until she
    // naps.
    this.gradient = toonGradient();
    this.material = new THREE.MeshToonMaterial({ color: 0xffffff, gradientMap: this.gradient });
    const chest = vrm?.humanoid?.getNormalizedBoneNode('chest') ?? vrm?.humanoid?.getNormalizedBoneNode('hips') ?? null;
    const chestY = chest ? chest.getWorldPosition(new THREE.Vector3()).y : 1.1;
    this.clouds = cloud(0.56, this.material, 'nap-cloud');
    this.clouds.position.set(0, chestY + PILLOW[0], PILLOW[1]);
    this.clouds.visible = false;
    vrm?.scene?.add(this.clouds);
  }

  get asleep() {
    return this.phase === 'asleep';
  }

  // Asleep or falling asleep: she floats, her eyes are shut or shutting.
  get dozing() {
    return this.phase === 'dozing' || this.phase === 'asleep';
  }

  // Where her z's come from while she's asleep (world, just above and to the side of her head), else null.
  get snore() {
    if (this.phase !== 'asleep' || !this.head) return null;
    return this.head.getWorldPosition(_head).add(_snoreOffset);
  }

  // She's back in view after `away` s out of sight: a break as long as a nap means she was napping. Not the first
  // time she's shown (she wasn't anywhere before that). Whether she was caught napping.
  shown(away) {
    if (!this.napAfterS || !Number.isFinite(away) || away < this.napAfterS) return false;
    this.fallAsleep({ now: true });
    this.wakeIn = NAP_WAKE_DELAY_S;
    return true;
  }

  // Every frame, before the animator. `idle`: nothing at all is going on (the idle state, standing at home, no
  // activity, moment or pointing); `visible`: she's on screen; `busy`: she has something to do (an activity, a
  // moment, pointing). A nap ends with anything but idle; being caught napping only ends early for `busy`.
  update(dt, { idle, visible, busy = false }) {
    this.t += dt;
    if (this.phase === 'awake') {
      this.idleFor = idle && visible ? this.idleFor + dt : 0;
      if (this.napAfterS && this.idleFor >= this.napAfterS) this.fallAsleep();
    } else if (this.phase === 'dozing' || this.phase === 'asleep') {
      this.wakeIn -= dt;
      const caught = Number.isFinite(this.wakeIn);
      if (!visible) this.wake({ now: true });
      else if (busy || (caught ? this.wakeIn <= 0 : !idle)) this.wake();
      else if (this.phase === 'dozing' && this.t >= PUFF_IN_S * 1.8) this.phase = 'asleep';
    } else if (this.phase === 'waking' && this.t >= PUFF_OUT_S) {
      this.phase = 'awake';
    }
    this.animateClouds();
  }

  fallAsleep({ now = false } = {}) {
    if (this.dozing) return;
    this.phase = now ? 'asleep' : 'dozing';
    this.t = now ? PUFF_IN_S : 0;
    this.idleFor = 0;
    this.wakeIn = Infinity;
    this.gesture = this.animator.playGesture('nap');
    if (now && this.gesture) this.gesture.t = this.gesture.def.fadeIn; // already curled up as she comes into view
    this.expressions?.setAsleep?.(true, now);
    this.expressions?.show?.('relaxed', 0.45, 1e6);
  }

  // Awake: a jolt, eyes wide, and the clouds puff away. `now`: she's out of sight, so it's all gone at once.
  wake({ now = false } = {}) {
    if (!this.dozing) return;
    if (this.gesture) {
      if (now) this.animator.gestures.drop(this.gesture);
      else this.animator.gestures.releaseOne(this.gesture);
    }
    this.gesture = null;
    this.expressions?.setAsleep?.(false);
    this.expressions?.release?.();
    this.wakeIn = Infinity;
    this.idleFor = 0;
    if (now) {
      this.phase = 'awake';
      this.t = 0;
      this.animateClouds();
      return;
    }
    this.animator.playGesture('surprised_jolt');
    this.expressions?.show?.('surprised', 0.8, 1.1);
    this.phase = 'waking';
    this.t = 0;
  }

  // The cloud puffs up as she dozes off (breathing a little itself while she sleeps), and swells and pops as she
  // wakes, like a soap bubble.
  animateClouds() {
    let scale = 0;
    if (this.phase === 'dozing') scale = easeOutBack(clamp(this.t / PUFF_IN_S, 0, 1), 1.6);
    else if (this.phase === 'asleep') scale = 1 + 0.015 * Math.sin(this.t * 1.3);
    else if (this.phase === 'waking') {
      const p = clamp(this.t / PUFF_OUT_S, 0, 1);
      scale = p < 0.3 ? 1 + 0.15 * smoothstep(p / 0.3) : 1.15 * (1 - smoothstep((p - 0.3) / 0.7));
    }
    this.clouds.visible = scale > 1e-3;
    this.clouds.scale.setScalar(Math.max(scale, 1e-3));
  }

  // Warms up the clouds' shader, so the first nap doesn't stutter.
  compile(renderer, camera, scene) {
    const shown = this.clouds.visible;
    this.clouds.visible = true;
    try {
      renderer.compile(this.clouds, camera, scene);
    } catch (err) {
      console.warn(`nap clouds: shader warm-up failed: ${err.message}`);
    }
    this.clouds.visible = shown;
  }

  dispose() {
    this.clouds.removeFromParent();
    this.clouds.traverse((o) => o.geometry?.dispose());
    this.material.dispose();
    this.gradient.dispose();
  }
}

// Where the cloud sits: [below her chest bone, in front of her] (m).
export const PILLOW = [-0.12, 0.3];

const _head = new THREE.Vector3();
const _snoreOffset = new THREE.Vector3(0.09, 0.12, 0.05);
