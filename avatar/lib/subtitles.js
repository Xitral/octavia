'use strict';
// Her subtitles' window (docs/subtitles.md): transparent, click-through, never focused, always on top, beside her and
// following her wherever she goes: dragged, or flown off to point or perch (her window's own 'move' events, placed by
// her body while she's away). With the pointer on the words they fade, as she does, so what's under them shows (the
// main process's cursor poll, as hers; the page decides with her own SeeThrough). Made on the first subtitle, hidden once a reply's
// last one has faded. Octavia decides what each sentence shows (src/octavia/avatar/subtitles.py); this shows it as
// the sentence's audio starts (the 'segment' message) and fades it when she's done ('subtitles-end'). Her state goes
// to it too, as it goes to her orb, so the words move with her: each sentence's feeling, a feeling or mood between
// sentences ('emotion', 'mood': a mood outlasts the reply, so the next one starts in it) and her voice level
// ('mouth', only while a line is up). When she leaves ('hide'), the words still up go with her, fading as she does.

const fs = require('node:fs/promises');
const path = require('node:path');
const { BrowserWindow } = require('electron');
const { placeSubtitles } = require('./subtitle-layout');

const BACKSTOP_MS = 60_000; // no word from Octavia for this long after a line: it fades anyway
const FADED_WAIT_MS = 4000; // the page says when it has faded; hidden after this whatever it said
const VOICE_HZ = 30; // her voice level passed on at most this often (renderer/subtitles/subtitles.js VOICE_FPS)
const POINTER_MS = 50; // the cursor poll while a line is up, as her own (lib/window.js CURSOR_FEED_MS)
// How long she takes to leave, by form: the words fade over the same time, so they go with her instead of staying up
// on their own after she's gone (the user, 2026-10-30). The orb dissolves (renderer/orb-motion.js LEAVE_S); the
// character waves, then sinks (renderer/presence.js WAVE_OUT + LEAVE).
const LEAVE_MS = { orb: 600, character: 1450 };

class Subtitles {
  // `avatar`: the AvatarWindow (her window's bounds and whether she's at home). `herBody()`: her body's rectangle
  // while she's away (pointing, perched, on a lap), whose window then also holds her arm and what she points at.
  // `pointedAt()`: the spot she's pointing at (DIP, screen) or null; the words go on her other side.
  constructor(cfg, { avatar, herBody, pointedAt, screen, url, preload, log, now = Date.now }) {
    this.cfg = cfg;
    this.avatar = avatar;
    this.herBody = herBody;
    this.pointedAt = pointedAt;
    this.screen = screen;
    this.url = url;
    this.preload = preload;
    this.log = log;
    this.win = null;
    this.pageReady = false; // the page has its listeners up (it said hello); until then messages wait in `queued`
    this.queued = [];
    this.layout = null; // what the page was last told
    this.showing = false; // a line is up (or fading)
    this.seq = 0; // counts lines: the page's "faded" names the last line it had, so a stale one hides nothing
    this.hideTimer = null;
    this.backstop = null;
    this.closed = false;
    this.now = now;
    this.mood = null; // {emotion, intensity, until}: what she was left feeling, for the next reply's lines
    // Each sentence's audio length by its place in the reply ('segment-audio'), for pacing its words: it often comes
    // before the sentence starts playing. Cleared when the reply ends.
    this.lengths = new Map();
    this.voiceAt = -Infinity; // when her voice level was last passed on
    this.pointerTimer = null;
    this.pointerIn = false; // the cursor was in the window at the last poll
    this.onMoved = () => this.showing && this.place();
    this.onDisplays = () => this.showing && this.place();
    // Home again (or off): her window may have stopped moving before she counted as home.
    if (avatar) avatar.onAtHome = this.onMoved;
    const herWin = avatar && avatar.win;
    if (herWin) for (const event of ['move', 'resize']) herWin.on(event, this.onMoved);
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) screen.on(event, this.onDisplays);
  }

  // ------------------------------------------------------------------ messages

  handle(msg) {
    if (this.closed) return;
    if (msg.type === 'mouth') {
      // Dropped, not queued, before the page listens: only the latest level matters. At most VOICE_HZ, as the page
      // follows it no faster (a silence always goes, so she never stays lit).
      const level = Number(msg.level) || 0;
      const t = this.now();
      if (!this.showing || !this.pageReady || (t - this.voiceAt < 1000 / VOICE_HZ && level > 0)) return;
      this.voiceAt = t;
      this.send('subtitles:voice', level);
    } else if (msg.type === 'emotion' || msg.type === 'mood') {
      const seconds = Number(msg.type === 'mood' ? msg.seconds : msg.hold);
      if (msg.type === 'mood' && seconds > 0) this.mood = { emotion: msg.emotion, intensity: Number(msg.intensity), until: this.now() + seconds * 1000 };
      if (this.showing) this.send('subtitles:feel', { emotion: msg.emotion, intensity: Number(msg.intensity), seconds: seconds > 0 ? seconds : null });
    } else if (msg.type === 'segment') {
      if (typeof msg.subtitle === 'string' && msg.subtitle.trim()) {
        const hold = Number(msg.hold);
        this.line(msg.subtitle, Number.isFinite(hold) && hold > 0 ? hold : null, msg);
      } else if (this.showing) {
        this.end(true); // a sentence with no subtitle (the setting's off, or a panel shows it): the last one goes
      }
    } else if (msg.type === 'segment-audio') {
      const index = Number(msg.index);
      const seconds = Number(msg.seconds);
      if (!Number.isInteger(index) || !(seconds > 0)) return;
      if (this.lengths.size >= 64) this.lengths.delete(this.lengths.keys().next().value); // the oldest: never grows
      this.lengths.set(index, seconds);
      if (this.showing && this.pageReady) this.send('subtitles:length', { index, seconds });
    } else if (msg.type === 'segment-word') {
      // She's reached a word, by her audio (her word times): only for the sentence up, which the page checks.
      if (this.showing && this.pageReady) this.send('subtitles:word', { index: Number(msg.index), word: Number(msg.word), of: Number(msg.of) });
    } else if (msg.type === 'subtitles-end') {
      this.lengths.clear();
      if (this.showing) this.end(msg.interrupted === true);
    } else if (msg.type === 'hide') {
      if (this.showing) this.end(false, LEAVE_MS[this.cfg.form] || LEAVE_MS.orb);
    }
  }

  // `said`: the segment, for the feeling it carries.
  line(text, holdS, said = {}) {
    this.ensureWindow();
    if (!this.win) return;
    clearTimeout(this.hideTimer);
    this.hideTimer = null;
    this.showing = true;
    this.place();
    this.seq += 1;
    const payload = { text: String(text), hold: holdS, seq: this.seq, emotion: said.emotion || null, intensity: Number(said.intensity) || 0 };
    // For pacing its words: what her voice says of it, its place in the reply and, if known yet, its audio's length.
    if (typeof said.text === 'string') payload.said = said.text;
    if (Number.isInteger(said.index)) {
      payload.index = said.index;
      if (this.lengths.has(said.index)) payload.seconds = this.lengths.get(said.index);
      this.lengths.delete(said.index);
    }
    if (this.mood && this.mood.until > this.now()) {
      payload.mood = { emotion: this.mood.emotion, intensity: this.mood.intensity, seconds: (this.mood.until - this.now()) / 1000 };
    } else this.mood = null;
    this.send('subtitles:line', payload);
    if (!this.win.isVisible()) this.win.showInactive();
    this.win.setAlwaysOnTop(true, 'screen-saver'); // back over anything that went topmost since
    this.startPointer();
    clearTimeout(this.backstop);
    this.backstop = setTimeout(() => this.end(false), BACKSTOP_MS);
  }

  // `leavingMs`: she's leaving, and the words fade now over this long, with her, rather than after reading time.
  end(interrupted, leavingMs = 0) {
    clearTimeout(this.backstop);
    this.backstop = null;
    if (!this.win || this.win.isDestroyed()) return;
    this.send('subtitles:end', leavingMs > 0 ? { interrupted, leavingMs } : { interrupted });
    clearTimeout(this.hideTimer);
    this.hideTimer = setTimeout(() => this.hide(), FADED_WAIT_MS + (interrupted ? 0 : 6000));
  }

  // The page has faded out after an end: nothing to show, so the window goes (a hidden window costs nothing to draw).
  faded(sender, seq) {
    if (!this.win || this.win.isDestroyed() || sender !== this.win.webContents) return;
    if (seq !== this.seq) return; // a line came since: it's showing, not faded
    this.hide();
  }

  hide() {
    clearTimeout(this.hideTimer);
    this.hideTimer = null;
    this.showing = false;
    this.stopPointer();
    if (this.win && !this.win.isDestroyed() && this.win.isVisible()) this.win.hide();
  }

  // ------------------------------------------------------------------ the pointer

  // While a line is up the page hears where the cursor is (window coordinates), as her own window does: a window
  // letting every click through hears no mouse events of its own. Only while it's in the window, and once as it
  // leaves.
  startPointer() {
    if (this.pointerTimer || typeof this.screen.getCursorScreenPoint !== 'function') return;
    this.pointerTimer = setInterval(() => this.pointerTick(), POINTER_MS);
    if (this.pointerTimer.unref) this.pointerTimer.unref(); // never what keeps the app (or a test) running
  }

  stopPointer() {
    clearInterval(this.pointerTimer);
    this.pointerTimer = null;
    if (this.pointerIn) this.send('subtitles:pointer', { x: -1, y: -1, inside: false });
    this.pointerIn = false;
  }

  pointerTick() {
    if (!this.win || this.win.isDestroyed() || !this.showing || !this.pageReady) return;
    const p = this.screen.getCursorScreenPoint();
    const b = this.win.getBounds();
    const inside = p.x >= b.x && p.x < b.x + b.width && p.y >= b.y && p.y < b.y + b.height;
    if (!inside && !this.pointerIn) return;
    this.pointerIn = inside;
    this.send('subtitles:pointer', { x: p.x - b.x, y: p.y - b.y, inside });
  }

  // ------------------------------------------------------------------ where

  // Where she is: her window at home; her body while she's away, since her window then spans her arm and the spot.
  herRect() {
    const a = this.avatar;
    if (a && a.atHome !== false) return a.bounds;
    return (this.herBody && this.herBody()) || (a && a.bounds);
  }

  place() {
    if (!this.win || this.win.isDestroyed()) return;
    const rect = this.herRect();
    const centre = { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
    const area = this.screen.getDisplayNearestPoint(centre).workArea;
    const away = Boolean(this.avatar) && this.avatar.atHome === false;
    const placed = placeSubtitles(rect, area, this.cfg.form, undefined, (away && this.pointedAt && this.pointedAt()) || null);
    const b = this.win.getBounds();
    const want = placed.bounds;
    if (b.x !== want.x || b.y !== want.y || b.width !== want.width || b.height !== want.height) {
      if (b.width !== want.width || b.height !== want.height) {
        this.win.setResizable(true); // some Windows builds won't resize a window that isn't resizable
        this.win.setBounds(want);
        this.win.setResizable(false);
      } else this.win.setPosition(want.x, want.y);
    }
    const layout = { anchor: placed.anchor, flipX: placed.flipX, flipY: placed.flipY };
    if (JSON.stringify(layout) !== JSON.stringify(this.layout)) {
      this.layout = layout;
      this.send('subtitles:layout', layout);
    }
  }

  // ------------------------------------------------------------------ the window

  ensureWindow() {
    if (this.win && !this.win.isDestroyed()) return;
    this.layout = null;
    this.pageReady = false;
    this.queued = [];
    try {
      this.win = new BrowserWindow({
        x: 0,
        y: 0,
        width: 560,
        height: 250,
        show: false,
        title: 'subtitles',
        transparent: true,
        frame: false,
        backgroundColor: '#00000000',
        hasShadow: false,
        resizable: false,
        maximizable: false,
        minimizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        focusable: false, // reading her words must never take the keyboard from what the user is typing into
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
    } catch (err) {
      this.log(`could not make the subtitles window: ${err.message}`);
      this.win = null;
      return;
    }
    this.win.setAlwaysOnTop(true, 'screen-saver');
    if (this.cfg.hideFromCapture) this.win.setContentProtection(true); // out of her own looks at the screen
    this.win.setIgnoreMouseEvents(true); // wholly click-through: words to read, never in the way of the mouse
    this.win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    this.win.webContents.on('will-navigate', (e) => e.preventDefault());
    this.win.webContents.on('did-start-loading', () => {
      this.pageReady = false;
    });
    this.win.webContents.on('render-process-gone', () => {
      if (this.win && !this.win.isDestroyed()) this.win.destroy();
      this.win = null;
      this.showing = false;
    });
    this.win.loadURL(this.url).catch((err) => this.log(`subtitles page failed to load: ${err.message}`));
  }

  owns(sender) {
    return Boolean(this.win && !this.win.isDestroyed() && sender === this.win.webContents);
  }

  send(channel, payload) {
    if (!this.win || this.win.isDestroyed()) return;
    if (!this.pageReady) {
      // A message sent before the page listens is lost: it waits (only the latest layout matters).
      if (channel === 'subtitles:layout') this.queued = this.queued.filter(([c]) => c !== channel);
      this.queued.push([channel, payload]);
      return;
    }
    this.win.webContents.send(channel, payload);
  }

  // The page has loaded and listens: what waited goes to it now, the layout first.
  hello(sender) {
    if (!this.owns(sender)) return;
    this.pageReady = true;
    const queued = this.queued.splice(0);
    if (this.layout && !queued.some(([c]) => c === 'subtitles:layout')) queued.unshift(['subtitles:layout', this.layout]);
    for (const [channel, payload] of queued) this.win.webContents.send(channel, payload);
  }

  // Debug (tools/subtitles-demo.mjs): what the window shows, to a PNG (blank while content protection is on).
  async capture(file) {
    if (!this.win || this.win.isDestroyed()) throw new Error('no subtitles window');
    if (typeof file !== 'string' || !path.isAbsolute(file)) throw new Error('capture needs an absolute path');
    const image = await this.win.webContents.capturePage();
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, image.toPNG());
  }

  destroy() {
    this.closed = true;
    if (this.avatar && this.avatar.onAtHome === this.onMoved) this.avatar.onAtHome = null;
    clearTimeout(this.hideTimer);
    clearTimeout(this.backstop);
    this.stopPointer();
    const herWin = this.avatar && this.avatar.win;
    if (herWin && !herWin.isDestroyed()) for (const event of ['move', 'resize']) herWin.removeListener(event, this.onMoved);
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) this.screen.removeListener(event, this.onDisplays);
    if (this.win && !this.win.isDestroyed()) this.win.destroy();
    this.win = null;
  }
}

module.exports = { Subtitles, BACKSTOP_MS, LEAVE_MS };
