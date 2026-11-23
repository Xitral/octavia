// The guided setup: seven small choices, each waking OCTAVIA's orb a little more (its colour, brightness and mood),
// the voices heard through it, and a flare when it comes to life. The setup process (octavia.platform.welcome_window)
// does the real work: devices, previews, finding the town, saving. Nothing is saved until "Bring OCTAVIA to life".

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
};

// ---------------------------------------------------------------- steps
const dots = $('#dots');
for (let i = 1; i < steps.length - 1; i += 1) dots.appendChild(document.createElement('li'));

function show(index) {
  state.at = Math.max(0, Math.min(steps.length - 1, index));
  steps.forEach((step, i) => {
    step.hidden = i !== state.at;
  });
  const step = steps[state.at];
  [...dots.children].forEach((dot, i) => dot.classList.toggle('on', i < state.at));
  form(state.at / (steps.length - 2));
  orb({ type: 'state', state: step.dataset.mood || 'idle' });
  if (state.at > 0) orb({ type: 'moment', name: 'step' });
  whisper(WHISPERS[step.dataset.step]);
  step.querySelector('input:not([type=radio]):not([type=checkbox]), select')?.focus({ preventScroll: true });
}

document.addEventListener('click', (event) => {
  const t = event.target.closest('button');
  if (!t) return;
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
  const audio = new Audio(`data:audio/mpeg;base64,${base64}`);
  state.playing = audio;
  $$('.voice').forEach((card) => card.classList.toggle('playing', card.dataset.id === id));
  audio.addEventListener('ended', () => $$('.voice').forEach((card) => card.classList.remove('playing')));
  speakThrough(audio);
  audio.play().catch(() => {});
}

function preview(id) {
  if (state.previews[id]) return play(id, state.previews[id]);
  if (!state.keyOk) {
    $('#key-note').textContent = 'Paste a Fish Audio key above to hear the voices.';
    return;
  }
  $('#key-note').textContent = 'Making the preview with your key (a few seconds)...';
  orb({ type: 'state', state: 'thinking' });
  host.send({ type: 'preview', voice_id: id, key: $('#fish').value.trim() });
}

$('#check-key').addEventListener('click', () => {
  const key = $('#fish').value.trim();
  if (!key) return;
  $('#key-note').textContent = 'Checking the key...';
  host.send({ type: 'check-key', key });
});

$('#find').addEventListener('click', () => {
  const town = $('#town').value.trim();
  if (!town) return;
  $('#found').textContent = 'Looking...';
  host.send({ type: 'geocode', place: town });
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
  set('brain.claude.auth', brain);
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

$('#finish').addEventListener('click', () => host.send({ type: 'done' }));

// ---------------------------------------------------------------- from the setup process
const handlers = {
  model(msg) {
    state.model = msg;
    fillSelect($('#mic'), msg.devices.inputs || [], null);
    fillSelect($('#speaker'), msg.devices.outputs || [], null);
    voiceCards();
    hardware(msg.hardware || {});
    if (msg.name) {
      $('#name').value = msg.name;
      $('#wake').value = msg.wake || msg.name.toLowerCase();
    }
  },
  'key-checked'(msg) {
    state.keyOk = msg.ok === true;
    $('#key-note').textContent = msg.ok ? 'Key works. Pick a voice and hear it.' : msg.message || "That key didn't work.";
    if (msg.ok) {
      orb({ type: 'emotion', emotion: 'happy', intensity: 0.6, hold: 2 });
      preview(state.voice);
    }
  },
  preview(msg) {
    if (msg.audio) {
      state.previews[msg.voice_id] = msg.audio;
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
    show(steps.length - 1);
    form(1);
    orb({ type: 'moment', name: 'rainboom' });
    orb({ type: 'emotion', emotion: 'happy', intensity: 1, hold: 6 });
    if (state.previews[state.voice]) play(state.voice, state.previews[state.voice]);
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
