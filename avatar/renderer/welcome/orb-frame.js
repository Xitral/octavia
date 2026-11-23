// OCTAVIA's own orb (../orb.js), inside the setup page: the page tells it what to show with the same messages the
// assistant sends (show, state, mouth, emotion, moment), passed in with postMessage. Nothing here talks to the app.
import { runOrb } from '../orb.js';

const listeners = [];
const host = {
  onMessage: (cb) => listeners.push(cb),
  onLayout: () => {},
  onCursor: () => {},
  emit: () => {},
  setIgnoreMouse: () => {},
  drag: () => {},
};
const send = (msg) => listeners.forEach((cb) => cb(msg));

window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin || !event.data || typeof event.data.type !== 'string') return;
  send(event.data);
});

await runOrb(host, { form: 'orb' }, document.getElementById('stage'));
send({ type: 'show' });
window.parent.postMessage({ type: 'orb-ready' }, window.location.origin);
