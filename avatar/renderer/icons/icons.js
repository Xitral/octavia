// Filled Tabler icons as inline SVG (tabler.io/icons, MIT: LICENSE-tabler.txt), drawn in the colour of the text
// around them (currentColor), so the panel's and a live block's own CSS tint them. Nothing is fetched: the path data
// is bundled here (chrome.js, tabler-filled.js; made by avatar/tools/tabler-icons.mjs).

const NS = 'http://www.w3.org/2000/svg';

// An <svg> of `paths` (one icon's path data, a 24 x 24 box). `size`: its width and height in CSS pixels, or null to
// leave them to CSS (1em by default, set in the stylesheet that uses it).
export function iconSvg(paths, { size = null, color = null, className = 'icon' } = {}) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'currentColor');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  if (className) svg.setAttribute('class', className);
  if (Number.isFinite(size) && size > 0) {
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
  }
  if (color) svg.style.color = color;
  for (const d of paths || []) {
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

// Puts icon `paths` into `el` in place of what it shows now (a button's text glyph), keeping its title.
export function setIcon(el, paths) {
  if (!el) return;
  el.replaceChildren(iconSvg(paths));
}
