// Draws a sample of every kind of mark with the real marks app for a few seconds and saves what the first monitor's
// window shows, to check the look by eye: `node avatar/tools/marks-demo.mjs --out <folder> [--electron <exe>]`.
// They're drawn in the top left corner of the main monitor (content protection off, so the capture isn't blank).

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const avatarDir = path.join(here, '..');
const arg = (name, fallback) => {
  const at = process.argv.indexOf(name);
  return at >= 0 && process.argv[at + 1] ? process.argv[at + 1] : fallback;
};
const out = path.resolve(arg('--out', 'marks-demo'));
const electron = arg('--electron', path.join(avatarDir, 'node_modules', 'electron', 'dist', 'electron.exe'));
fs.mkdirSync(out, { recursive: true });
const cfgPath = path.join(out, 'marks-config.json');
fs.writeFileSync(cfgPath, JSON.stringify({ hideFromCapture: false, debug: true }));

const SAMPLE = [
  { kind: 'box', x: 130, y: 60, box: [60, 40, 200, 80], label: 'File menu', step: 1, done: true },
  { kind: 'circle', x: 330, y: 60, box: [290, 45, 370, 75], label: 'Save', step: 2 },
  { kind: 'arrow', x: 520, y: 60, label: 'then this field', step: 3 },
  { kind: 'highlight', x: 160, y: 200, box: [60, 170, 260, 230] },
  { kind: 'label', x: 420, y: 220, label: 'your email goes here' },
];

const app = spawn(electron, [path.join(avatarDir, 'marks'), '--config', cfgPath], { stdio: ['pipe', 'pipe', 'inherit'] });
const send = (msg) => app.stdin.write(`${JSON.stringify(msg)}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const killer = setTimeout(() => app.kill(), 15000); // never left on screen
const lines = readline.createInterface({ input: app.stdout });
const waiting = [];
lines.on('line', (line) => {
  console.log(line);
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  for (const w of waiting.splice(0)) w(msg);
});
const next = (type) =>
  new Promise((resolve) => {
    const take = (msg) => (msg.type === type || msg.type === 'error' ? resolve(msg) : waiting.push(take));
    waiting.push(take);
  });

await next('ready');
send({ type: 'draw', id: 'demo', marks: SAMPLE, stay_s: 6 });
const drawn = await next('drawn');
await sleep(700); // past the unfold
send({ type: 'capture', path: path.join(out, 'marks.png') });
await next('captured');
send({ type: 'quit' });
clearTimeout(killer);
console.log(JSON.stringify(drawn));
