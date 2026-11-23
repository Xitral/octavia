'use strict';
// Where a new panel goes (all DIP, screen). Panels stack down a column from an anchor, the top-right corner of the
// first one (by default the top right of the work area; where the user last dragged one, once they have). A panel
// that doesn't fit under the others in a column starts the next column to the left; when no column has room it
// goes at the anchor, over the oldest.

const MARGIN = 16; // from the work area's edges
const GAP = 12; // between panels
const TOP = 0.06; // the default anchor sits this far down the work area, clear of title bars along the top

function defaultAnchor(workArea) {
  return { right: workArea.x + workArea.width - MARGIN, top: Math.round(workArea.y + workArea.height * TOP) };
}

// An anchor kept inside the work area, so a saved spot from a bigger monitor still opens on screen.
function clampAnchor(anchor, workArea, width) {
  const minRight = workArea.x + width + MARGIN;
  const maxRight = workArea.x + workArea.width - MARGIN;
  const right = Math.min(Math.max(anchor.right, minRight), maxRight);
  const top = Math.min(Math.max(anchor.top, workArea.y + MARGIN), workArea.y + workArea.height - MARGIN - 120);
  return { right: Math.round(right), top: Math.round(top) };
}

const overlapsX = (a, left, right) => a.x < right && a.x + a.width > left;

// `open`: bounds of the panels already on screen. Returns {x, y} for a panel of `width` x `height`.
function place({ workArea, open = [], width, height, anchor = null }) {
  const start = clampAnchor(anchor || defaultAnchor(workArea), workArea, width);
  const bottom = workArea.y + workArea.height - MARGIN;
  for (let right = start.right; right - width >= workArea.x + MARGIN; right -= width + GAP) {
    const left = right - width;
    const below = open
      .filter((b) => overlapsX(b, left, right))
      .reduce((lowest, b) => Math.max(lowest, b.y + b.height + GAP), start.top);
    if (below + height <= bottom) return { x: left, y: below };
  }
  return { x: start.right - width, y: start.top };
}

// The tallest a panel may be on this work area (it scrolls inside past this).
function maxHeight(workArea) {
  return Math.round(workArea.height * 0.8);
}

module.exports = { place, maxHeight, defaultAnchor, clampAnchor, MARGIN, GAP };
