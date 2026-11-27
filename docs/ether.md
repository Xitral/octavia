# The ETHER

**E**ntangled **T**hought **H**armonics and **E**mergent **R**esonance: the space where every OCTAVIA shares what it
learns.

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

The shared cases are the yardstick: every release records how many it passes (`octavia ether status`), so whether the
collective gets more capable is a number per release.

## What was learned, one item at a time

A lesson doesn't wait for a release, and doesn't need one to be taken back. The signed feed carries **items**
beside the releases: each lesson with an id, a version, what it was accepted on, and whether it still stands.

- **Measured before it's shared.** A lesson is shared with the case it fixes. `octavia ether share-lesson "..."
  --case <shared case> --feed <folder>` asks that case five times without the lesson and five times with it. The
  lesson is taken only when every ask was answered, the case passes at least two thirds of the time with it, and
  the difference is one chance makes no more than once in twenty (1 of 5 against 5 of 5 is; 2 of 5 against 5 of 5
  isn't yet, and `--tries 8` settles it either way); then every shared case is run with it in and without, and it's refused if the total
  drops by more than the set's own one-case flake. The numbers go into the feed with the item. A lesson is a
  sentence or two (300 characters at most), and it's still read by the maintainer before it's shared: the
  measurement says it helps, not that it's harmless.
- **Taken in at the next look.** An install checks the feed every `hive.check_every_h` hours; the active items go
  into its prompt then, no update, no restart. The newest twenty from the feed and the newest twenty a release
  shipped, so neither crowds the other out.
- **Revocable.** `octavia ether revoke <id> --reason "..." --feed <folder>` marks it revoked; every install drops it
  at its next look at the current feed. A lesson a release shipped is revoked the same way with `--shipped`.
- **Yours to refuse.** `octavia ether items` lists them with their measured effect; `octavia ether drop <id>` leaves
  one out on your install (`--undo` takes that back).
- **A feed that can't be turned back.** Every signing raises the feed's serial and renews its date (60 days;
  `octavia ether renew --feed <folder>` signs it again when nothing new is published). An install refuses a feed
  older than one it has already trusted, however well signed. What that doesn't cover, said plainly: an install
  that has never seen a newer feed takes an older one, and whoever serves the feed could keep handing out the copy
  from before a revoke. That's what the date is for: after 60 days at most that copy is refused, and the install
  takes the feed's items out of its prompt until it sees a current one.
- **An update that can be undone.** The files an update replaces are kept; `octavia ether rollback` puts them back
  (one update back). An update or an undo that stops partway can be run again. Packages a newer release installed
  stay installed.
- **A pass rate that's computed.** `octavia ether publish` and `octavia release github` run the shared cases
  themselves; a typed number is no longer taken. A release isn't published when not every case could be asked, or
  when it passes fewer than the last one (`--allow-drop` for the second).

## Privacy

- `hive.share` is off unless you turn it on (the setup wizard asks).
- Each proposal is checked with the same leak scanner as the public build, using **your** install's personal values:
  your `.env` values, your name and home from your settings, your account and git names, and the words in
  `data/release/private_terms.txt`. One that finds anything is held in `data/hive/outbox` with the reasons and never
  sent (`octavia ether outbox`, `octavia ether drop-held`).
- A fix may only change code (`src/octavia/`, `avatar/`, tests, docs), never the update or scanning code.
- Cases come only from caught mistakes you kept, and their words go through the same check.
- The maintainer checks every proposal again before taking it.

## Commands

For everyone:

- `octavia ether status`: the releases in the feed, their shared-eval pass rates, which one you run.
- `octavia ether update`: apply the newest signed release (keeps `data/`, `.env`, `config/local.yaml` and your persona).
- `octavia ether collect`: turn installed fixes and kept cases into proposals.
- `octavia ether lesson "..." --about App`: propose a lesson yourself.
- `octavia ether outbox`, `octavia ether send`, `octavia ether drop-held`.

For the maintainer:

- `octavia ether keygen`: the signing key, in `data/hive/maintainer_key.pem` (back it up; never share it). Put the
  public key it prints in `config/public.yaml` as `hive.public_key`.
- `octavia release build`, then `octavia ether publish dist/octavia-<v>.zip --feed <folder> --passed N --total M`:
  adds the release to a local feed folder and signs it. Putting that folder online is a separate, manual step.
- `octavia ether review <inbox folder>` and `octavia ether accept <file>`.

## Settings

```yaml
hive:
  feed: ""         # https address or folder with channel.json + channel.json.sig
  public_key: ""   # the maintainer's Ed25519 public key (hex)
  share: "off"     # off | on
  inbox: ""        # https address (POST) or a folder for proposals
```
