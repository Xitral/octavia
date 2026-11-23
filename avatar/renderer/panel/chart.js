// Bar, line and donut charts for a panel, as a plain tree of SVG elements ({tag, attrs, text, kids}) that the panel
// turns into the real thing. Colours come from the panel's stylesheet by class (s0..s5 for the series), so the
// look lives in one place. Pure, so it can be tested without a browser.
//
// Input: {style: bar | line | pie, labels: [...], series: [{name, values}], unit?}

const SERIES = 6;
const FONT = 12;
const el = (tag, attrs = {}, kids = [], text) => ({ tag, attrs, kids, text });

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

// Round ticks covering lo..hi (about `count` of them). Bars start from 0 (`zero`); a line only covers its data, with
// a little room, so a resting heart rate between 51 and 57 isn't drawn flat.
export function niceTicks(lo, hi, count = 4, zero = true) {
  let a = zero ? Math.min(lo, 0) : lo - (hi - lo) * 0.1;
  let b = zero ? Math.max(hi, 0) : hi + (hi - lo) * 0.1;
  if (!zero && lo >= 0 && a < 0) a = 0;
  if (a === b) [a, b] = zero ? [a, a + 1] : [a - 1, b + 1];
  const step = niceStep(b - a, count);
  a = Math.floor(a / step) * step;
  b = Math.ceil(b / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

export function formatNumber(v, unit = '') {
  const abs = Math.abs(v);
  let s;
  if (abs >= 1e9) s = `${+(v / 1e9).toFixed(1)}B`;
  else if (abs >= 1e6) s = `${+(v / 1e6).toFixed(1)}M`;
  else if (abs >= 1e4) s = `${+(v / 1e3).toFixed(1)}k`;
  else s = `${+v.toFixed(abs >= 100 ? 0 : abs >= 10 ? 1 : 2)}`;
  if (!unit) return s;
  return unit === '%' || unit.length === 1 ? `${s}${unit}` : `${s} ${unit}`;
}

const fit = (text, px) => {
  const room = Math.max(3, Math.floor(px / (FONT * 0.55)));
  const s = String(text);
  return s.length <= room ? s : `${s.slice(0, room - 1)}…`;
};

function clean(spec) {
  const labels = (spec.labels || []).map(String);
  const series = (spec.series || [])
    .slice(0, SERIES)
    .map((s, k) => ({ name: String(s.name ?? ''), values: (s.values || []).map((v) => (v == null ? NaN : Number(v))), k })) // null: missing, not 0
    .filter((s) => s.values.length);
  const n = Math.max(labels.length, ...series.map((s) => s.values.length), 0);
  return { labels: Array.from({ length: n }, (_, i) => labels[i] ?? ''), series, n, unit: String(spec.unit ?? '') };
}

function legend(series, width, y) {
  if (series.length < 2) return [];
  const per = Math.min(150, width / series.length);
  return series.map((s, i) =>
    el('g', { class: `legend s${s.k}`, transform: `translate(${(i * per).toFixed(1)},${y})` }, [
      el('rect', { x: 0, y: -9, width: 10, height: 10, rx: 2, class: 'swatch' }),
      el('text', { x: 15, y: 0, class: 'legend-text' }, [], fit(s.name, per - 20)),
    ]),
  );
}

function axes(ticks, scaleY, left, right, unit) {
  return ticks.map((t) =>
    el('g', { class: t === 0 ? 'tick zero' : 'tick' }, [
      el('line', { x1: left, x2: right, y1: scaleY(t).toFixed(1), y2: scaleY(t).toFixed(1) }),
      el('text', { x: left - 6, y: (scaleY(t) + 4).toFixed(1), 'text-anchor': 'end', class: 'tick-text' }, [], formatNumber(t, unit)),
    ]),
  );
}

function barOrLine(spec, width, line) {
  const { labels, series, n, unit } = clean(spec);
  const values = series.flatMap((s) => s.values.filter(Number.isFinite));
  if (!n || !values.length) return null;
  const ticks = niceTicks(Math.min(...values), Math.max(...values), 4, !line);
  const tickW = Math.max(...ticks.map((t) => formatNumber(t, unit).length)) * FONT * 0.58;
  const top = series.length > 1 ? 30 : 12;
  const left = Math.ceil(tickW + 12);
  const right = width - 8;
  const height = 230;
  const bottom = height - 30;
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const scaleY = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);
  const plotW = right - left;
  const slot = plotW / n;
  const kids = [...legend(series, width, 14), ...axes(ticks, scaleY, left, right, unit)];
  const every = Math.ceil((n * FONT * 3.2) / plotW); // how many labels to skip so they don't collide
  labels.forEach((label, i) => {
    if (i % every) return;
    const x = left + slot * (i + 0.5);
    kids.push(el('text', { x: x.toFixed(1), y: bottom + 18, 'text-anchor': 'middle', class: 'cat-text' }, [], fit(label, slot * every - 4)));
  });
  if (line) {
    series.forEach((s) => {
      const pts = s.values.map((v, i) => (Number.isFinite(v) ? [left + slot * (i + 0.5), scaleY(v)] : null));
      const runs = [];
      let run = [];
      for (const p of pts) {
        if (p) run.push(p);
        else if (run.length) {
          runs.push(run); // a gap in the data breaks the line
          run = [];
        }
      }
      if (run.length) runs.push(run);
      const g = el('g', { class: `series s${s.k}` });
      for (const r of runs) {
        const d = r.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
        if (series.length === 1 && r.length > 1) {
          const base = scaleY(Math.max(lo, Math.min(0, hi)));
          g.kids.push(el('path', { d: `${d} L${r[r.length - 1][0].toFixed(1)},${base.toFixed(1)} L${r[0][0].toFixed(1)},${base.toFixed(1)} Z`, class: 'area' }));
        }
        g.kids.push(el('path', { d, class: 'line draw', pathLength: 1 }));
      }
      pts.forEach((p, i) => {
        if (!p) return;
        g.kids.push(el('circle', { cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 3.2, class: 'dot pop', style: { '--i': i } }, [
          el('title', {}, [], `${labels[i]}: ${formatNumber(s.values[i], unit)}`),
        ]));
      });
      kids.push(g);
    });
  } else {
    const k = series.length;
    const barW = Math.min(38, (slot * 0.76) / k);
    const zero = scaleY(Math.max(lo, Math.min(0, hi)));
    const showValues = n * k <= 16;
    series.forEach((s, si) => {
      const g = el('g', { class: `series s${s.k}` });
      s.values.forEach((v, i) => {
        if (!Number.isFinite(v)) return;
        const x = left + slot * (i + 0.5) - (barW * k) / 2 + si * barW;
        const y = Math.min(scaleY(v), zero);
        const h = Math.max(1, Math.abs(scaleY(v) - zero));
        g.kids.push(
          el('rect', { x: x.toFixed(1), y: y.toFixed(1), width: (barW - 2).toFixed(1), height: h.toFixed(1), rx: 2, class: 'bar grow', style: { '--i': i + si } }, [
            el('title', {}, [], `${labels[i]}${s.name ? ` (${s.name})` : ''}: ${formatNumber(v, unit)}`),
          ]),
        );
        if (showValues) {
          const vy = v >= 0 ? y - 5 : y + h + 13;
          g.kids.push(el('text', { x: (x + (barW - 2) / 2).toFixed(1), y: vy.toFixed(1), 'text-anchor': 'middle', class: 'value-text pop', style: { '--i': i + si } }, [], formatNumber(v, unit)));
        }
      });
      kids.push(g);
    });
  }
  return el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, class: `chart ${line ? 'line-chart' : 'bar-chart'}`, role: 'img' }, kids);
}

function pie(spec, width) {
  const { labels, series, unit } = clean(spec);
  const s = series[0];
  if (!s) return null;
  const parts = s.values.map((v, i) => ({ v: Math.max(0, Number.isFinite(v) ? v : 0), label: labels[i], i })).filter((p) => p.v > 0);
  const total = parts.reduce((a, p) => a + p.v, 0);
  if (!total) return null;
  const height = Math.max(170, Math.min(260, parts.length * 24 + 30));
  const r = Math.min(height / 2 - 12, width / 4);
  const cx = r + 14;
  const cy = height / 2;
  const kids = [];
  let angle = -Math.PI / 2;
  const pt = (a, rad) => `${(cx + rad * Math.cos(a)).toFixed(2)},${(cy + rad * Math.sin(a)).toFixed(2)}`;
  const inner = r * 0.58;
  parts.forEach((p, k) => {
    const sweep = (p.v / total) * Math.PI * 2;
    const end = angle + sweep - (parts.length > 1 ? 0.012 : 0);
    const large = sweep > Math.PI ? 1 : 0;
    const d =
      parts.length === 1
        ? `M${pt(-Math.PI / 2, r)} A${r},${r} 0 1 1 ${pt(Math.PI * 1.5 - 0.0001, r)} L${pt(Math.PI * 1.5 - 0.0001, inner)} A${inner},${inner} 0 1 0 ${pt(-Math.PI / 2, inner)} Z`
        : `M${pt(angle, r)} A${r},${r} 0 ${large} 1 ${pt(end, r)} L${pt(end, inner)} A${inner},${inner} 0 ${large} 0 ${pt(angle, inner)} Z`;
    kids.push(
      el('path', { d, class: `slice s${k % SERIES} pop`, style: { '--i': k } }, [
        el('title', {}, [], `${p.label}: ${formatNumber(p.v, unit)} (${Math.round((p.v / total) * 100)}%)`),
      ]),
    );
    angle += sweep;
  });
  kids.push(el('text', { x: cx, y: cy + 5, 'text-anchor': 'middle', class: 'pie-total' }, [], formatNumber(total, unit)));
  const lx = cx + r + 22;
  const top = cy - (parts.length * 22) / 2 + 12;
  parts.forEach((p, k) => {
    const pct = Math.round((p.v / total) * 100);
    kids.push(
      el('g', { class: `legend s${k % SERIES}`, transform: `translate(${lx.toFixed(1)},${(top + k * 22).toFixed(1)})` }, [
        el('rect', { x: 0, y: -9, width: 10, height: 10, rx: 2, class: 'swatch' }),
        el('text', { x: 16, y: 0, class: 'legend-text' }, [], `${fit(p.label, width - lx - 70)}  ${pct}%`),
      ]),
    );
  });
  return el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, class: 'chart pie-chart', role: 'img' }, kids);
}

// The chart's tree, or null when there's nothing to draw.
export function buildChart(spec, width) {
  if (spec.style === 'pie') return pie(spec, width);
  return barOrLine(spec, width, spec.style === 'line');
}
