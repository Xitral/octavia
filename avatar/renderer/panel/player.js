// An explainer's player (visuals/explainer.py): the bar under the panel (replay, back, play/pause, next, a dot per
// step, the step's caption), her pointer gliding to what the step is about, and the things a step makes flash.
// The steps themselves come as ordinary panel changes: this only shows where it is and sends the presses back.
//
// Nothing loops: the pointer glides once per step and then rests, a flash plays once (a see-through window redraws
// whole for every frame anything moves).

import { CHROME } from '../icons/chrome.js';
import { iconSvg } from '../icons/icons.js';

const ACTIONS = new Set(['play', 'pause', 'next', 'back', 'replay', 'step']);

// The bar's content for a player state. Pure: {at, playing, steps:[{id, caption}]} -> what to show.
export function barState(player) {
  const steps = Array.isArray(player && player.steps) ? player.steps : [];
  const at = Math.max(0, Math.min(steps.length, Number(player && player.at) || 0));
  const current = at ? steps[at - 1] : null;
  return {
    count: steps.length,
    at,
    playing: !!(player && player.playing),
    caption: current ? String(current.caption || '') : '',
    dots: steps.map((s, k) => (k + 1 < at ? 'done' : k + 1 === at ? 'current' : 'todo')),
    canBack: at > 0,
    canNext: at < steps.length,
    label: `${at} / ${steps.length}`,
  };
}

// Where the pointer's ring goes round a box (the panel's own pixels), a little outside it.
export function ringAround(box, pad = 5) {
  if (!box) return null;
  return { x: box.x - pad, y: box.y - pad, w: box.w + 2 * pad, h: box.h + 2 * pad };
}

export function createPlayer({ host, h, sheet, footer, nodeOf, boxOf }) {
  const bar = h('div', 'player');
  bar.hidden = true;
  const button = (icon, action, title) => {
    const b = h('button', `pl-btn pl-${action}`);
    b.append(iconSvg(icon));
    b.title = title;
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      send(b.dataset.action || action);
    });
    return b;
  };
  const replay = button(CHROME.replay, 'replay', 'Play it again from the start');
  const back = button(CHROME.back, 'back', 'Back a step');
  const play = button(CHROME.play, 'play', 'Play');
  const next = button(CHROME.next, 'next', 'Next step');
  const dots = h('div', 'pl-dots');
  const count = h('span', 'pl-count');
  const caption = h('div', 'pl-caption');
  bar.append(h('div', 'pl-row', [replay, back, play, next, dots, count]), caption);
  footer.before(bar);

  const ring = h('div', 'pointer');
  ring.append(h('i', 'pointer-spark'));
  ring.hidden = true;

  let steps = [];
  let target = '';
  let placedOnce = false;
  let lastPulse = '';

  function send(action, step) {
    if (ACTIONS.has(action) && host.control) host.control(action, step);
  }

  function drawBar(player) {
    const s = barState(player);
    bar.hidden = !s.count;
    const action = s.playing ? 'pause' : 'play';
    if (play.dataset.action !== action) play.replaceChildren(iconSvg(s.playing ? CHROME.pause : CHROME.play));
    play.dataset.action = action;
    play.title = s.playing ? 'Pause' : s.at >= s.count ? 'Play it again' : 'Play';
    back.disabled = !s.canBack;
    next.disabled = !s.canNext;
    count.textContent = s.label;
    if (caption.textContent !== s.caption) {
      caption.textContent = s.caption;
      caption.classList.remove('swap');
      void caption.offsetWidth;
      if (s.caption) caption.classList.add('swap');
    }
    const ids = (player.steps || []).map((x) => x.id).join('\u0000');
    if (ids !== steps.join('\u0000')) {
      steps = (player.steps || []).map((x) => x.id);
      dots.replaceChildren(
        ...steps.map((id, k) => {
          const d = h('button', 'pl-dot');
          d.title = (player.steps[k].caption || id).slice(0, 80);
          d.addEventListener('click', (e) => {
            e.stopPropagation();
            send('step', k + 1);
          });
          return d;
        }),
      );
    }
    [...dots.children].forEach((d, k) => (d.dataset.state = s.dots[k]));
    bar.classList.toggle('playing', s.playing);
  }

  // The ring goes round what the step is about; with nothing to point at it fades where it is.
  function place() {
    if (!ring.isConnected) sheet.append(ring);
    const box = target ? ringAround(boxOf(target)) : null;
    if (!box) {
      ring.classList.add('resting');
      if (host.pointer) host.pointer(null);
      return;
    }
    const first = ring.hidden || !placedOnce;
    ring.hidden = false;
    ring.classList.remove('resting');
    if (first) ring.style.transition = 'none';
    ring.style.transform = `translate(${box.x.toFixed(1)}px, ${box.y.toFixed(1)}px)`;
    ring.style.width = `${box.w.toFixed(1)}px`;
    ring.style.height = `${box.h.toFixed(1)}px`;
    const round = nodeOf(target);
    ring.classList.toggle('round', !!(round && round.querySelector && round.querySelector(':scope > ellipse')));
    if (first) {
      void ring.offsetWidth;
      ring.style.transition = '';
      ring.classList.remove('arrive');
      void ring.offsetWidth;
      ring.classList.add('arrive');
    }
    placedOnce = true;
    // Where it is on the screen, for her orb to point at too (the main process turns it into screen pixels).
    const node = nodeOf(target);
    if (node && host.pointer) {
      const r = node.getBoundingClientRect();
      host.pointer({ x: r.left, y: r.top, width: r.width, height: r.height });
    }
  }

  function pulse(addresses) {
    for (const a of addresses) {
      const node = nodeOf(a);
      if (!node) continue;
      node.classList.remove('pulse');
      void node.getBoundingClientRect();
      node.classList.add('pulse');
    }
  }

  // A new version of the panel came: the bar, then (once laid out) the pointer and flashes.
  function update(spec) {
    const player = spec && spec.player;
    if (!player) {
      bar.hidden = true;
      ring.hidden = true;
      target = '';
      return;
    }
    drawBar(player);
    target = String(spec.pointer || '');
    const flash = (spec.pulse || []).map(String);
    const key = `${player.at}|${flash.join(',')}`;
    if (flash.length && key !== lastPulse) pulse(flash);
    lastPulse = key;
  }

  return { update, place, bar };
}
