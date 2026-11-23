// The settings page's plain logic, apart from the page so node can test it: reading a key press as a hotkey, how a
// hotkey and a value are shown, whether a group applies, and marking the words a search found.

// A key press as the hotkey Octavia's config takes ("ctrl+alt+j"), '' for Backspace/Delete (off), or null for a
// modifier on its own or a key hotkeys can't use. ``event`` needs code, key and the four modifier flags.
const NAMED = {
  Space: 'space',
  Enter: 'enter',
  NumpadEnter: 'enter',
  Tab: 'tab',
  Escape: 'esc',
  Pause: 'pause',
  Insert: 'insert',
  Home: 'home',
  End: 'end',
  PageUp: 'pageup',
  PageDown: 'pagedown',
  ArrowLeft: 'left',
  ArrowUp: 'up',
  ArrowRight: 'right',
  ArrowDown: 'down',
  Semicolon: ';',
  Equal: '=',
  Comma: ',',
  Minus: '-',
  Period: '.',
  Slash: '/',
  Backquote: '`',
  BracketLeft: '[',
  Backslash: '\\',
  BracketRight: ']',
  Quote: "'",
};
const MODIFIER_CODES = /^(Control|Alt|Shift|Meta|OS|CapsLock|NumLock|ContextMenu)/;

export function hotkeyOf(event) {
  const code = String(event.code || '');
  if (code === 'Backspace' || code === 'Delete') return '';
  if (MODIFIER_CODES.test(code)) return null;
  let name = null;
  let m;
  if ((m = /^Key([A-Z])$/.exec(code))) name = m[1].toLowerCase();
  else if ((m = /^(?:Digit|Numpad)([0-9])$/.exec(code))) name = m[1];
  else if ((m = /^F([0-9]{1,2})$/.exec(code)) && Number(m[1]) >= 1 && Number(m[1]) <= 24) name = `f${m[1]}`;
  else if (NAMED[code]) name = NAMED[code];
  if (!name) return null;
  const mods = [];
  if (event.ctrlKey) mods.push('ctrl');
  if (event.altKey) mods.push('alt');
  if (event.shiftKey) mods.push('shift');
  if (event.metaKey) mods.push('win');
  return [...mods, name].join('+');
}

// "ctrl+alt+j" as "Ctrl + Alt + J"; '' as ''.
export function prettyHotkey(combo) {
  return String(combo || '')
    .split('+')
    .filter(Boolean)
    .map((part) => (part.length > 1 ? part[0].toUpperCase() + part.slice(1) : part.toUpperCase()))
    .join(' + ');
}

// Whether a group's settings apply now: a group ``when`` [key, value] applies while that setting has that value.
export function applies(group, values) {
  if (!group || !Array.isArray(group.when) || group.when.length !== 2) return true;
  const [key, value] = group.when;
  return String(values[key]) === String(value);
}

// The words of a search, lowercased, for marking.
export function queryWords(query) {
  return String(query || '')
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
}

// ``text`` cut into [part, found] pieces, a piece found where one of ``words`` starts a word of it.
export function marked(text, words) {
  const source = String(text || '');
  if (!words.length) return [[source, false]];
  const lower = source.toLowerCase();
  const hits = new Array(source.length).fill(false);
  for (const word of words) {
    let at = lower.indexOf(word);
    while (at >= 0) {
      const starts = at === 0 || !/[\p{L}\p{N}]/u.test(lower[at - 1]);
      if (starts) for (let i = at; i < at + word.length; i += 1) hits[i] = true;
      at = lower.indexOf(word, at + 1);
    }
  }
  const out = [];
  for (let i = 0; i < source.length; i += 1) {
    const last = out[out.length - 1];
    if (last && last[1] === hits[i]) last[0] += source[i];
    else out.push([source[i], hits[i]]);
  }
  return out;
}

// How long until ``until`` (epoch seconds), said plainly: "1 h 20 min", "12 min", "under a minute".
export function timeLeft(until, now = Date.now() / 1000) {
  const s = Math.max(0, Number(until) - now);
  if (s < 60) return 'under a minute';
  const minutes = Math.round(s / 60);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} h${m ? ` ${m} min` : ''}` : `${m} min`;
}

// A clock time for ``until`` (epoch seconds), "14:05".
export function clock(until) {
  const d = new Date(Number(until) * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// Next/previous option for arrow keys in a list of ``count`` items, wrapping.
export function step(index, delta, count) {
  if (count <= 0) return -1;
  return (((index + delta) % count) + count) % count;
}
