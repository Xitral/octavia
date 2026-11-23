# Avatar protocol

The avatar is a separate Electron app (`avatar/`) that OCTAVIA starts as a child process. They talk over the
child's stdin and stdout: one JSON object per line, UTF-8. There is no network port.

## Starting

```
<electron> <repo>/avatar --config <path to a JSON file>
```

The config file:

| key         | type             | meaning                                                                          |
|-------------|------------------|----------------------------------------------------------------------------------|
| `model`     | string           | absolute path of the `.vrm` file                                                 |
| `height`    | int              | window height in px (width follows from the framing)                             |
| `framing`   | `"full"`/`"upper"` | whole body, or waist up                                                        |
| `position`  | `{x, y}` or null | window position in screen px; null = bottom-right of the primary work area       |
| `statePath` | string           | a JSON file the app may write (its last position after the user dragged it)      |
| `credit`    | string           | who made the character, e.g. "my oc by amia" (a small label when the pointer rests on it) |
| `hideFromCapture` | bool       | keep the window out of screenshots, recordings and screen shares (default true)  |
| `pointHeight` | int            | its height in px while pointing (whole body); 0 = the same as `height`           |
| `trail`     | bool             | draw a rainbow streak across the screen when it flies                           |
| `props`     | object           | the props it can hold, by name (below)                                          |
| `activityProps` | `{"camera", "search", "tasks", "timer", "reminder", "web", "files", "morning", "build", "music", "shout", "rain", "sun": name or null}` | the prop held for each use (a key of `props`); null = nothing |
| `previewProp` | string or null | with `--preview`: it holds this prop (a key of `props`) the whole time          |
| `hover`     | bool             | it floats, legs tucked and still, while listening (default false)               |
| `napAfterS` | number           | idle this long on screen, it naps on a cloud; back after a break this long, it's caught napping; 0 = never (default 1800) |
| `form`      | `"character"`/`"orb"` | the VRM character, or a glowing orb drawn by the app (`renderer/orb.js`; `model` and the props go unused) |
| `orbSize`   | int              | the orb's square window in px (96 to 600; it replaces `height` and `pointHeight` for the orb) |
| `debug`     | bool             | log extra detail to stderr                                                       |

Each entry of `props` (OCTAVIA always lists the thirteen built-ins, unless a file of the same name replaced one):

```json
"props": {
  "magnifier": {"file": null, "builtin": "magnifier", "hand": "right", "size": 0.18,
                "grip": [0, 0, 0], "rotation": [0, 0, 0], "style": "toon"},
  "teacup": {"file": "C:/Users/me/octavia/models/avatar/props/teacup.glb", "builtin": null, "hand": "left",
             "size": 0.12, "grip": [0.02, -0.03, 0], "rotation": [0, 90, 0], "style": "original"}
}
```

| key        | meaning                                                                                              |
|------------|------------------------------------------------------------------------------------------------------|
| `file`     | absolute path of a `.glb` (or a `.gltf` with everything embedded), or null for a built-in            |
| `builtin`  | `"magnifier"`, `"camera"`, `"clipboard"`, `"stopwatch"`, `"bell"`, `"globe"`, `"folder"`, `"mug"`, `"wrench"`, `"headphones"`, `"sunglasses"`, `"megaphone"` or `"umbrella"`: made by the app from simple shapes; null for a file |
| `hand`     | `"right"` or `"left"`: its hand that holds it (the clipboard's, folder's and globe's are `"left"` unless it says); headphones and sunglasses are worn on its head instead, whatever it says |
| `size`     | its longest side once scaled, in metres (a built-in keeps its own size when this is the default 0.18) |
| `grip`     | `[x, y, z]` metres: where it sits, in the frame of it (normalized) hand bone                        |
| `rotation` | `[x, y, z]` degrees, in the same frame                                                               |
| `style`    | `"toon"`: its materials are shaded like it; `"original"`: kept as they are                          |
| `parts`    | only for a prop it made (the `make_avatar_prop` tool, saved in `data/avatar/made_props.json`): the simple shapes the app builds it from (below) |

A made prop has `file` and `builtin` null and a list of up to 24 `parts`, each
`{"shape": "box", "size": [x, y, z], "position": [x, y, z], "rotation": [x, y, z], "color": "#rrggbb"}`: `shape` is
`box`, `sphere`, `cylinder`, `cone`, `torus`, `capsule` or `plane`; `size` its width, height and depth in metres
(0.002 to 1; a cylinder, cone or capsule stands along y, a torus is a ring in the x-y plane with z its tube's
thickness, a plane is flat in x-y and seen from both sides); `position` its centre (metres, at most 1 either way) and
`rotation` degrees (Euler XYZ), in the prop's own axes: +Y up, +Z its front, the origin the spot in its fist. It keeps
its own size (`size` 0.18 counts as not given, like a built-in's) and is held like the rest.

The hand bone's frame is the normalized VRM one, the same for every model: at the T-pose, for the left hand +X runs
along the fingers, -Y is the palm side and +Z the thumb (forward) side; the right hand mirrors X, so the same numbers
put a prop in the same place in either hand, mirrored. With zeros the model is held like a handle, its centre in the
middle of its fist (`avatar/renderer/props.js` has the details). During `search` it holds the prop's lens up to
its eye: the part named `lens` when the model has one, else the point 0.103 m from the middle of its fist along +Z
(where the built-in magnifier's lens is). The app only loads the files named in `props`, never a path from
anywhere else, and none over 64 MB (`MAX_PROP_BYTES` in `avatar/main.js`). OCTAVIA checks everything before
writing the file (see `scan_props` in `src/octavia/avatar/bridge.py`; a compressed model is left out, since the app
has no decoders, and so is one over that limit), and the app also falls back to defaults for anything it can't use.
A prop whose file still fails to load is reported as an `error` (below), with the reason.

`--preview` (instead of, or with, `--config`) runs a demo loop without OCTAVIA: shown, idle, listening, thinking,
speaking with fake lip sync and changing emotions, confirming, a snapshot (with the camera prop), a `search`
activity, pointing at the middle of the display it's on and then at the mouse pointer while talking, flying home,
and once hiding while pointing; then around again. With `previewProp` it holds that prop the whole time, and it
stands in for the magnifying glass in the `search` activity (or for the camera in the snapshot, when it's
`activityProps.camera`). With `--config` omitted it uses `../models/avatar/octavia.vrm`.

## OCTAVIA to the avatar (stdin)

| message | meaning |
|---------|---------|
| `{"type":"show","reason":"wake"}` | appear (pop in). `reason`: `wake`, `talk`, `announce`, `always` |
| `{"type":"hide"}` | leave (pop out); the window is hidden once the animation ends |
| `{"type":"state","state":"idle"}` | `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `confirming`, `stopped` |
| `{"type":"segment","text":"...","emotion":"happy","intensity":0.8,"gesture":"wave","subtitle":"...","hold":0.4}` | a sentence starts playing now: show this emotion (and gesture) while it plays. `subtitle`: what its subtitles show for it (docs/subtitles.md), or null for none (which also takes down the last one); `hold`: only for a short phrase outside a reply, its length in seconds, since no `subtitles-end` follows it |
| `{"type":"subtitles-end","interrupted":false}` | it's done speaking: the subtitles fade after a moment to finish reading; `interrupted` (talked over, the stop hotkey): at once |
| `{"type":"subtitles-capture","path":"C:/.../s.png"}` | debug: what the subtitles window shows, to a PNG (blank while content protection is on); answered by `captured` with the window's `bounds` and its own (`her`) |
| `{"type":"mouth","level":0.6,"v":[aa,ih,ou,ee,oh]}` | lip sync, about 50 per second while audio plays; each weight 0..1, their sum at most 1 |
| `{"type":"emotion","emotion":"sad","intensity":0.5,"hold":3}` | an emotion outside speech, held for `hold` seconds |
| `{"type":"gesture","name":"nod"}` | play a gesture now |
| `{"type":"snapshot"}` | OCTAVIA is taking a screenshot: hold up a camera, press the shutter, a brief flash (only while shown) |
| `{"type":"activity","name":"search"}` | a named activity with its own pose and prop (`activityProps`), held until it ends: `search` = the magnifying glass up to its eye, peering at the screen or searching for something; `tasks` = reading a clipboard, its other hand on its hip; `web` = a globe on its palm, spun now and then; `files` = flipping through an open folder; `morning` = a mug held at its chest, sipped whenever it's quiet |
| `{"type":"activity","name":null}` | the activity is over |
| `{"type":"moment","name":"tick"}` | one go of a gesture with its prop (only while shown): `tick` (a tick drawn in the air and onto the clipboard), `timer_start` (the stopwatch clicked on), `timer_done` (the stopwatch held up, ringing), `bell` (the bell rung), `rainboom` (a burst, and a rainbow ring racing out across the screen), `wrench` (a wrench twirled), `music` (headphones on, a head bob, headphones off), `sunglasses` (sunglasses on), `umbrella` (an umbrella popped open over its shoulder), `megaphone` (a megaphone at its mouth), `snapshot` (the same as the snapshot message). A new one takes over from one still playing |
| `{"type":"mood","emotion":"happy","intensity":0.45,"seconds":60}` | a mood that carries over: its resting face (idle or listening, nothing else asking for a face) shows it for `seconds`; an unknown emotion or 0 seconds clears it |
| `{"type":"point","x":1234,"y":567,"box":[1200,540,1300,600],"label":"the apple"}` | fly over to that spot and point at it, standing clear of `box` (the thing's extent, or null); shows it if it was away; a new `point` while pointing flies it from the old spot to the new one; pointing ends any activity |
| `{"type":"unpoint"}` | stop pointing and fly home; it stays shown |
| `{"type":"point-ending","seconds":5}` | pointing ends (an `unpoint` comes) in `seconds`: the ring slowly fades and draws in until then; a new `point` starts it afresh |
| `{"type":"press"}` | the spot it's pointing at is being clicked: a quick jab at it, and the ring flashes (nothing when it isn't pointing) |
| `{"type":"glance","x":1300,"y":500}` | the user switched to a window, its middle at (`x`, `y`): it glances that way for a moment (only at home; not while it naps) |
| `{"type":"startle"}` | something made its jump: a jolt and a surprised face; napping, it wakes with a start |
| `{"type":"perch","left":300,"top":500,"right":1500,"bottom":900}` | fly over and sit on top of the window with that frame, legs over its top edge (only from home, while shown; nothing when there's no room above it for it) |
| `{"type":"unperch"}` | fly home from the window it sits on |
| `{"type":"lap"}` | a victory lap: it zips round three corners of the screen (clockwise, starting after the one nearest home) and back home, beaming, then pumps its fist; only while it's shown at home (the app ignores it while it's away, leaving or pointing) |
| `{"type":"keys","ctrl":true}` | Ctrl went down (`true`) or up (`false`), while the pointer is on it: held, it's at full strength and takes the mouse (a click, a drag); up, it fades and lets clicks through. Never held up behind a flight |
| `{"type":"prop-define","name":"sword","prop":{...}}` | a prop it made (`prop` as in `props`, with `parts`), new or changed: built at once and ready to hold, no restart; if it holds the old one, the new one takes its place. A file or built-in prop of that name stays as it is (reported as an `error`) |
| `{"type":"prop-hold","name":"sword","hand":"left"}` | hold that prop (any key of `props`, or one made since) in `hand` (`"left"`, `"right"`, or null for the prop's own) whenever no activity or moment needs its hands for its own prop; `name: null` puts it down |
| `{"type":"prop-remove","name":"sword"}` | a prop it made is thrown away (out of its hand at once, if it holds it); others are kept |
| `{"type":"working","on":true}` | only with `form: "orb"`: one of the turn's tools is running (`true`) or none is any more (`false`, also when the turn ends): the orb's working look |
| `{"type":"capture","path":"C:/.../shot.png"}` | debug: write the window's current pixels (with alpha) to a PNG |
| `{"type":"quit"}` | close the app |

Positions in `point`, `glance` and `perch` (`x`, `y`, `box` as left, top, right, bottom) are **physical pixels on
the Windows virtual desktop**, what screenshots and UI Automation use (a monitor left of or above the main one has negative ones). The
app converts them to its own (DIP) coordinates. `label` is a short name for the thing, at most 60 characters.

When OCTAVIA sends them:

- `activity` `search`: when a screenshot was taken (the brain is studying it), and when any tool that searches or
  looks something up starts (`is_search` in `src/octavia/avatar/bridge.py`): a zoom into the screenshot, a search of
  the screen's labels, Claude's `WebSearch` and `WebFetch`, `open_web_search`, `recall`, `find_files`, `open_file`,
  `get_calendar_events`, and any tool whose name has `search`, `find`, `look up`/`lookup`, `recall`, `fetch`,
  `browse` or `query` in it (any case); only while it's shown (or `mode: always`). It ends (`name: null`) when the
  next sentence starts playing, unless a search is still running (that sentence was queued before its answer came
  back), when the assistant stops thinking and speaking (the turn ends, or it asks for a yes), or after 20 s. A
  `point` ends it too, in the app, with no separate message.
- `activity` `tasks`: while any of TickTick's tools (`ticktick_...`) runs, the same way as `search` (TickTick's
  reads don't count as searches).
- `moment`: `tick` when a TickTick task, checklist item or habit check-in went through (`complete_task`,
  `complete_tasks_in_project`, `complete_checklist_item`, `upsert_habit_checkins`, also through `ticktick_more`);
  `timer_start` when `set_timer` or a TickTick focus session (`create_focus`) went through; `timer_done` and `bell`
  as a timer's ring or a TickTick reminder starts being said (an announcement with the cue `timer` or `reminder`);
  `rainboom` when an improvement is installed (or the next time it speaks, within 2 minutes, if it was away) and
  when a turn ends that got 5 or more things done (tools that aren't searches, reads or pointing) with none failing,
  at most once every 15 minutes, unless `avatar.rainboom` is off. Only while it's shown (or `mode: always`).
- `activity` `web` and `files`: like `search`, while `WebSearch`, `WebFetch` or `open_web_search` (`web`), or
  `find_files` or `open_file` (`files`) run. `morning`: as the morning rundown starts being said (the cue
  `morning`); it lasts through the talking and the turn's end, until the next turn starts, a hide, a point, or 20 s.
- `moment` `wrench` when a build of its own starts (an improvement going to `building`); `music` when
  `play_on_spotify`, or `media_control` with `play`, `play_pause`, `next` or `previous`, went through; `umbrella` or
  `sunglasses` with its next sentence after `get_weather` went through, when it says it's raining or snowing now, or
  clear in the daytime (7 AM to 7 PM); `megaphone` with a sentence that's shouted (a shouting voice cue, two or more
  words in capitals, or one long one with "!"). Only while it's shown (or `mode: always`).
- `gesture` `impatient` when a call it's waiting on (any but a search, pointing or taking a screenshot) has run
  8 s while the assistant is still thinking and it isn't showing an activity.
- `mood` when a turn ends in which the user thanked or praised it (`happy`) or teased it (`smug`), while it's
  shown: for 60 s.
- `point`: when the `point_at_screen` tool runs, and for each step of `point_sequence`, the next one only once the
  previous step's line has been heard. It keeps pointing while it talks about it.
- `point-ending`: when the turn it pointed in is over (the assistant stops thinking and speaking), while it's
  still pointing; `seconds` is `avatar.point_linger_s` (5 by default).
- `press`: as the `click` tool clicks the spot it's pointing at, just before the click goes in.
- `perch`: in `mode: always`, while a build of its own runs (an improvement `building`), the assistant is idle and it
  isn't pointing: the window in front, when it's at least 360 px wide with 220 px above it on its monitor (not
  maximised), isn't its own, and it hasn't left a window it sat on in the last minute. Unless `avatar.perch` is off.
- `unperch`: when the build is over, the assistant does anything but wait for the wake word, or that window stops
  being in front or moves. A `point` takes it off the window by itself (it flies over from it, and home after), and
  so does a `hide`.
- `glance`: when another window comes to the front, while it's shown (or `mode: always`) and not pointing: at most
  one every 1.2 s, never at its own windows, the taskbar or the desktop, unless `avatar.glance` is off. OCTAVIA
  looks at the window in front every 0.4 s while it's on screen (a few Win32 calls; no hooks).
- `startle`: at a sudden bang in the room while the assistant waits for the wake word (only the microphone's
  loudness is looked at, on the PC: a sound that comes out of the quiet all at once, loud, and has died down 0.15 s
  later), or when a big window (a third of its monitor or more) that hasn't been in front before comes to the front
  while the assistant is idle and nobody has touched the keyboard or mouse for 3 s. While it's shown (or
  `mode: always`) and not pointing, at most once every 20 s, unless `avatar.startle` is off.
- `lap` (with an `emotion` `excited`): when the user has OCTAVIA tick off the last TickTick task due today or
  overdue (`complete_task` or `complete_tasks_in_project`, also through `ticktick_more`). OCTAVIA asks TickTick
  what's due as the call starts and again once it went through (no Claude usage): something due before and nothing
  after is a cleared day. Only while it's shown (or `mode: always`) and not pointing, unless `avatar.victory_lap`
  is off.
- `prop-define`, `prop-hold`, `prop-remove`: from the `make_avatar_prop`, `hold_avatar_prop` and
  `remove_avatar_prop` tools. Each made or removed prop, and the prop it was last asked to hold, is sent again
  whenever the app is (re)started and `ready`. Unless the user asked it to keep holding it (the tools' `keep`),
  OCTAVIA sends `prop-hold` with `name: null` once the conversation is over (back to the wake word), when a later
  request calls a tool that isn't one of those (and didn't mention the prop), or after 90 s with no talk of it.
- `keys`: from a `hover` with `over: true` until one with `over: false`, OCTAVIA looks at Ctrl every 0.05 s
  (`GetAsyncKeyState`; nothing else of the keyboard) and sends it when it differs from what the app was last told.
  The app takes Ctrl as up at the start of every hover, so one held already is sent at once.
- `unpoint`: `avatar.point_linger_s` after that `point-ending` (at once when it's 0), as soon as the user wakes it
  or cuts it off for a new request, as soon as a new turn starts (a new `point` in it brings it back), when any
  tool other than `point_at_screen` and `point_sequence` is called, or 90 s after the last `point`. A `hide` also ends pointing (it flies home and leaves); no `unpoint` is
  sent for it.

Emotions: `neutral`, `happy`, `angry`, `sad`, `relaxed`, `surprised`, `excited`, `smug`, `laughing`, `curious`,
`playful`, `thinking`. Unknown ones are treated as `neutral`.

Gestures: `wave`, `nod`, `shake`, `shrug`, `laugh`, `fist_pump`, `hands_on_hips`, `surprised_jolt`, `sigh`,
`think_chin`, `spin` (a show-off turn on the spot), `impatient` (arms crossed, tapping). Unknown ones are ignored.

The app ignores messages it doesn't understand, and must never crash on one. When stdin closes (OCTAVIA is gone),
it quits.

## The avatar to OCTAVIA (stdout)

| message | meaning |
|---------|---------|
| `{"type":"ready"}` | the model is loaded and the window can be shown |
| `{"type":"hover","over":true}` | the pointer came onto its pixels (`true`; it fades and lets clicks through) or left its window (`false`, 0.3 s after) |
| `{"type":"click"}` | the user clicked the character, holding Ctrl (OCTAVIA starts listening, like the wake word) |
| `{"type":"moved","x":1200,"y":400}` | the user dragged the window here (also saved to `statePath`) |
| `{"type":"hidden"}` | the pop-out animation ended (also after a `hide` that ended pointing) |
| `{"type":"captured","path":"..."}` | a capture was written |
| `{"type":"shutter"}` | the snapshot's shutter was pressed just now (the flash): OCTAVIA plays the shutter sound on that beat |
| `{"type":"error","message":"..."}` | something failed, e.g. the model couldn't load, or a prop's file (`prop "NAME" ...`); OCTAVIA shows it as a warning |

Anything else on stdout (or stderr) is logged, not parsed.
