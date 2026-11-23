// A simulation block: a picture with sliders under it. Every frame came with the spec (worked out by Octavia,
// src/octavia/study/sims), so a drag only picks the frame for where the sliders are and swaps the picture, its
// caption and its numbers; nothing is asked of her. The picture's box keeps one size (the spec's `aspect`) so the
// panel never changes height while a slider moves. Pictures are shown as images, like any drawing of hers.

// The frame for these slider positions: `index` lists one frame per mix of positions, the first slider slowest.
export function frameAt(block, positions) {
  const params = block.params || [];
  let at = 0;
  params.forEach((p, k) => {
    const count = (p.values || []).length;
    const pos = Math.min(Math.max(0, Math.round(Number(positions[k]) || 0)), Math.max(0, count - 1));
    at = at * count + pos;
  });
  const frame = (block.index || [])[at];
  return Number.isInteger(frame) && frame >= 0 && frame < (block.frames || []).length ? frame : 0;
}

export function startPositions(block) {
  return (block.params || []).map((p) => {
    const start = Number(p.start) || 0;
    return Math.min(Math.max(0, start), Math.max(0, (p.values || []).length - 1));
  });
}

const svgUrl = (svg) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(svg || ''))}`;

// `ui` is the page's own helpers ({ h, inline, parseInline }), so every word goes in as text.
export function buildSim(block, ui) {
  const { h, inline, parseInline } = ui;
  const frames = block.frames || [];
  if (!frames.length) return null;
  const positions = startPositions(block);
  const urls = frames.map((f) => svgUrl(f.svg));

  const img = new Image();
  img.alt = String(block.alt || 'a simulation');
  img.className = 'sim-picture';
  const stage = h('div', 'sim-stage', [img]);
  stage.style.setProperty('--aspect', String(Math.min(4, Math.max(0.05, Number(block.aspect) || 0.6))));
  const caption = h('div', 'sim-caption');
  const facts = h('div', 'facts sim-facts');

  const sliders = (block.params || []).map((p, k) => {
    const values = p.values || [];
    const input = h('input', 'sim-range');
    input.type = 'range';
    input.min = '0';
    input.max = String(Math.max(0, values.length - 1));
    input.step = '1';
    input.value = String(positions[k]);
    input.setAttribute('aria-label', String(p.label || 'setting'));
    const out = h('span', 'sim-value', String(values[positions[k]] ?? ''));
    input.addEventListener('input', () => {
      positions[k] = Number(input.value);
      out.textContent = String(values[positions[k]] ?? '');
      show();
    });
    // Marks under the track only when every value is short enough to fit ("0.5", "A*"); else the value beside it.
    const short = values.length <= 12 && values.every((v) => String(v).length <= 4);
    const ticks = short ? h('div', 'sim-ticks', values.map((v) => h('span', null, String(v)))) : null;
    return h('label', 'sim-slider', [h('span', 'sim-label', String(p.label || '')), input, out, ticks]);
  });

  let shown = -1;
  function show() {
    const at = frameAt(block, positions);
    if (at === shown) return;
    shown = at;
    const frame = frames[at] || {};
    img.src = urls[at];
    caption.replaceChildren(...inline(parseInline(String(frame.caption || ''))));
    facts.replaceChildren(
      ...(frame.facts || []).map((it) => h('div', 'fact', [h('div', 'fact-value', String(it.value ?? '')), h('div', 'fact-label', String(it.label ?? ''))])),
    );
    facts.hidden = !(frame.facts || []).length;
  }
  show();
  return h('div', 'sim', [stage, facts, caption, h('div', 'sim-sliders', sliders)]);
}
