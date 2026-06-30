# icloud-ics-import-tool

A small, dependency-light CalDAV toolbox for getting an **`.ics` file reliably into
iCloud** — and for diagnosing why a bulk import went wrong. Works against any RFC 4791
CalDAV server, but defaults to iCloud.

## The problem

Importing a large `.ics` (thousands of events) through the macOS/iOS **Calendar app**
is unreliable:

- The app pushes events to the server one request at a time and gets **throttled**, so
  the upload trickles for hours or days and often never finishes.
- After the import, the app **reconciles its local copy against the server**, which is
  authoritative. Events that failed to push get **deleted locally** on the next sync —
  so events you *saw* right after importing **disappear**.
- iCloud enforces **account-wide unique UIDs**. If a UID already exists in another
  calendar (e.g. a half-finished earlier import landed in your default calendar), a
  fresh import returns **`412 Precondition Failed`** and silently skips it.

This tool sidesteps all of that: it talks **CalDAV directly**, uploading each event with
pacing, retry, and resume, and it **skips what's already on the server** so it's safe to
run repeatedly. Because the data lands server-side first, your devices then sync it
*down* — the reliable direction.

It also bundles the diagnostics you need when something is off: analyse a file offline,
list calendars, find which calendar a UID lives in, diff a file against a calendar, and
export a calendar back to `.ics`.

## Install

Requires Python 3.8+.

```sh
git clone https://github.com/discostur/icloud-ics-import-tool.git
cd icloud-ics-import-tool
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Credentials

iCloud needs an **app-specific password** (not your login password — that fails under
two-factor authentication with `401`).

1. Go to <https://account.apple.com> → **Sign-In & Security → App-Specific Passwords**.
   *(Missing? Two-factor auth must be enabled on the Apple ID for this option to appear.)*
2. Generate one — Apple shows `xxxx-xxxx-xxxx-xxxx` (keep the dashes).
3. Provide credentials by any of (highest precedence first): CLI flags `--user/--password`,
   environment variables, or a `.env` file:

```sh
cp .env.example .env
# edit .env:
#   ICLOUD_USER=you@example.com          # your PRIMARY Apple ID e-mail
#   ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

`.env` is gitignored. Never commit it.

## Commands

| Command | What it does | Needs login |
|---|---|---|
| `analyze FILE` | Offline health check of an `.ics` (counts, UIDs, dups, attachments, timezones, encoding, date range) | no |
| `list` | List calendars with event counts | yes |
| `find UID` | Show which calendar(s) contain a UID (diagnose `412` conflicts) | yes |
| `diff FILE --calendar NAME` | What's in the file but missing on the server (and vice-versa) | yes |
| `import FILE --calendar NAME` | Upload — paced, retried, resumable, skips existing | yes |
| `export --calendar NAME -o OUT.ics` | Download a calendar back to `.ics` (backup) | yes |
| `create NAME` | Create a calendar | yes |
| `delete NAME --yes` | Delete a calendar | yes |

Run `icloud_ics.py <command> --help` for every flag.

### Typical migration

```sh
# 1. Sanity-check the file (no login needed)
python icloud_ics.py analyze calendar.ics

# 2. See your calendars
python icloud_ics.py list

# 3. Preview the import: parses, validates login, writes 3 sample .ics, uploads nothing
python icloud_ics.py import calendar.ics --calendar "My Import" --create --dry-run

# 4. Do it. Re-run any time — it skips what's already uploaded.
python icloud_ics.py import calendar.ics --calendar "My Import" --create

# 5. Verify nothing is missing
python icloud_ics.py diff calendar.ics --calendar "My Import"
```

On macOS, prefix the real import with `caffeinate -i` so the Mac doesn't sleep
mid-upload (sleep pauses networking).

### Useful `import` flags

- `--dry-run` — parse, validate credentials/endpoint, write samples, change nothing.
- `--delay 0.3` — seconds between uploads; raise it if you hit throttling.
- `--strip-bloat` — drop alarm-sound refs (`ATTACH:Basso`), dead Exchange/EWS
  attachments, and redundant Apple default-alarm `X-` props. Keeps all real event data
  and alarms. Useful for calendars exported from Exchange via Apple Calendar.
- `--regenerate-uids` — assign every event a fresh UID. Use when a `412` tells you the
  UIDs already exist in another calendar and you want independent copies.
- `--max N` — upload at most N (good for a small live test).
- `--no-skip-existing` — re-PUT everything instead of skipping what's on the server.

Recurring events with `RECURRENCE-ID` overrides are automatically grouped into one
resource per UID, so series stay intact.

## Troubleshooting

**`401 Unauthorized`** — You're using your normal login password. With two-factor auth
you must use an **app-specific password**, and `ICLOUD_USER` must be your **primary**
Apple ID e-mail (not an alias). Regenerate the password if unsure.

**`412 Precondition Failed` on some events** — Those UIDs already exist elsewhere on the
account (commonly a previous partial import sitting in your default *Kalender/Calendar*).
Find them with `find <UID>`. Then either upload into that **same** calendar (the tool
skips the ones already there), remove the old copies, or use `--regenerate-uids` to
upload independent copies.

**Upload is slow / events trickle in** — That's iCloud throttling. Increase `--delay`.
The tool retries `429/5xx` with backoff automatically. The job is resumable: if it dies,
re-run the same command.

**Events appeared then disappeared (via the Calendar app)** — Classic client→server
reconciliation loss. Stop importing through the app; use this tool so the data is
authoritative server-side, then let devices sync down.

**`NotOpenSSLWarning` / TLS errors on old macOS Python** — `pip install 'urllib3<2'`.

## How it works

Pure CalDAV over HTTPS (RFC 4791), no third-party CalDAV library:

1. `PROPFIND` for `current-user-principal`, following iCloud partition redirects
   (`pNN-caldav.icloud.com`).
2. `PROPFIND` for `calendar-home-set`, then enumerate calendars.
3. For each event group (one UID + its recurrence overrides) build a minimal `VCALENDAR`
   with the referenced `VTIMEZONE`s and `PUT` it to `<calendar>/<uid>.ics`.
4. Skip UIDs already present (a `PROPFIND` of the target), pace requests, retry on
   throttling, log each result for resume.

## Contributing

Issues and PRs welcome. The tool is intentionally a single dependency-light file
(`icloud_ics.py`) so it's easy to read, audit, and run anywhere.

## License

[Apache License 2.0](LICENSE).

> ⚠️ Use at your own risk. This writes to your calendar account. Always run `analyze`
> and `--dry-run` first, and keep a backup (`export`) of any calendar you care about.
