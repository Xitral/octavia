// Pose library: Euler angles in radians for VRM *normalized* bones.
//
// Normalized space: the character faces +Z, +X is her left, +Y is up, and every bone's rotation is
// identity in the T-pose. That makes angles portable between VRM models. Limb angles are written for the
// LEFT side and mirrored for the right (mirroring across the body's midplane negates y and z), so both
// arms share one set of numbers.
//
// Arm bones use Euler order YZX, which reads like a shoulder joint (left side):
//   upper arm  x twists the arm about its own axis first (x > 0 turns the elbow's bend toward the body,
//              x < 0 turns the palm forward); z then raises/lowers it (0 = level, -pi/2 = hanging);
//              y finally picks the direction (0 = out to the side, -pi/2 = straight ahead).
//   lower arm  x twists the forearm (x < 0 turns the palm up), y < 0 bends the elbow.
//   hand       z < 0 bends the wrist toward the palm, y < 0 toward the thumb.
// Other bones use XYZ:
//   fingers    z < 0 curls toward the palm; thumb x > 0 folds it under the palm.
//   spine/neck/head  x > 0 bends forward/down, y > 0 turns to her left, z > 0 tilts the top to her right.

const ARM_BONE = /(UpperArm|LowerArm|Hand)$/;
export const eulerOrder = (bone) => (ARM_BONE.test(bone) ? 'YZX' : 'XYZ');

function mirrorAngles([x, y, z]) {
  return [x, -y, -z];
}

// Prefixes left-side part names with the side, mirroring the angles for the right.
export function limb(side, parts) {
  const out = {};
  for (const [part, angles] of Object.entries(parts)) out[side + part] = side === 'left' ? angles : mirrorAngles(angles);
  return out;
}

const FINGERS = ['Index', 'Middle', 'Ring', 'Little'];

// A hand shape. `curl` bends every finger joint (radians, more toward the middle joint); `spread` fans
// the fingers; `thumb` folds the thumb in.
export function hand(side, { curl, spread = 0, thumb = 0.3 }) {
  const parts = {};
  FINGERS.forEach((finger, i) => {
    // Outer fingers curl a little more than the index, like a real resting hand.
    const c = curl * (1 + i * 0.12);
    const fan = spread * (i - 1.3) * 0.5;
    parts[`${finger}Proximal`] = [0, fan, -c * 0.8];
    parts[`${finger}Intermediate`] = [0, 0, -c * 1.1];
    parts[`${finger}Distal`] = [0, 0, -c * 0.8];
  });
  // The thumb bends about its y (checked on the model): x and z only swing it out from the hand, which left a
  // "relaxed" thumb sticking out like a thumbs-up.
  parts.ThumbMetacarpal = [0, thumb * 0.55, 0];
  parts.ThumbProximal = [0, thumb * 0.85, 0];
  parts.ThumbDistal = [0, thumb * 0.85, 0];
  return limb(side, parts);
}

export const HAND = {
  relaxed: { curl: 0.32, spread: 0.05, thumb: 0.35 },
  open: { curl: 0.08, spread: 0.22, thumb: 0.05 },
  soft: { curl: 0.55, spread: 0.02, thumb: 0.45 },
  fist: { curl: 1.35, spread: 0, thumb: 1.0 },
  // Palm up under something, fingers a little cupped round it.
  cradle: { curl: 0.35, spread: 0.05, thumb: 0.2 },
};

export const merge = (...parts) => Object.assign({}, ...parts);

// Pointing: the index finger out (a hair short of locked, like a real finger), the other fingers curled
// into the palm and the thumb folded over them.
export function pointingHand(side) {
  return merge(
    hand(side, { curl: 1.25, spread: 0, thumb: 0.95 }),
    limb(side, { IndexProximal: [0, 0.06, -0.05], IndexIntermediate: [0, 0, -0.04], IndexDistal: [0, 0, -0.03] }),
  );
}

// ---- arms (left side; fitted against target wrist/elbow positions on the model's skeleton) ----
//
// The ones for props were solved on the real model with the prop in her hand (renderer/tuning.js, through
// tools/drive.mjs's `solve` message): where the prop's working part goes and which way it faces, then checked by eye.

export const ARM = {
  // Hanging at the sides, clear of the hips, elbows soft, palms toward the thighs.
  relaxed: { UpperArm: [0.53, 0.18, -1.28], LowerArm: [-0.52, -0.25, 0], Hand: [0, 0.1, -0.15] },
  // Talking: arms down at her sides like relaxed, a little out from her so her hands clear the skirt (any
  // halfway pose toward `engaged` sinks them into it).
  talk: { UpperArm: [0.53, 0.18, -1.16], LowerArm: [-0.52, -0.25, 0], Hand: [0, 0.1, -0.15] },
  // A hand loosely up under her chin, fingers curled by her lips: a giggle.
  giggle: { UpperArm: [-1.04, -1.53, -1.17], LowerArm: [-0.99, -2.38, 0], Hand: [0, -0.01, -0.14] },
  // Hands a little forward, held out in front (the spin's finger gun ends here).
  engaged: { UpperArm: [0.18, -0.08, -1.24], LowerArm: [-1.01, -1.05, 0], Hand: [0.01, -0.34, -0.26] },
  // Leaning in to listen: hands drift together in front of the body.
  attentive: { UpperArm: [0.56, -0.31, -1.25], LowerArm: [-0.71, -0.96, 0], Hand: [0, -0.21, -0.07] },
  // Hand under the chin, thinking.
  chin: { UpperArm: [-1.04, -1.73, -0.97], LowerArm: [-0.99, -2.38, 0], Hand: [0, -0.01, -0.14] },
  // Across the waist under the other elbow.
  across: { UpperArm: [0.07, -1.13, -1.27], LowerArm: [-1.6, -1.44, 0], Hand: [-0.18, 0.36, -0.24] },
  // Palm up, offered forward: "so, what do you think?"
  palmUp: { UpperArm: [-0.77, -0.61, -1.23], LowerArm: [-1.6, -1.15, 0], Hand: [-0.21, -0.09, -0.2] },
  // Raised for a wave: elbow out, forearm up, palm forward.
  wave: { UpperArm: [-1.39, -0.47, -0.52], LowerArm: [0.46, -2.14, 0], Hand: [0, 0.1, 0.12] },
  // Hand on the hip, elbow out and back.
  hip: { UpperArm: [1.41, 0.3, -0.8], LowerArm: [-0.77, -1.33, 0], Hand: [-0.06, -0.47, 0.56] },
  // Fist raised above the head, and pulled down to the chest for the pump.
  fistUp: { UpperArm: [-1.3, -0.58, 0.02], LowerArm: [-1.6, -1.74, 0], Hand: [-0.13, -0.27, 0.18] },
  fistDown: { UpperArm: [-0.32, -0.15, -1.12], LowerArm: [-0.45, -2.06, 0], Hand: [0, -0.07, 0.08] },
  // Both hands up in front of the face, holding an imaginary camera.
  camera: { UpperArm: [-1.0, -1.38, -0.78], LowerArm: [-1.0, -1.95, 0], Hand: [0, 0.15, 0.1] },
  // A real camera (props.js's built-in; solved for its grip) held level in front of her face just under
  // her eyes, lens toward the screen: this hand holds its end, fingers pointing at the viewer...
  cameraHold: { UpperArm: [-0.6, -0.78, -0.1], LowerArm: [-1.58, -2.15, 0], Hand: [-0.23, 0.7, 1.1] },
  // ...and the other palm cradles it from below, fingers toward the holding hand.
  cameraSupport: { UpperArm: [-1.31, -1.64, -0.92], LowerArm: [-1.8, -2.14, 0], Hand: [-0.35, 0.42, 0.99] },
  // A magnifying glass (props.js's built-in) held upright a hand's width in front of her eye on this side,
  // elbow down...
  magnifier: { UpperArm: [-1.25, -1.82, -0.72], LowerArm: [-0.7, -2.36, 0], Hand: [0.36, 0.71, 1.11] },
  // ...and the other arm folded across her waist, that hand cupping the raised elbow.
  underElbow: { UpperArm: [0.27, -0.85, -1.05], LowerArm: [-1.64, -1.6, 0], Hand: [-0.24, 0.57, -0.33] },
  // Forearms out, palms up: "who knows?"
  shrug: { UpperArm: [-0.58, -0.27, -1.25], LowerArm: [-1.6, -1.26, 0], Hand: [-0.25, -0.28, -0.05] },
  // A clipboard (props.js's built-in) held up in front of her chest by its bottom corner, facing out, so you
  // can read the list too.
  clipboard: { UpperArm: [-0.3, -0.9, -1.15], LowerArm: [-0.2, -1.8, 0], Hand: [0, 0, 0] },
  // A finger at the third box of the clipboard (held up in the other hand), ready to tick it.
  airTick: { UpperArm: [-0.77, -1.14, -1.5], LowerArm: [0.28, -1.8, 0], Hand: [-0.02, -0.51, 0.27] },
  // A stopwatch lying in her palm, held up by her shoulder with its face to you...
  stopwatch: { UpperArm: [-1.3, -0.89, -0.81], LowerArm: [0.71, -2.65, 0], Hand: [0.33, 0.14, -0.1] },
  // ...and up high beside her head, ringing: time's up!
  stopwatchUp: { UpperArm: [-1.5, -0.49, -0.1], LowerArm: [0.41, -2.25, 0], Hand: [-0.03, 0.19, -0.26] },
  // A hand bell held up at shoulder height in front of her, hanging below her fist, its handle upright.
  bell: { UpperArm: [-0.73, -0.11, -0.38], LowerArm: [-1.14, -2.21, 0], Hand: [-0.43, 0.55, -0.61] },
  // A globe (builtins.js) on her upturned palm, raised beside her...
  globe: { UpperArm: [-0.59, -0.42, -1.5], LowerArm: [-1.92, -2.26, 0], Hand: [-0.14, 0.55, 0.68] },
  // ...and the other hand's fingertip at its side, ready to flick it round.
  globeSpin: { UpperArm: [-0.43, -1.06, -0.63], LowerArm: [-1.3, -1.91, 0], Hand: [-0.02, 0.55, -0.5] },
  // A mug held by its handle in front of her chest...
  mug: { UpperArm: [0.4, -0.04, -0.95], LowerArm: [-0.6, -2.26, 0], Hand: [0.05, 0.28, 0.67] },
  // ...and up at her lips for a sip.
  mugSip: { UpperArm: [-0.68, -0.83, -0.3], LowerArm: [-0.58, -2.29, 0], Hand: [-0.29, 0.48, -0.21] },
  // Both hands up at the sides of her head, palms on her ears: putting headphones on.
  ears: { UpperArm: [-1.51, -0.44, 0.01], LowerArm: [-1.09, -2.73, 0], Hand: [-0.24, 0.55, 1.03] },
  // A hand up at her temple, sliding sunglasses on.
  temple: { UpperArm: [-1.0, -1.1, -0.8], LowerArm: [0.2, -2.5, 0], Hand: [0, 0.2, 0.2] },
  // A megaphone held to her mouth, its bell pointing out to her side (end on, it would hide her face).
  megaphone: { UpperArm: [-1.67, -2.25, -0.83], LowerArm: [0.36, -2.27, 0], Hand: [0.19, 0.55, 1.2] },
  // A parasol: the handle at her side, the shaft over her shoulder, the canopy open behind her head.
  umbrella: { UpperArm: [1.05, 0.75, -0.51], LowerArm: [-0.16, -2.34, 0], Hand: [-0.24, -0.55, 0.82] },
  // Arms crossed high over her chest: waiting, and not patiently.
  crossed: { UpperArm: [0.1, -1.2, -1.1], LowerArm: [-1.5, -2.0, 0], Hand: [0, 0.3, 0] },
  // A hand up by her ear, tucking her hair back.
  tuck: { UpperArm: [-1.2, -1.1, -0.75], LowerArm: [0.1, -2.6, 0], Hand: [0, 0.4, 0.3] },
  // A hand held up in front of her shoulder, palm toward her, fingers curled: checking her nails.
  nails: { UpperArm: [-0.93, -1.15, -0.98], LowerArm: [-1.1, -1.71, 0], Hand: [-0.34, -0.39, -0.42] },
  // A hand over her mouth for a yawn.
  yawn: { UpperArm: [-0.92, -1.46, -0.76], LowerArm: [-0.9, -2.23, 0], Hand: [0, 0, -0.2] },
  // Forearms folded on top of each other on a cloud in front of her chest, for a nap: this one on top...
  fold: { UpperArm: [-0.12, -1.34, -0.23], LowerArm: [0.01, -1.79, 0], Hand: [0.45, 0.55, 0.2] },
  // ...the other forearm just under it (solved as her right, leaning into the nap: gestures.js).
  foldUnder: { UpperArm: [-0.09, -1.34, -0.22], LowerArm: [-0.04, -1.68, 0], Hand: [0.45, 0.55, 0.31] },
  // A wrench held up beside her shoulder, flat on to you.
  wrench: { UpperArm: [-0.46, 0.13, -0.24], LowerArm: [-1.71, -2.5, 0], Hand: [-0.44, 0.55, 0.66] },
  // Elbows out, fists up by her head: "YES!", for the sonic rainboom (inside the waist-up window).
  burst: { UpperArm: [-1.3, -0.35, 0.05], LowerArm: [0.2, -1.7, 0], Hand: [0, 0, 0.2] },
};

// ---- whole-body poses per state ----

const LEGS = merge(
  limb('left', { UpperLeg: [0, 0, 0.02], LowerLeg: [0, 0, 0], Foot: [0, 0, -0.02] }),
  limb('right', { UpperLeg: [0, 0, 0.02], LowerLeg: [0, 0, 0], Foot: [0, 0, -0.02] }),
);

// Flying (away from home, pointing at something): knees drawn up a little, one more than the other, turned
// in so the lower legs kick out to the sides (from the front a plain knee bend hardly shows), toes pointed.
// Upper leg x < 0 swings the thigh forward and y < 0 turns the knee in; lower leg x > 0 bends the knee;
// foot x > 0 points the toes.
export const HOVER_LEGS = merge(
  limb('left', { UpperLeg: [-0.3, -0.42, 0.05], LowerLeg: [0.78, 0, 0], Foot: [0.5, 0, 0] }),
  limb('right', { UpperLeg: [-0.12, -0.26, 0.03], LowerLeg: [0.45, 0, 0], Foot: [0.42, 0, 0] }),
);

const ZERO = [0, 0, 0];
function body({ hips = ZERO, spine = ZERO, chest = ZERO, upperChest = ZERO, neck = ZERO, head = ZERO }) {
  return { hips, spine, chest, upperChest, neck, head };
}

function arms(left, right, leftHand, rightHand = leftHand) {
  const shoulder = { Shoulder: ZERO };
  return merge(
    limb('left', { ...shoulder, ...left }),
    limb('right', { ...shoulder, ...right }),
    hand('left', leftHand),
    hand('right', rightHand),
  );
}

export const STATE_POSES = {
  idle: merge(LEGS, body({ spine: [0.02, 0, 0], neck: [0.02, 0, 0] }), arms(ARM.relaxed, ARM.relaxed, HAND.relaxed)),
  listening: merge(
    LEGS,
    body({ spine: [0.06, 0, 0], chest: [0.04, 0, 0], neck: [0.03, 0, -0.04], head: [0.03, 0.06, -0.17] }),
    arms(ARM.attentive, ARM.attentive, HAND.soft),
  ),
  thinking: merge(
    LEGS,
    body({ spine: [0.03, 0.05, 0], chest: [0.02, 0, 0], neck: [0.02, 0, 0], head: [-0.06, -0.08, 0.1] }),
    arms(ARM.across, ARM.chin, HAND.soft),
  ),
  speaking: merge(LEGS, body({ spine: [0.01, 0, 0], chest: [-0.02, 0, 0], neck: [0.02, 0, 0] }), arms(ARM.talk, ARM.talk, HAND.relaxed)),
  confirming: merge(
    LEGS,
    body({ spine: [0.03, 0, 0], neck: [0.02, 0, 0], head: [0.02, 0.06, 0.16] }),
    arms(ARM.palmUp, ARM.relaxed, HAND.open, HAND.relaxed),
  ),
  stopped: merge(LEGS, body({ neck: [0.02, 0, 0] }), arms(ARM.relaxed, ARM.relaxed, HAND.relaxed)),
};
// Transcribing looks the same as thinking: she's working on what you said.
STATE_POSES.transcribing = STATE_POSES.thinking;

// Every bone a state pose sets; the animator drives exactly these.
export const POSED_BONES = Object.keys(STATE_POSES.idle);
