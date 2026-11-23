// Drives the avatar the way Octavia does (JSON lines over stdin/stdout) and captures PNGs, for checking
// poses, expressions and gestures by eye.
//
//   node tools/drive.mjs <scenario> [--height 900] [--framing full|upper] [--point-height 560] [--out <dir>]
//                                   [--props <props.json>]
//
// Scenarios: states, transitions, expressions, mouth, gestures, robustness, presence, snapshot, all (every
// one of those), preview (runs the app's own --preview demo and captures it every 1.5 s), pointing (aim
// checks at targets all around her, inside one fixed window), point-flow (real points in desktop pixels:
// she flies around the primary monitor), props (the camera in a snapshot, the magnifier in the `search`
// activity, a snapshot in the middle of one, pointing ending one, one while pointing, and hiding during
// one), hold (she holds the config's previewProp through states, gestures, a close look, a point and a
// snapshot) and credit (the credit label with the pointer resting on her at home, then a flight and back;
// the pointer is a debug stand-in, so the real mouse stays put), moments (reading the clipboard and ticking a
// task off, the stopwatch clicked on and ringing, the bell, the sonic rainboom, floating while listening, the
// globe, the folder, the mug and its sip, the wrench, headphones, sunglasses, the umbrella and the megaphone), nap
// (dozing off after a short idle, waking, then caught napping after a short break: the nap time is set to 3 s
// for it) and life (sleepy at night and perky in the morning, a yawn, the fidgets, the spin, impatience, a mood
// carried over, peeking in, and a dust puff as she zips off and back), clips (every frame of the gestures named by
// --gestures, stepped exactly 1/--fps s apart, for judging motion: ffmpeg turns them into video). PNGs go to --out (default:
// %TEMP%/octavia-avatar-shots/<scenario>), and orb (the orb form, frame by frame like clips, through each of its looks
// and a point, into <out>/f0000.png and on; --orb-size sets its window, default 400), orb-live (the orb running in
// real time, idle, thinking with a tool, then speaking, --seconds each (default 15), with its frame gaps and main-thread
// time per frame from the renderer and the CPU the app's processes used, per phase); their paths are printed as they are written. The pointing
// scenarios also print how far each target is from the drawn shoulder-fingertip and finger lines.
//
// Poses for props can be solved rather than guessed: a debug message {type: 'solve', prop, start, goals, measure,
// fixed} (renderer/tuning.js) fits the arm holding `prop` so its parts land where the goals say and logs the angles
// as "solved: {...}" on stderr, ready for poses.js; {type: 'pose', pose, prop} then holds them for a capture.
//
// --props names a JSON file whose `props`, `activityProps` and `previewProp` go into the app's config as
// they are (docs/avatar-protocol.md), e.g. to try a .glb prop: {"props": {"cup": {"file": "C:/.../cup.glb",
// "hand": "left", "size": 0.12}}, "previewProp": "cup"}.

import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ELECTRON = require('electron'); // the path of the Electron binary

function option(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

const scenario = process.argv[2] || 'all';
const outDir = path.resolve(option('out', path.join(os.tmpdir(), 'octavia-avatar-shots', scenario)));
fs.mkdirSync(outDir, { recursive: true });

// The pointing checks need room around her: a big square window (waist-up framing is square), or a home
// window like Octavia's at the default spot, bottom right of the primary monitor.
// Props in her hands are small: the props scenarios frame her waist-up, big.
const WINDOW = {
  pointing: { height: 1000, framing: 'upper', position: { x: 40, y: 20 } },
  'point-flow': { height: 380, framing: 'upper', position: null },
  props: { height: 800, framing: 'upper', position: { x: 40, y: 20 } },
  hold: { height: 800, framing: 'upper', position: { x: 40, y: 20 } },
  moments: { height: 800, framing: 'upper', position: { x: 40, y: 20 } },
  life: { height: 800, framing: 'upper', position: { x: 40, y: 20 } },
  nap: { height: 800, framing: 'upper', position: { x: 40, y: 20 } },
  credit: { height: 560, framing: 'upper', position: { x: 40, y: 20 } },
  clips: { height: 600, framing: 'upper', position: { x: 40, y: 20 } },
  orb: { height: 600, framing: 'upper', position: { x: 40, y: 20 } },
  'orb-live': { height: 600, framing: 'upper', position: { x: 40, y: 20 } },
};
const propsFile = option('props', null);
const propsConfig = propsFile ? JSON.parse(fs.readFileSync(propsFile, 'utf8')) : {};
if (!propsConfig || typeof propsConfig !== 'object' || Array.isArray(propsConfig)) throw new Error(`${propsFile} must hold a JSON object`);
const config = {
  model: path.join(APP_DIR, '..', 'models', 'avatar', 'octavia.vrm'),
  height: Number(option('height', WINDOW[scenario]?.height ?? 900)),
  framing: option('framing', WINDOW[scenario]?.framing ?? 'full'),
  position: WINDOW[scenario] ? WINDOW[scenario].position : { x: 40, y: 40 },
  pointHeight: Number(option('point-height', 560)),
  statePath: path.join(outDir, 'state.json'),
  credit: 'my oc by amia',
  debug: true,
  ...(scenario === 'nap' ? { napAfterS: 3 } : {}),
  ...(scenario === 'orb' ? { form: 'orb', orbSize: Number(option('orb-size', 400)) } : {}),
  ...(scenario === 'orb-live' ? { form: 'orb', orbSize: Number(option('orb-size', 260)) } : {}),
};
for (const key of ['props', 'activityProps', 'previewProp']) if (key in propsConfig) config[key] = propsConfig[key];
if (scenario === 'hold' && !config.previewProp) throw new Error('hold needs a previewProp: pass --props with one');
const configPath = path.join(outDir, 'config.json');
fs.writeFileSync(configPath, JSON.stringify(config, null, 2));

const args = [APP_DIR, '--config', configPath, ...(scenario === 'preview' ? ['--preview'] : [])];
const child = spawn(ELECTRON, args, { stdio: ['pipe', 'pipe', 'pipe'] });
const waiters = [];
// From the renderer's debug log: its measurements of the model, and how well each point lines up.
let metrics = null;
const pointLogs = [];
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    console.log(`stdout (not JSON): ${line}`);
    return;
  }
  console.log(`<- ${line}`);
  for (const w of waiters.splice(0)) {
    if (w.match(msg)) w.resolve(msg);
    else waiters.push(w);
  }
});
const orbStats = []; // the orb-live scenario's per-phase frame numbers
const ticks = []; // waiters for the renderer's "ticked <id>" (the clips scenario)
readline.createInterface({ input: child.stderr }).on('line', (line) => {
  const ticked = / ticked (\S+)/.exec(line);
  const stats = / orb stats (\{.*\})/.exec(line);
  if (stats) {
    orbStats.push(JSON.parse(stats[1]));
    return;
  }
  if (ticked) {
    const i = ticks.findIndex((w) => w.id === ticked[1]);
    if (i >= 0) ticks.splice(i, 1)[0].resolve();
    return;
  }
  console.log(`stderr: ${line}`);
  const measured = /renderer \w+: metrics: (\{.*?\}) \(/.exec(line);
  if (measured) metrics = JSON.parse(measured[1]);
  const at = line.indexOf('pointing: ');
  if (at >= 0) pointLogs.push(line.slice(at).replace(/ \([^)]*:\d+\)$/, ''));
});
child.on('exit', (code) => console.log(`avatar exited with ${code}`));

const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function waitFor(match, ms = 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for ${match}`)), ms);
    waiters.push({ match, resolve: (m) => (clearTimeout(timer), resolve(m)) });
  });
}
async function capture(name) {
  const file = path.join(outDir, `${name}.png`);
  const done = waitFor((m) => (m.type === 'captured' && m.path === file) || m.type === 'error');
  send({ type: 'capture', path: file });
  const msg = await done;
  if (msg.type === 'error') throw new Error(msg.message);
  console.log(`captured ${file}`);
}

// Talks for `ms` (fake lip sync at 50 Hz), capturing at the given times.
async function talk(ms, shots = {}) {
  const pending = [];
  for (let t = 0; t < ms; t += 20) {
    const level = 0.35 + 0.35 * Math.abs(Math.sin(t / 90));
    send({ type: 'mouth', level, v: [level * 0.8, 0, level * 0.2, 0, 0] });
    for (const [name, at] of Object.entries(shots)) if (at >= t && at < t + 20) pending.push(capture(name));
    await sleep(20);
  }
  await Promise.all(pending);
}

// The accuracy log of the last point, if one came in since `since` log lines.
const lastPointLog = (since) => pointLogs.slice(since).pop() || 'no measurement logged';
const results = [];
function report() {
  if (!results.length) return;
  console.log('\n---- pointing accuracy ----');
  for (const line of results) console.log(line);
  fs.writeFileSync(path.join(outDir, 'pointing.txt'), `${results.join('\n')}\n`);
}

// Captures `<prefix>-<ms>` at each of `times` (ms from now, ascending).
async function captureAt(prefix, times) {
  let at = 0;
  for (const t of times) {
    await sleep(t - at);
    at = t;
    await capture(`${prefix}-${t}`);
  }
}

// Holds one vowel for `ms` with mouth messages at 50 Hz, capturing just before the end.
async function holdVowel(index, ms, name) {
  const v = [0, 0, 0, 0, 0];
  v[index] = 0.9;
  let capturing = null;
  for (let t = 0; t < ms; t += 20) {
    send({ type: 'mouth', level: 0.8, v });
    if (!capturing && t >= ms - 200) capturing = capture(name);
    await sleep(20);
  }
  await capturing;
}

const SCENARIOS = {
  async states() {
    for (const state of ['idle', 'listening', 'thinking', 'speaking', 'confirming', 'stopped']) {
      send({ type: 'state', state });
      await sleep(1800);
      await capture(`state-${state}`);
    }
  },
  async expressions() {
    send({ type: 'state', state: 'idle' });
    for (const emotion of ['neutral', 'happy', 'sad', 'angry', 'surprised', 'relaxed', 'excited', 'smug', 'laughing', 'curious', 'playful', 'thinking']) {
      send({ type: 'emotion', emotion, intensity: 1, hold: 10 });
      await sleep(1300);
      await capture(`emotion-${emotion}`);
    }
  },
  async mouth() {
    send({ type: 'state', state: 'speaking' });
    await sleep(1000);
    const names = ['aa', 'ih', 'ou', 'ee', 'oh'];
    for (let i = 0; i < names.length; i++) await holdVowel(i, 700, `mouth-${names[i]}`);
    send({ type: 'segment', text: 'This is great news, really!', emotion: 'happy', intensity: 0.9 });
    await holdVowel(0, 700, 'mouth-aa-while-happy');
    await sleep(400);
    await capture('mouth-closed-after-silence');
  },
  async gestures() {
    send({ type: 'state', state: 'idle' });
    await sleep(800);
    const plan = {
      wave: [700, 1000],
      nod: [250],
      shake: [300],
      shrug: [600],
      laugh: [500],
      fist_pump: [330, 580],
      hands_on_hips: [1200],
      surprised_jolt: [150],
      sigh: [1400],
      think_chin: [1300],
    };
    for (const [name, times] of Object.entries(plan)) {
      send({ type: 'gesture', name });
      let at = 0;
      for (const t of times) {
        await sleep(t - at);
        at = t;
        await capture(`gesture-${name}-${t}`);
      }
      send({ type: 'segment', text: '', emotion: 'neutral' }); // ends a sustained gesture
      send({ type: 'state', state: 'idle' });
      await sleep(2600 - at);
    }
  },
  // Bursts of frames across state changes, to catch limbs passing through the body mid-transition.
  async transitions() {
    for (const [from, to] of [
      ['idle', 'listening'],
      ['listening', 'thinking'],
      ['thinking', 'speaking'],
      ['speaking', 'confirming'],
      ['confirming', 'idle'],
    ]) {
      send({ type: 'state', state: from });
      await sleep(1500);
      send({ type: 'state', state: to });
      for (let i = 0; i < 6; i++) {
        await capture(`transition-${from}-${to}-${i}`);
        await sleep(60);
      }
    }
  },
  // Junk and half-formed messages must be ignored (or answered with an error), never crash the app.
  async robustness() {
    const junk = [
      'not json',
      '[]',
      '{}',
      '{"type":42}',
      '{"type":"__proto__"}',
      '{"type":"toString"}',
      '{"type":"state","state":7}',
      '{"type":"state","state":"dancing"}',
      '{"type":"segment"}',
      '{"type":"segment","text":null,"emotion":{},"intensity":"x","gesture":"moonwalk"}',
      '{"type":"mouth","v":"x"}',
      '{"type":"mouth","level":"a","v":[null,"x",5,-1,1e9]}',
      '{"type":"emotion","emotion":"ecstatic","intensity":7,"hold":-5}',
      '{"type":"gesture","name":"constructor"}',
      '{"type":"gesture"}',
      '{"type":"show","reason":null}',
      '{"type":"capture","path":"relative.png"}',
      '{"type":"capture"}',
    ];
    for (const line of junk) child.stdin.write(`${line}\n`);
    await sleep(1500);
    await capture('robustness-still-alive');
  },
  async presence() {
    await capture('presence-shown');
    send({ type: 'hide' });
    await sleep(1100);
    await capture('presence-leaving');
    await waitFor((m) => m.type === 'hidden', 5000);
    send({ type: 'show', reason: 'talk' });
    await sleep(180);
    await capture('presence-rising');
    await sleep(900);
    await capture('presence-back');
  },
  // Octavia takes a screenshot: the camera comes up, the shutter flashes.
  async snapshot() {
    send({ type: 'state', state: 'thinking' });
    await sleep(1200);
    send({ type: 'snapshot' });
    await sleep(330);
    await capture('snapshot-shutter');
    await sleep(1800);
    await capture('snapshot-after');
  },
  // Targets all around her, both arms, inside one fixed square window: layouts and renderer-level pointing
  // messages go straight to the renderer (wrapped as debug messages, which the main process passes
  // through), so every target sits exactly where the test puts it relative to her shoulder, as the main
  // process's layout would.
  async pointing() {
    const debug = (message) => send({ type: 'debug', message });
    const H = config.pointHeight;
    const W = Math.round(H * 0.55);
    const size = Math.min(config.height, 1000);
    const m = metrics || { shoulderY: 0.2, shoulderX: 0.07, reach: 0.4 };
    const nominal = 0.85 * m.reach * H + 0.05 * H; // the main process's shoulder-to-spot distance
    const targets = [
      // name, horizontal distance from the shoulder, vertical offset (down), optional box size
      ['level', nominal, 0.06 * H],
      ['above', 0.8 * nominal, -0.3 * H],
      ['below', 0.85 * nominal, 0.35 * H],
      ['far-below', nominal, 0.6 * H],
      ['taskbar', 0.9 * nominal, 0.85 * H], // her body kept above the taskbar, the spot on it
      ['far', 1.6 * nominal, 0.07 * H],
      ['near', 0.55 * nominal, 0.05 * H],
      ['box', nominal + 40, 0.04 * H, { w: 120, h: 70 }],
    ];
    for (const arm of ['right', 'left']) {
      const dir = arm === 'right' ? -1 : 1; // her right arm points screen-left
      const body = { x: arm === 'right' ? size - W - 30 : 30, y: Math.round(size * 0.18), w: W, h: H };
      const shoulder = { x: body.x + W / 2 + dir * m.shoulderX * H, y: body.y + m.shoulderY * H };
      debug({ type: 'zip-out', to: { x: size / 2, y: -400 } });
      await sleep(120);
      await capture(`point-${arm}-0-zip-out`);
      await sleep(400);
      debug({ type: 'layout', mode: 'point', framing: 'full', width: size, height: size, body, arm });
      for (const [i, [name, dx, dy, box]] of targets.entries()) {
        const target = { x: Math.round(shoulder.x + dir * dx), y: Math.round(shoulder.y + dy) };
        const around = box ? { x: target.x - box.w / 2, y: target.y - box.h / 2, w: box.w, h: box.h } : null;
        const since = pointLogs.length;
        // The first one flies in from above the window, like coming from home.
        debug({ type: 'point', target, box: around, arm, from: i === 0 ? { x: size / 2, y: -400 } : null });
        if (i === 0) {
          await sleep(150);
          await capture(`point-${arm}-1-zip-in`);
          await sleep(1500);
        } else {
          await sleep(1700);
        }
        await capture(`point-${arm}-${name}`);
        results.push(`${arm} ${name.padEnd(9)} ${lastPointLog(since)}`);
      }
    }
    // Pointing while she talks: the free hand stays on her hip, she glances at the viewer now and then.
    send({ type: 'state', state: 'speaking' });
    send({ type: 'segment', text: 'Right there, see it?', emotion: 'excited', intensity: 0.8 });
    const since = pointLogs.length;
    await talk(3200, { 'point-talking-1': 900, 'point-talking-2': 2400 });
    results.push(`left talking   ${lastPointLog(since)}`);
    // A screenshot while pointing: the arm comes down for the camera, then goes back.
    send({ type: 'snapshot' });
    await sleep(450);
    await capture('point-snapshot-camera');
    await sleep(1900);
    await capture('point-snapshot-after');
    send({ type: 'state', state: 'idle' });
    debug({ type: 'unpoint' });
    await sleep(700);
    await capture('point-unpointed');
    // Hide while pointing without the main process's zip: the old wave and sink still work.
    debug({ type: 'point', target: { x: 700, y: 300 }, box: null, arm: 'left' });
    await sleep(1200);
    send({ type: 'hide' });
    await sleep(500);
    await capture('point-hide-waving');
    await waitFor((msg) => msg.type === 'hidden', 5000);
    // Home again: back to the whole window, and the usual rise.
    debug({ type: 'layout', mode: 'home', framing: config.framing, width: size, height: size, body: null });
    send({ type: 'show', reason: 'talk' });
    await sleep(200);
    await capture('point-home-rising');
    await sleep(1000);
    await capture('point-home-back');
  },
  // The real thing through the main process: points in physical desktop pixels on the primary monitor,
  // so the window flies from home to each spot. Captures only show her own window.
  async 'point-flow'() {
    send({ type: 'state', state: 'speaking' });
    const spots = [
      ['centre', 960, 540],
      ['top-left', 260, 180],
      ['top-right', 1700, 140],
      ['bottom-left', 240, 990],
      ['taskbar', 1100, 1062],
    ];
    for (const [name, x, y] of spots) {
      const since = pointLogs.length;
      send({ type: 'point', x, y, box: null, label: name });
      await sleep(300);
      await capture(`flow-${name}-arriving`);
      await sleep(1500);
      await capture(`flow-${name}`);
      results.push(`flow ${name.padEnd(12)} ${lastPointLog(since)}`);
    }
    const since = pointLogs.length;
    send({ type: 'point', x: 700, y: 420, box: [640, 395, 760, 445], label: 'the button' });
    await sleep(1800);
    await capture('flow-box');
    results.push(`flow ${'box'.padEnd(12)} ${lastPointLog(since)}`);
    send({ type: 'unpoint' });
    await sleep(1400);
    await capture('flow-home');
    send({ type: 'point', x: 500, y: 500, box: null, label: 'one more' });
    await sleep(1600);
    send({ type: 'hide' });
    await waitFor((msg) => msg.type === 'hidden', 5000);
    send({ type: 'show', reason: 'talk' });
    await sleep(1200);
    await capture('flow-shown-again');
  },
  // The props, the way Octavia uses them: the camera in a snapshot, the magnifier in the search activity
  // (and the camera taking over in the middle of one), then a point ending one (a real point: she flies),
  // one while she points (the arm makes way), and a hide in the middle of one.
  async props() {
    send({ type: 'state', state: 'thinking' });
    await sleep(1200);
    send({ type: 'snapshot' });
    await captureAt('props-1-snapshot', [150, 330, 650, 1000, 1700]);
    send({ type: 'activity', name: 'search' });
    await captureAt('props-2-search', [250, 800, 2500, 4500]);
    send({ type: 'snapshot' });
    await captureAt('props-3-snapshot-mid-search', [400, 900, 1600, 2600]);
    send({ type: 'activity', name: null });
    await captureAt('props-4-search-over', [300, 1200]);
    send({ type: 'activity', name: 'search' });
    await sleep(1500);
    send({ type: 'point', x: 1250, y: 520, box: null, label: 'the apple' });
    await captureAt('props-5-point', [120, 600, 1500]);
    send({ type: 'activity', name: 'search' });
    await captureAt('props-6-search-while-pointing', [700, 2000]);
    send({ type: 'activity', name: null });
    await captureAt('props-7-pointing-again', [1200]);
    send({ type: 'unpoint' });
    await sleep(1600);
    send({ type: 'activity', name: 'search' });
    await sleep(1200);
    send({ type: 'hide' });
    await captureAt('props-8-hide', [300, 700]);
    await waitFor((m) => m.type === 'hidden', 5000);
    send({ type: 'show', reason: 'talk' });
    await sleep(1200);
    await capture('props-9-back-empty-handed');
  },
  // She holds the config's previewProp (see --props) through states, talking, gestures, a close look (it
  // stands in for the magnifier, up at her eye), a snapshot (the camera takes its place for a moment) and a
  // point, for tuning its grip and rotation by eye.
  async hold() {
    send({ type: 'state', state: 'idle' });
    await sleep(800);
    await capture('hold-idle');
    for (const state of ['listening', 'thinking']) {
      send({ type: 'state', state });
      await sleep(1500);
      await capture(`hold-${state}`);
    }
    send({ type: 'state', state: 'speaking' });
    send({ type: 'segment', text: 'Look what I found!', emotion: 'happy', intensity: 0.8 });
    await talk(2600, { 'hold-talking-1': 900, 'hold-talking-2': 2200 });
    for (const [name, at] of [
      ['wave', 900],
      ['fist_pump', 330],
      ['shrug', 600],
      ['think_chin', 1300],
    ]) {
      send({ type: 'gesture', name });
      await sleep(at);
      await capture(`hold-gesture-${name}`);
      await sleep(2600 - at);
    }
    send({ type: 'state', state: 'thinking' });
    send({ type: 'activity', name: 'search' });
    await captureAt('hold-search', [800, 2500]);
    send({ type: 'activity', name: null });
    await sleep(800);
    send({ type: 'snapshot' });
    await captureAt('hold-snapshot', [650, 2200]);
    send({ type: 'point', x: 1250, y: 520, box: null, label: 'there' });
    await sleep(1800);
    await capture('hold-pointing');
    send({ type: 'unpoint' });
    await sleep(1600);
    await capture('hold-home');
  },
  // Her props for your tasks, timers and reminders, as Octavia plays them: reading the clipboard while TickTick
  // works, a task ticked off on it (reading goes on after), the stopwatch clicked on for a timer and held up
  // ringing when one goes off, the bell before a reminder, the sonic rainboom, and floating while she listens.
  async moments() {
    send({ type: 'state', state: 'thinking' });
    await sleep(800);
    send({ type: 'activity', name: 'tasks' });
    await captureAt('moments-1-tasks', [900, 3000]);
    send({ type: 'moment', name: 'tick' });
    await captureAt('moments-2-tick', [300, 480, 650, 900, 1300, 2000]);
    send({ type: 'activity', name: null });
    await sleep(1200);
    send({ type: 'moment', name: 'timer_start' });
    await captureAt('moments-3-timer-start', [350, 500, 1000]);
    await sleep(1200);
    send({ type: 'moment', name: 'timer_done' });
    await captureAt('moments-4-timer-done', [450, 700, 1100]);
    await sleep(1200);
    send({ type: 'moment', name: 'bell' });
    await captureAt('moments-5-bell', [350, 480, 620, 900]);
    await sleep(1200);
    send({ type: 'moment', name: 'rainboom' });
    await captureAt('moments-6-rainboom', [150, 330, 450, 700, 1200]);
    await sleep(1200);
    send({ type: 'state', state: 'listening' });
    await captureAt('moments-7-hovering', [1200, 2000]);
    send({ type: 'state', state: 'idle' });
    await captureAt('moments-8-landed', [1500]);
    send({ type: 'state', state: 'thinking' });
    for (const [activity, times] of [
      ['web', [1000, 2600]],
      ['files', [1000, 2400]],
      ['morning', [1200, 4200]],
    ]) {
      send({ type: 'activity', name: activity });
      await captureAt(`moments-9-${activity}`, times);
      send({ type: 'activity', name: null });
      await sleep(1200);
    }
    for (const [moment, times] of [
      ['wrench', [300, 550, 1500]],
      ['music', [300, 1000, 3700]],
      ['sunglasses', [350, 1500]],
      ['umbrella', [300, 700]],
      ['megaphone', [800]],
    ]) {
      send({ type: 'moment', name: moment });
      await captureAt(`moments-10-${moment}`, times);
      await sleep(1500);
    }
  },
  // Life at home: sleepy (heavy eyelids) and a yawn, perky, the fidgets, the show-off spin, impatience, a mood
  // carried over, a peek in from the side, and a puff of dust as she zips off and back.
  async life() {
    const debug = (message) => send({ type: 'debug', message });
    send({ type: 'state', state: 'idle' });
    debug({ type: 'energy', sleepy: 1, perky: 0 });
    await captureAt('life-1-sleepy', [1500]);
    debug({ type: 'play', name: 'yawn' });
    await captureAt('life-2-yawn', [900]);
    await sleep(1500);
    debug({ type: 'energy', sleepy: 0, perky: 1 });
    await captureAt('life-3-perky', [1500]);
    debug({ type: 'energy', sleepy: 0, perky: 0 });
    for (const name of ['hair_tuck', 'check_nails', 'shoulder_roll']) {
      debug({ type: 'play', name });
      await captureAt(`life-4-${name}`, [900]);
      await sleep(1700);
    }
    send({ type: 'gesture', name: 'spin' });
    await captureAt('life-5-spin', [300, 500, 700, 1100]);
    await sleep(800);
    send({ type: 'gesture', name: 'impatient' });
    await captureAt('life-6-impatient', [1200]);
    await sleep(2500);
    send({ type: 'mood', emotion: 'smug', intensity: 0.6, seconds: 20 });
    await captureAt('life-7-mood', [1500]);
    send({ type: 'hide' });
    await waitFor((m) => m.type === 'hidden', 5000);
    debug({ type: 'peek', side: 1 });
    await captureAt('life-8-peek', [150, 500, 900, 1500]);
    debug({ type: 'zip-out', to: { x: 2000, y: 300 } });
    await captureAt('life-9-puff-out', [180]);
    await sleep(600);
    send({ type: 'show', reason: 'return', from: { x: 2000, y: 300 } });
    await captureAt('life-9-puff-in', [520]);
  },
  // The nap (napAfterS is 3 s here): idle a few seconds and she dozes off on her cloud; listening wakes her. Then
  // away for longer than that, she comes back caught napping and wakes up by herself.
  async nap() {
    send({ type: 'state', state: 'idle' });
    await captureAt('nap-1-idle', [2000, 3600, 5500]);
    send({ type: 'state', state: 'listening' });
    await captureAt('nap-2-woken', [120, 800]);
    send({ type: 'state', state: 'idle' });
    send({ type: 'hide' });
    await waitFor((m) => m.type === 'hidden', 5000);
    await sleep(3500);
    send({ type: 'show', reason: 'wake' });
    await captureAt('nap-3-caught-napping', [300, 700, 1100, 1800]);
  },
  // The credit: a label by the pointer once it has rested on her for a moment at home; none while she flies
  // out to point, none in the moment she's back before the pointer has rested again, and gone at once when
  // the pointer lets go or she leaves.
  async credit() {
    const hover = (p) => send({ type: 'debug', message: { type: 'hover', ...p } });
    const upper = config.framing === 'upper'; // waist-up the window is square, full body 0.55 as wide
    const chest = { x: Math.round((config.height * (upper ? 1 : 0.55)) / 2), y: Math.round(config.height * (upper ? 0.45 : 0.3)) };
    send({ type: 'state', state: 'idle' });
    await sleep(500);
    hover(chest);
    await captureAt('credit-1-resting', [300, 1000]); // not yet, then there
    send({ type: 'point', x: 1250, y: 520, box: null, label: 'the apple' });
    await captureAt('credit-2-flight', [120, 1600]); // zipping out, then pointing
    send({ type: 'unpoint' });
    await captureAt('credit-3-home', [700, 1800]); // just back, then rested again
    hover(null);
    await captureAt('credit-4-let-go', [150]);
    hover(chest);
    await sleep(1000);
    send({ type: 'hide' });
    await captureAt('credit-5-leaving', [80]);
    await waitFor((m) => m.type === 'hidden', 5000);
    hover(null);
  },
  // Frame-by-frame clips of gestures, for judging motion rather than poses: the clock is held and every frame is
  // stepped exactly 1/--fps s on, so the clip plays at true speed however slow capturing is. --gestures names them
  // (comma-separated; default the laugh); each one's frames go to <out>/<gesture>/f0000.png and on. The public
  // gestures come with a sentence and its face, as when she speaks (the laugh with a laughing face and a laugh's
  // mouth); the rest are played as they are.
  async clips() {
    const debug = (message) => send({ type: 'debug', message });
    const fps = Number(option('fps', 30));
    const names = option('gestures', 'laugh').split(',');
    let id = 0;
    const tick = async (frames = 1) => {
      const name = `t${++id}`;
      const done = new Promise((resolve, reject) => {
        ticks.push({ id: name, resolve });
        setTimeout(() => reject(new Error(`no tick ${name}`)), 10000).unref();
      });
      debug({ type: 'tick', dt: 1 / fps, frames, id: name });
      await done;
    };
    const SPOKEN = {
      laugh: { emotion: 'laughing', text: 'Ha ha ha! That is the funniest thing all day.' },
      sigh: { emotion: 'sad', text: 'Oh well, it happens.' },
      surprised_jolt: { emotion: 'surprised', text: 'Whoa, really?' },
      fist_pump: { emotion: 'excited', text: 'Yes, we did it!' },
      shake: { emotion: 'neutral', text: 'No, not that one.' },
      nod: { emotion: 'happy', text: 'Yes, exactly.' },
    };
    const LENGTH = { laugh: 2.4, impatient: 3.6, shoulder_roll: 2.8, stopwatch_ring: 2.4, spin: 2.2 };
    debug({ type: 'clock', held: true });
    // --turn <radians>: seen from the side, to check that her arms don't pass through her.
    if (option('turn', null)) debug({ type: 'turn', y: Number(option('turn', 0)) });
    for (const gesture of names) {
      const dir = path.join(outDir, gesture);
      fs.mkdirSync(dir, { recursive: true });
      const spoken = SPOKEN[gesture];
      send({ type: 'state', state: spoken ? 'speaking' : 'idle' });
      await tick(Math.round(fps * 1.2)); // settled into the state
      if (spoken) send({ type: 'segment', text: spoken.text, emotion: spoken.emotion, intensity: 0.9, gesture });
      else debug({ type: 'play', name: gesture });
      await sleep(100); // the message is in before the first step
      const frames = Math.round(fps * (LENGTH[gesture] ?? 2.2));
      for (let f = 0; f < frames; f++) {
        if (spoken) {
          // A laugh's mouth: open "ha"s with breaths between; plain talk otherwise.
          const t = f / fps;
          const level = gesture === 'laugh' ? 0.55 * Math.max(0, Math.sin(2 * Math.PI * 4.2 * t)) * (t < 1.6 ? 1 : 0) : 0.35 + 0.3 * Math.abs(Math.sin(t * 11));
          send({ type: 'mouth', level, v: [level * 0.9, 0, 0, level * 0.2, 0] });
          await sleep(5);
        }
        await tick();
        const file = path.join(dir, `f${String(f).padStart(4, '0')}.png`);
        const done = waitFor((m) => (m.type === 'captured' && m.path === file) || m.type === 'error');
        send({ type: 'capture', path: file });
        const msg = await done;
        if (msg.type === 'error') throw new Error(msg.message);
      }
      console.log(`clip ${gesture}: ${frames} frames in ${dir}`);
      send({ type: 'segment', text: '', emotion: 'neutral' });
      send({ type: 'state', state: 'idle' });
      await tick(Math.round(fps * 1.5));
    }
    debug({ type: 'clock', held: false });
  },
  // The orb form (form 'orb') frame by frame, as clips does: idle, listening, thinking, a tool running, speaking,
  // then pointing at a box from a corner of the window and back. Frames go to <out>/f0000.png and on.
  // The orb in real time: per phase, the frames' gaps and work (ms, from the renderer) and the CPU seconds every
  // process of the app used (Windows: summed over the Electron process tree), as a share of one core.
  async 'orb-live'() {
    const debug = (message) => send({ type: 'debug', message });
    const seconds = Number(option('seconds', 15));
    const tree = `$all = Get-CimInstance Win32_Process; $ids = @(${child.pid}); do { $n = $ids.Count; $ids = @($ids + ($all | ? { $ids -contains $_.ParentProcessId } | % ProcessId) | Select -Unique) } while ($ids.Count -gt $n)`;
    const powershell = (ps) => execFileSync('powershell', ['-NoProfile', '-Command', ps], { encoding: 'utf8' }).trim();
    const cpu = () => Number(powershell(`${tree}; ($all | ? { $ids -contains $_.ProcessId } | % { $_.KernelModeTime + $_.UserModeTime } | Measure -Sum).Sum`)) / 1e7;
    // The app's share of the GPU's 3D engine (Windows' per-process counters), averaged over 3 s.
    const gpu = () =>
      Number(
        powershell(
          `${tree}; $s = (Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -SampleInterval 1 -MaxSamples 3).CounterSamples | ? { $_.InstanceName -match 'engtype_3D' -and $_.InstanceName -match 'pid_(\\d+)_' -and $ids -contains [int]$Matches[1] }; [math]::Round((($s | Measure CookedValue -Sum).Sum) / 3, 2)`,
        ),
      );
    const phase = async (name, during = null) => {
      await sleep(1000);
      debug({ type: 'stats' }); // reset
      const c0 = cpu();
      const t0 = Date.now();
      let timer = null;
      if (during) timer = setInterval(during, 20);
      await sleep(seconds * 1000);
      if (timer) clearInterval(timer);
      const used = cpu() - c0;
      const gpuShare = gpu();
      const n = orbStats.length;
      debug({ type: 'stats' });
      while (orbStats.length === n) await sleep(50);
      const s = orbStats[orbStats.length - 1];
      const wall = (Date.now() - t0) / 1000;
      console.log(`phase ${name}: ${s.frames} frames (${(s.frames / wall).toFixed(1)} fps), gap ms ${JSON.stringify(s.gapMs)}, work ms ${JSON.stringify(s.workMs)}, CPU ${((100 * used) / wall).toFixed(1)}% of one core, GPU 3D ${gpuShare}%`);
    };
    // --fps: pace her at that rate instead of her own ~60, to see what a frame rate costs.
    if (option('fps', null)) debug({ type: 'pace', fps: Number(option('fps', 60)) });
    await sleep(1000);
    await phase('idle');
    send({ type: 'state', state: 'thinking' });
    send({ type: 'working', on: true });
    await phase('working');
    send({ type: 'working', on: false });
    send({ type: 'state', state: 'speaking' });
    const t0 = Date.now();
    await phase('speaking', () => {
      const t = (Date.now() - t0) / 1000;
      const level = (t % 1.6 < 1.3 ? 1 : 0) * (0.25 + 0.45 * Math.max(0, Math.sin(2 * Math.PI * 4.1 * t)) ** 2);
      send({ type: 'mouth', level, v: [level, 0, 0, 0, 0] });
    });
    send({ type: 'state', state: 'idle' });
    send({ type: 'hide' });
    await waitFor((m) => m.type === 'hidden', 5000);
    const c0 = cpu();
    await sleep(seconds * 1000);
    console.log(`phase hidden: CPU ${((100 * (cpu() - c0)) / seconds).toFixed(1)}% of one core`);
  },
  async orb() {
    const debug = (message) => send({ type: 'debug', message });
    const fps = Number(option('fps', 30));
    const size = config.orbSize;
    let id = 0;
    let frame = 0;
    const tick = async (frames = 1) => {
      const name = `t${++id}`;
      const done = new Promise((resolve, reject) => {
        ticks.push({ id: name, resolve });
        setTimeout(() => reject(new Error(`no tick ${name}`)), 10000).unref();
      });
      debug({ type: 'tick', dt: 1 / fps, frames, id: name });
      await done;
    };
    // `seconds` of frames, each captured; `each(t)` runs before each one (t in s).
    const record = async (seconds, each = null) => {
      const frames = Math.round(fps * seconds);
      for (let f = 0; f < frames; f++) {
        if (each) {
          each(f / fps);
          await sleep(5);
        }
        await tick();
        const file = path.join(outDir, `f${String(frame++).padStart(4, '0')}.png`);
        const done = waitFor((m) => (m.type === 'captured' && m.path === file) || m.type === 'error');
        send({ type: 'capture', path: file });
        const msg = await done;
        if (msg.type === 'error') throw new Error(msg.message);
      }
    };
    debug({ type: 'clock', held: true });
    await tick(Math.round(fps * 1));
    // --hop: only crossing the screen, as one window sees it: zipping out toward the right (frames f0000...), then
    // into a window from the left (the frames after). tools/orb-hop.py lays two windows and the streak round them.
    if (option('hop', null)) {
      await tick(Math.round(fps * 0.5));
      await record(0.1);
      debug({ type: 'zip-out', to: { x: size * 3, y: size / 2 } });
      await record(0.4);
      console.log(`hop: zipped out by f${String(frame - 1).padStart(4, '0')}`);
      debug({ type: 'show', reason: 'return', from: { x: -size * 2, y: size / 2 } });
      await record(0.8);
      debug({ type: 'clock', held: false });
      console.log(`orb: ${frame} frames in ${outDir}`);
      return;
    }
    // --clip idle|speaking|listening|thinking|working [--seconds N] [--ghost K]: only that look, N s of it (default
    // 8), for before/after clips of one state.
    const only = option('clip', null);
    if (only) {
      const seconds = Number(option('seconds', 8));
      if (only === 'working') {
        send({ type: 'state', state: 'thinking' });
        send({ type: 'working', on: true });
      } else if (only !== 'idle') send({ type: 'state', state: only });
      await tick(Math.round(fps * 1.5)); // settled into it
      // --ghost 0|1|2: one of her rare ghosts (a ring, a polygon, an arc) as the clip starts.
      // Without it, none at all: a random one in a long clip would spoil a before/after comparison.
      debug(option('ghost', null) != null ? { type: 'ghost', kind: Number(option('ghost', 0)) } : { type: 'ghost', off: true });
      await record(seconds, only === 'speaking' ? (t) => {
        const level = (t % 1.6 < 1.3 ? 1 : 0) * (0.25 + 0.45 * Math.max(0, Math.sin(2 * Math.PI * 4.1 * t)) ** 2);
        send({ type: 'mouth', level, v: [level, 0, 0, 0, 0] });
      } : null);
      debug({ type: 'clock', held: false });
      console.log(`orb: ${frame} frames in ${outDir}`);
      return;
    }
    await record(3); // idle: breathing
    send({ type: 'state', state: 'listening' });
    await record(2.5);
    send({ type: 'state', state: 'thinking' });
    await record(3);
    send({ type: 'working', on: true });
    await record(3);
    send({ type: 'working', on: false });
    send({ type: 'state', state: 'speaking' });
    // Syllables at about 4 a second, with pauses between phrases.
    await record(4, (t) => {
      const phrase = t % 1.6 < 1.3 ? 1 : 0;
      const level = phrase * (0.25 + 0.45 * Math.max(0, Math.sin(2 * Math.PI * 4.1 * t)) ** 2);
      send({ type: 'mouth', level, v: [level, 0, 0, 0, 0] });
    });
    send({ type: 'mouth', level: 0, v: [0, 0, 0, 0, 0] });
    send({ type: 'state', state: 'idle' });
    await record(1.2);
    // Pointing: she sits in the bottom left corner of the window and shows a box at the top right.
    send({ type: 'hide' });
    await tick(Math.round(fps * 0.6));
    const home = Math.round(size * 0.4);
    debug({ type: 'layout', width: size, height: size, mode: 'point', body: { x: 0, y: size - home, w: home, h: home } });
    send({ type: 'show', reason: 'always' });
    await record(1);
    debug({ type: 'point', target: { x: size * 0.72, y: size * 0.24 }, box: { x: size * 0.58, y: size * 0.16, w: size * 0.3, h: size * 0.16 } });
    await record(2.4);
    send({ type: 'point-ending', seconds: 1.2 });
    await record(1.2);
    // (Through debug, as the point was: sent plainly, the app's main process took it and she never came back.)
    debug({ type: 'unpoint' });
    await record(1.2);
    // Feelings, back at home: speaking happily (her motes lift), then sad after (they sink, she dims).
    send({ type: 'hide' });
    await tick(Math.round(fps * 0.6));
    debug({ type: 'layout', width: size, height: size, mode: 'home' });
    send({ type: 'show', reason: 'always' });
    await record(0.8);
    send({ type: 'state', state: 'speaking' });
    send({ type: 'segment', text: 'That worked!', emotion: 'happy', intensity: 0.8 });
    await record(3, (t) => {
      const level = (t % 1.6 < 1.3 ? 1 : 0) * (0.25 + 0.45 * Math.max(0, Math.sin(2 * Math.PI * 4.1 * t)) ** 2);
      send({ type: 'mouth', level, v: [level, 0, 0, 0, 0] });
    });
    send({ type: 'mouth', level: 0, v: [0, 0, 0, 0, 0] });
    send({ type: 'state', state: 'idle' });
    send({ type: 'emotion', emotion: 'sad', intensity: 0.6, hold: 3 });
    await record(3.5);
    debug({ type: 'clock', held: false });
    console.log(`orb: ${frame} frames in ${outDir}`);
  },
};
// Scenarios left out of `all`: they move the window around, or need something configured.
const SPECIAL = new Set(['pointing', 'point-flow', 'props', 'hold', 'credit', 'moments', 'nap', 'life', 'clips', 'orb', 'orb-live']);

async function run() {
  await waitFor((m) => m.type === 'ready' || m.type === 'error', 60000);
  if (scenario === 'preview') {
    // The demo drives itself, hide and show included; just watch.
    for (let i = 0; i < 32; i++) {
      await sleep(1500);
      await capture(`preview-${String(i).padStart(2, '0')}`).catch((err) => console.log(`no capture: ${err.message}`));
    }
    send({ type: 'quit' });
    return;
  }
  send({ type: 'show', reason: 'always' });
  await sleep(1500);
  const everyday = Object.keys(SCENARIOS).filter((name) => !SPECIAL.has(name));
  const names = scenario === 'all' ? everyday : [scenario];
  for (const name of names) {
    if (!SCENARIOS[name]) throw new Error(`unknown scenario ${name}`);
    await SCENARIOS[name]();
  }
  report();
  send({ type: 'quit' });
}

run()
  .catch((err) => {
    console.error(err);
    child.kill();
    process.exitCode = 1;
  })
  .finally(() => setTimeout(() => child.kill(), 3000).unref());
