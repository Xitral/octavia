// Her subtitles word by word (docs/subtitles.md), with no DOM in it (tested in node): a sentence cut into the units
// that light up one at a time, how long each takes to say, and which one she's saying now.
//
// Her words' real times come from her audio (tts/word_times.py: 'segment-word' as she reaches each); until they do
// (the first sentence of a reply often starts before its audio is all in), or without them, it's paced: each unit weighs its syllables (and the pause
// after a comma or a full stop), the sentence's length is the voice's own count of its audio when it comes (the
// 'segment-audio' message, as the voice finishes making it), or her recent pace times the syllables until then. The
// clock is the audio's, as near as the page can tell: it runs while she's voicing and through a short pause, and
// stops in a long silence (the voice's audio running late mid-sentence, a long breath), so the words wait for her
// rather than run on ahead.

// A unit is a run of the sentence without a space in it: a word with its punctuation, or a formula or bit of code
// whole (its parts, as the panels' Markdown reader gives them: {t: 'text'|'math'|'code', ..., wrap: ['b','i']}).

export const GAP_MS = 520; // a silence longer than this stops her clock (a pause between words is shorter)
export const DEFAULT_RATE = 0.2; // seconds per syllable, about: Fish at speed 1.0 (learnt as she goes)
const MIN_RATE = 0.09;
const MAX_RATE = 0.42;
const LEARN = 0.3; // how much of each sentence's pace goes into the next one's guess

const COMMA_PAUSE = 0.7; // in syllables: the pause after a comma, a colon, a dash
const STOP_PAUSE = 1.2; // and after a full stop inside the sentence

// About how many syllables a word has: its vowel groups, less a silent final e. Digits are said as numbers.
export function syllables(word) {
  const w = String(word).toLowerCase();
  let n = 0;
  for (const digits of w.match(/\d+/g) || []) n += digits.length <= 2 ? digits.length + 0.5 : digits.length * 1.4;
  const letters = w.replace(/[^a-z]/g, ' ').trim();
  for (const part of letters ? letters.split(/\s+/) : []) {
    let groups = (part.match(/[aeiouy]+/g) || []).length;
    if (groups > 1 && /[^aeiouy]e$/.test(part) && !/[^aeiouy]le$/.test(part)) groups -= 1;
    n += Math.max(1, groups);
  }
  // A symbol said as a word: "%" percent, "&" and, "+" plus, "=" equals.
  n += (w.match(/[%&+=<>×÷@#]/g) || []).length * 1.5;
  return n;
}

// How long a formula or code takes to say, in syllables: about one a symbol ("x squared plus one").
function spokenSymbols(text) {
  const t = String(text).replace(/\\[a-zA-Z]+/g, 'x').replace(/\s+/g, '');
  return Math.max(1, t.length * 0.75);
}

function partText(part) {
  if (part.t === 'math') return part.tex || '';
  return part.v || '';
}

// What a unit weighs, in syllables: what's said of it and the pause after it.
export function weigh(parts) {
  let w = 0;
  let tail = '';
  for (const part of parts) {
    if (part.t === 'math' || part.t === 'code') w += spokenSymbols(partText(part));
    else w += syllables(part.v);
    tail = part.t === 'text' ? part.v : '';
  }
  if (/[.!?…]["')\]]*$/.test(tail)) w += STOP_PAUSE;
  else if (/[,;:—–-]["')\]]*$/.test(tail)) w += COMMA_PAUSE;
  return Math.max(0.5, w);
}

// The panels' inline nodes for a sentence (panel/markdown.js parseInline) as units: [{parts, weight}]. Bold and
// italics carry over to the words in them (`wrap`); a line break is a space.
export function units(nodes) {
  const out = [];
  let open = false; // the last unit can still take more (no space since)
  const add = (part) => {
    if (open) out[out.length - 1].parts.push(part);
    else out.push({ parts: [part] });
    open = true;
  };
  const walk = (list, wrap) => {
    for (const n of list || []) {
      if (n.t === 'text') {
        for (const piece of String(n.v).split(/(\s+)/)) {
          if (!piece) continue;
          if (/^\s+$/.test(piece)) open = false;
          else add({ t: 'text', v: piece, wrap });
        }
      } else if (n.t === 'br') open = false;
      else if (n.t === 'code') add({ t: 'code', v: n.v, wrap });
      else if (n.t === 'math') add({ t: 'math', tex: n.tex, wrap });
      else if (n.t === 'b' || n.t === 'i' || n.t === 's') walk(n.kids, [...wrap, n.t]);
      else if (n.kids) walk(n.kids, wrap);
    }
  };
  walk(nodes, []);
  for (const u of out) u.weight = weigh(u.parts);
  return out;
}

const CUES = /\[[a-z][a-z ]{0,30}\]/g;
const CONTROL = /<\|[^|>]*\|>/g;

// The words her voice says of a sentence: without the voice cues ("[excited]") or marks. The same words, in the same
// order, as her word times count (tts/word_times.py spoken_words).
export function spokenWords(said) {
  return String(said || '')
    .replace(CUES, ' ')
    .replace(CONTROL, ' ')
    .split(/\s+/)
    .filter(Boolean);
}

// Each in syllables.
export const spokenWeights = (said) => spokenWords(said).map((word) => weigh([{ t: 'text', v: word }]));

export const spokenWeight = (said) => spokenWeights(said).reduce((a, b) => a + b, 0);

const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '');

// The spoken word each unit starts at. Units that are words she says as written match those words (longest common
// run, in order); the rest (a formula shown short but said long, "50%" said "fifty percent") share the words between
// the matched ones around them, in order.
export function startWords(list, spoken) {
  const a = list.map((u) => norm(u.parts.map((p) => (p.t === 'text' ? p.v : '\u0000')).join('')));
  const b = spoken.map(norm);
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Int16Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] && a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const at = new Array(n).fill(null);
  for (let i = 0, j = 0; i < n && j < m; ) {
    if (a[i] && a[i] === b[j]) {
      at[i] = j;
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) i += 1;
    else j += 1;
  }
  // The gaps: units with no match spread over the words between their matched neighbours.
  for (let i = 0; i < n; ) {
    if (at[i] != null) {
      i += 1;
      continue;
    }
    let k = i;
    while (k < n && at[k] == null) k += 1;
    const from = i > 0 ? at[i - 1] + 1 : 0;
    const to = k < n ? at[k] : m; // words from..to-1 are theirs
    const count = k - i;
    // (none left after the last matched word: they start with it, so they light as she reaches it)
    const last = Math.max(0, m - 1);
    for (let q = 0; q < count; q += 1) at[i + q] = Math.min(Math.max(Math.min(from, last), to - 1, 0), last, from + Math.floor(((to - from) * q) / count));
    i = k;
  }
  return at;
}

// The unit she's on when she starts spoken word `word`: the last one starting at or before it.
export function unitFor(word, starts) {
  let unit = starts.length ? 0 : -1;
  for (let i = 0; i < starts.length; i += 1) if (starts[i] <= word) unit = i;
  return unit;
}

// Which unit she's saying, for one sentence. `now` is milliseconds (performance.now()).
export class Pacer {
  constructor(rate = DEFAULT_RATE) {
    this.rate = rate; // seconds per syllable, learnt from each sentence she finishes
    this.weights = [];
    this.active = false;
  }

  // A sentence starts playing. `weights`: its units'; `said`: the syllables her voice says of it (0: the units');
  // `seconds`: its audio's length, when the voice has said.
  start(weights, now, said = 0, seconds = null) {
    this.weights = weights.slice();
    this.total = this.weights.reduce((a, b) => a + b, 0) || 1;
    this.said = said > 0 ? said : this.total;
    this.seconds = seconds > 0 ? seconds : null;
    this.clock = 0;
    this.at = now;
    this.voicedAt = now; // her first sound comes a moment after the sentence starts
    this.heardAny = false; // no voice level yet (lip sync off sends none): the clock is the wall's until one comes
    this.active = true;
  }

  // The voice has finished making this sentence's audio: it's this long.
  length(seconds) {
    if (this.active && seconds > 0) this.seconds = seconds;
  }

  // Her voice level (the 'mouth' messages).
  heard(level, now) {
    this.advance(now);
    this.heardAny = true;
    if (level > 0.01) this.voicedAt = now;
  }

  advance(now) {
    if (!this.active) return;
    // The clock runs up to GAP_MS into a silence, then waits for her (when her voice level is coming at all).
    const until = this.heardAny ? Math.min(now, this.voicedAt + GAP_MS) : now;
    this.clock += Math.max(0, until - this.at);
    this.at = Math.max(this.at, now);
  }

  // How long the whole sentence takes to say, in ms.
  durationMs() {
    return (this.seconds || this.said * this.rate) * 1000;
  }

  // The unit she's saying now: the one whose share of the sentence the clock is in. The last one waits for the end.
  index(now) {
    this.advance(now);
    if (!this.weights.length) return -1;
    const at = (this.clock / this.durationMs()) * this.total;
    let sum = 0;
    for (let i = 0; i < this.weights.length; i += 1) {
      sum += this.weights[i];
      if (at < sum) return i;
    }
    return this.weights.length - 1;
  }

  // A real time says she's starting unit `unit` now: the clock goes to it (the pacing carries on from there).
  sync(unit, now) {
    if (!this.active || unit < 0) return;
    this.advance(now);
    let before = 0;
    for (let k = 0; k < Math.min(unit, this.weights.length); k += 1) before += this.weights[k];
    this.clock = (before / this.total) * this.durationMs();
    this.voicedAt = now;
  }

  // When the unit after the current one starts, in ms from now, if she keeps talking: when to look again.
  nextMs(now) {
    const i = this.index(now);
    if (i < 0 || i >= this.weights.length - 1) return null;
    let sum = 0;
    for (let k = 0; k <= i; k += 1) sum += this.weights[k];
    return Math.max(16, (sum / this.total) * this.durationMs() - this.clock);
  }

  // The sentence is over (the next one started, or she finished): her pace in it goes into the next guess. Not from
  // one cut off (`learn` false), nor one with no length and no voice level to time it by.
  finish(now, learn = true) {
    if (!this.active) return;
    this.advance(now);
    this.active = false;
    if (!learn || (!this.seconds && !this.heardAny)) return;
    const seconds = this.seconds || this.clock / 1000;
    if (!(this.said >= 3) || !(seconds > 0.3)) return; // too short to say much about her pace
    const rate = Math.min(MAX_RATE, Math.max(MIN_RATE, seconds / this.said));
    this.rate += LEARN * (rate - this.rate);
  }
}
