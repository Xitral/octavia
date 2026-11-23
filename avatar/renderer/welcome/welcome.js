// The guided setup: seven small choices, each waking OCTAVIA's orb a little more (its colour, brightness and mood),
// the voices heard through it, and a flare when it comes to life. The setup process (octavia.platform.welcome_window)
// does the real work: devices, previews, finding the town, saving. Nothing is saved until "Bring OCTAVIA to life".

import { GUIDES, picture } from './guides.js';

const host = window.welcomeHost;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const steps = $$('.step');
const orbFrame = $('#orb');
const state = { at: 0, model: null, voice: null, keyOk: false, home: null, playing: null, previews: {} };

// ---------------------------------------------------------------- the orb
let orbReady = false;
const orbQueue = [];
function orb(msg) {
  if (!orbReady) return orbQueue.push(msg);
  orbFrame.contentWindow.postMessage(msg, window.location.origin);
}
window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin || event.data?.type !== 'orb-ready') return;
  orbReady = true;
  orbQueue.splice(0).forEach(orb);
});

// How formed it looks: grey and faint at first, its full blue by the end.
function form(progress) {
  const p = Math.max(0, Math.min(1, progress));
  orbFrame.style.filter = `saturate(${0.08 + 0.92 * p}) brightness(${0.35 + 0.65 * p}) blur(${(1 - p) * 2.5}px)`;
  orbFrame.style.opacity = String(0.45 + 0.55 * p);
}

function whisper(text) {
  const el = $('#whisper');
  el.classList.remove('on');
  if (!text) return;
  setTimeout(() => {
    el.textContent = text;
    el.classList.add('on');
  }, 250);
}

const WHISPERS = {
  intro: '',
  mind: 'a mind...',
  hearing: 'ears...',
  voice: 'a voice...',
  identity: 'a name...',
  home: 'a place...',
  reach: 'reach...',
  ready: 'almost...',
  alive: '',
  waking: '',
  guide: '',
};

// ---------------------------------------------------------------- steps
const dots = $('#dots');
const CHOICES = steps.filter((step) => !['intro', 'alive', 'waking', 'guide'].includes(step.dataset.step)).length;
for (let i = 0; i < CHOICES; i += 1) dots.appendChild(document.createElement('li'));
const indexOf = (name) => steps.findIndex((step) => step.dataset.step === name);

function show(index) {
  state.at = Math.max(0, Math.min(steps.length - 1, index));
  steps.forEach((step, i) => {
    step.hidden = i !== state.at;
  });
  const step = steps[state.at];
  [...dots.children].forEach((dot, i) => dot.classList.toggle('on', i < state.at));
  form(Math.min(1, state.at / CHOICES));
  orb({ type: 'state', state: step.dataset.mood || 'idle' });
  if (state.at > 0 && step.dataset.step !== 'guide') orb({ type: 'moment', name: 'step' });
  whisper(WHISPERS[step.dataset.step]);
  step.querySelector('input:not([type=radio]):not([type=checkbox]), select')?.focus({ preventScroll: true });
}

document.addEventListener('click', (event) => {
  const t = event.target.closest('button');
  if (!t) return;
  if (t.matches('.guide-me')) {
    event.preventDefault();
    return startGuide(t.dataset.guide);
  }
  if (t.matches('[data-next]')) show(state.at + 1);
  else if (t.matches('[data-back]')) show(state.at - 1);
  else if (t.matches('[data-win]')) host.window(t.dataset.win);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.target.matches('textarea, button, summary')) {
    steps[state.at].querySelector('[data-next]')?.click();
  }
});

// The API key field only when the API key is picked.
document.addEventListener('change', (event) => {
  if (event.target.name === 'brain') {
    $$('[data-when]').forEach((el) => {
      const [name, value] = el.dataset.when.split('=');
      el.hidden = document.querySelector(`input[name=${name}]:checked`)?.value !== value;
    });
  }
});

// ---------------------------------------------------------------- dropdowns
// Each <select> stays as the value holder, hidden; a button and a list in the app's look stand in for it.
function dress(select) {
  if (select.dataset.dressed) return refresh(select);
  select.dataset.dressed = '1';
  select.classList.add('native');
  const box = document.createElement('div');
  box.className = 'dropdown';
  box.innerHTML = '<button type="button" class="pick"><span></span><i aria-hidden="true"></i></button><ul class="list" role="listbox" hidden></ul>';
  select.after(box);
  const button = box.querySelector('.pick');
  const list = box.querySelector('.list');
  const close = () => {
    list.hidden = true;
    box.classList.remove('open');
  };
  button.addEventListener('click', (event) => {
    event.preventDefault();
    const opening = list.hidden;
    $$('.dropdown .list').forEach((l) => (l.hidden = true));
    $$('.dropdown').forEach((d) => d.classList.remove('open'));
    if (!opening) return;
    list.hidden = false;
    box.classList.add('open');
    list.querySelector('[aria-selected=true]')?.scrollIntoView({ block: 'nearest' });
  });
  list.addEventListener('click', (event) => {
    const li = event.target.closest('li');
    if (!li) return;
    select.value = li.dataset.value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    refresh(select);
    close();
  });
  document.addEventListener('click', (event) => {
    if (!box.contains(event.target)) close();
  });
  select.addEventListener('change', () => refresh(select));
  refresh(select);
}

function refresh(select) {
  const box = select.nextElementSibling;
  if (!box?.classList.contains('dropdown')) return;
  box.querySelector('.pick span').textContent = select.selectedOptions[0]?.textContent || '';
  const list = box.querySelector('.list');
  list.innerHTML = '';
  for (const option of select.options) {
    const li = document.createElement('li');
    li.textContent = option.textContent;
    li.dataset.value = option.value;
    li.setAttribute('aria-selected', String(option.selected));
    list.appendChild(li);
  }
}

$$('select').forEach(dress);

// ---------------------------------------------------------------- the model
function fillSelect(select, names, chosen) {
  select.innerHTML = '';
  const def = new Option("Windows' default", '');
  select.add(def);
  for (const name of names) select.add(new Option(name, name, false, name === chosen));
}

function voiceCards() {
  const box = $('#voices');
  box.innerHTML = '';
  for (const preset of state.model.presets) {
    const card = document.createElement('div');
    card.className = 'voice';
    card.dataset.id = preset.voice_id;
    card.innerHTML = '<span class="who"><b></b><small></small></span><button>Hear it</button>';
    card.querySelector('b').textContent = preset.name;
    card.querySelector('small').textContent = preset.about;
    card.addEventListener('click', (event) => {
      chooseVoice(preset.voice_id);
      if (event.target.closest('button')) preview(preset.voice_id);
    });
    box.appendChild(card);
  }
  chooseVoice(state.model.presets[0].voice_id);
}

function chooseVoice(id) {
  state.voice = id;
  $$('.voice').forEach((card) => card.classList.toggle('chosen', card.dataset.id === id));
}

function hardware(hw) {
  const list = $('#hardware');
  const rows = [
    ['System', hw.system || '?'],
    ['Processor', `${hw.cpus || '?'} threads`],
    ['Memory', `${hw.ram_gb || '?'} GB`],
    ['Graphics', (hw.gpus || []).join(', ') || 'none found'],
    ['Space here', `${hw.disk_free_gb ?? '?'} GB free`],
  ];
  if (hw.note) rows.push(['Note', hw.note]);
  list.innerHTML = '';
  for (const [k, v] of rows) {
    const li = document.createElement('li');
    li.innerHTML = '<b></b><span></span>';
    li.querySelector('b').textContent = k;
    li.querySelector('span').textContent = v;
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------- voices
let audioCtx = null;
function speakThrough(audio) {
  // Its orb moves with the voice: the clip's loudness, as the assistant sends it while it talks.
  audioCtx = audioCtx || new AudioContext();
  const source = audioCtx.createMediaElementSource(audio);
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  analyser.connect(audioCtx.destination);
  const data = new Uint8Array(analyser.fftSize);
  const tick = () => {
    if (audio.paused || audio.ended) {
      orb({ type: 'mouth', level: 0 });
      return;
    }
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += ((v - 128) / 128) ** 2;
    orb({ type: 'mouth', level: Math.min(1, Math.sqrt(sum / data.length) * 4) });
    requestAnimationFrame(tick);
  };
  orb({ type: 'state', state: 'speaking' });
  requestAnimationFrame(tick);
}

function play(id, base64) {
  state.playing?.pause();
  const audio = new Audio(`data:audio/wav;base64,${base64}`);
  state.playing = audio;
  $$('.voice').forEach((card) => card.classList.toggle('playing', card.dataset.id === id));
  audio.addEventListener('ended', () => $$('.voice').forEach((card) => card.classList.remove('playing')));
  speakThrough(audio);
  audio.play().catch(() => {});
}

// A preview is kept per voice and effect: changing the effect is heard on the next play.
const previewKey = (id) => `${id}|${$('#effect').value}`;
function preview(id) {
  if (state.previews[previewKey(id)]) return play(id, state.previews[previewKey(id)]);
  if (!state.keyOk) {
    $('#key-note').textContent = 'Paste a Fish Audio key above to hear the voices.';
    return;
  }
  $('#key-note').textContent = 'Making the preview with your key (a few seconds)...';
  orb({ type: 'state', state: 'thinking' });
  host.send({ type: 'preview', voice_id: id, key: $('#fish').value.trim(), effect: $('#effect').value });
}

$('#check-key').addEventListener('click', () => {
  const key = $('#fish').value.trim();
  if (!key) return;
  $('#key-note').textContent = 'Checking the key...';
  host.send({ type: 'check-key', key, effect: $('#effect').value });
});

// A new effect: hear it at once on the voice that's picked.
$('#effect').addEventListener('change', () => {
  if (state.keyOk && state.voice) preview(state.voice);
});

$('#find').addEventListener('click', () => {
  const town = $('#town').value.trim();
  if (!town) return;
  $('#found').textContent = 'Looking...';
  host.send({ type: 'geocode', place: town });
});

$('#other-model').addEventListener('change', () => {
  const m = state.model?.models?.[Number($('#other-model').value) || 0];
  if (!m) return;
  $('#other-url-field').hidden = Boolean(m.base_url);
  $('#other-key-field').hidden = !m.key_env;
  $('#other-name').value = m.model || '';
});

$('#name').addEventListener('input', () => {
  $('#wake').value = $('#name').value.trim().toLowerCase();
});

// ---------------------------------------------------------------- awakening
function choices() {
  const val = (sel) => $(sel).value.trim();
  const config = {};
  const env = {};
  const set = (dotted, value) => {
    const parts = dotted.split('.');
    let node = config;
    for (const p of parts.slice(0, -1)) node = node[p] = node[p] || {};
    node[parts.at(-1)] = value;
  };
  const brain = document.querySelector('input[name=brain]:checked').value;
  if (brain === 'other') {
    const m = state.model.models[Number($('#other-model').value) || 0];
    set('brain.provider', 'openai_compatible');
    set('brain.openai_compatible.base_url', m.base_url || val('#other-url'));
    set('brain.openai_compatible.api_key_env', m.key_env);
    if (val('#other-name')) set('brain.openai_compatible.model', val('#other-name'));
    if (m.key_env && val('#other-key')) env[m.key_env] = val('#other-key');
  } else {
    set('brain.provider', 'claude');
    set('brain.claude.auth', brain);
  }
  if (brain === 'api_key' && val('#anthropic')) env.ANTHROPIC_API_KEY = val('#anthropic');
  if (val('#mic')) set('audio.input_device', val('#mic'));
  if (val('#speaker')) set('audio.output_device', val('#speaker'));
  set('audio.headphones', $('#headphones').checked);
  set('tts.provider', 'fish_audio');
  set('tts.fallback', 'windows');
  set('tts.voice_effect', val('#effect'));
  if (state.voice) set('tts.fish_audio.reference_id', state.voice);
  if (val('#fish')) env.FISH_AUDIO_API_KEY = val('#fish');
  set('assistant.name', val('#name') || 'Octavia');
  set('assistant.wake_phrase', val('#wake') || (val('#name') || 'octavia').toLowerCase());
  set('assistant.user_name', val('#you'));
  if (val('#town')) {
    set('assistant.home', val('#town'));
    if (state.home && state.home.query === val('#town')) {
      set('assistant.home_latitude', state.home.latitude);
      set('assistant.home_longitude', state.home.longitude);
    }
  }
  if (val('#discord-token') && /^\d+$/.test(val('#discord-owner'))) {
    env.DISCORD_BOT_TOKEN = val('#discord-token');
    env.DISCORD_OWNER_ID = val('#discord-owner');
    set('discord_bridge.enabled', true);
  }
  if (val('#ticktick')) env.TICKTICK_API_TOKEN = val('#ticktick');
  if (val('#canvas-url')) env.CANVAS_BASE_URL = val('#canvas-url');
  if (val('#canvas-token')) env.CANVAS_API_TOKEN = val('#canvas-token');
  if (val('#gmail')) env.GMAIL_ADDRESS = val('#gmail');
  if (val('#gmail-password')) env.GMAIL_APP_PASSWORD = val('#gmail-password');
  if ($('#share').checked) set('hive.share', 'on');
  return { config, env };
}

$('#awaken').addEventListener('click', () => {
  $('#awaken').disabled = true;
  orb({ type: 'state', state: 'thinking' });
  whisper('waking...');
  host.send({ type: 'save', choices: choices() });
});

$('#sign-in').addEventListener('click', () => {
  $('#sign-in').disabled = true;
  $('#alive-text').textContent = 'A browser window is open: sign in there and pick your Claude subscription.';
  orb({ type: 'state', state: 'listening' });
  host.send({ type: 'sign-in' });
});

$('#finish').addEventListener('click', () => {
  // OCTAVIA itself starts now: the setup process launches it and says when it's up (or that it couldn't).
  show(indexOf('waking'));
  orb({ type: 'state', state: 'thinking' });
  wakingClock();
  host.send({ type: 'done' });
});

let wakingStarted = 0;
let wakingTimer = null;
function wakingClock() {
  wakingStarted = performance.now();
  const tick = () => {
    const s = (performance.now() - wakingStarted) / 1000;
    for (const li of $$('#waking-steps li')) {
      const at = Number(li.dataset.at);
      li.classList.toggle('done', s >= at + 6 && li.nextElementSibling && s >= Number(li.nextElementSibling.dataset.at));
      li.classList.toggle('now', s >= at && !li.classList.contains('done'));
    }
    if (s > 90) $('#waking-note').textContent = 'Still going. The first start can take a few minutes on some PCs.';
    orb({ type: 'mouth', level: 0.15 + 0.1 * Math.sin(s * 3) });
  };
  wakingTimer = setInterval(tick, 250);
  tick();
}

// ---------------------------------------------------------------- guided connections (guides.js)
// One step at a time: it says the step (in its voice when there's a key), opens the page, shows what to look for,
// and checks what's pasted at once. "I'm stuck" asks its model when one is reachable, else shows the step's tips.
const guide = { name: null, i: 0, from: 0, said: {}, sayId: 0 };

function startGuide(name) {
  if (!GUIDES[name]) return;
  Object.assign(guide, { name, i: 0, from: state.at });
  show(indexOf('guide'));
  guideStep();
}

function endGuide() {
  const from = guide.from;
  guide.name = null;
  show(from);
}

function speak(text) {
  const fish = $('#fish').value.trim();
  if (!state.keyOk || !fish || !state.voice) return;
  const cached = guide.said[`${state.voice}|${$('#effect').value}|${text}`];
  if (cached) return play(state.voice, cached);
  guide.sayId += 1;
  host.send({ type: 'say', id: guide.sayId, text, key: fish, voice_id: state.voice, effect: $('#effect').value });
  guide.pending = { id: guide.sayId, text };
}

function guideStep() {
  const g = GUIDES[guide.name];
  const step = g.steps[guide.i];
  $('#g-kicker').textContent = `${g.title} · ${guide.i + 1} / ${g.steps.length}`;
  $('#g-title').textContent = guide.i === 0 ? `Connecting ${g.title}` : g.title;
  $('#g-pic').innerHTML = step.picture ? picture(step.picture) : '';
  $('#g-say').textContent = step.say;
  $('#g-then').textContent = step.then || '';
  $('#g-help').hidden = true;
  $('#g-answer').textContent = '';
  $('#g-open').hidden = !step.open;
  $('#g-next').textContent = guide.i === g.steps.length - 1 ? 'Done' : 'Next';
  const box = $('#g-field');
  box.innerHTML = '';
  if (step.field) {
    const source = $(`#${step.field.id}`);
    const input = document.createElement('input');
    input.type = source?.type === 'password' ? 'password' : 'text';
    input.placeholder = step.field.label || '';
    input.value = source?.value || '';
    input.autocomplete = 'off';
    const note = document.createElement('p');
    note.className = 'note';
    box.append(input, note);
    let timer = null;
    const checkNow = () => {
      if (!input.value.trim()) return;
      note.textContent = 'Checking...';
      note.className = 'note dim';
      if (step.field.check === 'fish') {
        state.guideFish = note;
        host.send({ type: 'check-key', key: input.value.trim(), effect: $('#effect').value });
      } else if (step.field.check) {
        const extra = step.field.extra ? $(`#${step.field.extra}`)?.value || '' : '';
        state.guideNote = note;
        host.send({ type: 'check', what: step.field.check, value: input.value.trim(), extra });
      } else {
        note.textContent = '';
      }
    };
    input.addEventListener('input', () => {
      if (source) {
        source.value = input.value;
        source.dispatchEvent(new Event('input', { bubbles: true }));
      }
      clearTimeout(timer);
      timer = setTimeout(checkNow, 700);
    });
    setTimeout(() => input.focus(), 50);
  }
  if (step.open) host.send({ type: 'open', target: step.open, canvas_url: $('#canvas-url').value });
  speak(step.say);
}

$('#g-next').addEventListener('click', () => {
  const g = GUIDES[guide.name];
  if (guide.i < g.steps.length - 1) {
    guide.i += 1;
    guideStep();
  } else {
    orb({ type: 'emotion', emotion: 'happy', intensity: 0.7, hold: 2 });
    endGuide();
  }
});
$('#g-back').addEventListener('click', () => {
  if (guide.i > 0) {
    guide.i -= 1;
    guideStep();
  } else {
    endGuide();
  }
});
$('#g-open').addEventListener('click', () => {
  const step = GUIDES[guide.name].steps[guide.i];
  if (step.open) host.send({ type: 'open', target: step.open, canvas_url: $('#canvas-url').value });
});
$('#g-stuck').addEventListener('click', () => {
  $('#g-help').hidden = false;
  $('#g-question').focus();
});
$('#g-ask').addEventListener('click', () => {
  const g = GUIDES[guide.name];
  const step = g.steps[guide.i];
  const keys = {};
  if ($('#anthropic').value.trim()) keys.ANTHROPIC_API_KEY = $('#anthropic').value.trim();
  $('#g-answer').textContent = 'Thinking...';
  orb({ type: 'state', state: 'thinking' });
  host.send({ type: 'stuck', guide: g.title, step: step.say, question: $('#g-question').value.trim() || "I'm stuck", keys });
});

// ---------------------------------------------------------------- from the setup process
const handlers = {
  model(msg) {
    state.model = msg;
    fillSelect($('#mic'), msg.devices.inputs || [], null);
    fillSelect($('#speaker'), msg.devices.outputs || [], null);
    dress($('#mic'));
    dress($('#speaker'));
    voiceCards();
    hardware(msg.hardware || {});
    const models = $('#other-model');
    models.innerHTML = '';
    (msg.models || []).forEach((m, i) => models.add(new Option(m.name, String(i))));
    models.dispatchEvent(new Event('change'));
    dress(models);
    if (msg.name) {
      $('#name').value = msg.name;
      $('#wake').value = msg.wake || msg.name.toLowerCase();
    }
  },
  checked(msg) {
    const note = state.guideNote;
    if (!note) return;
    note.textContent = msg.message || (msg.ok ? 'Got it.' : "That didn't work.");
    note.className = msg.ok ? 'note good' : 'note bad';
    if (msg.ok) {
      orb({ type: 'emotion', emotion: 'happy', intensity: 0.6, hold: 2 });
      speak(msg.message.split('.')[0] + '.');
    }
  },
  said(msg) {
    if (!guide.pending || msg.id !== guide.pending.id || !msg.audio) return;
    guide.said[`${state.voice}|${$('#effect').value}|${guide.pending.text}`] = msg.audio;
    if (guide.name) play(state.voice, msg.audio);
  },
  answer(msg) {
    orb({ type: 'state', state: 'speaking' });
    if (msg.text) {
      $('#g-answer').textContent = msg.text;
      speak(msg.text);
      return;
    }
    const tips = GUIDES[guide.name]?.steps[guide.i]?.tips || [];
    $('#g-answer').innerHTML = '';
    for (const tip of tips) {
      const p = document.createElement('p');
      p.textContent = tip;
      $('#g-answer').appendChild(p);
    }
  },
  'key-checked'(msg) {
    state.keyOk = msg.ok === true;
    if (state.guideFish) {
      state.guideFish.textContent = msg.ok ? 'Got it, that key works.' : msg.message || "That key didn't work.";
      state.guideFish.className = msg.ok ? 'note good' : 'note bad';
      state.guideFish = null;
    }
    $('#key-note').textContent = msg.ok ? 'Key works. Pick a voice and hear it.' : msg.message || "That key didn't work.";
    if (msg.ok) {
      orb({ type: 'emotion', emotion: 'happy', intensity: 0.6, hold: 2 });
      preview(state.voice);
    }
  },
  preview(msg) {
    if (msg.audio) {
      state.previews[`${msg.voice_id}|${msg.effect || 'console'}`] = msg.audio;
      $('#key-note').textContent = '';
      if (msg.voice_id === state.voice) play(msg.voice_id, msg.audio);
    } else {
      $('#key-note').textContent = msg.message || "Couldn't make the preview.";
      orb({ type: 'state', state: 'speaking' });
    }
  },
  geocoded(msg) {
    if (msg.found) {
      state.home = { query: $('#town').value.trim(), latitude: msg.latitude, longitude: msg.longitude };
      $('#found').textContent = `Found ${msg.label}.`;
      orb({ type: 'moment', name: 'found' });
      const others = $('#others');
      others.innerHTML = '';
      for (const row of msg.others || []) {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = row.label;
        b.addEventListener('click', () => {
          $('#town').value = row.label;
          state.home = { query: row.label, latitude: row.latitude, longitude: row.longitude };
          $('#found').textContent = `Found ${row.label}.`;
          others.innerHTML = '';
        });
        others.appendChild(b);
      }
      if (others.children.length) others.insertAdjacentHTML('afterbegin', '<span class="dim">Not it?</span>');
    } else {
      state.home = null;
      $('#found').textContent = "Couldn't find it, so it's kept as typed.";
    }
  },
  saved(msg) {
    if (!msg.ok) {
      $('#awaken').disabled = false;
      whisper('');
      $('#hardware').insertAdjacentHTML('afterend', '<p class="note"></p>');
      $('#hardware').nextElementSibling.textContent = msg.message || "Couldn't save.";
      return;
    }
    const notes = $('#notes');
    notes.innerHTML = '';
    for (const text of msg.notes || []) {
      const li = document.createElement('li');
      li.textContent = text;
      notes.appendChild(li);
    }
    const name = $('#name').value.trim() || 'OCTAVIA';
    $('#alive-title').textContent = `${name} is awake.`;
    $('#alive-text').textContent = `Say "${$('#wake').value.trim() || name.toLowerCase()}" to start talking.`;
    $('#sign-in').hidden = !msg.sign_in;
    show(indexOf('alive'));
    form(1);
    orb({ type: 'moment', name: 'rainboom' });
    orb({ type: 'emotion', emotion: 'happy', intensity: 1, hold: 6 });
  },
  greeting(msg) {
    state.greeting = msg.audio;
  },
  awake(msg) {
    clearInterval(wakingTimer);
    if (msg.ok && state.greeting) play(state.voice, state.greeting);
    orb({ type: 'mouth', level: 0 });
    if (msg.ok) {
      $$('#waking-steps li').forEach((li) => li.classList.add('done'));
      $('#waking-title').textContent = 'Awake.';
      $('#waking-note').textContent = `Say "${$('#wake').value.trim() || 'octavia'}" whenever you're ready.`;
      form(1);
      orb({ type: 'moment', name: 'rainboom' });
      orb({ type: 'emotion', emotion: 'happy', intensity: 1, hold: 4 });
    } else {
      $('#waking-title').textContent = "It didn't start";
      $('#waking-note').textContent = msg.message || 'Start it from the Start menu: OCTAVIA.';
      orb({ type: 'state', state: 'idle' });
    }
  },
  'signed-in'(msg) {
    $('#sign-in').disabled = false;
    $('#sign-in').hidden = msg.ok === true;
    $('#alive-text').textContent = msg.ok ? 'Signed in. Say its name to start talking.' : msg.message || 'Not signed in yet: try again.';
    orb({ type: 'state', state: msg.ok ? 'speaking' : 'idle' });
  },
};

host.onMessage((msg) => handlers[msg?.type]?.(msg));
const hello = await host.hello();
(hello.pending || []).forEach((msg) => handlers[msg?.type]?.(msg));
form(0);
show(0);
window.welcomePage = { show, state, choices };
