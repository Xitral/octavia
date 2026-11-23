'use strict';
// Octavia's pop-up panels: diagrams, charts and formatted notes she shows while she explains something. A separate
// Electron app on the avatar's Electron (`<electron> avatar/panels --config <file>`), so it works with the avatar
// off and a panel crashing can't take her character with it. Driven over stdin/stdout; see docs/visuals.md.

const fs = require('node:fs');
const path = require('node:path');
const { app, ipcMain, session, shell, BrowserWindow, WebContentsView } = require('electron');
const { emit, log, readMessages } = require('../lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('../lib/scheme');
const { Panels } = require('./panels');
const { Guard, webSession } = require('./web');

const DEFAULTS = { hideFromCapture: true, maxPanels: 3, statePath: null, debug: false, imagesDir: null, offscreen: false, webAllowLocal: false };
const IMAGE_NAME = /^img-[0-9a-f]{32}\.(jpg|png)$/; // the pictures Octavia cleaned (src/octavia/visuals/web_images.py)

function loadConfig(argv) {
  const at = argv.indexOf('--config');
  const cfg = { ...DEFAULTS };
  if (at < 0 || !argv[at + 1]) return cfg;
  const raw = JSON.parse(fs.readFileSync(argv[at + 1], 'utf8'));
  if (typeof raw.hideFromCapture === 'boolean') cfg.hideFromCapture = raw.hideFromCapture;
  if (Number.isInteger(raw.maxPanels)) cfg.maxPanels = Math.min(Math.max(raw.maxPanels, 1), 6);
  if (typeof raw.statePath === 'string') cfg.statePath = raw.statePath;
  cfg.debug = raw.debug === true;
  if (typeof raw.imagesDir === 'string' && raw.imagesDir) cfg.imagesDir = raw.imagesDir;
  // Checks only (with debug): panels and pages drawn off screen and never shown; pages from a test server here.
  cfg.offscreen = cfg.debug && raw.offscreen === true;
  cfg.webAllowLocal = cfg.debug && raw.webAllowLocal === true;
  return cfg;
}

let cfg;
try {
  cfg = loadConfig(process.argv.slice(1));
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}

registerScheme();
let panels = null;

function dispatch(msg) {
  if (cfg.debug) log(`<- ${JSON.stringify(msg).slice(0, 300)}`);
  if (msg.type === 'quit') return app.quit();
  if (!panels) return;
  if (msg.type === 'show') panels.show(msg);
  else if (msg.type === 'close') {
    const by = 'octavia';
    if (msg.id == null) panels.closeAll(by);
    else if (!panels.close(String(msg.id), by)) emit({ type: 'closed', id: String(msg.id), by: 'gone' });
  } else if (msg.type === 'pin') {
    panels.pin(String(msg.id), msg.on === true, Number(msg.stay_s));
  } else if (msg.type === 'tuck') {
    panels.tuck(msg.on === true);
  } else if (msg.type === 'focus-from') {
    panels.focusFrom(String(msg.id), Number(msg.previous));
  } else if (msg.type === 'snapshot') {
    panels.snapshot(String(msg.id));
  } else if (msg.type === 'input' && cfg.debug) {
    panels.input(String(msg.id), Array.isArray(msg.events) ? msg.events.slice(0, 200) : []); // debug: the demo's pointer
  } else if (msg.type === 'capture') {
    panels
      .capture(String(msg.id), String(msg.path))
      .then(() => emit({ type: 'captured', id: msg.id, path: msg.path }))
      .catch((err) => emit({ type: 'error', id: msg.id, message: `capture failed: ${err.message}` }));
  }
}

// The view a web page goes in: a WebContentsView inside the panel's window (rounded, on her dark glass while it
// loads); in checks, an off-screen window of its own that's never shown.
function makeView(webPreferences) {
  if (cfg.offscreen) {
    const win = new BrowserWindow({ show: false, width: 640, height: 480, webPreferences: { ...webPreferences, offscreen: true } });
    return {
      webContents: win.webContents,
      attach() {},
      detach() {},
      setBounds(b) {
        if (!win.isDestroyed()) win.setContentSize(Math.max(1, b.width), Math.max(1, b.height));
      },
      setVisible() {},
      destroy() {
        if (!win.isDestroyed()) win.destroy();
      },
    };
  }
  const view = new WebContentsView({ webPreferences });
  view.setBackgroundColor('#0b1626');
  if (typeof view.setBorderRadius === 'function') view.setBorderRadius(4);
  return {
    webContents: view.webContents,
    attach: (win) => win.contentView.addChildView(view),
    detach: (win) => win.contentView.removeChildView(view),
    setBounds: (b) => view.setBounds(b),
    setVisible: (on) => view.setVisible(on),
    destroy() {},
  };
}

if (cfg) {
  app.whenReady().then(() => {
    handleScheme([['images/', cfg.imagesDir, IMAGE_NAME]]); // her cleaned pictures, and nothing else in data/
    // A panel never needs the camera, microphone, notifications or anything else a page can ask for.
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    // Nothing on a panel's own page goes to the network (its pictures are served here, web pages have a session of
    // their own): a proxy that goes nowhere means a live block's program can't get out by WebRTC either.
    session.defaultSession.setProxy({ proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '' }).catch((err) => log(`proxy: ${err.message}`));
    const guard = new Guard({ allowLocal: cfg.webAllowLocal });
    webSession(session, guard);
    panels = new Panels(cfg, {
      url: `${ORIGIN}/renderer/panel/index.html`,
      preload: path.join(__dirname, 'preload.js'),
      emit,
      log,
      web: { make: makeView, guard, openExternal: (url) => shell.openExternal(url).catch((err) => log(`open: ${err.message}`)) },
    });
    ipcMain.on('panel:web', (event, rect) => panels.webRect(event.sender, rect && typeof rect === 'object' ? rect : null));
    ipcMain.on('panel:open', (event, which) => panels.openSource(event.sender, String(which || '')));
    ipcMain.handle('panel:hello', (event) => panels.hello(event.sender));
    ipcMain.on('panel:size', (event, height, notes, refit) => panels.sized(event.sender, height, notes, refit === true));
    ipcMain.on('panel:close', (event, by) => {
      const entry = panels.bySender(event.sender);
      if (entry) panels.close(entry.id, by === 'timer' ? 'timer' : 'user');
    });
    ipcMain.on('panel:folded', (event) => panels.folded(event.sender));
    ipcMain.on('panel:pin', (event, on) => panels.pinAsked(event.sender, on === true));
    ipcMain.on('panel:mouse', (event, over) => panels.mouse(event.sender, over === true));
    ipcMain.on('panel:tick', (event) => panels.tick(event.sender));
    ipcMain.on('panel:typing', (event, on, restore) => panels.typing(event.sender, on === true, restore === true));
    ipcMain.on('panel:sent', (event, values, field, untouched, choice) => panels.sent(event.sender, values, field, untouched, choice === true));
    ipcMain.on('panel:ink', (event, strokes) => panels.inked(event.sender, Number(strokes)));
    ipcMain.on('panel:layout', (event, boxes) => panels.laidOut(event.sender, boxes));
    ipcMain.on('panel:live', (event, address, report) => panels.live(event.sender, String(address || ''), report));
    ipcMain.on('panel:control', (event, action, step) => panels.control(event.sender, String(action || ''), step));
    ipcMain.on('panel:pointer', (event, rect) => panels.pointer(event.sender, rect && typeof rect === 'object' ? rect : null));
    ipcMain.on('panel:resize', (event, phase, edge) =>
      phase === 'start' ? panels.startResize(event.sender, String(edge)) : panels.endResize(event.sender),
    );
    ipcMain.on('panel:drag', (event, phase) => (phase === 'start' ? panels.startDrag(event.sender) : panels.endDrag(event.sender)));
    app.on('web-contents-created', (_e, wc) => {
      wc.on('console-message', (event) => {
        const { level, message, sourceId, lineNumber } = event;
        if (level === 'error' || level === 'warning' || cfg.debug) log(`panel ${level}: ${String(message).slice(0, 500)} (${sourceId}:${lineNumber})`);
      });
    });
    emit({ type: 'ready' });
    readMessages(dispatch, () => app.quit()); // Octavia gone: so are her panels
  });
  // Panels come and go; the app stays until Octavia says quit or goes away.
  app.on('window-all-closed', () => {});
  app.on('before-quit', () => panels && panels.destroyAll());
}
