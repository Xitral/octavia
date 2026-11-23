# Marks on the screen

When you ask where something is on your screen or how to do something there ("where do I click?", "show me how to
add a filter"), OCTAVIA can draw on the screen itself: a ring round a button, corner brackets round a field, a
highlight over a row, an arrow coming in from the side with room, or a few words beside a spot ("your email goes
here"). A walkthrough marks each step as it says it, numbered, with the earlier steps staying on but quieter.

They're drawn in its orb's look: luminous blue lines with a soft bloom and a white-hot core, labels on near-black
glass, PS2 scanlines inside highlights. They unfold in a few quick steps, then hold still.

- **They never get in the way.** The windows they're drawn in can't be clicked (the mouse goes straight through),
  never take the keyboard, and stay on top of everything. Its own clicks go through them too.
- **They go away by themselves** after `visuals.marks_stay_s` (90 s), when it clears them, or when you press it
  **stop shortcut** (`hotkeys.stop`, Ctrl+Alt+K), which also stops its talking.
- **Every monitor, at any scaling.** It aims in physical pixels (what its screenshots and UI Automation use); the
  app turns them into each monitor's own scaled coordinates, and each mark is drawn whole on the monitor it's on,
  its arrow and label kept on that monitor. A monitor plugged in, unplugged or rescaled redraws what's up.
- **Kept out of screenshots** (its own too), recordings and shares, like its panels (`visuals.hide_from_capture`), so
  its next look sees your screen, not its marks.
- **They don't move with the screen.** If you scroll or switch windows they stay where they were; it's told so, and
  told when it's placing them from a picture more than 20 s old.
- They work with the avatar off. From your phone (the Discord bridge) it doesn't draw: you can't see it.

`visuals.marks: false` (Settings > Pop-up panels > Draw on the screen) turns it off. It needs the avatar app's
Electron (`uv run octavia avatar setup`).

## How it uses it

| tool | what it does |
|------|--------------|
| `mark_screen` | puts up to 8 marks (`circle`, `box`, `highlight`, `arrow`, `label`), each aimed like `point_at_screen`: a pixel (and box) of the latest `look_at_screen` / `zoom_screen` picture, or a `find_on_screen` match. Calling it again swaps them in place; `add: true` keeps what's up |
| `guide_on_screen` | up to 8 steps, each one mark and a line: the step is drawn, then its line is said (`ctx.extras["speak"]`), then the next; like `point_sequence`, it stops when you cut it off and says which lines you heard |
| `clear_marks` | takes them down |

All three are SAFE (they only draw on your screen). Each answers only once the marks are really showing, or says
why they aren't. With the avatar off, `point_at_screen`, `point_sequence`, `find_on_screen` and `look_at_screen`
point it to these instead.

## How it works

- `src/octavia/visuals/marks.py`: `MarksHost` (in `ctx.extras["marks"]`) starts the app on the first draw
  (`<electron> avatar/marks --config data/visuals/marks.json`), talks JSON lines over its stdin and stdout, and
  stops it after `visuals.idle_quit_s` with nothing up. `draw(marks, stay_s=...)` returns a `Drawn`, `clear()`,
  `clear_soon()` (the hotkey), `on_screen`, `subscribe()` for clears. Its process counts as its own for the screen
  tools (`avatar_pids`).
- `src/octavia/tools/extensions/screen_marks.py`: the tools.
- `avatar/marks/`: the Electron main process. `geometry.js` cleans the marks, converts them to DIPs
  (`screen.screenToDipPoint`) and gives each to the monitor it's on; `overlay.js` keeps one transparent, frameless,
  click-through (`setIgnoreMouseEvents`), non-focusable, `screen-saver`-level window per monitor (one pixel short, so
  Windows doesn't take it for a full-screen app), shown with `showInactive` only once its page has painted.
- `avatar/renderer/marks/`: the page. `layout.js` places rings, arrows (from the side facing the monitor's middle),
  labels (the first side where they fit without covering another mark) and step numbers; `marks.js` draws SVG from
  DOM nodes (every word a text node, nothing from its becomes markup); `marks.css` is the look. A mark already up
  doesn't unfold again when the marks are updated.

A transparent window is redrawn whole for every frame anything in it moves, so after the unfold the only motion is a
two-step pulse every 3.2 s on the current step's halo.

## Protocol (`avatar/marks`)

OCTAVIA to the app: `{"type":"draw","id":"m3","marks":[{"kind":"arrow","x":2400,"y":-100,"box":[l,t,r,b]|null,
"label":"Save","step":2,"done":false}],"stay_s":90}` (physical pixels; a new draw replaces what's up),
`{"type":"clear","by":"user"|"octavia"}`, `{"type":"capture","path":"...","display":0}` (debug PNG; no `display`:
the first window with marks), `{"type":"quit"}`.

The app to OCTAVIA: `ready`; `{"type":"drawn","id":...,"shown":2,"missing":[1],"dropped":0,"displays":1}` (`missing`:
marks on no monitor); `{"type":"cleared","id":...,"by":"timer"|"octavia"|"user"|"gone"}`; `{"type":"error","id":...,
"message":...}`. Errors in the app's main process are reported, never shown as a dialog.

## Trying it

`node avatar/tools/marks-demo.mjs --out <folder>` draws one of each kind in the top left of the main monitor for a
few seconds and saves a PNG of it.
