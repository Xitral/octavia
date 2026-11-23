'use strict';
// The rainbow streak she leaves across the screen when she flies somewhere, and the ring of a sonic rainboom
// racing out across it. Her own window only covers her, so they're drawn by one transparent, click-through
// window per monitor, each shown only while something crosses it (renderer/trail.html). They're made hidden a
// little after she's ready, so they don't slow her start, and remade when monitors come, go or change.

const { BrowserWindow, screen } = require('electron');
const { segmentCrossesRect } = require('./layout');

const BUILD_DELAY_MS = 3000;
const REBUILD_DELAY_MS = 500; // display changes come in bursts
const TAIL_FACTOR = 1.4; // the tail catches up with the head this long after the head started (x duration)
const DONE_GRACE_MS = 400; // a trail window that never says it's done is hidden this long after it should be

class Trails {
  constructor(cfg, { url, preload, log, debug }) {
    this.cfg = cfg;
    this.url = url;
    this.preload = preload;
    this.log = log;
    this.debug = debug;
    this.windows = [];
    this.nextId = 1;
    this.buildTimer = null;
    this.closed = false;
    this.onDisplaysChanged = (_event, _display, changed) => {
      // A work area change (the taskbar hiding itself, say) doesn't move a monitor.
      if (Array.isArray(changed) && changed.every((what) => what === 'workArea')) return;
      this.scheduleBuild(REBUILD_DELAY_MS);
    };
  }

  // Call once she's ready.
  start() {
    if (!this.cfg.trail || this.closed) return;
    this.scheduleBuild(BUILD_DELAY_MS);
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) screen.on(event, this.onDisplaysChanged);
  }

  scheduleBuild(ms) {
    clearTimeout(this.buildTimer);
    this.buildTimer = setTimeout(() => this.build(), ms);
  }

  build() {
    clearTimeout(this.buildTimer);
    this.buildTimer = null;
    if (this.closed) return;
    this.destroyWindows();
    for (const display of screen.getAllDisplays()) {
      try {
        this.windows.push(this.makeWindow(display));
      } catch (err) {
        this.log(`could not make a trail window: ${err.message}`);
      }
    }
    this.debug(`trail windows: ${this.windows.map((t) => JSON.stringify(t.bounds)).join(' ')}`);
  }

  makeWindow(display) {
    // One pixel short of the monitor: a topmost window covering a whole monitor counts as a full-screen app
    // to Windows (the taskbar steps back, notifications go quiet), even a see-through one.
    const bounds = { ...display.bounds, height: display.bounds.height - 1 };
    const win = new BrowserWindow({
      ...bounds,
      show: false,
      title: 'trail',
      transparent: true,
      frame: false,
      backgroundColor: '#00000000',
      hasShadow: false,
      resizable: false,
      movable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      focusable: false,
      alwaysOnTop: true,
      webPreferences: {
        preload: this.preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,
        spellcheck: false,
      },
    });
    win.setAlwaysOnTop(true, 'screen-saver');
    if (this.cfg.hideFromCapture) win.setContentProtection(true);
    // Fully click-through: nothing it draws may ever get in the way of the mouse.
    win.setIgnoreMouseEvents(true);
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    win.webContents.on('render-process-gone', () => this.scheduleBuild(REBUILD_DELAY_MS));
    const trail = { win, bounds, loaded: false, drawing: 0, hideTimer: null };
    win.webContents.once('did-finish-load', () => {
      trail.loaded = true;
    });
    win.loadURL(this.url).catch((err) => this.log(`trail window failed to load: ${err.message}`));
    return trail;
  }

  // Draws a streak from `from` to `to` (DIP, screen) whose head takes `durationMs` to get there, `thickness`
  // px wide. Returns whether any window is drawing it.
  draw(from, to, durationMs, thickness) {
    if (!this.cfg.trail || this.closed) return false;
    if (!this.windows.length) this.build(); // asked for before they were made: this one has to go without
    const id = this.nextId++;
    const shown = [];
    for (const trail of this.windows) {
      if (!trail.loaded || trail.win.isDestroyed()) continue;
      if (!segmentCrossesRect(from, to, trail.bounds, thickness)) continue;
      const o = trail.bounds;
      trail.win.webContents.send('trail:draw', {
        id,
        from: { x: from.x - o.x, y: from.y - o.y },
        to: { x: to.x - o.x, y: to.y - o.y },
        duration: durationMs,
        life: durationMs * TAIL_FACTOR,
        thickness,
        ...this.palette(),
      });
      trail.drawing = id;
      if (!trail.win.isVisible()) trail.win.showInactive();
      trail.win.setAlwaysOnTop(true, 'screen-saver');
      clearTimeout(trail.hideTimer);
      trail.hideTimer = setTimeout(() => this.hide(trail, id, 'timed out'), durationMs * TAIL_FACTOR + DONE_GRACE_MS);
      shown.push(JSON.stringify(o));
    }
    if (shown.length) this.debug(`trail ${id}: ${JSON.stringify(from)} -> ${JSON.stringify(to)} in ${Math.round(durationMs)} ms on ${shown.join(' ')}`);
    return shown.length > 0;
  }

  // A sonic rainboom going off at `at` (DIP, screen): a ring racing out `reach` px in `durationMs`, drawn on every
  // monitor it gets to. Returns whether any window is drawing it.
  boom(at, reach, durationMs) {
    if (!this.cfg.trail || this.closed) return false;
    if (!this.windows.length) this.build();
    const id = this.nextId++;
    let shown = 0;
    for (const trail of this.windows) {
      if (!trail.loaded || trail.win.isDestroyed()) continue;
      if (!circleCrossesRect(at, reach, trail.bounds)) continue;
      const o = trail.bounds;
      trail.win.webContents.send('trail:boom', { id, at: { x: at.x - o.x, y: at.y - o.y }, reach, duration: durationMs, ...this.palette() });
      trail.drawing = id;
      if (!trail.win.isVisible()) trail.win.showInactive();
      trail.win.setAlwaysOnTop(true, 'screen-saver');
      clearTimeout(trail.hideTimer);
      trail.hideTimer = setTimeout(() => this.hide(trail, id, 'timed out'), durationMs + DONE_GRACE_MS);
      shown++;
    }
    if (shown) this.debug(`rainboom ${id} at ${JSON.stringify(at)}, ${Math.round(reach)} px, on ${shown} monitor(s)`);
    return shown > 0;
  }

  // The orb leaves an electric blue light trail rather than her rainbow (renderer/trail.js has both).
  palette() {
    return this.cfg.form === 'orb' ? { palette: 'orb' } : {};
  }

  // The farthest any monitor's corner is from `at` (DIP, screen): how far a rainboom from there has to go.
  reachFrom(at) {
    let far = 0;
    for (const { bounds: b } of this.windows) {
      for (const [x, y] of [[b.x, b.y], [b.x + b.width, b.y], [b.x, b.y + b.height], [b.x + b.width, b.y + b.height]]) {
        far = Math.max(far, Math.hypot(x - at.x, y - at.y));
      }
    }
    return far;
  }

  // A trail window finished streak `id` (and has cleared itself).
  done(sender, id) {
    const trail = this.windows.find((t) => !t.win.isDestroyed() && t.win.webContents === sender);
    if (trail) this.hide(trail, id, 'done');
  }

  hide(trail, id, why) {
    // A newer streak in the same window keeps it up.
    if (trail.drawing !== id) return;
    trail.drawing = 0;
    clearTimeout(trail.hideTimer);
    trail.hideTimer = null;
    if (!trail.win.isDestroyed()) trail.win.hide();
    this.debug(`trail ${id} hidden (${why}) on ${JSON.stringify(trail.bounds)}`);
  }

  // Whether `sender` is one of the trail windows (only they may report back).
  owns(sender) {
    return this.windows.some((t) => !t.win.isDestroyed() && t.win.webContents === sender);
  }

  destroyWindows() {
    for (const trail of this.windows.splice(0)) {
      clearTimeout(trail.hideTimer);
      if (!trail.win.isDestroyed()) trail.win.destroy();
    }
  }

  destroy() {
    this.closed = true;
    clearTimeout(this.buildTimer);
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) screen.removeListener(event, this.onDisplaysChanged);
    this.destroyWindows();
  }
}

// Whether a circle (centre `c`, radius `r`) reaches into a rectangle ({x, y, width, height}).
function circleCrossesRect(c, r, rect) {
  const dx = Math.max(rect.x - c.x, 0, c.x - (rect.x + rect.width));
  const dy = Math.max(rect.y - c.y, 0, c.y - (rect.y + rect.height));
  return Math.hypot(dx, dy) <= r;
}

module.exports = { Trails, TAIL_FACTOR, circleCrossesRect };
