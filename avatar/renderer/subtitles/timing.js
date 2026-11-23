// Her subtitles' timing and geometry, with no DOM in it (tested in node): how long a line stays after she's done,
// and where the underline, line and text sit around the anchor.

export const CONNECTOR = 34; // the diagonal line's run, each way (px)
export const TEXT_GAP = 8; // between the text and the underline, which lies along the bottom rim of the box round the words
export const EDGE = 6; // the window's edge the text keeps clear of
export const TEXT_MAX_WIDTH = 420; // two short rows of words, not a page
export const MAX_LINES = 2; // the sentence being said, and the one before it (dimmed)

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// How long the last line stays up after she's finished speaking, before it fades: long enough to finish reading it.
export function tailMs(text) {
  return clamp(1400 + 30 * String(text).length, 2200, 5000);
}


// Where things go in a window `width` x `height` for an `anchor` (window coordinates, the spot on her the line runs
// to) on side `flipX` (text to her right) / `flipY` (text below her). `u` is where the line meets the underline; the
// text box hangs from it (its right edge at u unless flipped) and may be at most `maxWidth` x `maxHeight`.
export function geometry(width, height, anchor, flipX = false, flipY = false) {
  const u = { x: anchor.x + (flipX ? CONNECTOR : -CONNECTOR), y: anchor.y + (flipY ? CONNECTOR : -CONNECTOR) };
  const maxWidth = Math.max(80, Math.min(TEXT_MAX_WIDTH, flipX ? width - u.x - EDGE : u.x - EDGE));
  const maxHeight = Math.max(40, flipY ? height - (u.y + TEXT_GAP) - EDGE : u.y - TEXT_GAP - EDGE);
  return { u, maxWidth, maxHeight };
}
