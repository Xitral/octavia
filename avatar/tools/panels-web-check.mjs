// Checks pictures and web pages on her panels for real, off screen: nothing is ever shown and no browser is opened.
//   node avatar/tools/panels-web-check.mjs --out <dir> --image <a cleaned img-....jpg>
// Starts the panels app twice on the avatar's Electron with its checks-only `offscreen` setting. The first run lets
// pages come from a test server on this PC (`webAllowLocal`): a picture panel and a page panel come up, the page
// loads and says its title, its pop-up and download never reach the server, and a picture of each panel is saved.
// The second run is as she runs: the same page on this PC is blocked. Exits non-zero when a check fails.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const avatarDir = path.join(here, '..');
const args = process.argv.slice(2);
const out = path.resolve(args[args.indexOf('--out') + 1] || '.');
const imagePath = path.resolve(args[args.indexOf('--image') + 1]);
fs.mkdirSync(out, { recursive: true });
const imagesDir = path.join(out, 'images');
fs.mkdirSync(imagesDir, { recursive: true });
const imageName = path.basename(imagePath);
fs.copyFileSync(imagePath, path.join(imagesDir, imageName));

const hits = [];
const server = http.createServer((req, res) => {
  hits.push(req.url);
  if (req.url === '/page') {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end(`<!doctype html><title>Krebs cycle notes</title><body style="font:20px sans-serif;background:#fff">
      <h1>Krebs cycle</h1><p>Acetyl-CoA joins oxaloacetate.</p>
      <script>try { window.open('/popup'); } catch {}
      const a = document.createElement('a'); a.href = '/file.zip'; a.download = 'x.zip'; document.body.append(a); a.click();
      alert('hi'); navigator.geolocation && navigator.geolocation.getCurrentPosition(() => fetch('/located'));</script>`);
  } else if (req.url === '/file.zip') {
    res.writeHead(200, { 'content-type': 'application/zip', 'content-disposition': 'attachment' });
    res.end('PK');
  } else {
    res.writeHead(404);
    res.end();
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const pageUrl = `http://127.0.0.1:${server.address().port}/page`;

const electronDir = args.includes('--electron') ? path.resolve(args[args.indexOf('--electron') + 1]) : path.join(avatarDir, 'node_modules', 'electron');
const exe = path.join(electronDir, 'dist', fs.readFileSync(path.join(electronDir, 'path.txt'), 'utf8').trim());
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`);
  if (!ok) failures.push(what);
};

async function run(name, allowLocal, steps) {
  const config = path.join(out, `${name}.json`);
  fs.writeFileSync(config, JSON.stringify({ hideFromCapture: false, maxPanels: 3, statePath: null, debug: true, offscreen: true, webAllowLocal: allowLocal, imagesDir }));
  const child = spawn(exe, [path.join(avatarDir, 'panels'), '--config', config], { stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.on('data', (d) => fs.appendFileSync(path.join(out, `${name}.log`), d));
  const seen = [];
  const waiters = [];
  readline.createInterface({ input: child.stdout }).on('line', (line) => {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    seen.push(msg);
    for (const w of waiters.splice(0)) if (!w(msg)) waiters.push(w);
  });
  const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
  const next = (pred, ms = 15000) =>
    new Promise((resolve) => {
      const found = seen.find(pred);
      if (found) return resolve(found);
      const timer = setTimeout(() => resolve(null), ms);
      waiters.push((m) => (pred(m) ? (clearTimeout(timer), resolve(m), true) : false));
    });
  await next((m) => m.type === 'ready');
  try {
    await steps({ send, next, seen });
  } finally {
    send({ type: 'quit' });
    await sleep(500);
    child.kill();
  }
}

await run('local', true, async ({ send, next, seen }) => {
  send({
    type: 'show',
    id: 'pic',
    stay_s: 0,
    spec: {
      title: 'Sora',
      width: 'narrow',
      blocks: [{ kind: 'image', images: [{ src: imageName, alt: 'Sora', source: 'https://www.khwiki.com/Sora', site: 'khwiki.com', width: 0, height: 0 }], caption: '' }],
      overlays: [],
    },
  });
  const pic = await next((m) => m.type === 'shown' && m.id === 'pic');
  check(pic && pic.height > 200, `the picture panel came up (${pic && `${pic.width}x${pic.height}`}, notes: ${pic && pic.notes.join('; ')})`);
  check(pic && !pic.notes.length, 'the picture decoded from images/');
  await sleep(800);
  send({ type: 'capture', id: 'pic', path: path.join(out, 'picture.png') });
  await next((m) => m.type === 'captured' && m.id === 'pic');

  send({ type: 'show', id: 'web', stay_s: 0, spec: { title: 'Krebs notes', width: 'wide', blocks: [{ kind: 'page', url: pageUrl, height: 420, caption: '' }], overlays: [] } });
  const web = await next((m) => m.type === 'shown' && m.id === 'web');
  check(Boolean(web), 'the page panel came up');
  const loaded = await next((m) => m.type === 'page' && m.id === 'web' && m.state !== 'loading');
  check(loaded && loaded.state === 'loaded' && loaded.title === 'Krebs cycle notes', `the page loaded and said its title (${JSON.stringify(loaded)})`);
  await sleep(1500);
  check(!hits.includes('/popup'), 'its pop-up never opened');
  check(!hits.includes('/located'), 'it got no location');
  send({ type: 'capture', id: 'web', path: path.join(out, 'page-panel.png') });
  await next((m) => m.type === 'captured' && m.id === 'web');
  send({ type: 'snapshot', id: 'web' });
  const snap = await next((m) => m.type === 'snapshot' && m.id === 'web');
  check(snap && snap.error, 'look_at_panel refuses a page panel');
  // her change to the address: the same panel goes there
  send({ type: 'show', id: 'web', stay_s: 0, spec: { title: 'Krebs notes', width: 'wide', blocks: [{ kind: 'page', url: `${pageUrl}?2`, height: 420, caption: '' }], overlays: [] } });
  const again = await next((m) => m.type === 'page' && m.id === 'web' && m.state === 'loaded' && String(m.url).endsWith('?2'));
  check(Boolean(again), 'a changed address loads in the same panel');
  send({ type: 'close', id: 'web' });
  check(Boolean(await next((m) => m.type === 'closed' && m.id === 'web')), 'the page panel closes');
  void seen;
});

await run('blocked', false, async ({ send, next }) => {
  send({ type: 'show', id: 'web', stay_s: 0, spec: { title: 'x', width: 'wide', blocks: [{ kind: 'page', url: pageUrl, height: 300, caption: '' }], overlays: [] } });
  await next((m) => m.type === 'shown' && m.id === 'web');
  const blocked = await next((m) => m.type === 'page' && m.id === 'web' && m.state !== 'loading');
  check(blocked && blocked.state === 'blocked', `a page on this PC is blocked as she runs (${JSON.stringify(blocked)})`);
  // an image name that isn't one of hers: never served
  send({ type: 'show', id: 'bad', stay_s: 0, spec: { title: 'x', width: 'narrow', blocks: [{ kind: 'image', images: [{ src: '../panels.json', alt: '', source: '', site: '', width: 0, height: 0 }], caption: '' }], overlays: [] } });
  const bad = await next((m) => m.type === 'shown' && m.id === 'bad');
  check(bad && bad.notes.some((n) => /couldn't be drawn|couldn't be shown/.test(n)), `a stray picture name shows nothing (${bad && bad.notes.join('; ')})`);
});

server.close();
check(!hits.includes('/file.zip') || !fs.readdirSync(out).some((f) => f.endsWith('.zip')), 'no download was saved');
console.log(failures.length ? `${failures.length} failed` : 'all passed');
process.exit(failures.length ? 1 : 0);
