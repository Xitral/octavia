'use strict';
// Where she is and how she gets there: at home (the spot the user dragged her to), out on the screen pointing
// at something, sitting on top of a window, or flying a victory lap round the screen. Octavia's show, hide, point and unpoint all go through here, one step at a time. A
// step that has to wait for the renderer (she zips out before her window moves, then zips in) holds every
// later message until it's done, so they're applied in order and never land mid-flight. Lip sync and Ctrl (`keys`)
// are the exceptions: they have to stay in time with the audio and the user's hand. Every wait has a timeout, so a renderer that never
// answers can't wedge her.
//
// No Electron in here: the window, the trail and Electron's `screen` are handed in, so tests can fake them.

const { centreOf, cleanMetrics, homeLayout, lapStops, perchLayout, pointLayout } = require('./layout');

const TIMING = {
  zipOut: 700, // longest wait for the renderer's zip-out (about 0.25 s when all is well)
  arrive: 1200, // ...and for a zip-in (about 0.45 s)
  lead: 0.55, // she zips in once the streak's head is this far along
};
const LABEL_MAX = 60;
const COORD_MAX = 1e6; // anything bigger than this is not a spot on anyone's desktop
// The streak across the screen: quick for short hops, never slow for long ones.
const TRAIL_MIN_MS = 220;
const TRAIL_MS_PER_PX = 0.12;
const TRAIL_MAX_MS = 420;
const TRAIL_THICKNESS = 0.06; // of her height
const BOOM_MS = 1100; // a sonic rainboom's ring takes this long to race across the screen
const PRESENCE = new Set(['show', 'hide', 'point', 'unpoint']);
// Now or never: a glance or a jump held up behind a flight would come too late to mean anything.
const PASSING = new Set(['glance', 'startle']);
// Never held up: lip sync has to stay in time with the audio, and Ctrl (keys) with the user's hand.
const IMMEDIATE = new Set(['mouth', 'keys']);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const relative = (p, bounds) => ({ x: Math.round(p.x - bounds.x), y: Math.round(p.y - bounds.y) });
const usable = (v) => Number.isFinite(v) && Math.abs(v) < COORD_MAX;

class Choreographer {
  // avatar: an AvatarWindow (lib/window.js). trails: a Trails (lib/trail.js) or null. screen: Electron's.
  // emit: sends a message to Octavia.
  constructor({ cfg, avatar, trails, screen, emit, log, debug = () => {}, timing = {} }) {
    this.cfg = cfg;
    this.avatar = avatar;
    this.trails = trails;
    this.screen = screen;
    this.emit = emit;
    this.log = log;
    this.debug = debug;
    this.timing = { ...TIMING, ...timing };
    this.metrics = cleanMetrics(null);
    this.mode = 'home'; // 'home', 'point', 'perch' or 'lap'
    this.home = null; // her home window's bounds, from when she last left it
    this.away = null; // while pointing or perched: {centre (her body's, DIP screen), height}
    this.body = null; // while away (pointing, perched, on a lap): her body's rectangle (DIP screen), for her subtitles
    this.target = null; // while pointing: the spot (DIP screen), which her subtitles keep clear of
    this.hideRequested = false; // a hide at home is under way and nothing has overtaken it
    this.busy = false;
    this.queue = [];
    this.waiting = null; // {type, done(ok)} while a step waits for the renderer
    this.closed = false;
  }

  // Every protocol message once she's ready (except quit and capture, which main.js handles itself).
  handle(msg) {
    if (this.closed) return;
    if (!this.busy || IMMEDIATE.has(msg.type)) this.apply(msg);
    else if (!PASSING.has(msg.type)) this.queue.push(msg);
  }

  // The renderer's replies that are for main only. False when `msg` isn't one of them.
  fromRenderer(msg) {
    switch (msg.type) {
      case 'zipped-out':
      case 'arrived':
        if (this.waiting && this.waiting.type === msg.type) this.waiting.done(true);
        return true;
      case 'metrics':
        this.metrics = cleanMetrics(msg);
        this.debug(`metrics: ${JSON.stringify(this.metrics)}`);
        return true;
      case 'hidden':
        this.onHidden();
        return true;
      case 'rainboom':
        this.rainboom(msg);
        return true;
      default:
        return false;
    }
  }

  // The renderer's sonic rainboom went off at (x, y) (window px, her chest): its ring carries on across the
  // screen, out to the farthest monitor corner.
  rainboom(msg) {
    if (!this.trails || !Number.isFinite(msg.x) || !Number.isFinite(msg.y)) return;
    const b = this.avatar.bounds;
    const at = { x: b.x + msg.x, y: b.y + msg.y };
    const reach = this.trails.reachFrom(at);
    if (reach > 0) this.trails.boom(at, reach, BOOM_MS);
  }

  close() {
    this.closed = true;
    this.queue.length = 0;
    if (this.waiting) this.waiting.done(false);
  }

  apply(msg) {
    switch (msg.type) {
      case 'point':
        this.run('point', () => this.point(msg));
        return;
      case 'unpoint':
        if (this.mode === 'point') this.run('unpoint', () => this.flyHome());
        return;
      case 'perch':
        if (this.mode === 'home' && this.avatar.visible && !this.hideRequested) this.run('perch', () => this.perch(msg));
        return;
      case 'unperch':
        if (this.mode === 'perch') this.run('unperch', () => this.flyHome());
        return;
      case 'lap':
        // Only from home, and only when she's there to fly it.
        if (this.mode === 'home' && this.avatar.visible && !this.hideRequested) this.run('lap', () => this.lap());
        return;
      case 'glance': {
        // At home or sitting on a window: out pointing, her eyes have somewhere to be.
        const at = (this.mode === 'home' || this.mode === 'perch') && this.avatar.visible ? this.windowPoint(msg) : null;
        if (at) this.send({ type: 'glance', x: at.x, y: at.y });
        return;
      }
      case 'startle':
        if (this.avatar.visible) this.send(msg);
        return;
      case 'hide':
        if (this.mode !== 'home') {
          this.run('hide', () => this.hideFromPoint());
          return;
        }
        this.hideRequested = true;
        break;
      case 'show':
        if (this.mode !== 'home') return; // she's already out there
        this.hideRequested = false;
        this.avatar.show();
        break;
      default:
        break;
    }
    this.send(msg);
  }

  // Starts `step` right away (so it has claimed the window before anything else is handled) and holds the
  // queue until it's done.
  run(name, step) {
    this.busy = true;
    (async () => {
      try {
        await step();
      } catch (err) {
        this.log(`${name} failed: ${(err && err.stack) || err}`);
      } finally {
        this.busy = false;
        this.drain();
      }
    })();
  }

  drain() {
    while (!this.busy && !this.closed && this.queue.length) this.apply(this.queue.shift());
  }

  send(msg) {
    this.avatar.send('avatar:message', msg);
  }

  // The renderer finished a hide at home (wave, sink): the window can go.
  onHidden() {
    // A show (or a point) that came while the renderer was reporting this one wins: keep the window up.
    if (!this.hideRequested) return;
    this.hideRequested = false;
    this.avatar.hide();
    this.debug('hidden at home');
    this.emit({ type: 'hidden' });
  }

  // ---- the steps ----

  async point(msg) {
    const spot = this.spotOf(msg);
    if (!spot) {
      this.log(`ignoring a point without a usable spot: ${JSON.stringify(msg).slice(0, 200)}`);
      return;
    }
    const { avatar } = this;
    const leaving = this.mode === 'home';
    const home = leaving ? avatar.bounds : this.home;
    const origin = leaving ? centreOf(home) : this.away.centre;
    const plan = pointLayout({
      display: this.screen.getDisplayNearestPoint(spot.target),
      target: spot.target,
      box: spot.box,
      bodyHeight: this.cfg.pointHeight,
      metrics: this.metrics,
      home,
      from: origin,
    });
    const dest = centreOf(plan.body);
    // From here on she's leaving. A drag at home keeps wherever it got to (that's where home is now).
    avatar.endDrag(true, { click: false });
    avatar.setAtHome(false);
    this.home = home;
    this.hideRequested = false; // a point shows her: a hide still under way is overtaken
    const shown = avatar.visible;
    if (shown) {
      await this.zipOut(dest);
      if (this.closed) return;
      // Nothing's left of her to fly: if the queue ends up hiding her anyway, go straight there.
      if (this.hideQueued()) {
        this.leaveHidden();
        return;
      }
    }
    const lead = this.streak(origin, dest, plan.body.height);
    this.body = plan.body; // before her window moves: her subtitles go where she lands
    this.target = spot.target;
    avatar.setBounds(plan.bounds);
    avatar.send('avatar:layout', plan.layout);
    if (!shown) avatar.show();
    this.mode = 'point';
    this.away = { centre: dest, height: plan.body.height };
    this.debug(`pointing ${plan.side} of ${JSON.stringify(spot.target)}: window ${JSON.stringify(plan.bounds)}, ${shown ? 'flew' : 'appeared'}`);
    if (lead) await sleep(lead);
    if (this.closed) return;
    const { target, box, arm, from } = plan.layout;
    const arrived = this.waitFor('arrived', this.timing.arrive);
    this.send({ type: 'point', target, box, arm, from, label: spot.label });
    await arrived;
  }

  // Sits on top of the window whose frame the message gives (physical px): zips out from home and flies there,
  // landing seated with her legs over its top edge. Nothing when there's no room for her there.
  async perch(msg) {
    const frame = this.frameOf(msg);
    if (!frame) {
      this.log(`ignoring a perch without a usable window: ${JSON.stringify(msg).slice(0, 200)}`);
      return;
    }
    const { avatar } = this;
    const edge = { l: frame.x, t: frame.y, r: frame.x + frame.width };
    const display = this.screen.getDisplayNearestPoint({ x: Math.round(frame.x + frame.width / 2), y: frame.y });
    const plan = perchLayout({ display, edge, bodyHeight: this.cfg.pointHeight, metrics: this.metrics });
    if (!plan) {
      this.debug(`no room to sit on ${JSON.stringify(frame)}`);
      return;
    }
    avatar.endDrag(true, { click: false });
    const home = avatar.bounds;
    const origin = centreOf(home);
    const dest = centreOf(plan.body);
    avatar.setAtHome(false);
    this.home = home;
    await this.zipOut(dest);
    if (this.closed) return;
    if (this.hideQueued()) {
      this.leaveHidden();
      return;
    }
    const lead = this.streak(origin, dest, plan.body.height);
    this.body = plan.body; // before her window moves: her subtitles go where she lands
    this.target = null;
    avatar.setBounds(plan.bounds);
    avatar.send('avatar:layout', plan.layout);
    this.mode = 'perch';
    this.away = { centre: dest, height: plan.body.height };
    this.debug(`sitting on ${JSON.stringify(frame)}: window ${JSON.stringify(plan.bounds)}`);
    if (lead) await sleep(lead);
    if (this.closed) return;
    const arrived = this.waitFor('arrived', this.timing.arrive);
    this.send({ type: 'show', reason: 'return', from: relative(origin, plan.bounds) });
    await arrived;
  }

  // From pointing or a perch: zips out, flies home and zips in there.
  async flyHome() {
    const { avatar } = this;
    avatar.endDrag(true, { click: false });
    const home = this.homeBounds();
    const homeCentre = centreOf(home);
    const start = this.away.centre;
    await this.zipOut(homeCentre);
    if (this.closed) return;
    if (this.hideQueued()) {
      this.leaveHidden();
      return;
    }
    const lead = this.streak(start, homeCentre, this.away.height);
    this.goHome(home);
    this.debug(`back home: window ${JSON.stringify(home)}`);
    if (lead) await sleep(lead);
    if (this.closed) return;
    const arrived = this.waitFor('arrived', this.timing.arrive);
    // She zips in at home from where she was, instead of rising.
    this.send({ type: 'show', reason: 'return', from: relative(start, home) });
    await arrived;
  }

  // A victory lap: round three corners of the screen and home again, zipping from one to the next with her streak
  // across the screen behind her, as excited as she can be.
  async lap() {
    const { avatar } = this;
    avatar.endDrag(true, { click: false });
    const home = avatar.bounds;
    const display = this.screen.getDisplayNearestPoint(centreOf(home));
    const stops = lapStops({ display, home, bodyHeight: this.cfg.pointHeight, metrics: this.metrics });
    avatar.setAtHome(false);
    this.home = home;
    this.mode = 'lap';
    let from = centreOf(home);
    let height = home.height;
    for (const stop of [...stops, null]) {
      const back = stop ? null : this.homeBounds();
      const to = stop ? centreOf(stop.body) : centreOf(back);
      await this.zipOut(to);
      if (this.closed) return;
      if (this.hideQueued()) {
        this.leaveHidden();
        return;
      }
      const lead = this.streak(from, to, stop ? stop.body.height : height);
      if (stop) {
        this.body = stop.body;
        this.target = null;
        avatar.setBounds(stop.bounds);
        avatar.send('avatar:layout', stop.layout);
      } else {
        this.goHome(back);
      }
      if (lead) await sleep(lead);
      if (this.closed) return;
      const bounds = stop ? stop.bounds : back;
      const arrived = this.waitFor('arrived', this.timing.arrive);
      this.send({ type: 'show', reason: 'return', from: relative(from, bounds) });
      // Showing her after she's zipped out clears her face: she beams all the way round.
      this.send({ type: 'emotion', emotion: 'excited', intensity: 0.9, hold: 2 });
      await arrived;
      if (stop) {
        from = to;
        height = stop.body.height;
      }
    }
    this.send({ type: 'gesture', name: 'fist_pump' }); // and home: yes!
  }

  async hideFromPoint() {
    this.avatar.endDrag(true, { click: false });
    await this.zipOut(centreOf(this.homeBounds()));
    if (!this.closed) this.leaveHidden();
  }

  // She has zipped out while away: hide the window, put it back home, and tell Octavia she's gone.
  leaveHidden() {
    const home = this.homeBounds();
    this.avatar.hide();
    this.goHome(home);
    this.hideRequested = false; // so the renderer's own "hidden" below isn't passed on twice
    // She's already invisible; this lets the renderer stop drawing until she's shown again.
    this.send({ type: 'hide' });
    this.debug(`hidden while away; home is ${JSON.stringify(home)}`);
    this.emit({ type: 'hidden' });
  }

  goHome(home) {
    this.body = null;
    this.target = null;
    this.avatar.setBounds(home);
    this.avatar.send('avatar:layout', homeLayout(home, this.cfg.framing));
    this.avatar.setAtHome(true);
    this.mode = 'home';
    this.away = null;
  }

  // ---- helpers ----

  homeBounds() {
    return this.avatar.homeBounds(this.home || this.avatar.bounds);
  }

  // Where she is (DIP screen): her body's rectangle while she's away, else her window (home).
  bodyBounds() {
    return this.body ? { ...this.body } : this.avatar.bounds;
  }

  // The spot she's pointing at (DIP screen), or null.
  pointedAt() {
    return this.target ? { ...this.target } : null;
  }

  async zipOut(toward) {
    const done = this.waitFor('zipped-out', this.timing.zipOut);
    this.send({ type: 'zip-out', to: relative(toward, this.avatar.bounds) });
    await done;
  }

  // Resolves true when the renderer sends `type`, false after `ms` (or when closing).
  waitFor(type, ms) {
    if (this.waiting) this.waiting.done(false);
    return new Promise((resolve) => {
      const waiting = {
        type,
        done: (ok) => {
          clearTimeout(timer);
          if (this.waiting === waiting) this.waiting = null;
          if (!ok && !this.closed) this.log(`no ${type} from the renderer after ${ms} ms; carrying on`);
          resolve(ok);
        },
      };
      const timer = setTimeout(() => waiting.done(false), ms);
      this.waiting = waiting;
    });
  }

  // Whether the presence messages waiting in the queue end with a hide. If so they're dropped (their net
  // effect is that she's gone) and the caller hides her right away. Other messages keep their places.
  hideQueued() {
    let last = -1;
    this.queue.forEach((msg, i) => {
      if (PRESENCE.has(msg.type)) last = i;
    });
    if (last < 0 || this.queue[last].type !== 'hide') return false;
    this.queue = this.queue.filter((msg, i) => i > last || !PRESENCE.has(msg.type));
    return true;
  }

  // Draws the streak from `from` to `to` (DIP, screen). Returns how long to wait before she zips in.
  streak(from, to, height) {
    if (!this.cfg.trail || !this.trails) return 0;
    const distance = Math.hypot(to.x - from.x, to.y - from.y);
    if (distance < 1) return 0;
    const ms = Math.min(TRAIL_MAX_MS, TRAIL_MIN_MS + distance * TRAIL_MS_PER_PX);
    const drawn = this.trails.draw(from, to, ms, Math.max(12, Math.round(height * TRAIL_THICKNESS)));
    return drawn ? Math.round(ms * this.timing.lead) : 0;
  }

  // A message's window frame (left, top, right, bottom: physical px) in DIP screen coordinates, or null.
  frameOf(msg) {
    const edges = [msg.left, msg.top, msg.right, msg.bottom];
    if (!edges.every(usable)) return null;
    const [l, t, r, b] = edges.map(Math.round);
    return r > l && b > t ? this.toDipRect({ x: l, y: t, width: r - l, height: b - t }) : null;
  }

  // A message's x, y (physical px on the desktop) in her window's coordinates, or null when they aren't usable.
  windowPoint(msg) {
    if (!usable(msg.x) || !usable(msg.y)) return null;
    return relative(this.toDipPoint({ x: Math.round(msg.x), y: Math.round(msg.y) }), this.avatar.bounds);
  }

  // A point message's spot, box and label in DIP screen coordinates, or null when it has no usable spot.
  spotOf(msg) {
    if (!usable(msg.x) || !usable(msg.y)) return null;
    const target = this.toDipPoint({ x: Math.round(msg.x), y: Math.round(msg.y) });
    let box = null;
    if (Array.isArray(msg.box) && msg.box.length === 4 && msg.box.every(usable)) {
      const [l, t, r, b] = msg.box.map(Math.round);
      if (r > l && b > t) box = this.toDipRect({ x: l, y: t, width: r - l, height: b - t });
    }
    const label = typeof msg.label === 'string' ? msg.label.trim().slice(0, LABEL_MAX) : '';
    return { target, box, label };
  }

  // Octavia speaks physical pixels (what screenshots and UI Automation use); Electron places windows in DIP.
  // Only Windows (and Linux) have the conversion; elsewhere the two are the same.
  toDipPoint(p) {
    return typeof this.screen.screenToDipPoint === 'function' ? this.screen.screenToDipPoint(p) : p;
  }

  toDipRect(r) {
    return typeof this.screen.screenToDipRect === 'function' ? this.screen.screenToDipRect(null, r) : r;
  }
}

module.exports = { Choreographer, TIMING };
