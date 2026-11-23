// Small, soft sounds for the settings window, made on the spot (no files): a glassy blip moving between sections, a
// rising or falling pair for a switch, a faint tink when a change is kept, a low hum when one isn't. They follow her
// own sound cues: off when those are off, as loud as her cue volume says (and quieter still: a window, not her).

let ctx = null;
let level = 0.25; // her audio.earcon_volume
let on = true; // her audio.earcons
let last = 0;

export function configure(enabled, volume) {
  on = enabled !== false;
  if (Number.isFinite(volume)) level = Math.min(Math.max(volume, 0), 1);
}

function audio() {
  if (!on || level <= 0) return null;
  if (!ctx) {
    try {
      ctx = new AudioContext();
    } catch {
      on = false;
      return null;
    }
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  return ctx;
}

// One soft tone: a sine (or triangle) with a quick rise and a smooth fall, through a gentle low-pass.
function tone(ac, { freq, to = freq, at = 0, dur = 0.09, gain = 0.05, type = 'sine' }) {
  const t0 = ac.currentTime + at;
  const osc = ac.createOscillator();
  const amp = ac.createGain();
  const filter = ac.createBiquadFilter();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t0);
  if (to !== freq) osc.frequency.exponentialRampToValueAtTime(to, t0 + dur);
  filter.type = 'lowpass';
  filter.frequency.value = 3200;
  const peak = gain * level * 0.6;
  amp.gain.setValueAtTime(0.0001, t0);
  amp.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), t0 + 0.008);
  amp.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(filter).connect(amp).connect(ac.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

const SOUNDS = {
  open: (ac) => {
    tone(ac, { freq: 660, dur: 0.5, gain: 0.03 });
    tone(ac, { freq: 990, at: 0.06, dur: 0.45, gain: 0.025 });
    tone(ac, { freq: 1320, at: 0.12, dur: 0.4, gain: 0.018 });
  },
  nav: (ac) => {
    tone(ac, { freq: 1480, dur: 0.06, gain: 0.035 });
    tone(ac, { freq: 2220, at: 0.012, dur: 0.05, gain: 0.015 });
  },
  on: (ac) => {
    tone(ac, { freq: 880, dur: 0.06, gain: 0.04 });
    tone(ac, { freq: 1320, at: 0.05, dur: 0.08, gain: 0.04 });
  },
  off: (ac) => {
    tone(ac, { freq: 1100, dur: 0.06, gain: 0.035 });
    tone(ac, { freq: 740, at: 0.05, dur: 0.08, gain: 0.035 });
  },
  pick: (ac) => tone(ac, { freq: 1240, dur: 0.05, gain: 0.03 }),
  saved: (ac) => tone(ac, { freq: 1760, dur: 0.12, gain: 0.022 }),
  refused: (ac) => {
    tone(ac, { freq: 233, dur: 0.16, gain: 0.06, type: 'triangle' });
    tone(ac, { freq: 220, at: 0.09, dur: 0.18, gain: 0.05, type: 'triangle' });
  },
  open_menu: (ac) => tone(ac, { freq: 990, to: 1180, dur: 0.07, gain: 0.025 }),
};

export function play(name) {
  const now = performance.now();
  if (now - last < 40) return; // a burst of events is one sound
  last = now;
  const ac = audio();
  const sound = SOUNDS[name];
  if (!ac || !sound) return;
  try {
    sound(ac);
  } catch {
    // no sound is fine
  }
}
