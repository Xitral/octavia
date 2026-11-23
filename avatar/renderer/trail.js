'use strict';
// One monitor's part of the streak she leaves when she flies (see lib/trail.js): a bright head racing from
// `from` to `to` with a tail of Rainbow Dash's six colours tapering and fading out behind it. And of a sonic
// rainboom: a ring of those colours with a white shock front racing out from where she is. 2D canvas only,
// and nothing runs between them.

(() => {
  // Her mane's colours, from one edge of the streak to the other.
  const COLORS = ['#ee4144', '#f37033', '#fdf6af', '#62bc4d', '#1e98d3', '#672f89'];
  // The orb's (palette 'orb'): a hazy blue light trail, brightest down its middle, in her PS2 look (desaturated, never
  // gold).
  const ORB_COLORS = ['#17305f', '#3f6fc4', '#a9cdee', '#a9cdee', '#3f6fc4', '#17305f'];
  const colorsFor = (msg) => (msg.palette === 'orb' ? ORB_COLORS : COLORS);
  const TAIL_LAG = 0.1; // the tail sets off this fraction of the head's travel time after the head
  const TAIL_TAPER = 0.3; // the streak's width at its tail, as a fraction of its width at the head
  const BOOM_BAND = 14; // px: each colour's band in a rainboom's ring

  const host = window.trailHost;
  const canvas = document.getElementById('trail');
  const ctx = canvas.getContext('2d');
  // The page's content security policy allows no inline styles, so they're set from here.
  for (const el of [document.documentElement, document.body]) {
    Object.assign(el.style, { margin: '0', width: '100%', height: '100%', overflow: 'hidden', background: 'transparent' });
  }
  Object.assign(canvas.style, { display: 'block', width: '100%', height: '100%' });

  const rgba = (hex, alpha) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  };
  const clamp01 = (t) => Math.min(1, Math.max(0, t));
  const smoothstep = (t) => {
    const x = clamp01(t);
    return x * x * (3 - 2 * x);
  };
  // Off the mark fast, easing into the arrival.
  const easeOut = (t) => 1 - (1 - clamp01(t)) ** 3;
  const finite = (...values) => values.every(Number.isFinite);

  let streak = null;
  let booms = []; // rainbooms going off: { at, reach, duration, start }
  let latest = 0; // the newest thing asked for: once everything is drawn, that's the one reported done
  let ratio = 1;
  let running = false;

  function fit() {
    const ratio = window.devicePixelRatio || 1;
    const width = Math.round(window.innerWidth * ratio);
    const height = Math.round(window.innerHeight * ratio);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    return ratio;
  }

  function clear() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function paint(s, elapsed) {
    const { from, to, thickness, colors } = s;
    const head = easeOut(elapsed / s.duration);
    const tail = smoothstep((elapsed - TAIL_LAG * s.duration) / (s.life - TAIL_LAG * s.duration));
    // Once the head is there, the rest fades as the tail catches up.
    const alpha = 1 - smoothstep((elapsed - s.duration) / (s.life - s.duration));
    if (head - tail <= 0.001 || alpha <= 0) return;
    const hx = from.x + s.dx * head;
    const hy = from.y + s.dy * head;
    const tx = from.x + s.dx * tail;
    const ty = from.y + s.dy * tail;
    const stripe = thickness / colors.length;
    const half = colors.length / 2;
    for (let i = 0; i < colors.length; i++) {
      const a = (i - half) * stripe;
      const b = (i + 1 - half) * stripe;
      const gradient = ctx.createLinearGradient(tx, ty, hx, hy);
      gradient.addColorStop(0, rgba(colors[i], 0));
      gradient.addColorStop(0.6, rgba(colors[i], 0.55 * alpha));
      gradient.addColorStop(1, rgba(colors[i], 0.95 * alpha));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.moveTo(tx + s.nx * a * TAIL_TAPER, ty + s.ny * a * TAIL_TAPER);
      ctx.lineTo(hx + s.nx * a, hy + s.ny * a);
      // Overlap the next stripe by a hair so no seam shows between them.
      ctx.lineTo(hx + s.nx * (b + 0.5), hy + s.ny * (b + 0.5));
      ctx.lineTo(tx + s.nx * (b + 0.5) * TAIL_TAPER, ty + s.ny * (b + 0.5) * TAIL_TAPER);
      ctx.closePath();
      ctx.fill();
    }
    // A bright, rounded head.
    const glow = ctx.createRadialGradient(hx, hy, 0, hx, hy, thickness * 0.8);
    glow.addColorStop(0, `rgba(255, 255, 255, ${0.95 * alpha})`);
    glow.addColorStop(0.45, `rgba(255, 255, 255, ${0.5 * alpha})`);
    glow.addColorStop(1, 'rgba(255, 255, 255, 0)');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(hx, hy, thickness * 0.8, 0, Math.PI * 2);
    ctx.fill();
  }

  // A ring racing out from `at`, off like a shot and slowing, fading as it goes: six bands and a white front.
  function paintBoom(b, elapsed) {
    const p = clamp01(elapsed / b.duration);
    const r = b.reach * easeOut(p);
    const alpha = 1 - smoothstep((p - 0.35) / 0.65);
    if (alpha <= 0) return;
    for (let i = 0; i < b.colors.length; i++) {
      const radius = r - (i + 0.5) * BOOM_BAND;
      if (radius <= 0) continue;
      ctx.beginPath();
      ctx.arc(b.at.x, b.at.y, radius, 0, Math.PI * 2);
      ctx.lineWidth = BOOM_BAND + 0.5;
      ctx.strokeStyle = rgba(b.colors[i], 0.85 * alpha);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(b.at.x, b.at.y, r + 3, 0, Math.PI * 2);
    ctx.lineWidth = 5;
    ctx.strokeStyle = `rgba(255, 255, 255, ${0.95 * alpha})`;
    ctx.stroke();
  }

  function frame(now) {
    clear();
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (streak && now - streak.start >= streak.life) streak = null;
    if (streak) paint(streak, now - streak.start);
    booms = booms.filter((b) => now - b.start < b.duration);
    for (const b of booms) paintBoom(b, now - b.start);
    if (!streak && !booms.length) {
      running = false;
      clear();
      // Let that blank frame reach the screen before the window is hidden, so the next time it's shown it
      // can't flash the end of this one.
      const id = latest;
      requestAnimationFrame(() => requestAnimationFrame(() => host.done(id)));
      return;
    }
    requestAnimationFrame(frame);
  }

  function run() {
    if (running) return;
    running = true;
    requestAnimationFrame(frame);
  }

  host.onDraw((msg) => {
    const { from, to } = msg || {};
    if (!from || !to || !finite(from.x, from.y, to.x, to.y, msg.duration, msg.life, msg.thickness)) return;
    const length = Math.hypot(to.x - from.x, to.y - from.y);
    if (length < 1) {
      host.done(msg.id);
      return;
    }
    const duration = Math.min(3000, Math.max(50, msg.duration));
    ratio = fit();
    latest = msg.id;
    streak = {
      id: msg.id,
      from,
      to,
      dx: to.x - from.x,
      dy: to.y - from.y,
      nx: -(to.y - from.y) / length,
      ny: (to.x - from.x) / length,
      duration,
      life: Math.max(duration * 1.05, Math.min(5000, msg.life)),
      thickness: Math.min(200, Math.max(2, msg.thickness)),
      colors: colorsFor(msg),
      start: performance.now(),
    };
    run();
  });

  host.onBoom?.((msg) => {
    const { at } = msg || {};
    if (!at || !finite(at.x, at.y, msg.reach, msg.duration) || msg.reach <= 0) return;
    ratio = fit();
    latest = msg.id;
    booms.push({ at, reach: Math.min(20000, msg.reach), duration: Math.min(3000, Math.max(100, msg.duration)), colors: colorsFor(msg), start: performance.now() });
    run();
  });
})();
