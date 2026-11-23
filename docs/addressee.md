# Was that for it? (addressee check, shadow mode)

The first step toward talking to its without the wake word. For now it only writes down what it would have done. It
never answers on it, and the wake word works exactly as before.

One thing does change live, on calls. While you're on a call in another app (Discord, Zoom, Teams, a browser...), it
opens no follow-up window after its replies: what you say next is most likely for the people on the call, so it waits
for its name every time (answering a question it asked at the end of a reply too: "OCTAVIA, yes"). Talking over its
doesn't cut it off either; its name or the hotkey still do. A turn it gets during a call tells its it's a call, so it doesn't save what may have been
said to someone else as a fact about you. The same watch of which apps hold the microphone feeds the shadow below.
`conversation.name_on_calls: false` turns it off.

The same goes while another app plays sound (a video, music, a game: any app whose peak meter moved in the last 6
seconds). The headset mic picks up what's playing even through headphones: on 2026-10-29 a YouTube video fired the
wake detector, and then each line it said in the follow-up window was answered as yours. So while something plays,
there's no follow-up window without its name (an answer to a question it just asked still counts), and a wake
detector hit wakes it only once its second listen hears its name at the start of what was said ("OCTAVIA, pause the
video" still runs in one breath, about 0.2 s later). Without the second listen, the detector wakes it as before.
`conversation.name_while_playing: false` turns it off.

## What happens

While it waits for its name, its second listen (`wake/second_chance.py`) already transcribes each stretch of speech
it hears, to catch a wake word the detector missed. When a stretch doesn't start with its name, it goes to the
addressee check (`octavia.addressee`) instead of being dropped. So there's no extra transcription and nothing added to
a turn: it only runs while it's idle, never while it speaks, listens or works.

The local model (`local_brain`, the same llama-server and Qwen model as the turn shadow) is asked whether it was meant
for it. It's told:

- the words
- how long ago it last spoke, and what was said then if it was in the last 5 minutes
- the app in front and its window title (from Win32, never UI Automation, so browsers aren't read)
- call apps holding a microphone (Discord, Teams, Zoom, Steam, a browser...), from Windows' audio sessions
- apps that played sound in the last 6 seconds (peak meters; Sonar, Voicemod and audiodg are ignored)

Its answer is begun for the model (`{"for_her":`), so its first token is the verdict. The score is the model's odds
of "true" over "false" at that token. It'd have answered at `addressee.threshold` (0.5) or more.

Measured on this PC (2026-10-10, 7900 XT): the verdict comes about 0.09 s after asking, and the reason about
0.3 s after. On 25 made-up lines it got 23 right. Both misses were cautious: it stayed quiet on something meant for
it ("what are you doing tonight" with no call going, "how do I fix a null pointer in python" in VS Code).

## The model's slots

llama-server now runs with two slots (`-np 2`) sharing one KV cache (`-kvu`): the turn shadow uses slot 0, this uses
slot 1, so each keeps its prompt cached. `--no-cache-idle-slots` matters. Without it the server moves the idle slot
to RAM when the other gets work, and this model (a hybrid with recurrent layers) then re-reads the whole 40k-token
shadow prompt (15.7 s) instead of 15 new tokens (0.16 s). It uses no more GPU memory than before.

## What's kept

`data/addressee.jsonl`, one line per thing overheard, written about 50 s after it was heard (`repeat_window_s`, 30 s, plus 20 s for the request after a wake word to be heard):

- always: time, an id, word count, length, the score and verdict, how long the verdict took, seconds since it
  spoke, the app in front by name (never its title), call apps and apps playing sound
- the words and the model's reason: only when it'd have answered, or when you said it again with its name within
  `repeat_window_s` (`repeated`, a miss when it wouldn't have answered)
  ("Said again" means the whole thing, close in length and word for word: "yeah" before "yeah, set a timer" isn't
  it. The window title is taken out of the reason.)
- `skipped` lines (a game in front, the model not loaded, too many at once, an error): counts only

Lines older than 30 days go at start. `octavia addressee forget` deletes the file. The main log (`data/logs`) gets only a
line's id and score, never its words.

## The report

```
uv run octavia addressee            # how often she'd have answered, by situation, misses, your marks
uv run octavia addressee show       # ...and each line she'd have answered, with its id
uv run octavia addressee mark ID no # that one wasn't for her (or yes: it was)
uv run octavia addressee forget     # delete everything it kept
```

`--days N` limits it to the last N days (default 7). The false-trigger rate is what your marks say. The other
settings (70%, 90%) show how many it'd have answered with a stricter threshold.

## Limits

- The second listen ends a stretch after 0.6 s of quiet, and skips stretches over 8 s, ones less than 1.5 s after the
  last, and anything said within 1 s of its speaking. A request with a pause in it may be judged as two halves.
- It isn't on during its own Discord bridge calls. There, everything you say already goes to it.
- A game in front: nothing is judged, as in the turn shadow.

## Turning it off

`addressee.enabled: false` in config/local.yaml. It's also off without the local brain (`local_brain`) or the
second listen (`wake.second_chance`).

## Next

After a few days, `octavia addressee show` and your marks say how often it'd have cut in wrongly, and in which
situations. Going live (it answers without its name) waits for your go-ahead on those numbers. It would likely start
only where it's safest: no call, nothing playing, and a high threshold.
