// Renderer entry point: loads the VRM, wires protocol messages to the animation modules, runs the frame
// loop, and tells the main process when the pointer is over her (everything else is click-through). Over her
// she fades and is click-through too, unless Ctrl is held (seethrough.js).
//
// Pointing: the main process moves and resizes the window and sends its layout (where her body sits in
// it); coordinates in the renderer's messages are window CSS pixels. She zips in, points (a ring marks
// the spot) and zips out again when told, reporting 'arrived' and 'zipped-out' so the main process can
// choreograph the flight.
//
// Props: she holds a camera while taking a screenshot, a magnifying glass during the `search` activity, a
// clipboard while she works with your tasks, and a stopwatch or a bell for a moment (activities.js decides
// what's in her hand; props.js puts it there).
//
// Effects: the camera's flash, and the sonic rainboom: a burst behind her in her own window (marker.js) that
// the main process carries on across the screen.
//
// Sitting on a window: the main process lays her out on top of one (the 'perch' layout) and she sits there, legs
// over its edge, until she flies off.
//
// Life at home: she can float while you talk to her (cfg.hover, off by default), naps on a cloud when left alone
// long enough, or is
// caught napping after a long break (nap.js, cfg.napAfterS), now and then peeks in from the side instead of rising,
// kicks up a puff of dust when she zips off or brakes, is sleepy late at night and perky in the morning
// (daytime.js), and keeps a mood for a while after a conversation (the `mood` message).

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { Activities } from './activities.js';
import { Animator } from './animation.js';
import { Expressions } from './expressions.js';
import { Credit } from './credit.js';
import { PUBLIC_GESTURES } from './gestures.js';
import { LipSync } from './lipsync.js';
import { Marker } from './marker.js';
import { Nap } from './nap.js';
import { energyAt } from './daytime.js';
import { pick } from './math.js';
import { Presence } from './presence.js';
import { PropRack } from './props.js';
import { SeeThrough } from './seethrough.js';
import { Stage } from './stage.js';
import { solveArm } from './tuning.js';

const host = window.avatarHost;
const canvas = document.getElementById('stage');

const HIT_ALPHA = 24; // pixels fainter than this (anti-aliased edges) are click-through
const MAX_DT = 0.1; // after a stall, don't let springs and animations jump
const CALM_FRAME_MS = 1000 / 30 - 4; // a little under 33 ms so a 60 Hz display reliably hits every other frame
const FULL_ASPECT = 0.55; // the full-body window's width / height (ASPECT.full in lib/config.js)
const STREAK_WIDTH = 0.16; // m at her depth: the in-window rainbow streak of a zip
const POINT_LOG_S = 2; // how often the debug log reports how well the finger lines up
const JUMP = 0.05; // m her root may move in one frame before the whole body's transforms are refreshed
const SEAT_DROP = 0.13; // her seat is this much of her thigh's length below her hip joints
// How long 'ready' waits for the props to load. One that takes longer still works: it pops into her hand
// when it arrives.
const PROPS_WAIT_MS = 4000;
const HOVER_STATES = new Set(['listening', 'transcribing']); // she can float while you talk to her (cfg.hover)
const PEEK_REASONS = new Set(['wake', 'talk']); // called to talk, she sometimes peeks in rather than rising...
const PEEK_CHANCE = 0.3; // ...this often
const PUFF_M = 0.5; // a dust puff's size at her depth (m)
const ENERGY_EVERY_MS = 60 * 1000; // how often she checks the clock for how awake she is

const finite = (...values) => values.every(Number.isFinite);
// A message's {x, y} (window px), or null.
const toPoint = (p) => (p && finite(p.x, p.y) ? { x: p.x, y: p.y } : null);
// A message's {x, y, w, h} (window px) with some size, or null.
const toRect = (r) => (r && finite(r.x, r.y, r.w, r.h) && r.w > 0 && r.h > 0 ? { x: r.x, y: r.y, w: r.w, h: r.h } : null);

async function loadVrm(bytes) {
  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const gltf = await loader.parseAsync(buffer, '');
  const vrm = gltf.userData.vrm;
  if (!vrm || !vrm.humanoid) throw new Error('the model is not a VRM');
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  VRMUtils.combineSkeletons(gltf.scene);
  // VRM 0.x faces -Z; turn it around to face the camera like VRM 1.0.
  VRMUtils.rotateVRM0(vrm);
  // Skinned meshes keep their bind-pose bounds, which the animated body leaves; never cull her.
  vrm.scene.traverse((o) => {
    o.frustumCulled = false;
  });
  return vrm;
}

// Where her shoulder and fingertip sit in the full-body framing, as fractions of its height, so the main
// process can place her beside a target before she's shown there. Measured at rest (arms straight out).
// What she sits on when she sits (a little below her hip joints), world y at rest, or null without leg bones.
function seatHeight(vrm) {
  const y = (bone) => vrm.humanoid.getNormalizedBoneNode(bone)?.getWorldPosition(new THREE.Vector3()).y;
  const [hipL, hipR, kneeL, kneeR] = ['leftUpperLeg', 'rightUpperLeg', 'leftLowerLeg', 'rightLowerLeg'].map(y);
  if (![hipL, hipR, kneeL, kneeR].every(Number.isFinite)) return null;
  const hip = (hipL + hipR) / 2;
  return hip - SEAT_DROP * (hip - (kneeL + kneeR) / 2);
}

// `seat`: seatHeight() at rest (the main process keeps its default without it).
function metrics(stage, pointing, seat) {
  const arms = Object.values(pointing.chains).filter(Boolean);
  if (!arms.length) return null;
  const { top, span } = stage.viewSpan('full');
  const mean = (f) => arms.reduce((sum, arm) => sum + f(arm), 0) / arms.length;
  const out = {
    type: 'metrics',
    shoulderY: mean((arm) => top - arm.shoulder.y) / span,
    shoulderX: mean((arm) => Math.abs(arm.shoulder.x)) / span,
    reach: mean((arm) => arm.reach) / span,
    bodyWidth: FULL_ASPECT,
  };
  if (Number.isFinite(seat)) out.seatY = (top - seat) / span;
  return out;
}

// VRoid ships very lazy eye ranges (full eye travel only at 90 degrees off-axis). Keep each map's
// maximum rotation but reach it sooner, so glances are visible.
function livenEyes(vrm) {
  const applier = vrm.lookAt && vrm.lookAt.applier;
  if (!applier || !applier.rangeMapHorizontalInner) return;
  for (const key of ['rangeMapHorizontalInner', 'rangeMapHorizontalOuter', 'rangeMapVerticalDown', 'rangeMapVerticalUp']) {
    applier[key].inputMaxValue = Math.min(applier[key].inputMaxValue, 35);
  }
}

async function main() {
  const cfg = await host.init();
  if (cfg.form === 'orb') {
    // The orb (orb.js) instead of her: no model to load, the same messages.
    const { runOrb } = await import('./orb.js');
    await runOrb(host, cfg, canvas);
    return;
  }
  document.title = cfg.credit;
  // Shown like a tooltip while the pointer rests on her at home (not the canvas's title: see credit.js).
  const credit = new Credit(document.getElementById('credit'), cfg.credit);

  const stage = new Stage(canvas);
  const vrm = await loadVrm(await host.loadModel());
  livenEyes(vrm);
  // Zips move this group instead of her own root, and her hair and clothes simulate relative to it (a
  // spring bone's "center"; VRoid models already use their own root node, which moves with it), so
  // flying across the window doesn't fling them about.
  const flight = new THREE.Group();
  flight.add(vrm.scene);
  stage.scene.add(flight);
  for (const joint of vrm.springBoneManager?.joints ?? []) if (!joint.center) joint.center = flight;

  const bounds = new THREE.Box3().setFromObject(vrm.scene);
  const hips = vrm.humanoid.getNormalizedBoneNode('hips').getWorldPosition(new THREE.Vector3());
  const seat = seatHeight(vrm); // at rest, like the arms (pointing.js): later she may be out of sight, sunk away
  // Zips and their streaks are centred on her chest.
  const chest = vrm.humanoid.getNormalizedBoneNode('chest') || vrm.humanoid.getNormalizedBoneNode('hips');
  const centre = chest.getWorldPosition(new THREE.Vector3());
  stage.setModel(bounds, hips.y);
  stage.layout({ framing: cfg.framing, body: null });

  const lipsync = new LipSync();
  const expressions = new Expressions(vrm);
  const animator = new Animator(vrm, stage, expressions, lipsync);
  const pointing = animator.pointing;
  // The props load while the rest gets ready; a hand holding one closes round it. One that won't load is
  // left out (or its built-in stands in): Octavia shows the user why, since otherwise she just holds nothing.
  const rack = new PropRack(vrm, cfg.props, host.loadProp, {
    onProblem: (name, reason) => host.emit({ type: 'error', message: `prop "${name}" couldn't be loaded: ${reason}` }),
  });
  const propsLoaded = rack.preload();
  animator.setGrip(rack);
  const marker = new Marker(document.getElementById('marker'));
  // A brief brightening of her own pixels only; the transparent rest of the window stays see-through.
  const flash = () =>
    canvas.animate([{ filter: 'brightness(2.4)' }, { filter: 'brightness(1)' }], { duration: 220, easing: 'ease-out' });
  // The sonic rainboom: a burst from her chest behind her, and across the screen from there (the main process
  // draws that part in the trail windows, which never take the mouse).
  const rainboom = () => {
    const at = stage.toWindow(chest.getWorldPosition(new THREE.Vector3()));
    const reach = Math.hypot(window.innerWidth, window.innerHeight);
    marker.boom(at.x, at.y, reach);
    flash();
    host.emit({ type: 'rainboom', x: Math.round(at.x), y: Math.round(at.y) });
  };
  // The camera's flash is its shutter going off: Octavia plays the shutter sound on that very beat.
  const shutter = () => {
    flash();
    host.emit({ type: 'shutter' });
  };
  const effects = { flash: shutter, rainboom };
  // A puff of dust behind her, where she is now: taking off, or braking.
  const puff = () => {
    const at = stage.toWindow(chest.getWorldPosition(new THREE.Vector3()));
    marker.puff(at.x, at.y, PUFF_M * stage.pixelsPerMeter);
  };
  const activities = new Activities({
    animator,
    rack,
    expressions,
    props: cfg.props,
    activityProps: cfg.activityProps,
    previewProp: cfg.previewProp,
    onEffect: (name) => Object.hasOwn(effects, name) && effects[name](),
  });
  animator.addPostPose(activities); // keeps a magnifier in front of her eye
  const nap = new Nap({ animator, expressions, vrm, napAfterS: cfg.napAfterS });
  // Sleepy late at night, perky in the morning: from the clock, every minute.
  const checkClock = () => {
    const energy = energyAt(new Date());
    animator.setEnergy(energy);
    expressions.setEnergy(energy);
  };
  checkClock();
  setInterval(checkClock, ENERGY_EVERY_MS);
  let running = false;
  let gone = null; // { frames, messages }: she's out of sight; report it once a blank frame is up

  // She's out of sight: once a fully transparent frame is on screen, stop the loop and tell the main
  // process, which may hide or move the window next.
  const goneAfterFrame = (msg) => {
    if (!running) host.emit(msg);
    else if (gone) gone.messages.push(msg);
    else gone = { frames: 2, messages: [msg] };
  };

  // ---- pointing ----
  let pendingPoint = null; // a point waiting for her to arrive
  const beginPointing = ({ target, box, arm }) => {
    pendingPoint = null;
    if (pointing.start(target, box, arm)) marker.show(target.x, target.y, box);
  };
  const endPointing = () => {
    pendingPoint = null;
    pointing.stop();
    marker.hide();
  };
  // Window px -> the direction (world: x right, y up) from her chest toward it, or null.
  const directionTo = (p) => {
    if (!p) return null;
    const c = stage.toWindow(centre);
    return { x: p.x - c.x, y: c.y - p.y };
  };

  const events = {
    hidden() {
      pointing.cancel();
      marker.clear();
      goneAfterFrame({ type: 'hidden' });
    },
    arrived() {
      puff(); // braking
      host.emit({ type: 'arrived' });
      if (pendingPoint) beginPointing(pendingPoint);
    },
    zippedOut() {
      pendingPoint = null;
      pointing.cancel();
      marker.clear();
      goneAfterFrame({ type: 'zipped-out' });
    },
  };
  const presence = new Presence({ animator, canvas, flight, sink: bounds.max.y * 0.35, events });

  // Sitting on top of a window (the 'perch' layout): a seated pose held until another layout comes. `now`: she's out
  // of sight, so she's already seated (or standing) when she comes into view.
  let seated = null;
  const sit = (on, now) => {
    if (on && !seated) {
      seated = animator.playGesture('perch');
      if (now && seated) seated.t = seated.def.fadeIn;
    } else if (!on && seated) {
      if (now) animator.gestures.drop(seated);
      else animator.gestures.releaseOne(seated);
      seated = null;
    }
  };

  // The window's layout from the main process: the framing, where her body sits in the window, and
  // whether she's home, out pointing (flying) or sitting on a window.
  let atHome = true;
  const applyLayout = (l) => {
    if (!l || typeof l !== 'object') return;
    const mode = l.mode === 'point' || l.mode === 'perch' ? l.mode : 'home';
    atHome = mode === 'home';
    credit.hide(); // the window has moved or changed size under it
    const framing = l.framing === 'upper' || l.framing === 'full' ? l.framing : atHome ? cfg.framing : 'full';
    // At home her viewport is the whole window, whatever size it turned out to be.
    stage.layout({ framing, body: atHome ? null : toRect(l.body) });
    animator.setFlying(mode === 'point', !presence.visible);
    sit(mode === 'perch', !presence.visible);
  };
  host.onLayout?.(applyLayout);

  // ---- pointer: hit test, see-through, drag and click ----
  const pointer = { x: -1, y: -1, inside: false };
  let over = false;
  let dragging = false;
  // On her she fades and lets clicks through, unless Ctrl is held (seethrough.js).
  const seeThrough = new SeeThrough(host, (on) => canvas.classList.toggle('see-through', on));
  const setOver = (value) => {
    if (value === over) return;
    over = value;
    if (!value) credit.hide();
    seeThrough.setOver(value);
  };
  // Forwarded mouse moves arrive even while the window ignores the mouse. No 'pointerleave': switching to
  // click-through can fire one with the pointer still on her; the cursor feed says when it really left.
  canvas.addEventListener('pointermove', (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.inside = true;
  });
  canvas.addEventListener('pointerdown', (e) => {
    if (over) credit.dismiss();
    if (e.button !== 0 || !over || !seeThrough.takesMouse) return;
    dragging = true;
    seeThrough.setDragging(true);
    canvas.setPointerCapture(e.pointerId);
    host.drag('start');
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    host.drag('end');
    seeThrough.setDragging(false); // Ctrl let go by now: clicks go through her again
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('lostpointercapture', endDrag);
  // Debug, for tools/drive.mjs: a pointer held resting at {x, y} (window px) for the hit test and the
  // credit, whatever the real cursor does; null gives it back.
  let heldPointer = null;
  const holdPointer = (p) => {
    heldPointer = p;
    pointer.inside = !!p;
    if (p) Object.assign(pointer, p);
    else setOver(false);
  };
  host.onCursor((p) => {
    animator.setCursor(p.x, p.y);
    if (dragging || heldPointer) return;
    // The main process already stopped listening to the mouse once the cursor left; the frame loop lets her
    // come back (seeThrough.follow).
    pointer.inside = p.inside;
    if (p.inside) {
      pointer.x = p.x;
      pointer.y = p.y;
    }
  });

  // ---- frame loop (runs only while she, or her marker, is on screen) ----
  let last = 0;
  // Spring bones measure themselves against their children's world positions before updating those, so
  // when she jumps (a zip starting a body length away, a rise starting below the window) the whole body
  // must be brought along first, or a joint stretches across the gap and flings her hair about.
  const lastRoot = new THREE.Vector3(NaN, NaN, NaN);
  const root = new THREE.Vector3();
  const followJumps = () => {
    vrm.scene.getWorldPosition(root);
    if (!(root.distanceTo(lastRoot) < JUMP)) flight.updateMatrixWorld(true);
    lastRoot.copy(root);
  };
  const step = (dt) => {
    presence.update(dt);
    lipsync.update(dt);
    // Nothing at all going on: the idle state, standing at home, not pointing, busy or dragged about.
    const idle = animator.state === 'idle' && atHome && presence.settled && !pointing.active && !activities.busy && !dragging;
    nap.update(dt, { idle, visible: presence.visible, busy: activities.busy || pointing.active });
    // Floating, legs tucked, while you talk to her (if cfg.hover), and while she naps.
    animator.setHovering((cfg.hover === true && HOVER_STATES.has(animator.state)) || nap.dozing);
    activities.update(dt);
    animator.update(dt);
    followJumps();
    expressions.update(dt, lipsync);
    vrm.update(dt);
  };
  // The ring marks the spot; during a zip the in-window part of her streak trails behind her.
  const offsetPx = (o) => stage.toWindow(new THREE.Vector3(centre.x + o.x, centre.y + o.y, centre.z));
  const drawMarker = (dt) => {
    const z = nap.snore;
    marker.setSnore(z && stage.toWindow(z));
    const s = presence.streak;
    marker.setStreak(s && { tail: offsetPx(s.tail), head: offsetPx(s.head), width: STREAK_WIDTH * stage.pixelsPerMeter, alpha: s.alpha });
    marker.update(dt);
  };
  // Debug log (shown with the app's debug flag): how exactly the finger lines up with the spot.
  let pointLog = 0;
  const logPointing = (dt) => {
    if (pointing.weight < 1 || !pointing.settled) {
      pointLog = 0.4;
      return;
    }
    pointLog -= dt;
    if (pointLog > 0) return;
    pointLog = POINT_LOG_S;
    const m = pointing.measure();
    if (!m) return;
    const finger = m.fingerLine === undefined ? 'n/a' : `${m.fingerLine.toFixed(2)} px`;
    console.log(
      `pointing: ${m.side} arm at (${m.target.x.toFixed(0)}, ${m.target.y.toFixed(0)}): shoulder-fingertip line off by ` +
        `${m.shoulderLine.toFixed(2)} px, finger line ${finger}, fingertip ${m.tipGap.toFixed(0)} px away`,
    );
  };
  // Debug, for tools/drive.mjs's clips: with the clock held, frames stop stepping her on their own and only a
  // 'tick' message moves her on, by exact steps, so a clip comes out the same frame by frame however slow capturing is.
  let clockHeld = false;
  let sceneTurn = null; // her own facing, before a debug 'turn'
  const frame = (now) => {
    if (!running) return;
    if (clockHeld) {
      last = now;
      requestAnimationFrame(frame);
      return;
    }
    // Slow idle motion looks the same at half the frame rate and costs half the power; talking,
    // gestures and popping in/out get every frame.
    const calm = animator.calm && presence.settled && !dragging;
    if (calm && now - last < CALM_FRAME_MS) {
      requestAnimationFrame(frame);
      return;
    }
    // A frame's timestamp can come from just before start() ran; never step backwards.
    const dt = Math.min(MAX_DT, Math.max(0, (now - last) / 1000));
    last = now;
    step(dt);
    stage.render();
    drawMarker(dt);
    logPointing(dt);
    if (!dragging && presence.visible) {
      const onHer = pointer.inside && !over && stage.alphaAt(pointer.x, pointer.y) > HIT_ALPHA;
      setOver(seeThrough.follow(onHer, pointer.inside, now));
    }
    const home = atHome && presence.settled; // standing at home: not out pointing, arriving or leaving
    credit.update(dt, { over, home, x: pointer.x, y: pointer.y, width: window.innerWidth, height: window.innerHeight });
    if (gone && gone.frames-- === 0) {
      // She's gone and a fully transparent frame is on screen: now the window can be hidden or moved.
      const { messages } = gone;
      gone = null;
      running = false;
      setOver(false);
      messages.forEach((msg) => host.emit(msg));
      return;
    }
    requestAnimationFrame(frame);
  };
  const start = () => {
    if (running) return;
    running = true;
    last = performance.now();
    requestAnimationFrame(frame);
  };

  // Debug, for tools/drive.mjs: holds the pose in `msg.pose` (bone -> [x, y, z], as poses.js writes them, with
  // optional `add` offsets and a `look`) until the next one (null lets go), with prop `msg.prop` in the hand its
  // config names (null: empty hands); `msg.act` then has the prop do something (rack.act).
  let posed = null;
  const debugPose = (msg) => {
    if (posed) animator.gestures.drop(posed);
    const pose = msg.pose && typeof msg.pose === 'object' ? msg.pose : null;
    posed = pose ? animator.playGesture('custom', { pose, add: msg.add || {}, look: msg.look || null }) : null;
    activities.previewProp = typeof msg.prop === 'string' ? msg.prop : null;
    activities.sync();
    if (typeof msg.act === 'string') rack.act(msg.act);
  };
  // Debug, for tools/drive.mjs: fits an arm pose for prop `msg.prop` in its hand to `msg.goals` from `msg.start`
  // (tuning.js) and logs it as "solved: {...}"; the prop stays in her hand, as with a pose message.
  const debugSolve = (msg) => {
    debugPose({ pose: null, prop: typeof msg.prop === 'string' ? msg.prop : null });
    let result;
    try {
      // The arm with the prop; with none, the one the message names.
      const side = rack.hand === 'left' || rack.hand === 'right' ? rack.hand : msg.side === 'left' ? 'left' : 'right';
      const measure = Array.isArray(msg.measure) ? msg.measure : [];
      result = solveArm({ vrm, rack, camera: stage.camera, side, start: msg.start, goals: msg.goals || [], measure, fixed: msg.fixed === true });
    } catch (err) {
      result = { error: err.message };
    }
    console.log(`solved: ${JSON.stringify({ id: msg.id ?? null, ...result })}`);
  };

  // ---- protocol messages ----
  let segmentActive = false;
  const endSegment = () => {
    if (!segmentActive) return;
    segmentActive = false;
    expressions.release();
    animator.endSegment();
  };
  const handlers = {
    show(msg) {
      gone = null;
      if (!presence.visible) expressions.active = null; // a mood from while she was away is stale now
      // Back home from pointing somewhere, she zips in from that side instead of rising.
      const from = msg.reason === 'return' ? toPoint(msg.from) : null;
      if (from && presence.settled) host.emit({ type: 'arrived' }); // already here: nothing to wait for
      else if (!from || !presence.zipIn(directionTo(from))) {
        // Back after a long break, she was napping (never shown before counts as no break: Infinity); otherwise,
        // called to talk, now and then she peeks in from the side.
        const away = presence.visible ? 0 : performance.now() / 1000 - presence.hiddenSince;
        const napping = nap.shown(away);
        const peek = !napping && PEEK_REASONS.has(msg.reason) && Math.random() < PEEK_CHANCE ? pick([-1, 1]) : 0;
        presence.show(msg.reason, { peek });
      }
      start();
    },
    hide() {
      credit.hide();
      // Leaving ends whatever she was busy with (out of sight already, it's gone at once).
      activities.end({ now: !presence.visible });
      endPointing();
      presence.hide();
    },
    // Point at a spot (window px), with the thing's extent if known and which arm. Out of sight, she zips
    // in from `from` first; already here, she just re-aims. Pointing ends any activity: normally she's
    // out of sight by now, so she arrives without its pose and prop.
    point(msg) {
      const target = toPoint(msg.target);
      if (!target) return;
      const request = { target, box: toRect(msg.box), arm: msg.arm };
      credit.hide();
      activities.end({ now: !presence.visible });
      gone = null;
      if (presence.settled) {
        beginPointing(request);
        host.emit({ type: 'arrived' }); // she's already here: nothing to wait for
        return;
      }
      if (!presence.visible) expressions.active = null;
      pendingPoint = request;
      presence.zipIn(directionTo(toPoint(msg.from)));
      start();
    },
    // Stop pointing; she stays where she is.
    unpoint() {
      endPointing();
    },
    // Ctrl went down or up, while the pointer is on her: held, she takes the mouse back.
    keys(msg) {
      seeThrough.setCtrl(msg.ctrl === true);
    },
    // The assistant is clicking the spot she points at: a jab at it, and the ring flashes.
    press() {
      pointing.press();
      marker.press();
    },
    // The user switched to a window, its middle at (x, y) window px: she glances that way. Not while she naps, or
    // comes or goes.
    glance(msg) {
      const p = toPoint(msg);
      if (p && presence.settled && !nap.dozing) animator.glanceAt(p.x, p.y);
    },
    // Something made her jump (a bang in the room, a big window popping up by itself): a jolt and a surprised
    // face; napping, she wakes with a start.
    startle() {
      if (!presence.settled) return;
      if (nap.dozing) nap.wake();
      else {
        animator.playGesture('surprised_jolt');
        expressions.show('surprised', 0.85, 1.2);
      }
    },
    // Pointing ends (an unpoint comes) in `seconds`: the ring counts down by fading and drawing in.
    'point-ending'(msg) {
      marker.countdown(Number(msg.seconds));
    },
    // Fly off toward `to` (window px), still pointing, and report 'zipped-out' once gone.
    'zip-out'(msg) {
      pendingPoint = null;
      credit.hide();
      marker.hide();
      if (presence.visible) puff(); // where she takes off from
      if (presence.zipOut(directionTo(toPoint(msg.to)))) {
        start();
        return;
      }
      // Already out of sight: nothing to fly.
      pointing.cancel();
      marker.clear();
      host.emit({ type: 'zipped-out' });
    },
    // Debug, for tools/drive.mjs: a renderer message (a layout, a point in window px, ...) wrapped as
    // {type: 'debug', message}. It isn't a protocol message, so the main process passes it through untouched,
    // which lets the aim be tested inside a fixed window. Normally layouts come through host.onLayout and
    // pointing messages from the main process's choreography. `hover` holds the pointer at {x, y} (or lets
    // go of it with no point), so the credit can be checked without moving the real mouse.
    debug(msg) {
      const inner = msg.message;
      if (!inner || typeof inner.type !== 'string' || inner.type === 'debug') return;
      if (inner.type === 'layout') applyLayout(inner);
      else if (inner.type === 'hover') holdPointer(toPoint(inner));
      else if (inner.type === 'pose') debugPose(inner);
      else if (inner.type === 'solve') debugSolve(inner);
      else if (inner.type === 'nap') inner.on ? nap.fallAsleep() : nap.wake();
      else if (inner.type === 'play' && typeof inner.name === 'string') animator.playGesture(inner.name); // any gesture
      else if (inner.type === 'clock') clockHeld = inner.held === true;
      else if (inner.type === 'turn') {
        // Turns her `y` radians on the spot (0: facing you), to see from the side whether a limb passes through her.
        sceneTurn ??= vrm.scene.rotation.y;
        vrm.scene.rotation.y = sceneTurn + (Number(inner.y) || 0);
      }
      else if (inner.type === 'tick') {
        // Moves her on `frames` steps of `dt` s with the clock held, draws her, and once that frame is on screen
        // logs "ticked <id>" so the capture that follows gets it.
        const dt = Math.min(MAX_DT, Math.max(0, Number(inner.dt) || 0));
        const frames = Math.min(600, Math.max(1, Math.floor(Number(inner.frames) || 1)));
        for (let i = 0; i < frames; i++) step(dt);
        stage.render();
        drawMarker(dt);
        requestAnimationFrame(() => requestAnimationFrame(() => console.log(`ticked ${inner.id ?? ''}`)));
      }
      else if (inner.type === 'peek') {
        // Out of sight: peek in from a side (-1 her right, 1 her left) instead of rising.
        presence.show('talk', { peek: inner.side === -1 ? -1 : 1 });
        start();
      } else if (inner.type === 'energy') {
        // How awake she is, until the clock is next checked (a minute at most): { sleepy, perky }.
        animator.setEnergy(inner);
        expressions.setEnergy(inner);
      }
      else if (Object.hasOwn(handlers, inner.type)) handlers[inner.type](inner);
    },
    state(msg) {
      if (typeof msg.state !== 'string') return;
      if (msg.state !== 'speaking') endSegment();
      animator.setState(msg.state);
      expressions.setState(msg.state);
    },
    segment(msg) {
      endSegment();
      segmentActive = true;
      // Octavia doesn't say when a sentence ends (the next segment or state change does). Estimate its
      // length from the text so a lost ending can't hold the face forever.
      const text = typeof msg.text === 'string' ? msg.text : '';
      expressions.show(msg.emotion, msg.intensity, 2 + text.length * 0.08);
      if (PUBLIC_GESTURES.includes(msg.gesture)) animator.playGesture(msg.gesture);
    },
    mouth(msg) {
      lipsync.push(msg.level, msg.v);
    },
    emotion(msg) {
      const hold = Number.isFinite(msg.hold) ? Math.max(0, msg.hold) : 3;
      expressions.show(msg.emotion, msg.intensity, hold);
    },
    // A mood that carries over after a conversation: her resting face keeps it for `seconds`.
    mood(msg) {
      expressions.setMood(msg.emotion, msg.intensity, Number(msg.seconds));
    },
    gesture(msg) {
      if (PUBLIC_GESTURES.includes(msg.name)) animator.playGesture(msg.name);
    },
    snapshot() {
      // Octavia is taking a screenshot: she holds up a camera and it flashes. The window is kept out of
      // screenshots (hideFromCapture), so none of this ends up in the picture.
      if (!presence.visible) return;
      activities.snapshot();
    },
    // A moment (activities.js): a task ticked off, a timer set or going off, a reminder's bell, a rainboom.
    // Only while she's there: one that crossed a hide on its way is stale.
    moment(msg) {
      if (!presence.visible) return;
      activities.play(msg.name);
    },
    // What she's busy with: `search` (a magnifying glass up to her eye) until a null ends it. Octavia only
    // starts one while she's there, so a start that crossed a hide on its way is stale; mid-flight is fine.
    activity(msg) {
      if (typeof msg.name === 'string' && presence.phase === 'hidden') return;
      activities.set(msg.name);
    },
    // A prop Octavia made from simple shapes (`msg.prop`: its settings, with `parts`), new or changed: ready to hold
    // at once, no restart. One she was asked to carry before it existed goes into her hand now.
    'prop-define'(msg) {
      if (typeof msg.name !== 'string') return;
      const problem = rack.define(msg.name, msg.prop);
      if (problem) {
        host.emit({ type: 'error', message: `prop "${msg.name}" couldn't be made: ${problem}` });
        return;
      }
      activities.props[msg.name] = msg.prop; // its hand, for handOf
      rack.compile(stage.renderer, stage.camera, stage.scene, [msg.name]);
      if (activities.carried?.name === msg.name) activities.carry(msg.name, activities.carried.hand);
    },
    // Hold prop `msg.name` (any she has) in `msg.hand` whenever her hands are free; null puts it down.
    'prop-hold'(msg) {
      activities.carry(msg.name, msg.hand);
    },
    // A made prop is thrown away (out of her hand, if she holds it).
    'prop-remove'(msg) {
      if (typeof msg.name !== 'string') return;
      if (activities.carried?.name === msg.name) activities.carry(null);
      if (rack.remove(msg.name)) delete activities.props[msg.name];
    },
  };
  host.onMessage((msg) => {
    const handler = Object.hasOwn(handlers, msg.type) ? handlers[msg.type] : null;
    if (!handler) return;
    try {
      handler(msg);
    } catch (err) {
      // A malformed message must never take the avatar down.
      console.error(`message ${msg.type} failed: ${err.stack || err}`);
    }
  });

  // The props' shaders are compiled as soon as they're in; 'ready' waits a little for them (so a preview
  // prop is already in her hand), but never for long.
  const propsReady = propsLoaded.then(() => rack.compile(stage.renderer, stage.camera, stage.scene));
  nap.compile(stage.renderer, stage.camera, stage.scene);
  await Promise.race([propsReady, new Promise((resolve) => setTimeout(resolve, PROPS_WAIT_MS))]);

  // Settle the hair and clothes into the idle pose before anyone sees her, and compile every shader now
  // rather than hitching on the first shown frame.
  vrm.humanoid.update();
  vrm.scene.updateMatrixWorld(true);
  vrm.springBoneManager?.reset();
  for (let i = 0; i < 90; i++) step(1 / 60);
  stage.renderer.compile(stage.scene, stage.camera);
  const measured = metrics(stage, pointing, seat);
  if (measured) {
    host.emit(measured);
    console.log(`metrics: ${JSON.stringify(measured)}`);
  }
  host.emit({ type: 'ready' });
}

main().catch((err) => {
  console.error(err);
  host.emit({ type: 'error', message: `avatar failed to start: ${err.message || err}` });
});
