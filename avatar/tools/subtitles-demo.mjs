// Her subtitles in the real app, for judging them by eye: the orb at her default spot (or --x/--y), speaking a few
// sentences the way Octavia sends them (math, code, a long one that scrolls), each captured with the orb into one
// picture on a dark desktop, plus the subtitle window alone.
//
//   node tools/subtitles-demo.mjs [--out <dir>] [--x 1584 --y 720] [--orb-size 320]
//
// Content protection is off for the demo (capturePage is blank while it's on). Writes <out>/NN-<name>.png (the
// subtitles window) and <out>/NN-<name>-with-orb.png (orb and subtitles at their real places, on a dark ground).

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ELECTRON = require('electron');

const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
};
const outDir = path.resolve(option('out', path.join(os.tmpdir(), 'octavia-subtitles')));
fs.mkdirSync(outDir, { recursive: true });
const orbSize = Number(option('orb-size', 320));
const x = option('x', null);
const y = option('y', null);

const config = {
  model: path.join(APP_DIR, '..', 'models', 'avatar', 'octavia.vrm'),
  form: 'orb',
  orbSize,
  height: orbSize,
  position: x != null && y != null ? { x: Number(x), y: Number(y) } : null,
  statePath: path.join(outDir, 'state.json'),
  credit: 'Octavia',
  hideFromCapture: false,
  debug: false,
};
const configPath = path.join(outDir, 'config.json');
fs.writeFileSync(configPath, JSON.stringify(config));

const child = spawn(ELECTRON, [APP_DIR, '--config', configPath], { stdio: ['pipe', 'pipe', 'pipe'] });
const waiters = [];
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  for (const w of waiters.splice(0)) (w.match(msg) ? w.resolve(msg) : waiters.push(w));
});
readline.createInterface({ input: child.stderr }).on('line', (line) => console.error(`stderr: ${line}`));
const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = (match, ms = 20000) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out')), ms);
    waiters.push({ match, resolve: (m) => (clearTimeout(timer), resolve(m)) });
  });

async function shot(kind, file) {
  const done = waitFor((m) => (m.type === 'captured' && m.path === file) || m.type === 'error');
  send({ type: kind, path: file });
  const msg = await done;
  if (msg.type === 'error') throw new Error(msg.message);
  return msg;
}

const SCENES = [
  ['reminder', ['I set a reminder for 7 PM.', 'Do you want me to notify you on desktop and phone?']],
  ['math', ['The quadratic formula gives $x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$.', 'Here $a = 2$, $b = -3$ and $c = 1$, so $x = 1$ or $x = \\frac{1}{2}$.']],
  ['logic', ['De Morgan: $\\neg (p \\land q) \\equiv \\neg p \\lor \\neg q$.']],
  ['code', ['Run `uv run pytest -q -n 8` from the project folder, then check `data/logs/octavia.log` for 0x80070005.']],
  ['long', ['This one is deliberately long, the kind of sentence she says when she walks through a whole worked example in one breath, with $f(x) = 3x^2 + 2x - 5$, its derivative $f\'(x) = 6x + 2$, the critical point at $x = -\\frac{1}{3}$, and the value there, which is about $-5.33$, before she moves on to the second part of the question.']],
];

try {
  await waitFor((m) => m.type === 'ready');
  send({ type: 'show', reason: 'talk' });
  send({ type: 'state', state: 'speaking' });
  await sleep(1200);
  let n = 0;
  for (const [name, sentences] of SCENES) {
    for (const s of sentences) {
      send({ type: 'segment', text: s, emotion: 'neutral', intensity: 0.3, gesture: null, subtitle: s });
      await sleep(900);
    }
    n += 1;
    const tag = `${String(n).padStart(2, '0')}-${name}`;
    const subs = await shot('subtitles-capture', path.join(outDir, `${tag}.png`));
    await shot('capture', path.join(outDir, `${tag}-orb.png`));
    fs.writeFileSync(path.join(outDir, `${tag}.json`), JSON.stringify({ subs: subs.bounds, her: subs.her }));
    console.log(`${tag}: subtitles ${JSON.stringify(subs.bounds)}, her ${JSON.stringify(subs.her)}`);
    send({ type: 'subtitles-end', interrupted: false });
    await sleep(name === 'long' ? 200 : 4500);
  }
  if (true) {
    // the long one again, near the end of its scroll
    send({ type: 'segment', text: 'x', subtitle: SCENES.at(-1)[1][0] });
    await sleep(14000);
    const tag = `${String(n + 1).padStart(2, '0')}-long-later`;
    const subs = await shot('subtitles-capture', path.join(outDir, `${tag}.png`));
    await shot('capture', path.join(outDir, `${tag}-orb.png`));
    fs.writeFileSync(path.join(outDir, `${tag}.json`), JSON.stringify({ subs: subs.bounds, her: subs.her }));
  }
} finally {
  send({ type: 'quit' });
  setTimeout(() => child.kill(), 3000);
}
