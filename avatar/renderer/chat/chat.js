// Her chat window's page. Every word goes in as text, never as markup: her replies through the same Markdown reader
// her panels use, what else there is with textContent. Lines rise in once and stay still; nothing loops.

import { markdown } from '../panel/rich.js';
import { ago, clock, dayName, recall, shelfGroups, splitHits, splitLinks, stepsLabel, when } from './model.js';

const host = window.chatHost;
const $ = (id) => document.getElementById(id);
const log = $('log');
const today = $('today');
const past = $('past');
const box = $('box');

const SEARCH_WAIT_MS = 220;
const AT_BOTTOM_PX = 40;
const SETTLE_MS = 900; // the history comes in a burst: no rising in, and no smooth scrolling, until it has

let name = 'Octavia';
let settled = false;
let viewing = null; // the past day on show: { day, prev, next }; null for today
const sent = []; // what they've sent, for Up and Down
let sentAt = 0;
let unsent = '';
let lastToday = null; // the last day-separator's day in today's section
let searchId = 0;
let searchWait = null;
let shelfRows = [];

function h(tag, cls, kids) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const kid of kids || []) if (kid != null) el.append(kid);
  return el;
}

function text(tag, cls, words) {
  const el = h(tag, cls);
  el.textContent = words;
  return el;
}

// ---------------------------------------------------------------- web addresses she wrote, opened only on a click

function linkify(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (!walker.currentNode.parentElement.closest('code, pre, .link')) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const runs = splitLinks(node.nodeValue);
    if (!runs.some((run) => run.url)) continue;
    node.replaceWith(...runs.map((run) => {
      if (!run.url) return document.createTextNode(run.text);
      const link = text('a', 'link', run.url);
      link.dataset.url = run.url;
      return link;
    }));
  }
}

log.addEventListener('click', (event) => {
  const link = event.target.closest && event.target.closest('[data-url]');
  if (link) host.open(link.dataset.url);
});

// ---------------------------------------------------------------- the lines

function rich(words) {
  const body = h('div', 'body', markdown(words));
  linkify(body);
  return body;
}

function copyButton(words) {
  const button = text('button', 'copy', '');
  button.title = 'Copy';
  button.addEventListener('click', () => {
    host.copy(words);
    button.classList.add('done');
    setTimeout(() => button.classList.remove('done'), 900);
  });
  return button;
}

function spoken(who, words, at) {
  const yours = who === 'you';
  const head = h('header', 'who', [
    text('b', null, yours ? 'You' : name),
    text('time', null, clock(at)),
    copyButton(words),
  ]);
  const line = h('article', `line ${yours ? 'you' : 'her'}`, [head, rich(words)]);
  line.dataset.at = String(at);
  return line;
}

// Her tool lines fold into one row: the newest shows, the rest open with a click.
function addStep(section, words, at) {
  let group = section.lastElementChild;
  if (!group || !group.classList.contains('steps')) {
    const summary = h('summary');
    group = h('details', 'steps', [summary, h('ol')]);
    group.dataset.at = String(at);
    group.steps = [];
    section.append(group);
  }
  group.steps.push(words);
  group.querySelector('summary').textContent = stepsLabel(group.steps);
  const item = h('li', null, [text('time', null, clock(at)), text('span', null, words)]);
  group.querySelector('ol').append(item);
  return group;
}

function separator(at) {
  return text('div', 'day', dayName(at));
}

function addLine(section, who, words, at) {
  if (!String(words).trim()) return null;
  const day = dayName(at);
  if (section !== today || lastToday !== day) {
    if (section === today) {
      lastToday = day;
      section.append(separator(at));
    } else if (section.dataset.day !== day) {
      section.dataset.day = day;
      section.append(separator(at));
    }
  }
  const el = who === 'did' ? addStep(section, words, at) : spoken(who, words, at);
  if (who !== 'did') section.append(el);
  if (who === 'her' && section === today) { // what the small window shows
    for (const old of today.querySelectorAll('.newest')) old.classList.remove('newest');
    el.classList.add('newest');
  }
  if (settled) el.classList.add('rise');
  if (who === 'you' && section === today && sent[sent.length - 1] !== words) {
    sent.push(words);
    sentAt = sent.length;
  }
  return el;
}

function atBottom() {
  return log.scrollHeight - log.scrollTop - log.clientHeight < AT_BOTTOM_PX;
}

function toBottom(smooth) {
  log.scrollTo({ top: log.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

let following = true;
let noEarlier = false; // she said there's nothing before today

function showEarlier() {
  $('earlier').hidden = noEarlier || viewing !== null || log.scrollTop > 60;
}

log.addEventListener('scroll', () => {
  following = atBottom();
  if (following) $('latest').hidden = true;
  showEarlier();
});
$('latest').addEventListener('click', () => {
  $('latest').hidden = true;
  toBottom(true);
});

function onLine(msg) {
  const wasFollowing = following || !settled;
  addLine(today, String(msg.who || ''), String(msg.text || ''), Number(msg.at) || Date.now() / 1000);
  if (viewing) return;
  if (wasFollowing) toBottom(settled);
  else $('latest').hidden = false;
  showEarlier();
}

// ---------------------------------------------------------------- past days

function showToday(focus) {
  viewing = null;
  $('daybar').hidden = true;
  past.hidden = true;
  today.hidden = false;
  setTimeout(() => {
    if (focus) focusLine(today, focus);
    else toBottom(false);
    showEarlier();
  });
}

function focusLine(section, at) {
  const lines = [...section.querySelectorAll('[data-at]')];
  if (!lines.length) return;
  const nearest = lines.reduce((best, el) => (Math.abs(el.dataset.at - at) < Math.abs(best.dataset.at - at) ? el : best));
  nearest.scrollIntoView({ block: 'center' });
  nearest.classList.remove('found');
  void nearest.offsetWidth; // the fade starts over when the same line is found twice
  nearest.classList.add('found');
}

function onDay(msg) {
  if (msg.today) return showToday(msg.focus);
  if (msg.none) {
    noEarlier = true;
    showEarlier();
    return undefined;
  }
  viewing = { day: Number(msg.day), prev: msg.prev === true, next: msg.next === true };
  past.replaceChildren();
  delete past.dataset.day;
  for (const line of msg.lines || []) addLine(past, line.who, line.text, line.at);
  today.hidden = true;
  past.hidden = false;
  $('earlier').hidden = true;
  $('daybar').hidden = false;
  $('day-name').textContent = dayName(viewing.day);
  $('day-back').disabled = !viewing.prev;
  setTimeout(() => (msg.focus ? focusLine(past, msg.focus) : (log.scrollTop = 0)));
  return undefined;
}

$('earlier').addEventListener('click', () => {
  const first = today.querySelector('[data-at]');
  host.send({ type: 'history', before: first ? Number(first.dataset.at) : Date.now() / 1000 });
});
$('day-back').addEventListener('click', () => viewing && host.send({ type: 'history', before: viewing.day }));
$('day-next').addEventListener('click', () => viewing && host.send({ type: 'history', after: viewing.day }));
$('day-today').addEventListener('click', () => showToday());

// ---------------------------------------------------------------- search

function openSearch(on) {
  $('search').hidden = !on;
  if (on) {
    $('query').focus();
    $('query').select();
  } else {
    box.focus();
  }
}

function runSearch() {
  clearTimeout(searchWait);
  searchWait = setTimeout(() => {
    const query = $('query').value.trim();
    searchId += 1;
    if (query.length < 2) {
      $('results').replaceChildren();
      return;
    }
    host.send({ type: 'search', id: searchId, query });
  }, SEARCH_WAIT_MS);
}

function onFound(msg) {
  if (msg.id !== searchId) return; // an older search, answered late
  const rows = (msg.results || []).map((row) => {
    const snippet = h('p', null, splitHits(row.text, msg.query).map((run) => (run.hit ? text('mark', null, run.text) : document.createTextNode(run.text))));
    const open = h('button', 'result', [
      h('header', null, [text('b', null, row.who === 'you' ? 'You' : name), text('time', null, when(row.at))]),
      snippet,
    ]);
    open.addEventListener('click', () => {
      openSearch(false);
      host.send({ type: 'history', at: row.at });
    });
    return open;
  });
  $('results').replaceChildren(...(rows.length ? rows : [text('p', 'none', 'Nothing said matches that.')]));
}

$('query').addEventListener('input', runSearch);
$('open-search').addEventListener('click', () => openSearch($('search').hidden));
$('search-close').addEventListener('click', () => openSearch(false));

// ---------------------------------------------------------------- her panels: pin, bring back

function rowButton(label, action, id, title) {
  const button = text('button', 'plain', label);
  if (title) button.title = title;
  button.addEventListener('click', () => {
    button.disabled = true;
    host.send({ type: 'panel', action, id });
  });
  return button;
}

function shelfRow(row, now) {
  const buttons = [];
  if (row.up) {
    buttons.push(rowButton(row.pinned ? 'Unpin' : 'Pin', row.pinned ? 'unpin' : 'pin', row.id));
    buttons.push(rowButton('Close', 'close', row.id));
  } else {
    buttons.push(rowButton('Bring back', 'bring', row.id));
    if (!row.pinned) buttons.push(rowButton('✕', 'forget', row.id, 'Forget it'));
  }
  const detail = row.up ? row.about : `${row.at ? ago(row.at, now) : ''}${row.about ? ` · ${row.about}` : ''}`;
  return h('div', 'row', [h('div', 'what', [text('b', null, row.title), text('small', null, detail)]), h('div', 'buttons', buttons)]);
}

function drawShelf() {
  const { pinned, up, closed } = shelfGroups(shelfRows);
  const now = Date.now() / 1000;
  const group = (title, rows) => (rows.length ? [text('h2', null, title), ...rows.map((row) => shelfRow(row, now))] : []);
  const kids = [...group('Pinned', pinned), ...group('Up now', up), ...group('Closed', closed)];
  $('shelf-list').replaceChildren(...(kids.length ? kids : [text('p', 'none', "She hasn't shown a panel lately.")]));
  $('shelf-count').hidden = !pinned.length;
  $('shelf-count').textContent = String(pinned.length);
}

function openShelf(on) {
  $('shelf').hidden = !on;
  $('shelf').classList.toggle('open', on);
  if (on) {
    $('shelf-note').hidden = true;
    drawShelf();
  } else {
    box.focus();
  }
}

$('open-shelf').addEventListener('click', () => openShelf($('shelf').hidden));
$('shelf-close').addEventListener('click', () => openShelf(false));

// ---------------------------------------------------------------- her questions

function onAsk(msg) {
  const id = Number(msg.id);
  if ($('asks').querySelector(`[data-ask="${id}"]`)) return;
  const answer = (yes) => {
    host.send({ type: 'answer', id, yes });
    card.remove();
    box.focus();
  };
  const yes = text('button', 'send', 'Yes');
  const no = text('button', 'plain', 'No');
  yes.addEventListener('click', () => answer(true));
  no.addEventListener('click', () => answer(false));
  const card = h('article', 'ask rise', [text('p', null, String(msg.text || '')), h('div', 'buttons', [yes, no])]);
  card.dataset.ask = String(id);
  $('asks').append(card);
}

// ---------------------------------------------------------------- the box

function fit() {
  box.style.height = 'auto';
  box.style.height = `${Math.min(box.scrollHeight + 2, 150)}px`;
}

function submit() {
  const words = box.value.trim();
  if (!words) return;
  box.value = '';
  fit();
  host.send({ type: 'say', text: words });
  following = true;
  if (viewing) showToday();
}

function draft(words) {
  box.value = String(words);
  fit();
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}

box.addEventListener('input', () => {
  fit();
  sentAt = sent.length;
});
box.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  } else if ((event.key === 'ArrowUp' && box.selectionStart === 0) || (event.key === 'ArrowDown' && sentAt < sent.length && box.selectionEnd === box.value.length)) {
    if (event.key === 'ArrowUp' && sentAt === sent.length) unsent = box.value;
    const next = recall(sent, sentAt, event.key === 'ArrowUp' ? -1 : 1);
    if (next.at === sentAt) return;
    event.preventDefault();
    sentAt = next.at;
    box.value = next.text === null ? unsent : next.text;
    fit();
  }
});
$('send').addEventListener('click', submit);
$('stop').addEventListener('click', () => host.send({ type: 'stop' }));
$('say').addEventListener('change', () => host.send({ type: 'speak', on: $('say').checked }));

// ---------------------------------------------------------------- files dropped on the window

let dragging = 0;
window.addEventListener('dragenter', (event) => {
  event.preventDefault();
  dragging += 1;
  $('drop').hidden = false;
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('dragleave', () => {
  dragging = Math.max(0, dragging - 1);
  if (!dragging) $('drop').hidden = true;
});
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragging = 0;
  $('drop').hidden = true;
  const paths = host.pathsOf(event.dataTransfer.files);
  if (paths.length) host.send({ type: 'dropped', paths });
});

// ---------------------------------------------------------------- the window

function setStatus(words) {
  $('status').textContent = words;
  $('stop').hidden = !words;
}

function setMini(on) {
  document.body.classList.toggle('mini', on);
  $('make-mini').setAttribute('aria-pressed', String(on));
  $('make-mini').title = on ? 'Full window' : 'Small window';
  $('make-mini').textContent = on ? '' : '';
  if (!on && !viewing) setTimeout(() => toBottom(false));
}

$('make-mini').addEventListener('click', () => {
  const on = !document.body.classList.contains('mini');
  setMini(on);
  host.window('mini', on);
});
$('keep-top').addEventListener('click', () => {
  const on = $('keep-top').getAttribute('aria-pressed') !== 'true';
  $('keep-top').setAttribute('aria-pressed', String(on));
  host.window('top', on);
});
for (const button of document.querySelectorAll('[data-win]')) button.addEventListener('click', () => host.window(button.dataset.win));

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    if (!$('search').hidden) openSearch(false);
    else if (!$('shelf').hidden) openShelf(false);
    else host.window('close');
  } else if (event.ctrlKey && event.key.toLowerCase() === 'f') {
    event.preventDefault();
    openSearch(true);
  }
});

// ---------------------------------------------------------------- from her

function handle(msg) {
  switch (msg.type) {
    case 'line':
      return onLine(msg);
    case 'status':
      return setStatus(String(msg.text || ''));
    case 'ask':
      return onAsk(msg);
    case 'unask': {
      const card = $('asks').querySelector(`[data-ask="${Number(msg.id)}"]`);
      if (card) card.remove();
      return undefined;
    }
    case 'draft':
      return draft(msg.text);
    case 'focus-box':
      return box.focus();
    case 'settings':
      $('say').checked = msg.speak === true;
      return undefined;
    case 'day':
      return onDay(msg);
    case 'found':
      return onFound(msg);
    case 'panels':
      shelfRows = Array.isArray(msg.rows) ? msg.rows : [];
      if (msg.error) {
        $('shelf-note').textContent = String(msg.error);
        $('shelf-note').hidden = false;
      }
      return drawShelf();
    case 'window':
      if (typeof msg.mini === 'boolean') setMini(msg.mini);
      return undefined;
    default:
      return undefined;
  }
}

host.onMessage(handle);
host.hello().then((hello) => {
  name = hello.name || name;
  $('name').textContent = name;
  document.title = name;
  setMini(hello.mini === true);
  $('keep-top').setAttribute('aria-pressed', String(hello.top === true));
  box.placeholder = `Type to ${name}`;
  for (const msg of hello.pending || []) handle(msg);
  fit();
  box.focus();
  setTimeout(() => {
    settled = true;
    toBottom(false);
    showEarlier();
  }, SETTLE_MS);
});

// Previews and tests drive the page through this.
window.chatPage = {
  handle,
  // As it opened: one window serves a whole test file, as starting Electron for each test loads the whole suite.
  reset() {
    today.replaceChildren();
    past.replaceChildren();
    delete past.dataset.day;
    $('asks').replaceChildren();
    $('results').replaceChildren();
    $('search').hidden = true;
    $('shelf').hidden = true;
    box.value = '';
    lastToday = null;
    noEarlier = false;
    sent.length = 0;
    sentAt = 0;
    showToday();
  },
};
