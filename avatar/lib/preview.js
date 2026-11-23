'use strict';
// `--preview`: plays Octavia's side of the protocol by itself, so the avatar can be watched and tuned
// without the assistant running. Messages go through the same dispatch as real stdin messages.

const SEGMENTS = [
  { text: 'Hey there! Good to see you.', emotion: 'happy', gesture: 'wave' },
  { text: 'I looked into it, and it is actually pretty neat.', emotion: 'excited', gesture: 'fist_pump' },
  { text: 'Honestly? I called it ages ago.', emotion: 'smug', gesture: 'hands_on_hips' },
  { text: 'Want to try something a little bit fun?', emotion: 'playful', gesture: null },
  { text: 'Hmm, I wonder what that one does.', emotion: 'curious', gesture: 'think_chin' },
  { text: 'Ha! That is the funniest thing all day.', emotion: 'laughing', gesture: 'laugh' },
  { text: 'Wait, what? It did that?', emotion: 'surprised', gesture: 'surprised_jolt' },
  { text: 'Oh no, the build failed again.', emotion: 'sad', gesture: 'sigh' },
  { text: 'That is not okay. Not at all.', emotion: 'angry', gesture: 'shake' },
  { text: 'No idea, really. Could be either.', emotion: 'neutral', gesture: 'shrug' },
  { text: 'All done. Take it easy for a while.', emotion: 'relaxed', gesture: 'nod' },
  { text: 'Let me think about how to put this.', emotion: 'thinking', gesture: null },
];
// What she says while pointing: first at the middle of the screen, then at the mouse.
const POINTING = [
  { text: 'Found it! Right there, in the middle.', emotion: 'excited', gesture: null },
  { text: 'And that is where your cursor is hiding.', emotion: 'playful', gesture: null },
];
// Her props and moments (docs/avatar-life-plan.md): one of these each round. A number is a pause (ms).
const SHOW_OFF = [
  [{ type: 'activity', name: 'tasks' }, 1500, { type: 'moment', name: 'tick' }, 2200, { type: 'activity', name: null }],
  [{ type: 'moment', name: 'timer_start' }, 2000],
  [{ type: 'moment', name: 'bell' }, 2200],
  [{ type: 'moment', name: 'timer_done' }, 2400],
  [{ type: 'moment', name: 'rainboom' }, 2400],
  [{ type: 'activity', name: 'web' }, 3200, { type: 'activity', name: null }],
  [{ type: 'moment', name: 'music' }, 4600],
  [{ type: 'moment', name: 'sunglasses' }, 3000],
  [{ type: 'moment', name: 'umbrella' }, 3200],
  [{ type: 'activity', name: 'files' }, 3000, { type: 'activity', name: null }],
  [{ type: 'moment', name: 'megaphone' }, 2600],
  [{ type: 'activity', name: 'morning' }, 5500, { type: 'activity', name: null }],
  [{ type: 'moment', name: 'wrench' }, 2200],
  [{ type: 'gesture', name: 'spin' }, 1700],
];
const SEGMENTS_PER_ROUND = 3;
const HIDE_WHILE_POINTING_ROUND = 2; // once, the conversation ends while she's still pointing
const MOUTH_HZ = 50;

function wait(ms, token) {
  return new Promise((resolve) => {
    const t = setTimeout(() => {
      token.timers.delete(t);
      resolve();
    }, ms);
    token.timers.add(t);
  });
}

// Fake speech: syllables of random vowels at a natural rate, with short pauses between words.
async function speak(dispatch, seconds, token) {
  const frames = Math.round(seconds * MOUTH_HZ);
  let syllable = { start: 0, length: 1, vowel: 0 };
  for (let f = 0; f < frames && !token.stopped; f++) {
    if (f >= syllable.start + syllable.length) {
      const pause = Math.random() < 0.18 ? 4 + Math.floor(Math.random() * 8) : 0;
      syllable = { start: f + pause, length: 7 + Math.floor(Math.random() * 7), vowel: Math.floor(Math.random() * 5) };
    }
    const phase = (f - syllable.start) / syllable.length;
    const level = phase < 0 ? 0 : Math.sin(Math.PI * phase) * (0.55 + 0.4 * Math.random());
    const v = [0, 0, 0, 0, 0];
    v[syllable.vowel] = level * 0.8;
    v[(syllable.vowel + 1) % 5] = level * 0.2;
    dispatch({ type: 'mouth', level, v });
    await wait(1000 / MOUTH_HZ, token);
  }
}

// `spot(which)` gives a point to point at in physical desktop pixels, like Octavia's: 'centre' (of the
// monitor she lives on) or 'cursor'; null skips pointing. A prop being tuned (`--prop`) needs nothing here:
// the renderer has her hold it throughout and up at her eye in the close look, and the snapshot shows the
// camera prop.
function startPreview(dispatch, { spot = () => null } = {}) {
  const token = { stopped: false, timers: new Set() };
  let next = 0;
  let round = 0;
  let shown = 0; // SHOW_OFF entries played
  const say = (type, extra) => !token.stopped && dispatch({ type, ...extra });
  const talk = async (seg) => {
    say('segment', { text: seg.text, emotion: seg.emotion, intensity: 0.8, gesture: seg.gesture });
    await speak(dispatch, 0.8 + seg.text.length * 0.065, token);
    await wait(350, token);
  };
  const pointAt = (which, label) => {
    let p = null;
    try {
      p = spot(which);
    } catch {
      // No spot today (the screen API failed): the demo just skips the pointing.
    }
    if (!p || token.stopped) return false;
    say('point', { x: p.x, y: p.y, box: null, label });
    return true;
  };

  (async () => {
    while (!token.stopped) {
      round += 1;
      say('show', { reason: 'wake' });
      await wait(2500, token);
      say('state', { state: 'idle' });
      await wait(4000, token);
      say('state', { state: 'listening' });
      await wait(2500, token);
      say('state', { state: 'transcribing' });
      await wait(1000, token);
      say('state', { state: 'thinking' });
      await wait(1500, token);
      // "Find the apple on my screen": a screenshot, then a close look at it.
      say('snapshot');
      await wait(1800, token);
      say('activity', { name: 'search' });
      await wait(3000, token);
      say('activity', { name: null });
      if (pointAt('centre', 'the middle of the screen')) {
        await wait(1200, token); // the flight
        say('state', { state: 'speaking' });
        await talk(POINTING[0]);
        if (pointAt('cursor', 'your cursor')) await wait(1200, token);
        await talk(POINTING[1]);
        if (round === HIDE_WHILE_POINTING_ROUND) {
          // The conversation ends with her still out there: she leaves from where she is.
          say('state', { state: 'idle' });
          await wait(1500, token);
          say('hide');
          await wait(3000, token);
          continue;
        }
        say('unpoint');
        await wait(1500, token);
      }
      say('state', { state: 'speaking' });
      for (let i = 0; i < SEGMENTS_PER_ROUND && !token.stopped; i++) await talk(SEGMENTS[next++ % SEGMENTS.length]);
      for (const step of SHOW_OFF[shown++ % SHOW_OFF.length]) {
        if (typeof step === 'number') await wait(step, token);
        else say(step.type, step);
      }
      say('state', { state: 'confirming' });
      await wait(3000, token);
      say('state', { state: 'idle' });
      await wait(1500, token);
      say('emotion', { emotion: 'sad', intensity: 0.5, hold: 2 });
      await wait(3000, token);
      say('gesture', { name: 'nod' });
      await wait(2000, token);
      say('hide');
      await wait(3000, token);
    }
  })();

  return () => {
    token.stopped = true;
    for (const t of token.timers) clearTimeout(t);
  };
}

module.exports = { startPreview };
