// How awake she is by the clock (local time): sleepy late at night (heavier eyelids, slower blinks, slower breaths,
// yawns), perky in the morning (a brighter resting face, a little more fidgeting).
//   sleepy: 0 by day; rises from 22:00 to full at midnight, stays full until 05:00, gone by 08:00.
//   perky:  0 at night; rises from 06:30 to full at 08:00, stays until 10:30, gone by noon.

import { clamp } from './math.js';

const ramp = (x, from, to) => clamp((x - from) / (to - from), 0, 1);

export function energyAt(date = new Date()) {
  const h = date.getHours() + date.getMinutes() / 60;
  const sleepy = h >= 22 ? ramp(h, 22, 24) : h < 5 ? 1 : 1 - ramp(h, 5, 8);
  const perky = ramp(h, 6.5, 8) * (1 - ramp(h, 10.5, 12));
  return { sleepy, perky };
}
