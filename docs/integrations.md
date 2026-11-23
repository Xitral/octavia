# Connecting your services

Everything it connects to is set up the same way: a key in the `.env` file in its folder (next to `config/`),
never in YAML. This page lists them all. `.env.example` has the same list with a line for each, and
`src/octavia/keys.py` is where the code keeps it (a test keeps the three in step).

Only the Fish Audio key is required. Everything else is optional: leave a line out and that service just says it
isn't connected yet, and how to connect it.

## Checking your .env

```powershell
uv run octavia env              # every key: set, not set, or what's wrong with it (never shows a value)
uv run octavia env tidy         # sort your .env into commented sections; every value stays exactly as it is
uv run octavia env tidy --dry-run   # show the new layout first (values hidden)
```

`octavia env` catches the usual mix-ups: a misspelled name (`CALENDAR_ICS_URL` for `CALENDAR_ICS_URLS`), a line with
nothing after the `=`, a value still left as `<token>`, the same key twice (only the last one counts), a key put in
another `.env` that isn't read (the brain's work folder, `data/brain`), and a Windows environment variable of the
same name that wins over the file. `tidy` keeps a copy of the old file as `.env.bak` (git ignores it), and leaves a
file alone if it has anything it can't be sure of moving, such as a value quoted across several lines.

`uv run octavia doctor` runs the same check along with everything else.

Values go straight after the `=`, on one line; quotes are fine but not needed. Keys marked *restart* below are read
when it starts, so restart it after adding one; the others work as soon as the file is saved.

## The services

| Service | Key(s) | Where to get it | Restart |
|---|---|---|---|
| Its voice (Fish Audio) | `FISH_AUDIO_API_KEY` (required), `FISH_AUDIO_VOICE_ID` | [fish.audio/app/api-keys](https://fish.audio/app/api-keys); a voice's id from [fish.audio/app/discovery](https://fish.audio/app/discovery) or your own cloned voice | restart |
| Claude (API instead of your subscription) | `ANTHROPIC_API_KEY` | only with `brain.claude.auth: api_key`; otherwise leave it out, it would bill the API. The subscription needs `uv run octavia login` instead | restart |
| TickTick | `TICKTICK_API_TOKEN` | TickTick on the web: your avatar, Settings, Account & Security, API Token. See [ticktick.md](ticktick.md) | restart |
| Canvas | `CANVAS_API_TOKEN`, `CANVAS_BASE_URL` | Canvas: Account, Settings, "+ New Access Token". The address is your school's Canvas site (`https://<school>.instructure.com`) | no |
| Gmail | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | turn on 2-Step Verification, then make an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (never your usual password). Only used to read the inbox | no |
| Gmail search, school accounts too | none: a file, `data/gmail/client_secret_….json` | for accounts without app passwords (many schools' accounts), or to search whole emails. Once, at [console.cloud.google.com](https://console.cloud.google.com): a project with the Gmail API turned on; the OAuth consent screen (External, each address added as a test user, or the app published, since a Testing app's sign-in lapses after 7 days); under Credentials an OAuth client ID of type "Desktop app", its JSON downloaded into `data/gmail/`. Then ask it to connect a Gmail account, once per account. It reads mail, and drafts, replies and sends email only after you say yes to each one (hearing whom it goes to, the subject and the whole text); an account connected before that needs connecting again to write. A school's admin may block apps it hasn't approved; it says so when that happens | no |
| Google Chat (school or work accounts) | none: the same `data/gmail/client_secret_….json` | in the same Google Cloud project: turn on the Google Chat API and, on its Configuration tab, give it an app name, avatar URL and description (Google refuses until then); turn on the People API too, for senders' names. Then ask it to connect Google Chat (its own sign-in, read-only: direct messages and Spaces) | no |
| Google Calendar, adding and changing events | none: the same `data/gmail/client_secret_….json` | in the same Google Cloud project, turn on the Google Calendar API. Then ask it to connect Google Calendar (its own sign-in). It adds, moves and deletes events directly, asking you first each time; without it, adding an event opens a filled-in page you save | no |
| Calendar | `CALENDAR_ICS_URLS` | each calendar's private iCal address, all on one line, separated by spaces. Google Calendar: Settings, the calendar's name, "Secret address in iCal format" | no |
| Spotify playlists and playback | `SPOTIFY_CLIENT_ID` | a free app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) with the Web API and the redirect URI `http://127.0.0.1:8898/callback`; then ask it to connect Spotify. Connected, it plays songs by name, albums, playlists, artists and Liked Songs directly, and controls pause, skip, shuffle and Spotify's volume (Premium only); without it it clicks through the app. A sign-in made before playback control needs connecting again once | no |
| Naming a song | `AUDD_API_TOKEN` | a free token from [dashboard.audd.io](https://dashboard.audd.io) | no |
| 3D cities in God's Eye View | `CESIUM_ION_TOKEN` or `GOOGLE_MAPS_API_KEY` | a free Cesium ion token from [ion.cesium.com](https://ion.cesium.com) (Access Tokens), or a Google key with the Map Tiles API (billing required). Without either, God's Eye View shows flat satellite imagery | no |
| Texting it from your phone (Discord) | `DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID` (or `discord_bridge.owner_id`) | your own bot: an application at [discord.com/developers/applications](https://discord.com/developers/applications) with a Bot, its Message Content intent on, the token from Bot, Reset Token; your user ID from Developer Mode, right-click yourself, Copy User ID. Then `discord_bridge.enabled: true`. See [discord-bridge.md](discord-bridge.md) | restart |
| Vercel (your sites) | none: a file, `data/vercel/token.txt` | an access token from [vercel.com/account/tokens](https://vercel.com/account/tokens), scoped to the team that owns the projects, pasted alone on the file's first line. Read-only: projects, deployments and whether they're ready, failed or building, build logs, domains | no |
| Garmin watch data | none: a sign-in, kept in `data/garmin/tokens.json` | ask it to connect Garmin: a small window asks for your Garmin Connect email and password (typed, never spoken; the password isn't kept, only Garmin's tokens, sealed so only your Windows account can read them). A code Garmin emails for two-step verification you tell it once. Garmin's official developer API only takes approved businesses, so this signs in as the Garmin Connect app does. Read-only: heart rate, HRV, stress, Body Battery, sleep, steps, blood oxygen and activities; a scheduled job can watch for a high heart rate or a low Body Battery | no |
| Supabase (your databases) | none: a file, `data/supabase/token.txt` | a personal access token from [supabase.com/dashboard/account/tokens](https://supabase.com/dashboard/account/tokens), pasted alone on the file's first line. Read-only: projects, health, tables and row counts, SELECT queries (run as Supabase's read-only database user), logs | no |

Needing nothing at all: the weather (Open-Meteo; your home unless you name a place), playing a sound you ask for
(an animal call, a thunderstorm; a short clip from Wikimedia Commons), playing music and media keys,
timers, scheduled jobs, background tasks, files, apps, the screen tools, Discord (through its app on screen) and
the games. Email alerts (`email_alerts`) need the Gmail keys or an account connected through Google's sign-in (school
accounts included), and are turned on and off by asking it.

Android phone notifications (`phone_notifications`, and spoken alerts with `phone_alerts`) need no key either: pair
the phone in Windows' Phone Link app and let it show the phone's notifications. It reads the ones still in the
Windows notification center and, while Phone Link's window is open (minimised is fine), the ones in its own
notifications panel, read-only. The spoken alerts skip spam, political and marketing texts, and senders or apps
muted with `phone_alert_filter`.

## Advanced

Two settings can come from `.env` too, for unusual setups: `OCTAVIA_CONFIG` (another settings file instead of
`config/local.yaml`) and `OCTAVIA_MODELS_DIR` (where the speech models live, if not in `models/`). `OCTAVIA_HOME`
(which folder is its own) can't: it decides which `.env` is read, so set it as a Windows environment variable.

## Adding a service

Add its key to `KEYS` in `src/octavia/keys.py` (what it's for, how to get one, whether it needs a restart), read it
with `keys.value(NAME)`, and answer "not connected" with `keys.missing(NAME, cfg=ctx.config)` so the user hears
exactly which file was checked and what's wrong. Then run `uv run octavia env example` (it rewrites `.env.example`),
and add a row here.
