# The hivemind

Every OCTAVIA gets better from what every other one learns, with a person in the middle. Nothing goes from one
install straight into another.

```text
 your install ──proposals (scrubbed, opt-in)──▶ maintainer reviews ──accepts──▶ repository
      ▲                                                                            │
      └──────────── signed release (with its shared-eval pass rate) ◀── release build
```

## What's shared, and why those

Only things that make the next version more capable, never memories:

| Kind | Where it comes from | What accepting it does |
|---|---|---|
| fix | a change its self-improvement built, tested and installed here | saved as a patch for its own branch, judged by the tests and eval gate |
| case | a request it got wrong that became a graded eval case, one you kept | added to `src/octavia/evolve/shared_cases.yaml`, run on every build |
| lesson | a short general rule (how an app behaves, a habit that tested well) | added to `src/octavia/hive/shared_lessons.yaml`, which every prompt gets |

The shared cases are the yardstick: every release records how many it passes (`octavia hive status`), so whether the
collective gets more capable is a number per release.

## Privacy

- `hive.share` is off unless you turn it on (the setup wizard asks).
- Each proposal is checked with the same leak scanner as the public build, using **your** install's personal values:
  your `.env` values, your name and home from your settings, your account and git names, and the words in
  `data/release/private_terms.txt`. One that finds anything is held in `data/hive/outbox` with the reasons and never
  sent (`octavia hive outbox`, `octavia hive drop-held`).
- A fix may only change code (`src/octavia/`, `avatar/`, tests, docs), never the update or scanning code.
- Cases come only from caught mistakes you kept, and their words go through the same check.
- The maintainer checks every proposal again before taking it.

## Commands

For everyone:

- `octavia hive status`: the releases in the feed, their shared-eval pass rates, which one you run.
- `octavia hive update`: apply the newest signed release (keeps `data/`, `.env`, `config/local.yaml` and your persona).
- `octavia hive collect`: turn installed fixes and kept cases into proposals.
- `octavia hive lesson "..." --about App`: propose a lesson yourself.
- `octavia hive outbox`, `octavia hive send`, `octavia hive drop-held`.

For the maintainer:

- `octavia hive keygen`: the signing key, in `data/hive/maintainer_key.pem` (back it up; never share it). Put the
  public key it prints in `config/public.yaml` as `hive.public_key`.
- `octavia release build`, then `octavia hive publish dist/octavia-<v>.zip --feed <folder> --passed N --total M`:
  adds the release to a local feed folder and signs it. Putting that folder online is a separate, manual step.
- `octavia hive review <inbox folder>` and `octavia hive accept <file>`.

## Settings

```yaml
hive:
  feed: ""         # https address or folder with channel.json + channel.json.sig
  public_key: ""   # the maintainer's Ed25519 public key (hex)
  share: "off"     # off | on
  inbox: ""        # https address (POST) or a folder for proposals
```
