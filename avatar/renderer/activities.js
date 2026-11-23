// What she's busy with besides talking, and the props that go with it:
//   - an activity Octavia names (`search`: a magnifying glass up to her eye while Octavia studies the
//     screen; `tasks`: reading your TickTick tasks off a clipboard; `web`: spinning a little globe while she
//     looks something up online; `files`: flipping through a folder while she looks through your files;
//     `morning`: a mug of hot chocolate for the morning rundown, sipped between sentences): a held gesture with
//     its prop, until Octavia ends it, she points at something or she leaves;
//   - a moment Octavia names: one go of a gesture with its prop in hand, and the prop doing its thing at the
//     right beat: taking a picture (`snapshot`, the camera and its flash), ticking a task off (`tick`, on the
//     clipboard), clicking a stopwatch on for a timer (`timer_start`) or holding it up as it rings
//     (`timer_done`), ringing a bell for a reminder (`bell`), the sonic rainboom (`rainboom`, no prop),
//     twirling a wrench as a build of hers starts (`wrench`), headphones on and bobbing to music (`music`,
//     worn), sunglasses on for a sunny day (`sunglasses`, worn), an umbrella for a rainy one (`umbrella`) and a
//     megaphone for a shout (`megaphone`);
//   - tuning a prop (previewProp): holding it the whole time, and up at her eye in a search (it stands in
//     for the search prop there, unless it's the camera prop, which the pictures show anyway), so
//     `octavia avatar preview --prop NAME` shows how a search prop lines up with her eye.
// Every frame it works out which prop belongs in her hand right now and keeps the prop rack in step, so a
// picture taken mid-search swaps the magnifier for the camera and back again, and a prop never outlives the
// pose that holds it up. Which hand: the prop's own `hand` setting (right unless the config says left; the
// clipboard's is left); the gestures mirror to it. Once the pose is written, it also keeps a magnifier in
// front of her eye (apply).

import * as THREE from 'three';
import { eyeWorld } from './anatomy.js';
import {
  BELL_AT_S,
  CAMERA_SHUTTER_S,
  HEADPHONES_ON_S,
  RAINBOOM_AT_S,
  SHADES_ON_S,
  STOPWATCH_PRESS_S,
  TICK_AT_S,
  UMBRELLA_AT_S,
  WRENCH_AT_S,
} from './gestures.js';

// Octavia ends an activity herself (20 s after she last asked for it, at the latest); this only ends one
// whose end got lost on the way.
export const ACTIVITY_MAX_S = 60;

// name -> the gesture that shows it, the activityProps key naming its prop, the face that goes with it, and
// whether she peers through the prop (it's kept between her eye and the viewer)
const ACTIVITIES = {
  search: { gesture: 'inspect', prop: 'search', face: ['curious', 0.55], peer: true },
  tasks: { gesture: 'clipboard', prop: 'tasks', face: ['curious', 0.4], peer: false },
  web: { gesture: 'globe', prop: 'web', face: ['curious', 0.5], peer: false },
  files: { gesture: 'clipboard', prop: 'files', face: ['curious', 0.4], peer: false },
  morning: { gesture: 'mug', prop: 'morning', face: ['relaxed', 0.5], peer: false },
};

// name -> the gesture that shows it, the activityProps key naming its prop (null: empty hands) and when it
// appears (`propAt` s after the start: headphones go on once her hands are at her ears; at once by default), the
// face that goes with it ([emotion, intensity, s]), what the prop does and when ([action, s after the start]: see
// PropRack.act), and an effect the app draws and when ([name, s]: `flash` or `rainboom`).
const MOMENTS = {
  snapshot: { gesture: 'camera', prop: 'camera', face: ['playful', 0.5, 1.2], effect: ['flash', CAMERA_SHUTTER_S] },
  tick: { gesture: 'tick', prop: 'tasks', face: ['happy', 0.65, 1.7], act: ['tick', TICK_AT_S] },
  timer_start: { gesture: 'stopwatch_start', prop: 'timer', face: ['playful', 0.55, 1.6], act: ['start', STOPWATCH_PRESS_S] },
  timer_done: { gesture: 'stopwatch_ring', prop: 'timer', face: ['excited', 0.7, 2], act: ['ring', STOPWATCH_PRESS_S] },
  bell: { gesture: 'ring_bell', prop: 'reminder', face: ['excited', 0.6, 1.9], act: ['ring', BELL_AT_S] },
  rainboom: { gesture: 'rainboom', prop: null, face: ['excited', 1, 2.2], effect: ['rainboom', RAINBOOM_AT_S] },
  wrench: { gesture: 'wrench', prop: 'build', face: ['smug', 0.55, 2], act: ['twirl', WRENCH_AT_S] },
  music: { gesture: 'headphones', prop: 'music', propAt: HEADPHONES_ON_S, face: ['relaxed', 0.5, 4.2] },
  sunglasses: { gesture: 'shades', prop: 'sun', propAt: SHADES_ON_S, face: ['smug', 0.7, 2.8] },
  umbrella: { gesture: 'umbrella', prop: 'rain', face: ['happy', 0.45, 3], act: ['open', UMBRELLA_AT_S] },
  megaphone: { gesture: 'megaphone', prop: 'shout', face: ['excited', 0.85, 2.4] },
};
export const MOMENT_NAMES = Object.freeze(Object.keys(MOMENTS));

// Peering: the lens is kept on the line from the viewer to her eye, about this far (m) in front of it.
export const PEER_DEPTH = 0.1;
// The lens a prop is peered through is its part named 'lens' (any case, and with the number Blender adds to a
// copy: three.js turns 'Lens.001' into 'Lens001'): the built-in magnifier's glass, or a model's own part of
// that name. A prop without one is taken to have its lens here in her hand: a handle's length up from the
// middle of her fist, out of its thumb side (+Z of the hand frame in props.js), where the untuned built-in has
// it; its grip and rotation should put its lens there.
const LENS_NAME = /^lens(?:[_-]?\d+)?$/i;
export const LENS_FROM_FIST = new THREE.Vector3(0, 0, 0.103);
const SIDES = ['left', 'right'];

export class Activities {
  // `animator`: the Animator. `rack`: the PropRack (props.js), or null. `expressions`: for an activity's
  // face, or null. From the app's init(): `props` (each prop's settings, for its hand), `activityProps`
  // (which prop each activity uses) and `previewProp` (a prop to hold throughout, or null). `onEffect(name)`:
  // a moment's effect is due now (the camera's `flash`, the `rainboom`), for the app to draw.
  constructor({ animator, rack = null, expressions = null, props = {}, activityProps = {}, previewProp = null, onEffect = null }) {
    this.animator = animator;
    this.gestures = animator.gestures;
    this.rack = rack;
    this.expressions = expressions;
    this.props = props && typeof props === 'object' ? props : {};
    this.activityProps = activityProps && typeof activityProps === 'object' ? activityProps : {};
    this.previewProp = typeof previewProp === 'string' ? previewProp : null;
    this.onEffect = typeof onEffect === 'function' ? onEffect : null;
    this.name = null; // the activity going on
    this.elapsed = 0; // s since it started
    this.gesture = null; // its pose, once playing
    this.face = null; // the expression it put on
    this.moment = null; // { name, def, gesture, acted, effected } while a moment plays
    this.holding = null; // { name, side, ok }: what the rack was last asked for (null: empty hands)
    this.carried = null; // { name, hand }: a prop Octavia asked her to hold (carry), whenever her hands are free
    this.peer = null; // { gesture, side, anchor }: a pose peering through its prop, until it has faded out
    // What peering moves and aims at, per side: the arm's bones, and where the eye shows in the head's
    // frame (null when the model lacks any of it). Measured now, before any expression moves the face.
    const vrm = animator.vrm;
    const bone = (name) => vrm?.humanoid?.getNormalizedBoneNode(name) ?? null;
    this.head = vrm?.humanoid?.getRawBoneNode('head') ?? null;
    this.arms = Object.fromEntries(
      SIDES.map((side) => {
        const arm = { upper: bone(`${side}UpperArm`), hand: bone(`${side}Hand`), eye: this.head && visibleEye(vrm, side, this.head) };
        return [side, arm.upper && arm.hand && arm.eye ? arm : null];
      }),
    );
  }

  // An activity or a moment is going on.
  get busy() {
    return this.name !== null || this.moment !== null;
  }

  // The protocol's activity message: a known name starts that activity (a repeat carries on with it);
  // anything else, null included, ends the one going on.
  set(name) {
    if (typeof name !== 'string' || !Object.hasOwn(ACTIVITIES, name)) {
      this.end();
      return;
    }
    if (name === this.name) return;
    this.end();
    this.name = name;
    this.elapsed = 0;
  }

  // Ends the activity: its pose eases out and its prop pops out, or both are gone at once with `now` (she's
  // out of sight, and should reappear without them; a moment going on is dropped too).
  end({ now = false } = {}) {
    if (this.gesture) {
      if (now) this.gestures.drop(this.gesture);
      else this.gestures.releaseOne(this.gesture);
    }
    if (now && this.moment) {
      this.gestures.drop(this.moment.gesture);
      this.moment = null;
    }
    // Back to the state's face, unless something else has taken the face over since.
    if (this.face && this.expressions && this.expressions.active === this.face) this.expressions.release();
    this.name = null;
    this.gesture = null;
    this.face = null;
    this.sync();
    if (now) this.rack?.update(1); // finishes any pop at once
  }

  // Taking a picture: the camera gesture, with the camera prop in its hand when there is one (without one
  // she mimes it), and the flash.
  snapshot() {
    this.play('snapshot');
  }

  // A moment (MOMENTS): its gesture, with its prop in hand when there is one (without one she mimes it, or
  // holds nothing), its face, and the prop's action and the effect at their beats. A new moment takes over
  // from one still playing. False for unknown names.
  play(name) {
    const def = typeof name === 'string' && Object.hasOwn(MOMENTS, name) ? MOMENTS[name] : null;
    if (!def) return false;
    const prop = def.prop ? this.propFor(def.prop) : null;
    const side = this.handOf(prop);
    // A prop that appears later (propAt) isn't in her hand yet: sync() puts it there when its time comes.
    const held = def.propAt ? !!prop && !!this.rack?.has?.(prop) : this.hold(prop, side);
    const gesture = this.animator.playGesture(def.gesture, { side, held });
    if (!gesture) return false;
    this.moment = { name, def, gesture, acted: !def.act, effected: !def.effect };
    if (this.expressions && def.face) this.expressions.show(...def.face);
    return true;
  }

  // Every frame, before the animator: (re)starts the activity's pose whenever her arms are free (after a
  // picture interrupted it, say), sets off a moment's prop action and effect when their time comes, puts the
  // right prop in her hand and moves the props' pop animations on.
  update(dt) {
    if (this.name) {
      this.elapsed += dt;
      if (this.elapsed > ACTIVITY_MAX_S) this.end();
      else if (!this.gestures.live(this.gesture) && !this.gestures.armsHeld(this.gesture)) this.startPose();
    }
    this.cue();
    this.sync();
    this.rack?.update(dt);
  }

  // A moment's prop action and effect, each once, when the gesture gets to it. (The gesture's own clock: the
  // animator moves it on after this, so a beat lands within a frame of its time.)
  cue() {
    const m = this.moment;
    if (!m) return;
    if (!this.gestures.active.includes(m.gesture)) {
      this.moment = null;
      return;
    }
    const t = m.gesture.t;
    if (!m.acted && t >= m.def.act[1]) {
      m.acted = true;
      if (this.rack && this.holding?.ok) this.rack.act(m.def.act[0]);
    }
    if (!m.effected && t >= m.def.effect[1]) {
      m.effected = true;
      try {
        this.onEffect?.(m.def.effect[0]);
      } catch (err) {
        console.warn(`effect ${m.def.effect[0]} failed: ${err.message}`); // the moment goes on without it
      }
    }
  }

  startPose() {
    const activity = ACTIVITIES[this.name];
    const name = this.propFor(activity.prop);
    const side = this.handOf(name);
    const held = this.hold(name, side);
    // Whether she's quiet (not talking), for a pose that does something in the pauses: a sip of her mug.
    const quiet = () => !this.animator.lipsync?.talking;
    this.gesture = this.animator.playGesture(activity.gesture, { side, held, quiet });
    this.peer = activity.peer && held && this.gesture ? { gesture: this.gesture, side, anchor: null } : null;
    if (this.expressions) {
      const [emotion, intensity] = activity.face;
      this.expressions.show(emotion, intensity, ACTIVITY_MAX_S);
      this.face = this.expressions.active;
    }
  }

  // A post-pose layer (Animator.addPostPose): while she peers through a magnifier, turns that whole arm
  // about the shoulder so the lens sits on the line from the viewer to her eye, so she's seen through it
  // however her head moves (and whatever the model's proportions). Blended by the pose's own weight, so
  // it eases in and out with it.
  apply() {
    const peer = this.peer;
    if (!peer) return;
    if (!this.gestures.active.includes(peer.gesture)) {
      this.peer = null;
      return;
    }
    const arm = this.arms[peer.side];
    const w = this.gestures.weight(peer.gesture);
    if (!arm || !(w > 0)) return;
    peer.anchor ??= this.lensAnchor(peer.side);
    if (!peer.anchor) return; // the prop is still on its way
    arm.upper.updateWorldMatrix(true, true);
    const S = arm.upper.getWorldPosition(_s);
    const lens = arm.hand.localToWorld(_lens.copy(peer.anchor));
    // The model's own head bone gets this frame's pose only after the animator (vrm.update), so this is
    // where the head was a frame ago: too little to see at the pace she moves it.
    this.head.updateWorldMatrix(true, false);
    const eye = this.head.localToWorld(_eye.copy(arm.eye));
    const view = _view.subVectors(this.animator.stage.camera.position, eye).normalize();
    // The lens can only swing round her shoulder: put it where that sphere meets the line from her eye to
    // the viewer, taking the crossing nearer PEER_DEPTH in front of the eye.
    const r = lens.distanceTo(S);
    const d = _d.subVectors(eye, S);
    const b = view.dot(d);
    const disc = b * b - (d.lengthSq() - r * r);
    let t = -b; // no crossing: as close to the line as it gets
    if (disc >= 0) {
      const root = Math.sqrt(disc);
      t = Math.abs(-b - root - PEER_DEPTH) < Math.abs(-b + root - PEER_DEPTH) ? -b - root : -b + root;
    }
    const want = _want.copy(eye).addScaledVector(view, Math.max(t, 0.03));
    _turn.setFromUnitVectors(lens.sub(S).normalize(), want.sub(S).normalize());
    _q.identity().slerp(_turn, Math.min(1, w));
    // A world-space turn, as a change to the arm's own (parent-relative) rotation.
    const parent = arm.upper.parent.getWorldQuaternion(_parent);
    arm.upper.quaternion.premultiply(_q.premultiply(_inverse.copy(parent).invert()).multiply(parent));
  }

  // Where the lens is in her hand bone's frame (see LENS_NAME), once the prop is in that hand.
  lensAnchor(side) {
    const mount = this.rack && this.rack.hand === side ? this.rack.object(this.rack.holding) : null;
    const frame = mount?.parent;
    if (!frame) return null;
    // The mount sits on the grip point in the prop rack's per-hand frame, which is fixed to the hand bone,
    // turned by the prop's rotation setting; its scale is the pop, which the lens's place doesn't follow.
    const lens = lensInMount(mount);
    const inFrame = lens ? lens.applyQuaternion(mount.quaternion) : LENS_FROM_FIST.clone();
    return inFrame.add(mount.position).applyQuaternion(frame.quaternion).add(frame.position);
  }

  // The protocol's prop-hold message: she holds prop `name` in `hand` ('left' | 'right', else the prop's own) whenever
  // no activity or moment needs her hands, until another one or null (she puts it down).
  carry(name, hand = null) {
    this.carried = typeof name === 'string' && name ? { name, hand: hand === 'left' || hand === 'right' ? hand : null } : null;
    if (this.holding && !this.holding.ok) this.holding = null; // ask again: the prop may be new since the rack said no
    this.sync();
  }

  // Which prop belongs in her hand right now: a moment's (or nothing) while it plays, the activity's prop
  // while its pose holds it up, otherwise the preview prop or else the one she was asked to carry, if any.
  sync() {
    let name = null;
    let side = null;
    const m = this.moment;
    if (m && this.gestures.live(m.gesture)) name = m.def.prop && m.gesture.t >= (m.def.propAt ?? 0) ? this.propFor(m.def.prop) : null;
    else if (this.gestures.live(this.gesture)) name = this.propFor(ACTIVITIES[this.name].prop);
    else if (this.previewProp) name = this.previewProp;
    else if (this.carried) ({ name, hand: side } = this.carried);
    this.hold(name, side ?? this.handOf(name));
  }

  // Asks the rack for prop `name` in hand `side` (null: empty hands), unless that's what it was last asked
  // for. True when the prop is in her hand, or will be as soon as it has loaded.
  hold(name, side) {
    const last = this.holding;
    const unchanged = last ? last.name === name && last.side === side : name === null;
    if (unchanged) return last ? last.ok : false;
    if (!name || !this.rack) {
      this.rack?.release();
      this.holding = name ? { name, side, ok: false } : null;
      return false;
    }
    const ok = this.rack.hold(name, side);
    // An unknown or broken prop leaves the rack as it was: she mustn't keep holding the previous one.
    if (!ok) this.rack.release();
    this.holding = { name, side, ok };
    return ok;
  }

  // The prop an activity (or the camera) uses: a prop's name, or null for none. A prop being tuned stands
  // in for the search prop, unless it's the camera prop (the pictures already show that one).
  propFor(key) {
    const configured = (k) => {
      const name = Object.hasOwn(this.activityProps, k) ? this.activityProps[k] : null;
      return typeof name === 'string' && name ? name : null;
    };
    if (key === 'search' && this.previewProp && this.previewProp !== configured('camera')) return this.previewProp;
    return configured(key);
  }

  // The hand a prop goes in: its own setting, right unless it says left (and right for no prop at all).
  handOf(name) {
    const def = name && Object.hasOwn(this.props, name) ? this.props[name] : null;
    return def && def.hand === 'left' ? 'left' : 'right';
  }
}

// Where the middle of a prop's part named 'lens' is in the frame of its mount (props.js: the prop's root
// in her hand), or null when it has none. It's the middle of the part's geometry (its origin when it has
// none), carried up through the transforms in between: the grip offset, the turn that makes it a handle,
// the size scale and the model's own nodes. The mount's own transform isn't included.
function lensInMount(mount) {
  let lens = null;
  mount.traverse((o) => {
    if (!lens && o !== mount && LENS_NAME.test(o.name)) lens = o;
  });
  if (!lens) return null;
  const toMount = (node) => {
    const m = new THREE.Matrix4();
    for (let o = node; o && o !== mount; o = o.parent) {
      if (o.matrixAutoUpdate) o.updateMatrix();
      m.premultiply(o.matrix);
    }
    return m;
  };
  const box = new THREE.Box3();
  lens.traverse((o) => {
    if (!o.geometry) return;
    if (!o.geometry.boundingBox) o.geometry.computeBoundingBox();
    box.union(o.geometry.boundingBox.clone().applyMatrix4(toMount(o)));
  });
  return box.isEmpty() ? new THREE.Vector3().applyMatrix4(toMount(lens)) : box.getCenter(new THREE.Vector3());
}

// Where an eye shows (anatomy.js), in the frame of the model's own (raw) head bone; null without an eye bone.
function visibleEye(vrm, side, head) {
  const at = eyeWorld(vrm, side);
  return at && head.worldToLocal(at);
}

const _s = new THREE.Vector3();
const _lens = new THREE.Vector3();
const _eye = new THREE.Vector3();
const _view = new THREE.Vector3();
const _d = new THREE.Vector3();
const _want = new THREE.Vector3();
const _turn = new THREE.Quaternion();
const _q = new THREE.Quaternion();
const _parent = new THREE.Quaternion();
const _inverse = new THREE.Quaternion();
