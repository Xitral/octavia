# Pop-up panels

While it explains something, OCTAVIA can put a small panel on your screen: a diagram of how the parts connect, a
chart of the numbers, formatted notes with a table or a formula, key facts, a timeline, or two things side by side.
It looks like its orb (near-black glass lit by its luminous blue with a white-hot heart, a star that flares now and
then, the odd twinkle, fine PS2 scanlines and Bayer dithering), opens with a smooth unfold and a soft sound, and never
takes the keyboard from what you're typing in. As the conversation goes on it changes the panel that's up (lights
the node it's on, ticks off a step, adds the next one) instead of opening new ones.

- **Close it** with the ✕, a right-click anywhere on it, or by asking it ("close that"). Otherwise it closes by
  itself after `visuals.stay_s` (3 minutes); the thin line along its bottom shows the time left, and the count
  pauses while the pointer is on it.
- **Move it** by dragging its title bar; it follows the pointer at the display's rate. New panels open where you last
  left one.
- **Resize it** from any side edge, the bottom, or a bottom corner. The text grows with the width (up to 1.8 times),
  diagrams and charts are redrawn for it, and the width is kept for the next panels (narrow and wide ones apart). A
  height you set sticks for that panel; otherwise it follows its content.
- **Write on it** with the pen (✎ in the title bar): the left button draws, the right one erases a stroke, ⌫ wipes
  it all, ✎ again stops. The ink is scratch only: it stays while the panel is up (through its changes too, though
  it doesn't move with what it changes), and the timer waits while the pen is on. It knows when you've drawn on a
  panel, and when you ask it to look, `look_at_panel` shows it the panel as it is on screen, ink included (a
  picture of its own window, nothing else; it's let into capture only for that moment). The ink is never saved.
- **Answer in it**: a panel can have boxes to type into and choice buttons (an `input` block). Clicking a box gives
  the panel the keyboard, and only then. Enter sends a one-line box (Ctrl+Enter a bigger one), Send sends the
  block's boxes, a choice button sends itself, Escape gives the keyboard back unsent. After sending, the keyboard
  goes back to the window you were in. What you send is your turn, as if typed in its chat window, and it answers
  out loud. If that turn needs your yes for something, it asks in the chat window when it's open, and doesn't do it
  when it isn't.
- **Scroll** a long one with the wheel; it never grows past 80% of the screen's height.
- Up to `visuals.max_panels` (3) are up at once, stacked down the right side; a new one closes the oldest.
- They're kept out of screenshots (its own too), recordings and screen shares (`visuals.hide_from_capture`), like the
  avatar. Its clicks and scrolls never land on a panel: it says one is in the way instead.

**Sounds**: a rising crystal sparkle as a panel opens, one small tink when it changes it, two soft falling glints
when it closes (by you, its timer, or it), made in the spirit of Kingdom Hearts' and Dark Cloud's menus but from
scratch, in its sound pack (`audio.sound_pack`) and volume (`audio.earcon_volume`). They follow `audio.earcons`,
and stay silent in quiet hours (`scheduler.quiet_hours`) and during a focus session or snooze.

**Motion and cost**: a see-through window is redrawn whole for every frame anything in it moves, so nothing loops.
The unfold, a block coming in or changing, a node lighting up and the window easing to a new height play once,
smoothly; a twinkle or the star's flare plays for under a second every few seconds; the time-left line steps once a
second. With one panel up the panel app measured about 3 to 10% of one CPU core (psutil, over 10 s; noisy), and
nothing once it has quit.

It needs the avatar app's Electron (`uv run octavia avatar setup`), but not the avatar itself: panels work with the
avatar off. Settings > Pop-up panels has the settings; `visuals.enabled: false` turns the feature off.

## How it uses it

`show_visual` (a tool it loads when needed) takes a title and up to eight blocks:

| kind      | what it is                                                                                     |
|-----------|------------------------------------------------------------------------------------------------|
| `text`    | Markdown: headings, **bold**, *italic*, `code`, lists, quotes, tables, code blocks, `$x^2$` and `$$...$$` math (a TeX subset: fractions, roots, scripts, Greek, the usual symbols) |
| `diagram` | boxes and arrows (`nodes`, `edges`): processes, cycles, decisions, concept maps; laid out automatically, down or across, whichever fits |
| `chart`   | bar, line or donut, up to six series                                                           |
| `facts`   | a row of big numbers with labels                                                               |
| `steps`   | numbered steps, or a timeline when they have times                                             |
| `compare` | two or three columns side by side                                                              |
| `svg`     | a drawing of its own (cleaned first; see below)                                                 |
| `scene`   | a picture of separate elements it places itself (cards, rectangles, ellipses, lines, arrows, paths, text, emoji), each with an id, so any one can be changed later; the concept map is one |
| `sheet`   | a worksheet: its picture (an svg, like an exam's tree or graph) with blanks on it (`blanks`: `number` or `text` boxes, or `cut` spots a click crosses out in red), centred at points of the picture and scaling with it; Check sends every blank at once; a `mark` edit colours blanks (right, wrong, missed, extra) and puts a tip beside each, with a message under the sheet, keeping what was typed; ink works over it |
| `input`   | boxes to type into (one line, or several with `lines`) and choice buttons (`choices`), each with an `id`; what's sent comes back as the user's turn |
| `live`    | a program of its own (three.js 3D they turn, a 2D graph with points they drag, anything that moves) with sliders, switches and choices; see [Live blocks](#live-blocks-her-programs-on-a-panel) |

The tool answers only once the panel is really on screen (or says why it isn't), with its id. Showing again with the
same id swaps the content in place. `update_visual` makes small changes to a panel that's up without resending it:
`highlight` nodes (diagram nodes take `highlight`), `step_state` (steps take `state`: todo, current, done),
`append_step`, `append_text`, `add_nodes` (with edges), `replace_block`, `append_block`, `remove_block` and
`title`, and `set_value` (fill in an input box), checked as a whole and refused with the reason when one doesn't
fit. `list_visuals` says what's up, block by
block, and while panels are up each of its turns carries a short note of them, so it updates the one it's talking
about. `close_visual` closes one or all. All SAFE: no approval, since they only draw on your screen.

**Every part is its own to change.** Each block can have an `id`, and so does everything inside the ones that hold things
(a scene's elements, a diagram's nodes, an input block's fields). Besides the blocks, a panel has `overlays`: elements
laid over the whole panel, placed at a corner or side (`place`), next to another element or block (`near` + `side`:
above, below, left, right, center), `between` two, or at a point of the panel, then nudged with `dx`/`dy`. With
`update_visual` it can `add` an element (into a scene, or over the panel), `change` any field of any element, node,
field, overlay or block (`set`, with `dx`/`dy` to nudge), `remove` any of them, and `insert_block` / `move_block`
anywhere. Things are addressed as `#2` or `#map` (a block), `2:card-a` or `map:card-a` (something in it), or a bare
id. `list_visuals` gives it every address and where each thing is on the panel (the page reports the positions after
each layout), so "put a smiley among the concept map's cards" is an overlay `between` two of its cards. Its prompt
tells it to change panels on its own whenever it helps, not only when asked.

Only what changed is redrawn (`renderer/panel/patch.js`): a block that's the same stays as it is, a diagram that
only lit a node relights it with a soft flash, steps that changed state or got new ones at the end keep theirs (new
ones rise in), a scene changes only the elements that changed (new ones pop in, gone ones fade, overlays ease to a
new place), blocks are matched by id (so inserting or moving one doesn't redraw the others), anything else comes in
with a quick fade. Entrance animations are one-shot, so nothing kept ever replays. Nothing flashes or snaps.

Other features use `ctx.extras["visuals"]` (a `octavia.visuals.host.PanelHost`): `await host.show(visual,
panel_id=..., stay_s=...)` returns a `Shown`, `host.close(id)`, `host.open_panels`, and `host.subscribe(listener)`
hears every panel that closes and why (`user`, `timer`, `replaced`, `octavia`, `app`...); `host.update(id, edits)`
(`spec.Edit`), `host.contents()` and `host.note()` do what the tools do; `host.look(id)` returns a JPEG of a panel,
`host.ink(id)` its stroke count, `host.values(id)` what was last sent from its boxes, and `host.on_input` (set by the
app) hears each send; `host.claim(id, handler)` sends a panel's sends to `handler(values)` instead (a worksheet graded by code, never a turn of its own), until that panel closes.

## How it works

- `src/octavia/visuals/spec.py` checks what a panel holds (sizes, that arrows name real boxes, that a chart has
  numbers) with messages it can act on.
- `src/octavia/visuals/host.py` starts the panel app on first use (`<electron> avatar/panels --config
  data/visuals/panels.json`), talks JSON lines over its stdin and stdout, and stops it after `visuals.idle_quit_s`
  (10 minutes) with nothing up. A crashed app is started again on the next panel.
- `avatar/panels/` is the Electron main process: one transparent, frameless, non-focusable, always-on-top window
  per panel (`panels.js`), placed down a column (`placement.js`). A panel's window is shown (`showInactive`) only
  after its page has laid out and reported its height.
- `avatar/renderer/panel/` is the page: `markdown.js` and `tex.js` turn text into plain trees, `diagram.js` lays out
  a layered graph (cycles broken, barycentre ordering), `chart.js` draws charts, `panel.js` builds it all with DOM
  text nodes (never HTML from the spec), `panel.css` is the look.

**Safety.** Nothing from a spec becomes markup: every word goes in as text. A drawing of its own is parsed and rebuilt
from an allowlist of drawing elements and attributes (`svg_clean.py`: no scripts, event handlers, links out,
embedded pages, CSS imports, or document types), then shown as an `<img>`, where nothing could run anyway. The page
is served with a CSP that allows only its own scripts and styles and no requests out, it can't navigate or open
windows, and Electron's permission requests are all refused. Links in its text are shown as words, never opened.

## Protocol (`avatar/panels`)

OCTAVIA to the app (stdin), one JSON object per line:

| message | meaning |
|---------|---------|
| `{"type":"show","id":"krebs-1","spec":{...},"stay_s":180}` | show a panel (`spec` as `Visual.to_wire()`); an `id` already up swaps its content in place. `stay_s` 0 = until closed |
| `{"type":"close","id":"krebs-1"}` | fold that panel away; `id: null` closes all |
| `{"type":"snapshot","id":"krebs-1"}` | a JPEG of the panel as it's on screen, ink included |
| `{"type":"capture","id":"krebs-1","path":"..."}` | debug: write the panel's pixels to a PNG |
| `{"type":"input","id":"krebs-1","events":[{"type":"mouseDown","x":60,"y":120}]}` | debug: mouse events played into a panel's page (the demo tries the pen with it); never the real pointer |
| `{"type":"quit"}` | close the app (it also quits when stdin closes) |

The app to OCTAVIA (stdout):

| message | meaning |
|---------|---------|
| `{"type":"ready"}` | the app can take panels |
| `{"type":"shown","id":...,"replaced":false,"closed":["old-1"],"width":440,"height":360,"notes":[...]}` | the panel is on screen (`closed`: older panels it closed to make room; `notes`: anything it left out) |
| `{"type":"closed","id":...,"by":"user"}` | a panel went: `user`, `timer`, `replaced`, `octavia`, `crashed`, `failed` (never laid out), `gone` (wasn't up) |
| `{"type":"error","id":...,"message":"..."}` | something failed (a show that failed has its `id`) |
| `{"type":"snapshot","id":...,"jpeg":"<base64>","width":476,"height":336}` | the picture asked for (or `"error"` saying why not) |
| `{"type":"input","id":...,"values":{"answer":"..."},"field":"answer"}` | the user sent what's in an input block's boxes (or clicked a choice) |
| `{"type":"ink","id":...,"strokes":3}` | how many pen strokes the user has on the panel now |
| `{"type":"layout","id":...,"boxes":{"#0":{"x":0,"y":4,"w":600,"h":300},"0:topic-a":{...}}}` | where each block, element and overlay is on the panel (its own pixels), after each layout |
| `{"type":"typing","id":...,"on":false,"restore":true,"previous":12345}` | the user started or stopped typing in a box; `previous` is the window to hand the keyboard back to (when `restore`) |
| `{"type":"captured","id":...,"path":"..."}` | a capture was written |
| `{"type":"control","id":...,"action":"next","step":2}` | a press on an explainer's player bar (`play`, `pause`, `next`, `back`, `replay`, or `step` with its number) |
| `{"type":"pointer","id":...,"rect":{"x":1200,"y":340,"w":52,"h":52}}` | where an explainer's pointer points, in physical screen pixels |
| `{"type":"live","id":...,"block":"orbit","event":"error","message":"...","line":4}` | a live block's program (by the block's id or `#N`): `ok` (ran 0.6 s without an error), `error` (`message`, `line`, `hung` when it stopped responding), `state` (what it reports), `params` (`values`: where the user left its controls) |

The config file: `hideFromCapture` (bool), `maxPanels` (1 to 6), `statePath` (where the last dragged spot and the
widths you gave narrow and wide panels are kept), `debug`.

## Live blocks: its programs on a panel

For what's best understood by turning it round or playing with it (a 3D shape or vector, a field, an orbit, a wave,
a function with a parameter, a physics setup), a `live` block holds a small JavaScript program it writes on the
spot, with up to eight controls under it: sliders (a number with min, max, step, unit), on/off switches, rows of
choices, and buttons for one-shot actions (drop, reset, splash: each press counts its value up and the program
hears it). They turn a 3D scene with the mouse and zoom with the wheel, drag the points it made draggable, and move
the controls; the program redraws as they go, without asking it anything. One block kind, not a feature per topic:
whatever it can program, it can show.

- **What the program gets** (`avatar/renderer/live/frame.js`): three.js as `THREE`, and `oct`: `oct.three()` (a
  scene, camera, renderer and mouse orbit in its look: its blues, fog, a grid, drawn a little under screen
  resolution and smoothed up like the PS2's picture), `oct.arrow`, `oct.axes`, `oct.label` (crisp page text kept over a 3D point as the scene turns, sized in
  pixels, always readable; it joins the latest scene by itself and moves with any group it's added to; taken out
  of the scene, by removing it or clearing its group, its text leaves the page at the next draw; `.dispose()` too), `oct.local`
  (a world direction such as gravity in a rotated group's own frame); `oct.plane()` for 2D
  (units to pixels, `axes`, `curve`, `path`, `point`, `vector`, `text`, and `handle`: a point they drag), and
  `oct.canvas()` for a bare one; `oct.loop` for motion; `oct.params` and `oct.onParams` for the controls;
  `oct.set` to move a control from the program; `oct.report` to tell its what's happening (it shows in
  `list_visuals`). The guide is the `code` field's description in `show_visual`'s schema (`spec.LIVE_GUIDE`).
- **It hears how it went.** `show_visual` waits (up to 3 s) for a new program to run 0.6 s without an error, and
  says "runs" or "failed: ReferenceError: ... (line 4)", so it fixes it before pointing them to it. An error later
  on (in a loop) and controls the user moved show in its per-turn panel note; `list_visuals` shows each control's
  value (and whether they set it) and what the program last reported, marked as its program's output: data.
- **Changing it by voice.** `update_visual` `change` on `block:param` with `set: {value}` moves a control: the
  program keeps running and gets the new value. `edit_code` (`find`, text that's in the program exactly once, and
  `replace`) changes the program, which starts again, with the values the user set on the controls it didn't change.
  A refused `edit_code` shows it the program as it is.
- **Undo.** Every panel keeps its last 20 versions; `update_visual` with `undo: N` goes back N changes ("undo that"),
  for any panel, not only live ones (`PanelHost.undo`). An undo is a change of its own, so `undo: 1` right after one
  puts back what it took away ("no, put it back").
- **Cost.** Nothing runs between changes unless the program loops; a loop draws at most 30 frames a second and pauses
  after two minutes with nobody touching the panel (a click goes on), since a see-through window is redrawn whole for
  every frame.

**Safety.** The program is its own, but it could be steered by something it read, so it runs where it can only draw.
It's in an `<iframe sandbox="allow-scripts">` (`renderer/panel/live.js`): an opaque origin, so it can't reach the
panel's page, its bridge to it, or any storage, and can't navigate, open windows, submit forms or download. Its page
(`renderer/live/frame.html`) is served with its own CSP (`lib/scheme.js`): scripts only from the app (plus
`'unsafe-eval'`, to run its program), `connect-src 'none'`, pictures only from `blob:`/`data:`, and no workers or
frames, so nothing can be fetched or sent. CSP doesn't cover WebRTC, so it's removed before its code runs, panel
windows allow no UDP that skips the proxy, and the panels' session has a proxy that goes nowhere (its pages need no
network: pictures are served by the app, web pages have a session of their own); the app refuses every permission. The
panel's page may frame that one page and nothing else, and passes it only the controls' values; what comes back
(ran, failed, reported, values) is checked and cut to size on the way to it. A program that stops answering for
7 s while the panel is visible (an endless loop) has its frame's process ended (that frame's, found by its name)
and the block says so; a hidden panel's throttled timers never count. One verdict (runs or failed) per run, and what
the program reports at most four times a second, reach it. Moving a block keeps its frame running (`moveBefore`);
a frame that reloads anyway is given its program again. While a panel has a live block, the main process goes by
where the pointer is (as for a web page), since the frame has the mouse over it. The frame's files are the only ones the
scheme serves with `access-control-allow-origin` (the opaque origin loads them as modules). Its subtitles don't
count a program's text as words on the panel.

Tried off screen with `node avatar/tools/panels-demo.mjs`'s way of driving the app; note that mouse events played
into an off-screen (OSR) window never reach the frame, while CDP input on a real window does.

## Explainers: panels that play as it talks

For anything that unfolds step by step (an algorithm running, a process, a proof, how something changes), it makes
an explainer: a panel that moves on as it says each part. `explain` takes an ordinary panel (a scene of its own
elements, most often) plus up to 24 steps. A step is a few of `update_visual`'s edits (add, change, remove, arrange,
highlight...), what flashes as it plays (`pulse`), what its pointer goes to (`point`), a short `caption`, and how long
it plays without a voice (`hold_s`). `setup` edits are made before it opens (laying the picture out). Every step's
whole panel is worked out and checked when it's made, so one that doesn't fit is refused before anything shows.

**In time with its voice.** It ties its sentences to the steps with beats: `{{step-id}}` at the start of the
sentence for that step. Beats are never said or shown (not in subtitles, the chat window or Discord; braces in code
stay as written). The chunker never merges a sentence that has a beat with the one before it and carries the beat on
its segment (`Spoken.beats`); as that sentence's first audio plays (`SpeechSegmentStarted.beats`), the panel goes to
that step. So the picture changes on the sentence, not before or after it. A reply without beats still moves on, a
step per sentence, until a beat comes. What it said for each step is kept as its line.

**The player.** Under the content: ↺ (play it again from the start), ◂ and ▸ (a step back or on), play/pause, a dot
per step (click to jump), the step count, and the step's caption. Play and replay say its lines again, each step
moving as its line starts (spoken like an announcement, once it's free); with no lines it plays silently, `hold_s`
per step. Pausing stops its voice. Talking over it (or the stop hotkey) pauses it where it is; a new turn's words
are never taken as its lines. It can do the same by voice (`explainer_control`: play, pause, next, back, replay,
step). Its pointer is a soft ring of its light that glides to the thing a step is about (round for round things) and
rests there; flashes are one bright breath. Nothing loops. Its orb also flies over and points at it while the
avatar runs (`visuals.explainer_orb_points`).

**The pieces it builds them from** (all general, usable on any panel):

- lines and arrows with `joins: [a, b]`: their ends sit on the outlines of the two elements and follow them when they
  move (worked out in `spec.join_ends` each time a panel changes);
- `arrange` (an edit): lays a scene's elements out as a `row`, `column`, `grid`, `circle`, or `tree` (`parents`:
  child to parent; leaves share the width, each parent over the middle of its children, a row per depth), in an
  `area` of the scene; `link: line|arrow` adds the tree's joining lines under the nodes. Elements ease to their places;
- tones on lines (key, glow, dim, warn, good), changing with an eased colour.

How: `src/octavia/visuals/explainer.py` (`Explainer`, `Director` in `ctx.extras["explainers"]`, wired in
`app._start_explainers`), `tools/extensions/explainers.py`, `avatar/renderer/panel/player.js`. A step is shown as a
`StepView`: the step's panel plus `pointer`, `pulse` and `player` for the page. The page sends `control` presses and
where its pointer is (`pointer`, turned into physical screen pixels by the main process) through `PanelHost.drive`.

## The status panel

`octavia.visuals.status` keeps one panel, id `status`, showing what runs beside it: downloads and long commands,
background and web tasks, deep thinks, its own updates being built or installed, and objectives, each with its
state, progress and how it ended. It updates itself in place (silently) from what those trackers already keep, and
never reads files or processes of its own. `visuals.status_panel`: `auto` brings it up once something has run ten
seconds (never for work that only waits, never while a screen task works, and only with a panel place to spare),
`ask` only for "what's running?" (`whats_running`), `off` never. Finished items linger a minute (a failure ten),
then it closes; closing it yourself dismisses what was on it.

## The screen history filmstrip

One panel, id `film`, for dragging through a day of screen history: docs/screen-history.md has what it does and its
privacy rules. Only code builds its `film` block (`FilmVisual`, like `PageVisual`); `show_visual` doesn't offer it. The
page keeps the whole day and changes itself (`patch.js` action `film`), so it never rebuilds and loses its place; `seek`
going up makes it glide to `at`, and new `frames` keep the playhead at its time. It says where the playhead stopped as
an input claimed by `Filmstrip` (never a turn) and its day arrows as `control` `back` and `next`. `host.own_notes` gives
a self-updating panel its own line in its note.

## Pictures and pages from the web

"Show me Sora from Kingdom Hearts" puts a picture of him on a panel instead of opening Google Images in the browser
(`show_image`, always loaded). It searches Bing's image results (no key; Wikimedia Commons when Bing fails), takes
the first results that download and check out (a few at a time, about 2 s in all), and shows one, or up to four side
by side, each with the site it's from under it; ↗ there opens that page in your browser. It gets a small copy to
check it's the right thing. `url` shows a picture by its own address instead. Panels with pictures close after
`visuals.image_stay_s` (2 minutes).

`show_page` shows one web page live on a wide panel when a quick look suits better than the browser (a recipe, an
article or figure it's talking about). You can scroll it and click its links; ↗ under it opens the page you're on
in your browser. Typing goes nowhere (the panel never takes the keyboard), so browsing, logging in and anything you
keep still go to the browser (`open_url`), as does asking for the browser or a site by name. It closes after
`visuals.page_stay_s` (10 minutes); the count waits while the pointer is on it. The tool waits for the page to load
and says when it didn't (and closes the panel) or when the site answered with an error page; it can't see the page.

**Safety.**

- Pictures are fetched by OCTAVIA, never by the panel: only from public addresses (every name is looked up, every
  redirect checked again, nothing on this PC or the home network), at most 12 MB, never a page or an SVG. Each one is
  decoded with Pillow and drawn again as a fresh JPEG (PNG when it's see-through): first frame only (nothing loops
  on a panel), no metadata, at most 1600 px a side, nothing over 60 megapixels or under 64 px decoded or kept.
  They're stored in `data/visuals/images` under names made from their bytes (a day, at most 80), and the panel app
  serves only that folder, only names of that form (`images/img-<hash>.jpg`); an image block can name nothing else.
- A page runs in its own sandboxed view (`avatar/panels/web.js`) on an in-memory session (`octavia-web`): none of
  your browser's cookies, logins or history, nothing kept after the app quits, no preload or Node, no pop-ups,
  downloads, dialogs or permissions (camera, microphone, location, notifications...). Every request it makes is
  checked: only http(s), ws(s), data: and blob:, and never an address on this PC or the home network (blocked
  pages are reported as such). Its address comes from its spec, never from the panel's page, and a navigation away
  from http(s) is stopped. Like every panel it's out of screenshots and shares. Picture titles and page titles are
  the web's words: both tools are on `READS_OUTSIDE`.
- Pictures connect only to the address that was looked up and checked (`PublicOnlyBackend`), so a name that answers
  differently the second time (DNS rebinding) gets nowhere. A page's own requests are resolved by Chromium apart from
  the guard's lookup, so for pages that gap remains; the session holds nothing of yours to send, WebRTC's UDP is off,
  and page scripts can't read answers from other sites.

Settings > Pop-up panels: `visuals.web_images`, `visuals.web_pages`, `visuals.image_stay_s`, `visuals.page_stay_s`,
`visuals.safe_search` (strict, moderate, off).

Protocol additions: blocks `image` (`images`: `src`, `alt`, `source`, `site`, `width`, `height`) and `page` (`url`,
`height`; only in a `PageVisual`); the app says `{"type":"page","id":...,"state":"loading|loaded|failed|blocked",
"url":...,"title":...,"code":200,"error":""}`. Config: `imagesDir`; checks only (with `debug`): `offscreen` (panels
and pages drawn off screen, never shown) and `webAllowLocal`. `node avatar/tools/panels-web-check.mjs --out <dir>
--image <img-....jpg>` runs pictures and pages for real, off screen, against a test server here.

## Trying it

`node avatar/tools/panels-demo.mjs --out <folder>` shows one sample of each kind (`avatar/tools/panel-samples.mjs`)
and saves a PNG of each; `--only explain,math` picks some, `--keep` leaves them up for a minute, `--live` walks one
panel through its changes and a few pen strokes instead.

## Pinning and bringing back

The pin in a panel's title bar, its chat window's list of panels (the tiles button) and "pin that" (`pin_visual`) keep a
panel up: no timer, never pushed out by a new one, left up by "close them all", and back after a restart, where it was
left. Closing it by its x, a right-click or its id lets go of it. Panels that closed can be brought back from the same
list, from the tray's "Bring back a panel" or by asking (`bring_back_visual`, which finds it by its title or what's on
it), as they were and at no cost. Pen ink and typed answers aren't kept. Panels a feature drives (worksheets, explainers,
its status panel) aren't kept either: they mean nothing without it.

Settings > Pop-up panels: `visuals.shelf`, `visuals.shelf_days`. It's all in `data/visuals/shelf.json` on this PC.

Protocol additions: `show` carries `pinned`, `pinnable` (false: no pin button) and `at` (`x`, `y`, `width`, `height`);
OCTAVIA sends `{"type":"pin","id":...,"on":true,"stay_s":0}` and `{"type":"tuck","on":true}` (pinned panels out of sight,
still up, while they'd be in the way of its clicks); the app says `{"type":"pin-asked","id":...,"on":true}` and, for a
pinned panel, `{"type":"moved","id":...,"bounds":{...}}`. `node avatar/tools/chat-demo.mjs --out <dir>` pictures its chat
window in each state, hidden, without OCTAVIA; `panels-demo.mjs` takes `--electron <exe>` for a checkout with no
node_modules.

## Never "it's on the panel" before it is

Its words go out as it writes them, and it says them before its panel call, so it could say "It's now a 3D game,
round three on the panel" while the program for it was still being written; on 2026-11-22 the user spoke over it and
no panel ever came up. `core/panel_claims.py` stands between its words and its voice: a sentence saying a panel is up,
or newly shows something (any such sentence when none of its panels is up; only one saying it changed when one is),
waits, with everything after it, until one of its panel calls in the turn has come back done, then goes out in order.
If the turn ends without one, the claim is dropped, the rest is said, and it adds that the panel didn't come up;
what's kept and shown is what it said. Offers and plans ("want me to put it on a panel?") claim nothing. While they
study at the screen with a panel up, a sentence saying how things stand now ("SA is now green") waits the same way;
it's never dropped, but with no panel call done it adds that the panel doesn't show it yet.
Both are now kinds of the one claim hold for every tool (`core/claim_hold.py`, architecture section 12).

Said once, not nagged (2026-11-23: four "the panel didn't come up" asides in ten minutes of study). With a panel up,
saying what's on it ("the question is up on the panel") is said as written unless a panel call follows it in the turn
(then it waits for that call like a change does); what it told or asked someone about panels, and quoted words, aren't
claims. The line is said once (only this line: "that didn't go through" always follows its claim): within the next eight turns, until one
of its panel calls comes back done, a claim about a missing panel is just left out, and its brain is told none of its
panels is on screen (a restart closes them all). An input block may give one question straight (`prompt`, `choices`,
`answer`) in the schema too, so a show_visual like that night's isn't refused before it reaches the panel.

A quiz question's choice field can carry its right `answer` (never shown): it must be one of the choices, so a round
can't ask for an answer its buttons don't offer. What it's told is on a panel lists each field's buttons (and
answer), so a set left over from the last round shows.

## Knowledge maps: `oct.graph`

For a knowledge map or any graph of things and links, a live program calls `oct.graph({nodes, edges, kinds, layout})`
(`avatar/renderer/live/graph.js`) instead of placing spheres and labels itself: nodes carry an `id`, `label`, `group`
(a cluster), `kind` (its shape), `state` (weak, shaky, learning, solid, new: its colour), `detail` (a line about it) and
`importance` (1 to 3: size, label size, depth); edges a `kind` (prerequisite, part of, example of...) and a `label`.
From the user's 45-point critique of it "CT3 concepts in 3D" panel (2026-11-22: scattered spheres far off in a
spinning camera, labels piled on each other), it gives every map:

- a layout in screen pixels, so each label's size is known: groups round an ellipse, each node pushed off every other
  node's box (shape and label together), edges as springs, `layers` putting prerequisites above what builds on them;
- the camera framed on the map's real projected corners, in the room left by the buttons along the top and the legend
  along the bottom (`view.fit(object, {pad, area})`, any 3D program can use it);
- labels as crisp page text on dark plates (no glow blur), and a label that would cover a more important one hidden
  until there's room (every `oct.label` gets this, by `priority`, its text size by default);
- typed, directed edges (arrowheads, a colour per kind), with the link and its words on hover;
- shape by kind and colour by state (or by kind), with a legend that says which is which;
- hover lighting a node and its links while the rest dims, a click opening an inspector (what it is, what it comes from
  and leads to, each a link to that topic), reset, zoom and focus buttons (R, +, -, F, Esc, Tab when the frame has the
  keyboard), a hint that fades after a few seconds, and nothing spinning on its own.

Live blocks lost their own frame inside the panel's, and the scanlines lie lighter over them.
