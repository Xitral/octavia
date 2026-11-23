'use strict';
// Command line and config file handling. See docs/avatar-protocol.md for the keys.

const fs = require('node:fs');
const path = require('node:path');

const DEFAULTS = {
  model: path.join(__dirname, '..', '..', 'models', 'avatar', 'octavia.vrm'),
  height: 560,
  framing: 'full',
  position: null,
  statePath: null,
  credit: 'my oc by amia',
  hideFromCapture: true,
  pointHeight: 0, // 0: the same as height
  trail: true,
  hover: false, // she floats (legs tucked, a little off her spot, still) while you talk to her
  napAfterS: 1800, // idle this long on screen (`always` mode), she dozes off on a cloud; 0 = never
  form: 'character', // or 'orb': a glowing orb drawn by the app (renderer/orb.js), no model needed
  orbSize: 320, // the orb's square window (px), at home and while pointing
  debug: false,
};
const MAX_NAP_AFTER_S = 24 * 3600;
const FORMS = ['character', 'orb'];
const clampOrb = (s) => Math.round(Math.min(600, Math.max(96, s)));

// Props she can hold (see docs/avatar-protocol.md). The built-ins are drawn from three.js shapes and always
// exist unless the config replaces them with a file of the same name.
const BUILTIN_PROPS = [
  'magnifier',
  'camera',
  'clipboard',
  'stopwatch',
  'bell',
  'globe',
  'folder',
  'mug',
  'wrench',
  'headphones',
  'sunglasses',
  'megaphone',
  'umbrella',
];
// The hand a built-in goes in by default: the right, but the clipboard, the folder and the globe go in her left, so
// her right can tick things off, spin the globe or rest on her hip (renderer/builtins.js says the same).
// Headphones and sunglasses she wears.
const BUILTIN_HANDS = { clipboard: 'left', folder: 'left', globe: 'left' };
const PROP_NAME = /^[a-z0-9_-]{1,64}$/;
const PROP_DEFAULTS = { hand: 'right', size: 0.18, grip: [0, 0, 0], rotation: [0, 0, 0], style: 'toon' };
// What each use holds: a screenshot, a close look, TickTick tasks, timers, reminders, the web, files, the morning
// rundown, a build of hers, music, a shout, and rainy or sunny weather.
const ACTIVITY_PROPS = {
  camera: 'camera',
  search: 'magnifier',
  tasks: 'clipboard',
  timer: 'stopwatch',
  reminder: 'bell',
  web: 'globe',
  files: 'folder',
  morning: 'mug',
  build: 'wrench',
  music: 'headphones',
  shout: 'megaphone',
  rain: 'umbrella',
  sun: 'sunglasses',
};
const MAX_GRIP_M = 1; // an offset past arm's length is a typo, not a grip
const MAX_PROP_SIZE_M = 2;
const MIN_PROP_SIZE_M = 0.02;

// Window width as a fraction of the height, wide enough for the widest gestures (a wave, a shrug, elbows
// out on the hips: about 0.45 m either side of her) at each framing's scale.
const ASPECT = { full: 0.55, upper: 1.0 };

function parseArgs(argv) {
  const args = { configPath: null, preview: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--preview') args.preview = true;
    else if (a === '--config' && i + 1 < argv.length) args.configPath = argv[++i];
    else if (a.startsWith('--config=')) args.configPath = a.slice('--config='.length);
  }
  return args;
}

function isPoint(p) {
  return p != null && Number.isFinite(p.x) && Number.isFinite(p.y);
}

const isObject = (v) => v != null && typeof v === 'object' && !Array.isArray(v);
const isVector = (v) => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite);
const clampHeight = (h) => Math.round(Math.min(2000, Math.max(160, h)));

function defaultProp(builtin) {
  const hand = Object.hasOwn(BUILTIN_HANDS, builtin ?? '') ? BUILTIN_HANDS[builtin] : PROP_DEFAULTS.hand;
  return { file: null, builtin, ...PROP_DEFAULTS, hand, grip: [...PROP_DEFAULTS.grip], rotation: [...PROP_DEFAULTS.rotation] };
}

// One prop's settings; a bad value falls back to its default with a warning. Null when there's nothing to hold.
function readProp(name, raw, warn) {
  if (!isObject(raw)) {
    warn(`props.${name}: not an object`);
    return null;
  }
  const prop = defaultProp(BUILTIN_PROPS.includes(raw.builtin) ? raw.builtin : null);
  if (typeof raw.file === 'string' && raw.file) {
    // The renderer gets the bytes of exactly this file (asking by the prop's name), so only take model files.
    if (path.isAbsolute(raw.file) && /\.(glb|gltf)$/i.test(raw.file)) prop.file = path.resolve(raw.file);
    else warn(`props.${name}: file must be an absolute path to a .glb or .gltf file`);
  }
  if (BUILTIN_PROPS.includes(raw.builtin)) prop.builtin = raw.builtin;
  else if (raw.builtin != null) warn(`props.${name}: there's no built-in prop called ${JSON.stringify(raw.builtin)}`);
  if (!prop.file && !prop.builtin) {
    warn(`props.${name}: no file and no built-in, so there's nothing to hold`);
    return null;
  }
  const option = (key, ok, value) => {
    if (raw[key] == null) return;
    if (ok) prop[key] = value;
    else warn(`props.${name}: bad ${key}, using the default`);
  };
  option('hand', raw.hand === 'right' || raw.hand === 'left', raw.hand);
  option('size', Number.isFinite(raw.size) && raw.size > 0, Math.min(MAX_PROP_SIZE_M, Math.max(MIN_PROP_SIZE_M, raw.size)));
  option('grip', isVector(raw.grip), isVector(raw.grip) && raw.grip.map((v) => Math.min(MAX_GRIP_M, Math.max(-MAX_GRIP_M, v))));
  option('rotation', isVector(raw.rotation), isVector(raw.rotation) && raw.rotation.map((v) => v % 360));
  option('style', raw.style === 'toon' || raw.style === 'original', raw.style);
  return prop;
}

// name -> prop. The built-ins go in first, so a file with the same name replaces one.
function readProps(raw, warn) {
  const props = {};
  for (const name of BUILTIN_PROPS) props[name] = defaultProp(name);
  if (raw == null) return props;
  if (!isObject(raw)) {
    warn('props: not an object');
    return props;
  }
  for (const [name, def] of Object.entries(raw)) {
    // `__proto__` fits the pattern, and assigning it would change the object's prototype instead.
    if (!PROP_NAME.test(name) || name === '__proto__') {
      warn(`props: ${JSON.stringify(name).slice(0, 80)} is not a prop name (a-z, 0-9, _ and -)`);
      continue;
    }
    const prop = readProp(name, def, warn);
    if (prop) props[name] = prop;
  }
  return props;
}

// Which prop goes with each activity: a prop's name, or null for none.
function readActivityProps(raw, props, warn) {
  const out = {};
  for (const [activity, fallback] of Object.entries(ACTIVITY_PROPS)) out[activity] = Object.hasOwn(props, fallback) ? fallback : null;
  if (raw == null) return out;
  if (!isObject(raw)) {
    warn('activityProps: not an object');
    return out;
  }
  for (const activity of Object.keys(out)) {
    if (!Object.hasOwn(raw, activity)) continue;
    const name = raw[activity];
    if (name === null || name === '') out[activity] = null;
    else if (typeof name === 'string' && Object.hasOwn(props, name)) out[activity] = name;
    else {
      // A prop the user didn't pick would be a surprise; holding nothing isn't.
      warn(`activityProps.${activity}: no prop called ${JSON.stringify(name)}`);
      out[activity] = null;
    }
  }
  return out;
}

// Reads the config file (if any) and fills in defaults. Bad values fall back to the default rather than
// failing: a typo in the config should not keep the avatar from showing up. What was wrong is listed in
// `cfg.warnings`, for the log.
function loadConfig(args) {
  let raw = {};
  if (args.configPath) raw = JSON.parse(fs.readFileSync(args.configPath, 'utf8'));
  if (!isObject(raw)) throw new Error('the config file must hold a JSON object');
  const cfg = { ...DEFAULTS, warnings: [] };
  const warn = (text) => cfg.warnings.push(text);
  if (typeof raw.model === 'string' && raw.model) cfg.model = raw.model;
  if (Number.isFinite(raw.height)) cfg.height = clampHeight(raw.height);
  if (raw.framing === 'full' || raw.framing === 'upper') cfg.framing = raw.framing;
  if (isPoint(raw.position)) cfg.position = { x: Math.round(raw.position.x), y: Math.round(raw.position.y) };
  if (typeof raw.statePath === 'string' && raw.statePath) cfg.statePath = raw.statePath;
  if (typeof raw.credit === 'string') cfg.credit = raw.credit;
  if (typeof raw.hideFromCapture === 'boolean') cfg.hideFromCapture = raw.hideFromCapture;
  // Her full-body height while she's out pointing at something (at home she may be framed waist-up).
  cfg.pointHeight = Number.isFinite(raw.pointHeight) && raw.pointHeight > 0 ? clampHeight(raw.pointHeight) : cfg.height;
  if (typeof raw.trail === 'boolean') cfg.trail = raw.trail;
  if (typeof raw.hover === 'boolean') cfg.hover = raw.hover;
  if (Number.isFinite(raw.napAfterS) && raw.napAfterS >= 0) cfg.napAfterS = Math.min(MAX_NAP_AFTER_S, raw.napAfterS);
  cfg.props = readProps(raw.props, warn);
  cfg.activityProps = readActivityProps(raw.activityProps, cfg.props, warn);
  cfg.previewProp = null;
  if (typeof raw.previewProp === 'string' && raw.previewProp) {
    if (Object.hasOwn(cfg.props, raw.previewProp)) cfg.previewProp = raw.previewProp;
    else warn(`previewProp: no prop called ${JSON.stringify(raw.previewProp)}`);
  }
  cfg.debug = raw.debug === true;
  cfg.width = Math.round(cfg.height * ASPECT[cfg.framing]);
  if (FORMS.includes(raw.form)) cfg.form = raw.form;
  else if (raw.form != null) warn(`form: ${JSON.stringify(raw.form)} is not one of ${FORMS.join(', ')}`);
  if (Number.isFinite(raw.orbSize)) cfg.orbSize = clampOrb(raw.orbSize);
  // The orb's window is a square of its own size, and stays that size when it flies out to point.
  if (cfg.form === 'orb') cfg.height = cfg.width = cfg.pointHeight = cfg.orbSize;
  return cfg;
}

// The props as the renderer sees them: the same settings, but only a file's name, never where it lives
// (it asks for a prop's bytes by the prop's name).
function propsForRenderer(props) {
  const out = {};
  for (const [name, prop] of Object.entries(props)) out[name] = { ...prop, file: prop.file ? path.basename(prop.file) : null };
  return out;
}

function readSavedPosition(statePath) {
  if (!statePath) return null;
  try {
    const saved = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    return isPoint(saved) ? { x: Math.round(saved.x), y: Math.round(saved.y) } : null;
  } catch {
    return null;
  }
}

// Write to a temp file and rename, so a crash mid-write never leaves Octavia a half-written file.
function savePosition(statePath, pos) {
  if (!statePath) return;
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const tmp = `${statePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify({ x: pos.x, y: pos.y }));
  fs.renameSync(tmp, statePath);
}

module.exports = { parseArgs, loadConfig, propsForRenderer, readSavedPosition, savePosition };
