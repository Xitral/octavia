// Her subtitles in motion, for a before/after video: the real app speaking a few sentences the way Octavia sends
// them (with her voice level as 'mouth' messages and a feeling on each), the orb and the subtitle window captured
// over and over with the time of each, for tools/subtitles-video.py to put together on a dark-and-white ground.
//
//   node tools/subtitles-clip.mjs --out <dir> [--app <avatar dir>] [--fps 15] [--measure]
//
// --app runs another copy of the avatar app (an older checkout, for "before"). Writes <out>/s-NNNN.png (subtitles),
// <out>/o-NNNN.png (orb) and <out>/frames.json (each frame's time and both windows' bounds).
// --measure captures nothing: it speaks the script three times over and prints the app's share of the GPU's 3D engine
// and of one CPU core while she does (Windows' per-process counters, the whole Electron process tree).

import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
};
const HERE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP_DIR = path.resolve(option('app', HERE));
const ELECTRON = createRequire(path.join(APP_DIR, 'package.json'))('electron');
const outDir = path.resolve(option('out', 'subtitles-clip'));
fs.mkdirSync(outDir, { recursive: true });
const fps = Number(option('fps', 15));
const measure = process.argv.includes('--measure');
const run = promisify(execFile);

const config = {
  model: path.join(APP_DIR, '..', 'models', 'avatar', 'octavia.vrm'),
  form: 'orb',
  orbSize: 320,
  height: 320,
  position: { x: 1500, y: 640 },
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
  return msg.type === 'error' ? null : msg;
}

// [seconds in, sentence, emotion, intensity, seconds she speaks it, what her voice says (if not the sentence)]
// --script <json> gives a real recording's instead ({segments, levels: her voice at 50 a second, end, total}), so the
// clip can be put with its audio (subtitles-video.py --audio).
const scripted = option('script', null) ? JSON.parse(fs.readFileSync(option('script'), 'utf8')) : null;
const SCRIPT = scripted ? scripted.segments : [
  [0.4, 'Your essay is due Thursday at midnight.', 'neutral', 0.3, 2.6],
  [3.2, 'Good news, though: you already have most of it written!', 'happy', 0.85, 3.2],
  [6.6, 'The derivative of $f(x) = 3x^2 + 2x$ is $f\'(x) = 6x + 2$.', 'curious', 0.6, 3.4],
  [10.2, 'Sadly the lab report still needs its whole method section.', 'sad', 0.8, 3.4],
];
const END_S = scripted ? scripted.end : 13.8;
const TOTAL_S = scripted ? scripted.total : 19;

// Her voice level while she says a sentence: syllables at about 4 a second, with breaths between phrases.
function level(t, start, length) {
  if (scripted) return scripted.levels[Math.floor(t * 50)] || 0;
  const s = t - start;
  if (s < 0 || s > length) return 0;
  return (s % 1.5 < 1.25 ? 1 : 0.05) * (0.2 + 0.6 * Math.max(0, Math.sin(2 * Math.PI * 4.2 * s)) ** 1.5);
}

try {
  await waitFor((m) => m.type === 'ready');
  send({ type: 'show', reason: 'talk' });
  send({ type: 'state', state: 'speaking' });
  await sleep(1500);
  const t0 = Date.now();
  const now = () => (Date.now() - t0) / 1000;
  let next = 0;
  let ended = false;
  const voice = setInterval(() => {
    const t = now();
    while (next < SCRIPT.length && t >= SCRIPT[next][0]) {
      const [, text, emotion, intensity, len, said] = SCRIPT[next];
      send({ type: 'segment-audio', index: next, seconds: len }); // as her voice does, once it has made the audio
      send({ type: 'segment', text: said || text, emotion, intensity, gesture: null, subtitle: text, index: next });
      next += 1;
    }
    // A scripted sentence's word times (its 7th field), as her word times send them: each as the audio reaches it.
    for (let k = 0; k < next; k += 1) {
      const times = SCRIPT[k][6];
      if (!Array.isArray(times)) continue;
      SCRIPT[k].sent = SCRIPT[k].sent ?? -1;
      let reached = SCRIPT[k].sent;
      while (reached + 1 < times.length && t - SCRIPT[k][0] >= times[reached + 1]) reached += 1;
      if (reached > SCRIPT[k].sent) {
        SCRIPT[k].sent = reached;
        send({ type: 'segment-word', index: k, word: reached, of: times.length });
      }
    }
    const lv = SCRIPT.reduce((m, [start, , , , len]) => Math.max(m, level(t, start, len)), 0);
    send({ type: 'mouth', level: Number(lv.toFixed(3)) });
    if (!ended && t >= END_S) {
      ended = true;
      send({ type: 'state', state: 'idle' });
      send({ type: 'subtitles-end', interrupted: false });
    }
  }, 20);
  if (measure) {
    clearInterval(voice);
    const tree = `$all = Get-CimInstance Win32_Process; $ids = @(${child.pid}); do { $n = $ids.Count; $ids = @($ids + ($all | ? { $ids -contains $_.ParentProcessId } | % ProcessId) | Select -Unique) } while ($ids.Count -gt $n)`;
    // Not execFileSync: her voice has to keep coming while the counters are read.
    const powershell = async (ps) => (await run('powershell', ['-NoProfile', '-Command', ps], { encoding: 'utf8' })).stdout.trim();
    const cpu = async () => Number(await powershell(`${tree}; ($all | ? { $ids -contains $_.ProcessId } | % { $_.KernelModeTime + $_.UserModeTime } | Measure -Sum).Sum`)) / 1e7;
    // Each second's share, then their mean.
    const gpu = async (n) => {
      const each = (
        await powershell(
          `${tree}; $sets = Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -SampleInterval 1 -MaxSamples ${n}; ($sets | % { [math]::Round((($_.CounterSamples | ? { $_.InstanceName -match 'engtype_3D' -and $_.InstanceName -match 'pid_(\\d+)_' -and $ids -contains [int]$Matches[1] } | Measure CookedValue -Sum).Sum), 1) }) -join ' '`,
        )
      )
        .split(/\s+/)
        .map(Number);
      console.log(`GPU 3D each second: ${each.join(' ')}`);
      return Math.round((100 * each.reduce((a, b) => a + b, 0)) / each.length) / 100;
    };
    // Her script on a loop: each pass starts a new reply.
    let base = Date.now();
    let k = 0;
    let done = false;
    const loop = setInterval(() => {
      let t = (Date.now() - base) / 1000;
      if (t >= END_S + 1) {
        base = Date.now();
        k = 0;
        done = false;
        t = 0;
      }
      while (k < SCRIPT.length && t >= SCRIPT[k][0]) {
        const [, text, emotion, intensity, len] = SCRIPT[k];
        send({ type: 'segment-audio', index: k, seconds: len });
        send({ type: 'segment', text, emotion, intensity, gesture: null, subtitle: text, index: k });
        k += 1;
      }
      send({ type: 'mouth', level: Number(SCRIPT.reduce((m, [start, , , , len]) => Math.max(m, level(t, start, len)), 0).toFixed(3)) });
      if (!done && t >= END_S) {
        done = true;
        send({ type: 'subtitles-end', interrupted: false });
      }
    }, 20);
    await sleep(2000);
    const c0 = await cpu();
    const w0 = Date.now();
    const share = await gpu(30);
    const used = (await cpu()) - c0;
    clearInterval(loop);
    console.log(`speaking: GPU 3D ${share}%, CPU ${((100 * used) / ((Date.now() - w0) / 1000)).toFixed(1)}% of one core`);
    process.exit(0);
  }
  const frames = [];
  for (let i = 0; now() < TOTAL_S; i += 1) {
    const due = i / fps;
    while (now() < due) await sleep(4);
    const t = now();
    const tag = String(i).padStart(4, '0');
    const s = await shot('subtitles-capture', path.join(outDir, `s-${tag}.png`));
    await shot('capture', path.join(outDir, `o-${tag}.png`));
    frames.push({ i, t, subs: s ? s.bounds : null, her: s ? s.her : null });
  }
  clearInterval(voice);
  fs.writeFileSync(path.join(outDir, 'frames.json'), JSON.stringify(frames));
  console.log(`${frames.length} frames in ${now().toFixed(1)} s`);
} finally {
  send({ type: 'quit' });
  setTimeout(() => child.kill(), 3000);
}
