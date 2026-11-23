# Mouse and keyboard control

The assistant can click, type, press keys and scroll on your PC, so it finishes a task itself instead of telling
you where to click: "open Spotify and play the newest song in Liked Songs", "reply 'on my way' to Sam in Discord",
"turn on dark mode in Settings".

What you chose: **it clicks and types freely, but asks before anything that sends, buys, deletes, installs or
types into a password box. Moving your mouse or pressing Ctrl+Alt+K stops its instantly.**

## How it does a task

1. It finds what to click first: by its label through Windows UI Automation (`find_on_screen`, exact and cheap),
   or by looking at a screenshot (`look_at_screen`, zooming in on small things).
2. When the avatar is on, it flies over and points at it, then clicks once it's beside it. You see what it's
   about to click, and the click never lands on it.
3. It looks again whenever the next step depends on what the click did, and carries on until the task is done,
   without stopping to ask between ordinary steps.
4. When a step takes a while to show (a query running after Run, a submission being graded, a page loading), it
   waits inside your request with `wait_for_screen_change` (15 seconds unless it asks for more, up to 120). It
   compares the window with how it looked right before its last click or key press, so a result that already came
   counts at once, and it returns as soon as the change has settled. With `until_gone` ("Running", "Submitting")
   it returns once that progress label is gone from the window instead. Nothing leaves your PC while it waits.

It only uses the mouse and keyboard while you're talking with it: inside one of your requests, never in the
background or between conversations.

The tools are `click` (left, right, middle, double), `type_text` (any language, emoji, line breaks; into whatever
has the keyboard focus, so it clicks the field first), `press_keys` (one key or shortcut, like `ctrl+c`,
`alt+tab`, `page_down`, up to 20 times), `scroll` (1 to 30 notches, over a spot or wherever the pointer is) and
`drag` (press on one thing, carry it across in small steps and let go on another: sorting cards into buckets,
reordering a list, moving a slider). A drag asks first when it moves, copies or uploads files (File Explorer or the
desktop at either end), ends on a Delete or Trash target, or goes from one window into another; if you move the
mouse while it's dragging, it lets go at once.

## When it asks first

Before each click, typing or key press it reads what's really there with UI Automation: the button or field and
the ones around it, the dialog it's in, its window and its app. What the model says about it counts as well, but
can only add to the caution, never take it away. When it's risky you hear "Want me to click Send in Discord, which
sends something?".

| It asks before | For example |
|---|---|
| sending or posting | Send, Submit, Post, Share, Reply all; Enter (or Ctrl+Enter, Shift+Enter) in Discord, Slack, Teams, WhatsApp, Telegram, Signal, Outlook, Thunderbird, Mail, or a browser on Gmail, Outlook or Messenger |
| buying | Buy, Pay, Checkout, Place order, Subscribe, Upgrade, Donate, Add funds, a free trial |
| deleting or discarding | Delete, Remove, Trash, Empty, Clear history, Uninstall, Reset, Don't Save, the Delete key |
| installing, running or allowing | Install, Run, Run anyway, Allow, Accept, I agree, Trust; double-clicking a program or script (setup.exe, fix.bat); an installer's Next. A web page's own "Run" (an Athena query, a code playground) doesn't ask: it runs on that site, not on this PC; one in a Jupyter notebook does |
| moving money | Transfer, Withdraw, Sell, Trade |
| typing into a password box | any typing or pasting where Windows marks the field as a password |
| running commands | any click, typing or key in a console window (Command Prompt, PowerShell, Windows Terminal, Git Bash), the Run box, the Registry Editor or a management console; typing or keys in an app's own terminal (VS Code's) or Python's IDLE shell; Shift+Enter or Ctrl+Enter in a Jupyter notebook; Win+R, Win+X; Enter in the Start menu's search or File Explorer's address bar |
| closing apps | Alt+F4, Ctrl+Q, an app's own close button, Exit, Quit, End task |
| restarting the PC | Restart or Shut down in Windows' own menus |

A generic button (OK, Yes, Continue, Next, Apply) means whatever its dialog says: "OK" under "Are you sure you want
to permanently delete this file?" asks, "OK" under "File saved" doesn't. A word right after "don't" or "cancel"
doesn't count: "Don't send" is the safe choice.

Once you say yes, the same kind of thing goes ahead when it follows straight on (the next message of several you
asked it to send, say). As soon as it does anything else in between, like looking at the screen again, the next
one is asked again, so text on the screen can't ride your yes. Commands are the exception: every single input into
a console or the Run box asks, with the exact text it'd type, so one yes is never a free shell. So is anything in
an app too busy to be checked: it asks rather than guessing it's harmless.

Submitting and sending are the one kind your own words can approve ahead of time: asking for it ("fill it in and
submit it", "send it to him"), asking for work that's done by submitting its answers ("do this problem set"), or
saying it needn't ask ("you don't need authorization to submit these, just keep clicking through"). Then it
submits without the question, and it stands for the rest of the conversation in that window (later "keep going"s
included) until you take it back ("ask me before you submit") or the conversation ends; another window is asked. "Don't submit it", "ask me before you send anything" and "let me
check it first" keep the question, and so does anything after it has read a web page in that request. Buying,
deleting, installing, passwords and commands are always asked, whatever you said (`src/octavia/tools/asked.py`).

High-risk places (`permissions.always_ask_before_sending`: your email by default, plus Canvas in your settings)
are the opposite: submitting or sending there is asked every single time. No "you don't need to ask", standing
approval, `submit_without_asking` site or earlier yes skips that question. Deleting files in File Explorer is always
asked too.

Apps you trust it with (`permissions.trusted_apps` in `config/local.yaml`, e.g. `[PowerPoint]`) are the one
exception for deleting: removing a shape, a slide or a cell inside your document there goes ahead without the
question, since Ctrl+Z takes it back. That app's dialogs ("Don't Save", "Delete this file?") still ask, and so does
everything else risky in it: sending, buying, installing, commands, passwords and closing it.

Answering its question works like talking to a person: "sure, go for it", "mhm", "yeah why not" are yeses, "nah",
"not now", "leave it" are nos. Anything less plain is read for its meaning by its quick voice (Haiku), but a judged
yes never counts when the answer has a "don't", "not", "wait" or "but" in it: then it asks again. Asking about it
instead ("wait, who's it going to?", "what's in it?") gets an answer from the details of what it's about to do,
then "So, want me to ...?" (up to three questions). And when your own words just asked for exactly that action
("close Spotify"), it doesn't ask again at all, except for deleting, email, Canvas, the places in
`permissions.always_ask_before_sending`, and every risky kind of mouse or keyboard action (`src/octavia/core/consent.py`).

Right before clicking it checks the spot again. If it isn't what it asked you about (the screen changed while
you answered, or pointing at it brought up a different button, like a row's delete icon), it doesn't click and
looks again. Typing and keys check the keyboard focus the same way, and typing stops if the focus moves while it
types (a page moving it to a button, where a typed space would press it). Browsers answer one spot differently
from one look to the next, so some second looks still count as the same spot, in the same window and app: a bare
pane, picture or the window itself with nothing to click in it (Vivaldi re-rendering a tab or a dialog's Close),
or the option, tab or button the click was for, by its label ("gluelab" in a drop-down that just opened, "Refresh:
Events" for "refresh events"). What's there then still gets its own risk check, and so does what the pointer brings
up; any other named button, link, option or tab there stops the click.

When an app tells UI Automation nothing (games, drawings, some video players), it can still click it, but only its
own judgement of what it's clicking decides whether it asks.

When an app has stopped responding, it doesn't read it through UI Automation at all (a read into a hung app can
block for good), so what's there can't be checked and it asks first. A read that gets stuck anyway is left behind
on its own thread, so its later checks and searches still run.

## Taking over

Move the mouse, type, click or scroll, or press **Ctrl+Alt+K**, and it stops at once, even in the middle of typing
a long text. It won't touch the mouse or keyboard again for the rest of that request unless you ask it to.
Answering one of its questions (typing "y" in the chat, say) doesn't count as taking over.

It notices without an input hook: before each step it compares where the pointer is with where it left it,
and when Windows last saw input with when it last gave it some. A nudge of a few pixels is allowed; holding a key
or a mouse button counts as you taking over. Until it has moved, clicked or typed anything in a request, your mouse
is still yours: using it between its calls then (while it looks again after a click it refused) isn't taking
over, though moving it during a call (while the avatar points) still is.

## What it can't do

- Windows keeps apps running as administrator, UAC prompts, the lock screen and Ctrl+Alt+Delete out of reach of
  any normal app's input. Clicks and typing there don't arrive.
- It won't press the assistant's own hotkeys (Ctrl+Alt+J, Ctrl+Alt+K), which would interrupt it.
- It types text only into a text field: with the focus on a button, a menu or a web page itself, stray letters
  could press or trigger something, so it clicks the field first.
- Its checks read what apps say about themselves. A web page can label a button misleadingly, so it also asks when
  it itself judges a click risky, and the word lists can't cover every app. Keep an eye on tasks that matter.

## Turning it off

In `config/local.yaml`:

```yaml
tools:
  disabled: ["click", "type_text", "press_keys", "scroll", "drag"]   # no mouse and keyboard at all
permissions:
  tools:
    click: confirm        # ask before every click, risky or not
```

Setting one to `allow` changes nothing: a per-tool `allow` covers a tool's everyday calls, and the actions it
spots as risky are still asked. Only `permissions.confirm: allow`, which turns off every question the assistant
asks, would let those through too.

## For developers

- `src/octavia/tools/extensions/computer_control.py`: the four tools, the table of risky words (`RISKS`) and the
  rules (`classify_click`, `classify_typing`, `classify_keys`). `inspect_risk` records what it saw on the call's
  arguments, so `run` can refuse when the screen changed and `describe_call` can name the target.
- `src/octavia/tools/extensions/_input.py`: SendInput (absolute moves across the virtual desktop in a per-monitor
  DPI aware context, Unicode typing, key names) and the takeover watch (`Watch`), shared by all four tools.
- `src/octavia/tools/extensions/_uia.py`: `inspect_point` and `inspect_focus` describe the target (`Target`), on
  the UI Automation thread, by the window alone when an app doesn't answer in time; `focus_now` says what has the
  focus between typed chunks.
- `src/octavia/tools/extensions/screen_wait.py`: `wait_for_screen_change`. Clicks, typing and keys take a tiny copy
  of the window right before their input (`BEFORE`, an `InputSnapshots`; scrolling drops it), which the wait uses
  once as its baseline. Tests: `tests/test_screen_wait.py`, `tests/test_smarter_screen_wait.py`.
- The four tools act one at a time (a lock per event loop), so calls the model makes together never mix their
  moves and keys.
- Tests: `tests/test_computer_control.py`, `tests/test_input.py`, `tests/test_uia.py`.
