# Screen history ("total recall")

OCTAVIA keeps a private, searchable record of the text that was on your screen, so you can ask things like
"what was that website I had open Tuesday afternoon?" or "what did my professor's slide say about enzymes?".
It lives only on this PC, in `data/recall/screen_history.db`, and nothing is sent anywhere while it's recorded. Only
when you ask does the part that answers go to Claude, like any other tool result.

Code: `src/octavia/recall/` (capture, privacy, store, service) and the tools in
`src/octavia/tools/extensions/screen_history.py`. Settings: `screen_history:` in `config/default.yaml`.

## What it keeps

- **Text, never pictures.** It asks Windows UI Automation (what screen readers use) for the window in front: the
  visible part of its documents (a web page, a Word document, a PDF) and its visible text, links, list items, tabs
  and headers. When an app on the OCR list shows almost nothing to UI Automation, Windows' own offline OCR reads
  that window's pixels instead; the screenshot exists in memory only for that and is dropped right after.
- **Which window was in front, when.** App, window title and the page's address: scheme, host and path only. The
  `activity` table is a timeline of what you were doing, kept separate from the text so a later feature (learning by
  watching what you do) can reuse it as is.
- **Only what's new.** Each look keeps only the lines that weren't already kept for that page, so scrolling a page
  adds what comes into view and a page left open costs nothing more.

## What it never keeps

- **Browsers, for now.** Reading Vivaldi through UI Automation every 30 seconds froze it for good (2 of 9 test runs on
  2026-10-09 hung mid-read, none without the reads, and Windows logged 8 Vivaldi hangs in the two days after screen
  history arrived). So `READ_BROWSERS` in `recall/service.py` is off: browser windows aren't read and nothing of
  them is kept, not even the title. The browser rules below still apply if it's turned back on.
- **Electron apps' page text.** Discord, Slack, VS Code, Spotify and other apps drawn by Chromium (WebView2 apps
  like the new Teams and Outlook too) get a shallow read: the labels of their text elements, with no values and no
  page text through the text pattern. The full read is what froze Vivaldi (2 of 10 test runs); the shallow part alone
  never did. OCR still fills in when that finds too little, as before.
- Password fields (never read), and anything typed into a text box other than a browser's address bar.
- Private and incognito windows (by their title or the browser's private badge) and Tor Browser: no trace at all,
  not even the title. A browser window whose toolbar can't be read (full screen with F11, its own settings and
  password pages, a look cut short) can't be checked, so nothing of it is kept either, and browsers are never
  read with OCR.
- Password managers and Windows' sign-in and admin prompts (`privacy.EXCLUDED_APPS`), plus any app you exclude.
- Banking, payment and brokerage sites (`privacy.BANK_HOSTS`), windows titled as a bank, plus any site you exclude.
- Lines that look like a password, a key, a token or a one-time code; card numbers and passwords said in a
  sentence are cut out (`futures.privacy.redact`). Query strings and fragments of addresses, where sign-in tokens
  live, are dropped.
- Its own windows (the avatar).

`src/octavia/recall/privacy.py` and its tests are protected from self-improvement builds; the rest of the package and
the tools are sensitive (always shown to you before an install).

## Talking to it

- "What was that site about enzymes I had open Tuesday?" → `screen_history` (words, a time span, an app or site).
- "What was I doing yesterday afternoon?" → `screen_history` with no words lists the windows.
- "Pause screen history" / "pause it for an hour" / "resume screen history".
- "Forget the last hour", "forget everything from Discord", "never keep Signal", "delete my whole screen history".
- "How big is my screen history?" → status, including what it costs.

"Never keep Discord" (a name) covers the app and any site of that name; "never keep x.com" covers that site and its
subdomains only.

Pausing, forgetting and excluding happen right away when you ask: they only ever keep less. Deleting everything, or
more than a day at once, asks first. Any change at all asks first when the turn has read outside text (a web page,
mail, the screen, or screen history's own results could be asking) or came from your phone.

## The filmstrip

"Show me my day" or "take me back to 2 this afternoon" puts a day of it on a wide panel to drag through
(`screen_filmstrip`, `src/octavia/visuals/film.py`, the page in `avatar/renderer/panel/film.js`). A strip of frames, one
per window, runs under a fixed playhead; above it are the time, the app, the title and the words read of that window,
which scroll as you cross a long one. It's still **text, never pictures**: a frame's thumbnail is its lines drawn as
dithered bars, and nothing new is captured for it. A dithered gap on the strip means nothing was kept then, and doesn't
say why (a browser, a private window, an excluded app and being away all look the same).

- **Drag** the strip, flick it, scroll the wheel over it, or click a place on it: it follows you one to one, runs on and
  comes to rest wherever that is, with no snapping to frames. ◂ and ▸ go to the nearest other day with something kept.
- **The bar under the strip** is the whole day to scale, brighter where words were kept: drag it to get across hundreds
  of windows at once. A glance with no words (a task switcher, the Start menu) is only a thin line on the strip.
- **The wheel over the words** scrolls them, and a panel you make taller gives them the room. Short labels share a line
  so the sentences fit on screen. The wheel over the strip moves the strip.
- **Asking it** moves it the same way (a glide on the same day). It closes after 10 minutes without the pointer on it,
  or with ✕, a right-click or "close that".
- **Forgetting follows at once.** A forget, an exclusion or an old day being pruned redraws the panel from what's left
  before the request returns, and it closes when nothing's left or it can't be redrawn.
- **The rules are asked again** when it's shown: `ScreenHistory.day` runs `privacy` over what's stored, so something
  kept before you excluded it, or before a rule changed, doesn't come back on screen. A site shows as its host only.
- **Your eyes only.** It gets the playhead's time, never the words: the panel is out of its note and `list_visuals`
  counts its frames only, `look_at_panel` refuses it, and it's left out of what hides its subtitles. To read that
  moment it calls `screen_history`, which marks the turn as having read outside text. Like every panel it's kept out
  of screenshots and shares; with `visuals.hide_from_capture` off it asks first, and so it does when the turn read
  outside text or came from your phone.

A day holds at most 400 frames and about 60,000 characters of words (the page has it all, so dragging never waits on
anything); the busiest real day so far (497 windows, 364 frames) read in 9 ms and is 121 KB on the wire. Past that, the
briefest windows with no words go first, and the panel says how many.

`node avatar/tools/panels-demo.mjs --film --offscreen --out <dir>` drags the sample with real mouse events and saves a
picture of each stage.

## Keeping it light

- It checks which window is in front every 2 s (a few Win32 calls) and reads a window only after it has been in
  front for 3 s, then at most every 30 s while it stays there.
- No reading while you're away (2 minutes without a key or the mouse), while it's paused, while the PC is above
  85% CPU, or in a full-screen app that isn't a browser, Office, a PDF reader or a call: a game is only noted as
  open. OCR is used only on a short list of non-game apps, at most every 2 minutes per window.
- The reading and the database each run on one background-priority thread (`THREAD_MODE_BACKGROUND_BEGIN`: lower
  CPU, disk and memory priority than anything you run).
- Text older than `retention_days` (90) is deleted, and the oldest days go once the file is over `max_mb` (1000).
  Forgetting overwrites what's deleted (`secure_delete`) and rewrites the search index.

Measure it on your PC with `uv run python scripts/measure_screen_history.py --minutes 3` (a throwaway database;
nothing on screen is printed). The status ("how much does screen history cost?") reports the same numbers live.

## Searching

SQLite FTS5 with Porter stemming: every word you say must appear, the last one as a prefix ("enzym" finds
"enzymes"), best matches first. Local embeddings could be added later for "something like" searches; the store
keeps the text, so they could be built from it without capturing anything again.
