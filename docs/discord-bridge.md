# Texting and calling it from your phone (the Discord bridge)

DM a Discord bot of your own from your phone and OCTAVIA answers there, in text: "what's due tomorrow?", "is my
build done?", "send me the report I was working on", a photo with "what does this error mean?". It works on the
PC as usual (your tasks, your files, the screen), says nothing out loud there, and texts back. While you're away
from the PC, its announcements (a build ready, a timer, a reminder, a background task done) come to the DM too.

It runs in voice mode only (`uv run octavia`, or autostart), not in `octavia chat`.

## Setting it up

1. **Make an application.** Go to <https://discord.com/developers/applications>, click **New Application** and
   give it a name (say "OCTAVIA"). This is your own bot: nobody else has its token.
2. **Add a bot and copy its token.** In the application, open **Bot**. Click **Reset Token**, then copy the token
   it shows (only once). Put it in the `.env` file in OCTAVIA's folder, never in a YAML file:

   ```
   DISCORD_BOT_TOKEN=paste-the-token-here
   ```

   While you're on that page, you can turn **Public Bot** off, so only you can add it anywhere.
3. **Turn on the Message Content intent.** Still under **Bot**, in **Privileged Gateway Intents**, switch on
   **Message Content Intent** and save. Without it Discord hands the bot your DMs with no text in them, and it
   says so at startup.
4. **Find your own user ID.** In Discord, open **User Settings**, **Advanced**, and turn on **Developer Mode**.
   Then right-click (or long-press, on the phone) your own name anywhere and pick **Copy User ID**. It's a long
   number, like `123456789012345678`.
5. **Make it possible to DM the bot.** Discord only lets you DM a bot you share a server with, or one you added to
   your account. Either:
   - **A private server (simplest):** make a server just for yourself (the **+** in the server list, "Create My
     Own", "For me and my friends"). In the Developer Portal, open **OAuth2**, **URL Generator**, tick the **bot**
     scope, and under bot permissions tick only **Send Messages** and **Attach Files** (nothing else is needed).
     Open the URL it makes, pick your private server, and authorize. The bot never reads that server: it only
     listens to DMs. Then find the bot in the server's member list and click **Message**.
   - **Or as an app on your account:** under **Installation**, enable **User Install**, open the install link it
     shows and add the app to your account; then you can DM it. (If Discord doesn't offer you a DM with it that
     way, use the private server.)
6. **Turn the bridge on** in `config/local.yaml` (with your ID from step 4):

   ```yaml
   discord_bridge:
     enabled: true
     owner_id: 123456789012345678
   ```

   (Or put `DISCORD_OWNER_ID=123456789012345678` in `.env` instead of `owner_id`.)
7. **Restart it**, then check with `uv run octavia doctor` (add `--live` to have it log the bot in and look at the
   Message Content intent). Send the bot "hi" from your phone.

## What it's safe to let it do

The bot is a way into a PC it controls, so the bridge is careful:

- **Only you.** It answers DMs from `owner_id` and nobody else. A DM from anyone else, a message from a bot, and
  anything in a server channel are ignored without a word: never answered, never shown to its brain (only a debug
  line in the log, without what was said). Without an owner id the bridge doesn't start at all.
- **The token stays in `.env`.** It's read when the bot logs in and never written to a log, a message or a reply.
  If Discord refuses it, it says so once and stops trying (no retry loop): reset the token and restart.
- **Every risky step is asked in the DM.** A texted request is your own, but your words over Discord never approve
  anything ahead of time: "you don't need to ask, just send it" still gets the question, a yes earlier in the same
  task doesn't carry over to the next send, and neither `permissions.submit_without_asking` nor
  `permissions.trusted_apps` skip a question there. It texts "Want me to delete the old note? Reply yes or no." and
  waits up to `confirm_timeout_s` (2 minutes) for your next message; an unclear answer is asked once more, and no
  answer means no. Your high-risk places (`permissions.always_ask_before_sending`: email, Canvas) ask every time,
  as they do at the PC. Only harmless steps set to `auto` (a look at the screen, if you allowed it) go ahead.
- **Files it sends you** (`send_to_phone`) only go to your DM, and only ordinary files: it refuses `.env` and
  other secrets, its own login and notes (`data/claude-home`, `data/self`, `data/gmail`, its memory database), keys
  (`.pem`, `.key`, `id_rsa`...), anything with "token", "secret", "password" or "credential" in its name, folders,
  and anything over `max_file_mb` (Discord's 25 MB upload limit for bots), saying how big it was. Every send is in
  the audit log (`data/logs/octavia.log`, `octavia.audit`). It can also send the latest picture it got (a satellite
  picture, a screenshot) for ten minutes after it got it: it's only kept in memory, and written to a temporary file
  just for the upload.
- **A pause.** At most `max_turns_per_10_min` (20) texted requests in ten minutes; past that it replies once that
  it's pausing, and picks up again when the ten minutes have passed.

## Using it

- **Text it anything** you'd say out loud. Each message waits its turn: if you're talking with it at the PC, or
  it's in the middle of an announcement, it goes after that, never cutting in. The DM shows "typing..." while it
  works. Long answers come in several messages (Discord takes 2,000 characters each); lists, links and code are
  fine, since you read them.
- **Send its files.** Photos, screenshots, voice notes, documents: they're kept in `data/discord/inbox/` (up to
  `max_attachment_mb`), and it can look at a photo or read a text file (`open_phone_attachment`). Word, Excel and
  PowerPoint files can be opened with its Office tools. A photo too big for it to look at gets a smaller copy
  from Discord.
- **Send its voice messages.** Hold the mic button in the DM and talk: its own speech recognition, on the PC,
  writes it down, and it answers what you said as if you'd typed it.
- **Ask for files** from voice or text: "send that file to my phone", "text me the list".
- **Quick commands and "which model was that?"** work the same as at the PC.
- **Updates while you're away.** With `notify: away` (the default), its announcements are also texted once nobody
  has touched the keyboard or mouse for `away_after_min` minutes; `always` texts every one, `off` none. Your
  phone's own notifications (mirrored by Phone Link) are never texted back to it.

## Calling its

Join a voice channel on your server (the one its bot is in) from your phone, and it joins it too. From then on
the call is its ears and its voice: talk as you would at the PC, through your earbuds or your helmet's headset.

- **No wake word on a call.** Everything you say there is for it, the way a phone call is (`call_wake_word: true`
  brings the name back). A short click or bump on the line doesn't start a request; a word does.
- **Only you are heard.** If someone else is in the channel, their voice is dropped unread: it only decrypts and
  listens to your stream. Risky steps are asked out loud on the call, and you answer there.
- **The PC goes quiet.** Its voice goes to the call, not out of the PC's speaker too (`mute_pc_on_call`), and what's
  said at the PC isn't heard while you're on the call. Its announcements are said in the call instead of texted.
- **It follows you.** Move to another voice channel and it moves; leave and it leaves (and stops talking). If
  it's on when you're already in a channel, it joins you there.
- **Permissions.** It needs **Connect** and **Speak** in that channel. A new server's @everyone role has both, so
  usually there's nothing to do; if not, it says what's missing once (give its bot's role Connect and Speak under
  Server Settings, Roles). The two intents this uses (servers and voice states) aren't privileged: nothing to switch
  on in the Developer Portal. DMs and calls are separate: it never joins a call you start in the DM itself
  (Discord doesn't let bots take those), only a server voice channel.

Calls are end-to-end encrypted (Discord's DAVE); it uses the `davey` library for that, installed with
`discord.py[voice]`.

## Settings (`config/local.yaml`)

```yaml
discord_bridge:
  enabled: false
  owner_id: 0                  # your Discord user ID; 0 = DISCORD_OWNER_ID from .env
  notify: away                 # off | away | always
  away_after_min: 10
  speak_replies: false         # also say texted replies out loud at the PC
  confirm_timeout_s: 120       # how long a question waits for your answer; no answer = no
  max_turns_per_10_min: 20
  max_file_mb: 25              # the biggest file she sends you
  max_attachment_mb: 25        # the biggest file of yours she keeps
  voice_calls: true            # join your voice channel and talk there
  mute_pc_on_call: true        # on a call, her voice goes to the call only
  call_wake_word: false        # on a call, everything you say is for her
```

## For developers

- `src/octavia/bridge/discord_bridge.py`: `DiscordBridge` (the client's events, the owner check, the rate limit,
  replies split at 2,000 characters, the DM confirmer, announcements) on discord.py.
- `src/octavia/bridge/attachments.py`: the inbox, the turn's note about files, and `outgoing_problem` (what may be
  sent).
- `src/octavia/bridge/voice_notes.py`: voice messages (Ogg Opus) taken apart and decoded with the calls' libopus
  decoder, then written down by the assistant's speech recognition in pieces cut at quiet moments.
- `src/octavia/tools/extensions/phone_bridge.py`: `send_to_phone` and `open_phone_attachment`.
- `src/octavia/core/announcing.py`: `Assistant.run_from` queues a texted turn (source `discord`); `_REMOTE_SOURCES`
  (`core/turn_types.py`) turns off every word-based shortcut and routes the turn's questions to `remote_confirmers`.
- `src/octavia/bridge/discord_voice.py`: `VoiceCalls` (following the owner in and out of voice channels),
  `Receiver` (the voice socket's packets: RTP, the `aead_xchacha20_poly1305_rtpsize` transport layer, DAVE, then
  Opus decoded straight to 16 kHz mono; only the owner's SSRC, from the voice gateway's SPEAKING messages), and
  `CallVoiceClient` (discord.py's voice client plus that SSRC map). discord.py sends voice but doesn't receive it.
- `src/octavia/audio/call_audio.py`: `CallInput` (the caller's voice, one frame every 32 ms with silence between, so
  the endpointer hears utterances end), `CallOutput` (the speaker's render tap resampled to 48 kHz stereo for the
  call) and `CallAwareSource` (the microphone, or the call while there is one).
- `Assistant.start_call` / `end_call` and `HearingMixin._call_frame`: on a call, speech starts a request without the
  wake word, the call counts as headphones (the phone cancels its echo), and voice turns get a note that it's a call.
- Tests: `tests/test_discord_bridge.py` and `tests/test_discord_voice.py` (fake Discord clients and voice
  connections: nothing connects to Discord).
