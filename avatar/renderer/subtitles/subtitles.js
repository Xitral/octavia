// Her subtitles (docs/subtitles.md): what she's saying, word by word as she says it, thin and glowing, up and to the
// left of her orb over an underline that runs into a short line down to her, with a few sparks. Two short rows at
// most: the one she's saying, its current word lit, and the one before it, dimmed; an older row drifts out over the
// top. A word shows as she reaches it (words.js paces them to her audio), so it's never a wall of text.
// Math shows as math and code as written, through the panels' own Markdown and TeX readers: nothing from the text
// ever becomes markup. A formula lights up whole.
//
// Her state moves them as it moves her orb (feel.js): her voice slowly brightens the line to her and the underline and
// drifts the haze round the words; a sentence's feeling decides how its rows come in and leans her glow cyan or violet.
//
// With the pointer on the words they fade, as she does with it on her (../seethrough.js), so what's under them shows;
// the window lets every click through either way.
//
// A see-through window is redrawn whole for every frame anything in it moves, so nothing loops: each row comes in,
// rows move up, each word lights at once (no fade) and the sparks twinkle once; her voice is followed at VOICE_FPS
// only while she's speaking; in between the window is still.

import { parseInline } from '../panel/markdown.js';
import { parseTex } from '../panel/tex.js';
import { Voice, feelOf, leanOf } from './feel.js';
import { LEAVE_MS, SeeThrough } from '../seethrough.js';
import { MAX_LINES, TEXT_GAP, geometry, tailMs } from './timing.js';
import { Pacer, spokenWeight, spokenWords, startWords, unitFor, units } from './words.js';

const host = window.subtitlesHost;
const stage = document.getElementById('stage');
const view = document.getElementById('view');
const lines = document.getElementById('lines');
const measure = document.getElementById('measure');
const box = document.createElement('div'); // the box round the words (fitUnderline sizes it)
box.className = 'panel';
view.prepend(box);
const BOX = { x: 16, top: 9, bottom: 8 }; // its room round the rows
const underline = document.getElementById('underline');
const connector = document.getElementById('connector');
const haze = document.getElementById('haze');
const glow = document.getElementById('glow');
const hazeBox = haze.parentElement;
const lit = underline.querySelector('.lit');
const wire = connector.ownerSVGElement;
const sparks = [...document.querySelectorAll('.spark')];
const dust = [...document.querySelectorAll('.dust')];
// Still specks of her light by her edge, as the line leaves her (toward her: +x, +y before flipping).
const DUST = [[4, 24], [9, 29], [-11, 14], [27, -9], [33, -5]];

const FADE_MS = 900;
const CUT_FADE_MS = 260; // talked over: gone at once, nearly
const RISE_MS = 420;
const VOICE_FPS = 30; // smooth for a slow glow, at half the redraws of her orb
const GHOST_MS = 480; // a word's ghost settling onto it
const LOOK_MS = 120;
const WORD_GRACE_MS = 900; // no real word time for this long (longer than a pause between words): pace again // how often the words look at her clock at most while waiting (a silence, a length coming in)
const ON_PX = 6; // the pointer this close to the box round the words (or the underline) is on them
const NEAR_PX = 28; // and once on them, stays on them this close: brushing between two lines never brings them back

let layout = { anchor: { x: window.innerWidth - 44, y: window.innerHeight - 44 }, flipX: false, flipY: false };
let fadeTimer = null;
let goneTimer = null;
let ended = false; // she finished: the next line starts a new reply, not more of this one
let seq = 0; // the last line's number, told back with "faded"
const voice = new Voice();
let voiceTimer = null; // following her voice, while she speaks
let voiceAt = 0;
let feelTimer = null;

// The sentence being said: its units' spans, the rows they're in (each put up as she reaches it), how far she is.
const pacer = new Pacer();
let sentence = null; // {spans, rows: [{el, first}], shown: index of the last lit unit, index, text}
let stepTimer = null;

// ---------------------------------------------------------------- text

function h(tag, cls, kids = []) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  for (const k of [].concat(kids)) if (k != null) node.append(k);
  return node;
}

// As on her panels: letters in a formula are variables, in italics; a hyphen is a minus.
function mathText(v) {
  const out = document.createDocumentFragment();
  for (const run of v.replace(/-/g, '−').match(/[A-Za-z]+|[^A-Za-z]+/g) || []) {
    out.append(/[A-Za-z]/.test(run) ? h('i', 'var', run) : document.createTextNode(run));
  }
  return out;
}

function tex(nodes) {
  return nodes.map((n) => {
    if (n.t === 'text') return mathText(n.v);
    if (n.t === 'word') return document.createTextNode(n.v);
    if (n.t === 'sup' || n.t === 'sub') return h(n.t, null, tex(n.kids));
    if (n.t === 'frac') return h('span', 'frac', [h('span', 'num', tex(n.num)), h('span', 'den', tex(n.den))]);
    if (n.t === 'sqrt') return h('span', 'sqrt', ['√', h('span', 'radicand', tex(n.kids))]);
    return h('span', null, tex(n.kids || []));
  });
}

// Display math ($$...$$) in a subtitle sits inline: it's one sentence, not a page.
const parse = (text) => parseInline(String(text).replace(/\$\$([\s\S]+?)\$\$/g, (_m, t) => `$${t.trim()}$`));

const WRAP = { b: 'strong', i: 'em', s: 'del' };

// One unit (words.js): a word with its punctuation, or a formula or code whole, in its own span so it can light up.
function unitNode(unit) {
  const span = h('span', 'w');
  for (const part of unit.parts) {
    let node;
    if (part.t === 'math') node = h('span', 'math', tex(parseTex(part.tex)));
    else if (part.t === 'code') {
      node = h('code');
      node.textContent = part.v;
    } else node = document.createTextNode(part.v);
    for (const w of [...(part.wrap || [])].reverse()) node = h(WRAP[w], null, node);
    span.append(node);
  }
  return span;
}

// The sentence's units cut into rows as the window wraps them, measured once with every word in place, so a row never
// rewraps as its words come in. [[span, ...], ...]
function rowsOf(spans) {
  const ink = h('span', 'ink');
  spans.forEach((span, i) => {
    if (i) ink.append(' ');
    ink.append(span);
  });
  measure.replaceChildren(h('div', 'line', ink));
  const rows = [];
  let lastLeft = Infinity;
  let lastTop = -Infinity;
  for (const span of spans) {
    const r = span.getBoundingClientRect();
    // A unit further left than the one before it, and lower, starts a new row.
    if (!rows.length || (r.left < lastLeft - 0.5 && r.top > lastTop + 0.5)) rows.push([]);
    rows[rows.length - 1].push(span);
    lastLeft = r.left;
    lastTop = r.top;
  }
  measure.replaceChildren();
  return rows;
}

// ---------------------------------------------------------------- where things sit

function applyLayout() {
  const { u, maxWidth, maxHeight } = geometry(window.innerWidth, window.innerHeight, layout.anchor, layout.flipX, layout.flipY);
  const s = stage.style;
  s.setProperty('--ux', `${u.x}px`);
  s.setProperty('--uy', `${u.y}px`);
  s.setProperty('--max-w', `${maxWidth}px`);
  s.setProperty('--max-h', `${maxHeight}px`);
  stage.classList.toggle('flip-x', layout.flipX);
  stage.classList.toggle('flip-y', layout.flipY);
  const sx = layout.flipX ? -1 : 1;
  const sy = layout.flipY ? -1 : 1;
  dust.forEach((d, i) => {
    d.style.left = `${layout.anchor.x + sx * DUST[i][0]}px`;
    d.style.top = `${layout.anchor.y + sy * DUST[i][1]}px`;
  });
  connector.setAttribute('x1', String(layout.anchor.x));
  connector.setAttribute('y1', String(layout.anchor.y));
  connector.setAttribute('x2', String(u.x));
  connector.setAttribute('y2', String(u.y));
  s.setProperty('--gap', `${TEXT_GAP}px`);
  fitUnderline();
}

// The box round the rows that stay, as laid out (a row gliding up glides into it; one leaving fades above it), and
// the underline as wide as they are, out to the box's far edge: it's the box's rim, lit.
function fitUnderline() {
  let width = 0;
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  for (const el of lines.children) {
    if (el.classList.contains('leaving')) continue;
    width = Math.max(width, el.getBoundingClientRect().width);
    left = Math.min(left, el.offsetLeft);
    right = Math.max(right, el.offsetLeft + el.offsetWidth);
    top = Math.min(top, el.offsetTop);
    bottom = Math.max(bottom, el.offsetTop + el.offsetHeight);
  }
  width = Math.ceil(width + (width ? BOX.x : 0));
  stage.style.setProperty('--line-w', `${width}px`);
  box.classList.toggle('up', right > left);
  if (right > left) {
    const at = lines.offsetTop; // the rows' place in the view
    box.style.left = `${left - BOX.x}px`;
    box.style.top = `${at + top - BOX.top}px`;
    box.style.width = `${right - left + 2 * BOX.x}px`;
    box.style.height = `${bottom - top + BOX.top + BOX.bottom}px`;
  }
}

// ---------------------------------------------------------------- her state

// A feeling for a while (a sentence's, a mood's): the lean of her glow (registered properties, so it eases over; only
// on .glow, see the CSS), and how the next rows and sparks come (`felt`, fixed on each as it starts: see the CSS).
let felt = { lift: 0, energy: 0 };

function feel(emotion, intensity, seconds = null) {
  felt = feelOf(emotion, Number(intensity));
  const { cyan, violet } = leanOf(felt.lift);
  glow.style.setProperty('--cyan', cyan.toFixed(3));
  glow.style.setProperty('--violet', violet.toFixed(3));
  clearTimeout(feelTimer);
  feelTimer = seconds > 0 ? setTimeout(() => feel(null, 0), Math.min(seconds, 120) * 1000) : null;
}

// Her voice level (the orb's 'mouth' messages): followed VOICE_FPS times a second while she speaks, then still. A
// timer, not every display frame: only opacities and a transform change, which the compositor shows on its next frame.
// It's also the words' clock: they wait for her through a long silence.
function hear(level) {
  voice.set(Number(level));
  if (sentence) {
    pacer.heard(Number(level), performance.now());
    if (!stepTimer && !sentence.atEnd && Number(level) > 0.01) step(); // voicing again after a wait
  }
  if (!voiceTimer && stage.classList.contains('on')) {
    voiceAt = performance.now();
    voiceTimer = setTimeout(followVoice, 1000 / VOICE_FPS);
  }
}

function followVoice() {
  const now = performance.now();
  const dt = Math.min(0.1, (now - voiceAt) / 1000);
  voiceAt = now;
  voice.tick(dt);
  lightUp(voice.voice);
  if (ghosts.size) moveGhosts(now);
  haze.style.transform = `translateX(${(-voice.haze).toFixed(1)}px)`;
  if (voice.calm || !stage.classList.contains('on')) {
    lightUp(0);
    voiceTimer = null;
    if (ghosts.size && !ghostTimer) ghostTimer = setTimeout(ghostBeat, 1000 / VOICE_FPS);
    return;
  }
  voiceTimer = setTimeout(followVoice, 1000 / VOICE_FPS);
}

function lightUp(v) {
  lit.style.opacity = (0.55 * v).toFixed(3);
  wire.style.opacity = (0.7 + 0.3 * v).toFixed(3);
  hazeBox.style.opacity = (0.35 + 0.65 * v).toFixed(3);
}

// Fixes the feeling on an element as its animation starts: changed later, it would redraw it.
function stamp(el) {
  el.style.setProperty('--l', felt.lift.toFixed(2));
  el.style.setProperty('--e', felt.energy.toFixed(2));
}

// ---------------------------------------------------------------- rows and words

// As many sparks as the sentence has feeling: one for a flat line, all of them for an excited one.
function sparkle(intensity) {
  const { anchor, flipX, flipY } = layout;
  const sx = flipX ? -1 : 1;
  const sy = flipY ? -1 : 1;
  const k = Number.isFinite(intensity) ? Math.min(1, Math.max(0, intensity)) : 0.3;
  const count = 1 + Math.round(k * (sparks.length - 1));
  sparks.forEach((spark, i) => {
    if (i >= count) {
      spark.classList.remove('go');
      return;
    }
    // Round her edge where the line meets it, and one along the line: a little different each time.
    const last = i === count - 1 && count > 1;
    const along = last ? 0.55 : Math.random() * 0.25;
    const x = anchor.x - sx * along * 34 + (Math.random() * 26 - 13) * (last ? 0.3 : 1);
    const y = anchor.y - sy * along * 34 + (Math.random() * 26 - 13) * (last ? 0.3 : 1);
    spark.style.left = `${Math.round(x)}px`;
    spark.style.top = `${Math.round(y)}px`;
    spark.style.animationDelay = `${Math.round(i * 110 + Math.random() * 140)}ms`;
    spark.classList.remove('go');
    stamp(spark);
    void spark.offsetWidth; // restarts the one-off twinkle
    spark.classList.add('go');
  });
}

function cancelFade() {
  clearTimeout(fadeTimer);
  clearTimeout(goneTimer);
  fadeTimer = goneTimer = null;
  stage.classList.remove('fading', 'cut');
  stage.style.transitionDuration = '';
}

// A row goes up under the others, which glide up to make room (FLIP); past the last MAX_LINES, the oldest leaves.
function putUp(row) {
  const before = new Map([...lines.children].map((el) => [el, el.getBoundingClientRect().top]));
  for (const el of lines.children) el.classList.replace('now', 'past');
  stamp(row);
  lines.append(row);
  const kept = [...lines.children].filter((el) => !el.classList.contains('leaving'));
  for (const old of kept.slice(0, Math.max(0, kept.length - MAX_LINES))) leave(old);
  for (const [el, top] of before) {
    const dy = top - el.getBoundingClientRect().top;
    if (!dy) continue;
    el.style.transition = 'none';
    el.style.transform = `translateY(${dy}px)`;
    void el.offsetWidth;
    el.style.transition = '';
    el.style.transform = '';
  }
  fitUnderline();
  const { maxHeight } = geometry(window.innerWidth, window.innerHeight, layout.anchor, layout.flipX, layout.flipY);
  view.classList.toggle('tall', lines.scrollHeight > maxHeight); // clipped only when it has to be (see .view.tall)
}

// Lights the units up to `upto` (the one she's saying), putting up each row as she reaches it. Catching up over
// several rows at once (the words ran behind her), only the last goes up: the others would flash in and straight out.
function lightTo(upto) {
  if (!sentence) return;
  const { spans, rows } = sentence;
  const to = Math.min(upto, spans.length - 1);
  const reached = rows.filter((r) => r.first > sentence.shown && r.first <= to);
  if (reached.length) putUp(reached[reached.length - 1].el);
  for (let i = sentence.shown + 1; i <= to; i += 1) {
    if (i > 0) spans[i - 1].classList.replace('now', 'said');
    spans[i].classList.add('now');
    sentence.shown = i;
    if (i === to) materialize(spans[i]); // catching up over several, only the one she's on
  }
}

// A word comes in as a PS2 menu's text does: a cool, soft ghost of it (blurred, a touch large, a faint red and cyan
// fringe, interlaced) settles onto it and fades as the word itself shows. The ghost is its own small layer beside the
// row's text, never under the text's filters, and only its transform, opacity and blur move, once, for GHOST_MS: the
// row's text isn't redrawn for it (a fade of the word itself was: GPU 3D 9.7% against 1.8%). It moves on her voice's
// own beat (followVoice), so it adds no frames of its own while she talks.
const ghosts = new Set();
let ghostTimer = null;

function materialize(span) {
  const row = span.closest('.line');
  if (!row || !span.offsetWidth) return;
  const ghost = h('span', 'ghost');
  for (const kid of span.childNodes) ghost.append(kid.cloneNode(true));
  ghost.style.left = `${span.offsetLeft}px`;
  ghost.style.top = `${span.offsetTop}px`;
  ghost.born = performance.now();
  ghost.ms = GHOST_MS * (1 - 0.2 * felt.energy); // quicker when she's lively
  row.append(ghost);
  ghosts.add(ghost);
  moveGhosts(ghost.born);
  if (!voiceTimer && !ghostTimer) ghostTimer = setTimeout(ghostBeat, 1000 / VOICE_FPS);
}

// Between her voice's beats (she's quiet, or her voice level isn't coming): the ghosts keep their own.
function ghostBeat() {
  ghostTimer = null;
  moveGhosts(performance.now());
  if (ghosts.size && !voiceTimer) ghostTimer = setTimeout(ghostBeat, 1000 / VOICE_FPS);
}

const easeOut = (t) => 1 - (1 - t) ** 3;

function moveGhosts(now) {
  for (const g of ghosts) {
    const p = Math.min(1, (now - g.born) / g.ms);
    if (p >= 1 || !g.isConnected) {
      g.remove();
      ghosts.delete(g);
      continue;
    }
    const settle = easeOut(p);
    // In over the first 20% (the word itself is already there), from a little above, larger and blurred; then out
    // as it sharpens onto the word. Quick in, so the word never looks late.
    const opacity = p < 0.2 ? easeOut(p / 0.2) : 1 - easeOut((p - 0.2) / 0.8);
    g.style.opacity = opacity.toFixed(3);
    g.style.transform = `translateY(${(-6 * (1 - settle)).toFixed(2)}px) scale(${(1 + 0.26 * (1 - settle)).toFixed(3)})`;
    g.style.filter = `blur(${(7 * (1 - settle) ** 1.5).toFixed(2)}px)`;
  }
}

// Looks at her clock and lights what she's reached; looks again when the next word's due, or LOOK_MS while she's
// quiet (her clock waits then, and starts again with her voice: `hear`).
// With her real word times coming, the pacing only fills in when they stop for a while (her audio still coming in
// faster than the times can follow, or a sentence too long to time): from the last real word, never ahead of one.
function step() {
  clearTimeout(stepTimer);
  stepTimer = null;
  if (!sentence) return;
  const now = performance.now();
  if (!sentence.timed || now - sentence.wordAt > WORD_GRACE_MS) lightTo(pacer.index(now));
  const next = pacer.nextMs(now);
  if (next != null) stepTimer = setTimeout(step, Math.min(next, LOOK_MS));
  else sentence.atEnd = true; // on its last word: nothing more to look for
}

// The sentence is over (the next one's starting, or she's done): what's left of it shows at once.
function finishSentence(lightAll) {
  clearTimeout(stepTimer);
  stepTimer = null;
  if (!sentence) return;
  pacer.finish(performance.now(), lightAll); // cut off, it says nothing about her pace
  if (lightAll) lightTo(sentence.spans.length - 1);
  const last = sentence.spans[sentence.shown];
  if (last) last.classList.replace('now', 'said');
  sentence = null;
}

// `said`: the line as it came, with the feeling it carries (emotion, intensity), the mood she's in (mood), what her
// voice says of it (said), its place in the reply (index) and its audio's length if it's known yet (seconds).
function addLine(text, holdS, said = {}) {
  cancelFade();
  finishSentence(true);
  const fresh = !stage.classList.contains('on') || ended;
  ended = false;
  if (!stage.classList.contains('on')) {
    lines.replaceChildren();
    fitUnderline();
    stage.classList.add('on');
  }
  // A new reply over the last one's lingering rows: they drift out as it comes in, rather than vanishing.
  if (fresh) for (const el of lines.children) leave(el);
  // The sentence's own feeling, or the mood she's in when it has none.
  const own = feelOf(said.emotion, Number(said.intensity));
  if (own.lift || own.energy || !said.mood) feel(said.emotion, said.intensity);
  else feel(said.mood.emotion, said.mood.intensity, said.mood.seconds);

  const list = units(parse(text));
  if (!list.length) return;
  const spans = list.map(unitNode);
  const rows = rowsOf(spans).map((row) => {
    const ink = h('span', 'ink');
    row.forEach((span, i) => {
      if (i) ink.append(' ');
      ink.append(span);
    });
    return { el: h('div', 'line now', ink), first: spans.indexOf(row[0]) };
  });
  // Which spoken word each unit starts at, for her real word times ('segment-word' counts what her voice says).
  const words = spokenWords(said.said);
  const starts = startWords(list, words);
  sentence = { spans, rows, shown: -1, index: said.index ?? null, text, starts, words: words.length, timed: false };
  const seconds = Number(holdS) > 0 ? Number(holdS) : Number(said.seconds);
  pacer.start(list.map((u) => u.weight), performance.now(), spokenWeight(said.said), seconds > 0 ? seconds : null);
  step();
  sparkle(Number(said.intensity));
  if (holdS) fadeTimer = setTimeout(() => fade(false), holdS * 1000 + tailMs(text) * 0.6); // a phrase: no end follows
}

// The voice has made all of a sentence's audio: its length, for her clock.
function lengthOf(p) {
  if (sentence && p && Number(p.index) === sentence.index) {
    pacer.length(Number(p.seconds));
    step();
  }
}

// She's reached a word, by her audio as it plays (her word times): from here her words follow those, not the pacing.
// A word the pacing already showed stays shown.
function wordAt(p) {
  // Another sentence's (or a reply's that never played): its words aren't these.
  if (!sentence || !p || Number(p.index) !== sentence.index || Number(p.of) !== sentence.words) return;
  sentence.timed = true;
  sentence.wordAt = performance.now();
  const unit = unitFor(Number(p.word), sentence.starts);
  pacer.sync(unit, sentence.wordAt); // should they stop, the pacing goes on from here
  lightTo(unit);
}

// Out over the top, drifting up as it fades; the underline narrows to what's left.
function leave(el) {
  if (el.classList.contains('leaving')) return;
  el.classList.remove('now');
  el.classList.add('leaving');
  fitUnderline(); // the box shrinks to what stays as it goes
  setTimeout(() => {
    el.remove();
    fitUnderline();
  }, RISE_MS);
}

// `cut`: talked over, gone at once, nearly. `leavingMs`: she's leaving, and the words fade over the time she takes.
function fade(cut, leavingMs = 0) {
  clearTimeout(fadeTimer);
  fadeTimer = null;
  if (!stage.classList.contains('on')) {
    host.faded(seq);
    return;
  }
  const ms = cut ? CUT_FADE_MS : leavingMs > 0 ? leavingMs : FADE_MS;
  stage.style.transitionDuration = cut || !(leavingMs > 0) ? '' : `${ms}ms`;
  stage.classList.add('fading');
  stage.classList.toggle('cut', cut);
  clearTimeout(goneTimer);
  goneTimer = setTimeout(() => {
    finishSentence(false);
    stage.style.transitionDuration = '';
    stage.classList.remove('on', 'fading', 'cut');
    lines.replaceChildren();
    fitUnderline();
    feel(null, 0);
    host.faded(seq);
  }, ms);
}

function end({ interrupted, leavingMs }) {
  clearTimeout(fadeTimer);
  ended = true;
  // Done: the rest of her last sentence shows to be read (cut off, it simply goes).
  const text = sentence ? sentence.text : '';
  finishSentence(!interrupted);
  if (interrupted) fade(true);
  else if (Number(leavingMs) > 0) fade(false, Math.min(Number(leavingMs), 3000)); // with her, as she goes
  else fadeTimer = setTimeout(() => fade(false), tailMs(text));
}

// ---------------------------------------------------------------- the pointer

// Her own see-through rule, with the words as her pixels and their neighbourhood as her window. Nothing here ever
// takes the mouse, so there's no Ctrl to bring them back.
const seeThrough = new SeeThrough({ setIgnoreMouse() {}, emit() {} }, (on) => document.documentElement.classList.toggle('see-through', on));
let leaveTimer = null;

function onWords(x, y, pad) {
  if (!stage.classList.contains('on') || stage.classList.contains('fading')) return false;
  for (const el of [box, ...lines.children, underline]) {
    if (el.classList.contains('leaving') || (el === box && !box.classList.contains('up'))) continue;
    const r = el.getBoundingClientRect();
    if (r.width && x >= r.left - pad && x < r.right + pad && y >= r.top - pad && y < r.bottom + pad) return true;
  }
  return false;
}

// `p`: where the cursor is in the window, from the main process's poll ({x, y, inside}).
function pointer(p) {
  clearTimeout(leaveTimer);
  const x = Number(p.x);
  const y = Number(p.y);
  const near = p.inside === true && onWords(x, y, NEAR_PX);
  const over = seeThrough.follow(near && onWords(x, y, ON_PX), near, performance.now());
  seeThrough.setOver(over);
  // The poll stops once the cursor's out of the window: say it left after LEAVE_MS all the same.
  if (over && !near) leaveTimer = setTimeout(() => pointer({ inside: false }), LEAVE_MS + 20);
}

// ---------------------------------------------------------------- wiring

host.onLayout((l) => {
  if (!l || !l.anchor) return;
  layout = l;
  applyLayout();
});
host.onLine((p) => {
  if (!p) return;
  seq = Number(p.seq) || 0;
  addLine(String(p.text || ''), p.hold || null, p);
});
host.onEnd((p) => end(p || {}));
if (host.onLength) host.onLength((p) => lengthOf(p));
if (host.onWord) host.onWord((p) => wordAt(p));
if (host.onFeel) host.onFeel((p) => p && stage.classList.contains('on') && feel(p.emotion, p.intensity, Number(p.seconds) || 4));
if (host.onVoice) host.onVoice((level) => hear(level));
if (host.onPointer) host.onPointer((p) => p && pointer(p));
window.addEventListener('resize', applyLayout);
applyLayout();
host.hello();
