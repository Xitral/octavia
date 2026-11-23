'use strict';
// Octavia's marks on the user's real screen: arrows, circles, boxes, highlights and labels she draws over whatever is
// there to guide them ("click here, then this field"). A separate Electron app on the avatar's Electron
// (`<electron> avatar/marks --config <file>`), so it works with the avatar off. Driven over stdin/stdout; see
// docs/screen-marks.md.

const fs = require('node:fs');
const path = require('node:path');
const { app, ipcMain, session } = require('electron');
const { emit, log, readMessages } = require('../lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('../lib/scheme');
const { Overlay } = require('./overlay');

const DEFAULTS = { hideFromCapture: true, debug: false };

function loadConfig(argv) {
  const at = argv.indexOf('--config');
  const cfg = { ...DEFAULTS };
  if (at < 0 || !argv[at + 1]) return cfg;
  const raw = JSON.parse(fs.readFileSync(argv[at + 1], 'utf8'));
  if (typeof raw.hideFromCapture === 'boolean') cfg.hideFromCapture = raw.hideFromCapture;
  cfg.debug = raw.debug === true;
  return cfg;
}

let cfg;
try {
  cfg = loadConfig(process.argv.slice(1));
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}

// An error in the main process would otherwise pop Electron's error dialog onto the user's desktop: it's told to
// Octavia and logged instead, and the marks carry on.
process.on('uncaughtException', (err) => {
  log(`marks: ${err && err.stack ? err.stack : err}`);
  emit({ type: 'error', message: `marks app error: ${err && err.message ? err.message : err}` });
});

registerScheme();
let overlay = null;

function dispatch(msg) {
  if (cfg.debug) log(`<- ${JSON.stringify(msg).slice(0, 300)}`);
  if (msg.type === 'quit') return app.quit();
  if (!overlay) return;
  if (msg.type === 'draw') overlay.draw(msg);
  else if (msg.type === 'clear') {
    if (!overlay.clear(msg.by === 'user' ? 'user' : 'octavia')) emit({ type: 'cleared', id: null, by: 'gone' });
  } else if (msg.type === 'capture') {
    overlay
      .capture(Number.isInteger(msg.display) ? msg.display : null, String(msg.path))
      .then(() => emit({ type: 'captured', path: msg.path }))
      .catch((err) => emit({ type: 'error', message: `capture failed: ${err.message}` }));
  }
}

if (cfg) {
  app.whenReady().then(() => {
    handleScheme();
    // A mark never needs the camera, microphone, notifications or anything else a page can ask for.
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    overlay = new Overlay(cfg, {
      url: `${ORIGIN}/renderer/marks/index.html`,
      preload: path.join(__dirname, 'preload.js'),
      emit,
      log,
    });
    ipcMain.handle('marks:hello', (event) => overlay.hello(event.sender));
    ipcMain.on('marks:painted', (event, id) => overlay.painted(event.sender, String(id)));
    app.on('web-contents-created', (_e, wc) => {
      wc.on('console-message', (event) => {
        const { level, message, sourceId, lineNumber } = event;
        if (level === 'error' || level === 'warning' || cfg.debug) log(`marks ${level}: ${String(message).slice(0, 500)} (${sourceId}:${lineNumber})`);
      });
    });
    emit({ type: 'ready' });
    readMessages(dispatch, () => app.quit()); // Octavia gone: so are her marks
  });
  app.on('window-all-closed', () => {});
  app.on('before-quit', () => overlay && overlay.destroy());
}
