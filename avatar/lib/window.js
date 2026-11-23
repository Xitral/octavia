'use strict';
// The avatar's desktop window: transparent, frameless, always on top, never focused, and click-through
// everywhere except over the character's own pixels with Ctrl held (the renderer decides: seethrough.js).

const fs = require('node:fs/promises');
const path = require('node:path');
const { BrowserWindow, screen } = require('electron');
const { readSavedPosition, savePosition } = require('./config');
const { keepInside } = require('./layout');

const MARGIN = 16;
const CURSOR_FEED_MS = 50; // ~20 Hz is plenty for eyes following the mouse
const DRAG_STEP_MS = 16;
const DRAG_THRESHOLD_PX = 4; // less movement than this between press and release is a click
const CLICK_MAX_MS = 700;

function defaultPosition(width, height) {
  const wa = screen.getPrimaryDisplay().workArea;
  return { x: wa.x + wa.width - width - MARGIN, y: wa.y + wa.height - height - MARGIN };
}

// A saved position from a monitor that is no longer attached would put the avatar off screen.
function onSomeDisplay(pos, width, height) {
  const cx = pos.x + width / 2;
  const cy = pos.y + height / 2;
  return screen.getAllDisplays().some(({ bounds: b }) => cx >= b.x && cx < b.x + b.width && cy >= b.y && cy < b.y + b.height);
}

class AvatarWindow {
  constructor(cfg, { url, preload, onEmit, log }) {
    this.cfg = cfg;
    this.onEmit = onEmit;
    this.log = log;
    this.ignoring = true;
    this.drag = null;
    this.cursorTimer = null;
    // The user can drag her around only at home. While she's away (flying, or pointing at something) a press
    // is still a click, but it never moves her, and her saved home spot only ever comes from a drag at home.
    this.atHome = true;

    // Windows won't make a window taller than the screen; shrink both sides so the framing still fits.
    const maxHeight = screen.getPrimaryDisplay().workArea.height;
    const height = Math.min(cfg.height, maxHeight);
    const width = Math.round((cfg.width * height) / cfg.height);
    const wanted = cfg.position || readSavedPosition(cfg.statePath);
    let pos = wanted && onSomeDisplay(wanted, width, height) ? wanted : defaultPosition(width, height);
    if (cfg.form === 'orb') {
      // The orb is all there is in its window: none of it may hang off the screen, as the character's may.
      const area = screen.getDisplayNearestPoint({ x: Math.round(pos.x + width / 2), y: Math.round(pos.y + height / 2) }).workArea;
      pos = keepInside(pos, { width, height }, area);
    }

    this.win = new BrowserWindow({
      x: pos.x,
      y: pos.y,
      width,
      height,
      useContentSize: true,
      show: false,
      title: cfg.credit,
      transparent: true,
      frame: false,
      backgroundColor: '#00000000',
      hasShadow: false,
      resizable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      // Popping up must never take keyboard focus away from whatever the user is typing into.
      focusable: false,
      alwaysOnTop: true,
      webPreferences: {
        preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,
        spellcheck: false,
      },
    });
    // 'screen-saver', not 'floating': on Windows Electron puts 'floating' behind the taskbar, under every other
    // always-on-top window (a picture-in-picture video docked in the same corner hid her).
    this.win.setAlwaysOnTop(true, 'screen-saver');
    // Out of screenshots (the assistant's own look at the screen must see what's under her, not her), and out of
    // screen recordings and shares too. Windows 10 2004+; older builds would paint a black box instead.
    if (cfg.hideFromCapture) this.win.setContentProtection(true);
    this.win.setIgnoreMouseEvents(true, { forward: true });
    // The renderer never navigates or opens windows; refuse anything that tries.
    this.win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    this.win.webContents.on('will-navigate', (e) => e.preventDefault());
    this.win.loadURL(url);
  }

  get webContents() {
    return this.win.webContents;
  }

  get visible() {
    return !this.win.isDestroyed() && this.win.isVisible();
  }

  // DIP, screen. The window is frameless, so these are also the content bounds.
  get bounds() {
    return this.win.getBounds();
  }

  // Moves and resizes the window in one go (DIP, screen).
  setBounds(bounds) {
    if (this.win.isDestroyed()) return;
    const same = (b) => b.x === bounds.x && b.y === bounds.y && b.width === bounds.width && b.height === bounds.height;
    this.win.setBounds(bounds);
    if (!same(this.win.getBounds())) {
      // It's made non-resizable so the user can't resize it, and some Windows builds then won't let a program
      // resize it either; crossing onto a monitor with another scale can also come out wrong the first time.
      // One more go, allowed to resize.
      this.win.setResizable(true);
      this.win.setBounds(bounds);
      this.win.setResizable(false);
      const got = this.win.getBounds();
      if (!same(got)) this.log(`window is ${JSON.stringify(got)}, wanted ${JSON.stringify(bounds)}`);
    }
    this.win.setAlwaysOnTop(true, 'screen-saver'); // back on top of anything that went topmost since
  }

  // Where home is: `home` (DIP bounds) while its centre is still on some monitor, else the default spot at
  // the same size (a monitor went away while she was out).
  homeBounds(home) {
    const { width, height } = home;
    if (onSomeDisplay(home, width, height)) return { ...home };
    return { ...defaultPosition(width, height), width, height };
  }

  setAtHome(atHome) {
    this.atHome = atHome;
    if (this.onAtHome) this.onAtHome(atHome); // her subtitles: placed by her window again, or by her body
  }

  send(channel, payload) {
    if (!this.win.isDestroyed()) this.win.webContents.send(channel, payload);
  }

  show() {
    if (this.win.isDestroyed()) return;
    if (!this.win.isVisible()) this.win.showInactive();
    this.win.setAlwaysOnTop(true, 'screen-saver'); // back on top of anything that went topmost since
    this.startCursorFeed();
  }

  hide() {
    this.endDrag(false);
    this.stopCursorFeed();
    this.setIgnoreMouse(true);
    if (!this.win.isDestroyed()) this.win.hide();
  }

  setIgnoreMouse(ignore) {
    if (this.drag || this.win.isDestroyed() || ignore === this.ignoring) return;
    this.ignoring = ignore;
    this.win.setIgnoreMouseEvents(ignore, { forward: true });
  }

  // The renderer wants the cursor even when it is outside the window, so the eyes can follow it.
  startCursorFeed() {
    if (this.cursorTimer) return;
    this.cursorTimer = setInterval(() => {
      if (this.win.isDestroyed() || !this.win.isVisible()) return;
      const p = screen.getCursorScreenPoint();
      const b = this.win.getContentBounds();
      const inside = p.x >= b.x && p.x < b.x + b.width && p.y >= b.y && p.y < b.y + b.height;
      // Forwarded mouse events stop once the cursor leaves, so the renderer may never hear about it.
      if (!inside) this.setIgnoreMouse(true);
      this.send('avatar:cursor', { x: p.x - b.x, y: p.y - b.y, inside });
    }, CURSOR_FEED_MS);
  }

  stopCursorFeed() {
    clearInterval(this.cursorTimer);
    this.cursorTimer = null;
  }

  // Dragging follows the global cursor from the main process, so a fast flick that outruns the window
  // can't drop the drag the way renderer-side mouse deltas would.
  startDrag() {
    if (this.drag || this.win.isDestroyed()) return;
    this.setIgnoreMouse(false);
    const [x, y] = this.win.getPosition();
    const drag = { cursor0: screen.getCursorScreenPoint(), win0: { x, y }, t0: Date.now(), moved: false, atHome: this.atHome };
    drag.timer = setInterval(() => {
      const c = screen.getCursorScreenPoint();
      const dx = c.x - drag.cursor0.x;
      const dy = c.y - drag.cursor0.y;
      if (!drag.moved && Math.hypot(dx, dy) >= DRAG_THRESHOLD_PX) drag.moved = true;
      if (drag.moved && drag.atHome && !this.win.isDestroyed()) this.win.setPosition(drag.win0.x + dx, drag.win0.y + dy);
    }, DRAG_STEP_MS);
    this.drag = drag;
  }

  // `click: false` when the drag is cut short by something else (she's about to fly off): wherever a drag
  // at home got to still counts, but a press that hadn't moved yet isn't a click.
  endDrag(report = true, { click = true } = {}) {
    const drag = this.drag;
    if (!drag) return;
    clearInterval(drag.timer);
    this.drag = null;
    if (!report || this.win.isDestroyed()) return;
    if (drag.moved && drag.atHome) {
      let [x, y] = this.win.getPosition();
      if (this.cfg.form === 'orb') {
        // Dropped half off the screen: she settles back onto it, and that's the spot saved.
        const [width, height] = this.win.getSize();
        const area = screen.getDisplayNearestPoint({ x: Math.round(x + width / 2), y: Math.round(y + height / 2) }).workArea;
        ({ x, y } = keepInside({ x, y }, { width, height }, area));
        this.win.setPosition(x, y);
      }
      try {
        savePosition(this.cfg.statePath, { x, y });
      } catch (err) {
        this.log(`could not save position: ${err.message}`);
      }
      this.onEmit({ type: 'moved', x, y });
    } else if (click && !drag.moved && Date.now() - drag.t0 <= CLICK_MAX_MS) {
      this.onEmit({ type: 'click' });
    }
  }

  async capture(file) {
    if (typeof file !== 'string' || !path.isAbsolute(file)) throw new Error('capture needs an absolute path');
    const image = await this.win.webContents.capturePage();
    if (image.isEmpty()) throw new Error('capture is empty (is the window shown?)');
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, image.toPNG());
  }

  destroy() {
    this.endDrag(false);
    this.stopCursorFeed();
    if (!this.win.isDestroyed()) this.win.destroy();
  }
}

module.exports = { AvatarWindow };
