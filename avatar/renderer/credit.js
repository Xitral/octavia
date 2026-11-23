// The model's credit ("my oc by amia": her licence asks for it), shown like a tooltip: a small label by the
// pointer once it has rested on her for a moment, and only while she stands at home.
//
// It's drawn in the page rather than as the canvas's title tooltip. Chromium shows that one in a little
// window of its own, and when she flew off to point, her window moved out from under a still pointer and
// the tooltip was left behind on the desktop, over whatever app was there (content protection doesn't
// cover it either). This label is part of her window, so it moves and hides with her.

export const CREDIT_DELAY = 0.6; // s the pointer rests on her before the credit shows, like a tooltip
const OFFSET = { x: 12, y: 18 }; // px from the pointer to the label, clear of the cursor's arrow
const EDGE = 4; // px kept free at the window's edges

export class Credit {
  // `element`: the label (index.html's #credit). `text`: the credit; empty means there's none to show.
  constructor(element, text) {
    this.element = element;
    this.text = typeof text === 'string' ? text.trim() : '';
    this.element.textContent = this.text;
    this.wait = 0; // s the pointer has been on her
    this.shown = false;
    this.dismissed = false; // a press hid it: not again until the pointer has left her
  }

  // Every frame. `over`: the pointer is on her pixels. `home`: she's standing at home (not flying out there,
  // pointing, arriving or leaving). `x`, `y`: the pointer; `width`, `height`: the window (CSS px).
  update(dt, { over, home, x, y, width, height }) {
    if (!over) this.dismissed = false;
    if (!this.text || !over || !home || this.dismissed) {
      this.hide();
      return;
    }
    if (this.shown) return; // a tooltip stays where it came up
    this.wait += dt;
    if (this.wait >= CREDIT_DELAY) this.show(x, y, width, height);
  }

  // Below and right of the pointer like a tooltip; above it or further left where the window ends.
  show(x, y, width, height) {
    const w = this.element.offsetWidth;
    const h = this.element.offsetHeight;
    const fit = (v, size, room) => Math.max(EDGE, Math.min(v, room - EDGE - size));
    const below = y + OFFSET.y;
    const top = below + h <= height - EDGE ? below : y - EDGE - h;
    this.element.style.left = `${Math.round(fit(x + OFFSET.x, w, width))}px`;
    this.element.style.top = `${Math.round(fit(top, h, height))}px`;
    this.element.classList.add('shown');
    this.shown = true;
  }

  // Hides it at once and starts the wait over: the pointer left her, or she's moving or going.
  hide() {
    this.wait = 0;
    if (!this.shown) return;
    this.shown = false;
    this.element.classList.remove('shown');
  }

  // A press on her (a click to talk, or a drag): out of the way until the pointer has left her and come
  // back, like a native tooltip.
  dismiss() {
    this.dismissed = true;
    this.hide();
  }
}
