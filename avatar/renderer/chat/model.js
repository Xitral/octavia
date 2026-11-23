// What her chat window works out, apart from drawing it: plain functions, tested in node.

const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/gi;
const TRAILING = /[.,;:!?)\]}'"»”’]+$/;

// "9:41 PM"
export function clock(at) {
  return new Date(at * 1000).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

// "Thursday, October 30"
export function dayName(at) {
  return new Date(at * 1000).toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
}

// "Oct 30, 9:41 PM", for a search result from any day.
export function when(at) {
  const date = new Date(at * 1000);
  return `${date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}, ${clock(at)}`;
}

// How long ago, in a word or two: "just now", "12 min ago", "3 h ago", "2 days ago".
export function ago(at, now) {
  const s = Math.max(0, now - at);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const days = Math.floor(s / 86400);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

// What a group of tool lines says when folded: the newest, with how many there were.
export function stepsLabel(steps) {
  const last = steps[steps.length - 1] || '';
  return steps.length > 1 ? `${steps.length} steps · ${last}` : last;
}

// A text as runs of plain text and web addresses: [{text}, {url}]. Punctuation after an address isn't part of it.
export function splitLinks(text) {
  const out = [];
  let at = 0;
  for (const match of String(text).matchAll(URL_RE)) {
    const url = match[0].replace(TRAILING, '');
    if (match.index > at) out.push({ text: text.slice(at, match.index) });
    out.push({ url });
    at = match.index + url.length;
  }
  if (at < String(text).length) out.push({ text: String(text).slice(at) });
  return out;
}

// A text as runs, with what matches the search's words marked: [{text, hit}].
export function splitHits(text, query) {
  const words = String(query).toLowerCase().split(/\W+/).filter((w) => w.length > 1);
  if (!words.length) return [{ text, hit: false }];
  const pattern = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
  return String(text)
    .split(pattern)
    .filter((part) => part)
    .map((part) => ({ text: part, hit: words.includes(part.toLowerCase()) }));
}

// Up and Down through what they've sent, as a shell does. `at` is the place in `sent` (sent.length: the new message).
export function recall(sent, at, step) {
  const next = Math.min(sent.length, Math.max(0, at + step));
  return { at: next, text: next < sent.length ? sent[next] : null };
}

// Her panels as her window lists them: pinned first, then up, then closed (newest first, as they came).
export function shelfGroups(rows) {
  const groups = { pinned: [], up: [], closed: [] };
  for (const row of rows || []) {
    if (row.pinned) groups.pinned.push(row);
    else if (row.up) groups.up.push(row);
    else groups.closed.push(row);
  }
  return groups;
}
