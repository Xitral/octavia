// With the pointer on her she fades and lets clicks and scrolls through to whatever is under her, so she never
// gets in the way. Holding Ctrl brings her back: full strength, taking the mouse again (click to talk, drag).
//
// The window never has the keyboard, so Ctrl comes from Octavia: the renderer says when the pointer comes onto
// her and leaves ('hover'), and Octavia, watching Ctrl only in between, says when it goes down or up (`keys`).
// The pointer is still followed while the window lets the mouse through: forwarded moves and the main
// process's cursor feed keep coming.
//
// Once on her, the pointer stays on her while it's anywhere in her window, moving or not: the pixel under it
// slipping into a gap (between an arm and her side, through her hair) or a stray leave event from the window
// switching to click-through must never bring her back mid-hover. Only the main process's cursor poll against
// the window's rectangle says it left, since a window letting clicks through hears no reliable mouse events;
// and she comes back a moment later (LEAVE_MS), so brushing past an edge doesn't flicker her.

export const LEAVE_MS = 300;

export class SeeThrough {
  // host: { setIgnoreMouse(ignore), emit(msg) }. fade(on): fades her out (true) or back to full strength (false).
  constructor(host, fade) {
    this.host = host;
    this.fade = fade;
    this.over = false; // the pointer is on her (follow decides)
    this.ctrl = false; // Ctrl is held, as Octavia last said; only counts while over
    this.dragging = false;
    this.faded = false;
    this.seenInside = -Infinity; // when the pointer was last seen in her window (ms)
  }

  // Whether the pointer counts as on her now (ms): `onHer`, it's on her pixels; `inside`, it's in her window, as
  // the cursor poll last saw it. Coming on takes her pixels; staying on takes only the window, or having left it
  // under LEAVE_MS ago. Feed the answer to setOver.
  follow(onHer, inside, now) {
    if (inside) this.seenInside = now;
    if (this.over) return inside || now - this.seenInside < LEAVE_MS;
    return onHer && inside;
  }

  // She takes the mouse only with Ctrl held, or while a drag that started so is under way.
  get takesMouse() {
    return this.over && (this.ctrl || this.dragging);
  }

  setOver(over) {
    if (over === this.over) return;
    this.over = over;
    if (!over) this.ctrl = false; // Octavia stops watching; the next hover starts afresh
    this.host.emit({ type: 'hover', over });
    this.apply();
  }

  setCtrl(held) {
    const ctrl = held === true && this.over;
    if (ctrl === this.ctrl) return;
    this.ctrl = ctrl;
    this.apply();
  }

  setDragging(dragging) {
    if (dragging === this.dragging) return;
    this.dragging = dragging;
    this.apply();
  }

  apply() {
    const takes = this.takesMouse;
    const faded = this.over && !takes;
    if (faded !== this.faded) {
      this.faded = faded;
      this.fade(faded);
    }
    this.host.setIgnoreMouse(!takes);
  }
}
