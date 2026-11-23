// The screen history filmstrip (visuals/film.py): a day of what she kept, to drag through. A strip of frames runs under
// a fixed playhead and the words of the frame under it show above; between frames, a gap says only that nothing was
// kept. A thin bar of the whole day, to scale, sits under the strip for getting across it. The words are the user's
// screen: they go in as text nodes, and stay here (the page tells Octavia where the playhead stopped, nothing else).
//
// Nothing snaps: the strip follows the pointer one to one, runs on when flicked, and every other move is a critically
// damped spring, so it leaves and arrives at rest wherever that is. It only draws while it moves (a see-through window
// is redrawn whole for every frame anything in it moves).

import { CHROME } from '../icons/chrome.js';
import { iconSvg } from '../icons/icons.js';

const CELL_MIN = 72;
const CELL_MAX = 280;
const SLIVER = 16; // a glance with no words (a task switcher, the Start menu): there, but no room taken
const SLIVER_UNDER_S = 30;
const GAP_MIN = 18;
const GAP_MAX = 72;
const GAP_FROM_S = 60; // two windows less than this apart touch on the strip
const MARK_ROOM = 96; // time marks along the strip at least this far apart
const SHORT_LINE = 28; // lines this short are labels (a menu, a list): they share a line
const SCREEN_MIN = 168; // the words' height; a panel the user made taller gives them the room
const OVERSHOOT = 36; // how far past an end it gives under a drag
const DRAG_GIVE = 0.3;
const STILL = 0.02; // px/ms: slower than this, a flick is over
const FRICTION = 0.0032; // of the speed, per ms
const SPRING = 0.011; // per ms: gliding to a place takes a few hundred ms
const SEEK_SPRING = 0.006; // she moves it further and more slowly
const CLICK_PX = 4;

const log2 = (x) => Math.log(x) / Math.LN2;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

const isSliver = (frame) => !frame.text && frame.end - frame.start < SLIVER_UNDER_S;

// Frames (oldest first) as cells along the strip: {t0, t1, x, w, frame} in order, `frame` the frame's index or -1 in a
// gap. A window's width grows with its length, slowly, so a long one doesn't swallow the day and a brief one can be seen.
// Touching windows meet: each cell runs to the next one's start, so every moment of the strip has a time. Pure.
export function layoutFilm(frames) {
  const cells = [];
  let x = 0;
  frames.forEach((frame, k) => {
    const next = frames[k + 1];
    const gapAfter = next ? next.start - frame.end : 0;
    const touches = next && gapAfter < GAP_FROM_S;
    const t0 = frame.start;
    const t1 = Math.max(touches ? next.start : frame.end, t0 + 1);
    const w = isSliver(frame) ? SLIVER : clamp(CELL_MIN + 30 * log2(1 + (frame.end - frame.start) / 60), CELL_MIN, CELL_MAX);
    cells.push({ t0, t1, x, w, frame: k });
    x += w;
    if (next && !touches) {
      const gap = clamp(GAP_MIN + 7 * log2(1 + gapAfter / 60), GAP_MIN, GAP_MAX);
      cells.push({ t0: t1, t1: Math.max(next.start, t1 + 1), x, w: gap, frame: -1 });
      x += gap;
    }
  });
  return { cells, total: x };
}

function search(cells, value, key) {
  let low = 0;
  let high = cells.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (key(cells[mid]) <= value) low = mid;
    else high = mid - 1;
  }
  return cells[low];
}

export const cellAtX = (layout, x) => search(layout.cells, clamp(x, 0, layout.total), (c) => c.x);

export function timeAt(layout, x) {
  const c = cellAtX(layout, x);
  return c.t0 + (c.t1 - c.t0) * clamp((x - c.x) / c.w, 0, 1);
}

export function xAt(layout, t) {
  const c = search(layout.cells, t, (cell) => cell.t0);
  return c.x + c.w * clamp((t - c.t0) / (c.t1 - c.t0), 0, 1);
}

// Times along the strip, with room between them, so anywhere on it you can see when you are: [{x, t}], at windows'
// starts. Pure.
export function timeMarks(layout) {
  const marks = [];
  let room = -Infinity;
  for (const cell of layout.cells) {
    if (cell.frame < 0 || cell.x - room < MARK_ROOM) continue;
    marks.push({ x: cell.x, t: cell.t0 });
    room = cell.x;
  }
  return marks;
}

// The whole day's bar: where its hours fall, as shares of its width. Fewer the longer the day. Pure.
export function dayHours(t0, t1) {
  const hours = (t1 - t0) / 3600;
  const step = hours > 12 ? 3 : hours > 5 ? 2 : 1;
  const at = new Date(t0 * 1000);
  at.setMinutes(0, 0, 0);
  const marks = [];
  for (at.setHours(at.getHours() + 1); at.getTime() / 1000 < t1; at.setHours(at.getHours() + 1)) {
    if (at.getHours() % step) continue;
    marks.push({ share: (at.getTime() / 1000 - t0) / (t1 - t0), hour: at.getHours() });
  }
  return marks;
}

// One step of a critically damped spring towards `target`: it starts from what it's doing and comes to rest there
// without passing it.
export function springStep(pos, speed, target, omega, dt) {
  const shift = pos - target;
  const part = (speed + omega * shift) * dt;
  const decay = Math.exp(-omega * dt);
  return { pos: target + (shift + part) * decay, speed: (speed - omega * part) * decay };
}

// How far the words scroll, 0 to 1, at this place on a frame's cell.
export const textShare = (cell, x) => clamp((x - cell.x) / cell.w, 0, 1);

// What an app shows its screen reader is mostly labels, one to a line: a run of short lines shares a line, so the words
// that say something fit on the screen. Sentences stay as they are. Pure.
export function packLines(text) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length) out.push(run.join(' · '));
    run = [];
  };
  for (const line of String(text).split('\n')) {
    if (line.length <= SHORT_LINE) run.push(line);
    else {
      flush();
      out.push(line);
    }
  }
  flush();
  return out.join('\n');
}

const stamp = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const lasted = (s) => {
  if (s < 60) return `${Math.max(Math.round(s), 1)} s`;
  const minutes = Math.round(s / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
};

export function buildFilm(first, { h, host }) {
  let block = first;
  let frames = first.frames;
  let layout = { cells: [], total: 0 };
  let cellNodes = [];
  let pos = 0;
  let speed = 0;
  let target = null; // where a spring is taking it; null when it isn't
  let omega = SPRING;
  let raf = 0;
  let stamped = 0;
  let onCell = null;
  let dragging = false;
  let reportTimer = null;
  let scrollRoom = 0;
  let ownScroll = null; // the user scrolled these words themselves: they stay put until the window changes
  let side = 0;
  let dayStart = 0;
  let dayEnd = 1;

  const dayEl = h('div', 'film-day');
  const earlier = h('button', 'film-nav');
  earlier.append(iconSvg(CHROME.earlier));
  earlier.title = 'The day before';
  const later = h('button', 'film-nav');
  later.append(iconSvg(CHROME.later));
  later.title = 'The next day';
  earlier.addEventListener('click', () => host.control('back'));
  later.addEventListener('click', () => host.control('next'));

  const timeEl = h('div', 'film-time');
  const appEl = h('div', 'film-app');
  const titleEl = h('div', 'film-title');
  const faces = [h('div', 'film-words'), h('div', 'film-words')];
  const screen = h('div', 'film-screen', faces);
  const gate = h('div', 'film-gate', [h('div', 'film-where', [timeEl, h('div', 'film-names', [appEl, titleEl])]), screen]);

  const track = h('div', 'film-track');
  const playhead = h('div', 'film-playhead');
  const strip = h('div', 'film-strip', [track, playhead]);

  const shape = document.createElement('canvas');
  shape.className = 'film-shape';
  const hoursEl = h('div', 'film-hours');
  const marker = h('div', 'film-marker');
  const bar = h('div', 'film-bar', [shape, hoursEl, marker]);
  bar.title = 'The whole day: drag to get across it';

  const node = h('div', 'film', [h('div', 'film-top', [earlier, dayEl, later]), gate, strip, bar]);

  // ---------------------------------------------------------------- the strip's cells

  function frameCell(cell) {
    const f = frames[cell.frame];
    if (isSliver(f)) {
      const sliver = h('div', 'film-cell film-sliver');
      sliver.title = `${f.app}, ${lasted(f.end - f.start)}`;
      return sliver;
    }
    const bars = h('div', 'film-bars');
    const lines = f.text ? f.text.split('\n').slice(0, 4) : [];
    for (const line of lines) {
      const line_ = h('i');
      line_.style.width = `${clamp(line.length * 1.4, 18, 100)}%`;
      bars.append(line_);
    }
    const name = h('b');
    name.textContent = f.app;
    const words = h('span');
    words.textContent = f.title;
    const cellNode = h('div', 'film-cell', [name, words, bars]);
    cellNode.title = f.title ? `${f.app}: ${f.title}` : f.app;
    return cellNode;
  }

  function gapCell(cell) {
    const gap = h('div', 'film-cell film-gap');
    gap.title = `Nothing kept, ${lasted(cell.t1 - cell.t0)}`;
    return gap;
  }

  function lay() {
    layout = layoutFilm(frames);
    cellNodes = layout.cells.map((cell) => {
      const el = cell.frame < 0 ? gapCell(cell) : frameCell(cell);
      el.style.transform = `translateX(${cell.x}px)`;
      el.style.width = `${cell.w - 2}px`;
      return el;
    });
    const marks = timeMarks(layout).map(({ x, t }) => {
      const mark = h('div', 'film-hour');
      mark.textContent = stamp(t);
      mark.style.transform = `translateX(${x}px)`;
      return mark;
    });
    track.replaceChildren(...cellNodes, ...marks);
    track.style.width = `${layout.total}px`;
    onCell = null;
    dayStart = frames[0].start;
    dayEnd = Math.max(frames[frames.length - 1].end, dayStart + 60);
    hoursEl.replaceChildren(
      ...dayHours(dayStart, dayEnd).map(({ share, hour }) => {
        const mark = h('span');
        mark.textContent = new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: 'numeric' });
        mark.style.left = `${(share * 100).toFixed(2)}%`;
        return mark;
      }),
    );
    paintShape();
  }

  // The day to scale: a block of her light for each window, brighter where words were kept, dark where nothing was.
  function paintShape() {
    const width = bar.clientWidth;
    if (!width) return;
    const scale = (bar.getBoundingClientRect().width / width || 1) * (window.devicePixelRatio || 1);
    shape.width = Math.round(width * scale);
    shape.height = Math.round(shape.offsetHeight * scale) || 1;
    const paint = shape.getContext('2d', { willReadFrequently: true }); // drawn in software: painted once, and cheap
    const span = dayEnd - dayStart;
    for (const f of frames) {
      paint.fillStyle = f.text ? 'rgba(124, 192, 255, 0.62)' : 'rgba(124, 192, 255, 0.24)';
      const x = ((f.start - dayStart) / span) * shape.width;
      paint.fillRect(x, 0, Math.max(1, ((f.end - f.start) / span) * shape.width), shape.height);
    }
  }

  // ---------------------------------------------------------------- what's under the playhead

  function showCell(cell) {
    onCell = cell;
    ownScroll = null;
    const index = layout.cells.indexOf(cell);
    for (const el of track.querySelectorAll('.on')) el.classList.remove('on');
    if (cellNodes[index]) cellNodes[index].classList.add('on');
    const f = cell.frame >= 0 ? frames[cell.frame] : null;
    appEl.textContent = f ? `${f.app} · ${lasted(f.end - f.start)}` : `Nothing kept · ${lasted(cell.t1 - cell.t0)}`;
    titleEl.textContent = f ? [f.title, f.site].filter(Boolean).join(' · ') : '';
    titleEl.title = titleEl.textContent;
    // Two faces, so the words cross-fade however fast it's dragged.
    side = 1 - side;
    const face = faces[side];
    face.textContent = f ? (f.text ? packLines(f.text) : 'Only that it was open.') : 'Nothing kept here.';
    face.classList.toggle('none', !f || !f.text);
    face.style.transform = 'translateY(0)';
    faces[side].classList.add('front');
    faces[1 - side].classList.remove('front');
    measureWords();
  }

  function measureWords() {
    scrollRoom = Math.max(0, faces[side].scrollHeight - screen.clientHeight);
    screen.classList.toggle('more', scrollRoom > 0);
  }

  function placeWords(cell) {
    if (!scrollRoom) return;
    const y = ownScroll === null ? scrollRoom * textShare(cell, pos) : ownScroll;
    faces[side].style.transform = `translateY(${(-y).toFixed(1)}px)`;
  }

  function draw() {
    const width = strip.clientWidth;
    track.style.transform = `translateX(${(width / 2 - pos).toFixed(2)}px)`;
    const cell = cellAtX(layout, pos);
    if (cell !== onCell) showCell(cell);
    const t = timeAt(layout, pos);
    const minute = Math.floor(t / 60);
    if (minute !== stamped) {
      stamped = minute;
      timeEl.textContent = stamp(t);
    }
    marker.style.transform = `translateX(${(clamp((t - dayStart) / (dayEnd - dayStart), 0, 1) * bar.clientWidth).toFixed(1)}px)`;
    placeWords(cell);
  }

  // ---------------------------------------------------------------- moving

  let last = 0;
  function frame(now) {
    raf = 0;
    const dt = Math.min(now - last, 34) || 16;
    last = now;
    if (dragging) return;
    if (target !== null) {
      const step = springStep(pos, speed, target, omega, dt);
      pos = step.pos;
      speed = step.speed;
      if (Math.abs(pos - target) < 0.2 && Math.abs(speed) < STILL) {
        pos = target;
        speed = 0;
        target = null;
      }
    } else if (speed) {
      pos += speed * dt;
      speed *= Math.exp(-FRICTION * dt);
      if (pos < 0 || pos > layout.total) target = clamp(pos, 0, layout.total); // past the end: it's drawn back
      else if (Math.abs(speed) < STILL) speed = 0;
    }
    draw();
    if (target !== null || speed) run();
    else settled();
  }

  function run() {
    if (raf) return;
    if (!last) last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  function glide(to, rate = SPRING) {
    clearTimeout(reportTimer);
    target = clamp(to, 0, layout.total);
    omega = rate;
    last = performance.now();
    run();
  }

  // Octavia hears where the playhead stopped. The app takes one send at a time, so a stop just after another is said
  // again a moment later.
  function settled() {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => {
      const at = String(Math.round(timeAt(layout, pos)));
      host.sent({ at }, 'at', [], false);
      reportTimer = setTimeout(() => host.sent({ at }, 'at', [], false), 900);
    }, 250);
  }

  // ---------------------------------------------------------------- the pointer

  let grabbed = null; // {x, from, moved, scale, box, trail}
  strip.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    clearTimeout(reportTimer);
    strip.setPointerCapture(e.pointerId);
    const box = strip.getBoundingClientRect();
    grabbed = { x: e.clientX, from: e.clientX, moved: false, scale: box.width / (strip.offsetWidth || 1) || 1, box, trail: [] };
    dragging = true;
    target = null;
    speed = 0;
    strip.classList.add('held');
  });
  strip.addEventListener('pointermove', (e) => {
    if (!grabbed) return;
    if (!grabbed.moved && Math.abs(e.clientX - grabbed.from) < CLICK_PX) return;
    grabbed.moved = true;
    const delta = (grabbed.x - e.clientX) / grabbed.scale;
    grabbed.x = e.clientX;
    const past = pos < 0 || pos > layout.total;
    pos = clamp(pos + delta * (past ? DRAG_GIVE : 1), -OVERSHOOT, layout.total + OVERSHOOT);
    grabbed.trail.push([e.timeStamp, pos]);
    if (grabbed.trail.length > 6) grabbed.trail.shift();
    draw();
  });
  function letGo(e) {
    if (!grabbed) return;
    const { moved, trail, box, scale } = grabbed;
    grabbed = null;
    dragging = false;
    strip.classList.remove('held');
    last = performance.now();
    if (!moved) {
      // A click: it glides there.
      glide(pos + (e.clientX - box.left) / scale - strip.clientWidth / 2);
      return;
    }
    const old = trail.find(([at]) => e.timeStamp - at < 90);
    const lastedMs = old ? e.timeStamp - old[0] : 0;
    speed = lastedMs > 4 ? (pos - old[1]) / lastedMs : 0;
    if (pos < 0 || pos > layout.total) glide(pos);
    else if (Math.abs(speed) >= STILL) run();
    else {
      speed = 0;
      settled();
    }
  }
  strip.addEventListener('pointerup', letGo);
  strip.addEventListener('pointercancel', letGo);
  strip.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      e.stopPropagation();
      const move = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      glide((target === null ? pos : target) + move);
    },
    { passive: false },
  );

  // The bar of the whole day: the place under the pointer is the place in the day, at once while dragged.
  let barHeld = false;
  const barTime = (e) => {
    const box = bar.getBoundingClientRect();
    return dayStart + clamp((e.clientX - box.left) / box.width, 0, 1) * (dayEnd - dayStart);
  };
  bar.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    clearTimeout(reportTimer);
    bar.setPointerCapture(e.pointerId);
    barHeld = true;
    glide(xAt(layout, barTime(e)));
  });
  bar.addEventListener('pointermove', (e) => {
    if (barHeld) glide(xAt(layout, barTime(e)));
  });
  const barGo = () => {
    barHeld = false;
  };
  bar.addEventListener('pointerup', barGo);
  bar.addEventListener('pointercancel', barGo);

  // The wheel over the words reads them; with nothing more to read it does what it does anywhere on a panel.
  screen.addEventListener(
    'wheel',
    (e) => {
      if (!scrollRoom) return;
      e.preventDefault();
      e.stopPropagation();
      const now = ownScroll === null ? scrollRoom * textShare(onCell || cellAtX(layout, pos), pos) : ownScroll;
      ownScroll = clamp(now + e.deltaY, 0, scrollRoom);
      faces[side].style.transform = `translateY(${(-ownScroll).toFixed(1)}px)`;
    },
    { passive: false },
  );

  // ---------------------------------------------------------------- a new version from Octavia

  function update(next) {
    const before = block;
    block = next;
    dayEl.textContent = next.day;
    earlier.disabled = !next.earlier;
    later.disabled = !next.later;
    const sameDay = before.day === next.day;
    if (JSON.stringify(before.frames) !== JSON.stringify(next.frames)) {
      const t = timeAt(layout, pos); // something was forgotten: the playhead stays at its time, whatever's left
      frames = next.frames;
      lay();
      pos = xAt(layout, sameDay ? t : next.at);
      if (!sameDay) {
        target = null;
        speed = 0;
        node.classList.remove('swap');
        void node.offsetWidth;
        node.classList.add('swap');
      }
      draw();
    }
    if (next.seek !== before.seek) glide(xAt(layout, next.at), SEEK_SPRING);
  }

  dayEl.textContent = first.day;
  earlier.disabled = !first.earlier;
  later.disabled = !first.later;
  lay();
  pos = xAt(layout, first.at);
  draw();

  // A panel the user made taller gives the words the room; shrunk again, they give it back.
  function fitScreen() {
    const content = node.closest('.content');
    const sheet = node.closest('.sheet');
    if (!content || !sheet) return;
    const room = getComputedStyle(content);
    const padding = parseFloat(room.paddingTop) + parseFloat(room.paddingBottom);
    const others = sheet.offsetHeight - screen.offsetHeight;
    const height = Math.max(SCREEN_MIN, Math.floor(content.clientHeight - padding - others));
    if (height !== screen.offsetHeight) screen.style.height = `${height}px`;
  }

  // Its width is known once it's on the page, and the panel's size can change: everything measured follows.
  const watch = new ResizeObserver(() => {
    if (!node.isConnected) {
      watch.disconnect();
      clearTimeout(reportTimer);
      return;
    }
    requestAnimationFrame(() => {
      fitScreen(); // out of the observer's own pass: changing sizes inside it would make it complain
      paintShape();
      measureWords();
      draw();
    });
  });
  watch.observe(strip);
  queueMicrotask(() => {
    const content = node.closest('.content');
    if (content) watch.observe(content);
  });
  node.film = { update };
  return node;
}
