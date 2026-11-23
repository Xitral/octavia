// A live block: a program of Octavia's running on the panel (a 3D scene they turn with the mouse, a graph with a
// point to drag, a setup that moves), with her sliders, switches and choices under it. The program runs in a
// sandboxed frame of its own (renderer/live/frame.js), an opaque origin with no way to this page, her bridge or the
// network; this page only passes it the controls' values and passes on what it says (it ran, it failed and why, what
// it reports) to Octavia, as data. A frame that stops answering (an endless loop) is taken down and she's told.
//
// Moving a control doesn't ask her anything: the value goes straight to the program. When she changes a value, only
// that value moves (the program keeps running); when she changes the program, it starts again, keeping the values
// the user set on controls she didn't change.

const FRAME_URL = '/renderer/live/frame.html';
const PING_MS = 2000;
const HUNG_MS = 7000; // no answer for this long while the panel is up: the program is stuck
const REPORT_GAP_MS = 400; // the user's slider values reach Octavia at most this often
const STATE_GAP_MS = 250; // what the program reports reaches her at most this often (the latest wins)
const MAX_CODE = 40000;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function kindOf(param) {
  if (param.button === true) return 'button';
  if (typeof param.value === 'boolean') return 'switch';
  if (Array.isArray(param.choices) && param.choices.length) return 'choice';
  return 'number';
}

// The value a control starts at: the user's (when they moved it and she hasn't changed it since), else hers.
export function startValues(block, previous) {
  const out = {};
  for (const p of block.params || []) {
    const before = previous && previous.params ? previous.params.find((q) => q.id === p.id) : null;
    const theirs = previous && previous.values && p.id in previous.values ? previous.values[p.id] : undefined;
    const keep = before && theirs !== undefined && before.value === p.value && kindOf(before) === kindOf(p);
    out[p.id] = keep ? theirs : p.value;
  }
  return out;
}

function shown(param, value) {
  if (kindOf(param) === 'button') return '';
  if (kindOf(param) !== 'number') return String(value);
  const step = Number(param.step) || 0;
  const digits = step && step < 1 ? Math.min(6, Math.ceil(-Math.log10(step))) : 0;
  const text = Number(value).toFixed(digits);
  return param.unit ? `${text} ${param.unit}` : text;
}

// `ui`: the page's helpers { h, inline, parseInline } and `report(event)`, which sends to Octavia; `previous`: the
// block this one replaces and the values its controls had ({params, values}), or null.
export function buildLive(block, ui, previous = null) {
  const { h, inline, parseInline, report } = ui;
  const code = String(block.code || '').slice(0, MAX_CODE);
  const values = startValues(block, previous);
  let spec = block;

  const stage = h('div', 'live-stage');
  stage.style.height = `${clamp(Number(block.height) || 340, 120, 720)}px`;
  const frame = document.createElement('iframe');
  frame.className = 'live-frame';
  // Scripts only: no same origin (so not this page's), no forms, popups, navigating the panel, or downloads.
  frame.setAttribute('sandbox', 'allow-scripts');
  frame.setAttribute('allow', "camera 'none'; microphone 'none'; geolocation 'none'; display-capture 'none'; usb 'none'; serial 'none'; hid 'none'; clipboard-read 'none'; clipboard-write 'none'");
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.title = String(block.alt || 'a live picture');
  // Its name tells the main process which frame's process to end, should this one get stuck.
  frame.name = `live-${Math.random().toString(36).slice(2, 12).padEnd(8, '0')}`;
  frame.src = FRAME_URL;
  const fault = h('div', 'live-fault');
  fault.hidden = true;
  stage.append(frame, h('div', 'photo-scan'), fault);

  const controls = h('div', 'live-controls');
  const rows = new Map(); // id -> {set(value)}
  let reportTimer = null;
  const reportValues = () => {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => report({ type: 'params', values: { ...values } }), REPORT_GAP_MS);
  };
  const post = (msg) => {
    if (frame.contentWindow) frame.contentWindow.postMessage({ live: 1, ...msg }, '*');
  };
  const send = () => post({ type: 'params', values: { ...values } });

  function controlRow(p) {
    const label = h('span', 'sim-label', String(p.label || p.id));
    const kind = kindOf(p);
    let row;
    let set;
    if (kind === 'number') {
      const lo = Number(p.min);
      const hi = Number(p.max);
      const input = h('input', 'sim-range');
      input.type = 'range';
      input.min = String(lo);
      input.max = String(hi);
      input.step = String(Number(p.step) > 0 ? p.step : (hi - lo) / 100 || 'any');
      input.setAttribute('aria-label', String(p.label || p.id));
      const out = h('span', 'sim-value');
      set = (v) => {
        input.value = String(v);
        out.textContent = shown(p, v);
      };
      input.addEventListener('input', () => {
        values[p.id] = Number(input.value);
        out.textContent = shown(p, values[p.id]);
        send();
        reportValues();
      });
      row = h('label', 'sim-slider live-slider', [label, input, out]);
    } else if (kind === 'button') {
      // A one-shot action (drop a ball, reset, splash): each press counts up its value, and the program hears it.
      const button = h('button', 'live-switch live-button', String(p.label || p.id));
      button.type = 'button';
      set = () => {};
      button.addEventListener('click', () => {
        values[p.id] = (Number(values[p.id]) || 0) + 1;
        button.classList.remove('pressed');
        void button.offsetWidth;
        button.classList.add('pressed');
        send();
        reportValues();
      });
      row = h('div', 'sim-slider live-slider', [h('span', 'sim-label'), button, h('span', 'sim-value')]);
    } else if (kind === 'switch') {
      const button = h('button', 'live-switch');
      button.type = 'button';
      set = (v) => {
        button.setAttribute('aria-pressed', String(v === true));
        button.textContent = v === true ? 'on' : 'off';
      };
      button.addEventListener('click', () => {
        values[p.id] = values[p.id] !== true;
        set(values[p.id]);
        send();
        reportValues();
      });
      row = h('div', 'sim-slider live-slider', [label, button, h('span', 'sim-value')]);
    } else {
      const buttons = p.choices.map((choice) => {
        const b = h('button', 'live-choice', String(choice));
        b.type = 'button';
        b.addEventListener('click', () => {
          values[p.id] = choice;
          set(choice);
          send();
          reportValues();
        });
        return b;
      });
      set = (v) => buttons.forEach((b, k) => b.setAttribute('aria-pressed', String(p.choices[k] === v)));
      row = h('div', 'sim-slider live-slider', [label, h('div', 'live-choices', buttons), h('span', 'sim-value')]);
    }
    row.dataset.eid = String(p.id); // addressable as block:id, like a scene's elements
    set(values[p.id]);
    rows.set(p.id, { set });
    return row;
  }

  function drawControls() {
    rows.clear();
    controls.replaceChildren(...(spec.params || []).map(controlRow));
    controls.hidden = !(spec.params || []).length;
  }
  drawControls();

  // ---- the frame: given the program each time it says hello (moving the block reloads it); watched while it runs

  let started = false;
  const born = Date.now();
  let waiting = 0; // when the ping still unanswered went out (0: none out)
  let pinged = 0;
  let dead = false;
  let said = false; // ok or error already passed on for this load
  let stateTimer = null;
  let stateNext = null;
  const stop = (why) => {
    if (dead) return;
    dead = true;
    clearInterval(watchdog);
    clearTimeout(stateTimer);
    fault.textContent = why;
    fault.hidden = false;
    frame.remove();
  };
  // Stuck means a ping unanswered for HUNG_MS while the panel is visible: a hidden page's timers are throttled to
  // one a minute, which says nothing about the program.
  const watchdog = setInterval(() => {
    if (!frame.isConnected) {
      if (!document.contains(stage)) clearInterval(watchdog); // the block went (rebuilt, or the panel changed)
      return;
    }
    if (document.hidden) {
      waiting = 0;
      return;
    }
    if (!started) {
      if (Date.now() - born > HUNG_MS) {
        report({ type: 'error', message: "the live picture's page didn't start" });
        stop("couldn't start");
      }
      return;
    }
    if (waiting && Date.now() - waiting > HUNG_MS) {
      report({ type: 'error', message: 'the program stopped responding (an endless loop, or far too much work per frame)', hung: true, frame: frame.name });
      setTimeout(() => stop('stopped: it stopped responding'), 50); // the main process ends its process first
      return;
    }
    if (!waiting) {
      waiting = Date.now();
      post({ type: 'ping', n: ++pinged });
    }
  }, PING_MS);

  const onMessage = (event) => {
    if (dead || event.source !== frame.contentWindow) return;
    const msg = event.data;
    if (!msg || typeof msg !== 'object' || msg.live !== 1) return;
    if (msg.type === 'pong') waiting = 0;
    else if (msg.type === 'hello') {
      started = true;
      said = false;
      waiting = 0;
      post({ type: 'run', code, params: { ...values } });
    } else if (msg.type === 'ok' || msg.type === 'error') {
      if (said) return; // one verdict per run: a program can't flood her with them
      said = true;
      if (msg.type === 'ok') report({ type: 'ok' });
      else report({ type: 'error', message: String(msg.message || '').slice(0, 400), line: Number.isInteger(msg.line) ? msg.line : null });
    } else if (msg.type === 'state' && typeof msg.state === 'string') {
      stateNext = msg.state.slice(0, 1500);
      if (!stateTimer) {
        stateTimer = setTimeout(() => {
          stateTimer = null;
          report({ type: 'state', state: stateNext });
        }, STATE_GAP_MS);
      }
    } else if (msg.type === 'set' && rows.has(msg.id)) {
      const p = (spec.params || []).find((q) => q.id === msg.id);
      let v = msg.value;
      if (kindOf(p) === 'button') return; // only a press counts it up
      if (kindOf(p) === 'number') v = clamp(Number(v), Number(p.min), Number(p.max));
      else if (kindOf(p) === 'switch') v = v === true;
      else if (!p.choices.includes(v)) return;
      if (!Number.isFinite(v) && kindOf(p) === 'number') return;
      values[msg.id] = v;
      rows.get(msg.id).set(v);
      reportValues();
    }
  };
  window.addEventListener('message', onMessage);

  const node = h('div', 'live', [stage, controls]);
  node.live = {
    // The same program with other controls, values, words: the controls are drawn again, values she changed are
    // sent to it, and she hears where they all are now.
    update(next) {
      const before = spec;
      spec = next;
      const fresh = startValues(next, { params: before.params, values });
      for (const k of Object.keys(values)) if (!(k in fresh)) delete values[k];
      Object.assign(values, fresh);
      frame.title = String(next.alt || 'a live picture');
      drawControls();
      send();
      reportValues();
      return true;
    },
    values: () => ({ ...values }),
    block: () => spec,
    destroy() {
      window.removeEventListener('message', onMessage);
      clearInterval(watchdog);
      clearTimeout(stateTimer);
      clearTimeout(reportTimer);
    },
  };
  return node;
}

export { kindOf };
