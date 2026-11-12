# smile-watcher

Tells you when an earlier appointment frees up in the [Smile Centrum online booking](https://smilecentrum.xdent.cz/wizard)
(or any other xDent booking site at `<site>.xdent.cz`).

Every 5 minutes a GitHub Actions job reads the free slots the booking wizard shows and emails you (and pushes via
[ntfy](https://ntfy.sh), if set) when a slot appears that is:

- **with `before`**: earlier than that date. The first run tells you about everything already free before it.
- **without `before`**: earlier than anything that was free on the previous run. The first run only records what's free.
  After that, you hear about a slot only when it beats the current earliest one.

Each slot comes with a link that opens the wizard on that doctor's calendar with the slot pre-selected.
The watcher only reads. Booking is up to you.

## What to watch: `WATCH_CONFIG`

The repo is public, so what you watch isn't in it. It lives in the repository **secret** `WATCH_CONFIG`. It's a
secret, not a variable, because Actions logs print each step's env and mask only secrets. Keep it on one line, since
GitHub masks single-line values reliably:

```sh
gh secret set WATCH_CONFIG -R panarec/smile-watcher -b "$(python3 -c 'import json;print(json.dumps(json.load(open("config.json")),ensure_ascii=False))')"
```

Secrets can't be read back, so keep your copy in the git-ignored `config.json`. For the same reason the Actions logs
only show counts ("watch: 150 free slots, 0 new earlier"). Clinic, doctor and dates appear only in the email.

```json
{
  "site": "smilecentrum",
  "watches": [
    {"clinic": "Holešovice", "service": "Nový pacient"},
    {"clinic": "Vinohrady", "service": "Dentální hygiena", "staff": ["Kramárová"], "before": "2026-12-15",
     "from": "07:00", "to": "12:00", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}
  ]
}
```

Editing a watch starts it from scratch (it's identified by clinic + service + doctors), so a watch without
`before` records on its first run again.

| Field | Meaning |
| --- | --- |
| `clinic`, `service` | Name, part of the name (case and accents don't matter) or id. Must match exactly one. |
| `staff` | Optional doctor name(s). Leave out to accept any doctor offering the service. |
| `before` | Optional `YYYY-MM-DD` (= before that day) or `YYYY-MM-DD HH:MM`, e.g. your current appointment. |
| `after` | Optional: ignore slots before this date. |
| `from`, `to` | Optional: the appointment has to fit between these times of day. |
| `weekdays` | Optional: `mon`…`sun`. |
| `min_hours_ahead` | Ignore slots starting sooner than this (default 2). |

To see what's available and how things are named:

```sh
python3 watcher.py --list            # every clinic / service / doctor with its earliest free slot
python3 watcher.py --list holešovice # filtered
```

Locally the watcher reads `config.json` (git-ignored) unless a `WATCH_CONFIG` environment variable is set.

## Setup

Repository secrets:

| Secret | |
|---|---|
| `SMTP_USER`, `SMTP_PASSWORD` | Gmail address + [app password](https://myaccount.google.com/apppasswords) that sends the email |
| `EMAIL_TO` | where it goes (defaults to `SMTP_USER`) |
| `SMTP_HOST`, `SMTP_PORT` | optional, default `smtp.gmail.com:587` (465 = SSL, otherwise STARTTLS) |
| `EMAIL_FROM` | optional, defaults to `SMTP_USER` |
| `NTFY_TOPIC` | optional: push to the ntfy app too (subscribe to the same hard-to-guess topic name). Tapping the notification opens the booking. |

Check it: Actions → *Watch appointments* → Run workflow → tick "Only send a test email / push".

### 5-minute trigger (cron-job.org)

GitHub's `schedule` can start late or skip runs (in baskalka-bot it never fired). So, as there,
[cron-job.org](https://cron-job.org) starts the workflow and the `schedule` is only a backup. Actions minutes are
free in public repos, so the occasional double run doesn't matter.

1. GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new):
   *Only select repositories* → `smile-watcher`, permission **Actions: Read and write**, nothing else.
2. cron-job.org job, every 5 minutes:
   - URL `https://api.github.com/repos/panarec/smile-watcher/actions/workflows/watch.yml/dispatches`, method **POST**
   - headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
   - body `{"ref":"main"}`

## Behaviour

- State (the earliest slots from the last run) is kept in the Actions cache, not committed.
- A slot that keeps flipping between taken and free is announced at most every 6 hours.
- If a send fails, the run doesn't save state, so the same slots count as new on the next run.
- If every check fails for 2 hours (site down, API changed), you get one "watcher is failing" email, and one more when it works again.
  The run itself never shows as failed, so GitHub doesn't email you every 5 minutes.

## How it works

No browser and no login. The wizard's API at `napi.xdent.app` picks the clinic chain from the `Referer` header:

- `GET /api/OnlineBooking/MetadataByMiniWebsite`: clinics → services → doctors (ids, durations)
- `GET /api/OnlineBooking/GetAvailableTimesAll?idService=…&idStaffs=…&idStaffs=…`: every free start time
  (15-minute steps, clinic local time, ~4 months ahead)

Booking link: `https://<site>.xdent.cz/wizard?idLocation=<clinic>&idService=<service>&idStaff=<doctor>`.

## Local

```sh
cp config.example.json config.json   # edit
python3 watcher.py --dry-run         # print only
python3 watcher.py                   # notifies with SMTP_* / NTFY_TOPIC from the environment
```
