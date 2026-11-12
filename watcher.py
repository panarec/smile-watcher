#!/usr/bin/env python3
"""Notify me when an earlier appointment frees up in an xDent online booking (Smile Centrum by default).

Reads the same public API the booking wizard at https://<site>.xdent.cz uses, collects the free slots
for each watch (clinic + service + optional doctors) and emails / pushes the ones that are earlier
than the watch's threshold and weren't free on the previous run:

- watch with "before": any free slot starting before that date/time
- watch without "before": any slot earlier than everything that was free on the previous run
  (so the first run only records what's free; you hear about it once something sooner appears)

Config: JSON in $WATCH_CONFIG, else config.json. State (last seen slots): state.json.

Usage:
    python3 watcher.py --list [TEXT]   clinics, services, doctors and their earliest free slot
    python3 watcher.py --dry-run       print what would be sent, no notification, no state change
    python3 watcher.py --test-email    send a test notification and exit
    python3 watcher.py                 check, notify, update state.json
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://napi.xdent.app/api/OnlineBooking"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RENOTIFY_HOURS = 6       # a slot that flickers taken/free is announced at most this often
FAILURE_ALERT_HOURS = 2  # email once when every run has failed for this long
# Public repo => public Actions logs: there, print only counts unless PRIVATE_LOGS=0.
QUIET = os.environ.get("PRIVATE_LOGS", "1" if os.environ.get("GITHUB_ACTIONS") == "true" else "0") == "1"


def log(*args):
    print(*args, flush=True)


def detail(*args):
    if not QUIET:
        print(*args, flush=True)


class Booking:
    """The xDent booking API. The site is picked by the Referer header, no login needed."""

    def __init__(self, site):
        self.site_url = f"https://{site}.xdent.cz"
        self.meta = self._get("MetadataByMiniWebsite")
        if not self.meta.get("clinics"):
            raise RuntimeError(f"{self.site_url} has no clinics in its booking metadata")
        self.tz = ZoneInfo(self.meta["clinics"][0].get("timezone") or "Europe/Prague")

    def _get(self, endpoint, params=()):
        url = f"{API}/{endpoint}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "Referer": self.site_url + "/",
            "Accept": "application/json",
            "Accept-Language": "cs",
            "User-Agent": "Mozilla/5.0 (appointment-watcher)",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except (OSError, ValueError) as e:
                if attempt == 2 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                    raise
                time.sleep(5 * (attempt + 1))

    def slots(self, clinic, service, staffs):
        """Free slots of the given doctors, earliest first, as Slot objects."""
        data = self._get("GetAvailableTimesAll",
                         [("idService", service["idService"])] + [("idStaffs", s["idStaff"]) for s in staffs])
        by_id = {s["idStaff"]: s for s in staffs}
        found = []
        for day in data:
            staff = by_id.get(day.get("idStaff"))
            for hour in day.get("openHours") or []:
                if staff:  # times are the clinic's local time without an offset
                    start = dt.datetime.fromisoformat(hour["dateTime"]).replace(tzinfo=self.tz)
                    found.append(Slot(clinic, service, staff, start, self.link(clinic, service, staff)))
        return sorted(found, key=lambda s: (s.start, s.doctor))

    def link(self, clinic, service, staff=None):
        """Opens the wizard on the doctor's calendar (or the doctor list without a doctor)."""
        q = {"idLocation": clinic["id"], "idService": service["idService"]}
        if staff:
            q["idStaff"] = staff["idStaff"]
        else:
            q = {"idLocation": clinic["id"], "selectIdService": service["idService"]}
        return f"{self.site_url}/wizard?" + urllib.parse.urlencode(q)


class Slot:
    def __init__(self, clinic, service, staff, start, link):
        self.clinic, self.service, self.staff, self.start, self.link = clinic, service, staff, start, link
        self.doctor = " ".join(staff["name"].split())
        self.key = f"{staff['idStaff']}|{start:%Y-%m-%dT%H:%M}"

    def when(self):
        return fmt(self.start)

    def __str__(self):
        return f"{self.when()}  {self.doctor}"


class Watch:
    def __init__(self, spec, booking):
        self.spec = spec
        clinics = booking.meta["clinics"]
        self.clinic = pick(clinics, spec["clinic"], lambda c: c["name"], lambda c: c["id"], "clinic")
        self.service = pick(self.clinic["services"], spec["service"], lambda s: s["name"], lambda s: s["idService"],
                            f"service at {self.clinic['name']}",
                            lambda s: f"{s['name']} ({s['duration']} min)")
        staffs = self.service["staffs"]
        if spec.get("staff"):
            wanted = [pick(staffs, name, lambda s: s["name"], lambda s: s["idStaff"], "doctor for this service")
                      for name in as_list(spec["staff"])]
            staffs = list({s["idStaff"]: s for s in wanted}.values())
        self.staffs = staffs
        self.id = f"{self.clinic['id']}|{self.service['idService']}|" + ",".join(sorted(s["idStaff"] for s in staffs))
        self.before = parse_before(spec["before"], booking.tz) if spec.get("before") else None
        self.after = parse_before(spec["after"], booking.tz) if spec.get("after") else None
        self.days = {WEEKDAYS.index(w.lower()[:3]) for w in as_list(spec["weekdays"])} if spec.get("weekdays") else None
        self.lo = minutes(spec.get("from", "0:00"))
        self.hi = minutes(spec.get("to", "24:00"))
        self.lead = dt.timedelta(hours=float(spec.get("min_hours_ahead", 2)))
        self.link = booking.link(self.clinic, self.service, staffs[0] if len(staffs) == 1 else None)
        self.title = f"{self.service['name']} · {short_clinic(self.clinic['name'])}"

    def matching(self, slots, now):
        def ok(s):
            m = s.start.hour * 60 + s.start.minute
            return (s.start >= now + self.lead and (not self.after or s.start >= self.after)
                    and self.lo <= m and m + self.service["duration"] <= self.hi
                    and (self.days is None or s.start.weekday() in self.days))
        return [s for s in slots if ok(s)]


def pick(items, wanted, name, ident, what, label=None):
    """Find one item by id, exact name or unique name fragment (case and accents ignored)."""
    label = label or name
    w = norm(wanted)
    for test in (lambda i: ident(i) == wanted, lambda i: norm(name(i)) == w, lambda i: w in norm(name(i))):
        hits = [i for i in items if test(i)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            options = "\n".join(f"  {label(i)}   id {ident(i)}" for i in hits)
            raise SystemExit(f'{what}: "{wanted}" is ambiguous; use a longer name or the id:\n{options}')
    options = "\n".join(f"  {label(i)}" for i in items)
    raise SystemExit(f'{what}: nothing matches "{wanted}". Options:\n{options}')


def check(watches, booking, state, now):
    """Returns (fresh slots per watch, new state). fresh = earlier than the threshold and not free before."""
    prev = state.get("watches", {})
    notified = {k: dt.datetime.fromisoformat(v) for k, v in state.get("notified", {}).items()}
    results, new_watches = [], {}
    for w in watches:
        slots = w.matching(booking.slots(w.clinic, w.service, w.staffs), now)
        keys = [s.key for s in slots]
        before = prev.get(w.id)
        if w.before:
            threshold = w.before
        elif before:
            # Earliest slot that was free last time and is still in the future.
            starts = [dt.datetime.fromisoformat(k.split("|")[1]).replace(tzinfo=booking.tz) for k in before]
            starts = [s for s in starts if s >= now + w.lead]
            threshold = min(starts) if starts else None
        else:
            threshold = None  # first run without "before": just record what's free
        seen_before = set(before or [])
        recent = lambda s: s.key in notified and now - notified[s.key] < dt.timedelta(hours=RENOTIFY_HOURS)
        fresh = [s for s in slots if threshold and s.start < threshold and s.key not in seen_before and not recent(s)]
        results.append((w, slots, threshold, fresh))
        # Only the start of the list matters: with "before" the slots before it, otherwise the earliest
        # ones (the threshold is their minimum; extras cover the first few slipping into the past).
        new_watches[w.id] = [s.key for s in slots if s.start < w.before] if w.before else keys[:100]
        for s in fresh:
            notified[s.key] = now
    cutoff = now - dt.timedelta(hours=RENOTIFY_HOURS)
    new_state = {
        "watches": new_watches,
        "notified": {k: v.isoformat(timespec="seconds") for k, v in sorted(notified.items()) if v > cutoff},
    }
    return results, new_state


def group(slots):
    """Slots of one doctor on one day together: [(first slot, 'HH:MM HH:MM ...')], earliest first.
    The API lists every 15-min start, so one freed appointment shows up as several slots."""
    groups = {}
    for s in sorted(slots, key=lambda s: s.start):
        groups.setdefault((s.start.date(), s.staff["idStaff"]), []).append(s)
    out = []
    for g in groups.values():
        runs = [[g[0], g[0]]]
        for s in g[1:]:
            if s.start - runs[-1][1].start <= dt.timedelta(minutes=15):
                runs[-1][1] = s
            else:
                runs.append([s, s])
        times = ", ".join(f"{a.start:%H:%M}" + (f"–{b.start:%H:%M}" if b is not a else "") for a, b in runs)
        out.append((g[0], times))
    return out


def render(results):
    """Email subject, email body, push text and push click link for the watches with fresh slots."""
    hits = [(w, slots, threshold, fresh) for w, slots, threshold, fresh in results if fresh]
    s = hits[0][3][0]
    days = sum(len(group(f)) for *_, f in hits)
    more = f" (+{days - 1} more)" if days > 1 else ""
    subject = f"Earlier appointment: {s.when()} – {s.doctor}, {short_clinic(s.clinic['name'])}{more}"
    parts, push = [], []
    for w, slots, threshold, fresh in hits:
        new = group(fresh)
        lines = [w.title, f"Free earlier than {fmt(threshold)}:", ""]
        for first, times in new[:15]:
            lines.append(f"  NEW  {day_label(first.start)}  {times}  {first.doctor}\n       {first.link}")
            push.append(f"{day_label(first.start)} {times.split(',')[0]} {first.doctor}")
        if len(new) > 15:
            lines.append(f"  ... and {len(new) - 15} more days")
        lines.append("\nEarliest free right now:")
        lines += [f"  {day_label(first.start)}  {times}  {first.doctor}" for first, times in group(slots[:40])[:5]]
        lines.append(f"\nBook: {w.link}")
        parts.append("\n".join(lines))
    body = "\n\n" + ("\n\n" + "-" * 40 + "\n\n").join(parts) + "\n\nSlots go fast – book right away.\n"
    return subject, body, "\n".join(push[:10]), s.link


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    to = os.environ.get("EMAIL_TO") or user
    if not (user and password and to):
        log("email not configured (SMTP_USER / SMTP_PASSWORD)")
        return False
    port = int(os.environ.get("SMTP_PORT") or 587)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or user
    msg["To"] = to
    msg.set_content(body)
    try:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
        with smtp:
            if port != 465:
                smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log("email failed:", type(e).__name__)
        detail(e)
        return False


def send_push(title, body, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    # JSON publish keeps non-ASCII titles intact (headers would need RFC 2047).
    msg = {"topic": topic, "title": title, "message": body[:3500], "priority": 4, "tags": ["tooth"]}
    if click:
        msg["click"] = click
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).close()
        return True
    except OSError as e:
        log("ntfy failed:", type(e).__name__)
        return False


def notify(subject, body, push_text=None, click=None):
    sent_email = send_email(subject, body)
    sent_push = send_push(subject, push_text or body.strip(), click)
    return sent_email or sent_push


def list_site(booking, text, now):
    """Print clinics / services / doctors with their earliest free slot."""
    t = norm(text or "")
    for clinic in booking.meta["clinics"]:
        services = [s for s in clinic["services"]
                    if not t or t in norm(clinic["name"] + " " + s["name"] + " " + " ".join(x["name"] for x in s["staffs"]))]
        if not services:
            continue
        log(f"\n{clinic['name']}   [{clinic['id']}]")
        for service in services:
            log(f"  {service['name']} ({service['duration']} min)   [{service['idService']}]"
                + ("   (login required)" if service.get("requiresAuth") else ""))
            earliest = {}
            for s in booking.slots(clinic, service, service["staffs"]):
                if s.start >= now:
                    earliest.setdefault(s.staff["idStaff"], s)
            for staff in sorted(service["staffs"], key=lambda x: earliest[x["idStaff"]].start if x["idStaff"] in earliest else dt.datetime.max.replace(tzinfo=booking.tz)):
                s = earliest.get(staff["idStaff"])
                log(f"      {(s.when() if s else 'nothing free'):<22} {' '.join(staff['name'].split())}")
            time.sleep(0.2)


def load_config():
    raw = os.environ.get("WATCH_CONFIG") or (CONFIG_PATH.read_text() if CONFIG_PATH.exists() else "")
    if not raw.strip():
        raise SystemExit("No config: set WATCH_CONFIG or create config.json (see config.example.json)")
    config = json.loads(raw)
    if isinstance(config, list):
        config = {"watches": config}
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", nargs="?", const="", metavar="TEXT", help="list clinics/services/doctors (filter by TEXT)")
    parser.add_argument("--site", help="xdent.cz subdomain (default: config 'site' or smilecentrum)")
    parser.add_argument("--dry-run", action="store_true", help="print only: no notification, no state change")
    parser.add_argument("--test-email", action="store_true", help="send a test email / push and exit")
    args = parser.parse_args()

    if args.test_email:
        ok = notify("Appointment watcher: test", "Notifications work.\n")
        log("Test notification sent." if ok else "Nothing sent - check SMTP_* / NTFY_TOPIC.")
        return 0 if ok else 1

    config = {} if args.list is not None and not (os.environ.get("WATCH_CONFIG") or CONFIG_PATH.exists()) else load_config()
    site = args.site or config.get("site") or "smilecentrum"
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

    try:
        booking = Booking(site)
        now = dt.datetime.now(booking.tz)
        if args.list is not None:
            list_site(booking, args.list, now)
            return 0
        watches = [Watch(spec, booking) for spec in config["watches"]]
        results, new_state = check(watches, booking, state, now)
    except SystemExit as e:
        if QUIET and isinstance(e.code, str):  # config errors quote the watched names
            log("config error (details hidden in public logs): run `python3 watcher.py --dry-run` locally with the same config")
            return 2
        raise
    except Exception as e:  # site down or API changed: say so once, not every 5 minutes
        log("check failed:", type(e).__name__)
        detail(repr(e))
        if args.dry_run:
            raise
        failing_since = dt.datetime.fromisoformat(state.get("failing_since") or dt.datetime.now(dt.timezone.utc).isoformat())
        state["failing_since"] = failing_since.isoformat(timespec="seconds")
        if not state.get("failure_alerted") and dt.datetime.now(dt.timezone.utc) - failing_since > dt.timedelta(hours=FAILURE_ALERT_HOURS):
            if notify("Appointment watcher is failing",
                      f"Every check since {failing_since:%d.%m. %H:%M} UTC has failed.\nLast error: {e!r}\n\n"
                      "Usually the booking site is down; if it keeps failing, its API probably changed.\n"):
                state["failure_alerted"] = True
        save_state(state)
        return 1

    for w, slots, threshold, fresh in results:
        log(f"watch: {len(slots)} free slots, {len(fresh)} new earlier" + ("" if threshold else " (first run, recording)"))
        detail(f"  {w.title} – {', '.join(' '.join(s['name'].split()) for s in w.staffs)}")
        detail(f"  threshold: {fmt(threshold)}")
        shown = slots[:5] + [s for s in fresh if s not in slots[:5]]
        for s in sorted(shown, key=lambda s: s.start):
            detail(("  NEW  " if s in fresh else "       ") + str(s))

    if any(fresh for *_, fresh in results):
        subject, body, push_text, click = render(results)
        if args.dry_run:
            detail("\n--- would send ---\n" + subject + "\n" + body + "\n--- push ---\n" + push_text)
            return 0
        if not notify(subject, body, push_text, click):
            log("notification failed; will retry next run")
            return 1  # keep the old state so these slots count as new next time
        log("notified")
    if args.dry_run:
        return 0
    if state.get("failure_alerted"):
        notify("Appointment watcher works again", "Checks are succeeding again.\n")
    save_state(new_state)
    return 0


def save_state(state):
    text = json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    old = STATE_PATH.read_text() if STATE_PATH.exists() else None
    if text != old:
        STATE_PATH.write_text(text)
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("state_changed=true\n")


def parse_before(value, tz):
    """'2026-12-15' (= before that day) or '2026-12-15 14:00'."""
    value = str(value).strip().replace("T", " ")
    if len(value) == 10:
        return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), tz)
    return dt.datetime.fromisoformat(value).replace(tzinfo=tz)


def fmt(when):
    if not when:
        return "-"
    return f"{day_label(when)} {when:%H:%M}"


def day_label(when):
    return f"{WEEKDAYS[when.weekday()].capitalize()} {when.day}.{when.month}.{when.year}"


def short_clinic(name):
    return name.replace("Smile Centrum", "").strip(" -") or name


def norm(text):
    text = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def minutes(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def as_list(value):
    return value if isinstance(value, list) else [value]


if __name__ == "__main__":
    sys.exit(main())
