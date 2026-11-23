'use strict';
// Her settings window, in the look of her orb and panels. A separate Electron app on the avatar's Electron
// (`<electron> avatar/settings --config <file>`), driven over stdin/stdout by `octavia.platform.settings_window`,
// which owns everything about settings: what there is, checking a change and keeping it. This side only draws the
// window and passes on what the user does. See docs/octavia-architecture.md ("Settings window").
//
// In: `model` (every section and setting, once), `saved` / `refused` (a change kept or not), `status` (what waits for
// a restart, a snooze, the usage meters), `secret` (a key revealed), `quit`.
// Out: `ready`, `already-open` (another settings window is up: that one is brought forward and this one quits),
// `set` / `reset` / `reveal` / `restart` / `action` / `search` (from the page), `closed`.

const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, ipcMain, session, nativeImage, screen } = require('electron');
const { emit, log, readMessages } = require('../lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('../lib/scheme');

const SIZE = { width: 1120, height: 760 };
const MIN = { width: 860, height: 560 };
const PAGE_MESSAGES = new Set(['set', 'reset', 'reveal', 'restart', 'action', 'search']);

function loadConfig(argv) {
  const at = argv.indexOf('--config');
  const cfg = { title: 'Settings', icon: null, statePath: null, hidden: false };
  if (at < 0 || !argv[at + 1]) return cfg;
  const raw = JSON.parse(fs.readFileSync(argv[at + 1], 'utf8'));
  if (typeof raw.title === 'string') cfg.title = raw.title.slice(0, 80);
  if (typeof raw.icon === 'string') cfg.icon = raw.icon;
  if (typeof raw.statePath === 'string') cfg.statePath = raw.statePath;
  cfg.hidden = raw.hidden === true; // never shown: previews and tests drive it and take pictures (`capture`)
  return cfg;
}

// Where the user last left the window, and how big: kept for next time, when it's still on a screen.
function readBounds(statePath) {
  try {
    const b = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    if (![b.x, b.y, b.width, b.height].every(Number.isFinite)) return null;
    const area = screen.getDisplayMatching(b).workArea;
    const visible = b.x < area.x + area.width - 80 && b.x + b.width > area.x + 80 && b.y >= area.y - 10 && b.y < area.y + area.height - 80;
    if (!visible) return null;
    return { x: b.x, y: b.y, width: Math.max(MIN.width, b.width), height: Math.max(MIN.height, b.height) };
  } catch {
    return null;
  }
}

function writeBounds(statePath, win) {
  if (!statePath || win.isDestroyed() || win.isMaximized() || win.isMinimized()) return;
  try {
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify(win.getBounds()));
  } catch {
    // only a convenience
  }
}

let cfg;
try {
  cfg = loadConfig(process.argv.slice(1));
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}

// One settings window at a time: a second one brings the first forward instead (each app on this Electron has its
// own lock, by its package name).
if (cfg && !cfg.hidden && !app.requestSingleInstanceLock()) {
  emit({ type: 'already-open' });
  app.exit(0);
  cfg = null;
}

let win = null;
let pageReady = false;
const waiting = []; // messages for the page that came before it was listening

function toPage(msg) {
  if (!win || win.isDestroyed()) return;
  if (!pageReady) waiting.push(msg);
  else win.webContents.send('settings:message', msg);
}

function dispatch(msg) {
  if (msg.type === 'quit') return app.quit();
  if (msg.type === 'capture' && cfg.hidden) return capture(String(msg.path || ''));
  if (msg.type === 'script' && cfg.hidden && win) {
    // Hidden mode only (previews and tests): drive the page as the user would, through its window.settingsPage.
    return win.webContents
      .executeJavaScript(String(msg.code || ''))
      .then((result) => emit({ type: 'scripted', id: msg.id, result: result === undefined ? null : result }))
      .catch((err) => emit({ type: 'scripted', id: msg.id, error: String(err && err.message) }));
  }
  toPage(msg);
}

// A picture of the page (hidden mode only), once it has had a moment to settle.
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
  app.on('second-instance', () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });
  app.whenReady().then(() => {
    handleScheme();
    // The window never needs the camera, microphone, notifications or anything else a page can ask for.
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    const bounds = (!cfg.hidden && cfg.statePath && readBounds(cfg.statePath)) || SIZE;
    win = new BrowserWindow({
      ...bounds,
      minWidth: MIN.width,
      minHeight: MIN.height,
      frame: false,
      show: false,
      backgroundColor: '#070b13',
      title: cfg.title,
      icon: cfg.icon ? nativeImage.createFromPath(cfg.icon) : undefined,
      webPreferences: {
        backgroundThrottling: !cfg.hidden,
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        spellcheck: false,
      },
    });
    win.setMenu(null);
    // Nothing leaves the window: no navigation, no new windows.
    win.webContents.on('will-navigate', (event) => event.preventDefault());
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('console-message', (event) => {
      const { level, message, sourceId, lineNumber } = event;
      if (level === 'error' || level === 'warning') log(`settings ${level}: ${String(message).slice(0, 500)} (${sourceId}:${lineNumber})`);
    });
    win.once('ready-to-show', () => {
      if (cfg.hidden) return;
      win.show();
      win.focus();
    });
    win.on('maximize', () => toPage({ type: 'window', maximized: true }));
    win.on('unmaximize', () => toPage({ type: 'window', maximized: false }));
    win.on('close', () => cfg.hidden || writeBounds(cfg.statePath, win));
    win.on('closed', () => {
      win = null;
      emit({ type: 'closed' });
      app.quit();
    });

    ipcMain.handle('settings:hello', () => {
      pageReady = true;
      const pending = waiting.splice(0);
      return { title: cfg.title, pending };
    });
    ipcMain.on('settings:send', (_event, msg) => {
      if (msg && typeof msg === 'object' && PAGE_MESSAGES.has(msg.type)) emit(msg);
    });
    ipcMain.on('settings:window', (_event, what) => {
      if (!win) return;
      if (what === 'close') win.close();
      else if (what === 'minimize') win.minimize();
      else if (what === 'maximize') (win.isMaximized() ? win.unmaximize() : win.maximize());
    });

    win.loadURL(`${ORIGIN}/renderer/settings/index.html`);
    emit({ type: 'ready' });
    readMessages(dispatch, () => app.quit()); // the settings process gone: so is its window
  });
  app.on('window-all-closed', () => app.quit());
}
