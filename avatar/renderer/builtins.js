// The props that come built in, made from three.js primitives and stylised to sit next to a cel-shaded
// character: a magnifying glass, a camera, a clipboard, a stopwatch, a hand bell, a globe, a folder, a mug, a
// wrench, a megaphone and an umbrella for her hands, and headphones and sunglasses for her head
// (docs/avatar-life-plan.md). props.js puts them in her hand, or on her head.
//
// Each builder returns { model, holdAt, shape } and optionally `turn` and `rig`:
//   model   the prop, glTF-style: +Y up, +Z its front, in metres at its real size;
//   holdAt  the point on it that goes on the grip point (props.js): the middle of a handle, a corner, ...;
//   shape   how the hand closes round it, in poses.js hand() terms ({ curl, spread, thumb });
//   turn    [x, y, z] degrees, Euler XYZ in the hand frame: a prop that isn't held like a handle turned its way;
//   rig     its moving parts: act(name) starts one of its actions (true if it has it), update(dt) moves them
//           on, reset() puts it back as it was made;
//   worn    true for one she wears on her head (WORN_BUILTINS): then +Z is where she faces and holdAt is the point
//           that sits between her eyes.
// Glass and highlights use flat materials marked `keepStyle`, so they stay flat when the rest is toon-shaded.

import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { bump, easeOutBack, smoothstep } from './math.js';

const paint = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0, ...extra });
// Glass and highlights draw flat and unshaded in either style.
const glass = (color, opacity) => {
  const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
  material.userData.keepStyle = true;
  return material;
};

function part(geometry, material, name, [x, y, z] = [0, 0, 0]) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = name;
  mesh.position.set(x, y, z);
  return mesh;
}

// A glint painted on glass: a curved streak along the upper left and a dot above it. Flat and facing
// one way only, so the glint on the far side of a lens doesn't show through it.
function glint(radius, z, back) {
  const white = glass(0xffffff, 0.9);
  const streak = new THREE.RingGeometry(radius * 0.8, radius * 0.94, 14, 1, Math.PI * 0.64, Math.PI * 0.3);
  const angle = Math.PI * 0.54;
  const dot = new THREE.CircleGeometry(radius * 0.09, 14);
  dot.translate(Math.cos(angle) * radius * 0.87, Math.sin(angle) * radius * 0.87, 0);
  const group = new THREE.Group();
  group.name = back ? 'glint-back' : 'glint';
  group.add(part(streak, white, 'streak'), part(dot, white, 'dot'));
  // Seen from behind, a glint turned half round is again on the viewer's upper left.
  if (back) group.rotation.y = Math.PI;
  group.position.z = z;
  group.traverse((o) => (o.renderOrder = 1)); // after the lens it sits on
  return group;
}

// A magnifying glass, about 0.2 m long: golden rim, faint blue lens, dark handle with a golden knob.
// The lens faces +Z and the handle points down (-Y). Its glass is the part named 'lens': that's what the
// search activity holds up to her eye (activities.js), wherever a grip or rotation setting moves it.
function magnifier() {
  const R = 0.045; // lens radius
  const TUBE = 0.0065;
  const gold = paint(0xf2c14e, { roughness: 0.35 });
  const handle = paint(0x3f2b4b, { roughness: 0.45 });
  const rimBottom = -(R + TUBE);
  // The handle swells a little under the fingers and rounds off at the end.
  const profile = [
    [0, -0.143], [0.0075, -0.1425], [0.0098, -0.1392], [0.0105, -0.131], [0.0103, -0.112], [0.0094, -0.091],
    [0.0086, -0.075], [0.0083, -0.064], [0, -0.0625],
  ].map(([r, y]) => new THREE.Vector2(r, y));

  const model = new THREE.Group();
  model.name = 'magnifier';
  model.add(
    part(new THREE.TorusGeometry(R, TUBE, 14, 56), gold, 'rim'),
    part(new THREE.CylinderGeometry(R, R, 0.004, 48).rotateX(Math.PI / 2), glass(0xbfe6ff, 0.22), 'lens'),
    glint(R, 0.0021, false),
    glint(R, -0.0021, true),
    part(new THREE.CylinderGeometry(0.0085, 0.0095, 0.013, 20), gold, 'collar', [0, rimBottom - 0.0055, 0]),
    part(new THREE.LatheGeometry(profile, 20), handle, 'handle'),
    part(new THREE.SphereGeometry(0.0068, 16, 10), gold, 'knob', [0, -0.1448, 0]),
  );
  // In the fist: the middle of the handle.
  return { model, holdAt: new THREE.Vector3(0, -0.103, 0), shape: { curl: 1.05, spread: 0, thumb: 0.8 } };
}

// A compact camera, about 0.11 m wide: rounded cream body with a rainbow band (her colours, and the
// streak she flies in), a lens barrel with a glossy glass front, a flash window and a pink shutter
// button. The lens faces +Z; the shutter sits on top at the photographer's right (-X), the end her
// right hand holds.
function camera() {
  const W = 0.108;
  const H = 0.064;
  const D = 0.032;
  const front = D / 2;
  const [lx, ly] = [0.008, -0.004]; // where the lens axis meets the front
  const barrel = 0.014; // how far the barrel stands out
  const glassZ = front + barrel + 0.0054; // just proud of the ring's face
  const shine = glass(0xffffff, 0.9);
  const flash = paint(0xcfe0f2, { emissive: 0x1c2630 }); // a faint glow: glass over a reflector
  // Cylinders stand along Y; turned to stand out of the front.
  const disc = (r0, r1, depth) => new THREE.CylinderGeometry(r0, r1, depth, 32).rotateX(Math.PI / 2);
  const model = new THREE.Group();
  model.name = 'camera';
  model.add(
    part(new RoundedBoxGeometry(W, H, D, 4, 0.009), paint(0xf7f2e8), 'body'),
    part(disc(0.0205, 0.0215, barrel), paint(0x34343f), 'barrel', [lx, ly, front + barrel / 2]),
    part(disc(0.0168, 0.0168, 0.005), paint(0xe4e6ee, { roughness: 0.3 }), 'ring', [lx, ly, front + barrel + 0.0025]),
    part(new THREE.CircleGeometry(0.0128, 32), paint(0x1d3263, { roughness: 0.2 }), 'glass', [lx, ly, glassZ]),
    part(new THREE.CircleGeometry(0.0036, 16), shine, 'glint', [lx - 0.0045, ly + 0.0045, glassZ + 0.0003]),
    part(new THREE.CircleGeometry(0.0016, 12), shine, 'speck', [lx + 0.0048, ly - 0.0042, glassZ + 0.0003]),
    part(new RoundedBoxGeometry(0.019, 0.011, 0.003, 2, 0.0012), flash, 'flash', [0.034, 0.019, front]),
    part(new THREE.CylinderGeometry(0.0068, 0.0068, 0.005, 20), paint(0xff7aa2), 'shutter', [-0.031, H / 2 + 0.0022, -0.002]),
    part(new RoundedBoxGeometry(0.062, 0.04, 0.002, 2, 0.003), paint(0x28303f, { roughness: 0.25 }), 'screen', [0.006, 0, -front]),
  );
  for (const name of ['glint', 'speck']) model.getObjectByName(name).renderOrder = 1; // after the glass under them
  // Rainbow Dash's mane, red on top, running from the grip end to the flash (the barrel covers its middle).
  const band = 0.0024;
  [0xff5a5f, 0xffa34d, 0xffdf5e, 0x6fd88a, 0x5bb8f5, 0x9b7bf0].forEach((color, i) => {
    const y = 0.028 - (i + 0.5) * band;
    model.add(part(new THREE.BoxGeometry(0.068, band, 0.001), paint(color), `band${i}`, [-0.011, y, front + 0.0004]));
  });
  // Held in the left hand by its +X end: palm flat on that end, the fingers curled over the front edge
  // and resting on the front clear of the lens. The right hand mirrors this and holds the -X end, by
  // the shutter. (Tuned by eye on the real model.)
  return { model, holdAt: new THREE.Vector3(W / 2 - 0.009, 0, front - 0.012), shape: { curl: 0.9, spread: 0.02, thumb: 0.3 } };
}

// A thin bar from `a` to `b` ([x, y] on a flat face at depth z), drawn from `a`: scaling it along x draws it in.
function stroke(name, [ax, ay], [bx, by], width, material, z) {
  const length = Math.hypot(bx - ax, by - ay);
  const geometry = new THREE.BoxGeometry(length, width, 0.0006).translate(length / 2, 0, 0);
  const mesh = part(geometry, material, name, [ax, ay, z]);
  mesh.rotation.z = Math.atan2(by - ay, bx - ax);
  return mesh;
}

// A sky-blue clipboard, about 0.22 m tall with its clip: a sheet with five tasks on it (the first two ticked off), a silver
// clip at the top and a pencil tucked across it. The sheet faces +Z, the clip is up (+Y). Held by its side
// edge, the board running across her palm into her fingers: the left hand holds its -X edge (the right hand,
// mirrored, the other). Its rig: `tick` draws a tick into the next empty box, as if with her other hand;
// once all five are done the list starts over.
export const CLIPBOARD_ROWS = 5;
const CLIPBOARD_DONE = 2; // ticked off from the start
function clipboard() {
  const W = 0.15;
  const H = 0.21;
  const D = 0.005;
  const front = D / 2;
  const sheet = front + 0.0006;
  const ink = sheet + 0.0006;
  const green = paint(0x2fb45a);
  const line = paint(0x9fb3d1);
  const model = new THREE.Group();
  model.name = 'clipboard';
  model.add(
    part(new RoundedBoxGeometry(W, H, D, 2, 0.0022), paint(0x6fc3ee), 'board'),
    part(new THREE.BoxGeometry(0.128, 0.165, 0.0012), paint(0xfdfdf8), 'sheet', [0, -0.014, sheet]),
    part(new RoundedBoxGeometry(0.07, 0.028, 0.006, 2, 0.002), paint(0xc9ced8, { roughness: 0.3 }), 'clip', [0, H / 2 - 0.016, front + 0.003]),
    part(new THREE.TorusGeometry(0.013, 0.0024, 8, 24, Math.PI), paint(0xb3b9c6, { roughness: 0.3 }), 'lever', [0, H / 2 - 0.006, front + 0.006]),
    // A cartoon shine along the clip's top edge.
    part(new THREE.PlaneGeometry(0.05, 0.0028), glass(0xffffff, 0.75), 'shine', [-0.004, H / 2 - 0.006, front + 0.0062]),
  );
  // A pencil lying across the clip: yellow, a pink eraser, a sharpened tip.
  const pencil = new THREE.Group();
  pencil.name = 'pencil';
  pencil.add(
    part(new THREE.CylinderGeometry(0.0034, 0.0034, 0.08, 6), paint(0xffcf40), 'wood'),
    part(new THREE.CylinderGeometry(0.0035, 0.0035, 0.006, 12), paint(0xc9ced8), 'ferrule', [0, 0.043, 0]),
    part(new THREE.CylinderGeometry(0.0033, 0.0033, 0.006, 12), paint(0xff9fb5), 'eraser', [0, 0.049, 0]),
    part(new THREE.ConeGeometry(0.0034, 0.011, 6), paint(0xf1d3a0), 'tip', [0, -0.0455, 0]),
  );
  pencil.getObjectByName('tip').rotation.x = Math.PI; // the point down, away from the eraser
  pencil.rotation.z = Math.PI / 2 + 0.12;
  pencil.position.set(0.004, H / 2 - 0.021, front + 0.0095);
  model.add(pencil);
  // The list: a box and a line per task, top to bottom; a hidden tick in every box.
  const rows = [];
  const lengths = [0.078, 0.062, 0.084, 0.07, 0.056];
  const edge = paint(0x6c7a94);
  for (let i = 0; i < CLIPBOARD_ROWS; i++) {
    const y = 0.043 - i * 0.027;
    const box = new THREE.Group();
    box.name = `row${i}`;
    box.position.set(-0.047, y, 0);
    // The box's outline: four thin bars.
    for (const [w, h, x, yy] of [
      [0.011, 0.0012, 0, 0.0049],
      [0.011, 0.0012, 0, -0.0049],
      [0.0012, 0.011, 0.0049, 0],
      [0.0012, 0.011, -0.0049, 0],
    ]) {
      box.add(part(new THREE.BoxGeometry(w, h, 0.0005), edge, 'edge', [x, yy, ink]));
    }
    // Its tick: a short stroke down, then a long one up past the corner, like a quick pen mark.
    const first = stroke('tick-a', [-0.0046, 0.0004], [-0.0012, -0.0036], 0.0019, green, ink + 0.0002);
    const second = stroke('tick-b', [-0.0014, -0.0036], [0.0066, 0.0071], 0.0019, green, ink + 0.0002);
    box.add(first, second);
    model.add(box);
    model.add(part(new THREE.BoxGeometry(lengths[i], 0.0028, 0.0005), line, `line${i}`, [-0.035 + lengths[i] / 2, y, ink]));
    rows.push([first, second]);
  }
  const draw = (row, p) => {
    const [first, second] = rows[row];
    const a = smoothstep(p / 0.4);
    const b = smoothstep((p - 0.4) / 0.6);
    first.scale.x = Math.max(a, 1e-3);
    first.visible = a > 0;
    second.scale.x = Math.max(b, 1e-3);
    second.visible = b > 0;
  };
  let done = CLIPBOARD_DONE;
  let drawing = null; // { row, t } while a tick draws in
  const reset = () => {
    done = CLIPBOARD_DONE;
    drawing = null;
    for (let i = 0; i < CLIPBOARD_ROWS; i++) draw(i, i < done ? 1 : 0);
  };
  reset();
  const rig = {
    // `tick`: the next empty box gets its tick, drawn in over TICK_DRAW_S (the tick gesture's strokes).
    act(name) {
      if (name !== 'tick') return false;
      if (drawing) draw(drawing.row, 1);
      if (done >= CLIPBOARD_ROWS) {
        done = CLIPBOARD_DONE;
        for (let i = done; i < CLIPBOARD_ROWS; i++) draw(i, 0);
      }
      drawing = { row: done++, t: 0 };
      return true;
    },
    update(dt) {
      if (!drawing) return;
      drawing.t += dt;
      const p = drawing.t / TICK_DRAW_S;
      draw(drawing.row, Math.min(1, p));
      if (p >= 1) drawing = null;
    },
    reset,
    get ticked() {
      return done;
    },
  };
  // In the hand: at its side edge, near the bottom corner, so her fingers stay clear of the list.
  return {
    model,
    holdAt: new THREE.Vector3(-W / 2 + 0.014, -0.082, 0),
    shape: { curl: 0.6, spread: 0.02, thumb: 0.15 },
    rig,
  };
}
// How long a tick takes to draw in, from `act('tick')`: as long as the tick gesture's two strokes in the air.
export const TICK_DRAW_S = 0.32;

// A stopwatch, about 0.085 m tall with its crown (a little big, so it reads at desktop size): a sky-blue case
// with a golden bezel, crown and hanging ring, a white face with twelve marks and a red hand, under glass. The
// face is +Z and the crown +Y. It lies in her palm, face out, her thumb over the crown. Its rig: `start` presses
// the crown and sets the hand sweeping round; `ring` presses it again, snaps the hand to twelve and shakes the
// whole watch, ringing, for STOPWATCH_RING_S; `stop` just stops the hand.
export const STOPWATCH_LAP_S = 4; // a turn of the hand: quicker than a real one, so it's seen to run
export const STOPWATCH_RING_S = 1.3;
function stopwatch() {
  const R = 0.031;
  const D = 0.014;
  const front = D / 2;
  const gold = paint(0xf2c14e, { roughness: 0.35 });
  const dark = paint(0x2b2b3a);
  const model = new THREE.Group();
  model.name = 'stopwatch';
  const body = new THREE.Group(); // everything that shakes when it rings
  body.name = 'body';
  model.add(body);
  body.add(
    part(new THREE.CylinderGeometry(R, R, D, 40).rotateX(Math.PI / 2), paint(0x5bb8f5), 'case'),
    part(new THREE.TorusGeometry(R - 0.0006, 0.0028, 12, 48), gold, 'bezel', [0, 0, front]),
    part(new THREE.CircleGeometry(R - 0.003, 48), paint(0xfbfbf7), 'face', [0, 0, front + 0.0004]),
  );
  for (let i = 0; i < 12; i++) {
    const a = (i * Math.PI) / 6;
    const long = i % 3 === 0;
    const mark = part(new THREE.BoxGeometry(0.0013, long ? 0.0048 : 0.0028, 0.0004), dark, `mark${i}`);
    const r = R - (long ? 0.0072 : 0.0062);
    mark.position.set(Math.sin(a) * r, Math.cos(a) * r, front + 0.0008);
    mark.rotation.z = -a;
    body.add(mark);
  }
  const hand = new THREE.Group();
  hand.name = 'hand';
  hand.position.z = front + 0.0012;
  hand.add(part(new THREE.BoxGeometry(0.0014, 0.0205, 0.0005).translate(0, 0.0078, 0), paint(0xee4144), 'needle'));
  body.add(
    hand,
    part(new THREE.CylinderGeometry(0.0022, 0.0022, 0.001, 16).rotateX(Math.PI / 2), gold, 'cap', [0, 0, front + 0.0017]),
    part(new THREE.CircleGeometry(R - 0.003, 48), glass(0xdff4ff, 0.16), 'glass', [0, 0, front + 0.0024]),
    glint(R - 0.004, front + 0.0026, false),
  );
  // The crown on top (it presses in), its stem, and the ring it hangs by; a button either side of it.
  const crown = new THREE.Group();
  crown.name = 'crown';
  crown.add(
    part(new THREE.CylinderGeometry(0.0052, 0.0052, 0.0045, 20), gold, 'button', [0, R + 0.0068, 0]),
    part(new THREE.TorusGeometry(0.0062, 0.0014, 8, 24), gold, 'ring', [0, R + 0.0158, 0]),
  );
  body.add(part(new THREE.CylinderGeometry(0.0032, 0.0032, 0.006, 16), gold, 'stem', [0, R + 0.002, 0]), crown);
  for (const side of [-1, 1]) {
    const a = side * (Math.PI / 4);
    const button = part(new THREE.CylinderGeometry(0.0026, 0.0026, 0.0045, 12), gold, side < 0 ? 'lap' : 'reset');
    button.position.set(Math.sin(a) * (R + 0.0018), Math.cos(a) * (R + 0.0018), 0);
    button.rotation.z = -a;
    body.add(button);
  }
  let running = false;
  let press = Infinity; // s since the crown was last pressed
  let ringing = Infinity; // s since it started ringing
  const rig = {
    act(name) {
      if (name === 'start') {
        running = true;
        press = 0;
      } else if (name === 'ring') {
        running = false;
        hand.rotation.z = 0;
        press = 0;
        ringing = 0;
      } else if (name === 'stop') {
        running = false;
      } else {
        return false;
      }
      return true;
    },
    update(dt) {
      press += dt;
      ringing += dt;
      if (running) hand.rotation.z -= (dt * 2 * Math.PI) / STOPWATCH_LAP_S; // clockwise, seen from the front
      crown.position.y = -0.0018 * bump(press, 0, 0.2);
      // (Only while it rings: at rest `ringing` is Infinity, and 0 * sin(Infinity) is NaN.)
      const shake = ringing < STOPWATCH_RING_S ? 1 - smoothstep(ringing / STOPWATCH_RING_S) : 0;
      body.rotation.z = shake && 0.09 * shake * Math.sin(2 * Math.PI * 23 * ringing);
      body.position.x = shake && 0.0012 * shake * Math.sin(2 * Math.PI * 31 * ringing + 1);
    },
    reset() {
      running = false;
      press = Infinity;
      ringing = Infinity;
      hand.rotation.z = 0;
      rig.update(0);
    },
    get running() {
      return running;
    },
  };
  rig.reset();
  // Lying in the palm: its middle a little toward the crown from the fist's middle. Turned a quarter round
  // her fist's axis so the face looks out of her palm instead of along her fingers (see the header).
  return { model, holdAt: new THREE.Vector3(0, -0.004, -0.002), turn: [0, 0, -90], shape: { curl: 0.42, spread: 0.02, thumb: 0.5 }, rig };
}

// A brass hand bell, about 0.16 m tall: a flared bell with a clapper hanging inside, a golden collar and a
// violet wooden handle with a round knob. The handle is up (+Y), the mouth down. Held like the magnifier, by
// the middle of its handle, so it hangs below her fist. Its rig: `ring` swings the clapper, fading out over
// BELL_RING_S s.
export const BELL_RING_S = 1.5;
function bell() {
  const brass = paint(0xf2c14e, { roughness: 0.3 });
  const wood = paint(0x6b4bb8, { roughness: 0.45 });
  // Outside up from the lip, then back down the inside, so the open mouth shows a lining.
  const shell = [
    [0.0355, -0.0006], [0.0378, 0.0022], [0.0352, 0.0085], [0.0296, 0.0175], [0.0254, 0.03], [0.0228, 0.045],
    [0.0212, 0.057], [0.0196, 0.066], [0.0162, 0.0722], [0.0092, 0.0756], [0, 0.0766],
    [0, 0.0738], [0.0078, 0.0727], [0.0146, 0.0686], [0.0178, 0.0602], [0.0194, 0.0472], [0.0219, 0.0312],
    [0.0259, 0.0192], [0.0308, 0.0102], [0.0336, 0.0034], [0.0346, -0.0002],
  ].map(([r, y]) => new THREE.Vector2(r, y));
  const handle = [
    [0, 0.081], [0.0076, 0.0812], [0.0082, 0.086], [0.0095, 0.1], [0.0097, 0.114], [0.0086, 0.128], [0.0078, 0.137],
    [0.0092, 0.1405], [0, 0.141],
  ].map(([r, y]) => new THREE.Vector2(r, y));
  const model = new THREE.Group();
  model.name = 'bell';
  model.add(
    part(new THREE.LatheGeometry(shell, 40), brass, 'shell'),
    part(new THREE.TorusGeometry(0.0356, 0.0018, 8, 40).rotateX(Math.PI / 2), brass, 'lip', [0, 0.0006, 0]),
    part(new THREE.CylinderGeometry(0.0088, 0.0098, 0.007, 20), brass, 'collar', [0, 0.0795, 0]),
    part(new THREE.LatheGeometry(handle, 20), wood, 'handle'),
    part(new THREE.SphereGeometry(0.0108, 16, 12), wood, 'knob', [0, 0.1495, 0]),
    // A cartoon shine down the front of the bell: a thin strip of its own outline, just proud of the brass.
    part(
      new THREE.LatheGeometry(shell.slice(2, 8).map((v) => new THREE.Vector2(v.x * 1.012, v.y)), 1, -0.62, 0.16),
      glass(0xffffff, 0.55),
      'shine',
    ),
  );
  // The clapper swings from just under the crown of the bell.
  const clapper = new THREE.Group();
  clapper.name = 'clapper';
  clapper.position.y = 0.069;
  const iron = paint(0x5d5448, { roughness: 0.5 });
  clapper.add(
    part(new THREE.CylinderGeometry(0.0014, 0.0014, 0.05).translate(0, -0.025, 0), iron, 'rod'),
    part(new THREE.SphereGeometry(0.0062, 14, 10), iron, 'ball', [0, -0.055, 0]),
  );
  model.add(clapper);
  let ringing = Infinity;
  const rig = {
    act(name) {
      if (name !== 'ring') return false;
      ringing = 0;
      return true;
    },
    update(dt) {
      ringing += dt;
      const left = ringing < BELL_RING_S ? 1 - smoothstep(ringing / BELL_RING_S) : 0;
      // Hitting one side of the bell, then the other (at rest `ringing` is Infinity: no sin of that).
      clapper.rotation.z = left && 0.42 * left * Math.sin(2 * Math.PI * 3.4 * ringing);
    },
    reset() {
      ringing = Infinity;
      rig.update(0);
    },
  };
  rig.reset();
  return { model, holdAt: new THREE.Vector3(0, 0.11, 0), shape: { curl: 1.0, spread: 0, thumb: 0.75 }, rig };
}

// ---- phase 2: the web, your files, the morning, builds, music, shouting and the weather ----

const RAINBOW = [0xff5a5f, 0xffa34d, 0xffdf5e, 0x6fd88a, 0x5bb8f5, 0x9b7bf0]; // her mane, red to violet

// Painted per vertex from where it points (a unit vector): sea, land in soft blobs, and white ice at the poles.
function globeColours(geometry) {
  const sea = new THREE.Color(0x3d8fe0);
  const land = new THREE.Color(0x5cc46a);
  const ice = new THREE.Color(0xf4f8ff);
  const v = new THREE.Vector3();
  const colours = [];
  const pos = geometry.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).normalize();
    const blobs = Math.sin(3.1 * v.x + 1.3) * Math.sin(2.7 * v.y + 0.4) * Math.sin(3.7 * v.z + 2.1) + 0.35 * Math.sin(5.3 * v.x * v.z + 1.7);
    const c = Math.abs(v.y) > 0.9 ? ice : blobs > 0.16 ? land : sea;
    colours.push(c.r, c.g, c.b);
  }
  geometry.setAttribute('color', new THREE.Float32BufferAttribute(colours, 3));
  return geometry;
}

// A desk globe, about 0.12 m tall: a blue sea with green land and white poles, tilted on a golden half meridian
// over a round violet stand. Held on her flat palm, the stand's foot in it (turned: its up out of her palm). Its
// rig: the ball turns slowly all the while; `spin` flicks it round faster, slowing back down over a few seconds.
export const GLOBE_TURN = 1.1; // rad/s it turns by itself
function globe() {
  const R = 0.046;
  const ring = R + 0.006;
  const gold = paint(0xf2c14e, { roughness: 0.35 });
  const model = new THREE.Group();
  model.name = 'globe';
  model.add(
    part(new THREE.CylinderGeometry(0.03, 0.034, 0.009, 32), paint(0x6b4bb8, { roughness: 0.45 }), 'base', [0, -0.0655, 0]),
    part(new THREE.CylinderGeometry(0.0038, 0.0045, 0.012, 12), gold, 'stem', [0, -0.055, 0]),
  );
  // Tilted about the bottom pin, where the meridian meets the stem.
  const tilt = new THREE.Group();
  tilt.name = 'tilt';
  tilt.position.y = -0.049;
  tilt.rotation.z = -0.41;
  model.add(tilt);
  const ball = new THREE.Group();
  ball.name = 'ball';
  ball.position.y = ring;
  ball.add(part(globeColours(new THREE.SphereGeometry(R, 40, 28)), paint(0xffffff, { vertexColors: true, roughness: 0.4 }), 'earth'));
  tilt.add(
    ball,
    // A half ring from the bottom pin round the back to the top one.
    part(new THREE.TorusGeometry(ring, 0.0022, 8, 40, Math.PI).rotateZ(-Math.PI / 2).rotateY(Math.PI / 2), gold, 'meridian', [0, ring, 0]),
    part(new THREE.SphereGeometry(0.0042, 12, 8), gold, 'pin', [0, 2 * ring, 0]),
    part(new THREE.SphereGeometry(0.0042, 12, 8), gold, 'pin-bottom', [0, 0, 0]),
  );
  let speed = GLOBE_TURN;
  const rig = {
    act(name) {
      if (name !== 'spin') return false;
      speed = 9;
      return true;
    },
    update(dt) {
      speed = GLOBE_TURN + (speed - GLOBE_TURN) * Math.exp(-dt / 1.2);
      ball.rotation.y += speed * dt;
    },
    reset() {
      speed = GLOBE_TURN;
      ball.rotation.y = 0;
    },
  };
  return { model, holdAt: new THREE.Vector3(0, -0.07, 0), turn: [90, 0, 0], shape: { curl: 0.28, spread: 0.12, thumb: 0.12 }, rig };
}

// An open manila folder, about 0.26 m tall: two covers in a shallow V facing +Z, with printed sheets on both and a
// page that keeps turning over from right to left, as if she's flipping through. Held up like the clipboard, by
// the left cover's outer bottom corner (the left hand; mirrored in the right).
export const FOLDER_FLIP_EVERY_S = 2.2;
const FOLDER_FLIP_S = 0.45;
function folder() {
  const W = 0.19;
  const H = 0.26;
  const open = 0.32; // rad each cover is turned toward you from flat
  const manila = paint(0xf5d58a);
  const paper = paint(0xfdfdf8);
  const ink = paint(0x9fb3d1);
  const model = new THREE.Group();
  model.name = 'folder';
  // A cover with its sheet (printed, or blank: the back of a page), hinged on the spine; `side` -1 left, 1 right.
  const leaf = (side, name, { cover = true, printed = true } = {}) => {
    const hinge = new THREE.Group();
    hinge.name = name;
    hinge.rotation.y = -side * open;
    if (cover) hinge.add(part(new THREE.BoxGeometry(W, H, 0.002), manila, 'cover', [(side * W) / 2, 0, 0]));
    const sheet = new THREE.Group();
    sheet.name = 'sheet';
    sheet.position.set(side * (W / 2 - 0.004), -0.008, 0.002);
    sheet.add(part(new THREE.BoxGeometry(W - 0.022, H - 0.03, 0.0008), paper, 'paper'));
    for (let i = 0; printed && i < 7; i++) {
      const length = [0.12, 0.14, 0.1, 0.13, 0.08, 0.14, 0.11][i];
      sheet.add(part(new THREE.BoxGeometry(length, 0.0035, 0.0004), ink, `line${i}`, [-0.075 + length / 2, 0.085 - i * 0.026, 0.0007]));
    }
    hinge.add(sheet);
    return hinge;
  };
  const left = leaf(-1, 'left', { printed: false });
  const right = leaf(1, 'right');
  const page = leaf(1, 'page', { cover: false }); // the one turning over
  page.position.z = 0.0012;
  // The tab on the back cover, sticking up.
  right.add(part(new RoundedBoxGeometry(0.06, 0.022, 0.002, 2, 0.004), manila, 'tab', [W - 0.05, H / 2 + 0.008, -0.0005]));
  model.add(left, right, page);
  let t = 0;
  const rig = {
    act() {
      return false;
    },
    update(dt) {
      t += dt;
      // From lying on the right cover (turned -open) over to the left one (turned open - pi: the same page,
      // face down), standing out toward you half way. Then the next page lies on the right, looking the same.
      const p = smoothstep((t % FOLDER_FLIP_EVERY_S) / FOLDER_FLIP_S);
      page.rotation.y = -open - p * (Math.PI - 2 * open);
    },
    reset() {
      t = FOLDER_FLIP_S; // a page lying flat, and the next one turning soon
      rig.update(0);
    },
  };
  rig.reset();
  const corner = new THREE.Vector3(-W + 0.012, -H / 2 + 0.03, 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), open);
  return { model, holdAt: corner, shape: { curl: 0.6, spread: 0.02, thumb: 0.15 }, rig };
}

// A mug of hot chocolate, about 0.1 m tall: sky blue with a white cloud and a yellow lightning bolt on the front
// (her mark), a handle on the right, and steam curling up. Held by its handle, which runs up through her fist. Its
// rig: the steam rises and sways by itself.
function mug() {
  const R = 0.036;
  const H = 0.086;
  const blue = paint(0x6fc3ee, { side: THREE.DoubleSide });
  const white = paint(0xfdfdf8);
  const model = new THREE.Group();
  model.name = 'mug';
  model.add(
    part(new THREE.CylinderGeometry(R, R * 0.93, H, 36, 1, true), blue, 'body'),
    part(new THREE.CircleGeometry(R * 0.93, 36).rotateX(Math.PI / 2), blue, 'bottom', [0, -H / 2, 0]),
    part(new THREE.CylinderGeometry(R - 0.003, R * 0.93 - 0.003, H - 0.004, 36, 1, true), paint(0xfdfdf8, { side: THREE.BackSide }), 'inside', [0, 0.002, 0]),
    part(new THREE.CircleGeometry(R - 0.003, 36).rotateX(-Math.PI / 2), paint(0x6b3f2a, { roughness: 0.3 }), 'cocoa', [0, H / 2 - 0.012, 0]),
    part(new THREE.TorusGeometry(R - 0.0012, 0.0025, 8, 36).rotateX(Math.PI / 2), blue, 'rim', [0, H / 2, 0]),
    part(new THREE.TorusGeometry(0.022, 0.0062, 10, 24, Math.PI).rotateZ(-Math.PI / 2), blue, 'handle', [R - 0.002, 0.002, 0]),
  );
  // Her mark on the front: a white cloud with a yellow bolt coming out of it.
  const mark = new THREE.Group();
  mark.name = 'mark';
  mark.position.set(0, 0.004, R + 0.0006);
  for (const [x, y, r] of [
    [-0.009, 0.004, 0.008],
    [0.001, 0.009, 0.01],
    [0.011, 0.004, 0.0075],
    [0.001, 0.0005, 0.009],
  ]) {
    mark.add(part(new THREE.CircleGeometry(r, 20), white, 'cloud', [x, y, 0]));
  }
  const bolt = new THREE.Shape();
  [[0.002, -0.004], [-0.006, -0.02], [0.001, -0.018], [-0.004, -0.032], [0.01, -0.013], [0.003, -0.015], [0.009, -0.004]].forEach(([x, y], i) =>
    i ? bolt.lineTo(x, y) : bolt.moveTo(x, y),
  );
  mark.add(part(new THREE.ShapeGeometry(bolt), paint(0xffdf5e), 'bolt', [0, 0, 0.0004]));
  model.add(mark);
  // Steam: three soft curls rising from the top.
  const wisps = [];
  for (let i = 0; i < 3; i++) {
    const x = (i - 1) * 0.012;
    const curve = new THREE.CatmullRomCurve3(
      [0, 0.02, 0.04, 0.06, 0.075].map((y, k) => new THREE.Vector3(x + 0.006 * Math.sin(k * 1.7 + i), y, 0.004 * Math.cos(k * 1.3 + i))),
    );
    const material = glass(0xffffff, 0.5);
    const wisp = part(new THREE.TubeGeometry(curve, 24, 0.0032, 6, false), material, `steam${i}`, [0, H / 2 - 0.004, 0]);
    wisps.push(wisp);
    model.add(wisp);
  }
  let t = 0;
  const rig = {
    act() {
      return false;
    },
    update(dt) {
      t += dt;
      wisps.forEach((wisp, i) => {
        const p = (t * 0.45 + i / 3) % 1; // each curl rises, fades and starts again, a third of a cycle apart
        wisp.position.y = H / 2 - 0.004 + 0.02 * p;
        wisp.scale.set(1 + 0.4 * p, 0.7 + 0.5 * p, 1 + 0.4 * p);
        wisp.rotation.y = 0.6 * Math.sin(t * 0.9 + i * 2);
        wisp.material.opacity = 0.5 * Math.sin(Math.PI * p);
      });
    },
    reset() {
      t = 0;
      rig.update(0);
    },
  };
  rig.reset();
  return { model, holdAt: new THREE.Vector3(R + 0.02, 0.002, 0), shape: { curl: 1.0, spread: 0, thumb: 0.65 }, rig };
}

// A combination wrench, about 0.26 m long (big enough to read at desktop size): chrome, an open jaw at the top and a
// ring at the bottom, a sky-blue grip round the middle. Held in the fist by its grip, along her thumb, its flat side
// facing where her fingers point (so a wrench held up beside her is seen flat on). Its rig: `twirl` spins it round
// twice in her hand, flat on to you like a baton twirled at an audience, and it lands back where it was.
export const WRENCH_TWIRL_S = 0.7;
function wrench() {
  const chrome = paint(0xcfd5df, { roughness: 0.25 });
  const model = new THREE.Group();
  model.name = 'wrench';
  const spin = new THREE.Group(); // turns about the grip for a twirl
  spin.name = 'spin';
  model.add(spin);
  const body = new THREE.Group();
  body.name = 'body';
  body.scale.setScalar(1.3);
  spin.add(body);
  body.add(
    part(new RoundedBoxGeometry(0.014, 0.15, 0.0065, 2, 0.003), chrome, 'shaft', [0, 0.018, 0]),
    part(new RoundedBoxGeometry(0.019, 0.05, 0.011, 2, 0.004), paint(0x5bb8f5, { roughness: 0.5 }), 'grip', [0, 0, 0]),
    // The jaw: most of a ring, open at the top.
    part(new THREE.TorusGeometry(0.016, 0.0065, 8, 24, Math.PI * 1.35).rotateZ(-1.175 * Math.PI), chrome, 'jaw', [0, 0.1, 0]),
    part(new THREE.TorusGeometry(0.0115, 0.0045, 8, 24), chrome, 'ring', [0, -0.066, 0]),
  );
  let twirling = Infinity;
  const rig = {
    act(name) {
      if (name !== 'twirl') return false;
      twirling = 0;
      return true;
    },
    update(dt) {
      twirling += dt;
      const p = twirling < WRENCH_TWIRL_S ? twirling / WRENCH_TWIRL_S : 1;
      spin.rotation.z = 4 * Math.PI * (1 - (1 - p) ** 3); // off fast, easing into the catch
    },
    reset() {
      twirling = Infinity;
      rig.update(0);
    },
  };
  rig.reset();
  return { model, holdAt: new THREE.Vector3(0, 0, 0), shape: { curl: 1.1, spread: 0, thumb: 0.8 }, rig };
}

// Headphones, worn: sleek, studio-style, in black: a matte black band over her head with a softer pad along the
// top, gunmetal sliders and yokes, tall oval black cups with a gunmetal plate on the outside ringed by a thin
// sky-blue accent that glows, and soft dark cushions against her ears. Sized for her head and hair: the band clears
// her crown and the cushions' inner faces are 0.236 m apart, over her ears. On her head, not in a hand (props.js's
// head slot): +Z is where she faces, and `holdAt` is the point that sits between her eyes, in front of the band's
// middle and a little below it. Its rig: while they're on, eighth notes (HEADPHONE_NOTES of them, in the accent
// colour) drift up beside her head, one side then the other, each growing in and fading out over HEADPHONE_NOTE_S.
export const HEADPHONE_NOTES = 3;
export const HEADPHONE_NOTE_S = 1.8;
const HEADPHONE_ACCENT = 0x5bc8ff;
function headphones() {
  const band = 0.142; // the band's radius: half the width, over her hair
  const tall = 1.15; // the cups' oval: this much taller than wide
  const R = 0.05; // a cup's radius, front to back: 0.1 m wide, 0.115 m tall
  const cupY = -0.062; // the cups' middles, below the band's ends
  const cupX = 0.149; // ... and out from the middle: the cushions' inner faces 0.236 m apart
  const model = new THREE.Group();
  model.name = 'headphones';
  const black = paint(0x141418, { roughness: 0.8 });
  const gunmetal = paint(0x4a4f58, { roughness: 0.35, metalness: 0.6 });
  // A torus (or, axis 'y', a cylinder) turned to face out sideways, its axis along X, and stretched into the oval.
  const oval = (geometry, axis) => (axis === 'y' ? geometry.rotateZ(Math.PI / 2) : geometry.rotateY(Math.PI / 2)).scale(1, tall, 1);
  model.add(
    part(new THREE.TorusGeometry(band, 0.0075, 10, 56, Math.PI), black, 'band'),
    // The pad: a softer, thicker strip under the top of the band, where it rests on her hair.
    part(new THREE.TorusGeometry(band - 0.0065, 0.0095, 10, 24, 1.0).rotateZ(Math.PI / 2 - 0.5), paint(0x25252c, { roughness: 0.95 }), 'pad'),
  );
  for (const side of [-1, 1]) {
    const cup = new THREE.Group();
    cup.name = side < 0 ? 'right-cup' : 'left-cup';
    cup.position.set(side * cupX, cupY, 0);
    const fork = R + 0.006; // the yoke's arch round the cup, front to back
    cup.add(
      // The slider out of the band's end, and the yoke: an arch over the cup, holding it at the front and back.
      part(new THREE.BoxGeometry(0.009, 0.026, 0.012), gunmetal, 'slider', [-side * 0.0035, fork * tall + 0.008, 0]),
      part(oval(new THREE.TorusGeometry(fork, 0.003, 8, 32, Math.PI)), gunmetal, 'yoke'),
      part(new THREE.SphereGeometry(0.0045, 12, 8), gunmetal, 'pivot-front', [0, 0, fork]),
      part(new THREE.SphereGeometry(0.0045, 12, 8), gunmetal, 'pivot-back', [0, 0, -fork]),
      part(oval(new THREE.CylinderGeometry(R, R, 0.028, 48), 'y'), paint(0x111114, { roughness: 0.6 }), 'cup'),
      part(oval(new THREE.TorusGeometry(0.038, 0.011, 10, 40)), paint(0x1b1b21, { roughness: 0.95 }), 'cushion', [-side * 0.02, 0, 0]),
      part(oval(new THREE.CylinderGeometry(0.036, 0.036, 0.003, 48), 'y'), gunmetal, 'plate', [side * 0.015, 0, 0]),
      // The accent, lit from inside (emissive) so it glows against the black either way it's shaded. Not a flat
      // material: toon-shaded it keeps its glow.
      part(oval(new THREE.TorusGeometry(0.041, 0.0016, 8, 64)), paint(HEADPHONE_ACCENT, { emissive: HEADPHONE_ACCENT, emissiveIntensity: 0.9 }), 'accent', [side * 0.0152, 0, 0]),
    );
    model.add(cup);
  }
  // The notes: eighth notes (a tilted oval head, a stem and a flag) glowing in the accent colour. Each has a
  // material of its own, shared by its parts, since each fades by itself; they come out a third of a cycle apart.
  const notes = [];
  for (let i = 0; i < HEADPHONE_NOTES; i++) {
    const glow = paint(HEADPHONE_ACCENT, { emissive: HEADPHONE_ACCENT, emissiveIntensity: 0.8, transparent: true, opacity: 0, depthWrite: false });
    const note = new THREE.Group();
    note.name = `note${i}`;
    note.add(
      part(new THREE.SphereGeometry(0.0085, 16, 10).scale(1.3, 0.9, 0.55).rotateZ(0.4), glow, 'head'),
      part(new THREE.BoxGeometry(0.0026, 0.034, 0.0026), glow, 'stem', [0.0098, 0.017, 0]),
      // From the stem's top, curling down and out.
      part(new THREE.TorusGeometry(0.011, 0.0018, 6, 12, Math.PI * 0.45).rotateZ(Math.PI), glow, 'flag', [0.0208, 0.034, 0]),
    );
    notes.push(note);
    model.add(note);
  }
  let t = 0;
  const rig = {
    act() {
      return false;
    },
    update(dt) {
      t += dt;
      notes.forEach((note, i) => {
        const since = t - (i * HEADPHONE_NOTE_S) / HEADPHONE_NOTES; // its first one came out this long ago
        const loop = Math.floor(Math.max(since, 0) / HEADPHONE_NOTE_S);
        const p = since < 0 ? 0 : (since - loop * HEADPHONE_NOTE_S) / HEADPHONE_NOTE_S;
        // One side then the other, note after note: the k-th note out goes up her left (+X) if k is even.
        const side = (loop * HEADPHONE_NOTES + i) % 2 === 0 ? 1 : -1;
        const rise = 1 - (1 - p) ** 2; // quick off the cup, slowing as it floats away
        note.position.set(side * (0.2 + 0.035 * p), -0.05 + 0.15 * rise, 0.012 + 0.012 * Math.sin(Math.PI * 2 * p + i));
        note.rotation.z = side * (0.1 + 0.25 * Math.sin(Math.PI * 2 * p + i * 1.7));
        note.scale.setScalar(Math.max(smoothstep(p / 0.3), 1e-3)); // grows in (never a zero scale: no singular matrix)
        const opacity = since < 0 ? 0 : 0.95 * smoothstep(p / 0.12) * (1 - smoothstep((p - 0.55) / 0.45));
        note.visible = opacity > 0.01;
        // Through the meshes, so it's whatever material toonify swapped in.
        note.traverse((o) => o.isMesh && (o.material.opacity = opacity));
      });
    },
    reset() {
      t = 0;
      rig.update(0);
    },
  };
  rig.reset();
  return { model, holdAt: new THREE.Vector3(0, -0.035, 0.088), shape: { curl: 0.3, spread: 0.05, thumb: 0.3 }, worn: true, rig };
}

// Sunglasses, worn: "deal with it" shades, bigger than her (big) eyes so they read as shades and not as eyeliner: two
// flat-topped dark lenses, rounded below, each with a white streak of a glint, under a thick sky-blue brow bar (it
// shows against dark hair), sky-blue rims and arms running back over her ears. On her head (props.js's head slot): +Z
// is where she faces; `holdAt`, behind the lenses, sits between her eyes.
function sunglasses() {
  const W = 0.062; // a lens
  const H = 0.04;
  const X = 0.036; // each lens's middle from the bridge
  const frame = paint(0x5bb8f5, { roughness: 0.35 });
  // Opaque and flat: dark glass, unshaded in either style.
  const shade = new THREE.MeshBasicMaterial({ color: 0x16121f });
  shade.userData.keepStyle = true;
  // A lens's outline, centred on (0, 0): straight along the top, curving round below.
  const outline = (w, h) => {
    const shape = new THREE.Shape();
    shape.moveTo(-w / 2, h / 2);
    shape.lineTo(w / 2, h / 2);
    shape.bezierCurveTo(w / 2, -h * 0.25, w * 0.32, -h / 2, 0, -h / 2);
    shape.bezierCurveTo(-w * 0.32, -h / 2, -w / 2, -h * 0.25, -w / 2, h / 2);
    return new THREE.ShapeGeometry(shape, 12);
  };
  const shine = glass(0xffffff, 0.85);
  const model = new THREE.Group();
  model.name = 'sunglasses';
  for (const side of [-1, 1]) {
    const lens = new THREE.Group();
    lens.name = side < 0 ? 'right-lens' : 'left-lens';
    lens.position.x = side * X;
    // A streak across the upper left and a short one beside it, flat on the glass.
    const streak = part(new THREE.PlaneGeometry(0.02, 0.0045), shine, 'glint', [-0.012, 0.006, 0.0006]);
    const speck = part(new THREE.PlaneGeometry(0.008, 0.0035), shine, 'speck', [0.003, 0.009, 0.0006]);
    streak.rotation.z = speck.rotation.z = 0.5;
    streak.renderOrder = speck.renderOrder = 1; // after the lens under them
    lens.add(
      part(outline(W, H), shade, 'lens'),
      part(outline(W + 0.008, H + 0.008), frame, 'rim', [0, 0, -0.0015]),
      streak,
      speck,
    );
    model.add(lens);
    model.add(part(new THREE.BoxGeometry(0.004, 0.006, 0.11), frame, side < 0 ? 'right-arm' : 'left-arm', [side * (X + W / 2 + 0.003), H / 2 - 0.004, -0.056]));
  }
  model.add(part(new RoundedBoxGeometry(2 * X + W + 0.012, 0.009, 0.007, 2, 0.003), frame, 'brow', [0, H / 2 + 0.001, -0.001]));
  return { model, holdAt: new THREE.Vector3(0, -0.002, -0.035), shape: { curl: 0.5, spread: 0, thumb: 0.4 }, worn: true };
}

// A megaphone, about 0.17 m long: a white cone with a red rim and a sky-blue band, a dark mouthpiece at the back and
// a handle underneath with a trigger. The wide end faces +Z. Held by the handle, which runs up through her fist, so
// the cone points where her fingers do.
function megaphone() {
  const model = new THREE.Group();
  model.name = 'megaphone';
  const dark = paint(0x2b2b3a);
  model.add(
    part(new THREE.CylinderGeometry(0.056, 0.016, 0.15, 36, 1, true).rotateX(Math.PI / 2), paint(0xf7f2e8, { side: THREE.DoubleSide }), 'cone', [0, 0, 0.01]),
    part(new THREE.TorusGeometry(0.056, 0.0042, 8, 36), paint(0xff5a5f), 'rim', [0, 0, 0.085]),
    part(new THREE.CylinderGeometry(0.0475, 0.0445, 0.012, 36, 1, true).rotateX(Math.PI / 2), paint(0x5bb8f5, { side: THREE.DoubleSide }), 'band', [0, 0, 0.055]),
    part(new THREE.CylinderGeometry(0.018, 0.016, 0.022, 20).rotateX(Math.PI / 2), dark, 'mouthpiece', [0, 0, -0.073]),
    part(new RoundedBoxGeometry(0.02, 0.066, 0.026, 2, 0.006), dark, 'handle', [0, -0.05, -0.022]),
    part(new RoundedBoxGeometry(0.008, 0.016, 0.01, 2, 0.002), paint(0xff5a5f), 'trigger', [0, -0.034, -0.005]),
  );
  return { model, holdAt: new THREE.Vector3(0, -0.055, -0.022), shape: { curl: 1.05, spread: 0, thumb: 0.75 } };
}

// An umbrella in her six colours, a parasol about 0.44 m across on a 0.62 m shaft: a dome of six panels with a white tip,
// a thin shaft and a violet J handle. The canopy is up (+Y). Held by the shaft just above the handle, along her thumb,
// so it can rest over her shoulder with the canopy behind her head. Its rig: it
// comes closed, and `open` pops the canopy open with a little bounce.
export const UMBRELLA_OPEN_S = 0.35;
function umbrella() {
  const profile = [
    [0, 0.1], [0.053, 0.097], [0.105, 0.087], [0.15, 0.071], [0.192, 0.049], [0.226, 0.023], [0.252, -0.003], [0.26, -0.009],
  ].map(([r, y]) => new THREE.Vector2(r * 0.85, y));
  const model = new THREE.Group();
  model.name = 'umbrella';
  const canopy = new THREE.Group();
  canopy.name = 'canopy';
  RAINBOW.forEach((colour, i) => {
    const panel = new THREE.LatheGeometry(profile, 6, (i * Math.PI) / 3, Math.PI / 3);
    canopy.add(part(panel, paint(colour, { side: THREE.DoubleSide }), `panel${i}`));
  });
  model.add(
    canopy,
    part(new THREE.CylinderGeometry(0.005, 0.008, 0.026, 12), paint(0xfdfdf8), 'tip', [0, 0.108, 0]),
    part(new THREE.CylinderGeometry(0.005, 0.005, 0.62, 12), paint(0xcfd5df, { roughness: 0.3 }), 'shaft', [0, -0.21, 0]),
    part(new THREE.TorusGeometry(0.024, 0.0078, 10, 24, Math.PI).rotateZ(Math.PI), paint(0x6b4bb8, { roughness: 0.45 }), 'handle', [0.024, -0.52, 0]),
  );
  let opening = Infinity;
  const rig = {
    act(name) {
      if (name !== 'open') return false;
      opening = 0;
      return true;
    },
    update(dt) {
      opening += dt;
      const p = opening < UMBRELLA_OPEN_S ? opening / UMBRELLA_OPEN_S : 1;
      // Closed: a furled cone hugging the shaft. Open: the dome, with a little bounce as it snaps out.
      const out = opening === Infinity ? 0 : easeOutBack(p, 2.2);
      canopy.scale.set(0.12 + 0.88 * out, 1.35 - 0.35 * out, 0.12 + 0.88 * out);
      canopy.position.y = -0.07 * (1 - Math.min(out, 1));
    },
    reset() {
      opening = Infinity;
      rig.update(0);
    },
  };
  rig.reset();
  return { model, holdAt: new THREE.Vector3(0, -0.495, 0), shape: { curl: 1.05, spread: 0, thumb: 0.75 }, rig };
}

export const BUILTINS = Object.freeze({
  magnifier,
  camera,
  clipboard,
  stopwatch,
  bell,
  globe,
  folder,
  mug,
  wrench,
  headphones,
  sunglasses,
  megaphone,
  umbrella,
});
// The built-ins she wears on her head rather than holding (props.js's head slot), a file of the same name too.
export const WORN_BUILTINS = Object.freeze(new Set(['headphones', 'sunglasses']));
// The hand a built-in goes in unless its config says: the right, but the clipboard, the folder and the globe go in
// her left, so her right can tick things off, spin the globe or rest on her hip (Octavia's config and
// lib/config.js say the same).
const BUILTIN_HANDS = Object.freeze({ clipboard: 'left', folder: 'left', globe: 'left' });
export const defaultHand = (builtin) => (builtin && Object.hasOwn(BUILTIN_HANDS, builtin) ? BUILTIN_HANDS[builtin] : 'right');
