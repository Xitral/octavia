// Shows sample panels and saves a picture of each, for judging the look by eye:
//   node avatar/tools/panels-demo.mjs --out <dir> [--keep] [--live | --drag | --film] [--offscreen]
// Starts the panels app on the avatar's Electron, sends each sample from SAMPLES, captures it once its unfold
// has played, and closes it (unless --keep, which leaves the last ones up for a minute). --live instead walks one
// panel through her changes (a node lit, steps marked, a step added) and a few pen strokes, capturing each.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import { LIVE, SAMPLES } from './panel-samples.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const avatarDir = path.join(here, '..');
const args = process.argv.slice(2);
const out = path.resolve(args[args.indexOf('--out') + 1] || '.');
const keep = args.includes('--keep');
const only = args.includes('--only') ? args[args.indexOf('--only') + 1].split(',') : null;
fs.mkdirSync(out, { recursive: true });

const electronDir = path.join(avatarDir, 'node_modules', 'electron');
const exe = args.includes('--electron') // a checkout with no node_modules of its own
  ? args[args.indexOf('--electron') + 1]
  : path.join(electronDir, 'dist', fs.readFileSync(path.join(electronDir, 'path.txt'), 'utf8').trim());
const config = path.join(out, 'panels-config.json');
// Not hidden from capture here: capturePage reads the page itself, but a screen recorder should see it too.
// Debug on: the --live run plays mouse events into the page to try the pen.
fs.writeFileSync(config, JSON.stringify({ hideFromCapture: false, maxPanels: 3, statePath: null, debug: true, offscreen: args.includes('--offscreen') }));
const child = spawn(exe, [path.join(avatarDir, 'panels'), '--config', config], { stdio: ['pipe', 'pipe', 'inherit'] });
const waiters = [];
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  console.log('<-', line.slice(0, 200));
  for (const w of waiters.splice(0)) if (!w(msg)) waiters.push(w);
});
const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
const next = (pred) => new Promise((resolve) => waiters.push((m) => (pred(m) ? (resolve(m), true) : false)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await next((m) => m.type === 'ready');
const capture = async (id, name) => {
  send({ type: 'capture', id, path: path.join(out, `${name}.png`) });
  await next((m) => (m.type === 'captured' || m.type === 'error') && m.id === id);
};
if (args.includes('--film')) {
  // The filmstrip: dragged by mouse events played into the page, then let go with a flick, a picture at each stage.
  send({ type: 'show', id: 'film', spec: SAMPLES.film, stay_s: 0 });
  await next((m) => m.type === 'shown' && m.id === 'film');
  await sleep(700);
  await capture('film', 'film-1-start');
  const y = 400;
  send({ type: 'input', id: 'film', events: [{ type: 'mouseMove', x: 400, y }, { type: 'mouseDown', x: 400, y }] });
  for (let x = 400; x >= 160; x -= 20) {
    send({ type: 'input', id: 'film', events: [{ type: 'mouseMove', x, y, modifiers: ['leftButtonDown'] }] });
    await sleep(16);
  }
  await capture('film', 'film-2-dragged');
  send({ type: 'input', id: 'film', events: [{ type: 'mouseUp', x: 160, y }] });
  await sleep(900);
  await capture('film', 'film-3-flicked');
  send({ type: 'show', id: 'film', spec: { ...SAMPLES.film, blocks: [{ ...SAMPLES.film.blocks[0], seek: 1, at: SAMPLES.film.blocks[0].frames[0].start + 200 }] }, stay_s: 0 });
  await sleep(1400);
  await capture('film', 'film-4-she-moved-it');
  send({ type: 'quit' });
  process.exit(0);
}
if (args.includes('--drag')) {
  // Holds the title bar for 1 s (the pointer itself stays put): the app logs how many steps the drag took.
  send({ type: 'show', id: 'drag', spec: SAMPLES.explain, stay_s: 0 });
  await next((m) => m.type === 'shown' && m.id === 'drag');
  await sleep(600);
  send({ type: 'input', id: 'drag', events: [{ type: 'mouseMove', x: 120, y: 38 }, { type: 'mouseDown', x: 120, y: 38 }] });
  await sleep(1000);
  send({ type: 'input', id: 'drag', events: [{ type: 'mouseUp', x: 120, y: 38 }] });
  await sleep(300);
  send({ type: 'quit' });
  process.exit(0);
}
if (args.includes('--live')) {
  const walk = structuredClone(LIVE);
  send({ type: 'show', id: 'walk', spec: walk, stay_s: 0 });
  const first = await next((m) => m.type === 'shown' && m.id === 'walk');
  await sleep(1500);
  await capture('walk', 'live-0');
  walk.blocks[0].nodes[1].highlight = true;
  walk.blocks[1].items[0].state = 'done';
  walk.blocks[1].items[1].state = 'current';
  walk.blocks[1].items.push({ title: 'Acetyl-CoA joins the cycle', state: 'todo' });
  send({ type: 'show', id: 'walk', spec: walk, stay_s: 0 });
  await next((m) => m.type === 'shown' && m.id === 'walk');
  await capture('walk', 'live-1a'); // right after: what it kept must still be there, not blinking back in
  await sleep(700);
  await capture('walk', 'live-1');
  // the pen: its button, then a squiggle and a circle over the diagram
  const pad = 18;
  const x0 = first.width + pad - 50;
  const ev = (type, x, y, button = 'left') => ({ type, x, y, button });
  const strokes = [ev('mouseMove', x0, pad + 18), ev('mouseDown', x0, pad + 18), ev('mouseUp', x0, pad + 18)];
  const pts = Array.from({ length: 40 }, (_, k) => [70 + k * 6, 120 + Math.sin(k / 3) * 14]);
  const held = (x, y) => ({ ...ev('mouseMove', x, y), modifiers: ['leftButtonDown'] }); // a move with the button down
  strokes.push(ev('mouseMove', ...pts[0]), ev('mouseDown', ...pts[0]), ...pts.map((p) => held(...p)), ev('mouseUp', ...pts.at(-1)));
  const ring = Array.from({ length: 36 }, (_, k) => [240 + 40 * Math.cos(k / 5.5), 210 + 24 * Math.sin(k / 5.5)]);
  strokes.push(ev('mouseMove', ...ring[0]), ev('mouseDown', ...ring[0]), ...ring.map((p) => held(...p)), ev('mouseUp', ...ring.at(-1)));
  send({ type: 'input', id: 'walk', events: strokes });
  await sleep(500);
  await capture('walk', 'live-2');
  walk.blocks[0].nodes[1].highlight = false;
  walk.blocks[0].nodes[2].highlight = true;
  send({ type: 'show', id: 'walk', spec: walk, stay_s: 0 });
  await next((m) => m.type === 'shown' && m.id === 'walk');
  await sleep(700);
  await capture('walk', 'live-3');
  if (keep) await sleep(60000);
  send({ type: 'quit' });
  process.exit(0);
}
for (const [name, spec] of Object.entries(SAMPLES)) {
  if (only && !only.includes(name)) continue;
  const started = Date.now();
  send({ type: 'show', id: name, spec, stay_s: keep ? 60 : 0 });
  const shown = await next((m) => (m.type === 'shown' || m.type === 'error') && m.id === name);
  console.log(`${name}: ${shown.type} in ${Date.now() - started} ms`);
  await sleep(1600);
  send({ type: 'capture', id: name, path: path.join(out, `${name}.png`) });
  await next((m) => (m.type === 'captured' || m.type === 'error') && m.id === name);
  if (!keep) {
    send({ type: 'close', id: name });
    await next((m) => m.type === 'closed' && m.id === name);
  }
}
if (keep) await sleep(60000);
send({ type: 'quit' });
