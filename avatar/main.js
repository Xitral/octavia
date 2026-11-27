'use strict';
// Octavia's desktop avatar. Octavia starts this as a child process and drives it over stdin/stdout;
// see docs/avatar-protocol.md.

const fs = require('node:fs/promises');
const path = require('node:path');
const { app, ipcMain, screen, session } = require('electron');
const { parseArgs, loadConfig, propsForRenderer } = require('./lib/config');
const { emit, log, readMessages } = require('./lib/protocol');
const { ORIGIN, registerScheme, handleScheme } = require('./lib/scheme');
const { AvatarWindow } = require('./lib/window');
const { Choreographer } = require('./lib/choreography');
const { Trails } = require('./lib/trail');
const { Subtitles } = require('./lib/subtitles');
const { Whereabouts } = require('./lib/where');
const { startPreview } = require('./lib/preview');

// A prop is a small model; anything bigger is a mistake. Octavia's scan leaves out a bigger file first
// (_PROP_BYTES_MAX in src/octavia/avatar/bridge.py, which a test keeps equal to this).
const MAX_PROP_BYTES = 64 * 1024 * 1024;

const args = parseArgs(process.argv.slice(1));
let cfg;
try {
  cfg = loadConfig(args);
} catch (err) {
  emit({ type: 'error', message: `bad config: ${err.message}` });
  app.exit(1);
}

registerScheme();

let avatar = null;
let choreo = null;
let trails = null;
let subtitles = null;
let whereabouts = null; // where she is across the screen, for her voice to come from there
let ready = false;
const pending = []; // messages that arrived before the model finished loading
// What her subtitles hear: lines and their end, her feelings and voice so the words move with her, and her leaving
// (they fade with her).
const SUBTITLED = new Set(['segment', 'segment-audio', 'segment-word', 'subtitles-end', 'emotion', 'mood', 'mouth', 'hide']);
let stopPreview = null;

const debug = (...parts) => cfg && cfg.debug && log(...parts);

// Everything Octavia (or the preview) says goes through here.
function dispatch(msg) {
  if (!avatar) return;
  if (msg.type !== 'mouth') debug(`<- ${JSON.stringify(msg)}`);
  if (msg.type === 'quit') {
    app.quit();
    return;
  }
  // Her subtitles need only her window, not her model: they go up as the sentence starts, ready or not.
  if (subtitles && SUBTITLED.has(msg.type) && !msg.subtitled) subtitles.handle(msg);
  if (msg.type === 'subtitles-end' || msg.type === 'segment-audio' || msg.type === 'segment-word') return; // only the subtitles' business
  if (msg.type === 'subtitles-capture') {
    const file = msg.path;
    (subtitles ? subtitles.capture(file) : Promise.reject(new Error('no subtitles')))
      .then(() => emit({ type: 'captured', path: file, bounds: subtitles.win.getBounds(), her: avatar.bounds }))
      .catch((err) => emit({ type: 'error', message: `capture failed: ${err.message}` }));
    return;
  }
  if (msg.type === 'capture') {
    const file = msg.path;
    avatar
      .capture(file)
      .then(() => emit({ type: 'captured', path: file }))
      .catch((err) => emit({ type: 'error', message: `capture failed: ${err.message}` }));
    return;
  }
  if (!ready) {
    pending.push(SUBTITLED.has(msg.type) ? { ...msg, subtitled: true } : msg); // the subtitles had it already
    return;
  }
  // Showing, hiding, flying and pointing are choreographed there; everything else passes through in order.
  choreo.handle(msg);
}

// Messages from the renderer ('where' comes from main itself: lib/where.js). Only 'ready', 'hidden' (via the choreographer), 'shutter', 'hover' and 'error' reach Octavia; the
// renderer's reports on its own animations ('zipped-out', 'arrived', 'metrics') are for main alone.
function onRendererEmit(msg) {
  if (!msg || typeof msg.type !== 'string') return;
  if (msg.type === 'ready') {
    if (ready) return;
    ready = true;
    emit({ type: 'ready' });
    pending.splice(0).forEach(dispatch);
    trails.start();
    whereabouts.start();
    if (args.preview) stopPreview = startPreview(dispatch, { spot: previewSpot });
  } else if (msg.type === 'error') {
    emit({ type: 'error', message: String(msg.message) });
  } else if (msg.type === 'shutter') {
    emit({ type: 'shutter' }); // the camera's shutter pressed just now: Octavia plays its sound
  } else if (msg.type === 'hover') {
    emit({ type: 'hover', over: msg.over === true }); // Octavia watches Ctrl while the pointer is on her
  } else {
    choreo.fromRenderer(msg);
  }
}

// Spots for the preview to point at, in physical pixels like Octavia's: the middle of the monitor she lives
// on, or wherever the mouse is.
function previewSpot(which) {
  const dip =
    which === 'cursor'
      ? screen.getCursorScreenPoint()
      : (() => {
          const { bounds } = screen.getDisplayMatching(choreo.homeBounds());
          return { x: Math.round(bounds.x + bounds.width / 2), y: Math.round(bounds.y + bounds.height / 2) };
        })();
  return typeof screen.dipToScreenPoint === 'function' ? screen.dipToScreenPoint(dip) : dip;
}

function fromAvatar(event) {
  return avatar && event.sender === avatar.webContents;
}

function wireIpc() {
  ipcMain.handle('avatar:init', (event) => {
    if (!fromAvatar(event)) return null;
    return {
      framing: cfg.framing,
      credit: cfg.credit,
      props: propsForRenderer(cfg.props),
      activityProps: cfg.activityProps,
      previewProp: cfg.previewProp, // Octavia sets it only for `octavia avatar preview --prop`
      trail: cfg.trail,
      hover: cfg.hover,
      napAfterS: cfg.napAfterS,
      form: cfg.form,
    };
  });
  // The renderer gets the model's bytes, never a path it could use to read other files.
  ipcMain.handle('avatar:load-model', async (event) => {
    if (!fromAvatar(event)) return null;
    return fs.readFile(cfg.model);
  });
  // Likewise a prop's: only the file configured for a prop of that name, asked for by name. Null when it can't be
  // read. A file too big is refused with the reason instead, which the renderer passes on to the user. Octavia's
  // scan leaves such a file out, so this only catches one that grew after it looked, or a config written by hand.
  ipcMain.handle('avatar:load-prop', async (event, name) => {
    if (!fromAvatar(event)) return null;
    const prop = typeof name === 'string' && Object.hasOwn(cfg.props, name) ? cfg.props[name] : null;
    if (!prop || !prop.file) return null;
    let size;
    try {
      ({ size } = await fs.stat(prop.file));
      if (size <= MAX_PROP_BYTES) return await fs.readFile(prop.file);
    } catch (err) {
      log(`can't load prop ${name}: ${err.message}`);
      return null;
    }
    const mb = 1024 * 1024; // as Windows counts them, like the scan's message
    throw new Error(`it's too big (${Math.ceil(size / mb)} MB; a prop can be at most ${MAX_PROP_BYTES / mb} MB)`);
  });
  ipcMain.on('avatar:emit', (event, msg) => fromAvatar(event) && onRendererEmit(msg));
  ipcMain.on('avatar:ignore-mouse', (event, ignore) => fromAvatar(event) && avatar.setIgnoreMouse(ignore === true));
  ipcMain.on('avatar:drag', (event, phase) => {
    if (!fromAvatar(event)) return;
    if (phase === 'start') avatar.startDrag();
    else if (phase === 'end') avatar.endDrag();
  });
  ipcMain.on('subtitles:hello', (event) => subtitles && subtitles.hello(event.sender));
  ipcMain.on('subtitles:faded', (event, seq) => subtitles && subtitles.faded(event.sender, seq));
  ipcMain.on('trail:done', (event, id) => trails && trails.owns(event.sender) && trails.done(event.sender, id));
}

if (cfg) {
  app.whenReady().then(() => {
    handleScheme();
    wireIpc();
    // The avatar never needs the camera, microphone, notifications or anything else a page can ask for.
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    for (const warning of cfg.warnings) log(`config: ${warning}`);
    avatar = new AvatarWindow(cfg, {
      url: `${ORIGIN}/renderer/index.html`,
      preload: path.join(__dirname, 'preload.js'),
      onEmit: emit,
      log,
    });
    trails = new Trails(cfg, {
      url: `${ORIGIN}/renderer/trail.html`,
      preload: path.join(__dirname, 'trail-preload.js'),
      log,
      debug,
    });
    choreo = new Choreographer({ cfg, avatar, trails, screen, emit, log, debug });
    subtitles = new Subtitles(cfg, {
      avatar,
      herBody: () => choreo.bodyBounds(),
      pointedAt: () => choreo.pointedAt(),
      screen,
      url: `${ORIGIN}/renderer/subtitles/index.html`,
      preload: path.join(__dirname, 'subtitle-preload.js'),
      log,
    });
    const wc = avatar.webContents;
    wc.on('console-message', (event) => {
      const { level, message, sourceId, lineNumber } = event;
      if (level === 'error' || level === 'warning' || cfg.debug) log(`renderer ${level}: ${message} (${sourceId}:${lineNumber})`);
    });
    wc.on('render-process-gone', (_e, details) => {
      emit({ type: 'error', message: `renderer gone: ${details.reason}` });
      app.exit(1);
    });
    wc.on('preload-error', (_e, file, err) => emit({ type: 'error', message: `preload failed: ${err.message}` }));
    whereabouts = new Whereabouts({
      herRect: () => (avatar.atHome !== false ? avatar.bounds : choreo.bodyBounds()),
      screen,
      emit,
    });
    debug(`config: ${JSON.stringify(cfg)}`);

    // Octavia closing our stdin means it is gone, so we go too. The preview has no one on the other end.
    readMessages(dispatch, () => {
      if (!args.preview) app.quit();
    });
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => {
    if (stopPreview) stopPreview();
    if (whereabouts) whereabouts.stop();
    if (choreo) choreo.close();
    if (trails) trails.destroy();
    if (subtitles) subtitles.destroy();
    if (avatar) avatar.destroy();
  });
}
