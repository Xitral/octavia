// Her settings window's page. Everything about settings (what there is, checking and keeping a change, what waits
// for a restart) is the settings process's (octavia.platform.settings_window): it sends the whole map once (`model`),
// answers each change (`saved` / `refused`) and keeps the page told how she is (`status`). This page draws it, in her
// orb's look, and says what the user does.

import { applies, clock, hotkeyOf, marked, prettyHotkey, queryWords, step, timeLeft } from './model.js';
import { configure as configureSounds, play } from './sounds.js';

const host = window.settingsHost;
const $ = (id) => document.getElementById(id);

const state = {
  model: null,
  settings: new Map(), // key -> setting (as last sent)
  values: {}, // key -> value, for groups that apply only with another setting's value
  section: null,
  query: '',
  searchId: 0,
  found: null, // {keys, sections} for the query shown
  open: new Set(), // fine-tuning groups opened, "section/group"
  status: null,
  pending: new Map(), // id -> key of a change on its way
  nextId: 1,
  rows: new Map(), // key -> {row, control: {set}}
};

// ------------------------------------------------------------ talking to the settings process

function send(msg) {
  host.send(msg);
}

function change(key, raw) {
  const id = state.nextId++;
  state.pending.set(id, key);
  send({ type: 'set', id, key, raw });
}

function receive(msg) {
  if (msg.type === 'model') return loaded(msg);
  if (msg.type === 'saved') return saved(msg);
  if (msg.type === 'refused') return refused(msg);
  if (msg.type === 'status') return statusChanged(msg);
  if (msg.type === 'found') return foundResults(msg);
  if (msg.type === 'secret') return revealed(msg);
  if (msg.type === 'money_purged') return moneyPurged(msg);
  if (msg.type === 'forgotten') return say(msg.message, '');
  if (msg.type === 'window') document.body.classList.toggle('maximized', msg.maximized === true);
  return undefined;
}

// ------------------------------------------------------------ the map

function loaded(msg) {
  state.model = msg;
  state.settings = new Map(msg.settings.map((s) => [s.key, s]));
  state.values = Object.fromEntries(msg.settings.map((s) => [s.key, s.value]));
  configureSounds(state.values['audio.earcons'], msg.sounds && msg.sounds.volume);
  $('name').textContent = msg.name || 'Octavia';
  document.title = `${msg.name || 'Octavia'}: settings`;
  buildNav();
  const first = msg.start && msg.sections.some((s) => s.id === msg.start) ? msg.start : msg.sections[0].id;
  show(first, { quiet: true });
  if (msg.status !== undefined) statusChanged({ status: msg.status, usage: msg.usage });
  play('open');
}

function sectionOf(id) {
  return state.model.sections.find((s) => s.id === id);
}

function buildNav() {
  const nav = $('nav');
  nav.querySelectorAll('.area, .item').forEach((el) => el.remove());
  let area = null;
  for (const section of state.model.sections) {
    if (section.area !== area) {
      area = section.area;
      const heading = document.createElement('div');
      heading.className = 'area';
      heading.textContent = area;
      nav.append(heading);
    }
    const item = document.createElement('button');
    item.className = 'item';
    item.dataset.section = section.id;
    item.innerHTML = '<span class="glyph"></span><span class="title"></span>';
    item.querySelector('.glyph').textContent = section.glyph || '◇';
    item.querySelector('.title').textContent = section.title;
    item.addEventListener('click', () => {
      clearSearch();
      show(section.id);
    });
    nav.append(item);
  }
}

function paintNav() {
  const searching = Boolean(state.query);
  let selected = null;
  for (const item of $('nav').querySelectorAll('.item')) {
    const on = !searching && item.dataset.section === state.section;
    item.classList.toggle('on', on);
    if (on) selected = item;
    const waiting = waitingIn(item.dataset.section);
    let count = item.querySelector('.count');
    if (waiting && !count) {
      count = document.createElement('span');
      count.className = 'count';
      item.append(count);
    }
    if (count) {
      if (waiting) {
        count.textContent = String(waiting);
        count.title = `${waiting} waiting for a restart`;
      } else count.remove();
    }
  }
  const marker = $('marker');
  if (selected) {
    marker.style.transform = `translateY(${selected.offsetTop}px)`;
    marker.style.opacity = '1';
  } else marker.style.opacity = '0';
}

function waitingIn(sectionId) {
  const pending = (state.status && state.status.pending) || [];
  return pending.filter((key) => {
    const s = state.settings.get(key);
    return s && s.section === sectionId;
  }).length;
}

// ------------------------------------------------------------ what's shown

function show(sectionId, { quiet = false } = {}) {
  const section = sectionOf(sectionId);
  if (!section) return;
  const moved = state.section !== sectionId;
  state.section = sectionId;
  $('title').textContent = section.title;
  $('about').textContent = section.about;
  render(() => {
    const content = $('content');
    if (section.id === 'quiet') content.append(quietCard());
    if (section.id === 'usage') content.append(usageCard());
    for (const group of section.groups) content.append(groupBlock(section, group));
    if (section.id === 'privacy') content.append(forgetCard(), moneyCard());
  });
  paintNav();
  if (moved && !quiet) play('nav');
}

// Swap the content: a quick fade out, the new rows rising in one after another.
function render(fill) {
  const content = $('content');
  closeMenu();
  content.replaceChildren();
  state.rows.clear();
  fill();
  let i = 0;
  for (const el of content.querySelectorAll('.card, .group-title, .row')) {
    el.classList.add('rise');
    el.style.setProperty('--i', String(Math.min(i++, 14)));
  }
  content.scrollTop = 0;
}

function groupBlock(section, group, words = []) {
  const block = document.createElement('section');
  block.className = 'group';
  const id = `${section.id}/${group.title}`;
  const title = document.createElement('div');
  title.className = 'group-title';
  title.append(document.createTextNode(group.title));
  if (group.advanced) {
    block.classList.add('fold');
    if (state.open.has(id) || words.length) block.classList.add('open');
    const chev = document.createElement('span');
    chev.className = 'chev';
    chev.textContent = '';
    title.prepend(chev);
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = `${group.keys.length} more, for tuning`;
    title.append(note);
    title.addEventListener('click', () => {
      block.classList.toggle('open');
      if (block.classList.contains('open')) state.open.add(id);
      else state.open.delete(id);
      play('pick');
    });
  } else if (group.about) {
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = group.about;
    block.dataset.about = group.about;
    title.append(note);
  }
  block.append(title);
  const rows = document.createElement('div');
  rows.className = 'rows';
  for (const key of group.keys) {
    const setting = state.settings.get(key);
    if (setting) rows.append(row(setting, words));
  }
  block.append(rows);
  block.dataset.group = id;
  paintGroup(block, group);
  return block;
}

// A group that applies only with another setting's value is dimmed, with why, while it doesn't.
function paintGroup(block, group) {
  const asleep = !applies(group, state.values);
  block.classList.toggle('asleep', asleep);
  if (group.when && group.about) {
    const note = block.querySelector('.group-title .note');
    if (note) note.textContent = asleep ? group.about : '';
  }
}

function repaintGroups() {
  for (const block of $('content').querySelectorAll('.group')) {
    const [sectionId, ...rest] = block.dataset.group.split('/');
    const section = sectionOf(sectionId);
    const group = section && section.groups.find((g) => g.title === rest.join('/'));
    if (group) paintGroup(block, group);
  }
}

function fillMarked(el, text, words) {
  el.replaceChildren();
  for (const [part, hit] of marked(text, words)) {
    if (hit) {
      const m = document.createElement('mark');
      m.textContent = part;
      el.append(m);
    } else el.append(document.createTextNode(part));
  }
}

function row(setting, words = []) {
  const el = document.createElement('div');
  el.className = 'row';
  el.dataset.key = setting.key;
  const text = document.createElement('div');
  text.className = 'text';
  const label = document.createElement('div');
  label.className = 'label';
  const name = document.createElement('span');
  fillMarked(name, setting.label, words);
  label.append(name);
  text.append(label);
  if (setting.about) {
    const about = document.createElement('div');
    about.className = 'about';
    fillMarked(about, setting.about, words);
    text.append(about);
  }
  const control = document.createElement('div');
  control.className = 'control';
  el.append(text, control);
  const entry = { row: el, label, text, control, setting, set: () => {} };
  state.rows.set(setting.key, entry);
  build(entry);
  paintTags(entry);
  return el;
}

// The little marks beside a label: changed here (with Reset), waits for a restart.
function paintTags(entry) {
  const { label, setting } = entry;
  label.querySelectorAll('.tag, .reset, .changed-dot').forEach((el) => el.remove());
  const waiting = ((state.status && state.status.pending) || []).includes(setting.key);
  if (waiting) {
    label.append(tag('waiting for restart', 'waiting'));
  } else if (setting.restart) {
    const t = tag('after restart', 'restart');
    t.title = 'She reads this one when she starts';
    label.append(t);
  }
  if (setting.changed) {
    const dot = document.createElement('span');
    dot.className = 'changed-dot';
    dot.title = 'Changed here from what her config files say';
    const reset = document.createElement('button');
    reset.className = 'reset';
    reset.textContent = 'Reset';
    reset.title = 'Back to what her config files say';
    reset.addEventListener('click', () => {
      const id = state.nextId++;
      state.pending.set(id, setting.key);
      send({ type: 'reset', id, key: setting.key });
    });
    label.append(dot, reset);
  }
}

function tag(text, kind) {
  const t = document.createElement('span');
  t.className = `tag ${kind}`;
  t.textContent = text;
  return t;
}

// ------------------------------------------------------------ controls

function build(entry) {
  const kinds = {
    toggle: toggleControl,
    choice: choiceControl,
    device: selectControl,
    slider: sliderControl,
    hotkey: hotkeyControl,
    secret: secretControl,
  };
  (kinds[entry.setting.kind] || fieldControl)(entry);
}

function toggleControl(entry) {
  const { setting, control } = entry;
  const button = document.createElement('button');
  button.className = 'toggle';
  button.setAttribute('role', 'switch');
  const paint = (on) => {
    button.classList.toggle('on', on);
    button.setAttribute('aria-checked', String(on));
  };
  button.addEventListener('click', () => {
    const on = !button.classList.contains('on');
    paint(on);
    play(on ? 'on' : 'off');
    change(setting.key, on);
  });
  control.append(button);
  entry.set = (value) => paint(value === true);
  entry.set(setting.value);
}

function choiceControl(entry) {
  const { setting } = entry;
  const options = setting.options || [];
  const short = options.length <= 3 && options.reduce((n, o) => n + o.label.length, 0) <= 34;
  if (!short) return selectControl(entry);
  const strip = document.createElement('div');
  strip.className = 'segments';
  const buttons = options.map((option) => {
    const b = document.createElement('button');
    b.textContent = option.label;
    b.addEventListener('click', () => {
      if (b.classList.contains('on')) return;
      paint(option.value);
      play('pick');
      change(setting.key, option.value);
    });
    strip.append(b);
    return [option.value, b];
  });
  const paint = (value) => buttons.forEach(([v, b]) => b.classList.toggle('on', String(v) === String(value)));
  entry.control.append(strip);
  entry.set = paint;
  paint(setting.value);
}

// A drop-down list in her look (Windows' own list can't be styled).
let menu = null;

function closeMenu() {
  if (!menu) return;
  menu.el.remove();
  menu.owner.classList.remove('open');
  document.removeEventListener('mousedown', menu.away, true);
  menu = null;
}

function selectControl(entry) {
  const { setting } = entry;
  const wrap = document.createElement('div');
  wrap.className = 'select';
  const button = document.createElement('button');
  const shown = document.createElement('span');
  const chev = document.createElement('i');
  chev.textContent = '';
  button.append(shown, chev);
  wrap.append(button);
  let current = setting.value;
  const labelOf = (value) => {
    const o = (setting.options || []).find((opt) => String(opt.value) === String(value));
    return o ? o.label : String(value ?? '');
  };
  const paint = (value) => {
    current = value;
    shown.textContent = labelOf(value);
    button.title = shown.textContent;
  };
  const pick = (value) => {
    closeMenu();
    if (String(value) === String(current)) return;
    paint(value);
    play('pick');
    change(setting.key, value);
  };
  const open = () => {
    if (menu && menu.owner === wrap) return closeMenu();
    closeMenu();
    const list = document.createElement('div');
    list.className = 'menu';
    list.setAttribute('role', 'listbox');
    const items = (setting.options || []).map((option) => {
      const b = document.createElement('button');
      b.textContent = option.label;
      b.classList.toggle('chosen', String(option.value) === String(current));
      b.addEventListener('click', () => pick(option.value));
      list.append(b);
      return b;
    });
    document.body.append(list);
    const r = button.getBoundingClientRect();
    list.style.minWidth = `${r.width}px`;
    const below = window.innerHeight - r.bottom - 12;
    list.style.left = `${Math.min(r.left, window.innerWidth - list.offsetWidth - 12)}px`;
    if (below < Math.min(list.offsetHeight, 200) && r.top > below) {
      list.style.bottom = `${window.innerHeight - r.top + 4}px`;
    } else list.style.top = `${r.bottom + 4}px`;
    let active = Math.max(0, items.findIndex((b) => b.classList.contains('chosen')));
    const light = () => items.forEach((b, i) => b.classList.toggle('active', i === active));
    light();
    if (items[active]) items[active].scrollIntoView({ block: 'nearest' });
    const away = (e) => {
      if (!list.contains(e.target) && !wrap.contains(e.target)) closeMenu();
    };
    document.addEventListener('mousedown', away, true);
    wrap.classList.add('open');
    menu = {
      el: list,
      owner: wrap,
      away,
      key: (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          active = step(active, e.key === 'ArrowDown' ? 1 : -1, items.length);
          light();
          items[active].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter') items[active] && items[active].click();
        else if (e.key === 'Escape') closeMenu();
        else return false;
        return true;
      },
    };
    play('open_menu');
    return undefined;
  };
  button.addEventListener('click', open);
  entry.control.append(wrap);
  entry.set = paint;
  paint(setting.value);
}

function sliderControl(entry) {
  const { setting } = entry;
  const wrap = document.createElement('div');
  wrap.className = 'slider';
  const range = document.createElement('input');
  range.type = 'range';
  range.min = '0';
  range.max = String(setting.max ?? 100);
  range.step = '1';
  const out = document.createElement('output');
  wrap.append(range, out);
  let kept = Number(setting.value);
  const paint = (v) => {
    range.value = String(v);
    out.textContent = `${Math.round(v)}%`;
    range.style.setProperty('--fill', `${(100 * v) / Number(range.max)}%`);
  };
  range.addEventListener('input', () => paint(Number(range.value)));
  range.addEventListener('change', () => {
    const v = Number(range.value);
    if (v === kept) return;
    kept = v;
    play('pick');
    change(setting.key, v);
  });
  entry.control.append(wrap);
  entry.set = (v) => {
    kept = Number(v);
    paint(kept);
  };
  entry.set(setting.value);
}

function fieldControl(entry) {
  const { setting } = entry;
  const input = document.createElement('input');
  input.className = `field ${setting.width || 'mid'}`;
  input.spellcheck = false;
  if (setting.placeholder) input.placeholder = setting.placeholder;
  let kept = String(setting.value ?? '');
  input.value = kept;
  const commit = () => {
    if (input.value.trim() === kept.trim()) return;
    kept = input.value;
    change(setting.key, input.value);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      commit();
      input.blur();
    } else if (e.key === 'Escape') {
      input.value = kept;
      input.blur();
      e.stopPropagation();
    } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && setting.kind === 'number') {
      const n = Number(input.value.replace(',', '.'));
      if (!Number.isFinite(n)) return;
      const by = (setting.step || 1) * (e.key === 'ArrowUp' ? 1 : -1) * (e.shiftKey ? 10 : 1);
      let next = Math.round((n + by) * 1e6) / 1e6;
      if (setting.min != null) next = Math.max(setting.min, next);
      if (setting.max != null) next = Math.min(setting.max, next);
      input.value = String(next);
      e.preventDefault();
    }
  });
  input.addEventListener('blur', commit);
  entry.control.append(input);
  if (setting.unit) {
    const unit = document.createElement('span');
    unit.className = 'unit';
    unit.textContent = setting.unit;
    entry.control.append(unit);
  }
  entry.flush = commit;
  entry.set = (value) => {
    kept = String(value ?? '');
    if (document.activeElement !== input) input.value = kept;
    else input.value = kept;
  };
}

function hotkeyControl(entry) {
  const { setting } = entry;
  const button = document.createElement('button');
  button.className = 'hotkey';
  let kept = setting.value || '';
  let listening = false;
  const paint = () => {
    button.classList.toggle('listening', listening);
    button.classList.toggle('off', !listening && !kept);
    button.textContent = listening ? 'Press the keys…' : kept ? prettyHotkey(kept) : 'Off';
  };
  button.addEventListener('click', () => {
    listening = true;
    paint();
    play('open_menu');
  });
  button.addEventListener('blur', () => {
    listening = false;
    paint();
  });
  button.addEventListener('keydown', (e) => {
    if (!listening) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.code === 'Escape' && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey) {
      listening = false;
      paint();
      return;
    }
    const combo = hotkeyOf(e);
    if (combo === null) return; // a modifier on its own: keep listening
    listening = false;
    kept = combo;
    paint();
    play('pick');
    change(setting.key, combo);
  });
  entry.control.append(button);
  entry.set = (v) => {
    kept = v || '';
    paint();
  };
  paint();
}

function secretControl(entry) {
  const { setting } = entry;
  const input = document.createElement('input');
  input.className = 'field wide';
  input.type = 'password';
  input.spellcheck = false;
  input.autocomplete = 'off';
  const eye = document.createElement('button');
  eye.className = 'eye';
  eye.title = 'Show';
  eye.textContent = '';
  const dot = document.createElement('span');
  dot.className = 'dot';
  let isSet = setting.set === true;
  let revealed = false;
  const paint = () => {
    dot.classList.toggle('set', isSet);
    dot.textContent = isSet ? '● set' : '○ not set';
    input.placeholder = isSet ? '••••••••  (kept)' : 'Paste it here';
  };
  let sent = null; // what was last sent, so Enter and the blur after it send once
  const commit = () => {
    if (!input.value && !revealed) return; // nothing typed: the kept one stays
    if (revealed && input.value === entry.revealedValue) return;
    if (input.value === sent) return;
    sent = input.value;
    change(setting.key, input.value);
  };
  eye.addEventListener('click', () => {
    if (revealed) {
      revealed = false;
      input.type = 'password';
      if (input.value === entry.revealedValue) input.value = '';
      eye.classList.remove('shown');
      eye.textContent = '';
      eye.title = 'Show';
      return;
    }
    send({ type: 'reveal', key: setting.key });
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      commit();
      input.blur();
    } else if (e.key === 'Escape') {
      // Taken back: nothing half-typed is kept.
      input.value = revealed ? entry.revealedValue : '';
      e.stopPropagation();
      input.blur();
    }
  });
  input.addEventListener('blur', commit);
  input.addEventListener('input', () => {
    sent = null;
  });
  entry.control.append(input, eye, dot);
  entry.reveal = (value) => {
    revealed = true;
    entry.revealedValue = value;
    input.type = 'text';
    input.value = value;
    eye.classList.add('shown');
    eye.textContent = '';
    eye.title = 'Hide';
  };
  entry.flush = commit;
  entry.set = (value, s) => {
    isSet = Boolean(s && s.set);
    if (revealed) entry.revealedValue = input.value;
    else input.value = '';
    paint();
  };
  paint();
}

function revealed(msg) {
  const entry = state.rows.get(msg.key);
  if (entry && entry.reveal) entry.reveal(String(msg.value || ''));
}

// ------------------------------------------------------------ answers

function saved(msg) {
  const key = state.pending.get(msg.id) || msg.key;
  state.pending.delete(msg.id);
  const setting = msg.setting;
  state.settings.set(setting.key, setting);
  state.values[setting.key] = setting.value;
  const entry = state.rows.get(setting.key);
  if (entry) {
    entry.setting = setting;
    entry.set(setting.value, setting);
    entry.row.classList.remove('refused', 'kept');
    entry.row.querySelectorAll('.error').forEach((el) => el.remove());
    void entry.row.offsetWidth; // start the light again for a second change
    entry.row.classList.add('kept');
    paintTags(entry);
  }
  if (key === 'audio.earcons' || key === 'audio.earcon_volume') {
    configureSounds(state.values['audio.earcons'], state.values['audio.earcon_volume'] / 100);
  }
  say(msg.message, msg.tone);
  repaintGroups();
  if (msg.tone !== 'idle') play('saved');
}

function refused(msg) {
  state.pending.delete(msg.id);
  if (!msg.key && state.restarting) {
    state.restarting = null; // asking her failed
    paintBanner();
  }
  const entry = state.rows.get(msg.key);
  if (entry) {
    if (msg.setting) {
      entry.setting = msg.setting;
      entry.set(msg.setting.value, msg.setting);
    }
    entry.row.classList.add('refused');
    entry.row.querySelectorAll('.error').forEach((el) => el.remove());
    const error = document.createElement('div');
    error.className = 'error';
    error.textContent = msg.error;
    entry.text.append(error);
  }
  say(msg.error, 'bad');
  play('refused');
}

function say(text, tone) {
  const message = $('message');
  message.textContent = text || '';
  message.className = `message ${tone || ''}`;
}

// ------------------------------------------------------------ her side: restart, snooze, usage

function statusChanged(msg) {
  state.status = msg.status || null;
  if (state.restarting) {
    // She's back (another run), or she didn't restart (a build she wouldn't cut off): the banner says what's true.
    const back = state.status && state.status.pid !== state.restarting.pid;
    if (back || Date.now() - state.restarting.at > 45000) state.restarting = null;
  }
  if (msg.usage !== undefined) state.usage = msg.usage;
  paintBanner();
  paintNav();
  for (const entry of state.rows.values()) paintTags(entry);
  if (state.section === 'quiet' && !state.query) refreshCard('quiet', quietCard);
  if (state.section === 'usage' && !state.query && msg.usage !== undefined) refreshCard('usage', usageCard);
}

function refreshCard(name, make) {
  const old = $('content').querySelector(`.card[data-card="${name}"]`);
  if (old) old.replaceWith(make());
}

function nameOf(key) {
  const s = state.settings.get(key);
  return s ? s.label : key;
}

function paintBanner() {
  const banner = $('banner');
  const name = (state.model && state.model.name) || 'She';
  const status = state.status;
  banner.replaceChildren();
  banner.className = 'banner';
  const text = document.createElement('span');
  banner.append(text);
  if (state.restarting) {
    text.textContent = `Restarting ${name}…`;
  } else if (!status) {
    banner.classList.add('idle');
    text.textContent = `${name} isn't running. Your changes are kept, and she uses them when she starts.`;
  } else if (status.error) {
    banner.classList.add('bad');
    text.textContent = `Her settings files don't load, so she kept what she had: ${status.error}`;
  } else if ((status.pending || []).length) {
    const waiting = status.pending.map(nameOf);
    const shown = waiting.slice(0, 3).join(', ') + (waiting.length > 3 ? ` and ${waiting.length - 3} more` : '');
    text.textContent = `${waiting.length} change${waiting.length === 1 ? '' : 's'} ${waiting.length === 1 ? 'waits' : 'wait'} for a restart: ${shown}.`;
    if (status.can_restart) {
      const b = document.createElement('button');
      b.className = 'button wait';
      b.textContent = 'Restart now';
      b.addEventListener('click', () => {
        state.restarting = { pid: status.pid, at: Date.now() };
        send({ type: 'restart' });
        paintBanner();
      });
      banner.append(b);
    } else {
      const hint = document.createElement('span');
      hint.style.flex = 'none';
      hint.textContent = `Quit ${name} and start her again.`;
      banner.append(hint);
    }
  } else if ((status.dropped || []).length) {
    banner.classList.add('bad');
    text.textContent = `Some choices here don't fit her config and were left out: ${status.dropped.map(nameOf).join(', ')}.`;
  } else {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
}

function quietCard() {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.card = 'quiet';
  const name = (state.model && state.model.name) || 'She';
  const running = Boolean(state.status);
  const quiet = state.status && state.status.quiet;
  const live = quiet && Number(quiet.until) > Date.now() / 1000;
  card.innerHTML = '<h2>Right now</h2><div class="state"><i></i><span></span></div><p></p><div class="buttons"></div>';
  const line = card.querySelector('.state');
  line.classList.toggle('on', Boolean(live));
  line.querySelector('span').textContent = !running
    ? `${name} isn't running.`
    : live
      ? `${quiet.kind === 'focus' ? 'Focus mode' : 'Snoozed'} until ${clock(quiet.until)} (${timeLeft(quiet.until)} left).`
      : 'Not snoozed.';
  card.querySelector('p').textContent =
    'Snooze holds her alerts and her own remarks; you can still talk to her. Focus also turns on Windows’ Do Not Disturb and pauses your music.';
  const buttons = card.querySelector('.buttons');
  const add = (label, action, minutes, quietStyle = false) => {
    const b = document.createElement('button');
    b.className = `button${quietStyle ? ' quiet' : ''}`;
    b.textContent = label;
    b.disabled = !running;
    b.addEventListener('click', () => {
      send({ type: 'action', action, minutes });
      play(action === 'wake' ? 'off' : 'on');
      say(action === 'wake' ? 'Asked her to end it.' : `Asked her to ${action === 'focus' ? 'focus' : 'snooze'}.`, '');
    });
    buttons.append(b);
  };
  add('Snooze 30 min', 'snooze', 30);
  add('Snooze 1 hour', 'snooze', 60);
  add('Snooze 2 hours', 'snooze', 120);
  add('Focus 1 hour', 'focus', 60);
  if (live) add('End it now', 'wake', 0, true);
  return card;
}

// Forget the last stretch of time: everything she kept from then, in every store. Two clicks, the second within a
// few seconds, are the yes. A copy of what was there goes to the octavia-backups folder first.
const FORGET_CHOICES = [
  [15, 'Last 15 minutes'],
  [30, 'Last 30 minutes'],
  [60, 'Last hour'],
  [1440, 'Last day'],
];

function forgetCard() {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.card = 'forget';
  card.innerHTML = '<h2>Forget the last...</h2><p></p><div class="buttons"></div>';
  card.querySelector('p').textContent =
    'Everything she kept from that time goes: what was said, her notes, screen history, logs and her own records of it. ' +
    'She restarts into a new conversation. A copy of what was there goes to the octavia-backups folder in your user folder first.';
  const buttons = card.querySelector('.buttons');
  let armed = null; // [button, timer]: one choice armed at a time
  const disarm = () => {
    if (!armed) return;
    clearTimeout(armed[1]);
    armed[0].textContent = armed[0].dataset.label;
    armed = null;
  };
  for (const [minutes, label] of FORGET_CHOICES) {
    const b = document.createElement('button');
    b.className = 'button quiet';
    b.textContent = label;
    b.dataset.label = label;
    b.addEventListener('click', () => {
      if (!armed || armed[0] !== b) {
        disarm();
        b.textContent = 'Click again to forget';
        armed = [b, setTimeout(disarm, 5000)];
        return;
      }
      disarm();
      send({ type: 'action', action: 'forget', minutes });
      play('off');
    });
    buttons.append(b);
  }
  return card;
}

// Money: delete all of it. Two clicks, the second within a few seconds: nothing of it can be brought back.
function moneyCard() {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.card = 'money';
  card.innerHTML = '<h2>Your money data</h2><p></p><div class="buttons"></div>';
  card.querySelector('p').textContent =
    'Transactions, balances, your credit score and loans she read from your statements and bank pages, kept on this PC only. ' +
    'Deleting signs her bank browser out too. Statement files in a folder of your own are left alone.';
  const b = document.createElement('button');
  b.className = 'button quiet';
  b.textContent = 'Delete all money data';
  let armed = null;
  b.addEventListener('click', () => {
    if (!armed) {
      b.textContent = 'Click again to delete it all';
      armed = setTimeout(() => {
        armed = null;
        b.textContent = 'Delete all money data';
      }, 5000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    b.textContent = 'Delete all money data';
    send({ type: 'action', action: 'finance_purge' });
    play('off');
  });
  card.querySelector('.buttons').append(b);
  return card;
}

function moneyPurged(msg) {
  say(msg.running ? 'Asked her to delete all money data.' : 'Deleted all money data.', '');
}

function usageCard() {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.card = 'usage';
  const usage = state.usage;
  card.innerHTML = '<h2>Where your limits stand</h2>';
  if (!usage) {
    const p = document.createElement('p');
    p.textContent = 'No reading yet.';
    card.append(p);
    return card;
  }
  const meter = (title, used, limit, note) => {
    const m = document.createElement('div');
    m.className = 'meter';
    m.innerHTML = '<div class="top"><span></span><b></b></div><div class="bar"><div class="fill"></div></div>';
    m.querySelector('.top span').textContent = title;
    m.querySelector('.top b').textContent = note;
    m.querySelector('.fill').style.width = `${Math.min(100, Math.max(0, used * 100))}%`;
    if (Number.isFinite(limit)) {
      const mark = document.createElement('i');
      mark.className = 'limit';
      mark.style.left = `${Math.min(100, limit * 100)}%`;
      mark.title = 'Her background work pauses here';
      m.querySelector('.bar').append(mark);
    }
    card.append(m);
  };
  for (const w of usage.meters || []) meter(w.title, w.used, w.limit, w.note);
  const p = document.createElement('p');
  p.textContent = usage.text || '';
  card.append(p);
  return card;
}

// ------------------------------------------------------------ search

let searchTimer = null;

function searched() {
  const query = $('search').value.trim();
  state.query = query;
  clearTimeout(searchTimer);
  if (!query) {
    state.found = null;
    show(state.section, { quiet: true });
    return;
  }
  searchTimer = setTimeout(() => {
    state.searchId += 1;
    send({ type: 'search', id: state.searchId, query });
  }, 70);
}

function foundResults(msg) {
  if (msg.id !== state.searchId || !state.query) return;
  state.found = msg;
  const words = queryWords(state.query);
  $('title').textContent = 'Search';
  const n = msg.keys.length;
  $('about').textContent = n
    ? `${n} setting${n === 1 ? '' : 's'} for “${state.query}”, best first.`
    : `Nothing matches “${state.query}”. Try another word.`;
  render(() => {
    const content = $('content');
    if (msg.sections && msg.sections.length) {
      const hit = document.createElement('div');
      hit.className = 'section-hit';
      hit.textContent = 'Go to';
      for (const id of msg.sections) {
        const section = sectionOf(id);
        if (!section) continue;
        const b = document.createElement('button');
        b.textContent = section.title;
        b.addEventListener('click', () => {
          clearSearch();
          show(id);
        });
        hit.append(b);
      }
      content.append(hit);
    }
    if (!n && !(msg.sections || []).length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Try what it does ("pauses", "louder", "night") or where it is ("voice", "look").';
      content.append(empty);
      return;
    }
    const block = document.createElement('section');
    block.className = 'group';
    const rows = document.createElement('div');
    rows.className = 'rows';
    for (const key of msg.keys) {
      const setting = state.settings.get(key);
      if (!setting) continue;
      const r = row(setting, words);
      const where = document.createElement('div');
      where.className = 'where';
      const section = sectionOf(setting.section);
      where.textContent = `${section ? section.title : ''} › ${setting.group}`;
      r.querySelector('.text').prepend(where);
      rows.append(r);
    }
    block.append(rows);
    content.append(block);
  });
  paintNav();
}

function clearSearch() {
  if (!$('search').value) return;
  $('search').value = '';
  state.query = '';
  state.found = null;
}

// ------------------------------------------------------------ keys and start

function flushAll() {
  for (const entry of state.rows.values()) if (entry.flush) entry.flush();
}

document.addEventListener('keydown', (e) => {
  if (menu && menu.key(e)) {
    e.preventDefault();
    return;
  }
  if ((e.ctrlKey && e.key.toLowerCase() === 'f') || (e.key === '/' && document.activeElement.tagName !== 'INPUT')) {
    e.preventDefault();
    $('search').focus();
    $('search').select();
    return;
  }
  if (e.key === 'Escape') {
    if (document.activeElement === $('search') && $('search').value) {
      $('search').value = '';
      searched();
      return;
    }
    if (document.activeElement && document.activeElement.tagName === 'INPUT') {
      document.activeElement.blur();
      return;
    }
    flushAll();
    host.window('close');
  }
  if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp') && state.model) {
    const ids = state.model.sections.map((s) => s.id);
    clearSearch();
    show(ids[step(ids.indexOf(state.section), e.key === 'ArrowDown' ? 1 : -1, ids.length)]);
    e.preventDefault();
  }
});

$('search').addEventListener('input', searched);
for (const b of document.querySelectorAll('.win')) {
  b.addEventListener('click', () => {
    if (b.dataset.win === 'close') flushAll();
    host.window(b.dataset.win);
  });
}
window.addEventListener('resize', () => {
  closeMenu();
  paintNav();
});
$('content').addEventListener('scroll', closeMenu);
window.addEventListener('beforeunload', flushAll);

// Her emblem flares now and then: one short twinkle every 7 to 13 s, nothing in between.
function twinkle() {
  const emblem = document.querySelector('.emblem');
  emblem.classList.remove('twinkle');
  void emblem.offsetWidth;
  emblem.classList.add('twinkle');
  setTimeout(twinkle, 7000 + Math.random() * 6000);
}
setTimeout(twinkle, 2500);

// The snooze's time left counts down while Quiet hours is open, and a banner about a restart is looked at again.
setInterval(() => {
  if (state.section === 'quiet' && !state.query) refreshCard('quiet', quietCard);
  if (state.restarting && Date.now() - state.restarting.at > 45000) {
    state.restarting = null;
    paintBanner();
  }
}, 30000);

host.onMessage(receive);
host.hello().then(({ pending }) => {
  for (const msg of pending || []) receive(msg);
});

// For the preview tool and tests (hidden windows only care): what's on screen, by key.
window.settingsPage = {
  show: (id) => {
    clearSearch();
    show(id, { quiet: true });
  },
  search: (q) => {
    $('search').value = q;
    searched();
  },
  open: (sectionId, title) => {
    state.open.add(`${sectionId}/${title}`);
  },
  rows: () => [...state.rows.keys()],
};
