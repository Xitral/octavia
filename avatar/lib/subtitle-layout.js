'use strict';
// Where her subtitles go: a window beside her, its text up and to the left of the orb with a thin line running down
// to the orb's edge (docs/subtitles.md). Pure geometry, no Electron, so it can be unit-tested. DIP throughout.
//
// The window is placed so that the line's end (the anchor) touches her: the orb's upper-left edge, or near the
// character's head. Without room on the left (she's at the left edge of a monitor) it goes on her right; without
// room above, below her. While she points, it goes on her side away from the spot, so the words never cover it. It never leaves the work area of the monitor she's on, and the anchor is given in window
// coordinates, so the line still meets her wherever the window had to shift.

const WIDTH = 560;
const HEIGHT = 280;
const ORB_RADIUS = 0.18; // renderer/orb-motion.js: of the smaller side of her window
const GAP = 10; // past the orb's edge: the line stops at her glow, not inside it
const INSET = 44; // the anchor's distance from the window's corner: room for the dust of light round her edge
const DIAG = Math.SQRT1_2;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// The spot on her the line runs to (DIP, screen), for her window `rect` and her `form`, on the side `flipX` (right of
// her) / `flipY` (below her).
function anchorOn(rect, form, flipX, flipY) {
  const sx = flipX ? 1 : -1;
  const sy = flipY ? 1 : -1;
  if (form === 'orb') {
    const r = ORB_RADIUS * Math.min(rect.width, rect.height) + GAP;
    return { x: rect.x + rect.width / 2 + sx * r * DIAG, y: rect.y + rect.height / 2 + sy * r * DIAG };
  }
  // The character: beside her head, at the edge of her figure (she stands in the middle third of her window).
  return { x: rect.x + rect.width / 2 + sx * rect.width * 0.18, y: rect.y + rect.height * (flipY ? 0.42 : 0.16) };
}

// `rect`: her window, or her body while she's away (DIP, screen). `area`: the work area of the monitor she's on.
// `avoid`: the spot she's pointing at (DIP, screen), or null. Returns the subtitle window's bounds, the anchor in
// window coordinates, and which way it was flipped.
function placeSubtitles(rect, area, form = 'orb', size = { width: WIDTH, height: HEIGHT }, avoid = null) {
  const { width, height } = size;
  const roomLeft = anchorOn(rect, form, false, false).x - area.x >= width * 0.75;
  const roomRight = area.x + area.width - anchorOn(rect, form, true, false).x >= width * 0.75;
  const spotLeft = Boolean(avoid) && avoid.x < rect.x + rect.width / 2;
  const flipX = spotLeft ? roomRight || !roomLeft : !roomLeft; // her left, unless there's no room or it's the spot's side
  const flipY = anchorOn(rect, form, flipX, false).y - area.y < height * 0.6;
  const a = anchorOn(rect, form, flipX, flipY);
  // The anchor sits INSET from the corner nearest her.
  const ax = flipX ? INSET : width - INSET;
  const ay = flipY ? INSET : height - INSET;
  const x = Math.round(clamp(a.x - ax, area.x, area.x + area.width - width));
  const y = Math.round(clamp(a.y - ay, area.y, area.y + area.height - height));
  return {
    bounds: { x, y, width, height },
    anchor: { x: Math.round(a.x - x), y: Math.round(a.y - y) },
    flipX,
    flipY,
  };
}

module.exports = { placeSubtitles, anchorOn, WIDTH, HEIGHT };
