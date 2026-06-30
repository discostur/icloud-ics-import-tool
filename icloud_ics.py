#!/usr/bin/env python3
"""
icloud-ics — a CalDAV toolbox for getting an .ics file reliably into iCloud (or any
CalDAV server) and for diagnosing why a bulk import went wrong.

Importing a large .ics through the macOS/iOS Calendar app pushes each event to the
server one request at a time, gets throttled, and the local-vs-server reconciliation
silently drops events that failed to push — so events "disappear" and the import never
finishes. This tool uploads events itself over CalDAV: paced, retried, resumable, and
idempotent (it skips what's already on the server). It also ships the diagnostics we
needed along the way: analyse a file offline, list calendars, find which calendar a UID
lives in, diff a file against a calendar, and export a calendar back to .ics.

Credentials (resolved in this order: CLI flag > environment > .env file):
    ICLOUD_USER           Apple ID e-mail (the PRIMARY one you sign into iCloud with)
    ICLOUD_APP_PASSWORD   app-specific password from https://account.apple.com
                          (Sign-In & Security -> App-Specific Passwords), WITH dashes.
                          NOT your normal login password — that always fails under 2FA.

Quick start:
    icloud_ics.py analyze calendar.ics                 # offline health check, no login
    icloud_ics.py list                                 # all calendars + event counts
    icloud_ics.py diff calendar.ics --calendar "Home"  # what's missing on the server
    icloud_ics.py import calendar.ics --calendar "Home" --create --dry-run
    icloud_ics.py import calendar.ics --calendar "Home" --create

Works against any RFC 4791 CalDAV server via --server; defaults to iCloud.
"""

import argparse
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urljoin

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is required:  pip install -r requirements.txt")

DEFAULT_SERVER = "https://caldav.icloud.com/"
PRODID = "-//icloud-ics-import-tool//EN"

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "i": "http://apple.com/ns/ical/",
}

RETRY_STATUS = {429, 500, 502, 503, 504, 507}

# Properties stripped by --strip-bloat (everything else is preserved verbatim).
BLOAT_PREFIXES = (
    "X-APPLE-DEFAULT-ALARM",
    "X-APPLE-LOCAL-DEFAULT-ALARM",
    "X-WR-ALARMUID",
    "X-APPLE-CREATOR-IDENTITY",
    "X-APPLE-CREATOR-TEAM-IDENTITY",
)


# ============================================================ iCalendar parsing

def read_unfolded(path):
    """Read an .ics file, normalize line endings, and unfold RFC 5545 folded lines."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)  # a line starting with space/tab continues prev
    return text.split("\n")


def parse_components(lines):
    """Extract top-level VTIMEZONE and VEVENT blocks.

    Returns (vtimezones: {tzid: [lines]}, events: [[lines], ...]). VEVENT blocks are
    kept verbatim, including any nested VALARM sub-components.
    """
    vtimezones, events = {}, []
    block, kind, depth = None, None, 0
    for ln in lines:
        if kind is None:
            if ln == "BEGIN:VTIMEZONE":
                kind, block, depth = "vtz", [ln], 1
            elif ln == "BEGIN:VEVENT":
                kind, block, depth = "vevent", [ln], 1
            continue
        block.append(ln)
        if ln.startswith("BEGIN:"):
            depth += 1
        elif ln.startswith("END:"):
            depth -= 1
            if depth == 0:
                if kind == "vtz":
                    tzid = next((l[5:] for l in block if l.startswith("TZID:")), None)
                    if tzid and tzid not in vtimezones:
                        vtimezones[tzid] = block
                else:
                    events.append(block)
                block, kind = None, None
    return vtimezones, events


def prop_line(block, name):
    for ln in block:
        if ln.startswith(name + ":") or ln.startswith(name + ";"):
            return ln
    return None


def prop_val(block, name):
    ln = prop_line(block, name)
    if ln is None or ":" not in ln:
        return None
    return ln.split(":", 1)[1]


def seq_of(block):
    try:
        return int(prop_val(block, "SEQUENCE"))
    except (TypeError, ValueError):
        return 0


def is_sound_attach(line):
    """ATTACH lines that are bare alarm-sound names (e.g. ATTACH;VALUE=URI:Basso)."""
    if not line.startswith("ATTACH") or "VALUE=URI:" not in line:
        return False
    val = line.split("VALUE=URI:", 1)[1]
    return "/" not in val and not val.lower().startswith("http")


def clean_event(block):
    """Drop bloat: default-alarm X-props, alarm-sound refs, dead Exchange/EWS attachments."""
    out = []
    for ln in block:
        if ln.startswith(BLOAT_PREFIXES):
            continue
        if ln.startswith("ATTACH") and "X-APPLE-EWS-ATTACHMENT" in ln:
            continue
        if is_sound_attach(ln):
            continue
        out.append(ln)
    return out


def regenerate_uid(block, new_uid, old_uid):
    """Replace the VEVENT-level UID (not VALARM UIDs) with new_uid."""
    out, depth = [], 0
    for ln in block:
        if ln.startswith("BEGIN:"):
            depth += 1
        elif ln.startswith("END:"):
            depth -= 1
        # depth 1 == directly inside the VEVENT (VALARM pushes depth to 2)
        if depth == 1 and ln == "UID:" + old_uid:
            out.append("UID:" + new_uid)
        else:
            out.append(ln)
    return out


def group_by_uid(events, on_drop=None):
    """Group events by UID, merging RECURRENCE-ID overrides into one resource.

    On a true (UID, RECURRENCE-ID) collision the higher SEQUENCE wins (tie-break
    LAST-MODIFIED); the loser is reported via on_drop(uid, rid).
    """
    groups = {}
    for ev in events:
        uid = prop_val(ev, "UID")
        if uid is None:
            continue
        rid = prop_line(ev, "RECURRENCE-ID")
        slot = groups.setdefault(uid, {})
        if rid in slot:
            keep = slot[rid]
            newer = seq_of(ev) > seq_of(keep) or (
                seq_of(ev) == seq_of(keep)
                and (prop_val(ev, "LAST-MODIFIED") or "") > (prop_val(keep, "LAST-MODIFIED") or "")
            )
            slot[rid] = ev if newer else keep
            if on_drop:
                on_drop(uid, rid)
        else:
            slot[rid] = ev
    return groups


def referenced_tzids(blocks):
    tzids = set()
    for b in blocks:
        for ln in b:
            tzids.update(re.findall(r"TZID=([^:;]+)", ln))
    return tzids


def build_resource(blocks, vtimezones):
    """Wrap event block(s) in a minimal VCALENDAR (CRLF), including needed VTIMEZONEs."""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID, "CALSCALE:GREGORIAN"]
    for tzid in sorted(referenced_tzids(blocks)):
        if tzid in vtimezones:
            out.extend(vtimezones[tzid])
    for b in blocks:
        out.extend(b)
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


# ============================================================ credentials / .env

def load_dotenv(path):
    env = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_credentials(args):
    dotenv = load_dotenv(args.env)
    user = args.user or os.environ.get("ICLOUD_USER") or dotenv.get("ICLOUD_USER")
    pw = args.password or os.environ.get("ICLOUD_APP_PASSWORD") or dotenv.get("ICLOUD_APP_PASSWORD")
    server = args.server or os.environ.get("ICLOUD_SERVER") or dotenv.get("ICLOUD_SERVER") or DEFAULT_SERVER
    if not user or not pw:
        sys.exit("Missing credentials. Set ICLOUD_USER and ICLOUD_APP_PASSWORD via --user/"
                 "--password, environment, or a .env file. See --help.")
    return user, pw, server


# ============================================================ CalDAV client

class Client:
    def __init__(self, user, password, server, timeout=60, verbose=False):
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.server = server
        self.timeout = timeout
        self.verbose = verbose
        self.home = None

    def _req(self, method, url, body=None, depth=None, ctype="application/xml; charset=utf-8"):
        headers = {}
        if depth is not None:
            headers["Depth"] = depth
        if body is not None:
            headers["Content-Type"] = ctype
        data = body.encode("utf-8") if isinstance(body, str) else body
        if self.verbose:
            sys.stderr.write(f"  {method} {url}\n")
        r = self.s.request(method, url, data=data, headers=headers, timeout=self.timeout)
        return r

    def discover(self):
        """Resolve the calendar-home-set, following partition redirects (e.g. pNN-caldav)."""
        if self.home:
            return self.home
        body = ('<d:propfind xmlns:d="DAV:"><d:prop>'
                '<d:current-user-principal/></d:prop></d:propfind>')
        r = self._req("PROPFIND", self.server, body, depth="0")
        if r.status_code == 401:
            sys.exit("401 Unauthorized. With 2FA on you MUST use an app-specific password "
                     "(account.apple.com), not your login password, and the PRIMARY Apple ID e-mail.")
        r.raise_for_status()
        principal = self._href(r.text, "{DAV:}current-user-principal")
        if not principal:
            sys.exit("Could not resolve current-user-principal.")
        principal = urljoin(r.url, principal)
        body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><c:calendar-home-set/></d:prop></d:propfind>')
        r = self._req("PROPFIND", principal, body, depth="0")
        r.raise_for_status()
        home = self._href(r.text, "{urn:ietf:params:xml:ns:caldav}calendar-home-set")
        if not home:
            sys.exit("Could not resolve calendar-home-set.")
        self.home = urljoin(r.url, home)
        return self.home

    @staticmethod
    def _href(xml_text, prop_tag):
        root = ET.fromstring(xml_text)
        for el in root.iter():
            if el.tag == prop_tag:
                h = el.find(".//d:href", NS)
                if h is not None and h.text:
                    return h.text.strip()
        return None

    def calendars(self):
        """Return list of dicts: {name, url, color}."""
        home = self.discover()
        body = ('<d:propfind xmlns:d="DAV:" xmlns:i="http://apple.com/ns/ical/"><d:prop>'
                '<d:resourcetype/><d:displayname/><i:calendar-color/></d:prop></d:propfind>')
        r = self._req("PROPFIND", home, body, depth="1")
        r.raise_for_status()
        out = []
        for resp in ET.fromstring(r.text).findall("d:response", NS):
            rtype = resp.find(".//d:resourcetype", NS)
            if rtype is None or rtype.find("c:calendar", NS) is None:
                continue
            href = resp.find("d:href", NS)
            name = resp.find(".//d:displayname", NS)
            color = resp.find(".//i:calendar-color", NS)
            out.append({
                "url": urljoin(r.url, href.text.strip()),
                "name": (name.text if name is not None else "") or "",
                "color": (color.text if color is not None else "") or "",
            })
        return out

    def find_calendar(self, name):
        return next((c for c in self.calendars() if c["name"] == name), None)

    def resource_uids(self, calendar_url):
        """Set of event UIDs in a calendar (resources are named <UID>.ics on iCloud)."""
        body = '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        r = self._req("PROPFIND", calendar_url, body, depth="1")
        r.raise_for_status()
        uids = set()
        for h in ET.fromstring(r.text).iter("{DAV:}href"):
            if h.text and h.text.endswith(".ics"):
                uids.add(unquote(h.text.rsplit("/", 1)[-1][:-4]))
        return uids

    def hrefs(self, calendar_url):
        body = '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        r = self._req("PROPFIND", calendar_url, body, depth="1")
        r.raise_for_status()
        return [h.text for h in ET.fromstring(r.text).iter("{DAV:}href")
                if h.text and h.text.endswith(".ics")]

    def make_calendar(self, name, color="#34AADC"):
        home = self.discover()
        slug = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-").lower() or "calendar"
        url = urljoin(home if home.endswith("/") else home + "/", slug + "/")
        body = (f'<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                f'xmlns:i="http://apple.com/ns/ical/"><d:set><d:prop>'
                f'<d:displayname>{name}</d:displayname>'
                f'<i:calendar-color>{color}</i:calendar-color>'
                f'<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
                f'</c:supported-calendar-component-set></d:prop></d:set></c:mkcalendar>')
        r = self._req("MKCALENDAR", url, body)
        if r.status_code not in (200, 201):
            sys.exit(f"MKCALENDAR failed: {r.status_code} {r.text[:300]}")
        return url

    def delete_calendar(self, calendar_url):
        r = self._req("DELETE", calendar_url)
        if r.status_code not in (200, 204, 404):
            sys.exit(f"DELETE failed: {r.status_code} {r.text[:300]}")
        return r.status_code

    def get(self, url):
        return self._req("GET", url)

    def put_event(self, calendar_url, uid, payload, delay=0.0, max_retries=6):
        url = urljoin(calendar_url, quote(uid, safe="") + ".ics")
        last = ""
        for attempt in range(max_retries):
            try:
                r = self.s.put(url, data=payload.encode("utf-8"),
                               headers={"Content-Type": "text/calendar; charset=utf-8"},
                               timeout=self.timeout)
                if r.status_code in (200, 201, 204):
                    if delay:
                        time.sleep(delay)
                    return True, str(r.status_code)
                if r.status_code in RETRY_STATUS:
                    ra = r.headers.get("Retry-After")
                    time.sleep(min(int(ra), 60) if ra and ra.isdigit() else min(2 ** attempt, 30))
                    continue
                return False, f"{r.status_code} {r.text[:160]}"
            except requests.RequestException as e:
                last = str(e)
                time.sleep(min(2 ** attempt, 30))
        return False, f"gave up after {max_retries} tries ({last or 'throttled'})"


# ============================================================ subcommands

def cmd_analyze(args):
    """Offline health check of an .ics file — no network, no credentials."""
    lines = read_unfolded(args.file)
    raw = "\n".join(lines)
    vtz, events = parse_components(lines)
    groups = group_by_uid(events)
    no_uid = sum(1 for e in events if prop_val(e, "UID") is None)
    overrides = sum(1 for e in events if prop_line(e, "RECURRENCE-ID"))
    parts = sum(len(s) for s in groups.values())

    # attachments
    sound = sum(1 for e in events for ln in e if is_sound_attach(ln))
    ews = raw.count("X-APPLE-EWS-ATTACHMENT")
    real_attach = sum(1 for e in events for ln in e
                      if ln.startswith("ATTACH") and not is_sound_attach(ln)
                      and "X-APPLE-EWS-ATTACHMENT" not in ln)

    # timezones
    used = set()
    for e in events:
        used.update(referenced_tzids([e]))
    undefined = used - set(vtz)

    # dates
    years = {}
    for e in events:
        m = re.search(r"(\d{4})\d{4}", prop_val(e, "DTSTART") or "")
        if m:
            years[m.group(1)] = years.get(m.group(1), 0) + 1

    valid_utf8 = True
    try:
        open(args.file, "rb").read().decode("utf-8")
    except UnicodeDecodeError:
        valid_utf8 = False
    max_line = max((len(l) for l in raw.split("\n")), default=0)

    print(f"file:               {args.file}")
    print(f"valid UTF-8:        {valid_utf8}")
    print(f"max line (unfolded):{max_line}  (informational; folding is handled on import)")
    print(f"VEVENTs:            {len(events)}")
    print(f"  without UID:      {no_uid}")
    print(f"  recurrence overrides (RECURRENCE-ID): {overrides}")
    print(f"unique UIDs (= server resources): {len(groups)}")
    print(f"event parts kept:   {parts}  (parts + deduped collisions == VEVENTs)")
    print(f"VTIMEZONE defs:     {sorted(vtz)}")
    print(f"TZIDs referenced:   {sorted(used)}")
    if undefined:
        print(f"  !! referenced but UNDEFINED timezones: {sorted(undefined)}")
    print(f"attachments: alarm-sound refs={sound}  dead Exchange/EWS={ews}  real files={real_attach}")
    if years:
        print("events per year:")
        for y in sorted(years):
            print(f"  {y}: {years[y]}")
    print("\nVerdict: " + ("looks importable." if not undefined and valid_utf8
                            else "review the !! warnings above before importing."))


def cmd_list(args):
    c = client(args)
    cals = c.calendars()
    print(f"calendar-home-set: {c.home}\n")
    width = max((len(x["name"]) for x in cals), default=10)
    for cal in cals:
        n = len(c.resource_uids(cal["url"])) if not args.no_count else "?"
        print(f"  {cal['name']:<{width}}  events={n:<6} {cal['color']:<8} {cal['url']}")


def cmd_find(args):
    c = client(args)
    hits = []
    for cal in c.calendars():
        if args.uid in c.resource_uids(cal["url"]):
            hits.append(cal["name"])
    if hits:
        print(f"UID {args.uid} found in: {', '.join(hits)}")
        print("(iCloud enforces account-wide unique UIDs — a UID already here causes a 412 "
              "when you PUT it into a different calendar.)")
    else:
        print(f"UID {args.uid} not found in any calendar.")


def cmd_diff(args):
    c = client(args)
    cal = c.find_calendar(args.calendar)
    if not cal:
        sys.exit(f"Calendar {args.calendar!r} not found. Use 'list'.")
    server = c.resource_uids(cal["url"])
    _, events = parse_components(read_unfolded(args.file))
    file_uids = set(group_by_uid(events))
    missing = file_uids - server
    extra = server - file_uids
    print(f"file unique UIDs : {len(file_uids)}")
    print(f"calendar events  : {len(server)}")
    print(f"MISSING on server (in file, not uploaded): {len(missing)}")
    print(f"extra on server  (not in file)           : {len(extra)}")
    for u in list(missing)[:args.sample]:
        print(f"  missing: {u}")


def cmd_export(args):
    c = client(args)
    cal = c.find_calendar(args.calendar)
    if not cal:
        sys.exit(f"Calendar {args.calendar!r} not found. Use 'list'.")
    hrefs = c.hrefs(cal["url"])
    print(f"exporting {len(hrefs)} resources from {args.calendar!r} ...", file=sys.stderr)
    vtz, events = {}, []
    for i, h in enumerate(hrefs, 1):
        r = c.get(urljoin(cal["url"], h.rsplit("/", 1)[-1]))
        if r.status_code == 200:
            v, e = parse_components(read_unfolded_text(r.text))
            vtz.update(v)
            events.extend(e)
        if args.delay:
            time.sleep(args.delay)
        if i % 200 == 0:
            print(f"  {i}/{len(hrefs)}", file=sys.stderr)
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID, "CALSCALE:GREGORIAN"]
    for block in vtz.values():
        out.extend(block)
    for e in events:
        out.extend(e)
    out.append("END:VCALENDAR")
    data = "\r\n".join(out) + "\r\n"
    if args.output:
        open(args.output, "w", encoding="utf-8").write(data)
        print(f"wrote {len(events)} events -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(data)


def cmd_create(args):
    c = client(args)
    if c.find_calendar(args.name):
        sys.exit(f"Calendar {args.name!r} already exists.")
    url = c.make_calendar(args.name, args.color)
    print(f"created {args.name!r} -> {url}")


def cmd_delete(args):
    c = client(args)
    cal = c.find_calendar(args.calendar)
    if not cal:
        sys.exit(f"Calendar {args.calendar!r} not found.")
    if not args.yes:
        sys.exit(f"Refusing to delete {args.calendar!r} without --yes "
                 f"({len(c.resource_uids(cal['url']))} events would be lost).")
    print(f"DELETE {args.calendar!r} -> {c.delete_calendar(cal['url'])}")


def cmd_import(args):
    lines = read_unfolded(args.file)
    vtz, events = parse_components(lines)
    if args.strip_bloat:
        events = [clean_event(e) for e in events]
    dropped = []
    groups = group_by_uid(events, lambda u, r: dropped.append(u))
    print(f"parsed {len(events)} VEVENTs -> {len(groups)} resources "
          f"(deduped {len(dropped)} collision(s)); timezones {sorted(vtz)}")

    if args.regenerate_uids:
        new_groups = {}
        for uid, slot in groups.items():
            nu = str(uuid.uuid4()).upper()
            new_groups[nu] = {rid: regenerate_uid(b, nu, uid) for rid, b in slot.items()}
        groups = new_groups
        print("regenerated all UIDs (avoids cross-calendar 412 conflicts)")

    if args.dry_run:
        sample_dir = args.sample_dir
        for i, (uid, slot) in enumerate(list(groups.items())[:3]):
            payload = build_resource(list(slot.values()), vtz)
            if sample_dir:
                p = os.path.join(sample_dir, f"sample_{i}.ics")
                open(p, "w", encoding="utf-8").write(payload)
                print(f"  sample -> {p} (uid={uid[:36]}, parts={len(slot)})")
        try:
            c = client(args)
            c.discover()
            cal = c.find_calendar(args.calendar)
            print(f"discovery OK. home={c.home}")
            print(f"existing calendars: {[x['name'] for x in c.calendars()]}")
            print(f"target {args.calendar!r}: " + ("EXISTS" if cal else
                  ("will be created" if args.create else "MISSING (pass --create)")))
        except SystemExit as e:
            print(f"discovery: {e}")
        print("dry-run complete. nothing written.")
        return

    c = client(args)
    cal = c.find_calendar(args.calendar)
    if cal:
        coll = cal["url"]
        print(f"using existing calendar {args.calendar!r}: {coll}")
    elif args.create:
        coll = c.make_calendar(args.calendar)
        print(f"created calendar {args.calendar!r}: {coll}")
    else:
        sys.exit(f"Calendar {args.calendar!r} not found. Pass --create to make it.")

    done = load_done(args.log) if args.resume else set()
    if not args.no_skip_existing:
        server = c.resource_uids(coll)
        print(f"calendar holds {len(server)} events already — those UIDs are skipped")
        done |= server

    logf = open(args.log, "a", encoding="utf-8") if args.log else None
    total = len(groups)
    ok = skipped = failed = 0
    failures = []
    for i, (uid, slot) in enumerate(groups.items(), 1):
        if uid in done:
            skipped += 1
            continue
        if args.max and (ok + failed) >= args.max:
            print(f"--max {args.max} reached; stopping.")
            break
        payload = build_resource(list(slot.values()), vtz)
        success, detail = c.put_event(coll, uid, payload, args.delay, args.max_retries)
        if logf:
            logf.write(f"{uid}\t{'OK' if success else 'FAIL'}\t{detail}\n")
            logf.flush()
        if success:
            ok += 1
        else:
            failed += 1
            failures.append((uid, detail))
        if i % 100 == 0 or i == total:
            print(f"[{i}/{total}] ok={ok} fail={failed} skipped={skipped}")
    if logf:
        logf.close()

    print(f"\n=== summary === resources={total} OK={ok} failed={failed} skipped={skipped}")
    for uid, detail in failures[:20]:
        print(f"  FAIL {uid}  {detail}")
    if failed:
        print(f"rerun the same command to retry the {failed} failure(s) "
              f"(already-uploaded events are skipped automatically).")


def read_unfolded_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    return text.split("\n")


def load_done(path):
    done = set()
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[1] == "OK":
                done.add(p[0])
    return done


def client(args):
    user, pw, server = resolve_credentials(args)
    c = Client(user, pw, server, timeout=args.timeout, verbose=args.verbose)
    c.discover()
    return c


# ============================================================ CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="icloud_ics.py",
        description="CalDAV toolbox for reliable .ics import into iCloud (or any CalDAV server).")
    p.add_argument("--user", help="Apple ID e-mail (or ICLOUD_USER / .env)")
    p.add_argument("--password", help="app-specific password (or ICLOUD_APP_PASSWORD / .env)")
    p.add_argument("--server", help=f"CalDAV base URL (default {DEFAULT_SERVER})")
    p.add_argument("--env", default=".env", help="path to a KEY=VALUE credentials file")
    p.add_argument("--timeout", type=int, default=60, help="per-request timeout (s)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="offline health check of an .ics file (no login)")
    a.add_argument("file")
    a.set_defaults(func=cmd_analyze)

    l = sub.add_parser("list", help="list calendars with event counts")
    l.add_argument("--no-count", action="store_true", help="skip per-calendar counting (faster)")
    l.set_defaults(func=cmd_list)

    f = sub.add_parser("find", help="locate which calendar(s) hold a UID")
    f.add_argument("uid")
    f.set_defaults(func=cmd_find)

    d = sub.add_parser("diff", help="compare an .ics file against a calendar")
    d.add_argument("file")
    d.add_argument("--calendar", required=True)
    d.add_argument("--sample", type=int, default=10, help="how many missing UIDs to print")
    d.set_defaults(func=cmd_diff)

    i = sub.add_parser("import", help="upload an .ics into a calendar (paced, resumable)")
    i.add_argument("file")
    i.add_argument("--calendar", required=True)
    i.add_argument("--create", action="store_true", help="create the calendar if missing")
    i.add_argument("--dry-run", action="store_true", help="parse + validate, write nothing")
    i.add_argument("--strip-bloat", action="store_true",
                   help="drop alarm-sound refs, dead Exchange attachments, default-alarm X-props")
    i.add_argument("--regenerate-uids", action="store_true",
                   help="assign fresh UIDs (use when a 412 says the UID exists elsewhere)")
    i.add_argument("--delay", type=float, default=0.3, help="seconds between PUTs (throttle)")
    i.add_argument("--max-retries", type=int, default=6)
    i.add_argument("--max", type=int, default=0, help="upload at most N resources (0 = all)")
    i.add_argument("--resume", action="store_true", help="also skip UIDs logged OK previously")
    i.add_argument("--no-skip-existing", action="store_true",
                   help="re-PUT every event instead of skipping what's on the server")
    i.add_argument("--log", default="import.log", help="progress/resume log (set '' to disable)")
    i.add_argument("--sample-dir", default=".", help="where --dry-run writes sample .ics files")
    i.set_defaults(func=cmd_import)

    e = sub.add_parser("export", help="download a calendar to an .ics file")
    e.add_argument("--calendar", required=True)
    e.add_argument("-o", "--output", help="output file (default: stdout)")
    e.add_argument("--delay", type=float, default=0.0)
    e.set_defaults(func=cmd_export)

    cr = sub.add_parser("create", help="create a new calendar")
    cr.add_argument("name")
    cr.add_argument("--color", default="#34AADC")
    cr.set_defaults(func=cmd_create)

    de = sub.add_parser("delete", help="delete a calendar (irreversible)")
    de.add_argument("calendar")
    de.add_argument("--yes", action="store_true", help="confirm deletion")
    de.set_defaults(func=cmd_delete)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
