# The desktop avatar

A 3D character that lives on your desktop and shows what the assistant is doing. It pops in when you talk to
it, listens, thinks, and talks with moving lips and changing expressions, then leaves once the conversation is
over.

## Setting it up

1. Install the avatar app once. It needs [Node.js](https://nodejs.org) (LTS), and downloads Electron and three.js,
   about 200 MB:

   ```powershell
   uv run octavia avatar setup
   ```

2. Put your character, a `.vrm` file, at `models/avatar/octavia.vrm`, or point `avatar.model` at it. `models/`
   isn't committed to git, so the file stays private.

3. Have a look without the assistant (it loops through moods and gestures; Ctrl+C closes it):

   ```powershell
   uv run octavia avatar preview
   ```

4. Turn it on in `config/local.yaml`, then start the assistant as usual:

   ```yaml
   avatar:
     enabled: true
   ```

## The orb

Instead of the character it can be a glowing orb in a PS2-era style: a white-hot heart inside a grainy, see-through
blue sphere, wisps of light on tilted orbits round it, motes of light and scanlines. It needs no `.vrm` file, and it's
drawn by a single shader on the graphics card at about 60 frames a second, so it's light on the CPU (no frames at all
while it's away). Its window always stays wholly on screen, wherever the character was last put.

```yaml
avatar:
  enabled: true
  form: orb        # "character" (the default) or "orb"
  orb_size: 320    # its square window in pixels, 96 to 600
```

| The assistant is... | The orb... |
|---|---|
| waiting | breathes gently; its inner light leans toward your mouse pointer |
| listening | brightens, with a halo ring breathing round it |
| thinking | swirls and shimmers, tinted toward cyan |
| running a tool, taking a screenshot or searching | a distinct working look: three satellites and a dashed arc circling it, tinted toward violet |
| speaking | pulses with the voice, and a ripple races out with each syllable |
| showing you something on screen | flies over (leaving a blue light trail), turns into a small comet-like pointer and rings the thing with light; it turns back into an orb as it leaves |

Every change of look is eased, so it never jumps. Like the character it's click-through: resting the pointer on it
fades it and lets clicks through, Ctrl brings it back to click or drag, and its pointer form never takes the mouse.
The character's props, gestures and naps don't apply to the orb. Another form (a mascot) can be added later the same
way: a renderer module picked by `avatar.form` (`avatar/renderer/orb.js` is the orb's).

## Using it

- **It comes when you talk to it** (the wake word, the talk hotkey, Ctrl-clicking it, or an announcement), and
  leaves as soon as the conversation is over and the assistant is back to waiting for the wake word (otherwise,
  after `hide_after_s` seconds idle). With `mode: always` it stays on screen.
- **Rest the pointer on it** and it fades to see-through, and clicks and scrolls go through it to whatever is
  underneath. It stays see-through while the pointer moves about anywhere over its window, and is back a
  moment after it leaves.
- **Hold Ctrl** with the pointer on it to bring it back and take the mouse: then **click it** to start talking,
  like saying the wake word (`click_to_talk`), or **drag it** anywhere; it remembers where. While the pointer is
  on it the assistant looks at whether Ctrl is down (twenty times a second, only then, on your PC).
- Everywhere else the window lets clicks through, so it never gets in the way, and appearing never takes the
  keyboard away from what you're doing.

What it shows:

| The assistant is... | It... |
|---|---|
| waiting | idles: breathes, blinks, shifts its weight, follows your mouse pointer with its eyes |
| listening | leans in and looks at you |
| thinking | puts a hand to its chin and looks up |
| taking a screenshot | holds up a camera and presses the shutter (you hear it too) |
| searching: studying your screen, the web, your files, its memory or your calendar | peers through a magnifying glass |
| showing you something on screen | flies over to it and points at it |
| speaking | moves its lips with the voice and gestures as it talks |
| asking you to confirm | tilts its head, palm up |

Each sentence brings its own mood the moment you hear it: from the voice's cues (`[excited]`, `[laughing]`,
`[sigh]`...) and from what's said (a greeting, an apology, a question, "wow"...). A failed tool or an error shows
briefly on its face; an update ready to install makes its excited.

## Pointing at things on your screen

Ask where something is, and it shows you:

- **"OCTAVIA, find the apple on my screen."** It takes a screenshot (shutter sound, camera in hand), studies it
  through its magnifying glass, zooming in when the thing is small or among look-alikes, then flies across the
  screen as a rainbow streak, pops in beside it (full body, on the side with more room, never covering it) and
  points at it with its finger while a glowing ring marks the spot.
- **"Point at the save button."** Buttons, links, menus and other labelled controls are found through Windows UI
  Automation instead: no screenshot is taken, only the labels that match (and the window each is in) go to
  Claude, and it points at the control's exact outline. Pictures, games, videos and canvases have no such labels,
  so for those it takes a screenshot after all.
- **"Show me how to save this as a PDF."** When there are several things to show, it goes through them in order
  (`point_sequence`, up to eight): it points at the first and says its line, stays there until you've heard it,
  then flies to the next, and stays on the last one. Each line goes with its own spot, and it doesn't say them
  all again afterwards. Talking over it (or the stop hotkey) ends the walk-through where it is.

It keeps pointing while it talks about it, and for a few seconds after its reply is over (`point_linger_s`),
while the ring slowly fades and draws in; then it flies home. Waking it or talking over it for something new
sends its home at once, and when the conversation ends it leaves as usual. Asking about something else on the
screen just sends its flying from one spot to the next. It never keeps pointing for more than a minute and a half.

When you ask it yourself to look at your screen, find or point at something, your words are the go-ahead; if it
wants to look on its own initiative, it asks first. Once it has searched or read the web in the same turn, it
asks anyway, since a web page could have put it up to it.

Settings (in `config/local.yaml`, under `avatar:`):

| setting | default | meaning |
|---|---|---|
| `point` | `true` | it can fly over to things on your screen and point at them; off = it describes where they are |
| `point_height` | `0` | its height in pixels while pointing (it's shown whole there); 0 = the same as `height` |
| `point_linger_s` | `5` | seconds it keeps pointing once its reply is over (0 = flies home right away) |
| `trail` | `true` | the rainbow streak across the screen when it flies |

## Props

It holds things for some of what it does:

| activity | prop (default) | setting |
|---|---|---|
| taking a screenshot | `camera` | `avatar.props.camera` |
| searching or looking something up | `magnifier` (a magnifying glass) | `avatar.props.search` |
| working with your TickTick tasks, and ticking one off | `clipboard` | `avatar.props.tasks` |
| setting a timer or a focus session, and a timer going off | `stopwatch` | `avatar.props.timer` |
| reminding you of a task | `bell` (a hand bell) | `avatar.props.reminder` |
| searching or reading the web | `globe` | `avatar.props.web` |
| looking through your files | `folder` | `avatar.props.files` |
| the morning rundown | `mug` (hot chocolate) | `avatar.props.morning` |
| starting to build an improvement of its own | `wrench` | `avatar.props.build` |
| starting your music | `headphones` (worn) | `avatar.props.music` |
| shouting | `megaphone` | `avatar.props.shout` |
| telling you it's raining or snowing | `umbrella` | `avatar.props.rain` |
| telling you it's sunny | `sunglasses` (worn) | `avatar.props.sun` |

- **The clipboard** comes up (in its left hand) while any of TickTick's tools runs, and it reads down the list with
  its other hand on its hip. When a task, a checklist item or a habit check-in is done, it draws a tick in the air
  and a tick draws itself into the next box on the clipboard.
- **The stopwatch**: when it sets a timer (or starts a TickTick focus session) it holds it up by its shoulder and
  clicks it on, and its hand starts sweeping. When a timer goes off it holds it up high, ringing and shaking.
- **The bell**: it rings it as it says a TickTick reminder ("Heads up: ...").
- **The globe** sits on its palm and turns while it searches or reads the web; its other hand spins it now and
  then. **The folder** it holds open like the clipboard, flipping through its pages, while it looks through your
  files.
- **The mug** comes out for the morning rundown and stays for a little after, so it can sip it whenever it's
  quiet; it's put away when you ask it something new.
- **The wrench**: when a build of its own starts while it's on screen, it twirls it like a baton.
- **Headphones**: when it starts your music (Spotify, or resuming or skipping a track), it slides them on (sleek
  black studio headphones with gunmetal yokes and a thin glowing sky-blue ring on each cup, sized to sit over its
  ears), grooves to the beat for a few seconds with a cool, relaxed look (chin up, a small nod, a slow sway, music
  notes drifting up beside its head), and takes them off.
- **The umbrella** and **the sunglasses**: when it tells you the weather, it's the umbrella if it's raining or
  snowing right now, and the sunglasses if the sky's clear in the daytime. They come up as it says it.
- **The megaphone**: for a shouted sentence (a shouting voice cue, or words in capitals like "LET'S GO!").

It holds the magnifying glass up whenever it searches: from the moment a screenshot is taken until it has
studied it, and while it zooms into it or looks through your screen's buttons and labels, searches its memory, or
checks your calendar (the web has its globe and your files their folder). Any tool added later whose name says
it searches (search, find, look up, recall, fetch, browse or query) counts too. It puts it down when it starts
telling you what it found (a "let me look that up" said while the search still runs doesn't count), when it
asks you to confirm something or is done with your request, when it points at something, or after 20 seconds at
most.

All thirteen come built in, made from simple shapes. The headphones and sunglasses it wears on its head rather
than holding (a model of yours replacing either is worn too: centred between its eyes, facing forward). You can
give it your own 3D models instead, or as well:

1. Put the model in `models/avatar/props/` (change the folder with `avatar.props_dir`). Git ignores `models/`, so
   what you put there stays on your PC; mind each model's license all the same.
   - **A `.glb` file** (everything in one file) is best. A `.gltf` works only when everything is embedded in it:
     one with a `.bin` or textures beside it won't load. Sites like Sketchfab offer both downloads: take the GLB.
   - It mustn't be compressed. Draco or meshopt meshes and KTX2 (Basis) textures, which some exporters, tools like
     gltfpack and download sites use to make files smaller, won't load: export or download it without
     compression. `octavia avatar props` (next step) names a compressed file and leaves it out.
   - It can be at most 64 MB (67,108,864 bytes): the app won't load a bigger file. `octavia avatar props` names a
     bigger one and leaves it out. Large textures are what usually makes a model that big.
   - The file name is the prop's name: lowercase, with spaces and other symbols turned into `-`, so
     `Brass Spyglass.glb` is `brass-spyglass`.
   - A file named like a built-in (`magnifier.glb`, `camera.glb`, `clipboard.glb`, `stopwatch.glb`, `bell.glb`,
     `globe.glb`, `folder.glb`, `mug.glb`, `wrench.glb`, `headphones.glb`, `sunglasses.glb`, `megaphone.glb`,
     `umbrella.glb`) replaces it everywhere. (Its moving parts, like the stopwatch's hand, are the built-in's own: a model of yours
     just stays as it is.)
2. Check it found it (and anything wrong with it):

   ```powershell
   uv run octavia avatar props
   ```

   This lists every prop, where it comes from (its file, or built-in), the hand that holds it, its size, and which
   activity uses it.
3. Tune how it holds it: put a settings file beside it with the same file name, ending in `.json` instead
   (`Brass Spyglass.json` beside `Brass Spyglass.glb`). All keys are optional:

   ```json
   {
     "hand": "right",
     "size": 0.22,
     "grip": [0.03, -0.02, 0.0],
     "rotation": [0, 0, 90],
     "style": "toon"
   }
   ```

   | key | default | meaning |
   |---|---|---|
   | `hand` | `"right"` (`"left"` for the clipboard, folder and globe) | `"right"` or `"left"`: its hand that holds it (worn ones ignore it) |
   | `size` | `0.18` | its longest side in metres, 0.02 to 2 (the model is scaled to it); the built-ins keep their own size unless you give one |
   | `grip` | `[0, 0, 0]` | where it sits in its hand, `[x, y, z]` in metres (the directions are below) |
   | `rotation` | `[0, 0, 0]` | how it's turned, in degrees around those same x, y and z |
   | `style` | `"toon"` | `"toon"` shades it like it (flat, cartoon lighting); `"original"` keeps the model's own look |

   With everything at zero it holds it like a handle: its middle sits in the middle of its closed fist, its top
   sticks out past its thumb, and its front faces where its fingers point. Then `grip` slides it so the right
   part (a handle, say) is in its fist, and `rotation` tilts it about its fist. The directions are its hand's
   own, whatever pose it's in: x along its fingers toward the fingertips, y out of the back of its hand
   (negative y is into the palm), z toward its thumb. The same numbers work in either hand (mirrored), so
   switching `hand` needs no other change. A value that's wrong (a size of `18`, meant as centimetres, say) is
   named in a warning and its default used instead; the rest still count. A settings file named like a built-in
   (`magnifier.json`, `clipboard.json`...) on its own tunes the built-in.

   When it looks through the prop it uses for `search`, it holds up to its eye the spot about 10 cm from the
   middle of its fist toward its thumb (+z), so set `grip` and `rotation` to put the middle of the lens there. If
   your model's lens part is named "lens" (or "Lens.001" and the like), it finds it itself.

   Watch the result while you edit: this runs the preview with its holding the prop the whole time (close it with
   Ctrl+C, edit the file, run it again):

   ```powershell
   uv run octavia avatar preview --prop brass-spyglass
   ```

   The preview still shows its taking a screenshot and looking closely at the screen, with your prop standing in
   for the magnifying glass, so you can check it peers through its lens. (The prop it uses for screenshots
   stands in for the camera instead.)
4. Use it for an activity in `config/local.yaml` (or `""` for nothing in its hands):

   ```yaml
   avatar:
     props:
       search: brass-spyglass
   ```

The assistant reads the props folder when it starts, so restart it after adding or changing a prop. If the app
still can't load one (its data is damaged, say), the console shows a warning saying why (`Avatar: prop "..."`).

## Little things that bring it to life

None of this uses any Claude usage: it all runs off what the assistant is already doing, or in the avatar app by
itself.

- **Floating** (off unless you turn it on with `avatar.hover: true`): while you talk to its it floats a little
  off its spot, legs tucked, and holds still there.
- **Napping**: in `always` mode, left idle for `avatar.nap_after_min` minutes (30 by default) it dozes off on a
  little cloud, eyes shut, z's floating up. Anything happening wakes it with a jolt and the cloud pops. In
  `conversation` mode it's gone while idle, so instead, when you call it after a break that long, it's caught
  napping: it comes up asleep on its cloud and wakes up a moment later. `0` turns napping off.
- **The sonic rainboom**: when something big is done, it bursts into a "YES!" pose and a rainbow ring races out
  from its across the screen (in the same see-through, click-through windows as its flying streak, so it never
  gets in your way). That's when an improvement to itself is installed, or when a request took its 5 or more
  things to do (clicks, keys, tasks changed...) and none of them failed. At most once every 15 minutes, so it
  stays special. `avatar.rainboom: false` turns it off; with `avatar.trail: false` it stays inside its own window.
- **The victory lap**: have its tick off the last thing due today in TickTick and it does a lap of your
  screen, zipping from corner to corner as a rainbow streak, beaming, then pumps its fist back home. Tasks you tick
  off in the TickTick app itself don't count (it doesn't see those). `avatar.victory_lap: false` turns it off.
- **Pressing the button**: when it's pointing at what the assistant clicks, it jabs at it just as the click goes
  in, and the ring flashes.
- **Glancing at your window**: switch to another window and it glances over at it for a moment. The assistant
  looks at which window is in front a couple of times a second while it's on screen; that never leaves your PC.
  `avatar.glance: false` turns it off.
- **Jumping**: a sudden bang in the room (a door slamming) makes its jump, as does a big window popping up by itself
  (with nobody at the keyboard or mouse); if it was napping, it wakes it with a start. For the bang, only how loud
  the microphone is gets looked at, on your PC, and only while the assistant waits for the wake word. At most every
  20 seconds; `avatar.startle: false` turns it off.
- **Sitting on your window**: with `mode: always`, while it's building an improvement to itself, it flies over
  and sits on top of the window you're working in, legs over its edge, swinging its feet, as long as it has room
  above it (not maximised). It flies home when the build is done, when you talk to it, or when you switch windows
  or move that one. `avatar.perch: false` turns it off.
- **Showing off**: a boastful line ("Nailed it!", "Called it.") gets a spin on the spot, hair flying.
- **Impatience**: when something it's waiting on takes more than about 8 seconds (an app opening, a slow tool)
  and it has nothing in its hands to show for it, it crosses its arms and taps its foot.
- **Fidgets**: while idle it now and then tucks its hair behind its ear, checks its nails or rolls its shoulders,
  besides looking around and stretching.
- **Sleepy at night, perky in the morning**: late at night its eyelids hang lower, it blinks and breathes slower
  and yawns; in the morning its resting face is brighter and it fidgets a little more. (By your PC's clock.)
- **Moods that last**: thank or praise it and it stays a little happy for a minute after; tease it ("took you
  long enough") and it stays smug.
- **Peeking in**: now and then, when you call it, it peeks in from the side of its window instead of rising.
- **Dust puffs**: a little puff where it zips off, and where it brakes when it arrives.

`uv run octavia avatar preview` shows each of these props in turn (one a round), and `node avatar/tools/drive.mjs
moments` / `nap` / `life` capture them for checking by eye.

## How it works

The character is drawn by a small Electron app in `avatar/` (three.js and
[three-vrm](https://github.com/pixiv/three-vrm)). The assistant starts it and talks to it over its stdin and stdout
(docs/avatar-protocol.md): no network port. It's restarted if it crashes; after a few crashes it stays off until
the next start, and the assistant works the same without it.

- **Lip sync** comes from the audio as it's handed to the speaker: a worker thread measures loudness and the
  voice's formants every 20 ms (`src/octavia/avatar/lipsync.py`) and sends the vowel shapes (A, I, U, E, O).
- **Moods** come from each sentence as it starts playing (`src/octavia/avatar/emotion.py`).
- **Animations** are generated in the app (breathing, blinking, gaze, poses, gestures), so any VRM with the
  standard expressions works: VRoid Studio models do.
- **Pointing**: OCTAVIA keeps its own full-resolution copy of each screenshot only in memory, for five minutes, so
  a position in the picture Claude saw, or in a zoom into it, maps back to the exact spot on your screen. The
  `point_at_screen` tool sends that spot to the app, which moves its window beside it, draws the streak in a
  see-through, click-through window over each monitor, and aims its arm so its finger's line runs through the
  spot. Its arm is aimed every frame, so it stays on target while it breathes and talks. `point_sequence` works
  out every spot first (so a bad one fails before it moves), then sends them one at a time, each followed by its
  line said inside the running reply; the next spot goes only once that line has been heard. Without a voice it
  still points at each, pausing about as long as its line would take, and gives the lines in its reply instead.
- **What Claude gets, and where it's kept**: the scaled-down screenshot and any zoom crops are sent to Claude, and
  `find_on_screen` sends the labels that match (no picture). Like the rest of the conversation, they're kept in
  the brain's Claude Code transcripts, in `data/claude-home/projects` (the `projects` folder of
  `brain.claude.config_dir`). Claude Code deletes old transcripts after about 30 days. To remove them sooner,
  delete that `projects` folder while the assistant is stopped; the brain's history of those conversations goes
  with it. Leave the rest of `data/claude-home` alone: it holds the assistant's Claude login. The assistant's own
  log, `data/logs/octavia.log` and its older copies `octavia.log.1` to `octavia.log.5`, also keeps a line for each
  tool call (logger `octavia.audit`) with what it was asked, such as what was searched for, and the start of the
  answer. For `find_on_screen` that includes the first match's label and its window title, for
  `point_at_screen` the label it pointed at, and for `point_sequence` the labels and the lines it said.
  `octavia chat` keeps the same lines in `octavia-chat.log*`, and when it starts in the background at login, what
  the console would show goes to `octavia.stdio.log`. To remove all of it, delete everything in `data/logs` with the
  assistant stopped (Windows won't delete a file while it's open).

## Your character's license

VRM files carry their creator's terms. The default character, "my oc" by amia, is used with the creator's
permission for personal, non-profit use, with credit (a small label when you rest the pointer on it, `avatar.credit`), and may not
be modified or shared: keep it out of git (it is, in `models/`) and don't give the file to anyone.
