'use strict';
// OCTAVIA's guided setup ("forming OCTAVIA"): a window where each choice brings its orb a little more to life.
// A separate Electron app on the avatar's Electron (`<electron> avatar/welcome --config <file>`), driven over
// stdin/stdout by `octavia.platform.welcome_window`, which owns everything (devices, voice previews, saving). This
// side draws the window and passes on what the user does.
//
// In: `model` (defaults, presets, devices, this PC), `preview`, `key-checked`, `geocoded`, `saved`, `signed-in`,
// `quit`. Out: `ready`, `closed`, and from the page `preview` / `check-key` / `geocode` / `save` / `sign-in` / `done`.

const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, ipcMain, session, nativeImage } = require('electron');
const { emit, log, readMessages } = require('../lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('../lib/scheme');

const SIZE = { width: 1080, height: 720 };
const PAGE_MESSAGES = new Set(['preview', 'check-key', 'geocode', 'save', 'sign-in', 'done', 'open', 'check', 'say', 'stuck']);

function loadConfig(argv) {
  const at = argv.indexOf('--config');
  const cfg = { title: 'OCTAVIA', icon: null, hidden: false };
  if (at < 0 || !argv[at + 1]) return cfg;
  const raw = JSON.parse(fs.readFileSync(argv[at + 1], 'utf8'));
  if (typeof raw.title === 'string') cfg.title = raw.title.slice(0, 80);
  if (typeof raw.icon === 'string') cfg.icon = raw.icon;
  cfg.hidden = raw.hidden === true; // never shown: tests drive it and take pictures (`capture`)
  return cfg;
}

let cfg;
try {
  cfg = loadConfig(process.argv.slice(1));
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}
if (cfg && !cfg.hidden && !app.requestSingleInstanceLock()) {
  emit({ type: 'already-open' });
  app.exit(0);
  cfg = null;
}

let win = null;
let pageReady = false;
const waiting = [];

function toPage(msg) {
  if (!win || win.isDestroyed()) return;
  if (!pageReady) waiting.push(msg);
  else win.webContents.send('welcome:message', msg);
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

function dispatch(msg) {
  if (msg.type === 'quit') return app.quit();
  if (msg.type === 'capture' && cfg.hidden) return capture(String(msg.path || ''));
  if (msg.type === 'script' && cfg.hidden && win) {
    return win.webContents
      .executeJavaScript(String(msg.code || ''))
      .then((result) => emit({ type: 'scripted', id: msg.id, result: result === undefined ? null : result }))
      .catch((err) => emit({ type: 'scripted', id: msg.id, error: String(err && err.message) }));
  }
  toPage(msg);
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
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    win = new BrowserWindow({
      ...SIZE,
      resizable: false,
      maximizable: false,
      frame: false,
      show: false,
      backgroundColor: '#05080f',
      title: cfg.title,
      icon: cfg.icon ? nativeImage.createFromPath(cfg.icon) : undefined,
      webPreferences: {
        backgroundThrottling: !cfg.hidden,
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        spellcheck: false,
        autoplayPolicy: 'no-user-gesture-required',
      },
    });
    win.setMenu(null);
    win.webContents.on('will-navigate', (event) => event.preventDefault());
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('console-message', (event) => {
      const { level, message, sourceId, lineNumber } = event;
      if (level === 'error' || level === 'warning') log(`welcome ${level}: ${String(message).slice(0, 500)} (${sourceId}:${lineNumber})`);
    });
    win.once('ready-to-show', () => {
      if (cfg.hidden) return;
      win.show();
      win.focus();
    });
    win.on('closed', () => {
      win = null;
      emit({ type: 'closed' });
      app.quit();
    });
    ipcMain.handle('welcome:hello', () => {
      pageReady = true;
      return { title: cfg.title, pending: waiting.splice(0) };
    });
    ipcMain.on('welcome:send', (_event, msg) => {
      if (msg && typeof msg === 'object' && PAGE_MESSAGES.has(msg.type)) emit(msg);
    });
    ipcMain.on('welcome:window', (_event, what) => {
      if (!win) return;
      if (what === 'close') win.close();
      else if (what === 'minimize') win.minimize();
    });
    win.loadURL(`${ORIGIN}/renderer/welcome/index.html`);
    emit({ type: 'ready' });
    readMessages(dispatch, () => app.quit());
  });
  app.on('window-all-closed', () => app.quit());
}
