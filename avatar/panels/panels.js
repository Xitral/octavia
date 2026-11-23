'use strict';
// Her pop-up panels: one small window each, transparent and frameless, on top, and never focused, so whatever the
// user is typing into keeps the keyboard. See docs/visuals.md for the protocol.
//
// A panel's life: `show` makes a hidden window; its page asks for the spec (`panel:hello`), lays it out and says
// how tall it came out (`panel:size`); only then is the window placed, shown (without activating) and reported
// `shown`. A `show` with the id of a panel on screen swaps its content in place. Closing (the x, a right-click, its
// time running out, or Octavia) plays a short fold and destroys the window.
//
// The user can drag a panel by its title bar and resize it from its edges and corners; where they left one and how
// wide they made it (narrow and wide panels apart) are kept for the next ones. A height they set sticks for that
// panel (its content scrolls); otherwise the window follows the content's height, easing to it when she changes it.
//
// Moving, resizing and easing are stepped by the page's own animation frames (`panel:tick`, asked for with
// `panel:ticks` while any of them runs), so the window follows the pointer at the display's rate. A main-process
// timer only backs them up: on Windows a 16 ms interval really fires every ~31 ms, which made dragging lag.
//
// A panel never takes the keyboard, except while the user types into one of its boxes: clicking a box asks Octavia
// which window has the keyboard now (`typing` on, answered by `focus-from`; clicking a panel never activates it, so
// that's still the user's window), then makes the panel focusable and focuses it. Sending or Escape ends it, and
// Octavia hands the keyboard back to that window. Clicking into another window ends it too, with nothing to hand back.
// A send is taken at most every SEND_GAP_MS per panel: double clicks and choice spam don't queue turns.

const fs = require('node:fs');
const { BrowserWindow, screen } = require('electron');
const { place, maxHeight, clampAnchor } = require('./placement');
const { PageView } = require('./web');

const PAD = 18; // transparent room around the frame for its glow, each side (DIP)
const WIDTHS = { narrow: 440, wide: 680 };
const SIZE_TIMEOUT_MS = 8000; // a page that hasn't laid out by then is given up on
const FOLD_MS = 420; // longest the closing fold may take before the window goes anyway
const DRAG_STEP_MS = 16; // the backup timer's interval (it runs at ~31 ms on Windows: the page's frames lead)
const TWEEN_MS = 170; // the window easing to a new height when her change made the content taller or shorter
const MIN_FRAME = { width: 320, height: 120 }; // DIP, the frame without its glow room
const MAX_FRAME_WIDTH = 1300;
const ID = /^[A-Za-z0-9_-]{1,64}$/;
const FOCUS_WAIT_MS = 250; // how long to wait for Octavia to say which window had the keyboard before taking it
const SEND_GAP_MS = 800;
const MAX_SENT = 200; // values in one send: a worksheet's blanks
const CONTROLS = new Set(['play', 'pause', 'next', 'back', 'replay', 'step']); // an explainer's player bar
const MAX_SNAPSHOT_WIDTH = 1600; // DIP; a wider panel's picture is scaled down to this
const FIELD_ID = /^[A-Za-z0-9_-]{1,40}$/;
const LIVE_EVENTS = new Set(['ok', 'error', 'state', 'params']);
const LIVE_ADDRESS = /^(#[0-9]{1,2}|[A-Za-z0-9_-]{1,40})$/; // a block's number (#2) or id
const LIVE_FRAME_URL = 'avatar://app/renderer/live/'; // lib/scheme.js ORIGIN
const HOVER_POLL_MS = 60; // a panel with a web page on it: whether the pointer is on its frame, asked this often

// The web page a spec puts on its panel ("" for none). Only Octavia's spec names it, never the panel's own page.
function pageUrl(spec) {
  const block = spec && Array.isArray(spec.blocks) ? spec.blocks.find((b) => b && b.kind === 'page') : null;
  return block && typeof block.url === 'string' && /^https?:\/\//i.test(block.url) ? block.url : '';
}

// Whether a spec has a live block: the pointer over its frame is that frame's, so the page can't tell whether it's
// on the panel, and the main process goes by where the pointer is instead (as for a web page).
const hasLive = (spec) => Boolean(spec && Array.isArray(spec.blocks) && spec.blocks.some((b) => b && b.kind === 'live'));

// Every address a panel may send the user's browser to with its ↗: its pictures' sources (its page is apart).
function sourcesOf(spec) {
  const out = new Set();
  for (const b of (spec && spec.blocks) || []) {
    if (b && b.kind === 'image') for (const p of b.images || []) if (typeof p.source === 'string') out.add(p.source);
  }
  return out;
}

// What's kept between panels (and restarts): where the user last left one, and the widths they gave narrow and wide
// ones. Anything unreadable counts as nothing kept.
function readState(statePath) {
  const state = { anchor: null, sizes: {} };
  if (!statePath) return state;
  try {
    const { anchor, sizes } = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    if (anchor && Number.isFinite(anchor.right) && Number.isFinite(anchor.top)) state.anchor = { right: anchor.right, top: anchor.top };
    for (const kind of Object.keys(WIDTHS)) {
      const w = sizes && sizes[kind];
      if (Number.isFinite(w)) state.sizes[kind] = Math.min(Math.max(w, MIN_FRAME.width), MAX_FRAME_WIDTH);
    }
  } catch {
    // nothing kept yet, or a file from something else
  }
  return state;
}

function writeState(statePath, state) {
  if (!statePath) return;
  try {
    fs.writeFileSync(statePath, JSON.stringify({ anchor: state.anchor, sizes: state.sizes }));
  } catch {
    // Only a convenience: the next panel opens at the default spot and size instead.
  }
}

const ease = (t) => 1 - (1 - t) ** 3;

const inside = (p, b) => p.x >= b.x && p.x < b.x + b.width && p.y >= b.y && p.y < b.y + b.height;

// Bounds sent with a `show` (where a pinned panel was left), or null for anything else.
function goodBounds(at) {
  if (!at || typeof at !== 'object') return null;
  const b = { x: Math.round(at.x), y: Math.round(at.y), width: Math.round(at.width), height: Math.round(at.height) };
  if (!Object.values(b).every(Number.isFinite)) return null;
  if (b.width < MIN_FRAME.width || b.width > MAX_FRAME_WIDTH + 2 * PAD || b.height < MIN_FRAME.height) return null;
  return b;
}

// Whether its title bar is on one of the screens: a monitor unplugged since leaves it to the usual spot.
const onScreen = (b, displays) => displays.some((d) => inside({ x: b.x + b.width / 2, y: b.y + PAD + 10 }, d.workArea));

class Panels {
  // `web`: { make(prefs) -> a view for a page, guard, openExternal(url) } (main.js); without it, no web pages.
  constructor(cfg, { url, preload, emit, log, web = null }) {
    this.cfg = cfg;
    this.web = web;
    this.url = url;
    this.preload = preload;
    this.emit = emit;
    this.log = log;
    this.panels = new Map(); // id -> entry, in the order they opened
    const kept = readState(cfg.statePath);
    this.anchor = kept.anchor;
    this.sizes = kept.sizes; // narrow / wide -> the frame width the user last gave one
    this.drag = null;
    this.resizing = null;
  }

  // The panel a renderer message came from.
  bySender(sender) {
    for (const entry of this.panels.values()) if (!entry.win.isDestroyed() && entry.win.webContents === sender) return entry;
    return null;
  }

  // The work area new panels go on: the monitor of the saved anchor while it's attached, else the one the pointer is on.
  workArea() {
    const displays = screen.getAllDisplays();
    if (this.anchor) {
      const spot = { x: this.anchor.right - 1, y: this.anchor.top };
      const home = displays.find((d) => inside(spot, d.workArea));
      if (home) return home.workArea;
    }
    const cursor = screen.getCursorScreenPoint();
    const under = displays.find((d) => inside(cursor, d.bounds));
    return (under || screen.getPrimaryDisplay()).workArea;
  }

  show(msg) {
    const id = typeof msg.id === 'string' && ID.test(msg.id) ? msg.id : null;
    if (!id || !msg.spec || typeof msg.spec !== 'object') {
      // The id as sent, so Octavia can match the error to the panel it waits on.
      const raw = typeof msg.id === 'string' ? msg.id.slice(0, 100) : null;
      this.emit({ type: 'error', id: raw, message: 'a show needs an id (letters, digits, - or _) and a spec' });
      return;
    }
    const stayS = Number.isFinite(msg.stay_s) && msg.stay_s > 0 ? Math.min(msg.stay_s, 3600) : 0;
    const pinned = msg.pinned === true;
    const pinnable = msg.pinnable !== false;
    const existing = this.panels.get(id);
    if (existing && !existing.closing) {
      existing.spec = msg.spec;
      existing.stayS = stayS;
      existing.pinned = pinned;
      existing.pinnable = pinnable;
      // A panel still laying out (never shown) just takes the newer content: it isn't a swap.
      existing.replaced = this.isShown(existing.win);
      this.pageChanged(existing);
      if (hasLive(msg.spec) && !existing.hoverPoll) this.startHoverPoll(existing);
      existing.reported = false;
      existing.win.webContents.send('panel:spec', this.payload(existing));
      this.watchSize(existing);
      return;
    }
    if (existing) this.finish(existing, existing.by); // still folding away: it goes now, and the new one takes its place
    const closed = [];
    // Pinned panels are the user's: never pushed out, and they don't use up her room for new ones.
    const open = () => [...this.panels.values()].filter((e) => !e.closing && !e.pinned);
    while (open().length >= this.cfg.maxPanels) {
      const oldest = open()[0];
      closed.push(oldest.id);
      this.close(oldest.id, 'replaced');
    }
    const kind = WIDTHS[msg.spec.width] ? msg.spec.width : 'narrow';
    const width = (this.sizes[kind] || WIDTHS[kind]) + 2 * PAD;
    const win = new BrowserWindow({
      width,
      height: 200,
      useContentSize: true,
      show: false,
      title: 'Octavia',
      transparent: true,
      frame: false,
      backgroundColor: '#00000000',
      hasShadow: false,
      resizable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      focusable: false, // it must never take the keyboard from what the user is typing into
      alwaysOnTop: true,
      webPreferences: {
        preload: this.preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        spellcheck: false,
        offscreen: this.cfg.offscreen === true, // checks only: drawn off screen, never shown
      },
    });
    if (this.cfg.hideFromCapture) win.setContentProtection(true); // out of her own screenshots, and shares
    // The glow room round the frame lets clicks through to what's under it; the page takes the mouse back while the
    // pointer is on the frame itself (panel:mouse).
    win.setIgnoreMouseEvents(true, { forward: true });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    // A live block's program can't fetch (its CSP), but WebRTC isn't covered by CSP: no UDP that skips the proxy,
    // and the panels' session has a proxy that goes nowhere (main.js), so it can't get out that way either.
    if (typeof win.webContents.setWebRTCIPHandlingPolicy === 'function') win.webContents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    const entry = { id, win, width, kind, userHeight: false, tween: null, spec: msg.spec, stayS, pinned, pinnable, at: goodBounds(msg.at), replaced: false, reported: false, closing: false, timer: null, closedEarlier: closed };
    win.on('blur', () => this.typing(win.webContents, false, false)); // they clicked away: the keyboard is theirs
    win.webContents.on('render-process-gone', (_e, details) => {
      this.emit({ type: 'error', id, message: `panel page gone: ${details.reason}` });
      this.finish(entry, 'crashed');
    });
    // A page stuck (a runaway layout) can't be closed by its own x or timer: it goes now.
    win.webContents.on('unresponsive', () => {
      this.emit({ type: 'error', id, message: 'the panel stopped responding' });
      this.finish(entry, 'crashed');
    });
    entry.pageUrl = this.web ? pageUrl(msg.spec) : '';
    if (entry.pageUrl || hasLive(msg.spec)) this.startHoverPoll(entry);
    this.panels.set(id, entry);
    this.watchSize(entry);
    win.loadURL(this.url);
  }

  payload(entry) {
    // `base`: the kind's own width, the one the text is sized for (a wider panel zooms its text up).
    return { id: entry.id, spec: entry.spec, stayS: entry.stayS, pinned: entry.pinned, pinnable: entry.pinnable, pad: PAD, width: entry.width - 2 * PAD, base: WIDTHS[entry.kind], update: entry.replaced };
  }

  watchSize(entry) {
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      if (entry.reported) return;
      this.emit({ type: 'error', id: entry.id, message: "the panel didn't lay out in time" });
      this.finish(entry, 'failed'); // a swap too: a page that stuck on its new content can't be trusted to close
    }, SIZE_TIMEOUT_MS);
  }

  hello(sender) {
    const entry = this.bySender(sender);
    return entry ? this.payload(entry) : null;
  }

  // The page laid out: `height` is its content's (DIP, frame included, glow room excluded). `refit`: only its height
  // after the window changed size (a pull from the side), never the report of new content: it moves the window but is
  // not the `shown` a show waits for (one sent just before the page took a swap's new spec would be the old one's).
  sized(sender, height, notes, refit = false) {
    const entry = this.bySender(sender);
    if (!entry || entry.closing || !Number.isFinite(height)) return;
    const win = entry.win;
    if (refit && !this.isShown(win)) return;
    const area = this.workArea();
    const h = Math.round(Math.min(Math.max(height, 60), maxHeight(area))) + 2 * PAD;
    if (this.isShown(win)) {
      // Its top and width stay; the height eases to the content's, unless the user set one (left at a height they
      // chose, or pulling its bottom now). Pulled from the side, its height follows the content as it rewraps, frame
      // by frame, rather than jumping there once they let go.
      entry.wantHeight = h;
      const resizing = this.resizing && this.resizing.entry === entry ? this.resizing : null;
      if (resizing) {
        if (!entry.userHeight && !resizing.edge.includes('b')) resizing.follow(h);
      } else if (!entry.userHeight) this.tweenHeight(entry, h);
    } else {
      const others = [...this.panels.values()].filter((e) => e !== entry && !e.closing && this.isShown(e.win));
      const spot = place({ workArea: area, open: others.map((e) => e.win.getBounds()), width: entry.width, height: h, anchor: this.anchor });
      // A pinned panel comes back where the user left it, as long as that's still on a screen.
      const at = entry.at && onScreen(entry.at, screen.getAllDisplays()) ? entry.at : null;
      if (at) {
        entry.width = at.width;
        entry.userHeight = at.height !== h;
      }
      this.setBounds(win, at || { ...spot, width: entry.width, height: h });
      if (at) win.webContents.send('panel:spec', this.payload(entry));
      if (this.cfg.offscreen) win.offscreenShown = true;
      else if (!this.tucked || !entry.pinned) win.showInactive();
      win.everShown = true; // tucked away it isn't visible, but it's up
      win.setAlwaysOnTop(true, 'floating');
      win.webContents.send('panel:go');
    }
    if (refit) return;
    const first = !entry.reported;
    entry.reported = true;
    clearTimeout(entry.timer);
    if (!first) return;
    const b = win.getBounds();
    const settles = entry.userHeight ? b.height : h; // where an easing height is going, not where it is yet
    this.emit({
      type: 'shown',
      id: entry.id,
      replaced: entry.replaced,
      closed: entry.closedEarlier,
      width: b.width - 2 * PAD,
      height: settles - 2 * PAD,
      notes: Array.isArray(notes) ? notes.slice(0, 8).map((note) => String(note).slice(0, 300)) : [],
    });
    entry.closedEarlier = [];
  }

  // The window eases to `height` over TWEEN_MS, its top edge where it is.
  tweenHeight(entry, height) {
    if (entry.tween && entry.tween.to === height) return; // already on its way there: a restart would only slow it
    this.stopTween(entry);
    const win = entry.win;
    if (win.isDestroyed()) return;
    const start = win.getBounds();
    if (start.height === height) {
      win.setResizable(false); // a tween this one cut short may have left it on
      return;
    }
    const began = Date.now();
    win.setResizable(true);
    const tween = {
      to: height,
      step: () => {
        if (win.isDestroyed()) return this.stopTween(entry);
        const t = Math.min(1, (Date.now() - began) / TWEEN_MS);
        const now = win.getBounds();
        win.setBounds({ x: now.x, y: now.y, width: now.width, height: Math.round(start.height + (height - start.height) * ease(t)) });
        if (t >= 1) {
          this.stopTween(entry);
          win.setResizable(false);
        }
      },
    };
    tween.timer = setInterval(tween.step, DRAG_STEP_MS);
    entry.tween = tween;
    this.updateTicks(entry);
  }

  stopTween(entry) {
    if (!entry.tween) return;
    clearInterval(entry.tween.timer);
    entry.tween = null;
    this.updateTicks(entry);
  }

  // Asks the page for a tick every animation frame while the panel is moving, resizing or easing, and stops it after.
  updateTicks(entry) {
    const wants = Boolean((this.drag && this.drag.entry === entry) || (this.resizing && this.resizing.entry === entry) || entry.tween);
    if (wants === Boolean(entry.ticking) || entry.win.isDestroyed()) return;
    entry.ticking = wants;
    entry.win.webContents.send('panel:ticks', wants);
  }

  // A frame of the page: the drag, the resize and the easing each take a step (each works out where the window
  // should be from the pointer or the clock, so an extra step from the backup timer does no harm).
  tick(sender) {
    const entry = this.bySender(sender);
    if (!entry) return;
    if (this.drag && this.drag.entry === entry) this.drag.step();
    if (this.resizing && this.resizing.entry === entry) this.resizing.step();
    if (entry.tween) entry.tween.step();
  }

  // The user pressed on an edge or corner of a panel (`edge`: r, l, b, br, bl): that side follows the pointer until
  // they let go. Like the drag, the main process moves it: the window never has focus.
  startResize(sender, edge) {
    const entry = this.bySender(sender);
    if (!entry || entry.closing || !/^(r|l|b|br|bl)$/.test(edge)) return;
    this.endResize();
    this.stopTween(entry);
    const from = entry.win.getBounds();
    const cursor = screen.getCursorScreenPoint();
    const area = this.workArea();
    const maxH = area.height + 2 * PAD;
    const resizing = { entry, edge, from, cursor, moved: false, height: from.height };
    entry.win.setResizable(true);
    // Where the window should be now, from the pointer (and, from the side, the content's latest height).
    const target = () => {
      const p = screen.getCursorScreenPoint();
      const dx = p.x - cursor.x;
      const dy = p.y - cursor.y;
      if (!resizing.moved && Math.abs(dx) + Math.abs(dy) < 3) return null;
      resizing.moved = true;
      const b = { ...from, height: resizing.height };
      const clampW = (w) => Math.min(Math.max(w, MIN_FRAME.width + 2 * PAD), MAX_FRAME_WIDTH + 2 * PAD);
      if (edge.includes('r')) b.width = clampW(from.width + dx);
      if (edge.includes('l')) {
        b.width = clampW(from.width - dx);
        b.x = from.x + from.width - b.width; // the right edge stays put
      }
      if (edge.includes('b')) b.height = Math.min(Math.max(from.height + dy, MIN_FRAME.height + 2 * PAD), maxH);
      return b;
    };
    resizing.step = () => {
      if (entry.win.isDestroyed()) return this.endResize();
      const b = target();
      if (!b) return;
      const now = entry.win.getBounds();
      // Only when it changed: an unchanged setBounds still makes Windows repaint the see-through window.
      if (b.x !== now.x || b.y !== now.y || b.width !== now.width || b.height !== now.height) entry.win.setBounds(b);
    };
    resizing.follow = (height) => {
      resizing.height = height;
      if (resizing.moved) resizing.step();
    };
    resizing.timer = setInterval(resizing.step, DRAG_STEP_MS);
    this.resizing = resizing;
    this.updateTicks(entry);
  }

  // Let go: the panel keeps its new size; its width is kept for the next panels of its kind, and a height the user
  // set sticks for this one.
  endResize(sender) {
    const resizing = this.resizing;
    if (!resizing || (sender && resizing.entry.win.webContents !== sender)) return;
    clearInterval(resizing.timer);
    this.resizing = null;
    const { entry } = resizing;
    if (entry.win.isDestroyed()) return;
    this.updateTicks(entry);
    entry.win.setResizable(false);
    if (!resizing.moved) return;
    const b = entry.win.getBounds();
    entry.width = b.width;
    if (resizing.edge.includes('b')) entry.userHeight = true;
    // A height they set earlier stays; otherwise the content's last height, if a report came after the last step.
    else if (!entry.userHeight && entry.wantHeight) this.tweenHeight(entry, entry.wantHeight);
    this.sizes[entry.kind] = b.width - 2 * PAD;
    writeState(this.cfg.statePath, { anchor: this.anchor, sizes: this.sizes });
    this.reportMoved(entry);
  }

  setBounds(win, bounds) {
    if (win.isDestroyed()) return;
    win.setResizable(true); // some Windows builds won't let a program resize a non-resizable window
    win.setBounds(bounds);
    win.setResizable(false);
  }

  // Octavia's word on a panel's pin: the page shows it and its timer stops or starts again.
  pin(id, on, stayS) {
    const entry = this.panels.get(id);
    if (!entry || entry.closing || entry.win.isDestroyed()) return;
    entry.pinned = on === true;
    entry.stayS = Number.isFinite(stayS) && stayS > 0 ? Math.min(stayS, 3600) : 0;
    entry.win.webContents.send('panel:pinned', { on: entry.pinned, stayS: entry.stayS });
    if (entry.pinned) this.reportMoved(entry);
    if (this.tucked && !entry.pinned && !this.isVisible(entry.win)) entry.win.showInactive();
  }

  // The pin button: Octavia decides (`pin` comes back), so the button never says more than she knows.
  pinAsked(sender, on) {
    const entry = this.bySender(sender);
    if (entry && !entry.closing && entry.pinnable) this.emit({ type: 'pin-asked', id: entry.id, on: on === true });
  }

  reportMoved(entry) {
    if (!entry.pinned || entry.win.isDestroyed()) return;
    const b = entry.win.getBounds();
    this.emit({ type: 'moved', id: entry.id, bounds: { x: b.x, y: b.y, width: b.width, height: b.height } });
  }

  // Pinned panels out of sight (a screen task has the mouse: a panel in the way would stop its clicks), or back.
  tuck(on) {
    this.tucked = on === true;
    for (const entry of this.panels.values()) {
      if (!entry.pinned || entry.closing || entry.win.isDestroyed() || !entry.reported) continue;
      if (this.tucked) entry.win.hide();
      else if (!this.cfg.offscreen) entry.win.showInactive();
    }
  }

  isVisible(win) {
    return !win.isDestroyed() && win.isVisible();
  }

  // Folds the panel away and destroys it; `by` says why ("user", "timer", "replaced", "octavia").
  close(id, by) {
    const entry = this.panels.get(id);
    if (!entry || entry.closing) return false;
    entry.closing = true;
    clearTimeout(entry.timer);
    if (this.drag && this.drag.entry === entry) this.endDrag();
    this.closePage(entry); // a native view would stay sharp on top of the fold: it goes first
    if (!entry.win.isDestroyed() && this.isShown(entry.win)) {
      entry.win.webContents.send('panel:fold');
      entry.timer = setTimeout(() => this.finish(entry, by), FOLD_MS);
      entry.by = by;
    } else {
      this.finish(entry, by);
    }
    return true;
  }

  closeAll(by) {
    return [...this.panels.keys()].filter((id) => this.close(id, by));
  }

  // The page's fold is over (or took too long): the window goes.
  folded(sender) {
    const entry = this.bySender(sender);
    if (entry && entry.closing) this.finish(entry, entry.by);
  }

  finish(entry, by) {
    if (this.panels.get(entry.id) !== entry) return;
    this.destroy(entry);
    this.emit({ type: 'closed', id: entry.id, by });
  }

  destroy(entry) {
    this.closePage(entry);
    clearInterval(entry.hoverPoll);
    clearTimeout(entry.timer);
    clearTimeout(entry.focusWait);
    if (entry.tween) clearInterval(entry.tween.timer);
    entry.tween = null;
    if (this.resizing && this.resizing.entry === entry) this.endResize();
    if (this.panels.get(entry.id) === entry) this.panels.delete(entry.id);
    if (this.drag && this.drag.entry === entry) this.endDrag();
    if (!entry.win.isDestroyed()) entry.win.destroy();
  }

  // The page says whether the pointer is on the frame (take the mouse) or in the glow room round it (let it through).
  mouse(sender, over) {
    const entry = this.bySender(sender);
    if (!entry || entry.win.isDestroyed() || entry.hoverPoll) return; // a page's or live block's: by the pointer itself
    if (over) entry.win.setIgnoreMouseEvents(false);
    else entry.win.setIgnoreMouseEvents(true, { forward: true });
  }

  // The user pressed on a panel's title bar: it follows the pointer until they let go. The window never has focus,
  // so the main process moves it, like the avatar's own drag.
  startDrag(sender) {
    const entry = this.bySender(sender);
    if (!entry || entry.closing) return;
    this.endDrag();
    const cursor = screen.getCursorScreenPoint();
    const [x, y] = entry.win.getPosition();
    const drag = { entry, dx: cursor.x - x, dy: cursor.y - y, moved: false, start: cursor, steps: 0, began: Date.now() };
    drag.step = () => {
      if (entry.win.isDestroyed()) return this.endDrag();
      drag.steps++;
      const p = screen.getCursorScreenPoint();
      if (!drag.moved && Math.abs(p.x - drag.start.x) + Math.abs(p.y - drag.start.y) < 4) return;
      drag.moved = true;
      const nx = Math.round(p.x - drag.dx);
      const ny = Math.round(p.y - drag.dy);
      if (nx === drag.x && ny === drag.y) return; // the pointer hasn't moved since the last frame
      drag.x = nx;
      drag.y = ny;
      entry.win.setPosition(nx, ny);
    };
    drag.timer = setInterval(drag.step, DRAG_STEP_MS);
    this.drag = drag;
    this.updateTicks(entry);
  }

  // Let go: where it ended up is where the next panels start (saved for next time too).
  endDrag(sender) {
    const drag = this.drag;
    if (!drag || (sender && drag.entry.win.webContents !== sender)) return;
    clearInterval(drag.timer);
    this.drag = null;
    if (this.cfg.debug) this.log(`drag: ${drag.steps} steps in ${Date.now() - drag.began} ms`);
    if (drag.entry.win.isDestroyed()) return;
    this.updateTicks(drag.entry);
    if (!drag.moved) return;
    this.reportMoved(drag.entry);
    if (drag.entry.pinned) return; // where a pinned one sits is its own, not where the next panels start
    const b = drag.entry.win.getBounds();
    this.anchor = clampAnchor({ right: b.x + b.width, top: b.y }, this.workArea(), b.width);
    writeState(this.cfg.statePath, { anchor: this.anchor, sizes: this.sizes });
  }

  // Debug: mouse events played into a panel's page (the demo uses it to try the pen), never the real pointer.
  input(id, events) {
    const entry = this.panels.get(id);
    if (!entry || entry.win.isDestroyed()) return;
    for (const e of events) {
      if (!e || !['mouseDown', 'mouseUp', 'mouseMove'].includes(e.type) || !Number.isFinite(e.x) || !Number.isFinite(e.y)) continue;
      const button = ['left', 'right', 'middle'].includes(e.button) ? e.button : 'left';
      const modifiers = Array.isArray(e.modifiers) ? e.modifiers.filter((m) => m === 'leftButtonDown' || m === 'rightButtonDown') : [];
      try {
        entry.win.webContents.sendInputEvent({ type: e.type, x: Math.round(e.x), y: Math.round(e.y), button, clickCount: 1, modifiers });
      } catch (err) {
        this.log(`input: ${err.message}`);
      }
    }
  }

  // The page says the user is typing into a box (`on`), or stopped (`restore`: hand the keyboard back, as after
  // sending or Escape; not when they clicked away).
  typing(sender, on, restore) {
    const entry = this.bySender(sender);
    if (!entry || entry.win.isDestroyed()) return;
    if (on) {
      if (entry.typing) return;
      entry.typing = true;
      entry.previous = 0;
      this.emit({ type: 'typing', id: entry.id, on: true }); // Octavia answers with the window that has the keyboard
      entry.focusWait = setTimeout(() => this.takeKeyboard(entry), FOCUS_WAIT_MS);
      return;
    }
    if (!entry.typing) return;
    entry.typing = false;
    clearTimeout(entry.focusWait);
    if (entry.hasKeyboard) {
      entry.hasKeyboard = false;
      entry.win.setFocusable(false);
      entry.win.setSkipTaskbar(true); // focusable windows get a taskbar button on Windows: never one of hers
    }
    this.emit({ type: 'typing', id: entry.id, on: false, restore: Boolean(restore), previous: entry.previous || 0 });
  }

  // Octavia says which window had the keyboard when the user clicked a box: the one to hand it back to.
  focusFrom(id, previous) {
    const entry = this.panels.get(id);
    if (!entry || !entry.typing || entry.hasKeyboard) return;
    entry.previous = Number.isInteger(previous) && previous > 0 ? previous : 0;
    clearTimeout(entry.focusWait);
    this.takeKeyboard(entry);
  }

  takeKeyboard(entry) {
    if (!entry.typing || entry.hasKeyboard || entry.win.isDestroyed()) return;
    entry.hasKeyboard = true;
    entry.win.setFocusable(true);
    entry.win.setSkipTaskbar(true);
    entry.win.focus();
  }

  // The user sent what's in a panel's boxes (or clicked a choice): Octavia hears it as their turn.
  // `untouched`: fields sent as she pre-filled them; `choice`: a choice button she offered was clicked.
  sent(sender, values, field, untouched, choice) {
    const entry = this.bySender(sender);
    if (!entry || !values || typeof values !== 'object') return;
    const now = Date.now();
    if (now - (entry.lastSent || 0) < SEND_GAP_MS) return; // a double click, or choices clicked through: one turn
    entry.lastSent = now;
    const clean = {};
    for (const [k, v] of Object.entries(values).slice(0, MAX_SENT)) if (FIELD_ID.test(k)) clean[k] = String(v).slice(0, 2000);
    if (!Object.keys(clean).length) return;
    const marks = Array.isArray(untouched) ? untouched.filter((k) => Object.hasOwn(clean, k)) : [];
    this.emit({ type: 'input', id: entry.id, values: clean, field: FIELD_ID.test(String(field)) ? String(field) : '', untouched: marks, choice: choice === true });
  }

  // Where everything on the panel is (its own pixels), for Octavia to edit by: at most 400 boxes, numbers only.
  laidOut(sender, boxes) {
    const entry = this.bySender(sender);
    if (!entry || !boxes || typeof boxes !== 'object') return;
    const clean = {};
    for (const [key, b] of Object.entries(boxes).slice(0, 400)) {
      if (typeof key !== 'string' || key.length > 90 || !b) continue;
      const box = ['x', 'y', 'w', 'h'].map((k) => Math.round(Number(b[k])));
      if (box.every(Number.isFinite)) clean[key] = { x: box[0], y: box[1], w: box[2], h: box[3] };
    }
    this.emit({ type: 'layout', id: entry.id, boxes: clean });
  }

  // A press on an explainer's player bar: Octavia plays, pauses, steps or replays it.
  control(sender, action, step) {
    const entry = this.bySender(sender);
    if (!entry || !CONTROLS.has(action)) return;
    const msg = { type: 'control', id: entry.id, action };
    if (Number.isInteger(step) && step >= 0 && step <= 100) msg.step = step;
    this.emit(msg);
  }

  // What a live block's program said, passed on to Octavia as data: ok, error (message, line), state (what the
  // program reports), params (the values the user set on its controls). A program that stopped responding has its
  // frame's process ended here first (the page takes the frame down after), unless that's the page's own process.
  live(sender, address, report) {
    const entry = this.bySender(sender);
    const r = report && typeof report === 'object' ? report : {};
    if (!entry || !LIVE_EVENTS.has(r.type) || !LIVE_ADDRESS.test(address)) return;
    const msg = { type: 'live', id: entry.id, block: address, event: r.type };
    if (r.type === 'error') {
      msg.message = String(r.message || '').slice(0, 400);
      if (Number.isInteger(r.line) && r.line > 0) msg.line = r.line;
      if (r.hung === true) {
        msg.hung = true;
        this.endLiveFrame(entry, String(r.frame || ''));
      }
    } else if (r.type === 'state') msg.state = String(r.state || '').slice(0, 1500);
    else if (r.type === 'params' && r.values && typeof r.values === 'object') {
      msg.values = {};
      for (const [k, v] of Object.entries(r.values).slice(0, 16)) {
        if (!FIELD_ID.test(k)) continue;
        if (typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))) msg.values[k] = v;
        else if (typeof v === 'string') msg.values[k] = v.slice(0, 40);
      }
    }
    this.emit(msg);
  }

  // The stuck one only, found by the name its page gave its frame. Frames that share its process are stuck with it.
  endLiveFrame(entry, name) {
    if (entry.win.isDestroyed() || !/^live-[0-9a-z]{4,40}$/.test(name)) return;
    const main = entry.win.webContents.mainFrame;
    for (const frame of main.framesInSubtree) {
      if (frame === main || frame.name !== name || !String(frame.url).startsWith(LIVE_FRAME_URL)) continue;
      const pid = frame.osProcessId;
      if (!pid || pid === main.osProcessId) continue; // in the page's own process: 'unresponsive' deals with that
      try {
        process.kill(pid);
        this.log(`ended a stuck live frame (pid ${pid}) on ${entry.id}`);
      } catch (err) {
        this.log(`couldn't end a stuck live frame: ${err.message}`);
      }
    }
  }

  // Where an explainer's pointer points, as screen pixels (physical, as her orb is told), from the page's own.
  pointer(sender, rect) {
    const entry = this.bySender(sender);
    if (!entry || !rect || entry.win.isDestroyed()) return;
    const r = ['x', 'y', 'width', 'height'].map((k) => Number(rect[k]));
    if (!r.every(Number.isFinite) || r[2] <= 0 || r[3] <= 0) return;
    const content = entry.win.getContentBounds();
    const dip = { x: Math.round(content.x + r[0]), y: Math.round(content.y + r[1]), width: Math.round(r[2]), height: Math.round(r[3]) };
    let px = dip;
    try {
      if (typeof screen.dipToScreenRect === 'function') px = screen.dipToScreenRect(entry.win, dip);
    } catch {
      px = dip;
    }
    this.emit({ type: 'pointer', id: entry.id, rect: { x: px.x, y: px.y, w: px.width, h: px.height } });
  }

  // How many pen strokes the user has on the panel now.
  inked(sender, strokes) {
    const entry = this.bySender(sender);
    if (!entry || !Number.isFinite(strokes)) return;
    this.emit({ type: 'ink', id: entry.id, strokes: Math.max(0, Math.round(strokes)) });
  }

  // A JPEG of the panel as it's on screen, ink and all, for Octavia to look at. A window kept out of captures reads
  // back blank, so it's let in for the moment of the picture.
  // One at a time: each lets its window into capture for a moment, and two at once would let it out mid-picture.
  snapshot(id) {
    this.snapshots = (this.snapshots || Promise.resolve()).then(() => this.takeSnapshot(id));
    return this.snapshots;
  }

  async takeSnapshot(id) {
    const entry = this.panels.get(id);
    if (!entry || entry.win.isDestroyed()) {
      this.emit({ type: 'snapshot', id, error: `no panel ${id}` });
      return;
    }
    if (entry.page) {
      this.emit({ type: 'snapshot', id, error: 'it has a web page on it, and the page itself is not for me to look at' });
      return;
    }
    const win = entry.win;
    try {
      if (this.cfg.hideFromCapture) win.setContentProtection(false);
      let image = await win.webContents.capturePage();
      if (image.getSize().width > MAX_SNAPSHOT_WIDTH) image = image.resize({ width: MAX_SNAPSHOT_WIDTH, quality: 'good' });
      const size = image.getSize();
      this.emit({ type: 'snapshot', id, jpeg: image.toJPEG(85).toString('base64'), width: size.width, height: size.height });
    } catch (err) {
      this.emit({ type: 'snapshot', id, error: err.message });
    } finally {
      if (this.cfg.hideFromCapture && !win.isDestroyed()) win.setContentProtection(true);
    }
  }

  async capture(id, file) {
    const entry = this.panels.get(id);
    if (!entry || entry.win.isDestroyed()) throw new Error(`no panel ${id}`);
    const image = await entry.win.webContents.capturePage();
    await fs.promises.writeFile(file, image.toPNG());
  }

  isShown(win) {
    return !win.isDestroyed() && (win.isVisible() || win.offscreenShown === true || win.everShown === true);
  }

  // ---------------------------------------------------------------- a web page on a panel

  // Her spec for a panel that's up changed: its page goes to a new address, comes, or goes.
  pageChanged(entry) {
    const url = this.web ? pageUrl(entry.spec) : '';
    if (url === entry.pageUrl) return;
    entry.pageUrl = url;
    if (!url) {
      this.closePage(entry);
      if (!hasLive(entry.spec)) {
        clearInterval(entry.hoverPoll);
        entry.hoverPoll = null;
      }
      return;
    }
    if (!entry.hoverPoll) this.startHoverPoll(entry);
    if (entry.page) entry.page.navigate(url); // the panel's page reports where it goes next (`web`)
  }

  // The panel's page says where on it the web page goes (its own pixels), or null when it's out of sight.
  webRect(sender, rect) {
    const entry = this.bySender(sender);
    if (!entry || entry.closing || !entry.pageUrl || entry.win.isDestroyed()) return;
    if (!entry.page) {
      const page = new PageView({
        url: entry.pageUrl,
        make: this.web.make,
        guard: this.web.guard,
        report: (state) => {
          if (entry.page !== page) return;
          this.emit({ type: 'page', id: entry.id, ...state });
          if (!entry.win.isDestroyed()) entry.win.webContents.send('panel:page', state);
        },
      });
      entry.page = page;
      page.view.attach(entry.win);
      page.load();
    }
    const view = entry.page.view;
    const ok = rect && ['x', 'y', 'width', 'height'].every((k) => Number.isFinite(rect[k]));
    if (!ok || rect.width < 8 || rect.height < 8) {
      view.setVisible(false);
      return;
    }
    const [w, h] = entry.win.getContentSize();
    const x = Math.max(0, Math.round(rect.x));
    const y = Math.max(0, Math.round(rect.y));
    const bounds = { x, y, width: Math.max(1, Math.min(Math.round(rect.width), w - x)), height: Math.max(1, Math.min(Math.round(rect.height), h - y)) };
    view.setBounds(bounds);
    view.setVisible(true);
  }

  closePage(entry) {
    const page = entry.page;
    if (!page) return;
    entry.page = null;
    try {
      if (!entry.win.isDestroyed()) page.view.detach(entry.win);
    } catch {
      // the window went first
    }
    page.close();
  }

  // A page's panel can't hear the pointer over the page (the page's own view has it), so the main process asks
  // where the pointer is: on the frame, the panel takes the mouse and its count waits; off it, clicks go through.
  startHoverPoll(entry) {
    clearInterval(entry.hoverPoll);
    entry.over = null;
    entry.hoverPoll = setInterval(() => {
      if (entry.win.isDestroyed()) return clearInterval(entry.hoverPoll);
      const dragging = (this.drag && this.drag.entry === entry) || (this.resizing && this.resizing.entry === entry);
      const b = entry.win.getBounds();
      const frame = { x: b.x + PAD, y: b.y + PAD, width: b.width - 2 * PAD, height: b.height - 2 * PAD };
      const over = dragging || inside(screen.getCursorScreenPoint(), frame);
      if (over === entry.over) return;
      entry.over = over;
      if (over) entry.win.setIgnoreMouseEvents(false);
      else entry.win.setIgnoreMouseEvents(true, { forward: true });
      entry.win.webContents.send('panel:hover', over);
    }, HOVER_POLL_MS);
  }

  // The user clicked ↗ on a panel: its page (`which` "page") or one of its pictures' sources, in their own browser.
  openSource(sender, which) {
    const entry = this.bySender(sender);
    if (!entry || !this.web || typeof which !== 'string') return;
    const url = which === 'page' ? (entry.page ? entry.page.currentUrl() : entry.pageUrl) : which;
    if (which !== 'page' && !sourcesOf(entry.spec).has(url)) return; // only what she put there
    if (!/^https?:\/\//i.test(url || '')) return;
    this.web.openExternal(url);
  }

  destroyAll() {
    for (const entry of [...this.panels.values()]) this.destroy(entry);
  }
}

module.exports = { Panels, PAD, WIDTHS, pageUrl };
