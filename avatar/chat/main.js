'use strict';
// Her chat window, in the look of her orb and panels. A separate Electron app on the avatar's Electron
// (`<electron> avatar/chat --config <file>`), driven over stdin/stdout by `octavia.core.chat.ChatLink`, which owns the
// conversation. This side only draws the window and passes on what the user does. See
// docs/octavia-architecture.md ("Her chat window on Electron").
//
// In: `line`, `status`, `ask`, `unask`, `show`, `draft`, `settings`, `day`, `found`, `panels`, `quit`.
// Out: `ready`, `say`, `answer`, `speak`, `history`, `search`, `panel`, `stop`, `dropped`, `closed`.
// Its stdin closing (she stopped) closes it. An ordinary window, so it may take the keyboard, unlike her panels.

const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, clipboard, ipcMain, nativeImage, screen, session, shell } = require('electron');
const { emit, log, readMessages } = require('../lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('../lib/scheme');

const SIZE = { full: { width: 500, height: 680 }, mini: { width: 460, height: 190 } };
const MIN = { full: { width: 360, height: 400 }, mini: { width: 340, height: 140 } };
const PAGE_MESSAGES = new Set(['say', 'answer', 'speak', 'history', 'search', 'panel', 'stop', 'dropped']);
const TOP_FOR_MS = 400; // in front of the window the user is in for this long, then an ordinary window
const TWEEN_MS = 200;
const TWEEN_STEP_MS = 16;
const MAX_COPY = 200000;

function loadConfig(argv) {
  const at = argv.indexOf('--config');
  const cfg = { name: 'Octavia', icon: null, statePath: null, hidden: false };
  if (at < 0 || !argv[at + 1]) return cfg;
  const raw = JSON.parse(fs.readFileSync(argv[at + 1], 'utf8'));
  if (typeof raw.name === 'string') cfg.name = raw.name.slice(0, 80);
  if (typeof raw.icon === 'string') cfg.icon = raw.icon;
  if (typeof raw.statePath === 'string') cfg.statePath = raw.statePath;
  cfg.hidden = raw.hidden === true; // never shown: previews and tests drive it and take pictures (`capture`)
  return cfg;
}

// Where the user left the window in each size, whether it's small and whether it stays on top. Anything unreadable
// or no longer on a screen counts as nothing kept.
function readState(statePath) {
  const state = { mini: false, top: false, full: null, small: null };
  if (!statePath) return state;
  try {
    const raw = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    state.mini = raw.mini === true;
    state.top = raw.top === true;
    state.full = visible(raw.full);
    state.small = visible(raw.small);
  } catch {
    // nothing kept yet
  }
  return state;
}

function visible(b) {
  if (!b || ![b.x, b.y, b.width, b.height].every(Number.isFinite)) return null;
  const area = screen.getDisplayMatching(b).workArea;
  const on = b.x < area.x + area.width - 80 && b.x + b.width > area.x + 80 && b.y >= area.y - 10 && b.y < area.y + area.height - 60;
  return on ? { x: b.x, y: b.y, width: b.width, height: b.height } : null;
}

let cfg;
try {
  cfg = loadConfig(process.argv.slice(1));
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}

let win = null;
let pageReady = false;
let state = null;
let tween = null;
let topTimer = null;
const waiting = []; // messages for the page that came before it was listening

function toPage(msg) {
  if (!win || win.isDestroyed()) return;
  if (!pageReady) waiting.push(msg);
  else win.webContents.send('chat:message', msg);
}

function saveState() {
  if (!cfg.statePath || cfg.hidden) return;
  try {
    fs.mkdirSync(path.dirname(cfg.statePath), { recursive: true });
    fs.writeFileSync(cfg.statePath, JSON.stringify(state));
  } catch {
    // only a convenience
  }
}

function keepBounds() {
  if (!win || win.isDestroyed() || win.isMaximized() || win.isMinimized() || tween) return;
  state[state.mini ? 'small' : 'full'] = win.getBounds();
}

// To the front, over the window the user is in, then an ordinary window again (unless they keep it on top).
function bringForward() {
  if (!win || win.isDestroyed() || cfg.hidden) return;
  if (win.isMinimized()) win.restore();
  win.setAlwaysOnTop(true);
  win.show();
  win.focus();
  clearTimeout(topTimer);
  topTimer = setTimeout(() => win && !win.isDestroyed() && win.setAlwaysOnTop(state.top), TOP_FOR_MS);
  toPage({ type: 'focus-box' });
}

const ease = (t) => 1 - (1 - t) ** 3;

// The window eases between its two sizes: its bottom-right corner stays put, as that's where the box the user is
// typing in sits, and it's kept on the screen. Never snaps.
function setMini(on) {
  if (!win || win.isDestroyed() || on === state.mini) return;
  keepBounds();
  saveState();
  state.mini = on;
  const from = win.getBounds();
  const size = (on ? state.small : state.full) || SIZE[on ? 'mini' : 'full'];
  const area = screen.getDisplayMatching(from).workArea;
  const remembered = on ? state.small : state.full;
  const to = {
    width: size.width,
    height: size.height,
    x: remembered ? remembered.x : Math.max(area.x, from.x + from.width - size.width),
    y: remembered ? remembered.y : Math.max(area.y, from.y + from.height - size.height),
  };
  win.setMinimumSize(MIN[on ? 'mini' : 'full'].width, MIN[on ? 'mini' : 'full'].height);
  clearInterval(tween);
  const began = Date.now();
  win.setMinimumSize(Math.min(MIN.mini.width, MIN.full.width), Math.min(MIN.mini.height, MIN.full.height));
  tween = setInterval(() => {
    if (!win || win.isDestroyed()) return clearInterval(tween);
    const t = Math.min(1, (Date.now() - began) / TWEEN_MS);
    const k = ease(t);
    const at = (a, b) => Math.round(a + (b - a) * k);
    win.setBounds({ x: at(from.x, to.x), y: at(from.y, to.y), width: at(from.width, to.width), height: at(from.height, to.height) });
    if (t >= 1) {
      clearInterval(tween);
      tween = null;
      win.setMinimumSize(MIN[on ? 'mini' : 'full'].width, MIN[on ? 'mini' : 'full'].height);
      saveState();
    }
  }, TWEEN_STEP_MS);
  toPage({ type: 'window', mini: on, top: state.top });
}

function dispatch(msg) {
  if (msg.type === 'quit') return app.quit();
  if (msg.type === 'show') return bringForward();
  if (msg.type === 'capture' && cfg.hidden) return capture(String(msg.path || ''));
  if (msg.type === 'script' && cfg.hidden && win) {
    // Hidden mode only (previews and tests): drive the page as the user would.
    return win.webContents
      .executeJavaScript(String(msg.code || ''))
      .then((result) => emit({ type: 'scripted', id: msg.id, result: result === undefined ? null : result }))
      .catch((err) => emit({ type: 'scripted', id: msg.id, error: String(err && err.message) }));
  }
  toPage(msg);
  if (msg.type === 'ask' || msg.type === 'draft') bringForward(); // she needs them here now, as the plain window did
}

async function capture(file) {
  if (!win || !file.toLowerCase().endsWith('.png')) return emit({ type: 'error', message: 'capture: no window or not a .png' });
  try {
    const image = await win.webContents.capturePage();
    fs.writeFileSync(file, image.toPNG());
    emit({ type: 'captured', path: file });
  } catch (err) {
    emit({ type: 'error', message: `capture failed: ${err.message}` });
  }
}

if (cfg) {
  registerScheme();
  app.whenReady().then(() => {
    handleScheme();
    // The window never needs the camera, microphone, notifications or anything else a page can ask for.
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    state = readState(cfg.statePath);
    const mini = state.mini && !cfg.hidden;
    if (cfg.hidden) state.mini = false;
    const bounds = (mini ? state.small : state.full) || SIZE[mini ? 'mini' : 'full'];
    win = new BrowserWindow({
      ...bounds,
      minWidth: MIN[mini ? 'mini' : 'full'].width,
      minHeight: MIN[mini ? 'mini' : 'full'].height,
      frame: false,
      show: false,
      backgroundColor: '#070b13',
      title: cfg.name,
      alwaysOnTop: state.top && !cfg.hidden,
      icon: cfg.icon ? nativeImage.createFromPath(cfg.icon) : undefined,
      webPreferences: {
        backgroundThrottling: !cfg.hidden,
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        spellcheck: true,
      },
    });
    win.setMenu(null);
    // Nothing leaves the window: no navigation, no new windows. A link she wrote opens only on the user's click.
    win.webContents.on('will-navigate', (event) => event.preventDefault());
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('console-message', (event) => {
      const { level, message, sourceId, lineNumber } = event;
      if (level === 'error' || level === 'warning') log(`chat ${level}: ${String(message).slice(0, 500)} (${sourceId}:${lineNumber})`);
    });
    win.once('ready-to-show', () => cfg.hidden || bringForward());
    win.on('maximize', () => toPage({ type: 'window', maximized: true }));
    win.on('unmaximize', () => toPage({ type: 'window', maximized: false }));
    win.on('close', () => {
      keepBounds();
      saveState();
    });
    win.on('closed', () => {
      win = null;
      clearInterval(tween);
      clearTimeout(topTimer);
      emit({ type: 'closed' });
      app.quit();
    });

    ipcMain.handle('chat:hello', () => {
      pageReady = true;
      return { name: cfg.name, mini: state.mini, top: state.top, pending: waiting.splice(0) };
    });
    ipcMain.on('chat:send', (_event, msg) => {
      if (msg && typeof msg === 'object' && PAGE_MESSAGES.has(msg.type)) emit(msg);
    });
    ipcMain.on('chat:copy', (_event, text) => clipboard.writeText(String(text).slice(0, MAX_COPY)));
    ipcMain.on('chat:open', (_event, url) => {
      if (/^https?:\/\//i.test(String(url))) shell.openExternal(String(url)).catch((err) => log(`open: ${err.message}`));
    });
    ipcMain.on('chat:window', (_event, what, on) => {
      if (!win) return;
      if (what === 'close') win.close();
      else if (what === 'minimize') win.minimize();
      else if (what === 'mini') setMini(on === true);
      else if (what === 'top') {
        state.top = on === true;
        win.setAlwaysOnTop(state.top);
        saveState();
      }
    });

    win.loadURL(`${ORIGIN}/renderer/chat/index.html`);
    emit({ type: 'ready' });
    readMessages(dispatch, () => app.quit()); // she's gone: so is her window
  });
  app.on('window-all-closed', () => app.quit());
}
