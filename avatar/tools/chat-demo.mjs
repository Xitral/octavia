// Her chat window in the real app, for judging it by eye: hidden, filled with a sample conversation, and pictured in
// each of its states.
//
//   node tools/chat-demo.mjs [--out <dir>] [--electron <electron.exe>]
//
// Writes <out>/NN-<name>.png. Nothing is shown on screen; nothing talks to Octavia.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
};
const ELECTRON = option('electron', null) || require('electron');
const outDir = path.resolve(option('out', path.join(os.tmpdir(), 'octavia-chat')));
fs.mkdirSync(outDir, { recursive: true });
const configPath = path.join(outDir, 'config.json');
fs.writeFileSync(configPath, JSON.stringify({ name: 'Octavia', hidden: true }));

const child = spawn(ELECTRON, [path.join(APP_DIR, 'chat'), '--config', configPath], { stdio: ['pipe', 'pipe', 'inherit'] });
const waiters = [];
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  for (const w of waiters.filter((w) => w.type === msg.type)) {
    waiters.splice(waiters.indexOf(w), 1);
    w.resolve(msg);
  }
});
const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
const next = (type) => new Promise((resolve) => waiters.push({ type, resolve }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let shot = 0;
async function picture(name) {
  await sleep(350);
  const file = path.join(outDir, `${String(++shot).padStart(2, '0')}-${name}.png`);
  const done = next('captured');
  send({ type: 'capture', path: file }); // a hidden window's picture lags a frame: the second is the one kept
  await done;
  await sleep(120);
  const again = next('captured');
  send({ type: 'capture', path: file });
  await again;
  console.log(file);
}
async function script(code) {
  const done = next('scripted');
  send({ type: 'script', id: 1, code });
  return done;
}

const ready = next('ready');
await ready;
await sleep(500);
const now = Date.now() / 1000;
const line = (who, text, ago) => send({ type: 'line', who, text, at: now - ago });
send({ type: 'settings', speak: true });
line('you', 'Whats the quadratic formula again, and can you check the weather for tomorrow?', 3000);
line('did', 'web search: weather tomorrow Boston', 2990);
line('did', 'show a panel “Quadratic formula”', 2985);
line('her', 'It\'s $x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$. Tomorrow looks **cold and clear**, about 41 degrees, and the forecast is at https://weather.example.com/boston.', 2980);
line('you', 'nice. what do I still have due this week', 1500);
line('did', 'canvas assignments', 1495);
line('her', 'Three things:\n\n- **CS 311** problem set 6, Tuesday at 11:59\n- **Stats** quiz 4, Wednesday\n- **Writing** draft, Friday\n\n```\nleft: 3   done: 7\n```', 1490);
line('you', 'pin the formula panel and remind me about the quiz', 200);
line('did', 'pin a panel', 195);
line('did', 'create reminder: stats quiz', 190);
line('her', 'Pinned, and I\'ll remind you tomorrow evening.', 185);
send({
  type: 'panels',
  rows: [
    { id: 'q', title: 'Quadratic formula', up: true, pinned: true, at: null, about: 'notes' },
    { id: 'w', title: 'Weather tomorrow', up: true, pinned: false, at: null, about: 'facts' },
    { id: 'k', title: 'Krebs cycle', up: false, pinned: false, at: now - 5400, about: 'diagram, notes' },
    { id: 'm', title: 'Master theorem cases', up: false, pinned: false, at: now - 190000, about: 'steps' },
  ],
});
await sleep(1100);
await script("document.body.classList.add('still')");
await picture('conversation');

send({ type: 'status', text: 'Thinking...' });
send({ type: 'ask', id: 1, text: 'Should I send the email to Professor Lee?\nSubject: Extension on problem set 6' });
await picture('question');
send({ type: 'unask', id: 1 });
send({ type: 'status', text: '' });

await script("document.getElementById('open-shelf').click()");
await picture('her-panels');
await script("document.getElementById('shelf-close').click()");

await script("document.getElementById('open-search').click(); document.getElementById('query').value = 'quiz';");
send({
  type: 'found',
  id: 0,
  query: 'quiz',
  results: [
    { who: 'you', text: 'pin the formula panel and remind me about the quiz', at: now - 200 },
    { who: 'her', text: 'Stats quiz 3 went fine, you got 18 out of 20 on it.', at: now - 700000 },
  ],
});
await picture('search');
await script("document.getElementById('search-close').click()");

send({
  type: 'day',
  day: now - 90000,
  prev: true,
  next: true,
  focus: null,
  lines: [
    { who: 'you', text: 'how does the master theorem work', at: now - 90000 },
    { who: 'her', text: 'It compares how fast the work grows at each level. Three cases: leaves dominate, it\'s even, or the root dominates.', at: now - 89990 },
  ],
});
await picture('past-day');
await script("document.getElementById('day-today').click()");

await script("document.getElementById('make-mini').click()");
await sleep(400);
await picture('small');
send({ type: 'quit' });
