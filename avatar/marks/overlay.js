'use strict';
// The windows her marks are drawn in: one transparent, click-through, never-focusable, always-on-top window per
// monitor, shown only while it has marks. Made on the first draw and remade when monitors come, go or change (and
// whatever is up is drawn again, converted afresh, since a monitor's scaling may be what changed).
//
// A draw is reported (`drawn`) only once every window it needs has painted its marks and is showing, so Octavia never
// says something is marked that isn't. One set of marks is up at a time: a new draw replaces it in place.

const { BrowserWindow, screen } = require('electron');
const { sanitize, toDip, assign } = require('./geometry');

const REBUILD_DELAY_MS = 400; // display changes come in bursts
const PAINT_TIMEOUT_MS = 4000;
const FADE_MS = 180; // the page's fade-out, before its window is hidden
const MAX_STAY_S = 3600;

class Overlay {
  constructor(cfg, { url, preload, emit, log }) {
    this.cfg = cfg;
    this.url = url;
    this.preload = preload;
    this.emit = emit;
    this.log = log;
    this.windows = [];
    this.current = null; // {id, raw (physical marks), stayS}
    this.pending = null; // {id, waiting: Set of windows yet to paint, shown, missing, dropped, timer}
    this.stayTimer = null;
    this.rebuildTimer = null;
    this.closed = false;
    this.onDisplaysChanged = (_event, _display, changed) => {
      if (Array.isArray(changed) && changed.every((what) => what === 'workArea')) return; // the taskbar, not a monitor
      this.scheduleRebuild();
    };
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) screen.on(event, this.onDisplaysChanged);
  }

  // ------------------------------------------------------------------ windows

  build() {
    this.destroyWindows();
    for (const display of screen.getAllDisplays()) {
      try {
        this.windows.push(this.makeWindow(display));
      } catch (err) {
        this.log(`could not make a marks window: ${err.message}`);
      }
    }
  }

  makeWindow(display) {
    // One pixel short of the monitor: a topmost window covering a whole monitor counts as a full-screen app to
    // Windows (the taskbar steps back, notifications go quiet), even a see-through one.
    const bounds = { ...display.bounds, height: display.bounds.height - 1 };
    const win = new BrowserWindow({
      ...bounds,
      show: false,
      title: 'marks',
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
    // Placed again once made: a window made for a monitor at another scaling can come out the wrong size, and some
    // Windows builds won't resize a window that isn't resizable. Its marks go by where it really is.
    win.setResizable(true);
    win.setBounds(bounds);
    win.setResizable(false);
    const real = win.getBounds();
    win.setAlwaysOnTop(true, 'screen-saver');
    if (this.cfg.hideFromCapture) win.setContentProtection(true);
    // Fully click-through: a mark is there to look at, never to get in the way of the mouse.
    win.setIgnoreMouseEvents(true);
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    win.webContents.on('render-process-gone', () => this.scheduleRebuild());
    const entry = { win, bounds: real.width > 0 && real.height > 0 ? real : bounds, scale: display.scaleFactor || 1, payload: null, hideTimer: null };
    win.loadURL(this.url).catch((err) => this.log(`marks window failed to load: ${err.message}`));
    return entry;
  }

  scheduleRebuild() {
    if (this.closed) return;
    clearTimeout(this.rebuildTimer);
    this.rebuildTimer = setTimeout(() => this.rebuild(), REBUILD_DELAY_MS);
  }

  // Monitors changed (or a page crashed): new windows, and what's up drawn again on them.
  rebuild() {
    this.rebuildTimer = null;
    if (this.closed) return;
    const up = this.current;
    this.build();
    if (up) this.place(up.id, up.raw, false);
  }

  byWebContents(sender) {
    return this.windows.find((w) => !w.win.isDestroyed() && w.win.webContents === sender) || null;
  }

  // ------------------------------------------------------------------ drawing

  // {type: "draw", id, marks: [...physical...], stay_s}
  draw(msg) {
    if (this.closed) return;
    const id = String(msg.id ?? '');
    const { marks, dropped } = sanitize(msg.marks);
    if (!marks.length) {
      this.emit({ type: 'error', id, message: 'no marks it could draw' });
      return;
    }
    const stay = Number(msg.stay_s);
    const stayS = Number.isFinite(stay) && stay > 0 ? Math.min(stay, MAX_STAY_S) : 0;
    this.current = { id, raw: marks, stayS };
    if (!this.windows.length) this.build();
    this.place(id, marks, true, dropped);
    clearTimeout(this.stayTimer);
    this.stayTimer = stayS ? setTimeout(() => this.clear('timer'), stayS * 1000) : null;
  }

  // Converts `raw` for today's monitors and hands each window its share. `report`: say `drawn` (or why not) once
  // it's painted; a redraw after a monitor change says nothing.
  place(id, raw, report, dropped = []) {
    this.failPending('a newer draw took its place');
    const dip = raw.map((m) => toDip(screen, m));
    const { perWindow, missing } = assign(dip, this.windows.map((w) => w.bounds));
    const waiting = new Set();
    this.windows.forEach((w, i) => {
      const marks = perWindow[i];
      if (!marks.length) {
        w.payload = null;
        this.blank(w);
        return;
      }
      w.payload = { id, marks, scale: w.scale };
      clearTimeout(w.hideTimer);
      w.hideTimer = null;
      waiting.add(w);
      if (!w.win.isDestroyed()) w.win.webContents.send('marks:draw', w.payload);
    });
    const shown = raw.length - missing.length;
    if (!report) {
      if (!waiting.size) {
        // A monitor change left none of them on a screen: they're gone, and Octavia is told so.
        this.current = null;
        clearTimeout(this.stayTimer);
        this.stayTimer = null;
        this.emit({ type: 'cleared', id, by: 'app' });
      }
      return;
    }
    if (!waiting.size) {
      this.current = null;
      clearTimeout(this.stayTimer);
      this.emit({ type: 'drawn', id, shown: 0, missing, dropped, displays: 0 });
      return;
    }
    const timer = setTimeout(() => {
      if (this.pending && this.pending.id === id) this.failPending("its windows didn't paint in time");
    }, PAINT_TIMEOUT_MS);
    this.pending = { id, waiting, displays: waiting.size, shown, missing, dropped, timer };
  }

  failPending(why) {
    const p = this.pending;
    if (!p) return;
    this.pending = null;
    clearTimeout(p.timer);
    this.emit({ type: 'error', id: p.id, message: why });
  }

  // A page asks what it should draw (it just loaded, or was reloaded).
  hello(sender) {
    const w = this.byWebContents(sender);
    return w ? w.payload : null;
  }

  // A page has drawn the marks of draw `id`: its window is shown, and once all of them are, the draw is reported.
  painted(sender, id) {
    const w = this.byWebContents(sender);
    if (!w || !w.payload || w.payload.id !== id) return;
    if (!w.win.isVisible()) w.win.showInactive();
    w.win.setAlwaysOnTop(true, 'screen-saver'); // back over anything that went topmost since
    const p = this.pending;
    if (!p || p.id !== id || !p.waiting.has(w)) return;
    p.waiting.delete(w);
    if (p.waiting.size) return;
    this.pending = null;
    clearTimeout(p.timer);
    this.emit({ type: 'drawn', id, shown: p.shown, missing: p.missing, dropped: p.dropped, displays: p.displays });
  }

  // Fades a window's marks out and hides it.
  blank(w) {
    if (w.win.isDestroyed()) return;
    w.win.webContents.send('marks:clear');
    clearTimeout(w.hideTimer);
    if (!w.win.isVisible()) return;
    w.hideTimer = setTimeout(() => {
      w.hideTimer = null;
      if (!w.payload && !w.win.isDestroyed()) w.win.hide();
    }, FADE_MS);
  }

  // Takes every mark down. Returns whether any were up (or on their way).
  clear(by) {
    clearTimeout(this.stayTimer);
    this.stayTimer = null;
    const up = this.current;
    if (this.pending) this.failPending(`cleared (${by}) before it was drawn`);
    this.current = null;
    for (const w of this.windows) {
      w.payload = null;
      this.blank(w);
    }
    if (up) this.emit({ type: 'cleared', id: up.id, by });
    return Boolean(up);
  }

  // Debug: writes what a monitor's window shows to a PNG (blank while content protection is on). `index` null: the
  // first window with marks.
  async capture(index, file) {
    const w = index == null ? this.windows.find((x) => x.payload) : this.windows[index];
    if (!w || w.win.isDestroyed()) throw new Error(`no marks window ${index}`);
    const image = await w.win.webContents.capturePage();
    require('node:fs').writeFileSync(file, image.toPNG());
  }

  destroyWindows() {
    for (const w of this.windows.splice(0)) {
      clearTimeout(w.hideTimer);
      if (!w.win.isDestroyed()) w.win.destroy();
    }
  }

  destroy() {
    this.closed = true;
    clearTimeout(this.stayTimer);
    clearTimeout(this.rebuildTimer);
    if (this.pending) clearTimeout(this.pending.timer);
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) screen.removeListener(event, this.onDisplaysChanged);
    this.destroyWindows();
  }
}

module.exports = { Overlay, PAINT_TIMEOUT_MS, FADE_MS };
