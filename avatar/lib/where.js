'use strict';
// Where she is across the main monitor, for Octavia to play her voice from there (src/octavia/audio/spatial.py):
// {"type":"where","x":0.82} is -1 at the main monitor's left edge, 1 at its right edge, 0 in its middle, and stays at
// the edge past it (she's on another monitor). Her window at home; her body while she's away (flying, pointing,
// perched), since her window then spans her arm and the spot too. Looked at a few times a second and sent only when
// it moved a little, so it costs nothing while she sits still.

const POLL_MS = 150;
const STEP = 0.01; // a change smaller than this isn't heard: not sent

// `rect` (DIP, screen) across `primary` (the main monitor's bounds): -1..1.
function across(rect, primary) {
  const half = primary && primary.width / 2;
  if (!rect || !(half > 0)) return 0;
  const x = (rect.x + rect.width / 2 - (primary.x + half)) / half;
  if (!Number.isFinite(x)) return 0;
  return Math.round(Math.max(-1, Math.min(1, x)) * 1000) / 1000;
}

class Whereabouts {
  // `herRect()`: where she is (DIP, screen), or null. `screen`: Electron's (getPrimaryDisplay). `emit`: to Octavia.
  constructor({ herRect, screen, emit, every = POLL_MS }) {
    this.herRect = herRect;
    this.screen = screen;
    this.emit = emit;
    this.every = every;
    this.last = null;
    this.timer = null;
  }

  start() {
    if (this.timer) return;
    this.last = null; // a new start: say where she is once, whatever was said before
    this.tick();
    this.timer = setInterval(() => this.tick(), this.every);
  }

  tick() {
    let x;
    try {
      const rect = this.herRect();
      if (!rect) return;
      x = across(rect, this.screen.getPrimaryDisplay().bounds);
    } catch {
      return; // her window is gone (quitting): nothing to say
    }
    if (this.last !== null && Math.abs(x - this.last) < STEP) return;
    this.last = x;
    this.emit({ type: 'where', x });
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  }
}

module.exports = { Whereabouts, across, POLL_MS, STEP };
