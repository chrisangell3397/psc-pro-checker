#!/usr/bin/env python3
"""
Paper Court SC - PRO duty watcher
=================================
Logs in to the members portal, loads the roster page already filtered to PRO
duties with open slots, keeps only Saturday-afternoon slots, and notifies you
about slots it hasn't told you about before.

Setup
-----
    pip install requests beautifulsoup4 python-dateutil

    export PSC_USERNAME="your login"
    export PSC_PASSWORD="your password"

Notifications appear as a macOS notification banner. Nothing extra is needed;
optionally `brew install terminal-notifier` so that clicking the banner opens
the roster page. If no banner appears, check System Settings > Notifications
for "Script Editor" (or "terminal-notifier") and allow notifications.

Run
---
    python psc_pro_checker.py              # normal run (what cron should call)
    python psc_pro_checker.py --debug      # saves roster HTML + shows what was parsed
    python psc_pro_checker.py --dry-run    # print matches, don't notify or save state
    python psc_pro_checker.py --all        # report every match, not just new ones

Cron example (hourly, on the hour):
    0 * * * * cd /home/you/psc && /usr/bin/python3 psc_pro_checker.py >> psc.log 2>&1

NOTE: the login and roster parsing are written without having seen the pages,
so they use heuristics. If something doesn't match, run with --debug and adjust
the CONFIG section or the parse_roster() function.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = "https://members.papercourt-sc.org.uk"
LOGIN_URL = f"{BASE}/login?redirect=%2Fportal"
ROSTER_URL = f"{BASE}/roster?utf8=%E2%9C%93&type=2651&event_type=&tag=&needed=1"

SATURDAY = 5                      # Monday=0 ... Sunday=6
AFTERNOON_FROM = time(12, 0)      # slots starting at or after this count as "afternoon"

STATE_FILE = Path(os.environ.get("PSC_STATE", "seen_slots.json"))
DEBUG_HTML = Path("last_roster.html")

# If auto-detection of the login field names fails, set these env vars.
USER_FIELD_OVERRIDE = os.environ.get("PSC_USER_FIELD")   # e.g. "user[email]"
HEADERS = {
    # A 406 usually means the server rejects unfamiliar clients or Accept headers,
    # so send the same basic headers an ordinary browser would.
    "User-Agent": os.environ.get(
        "PSC_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.8",
}

# ----------------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------------
def login(session: requests.Session, username: str, password: str) -> None:
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    pw_input = soup.find("input", {"type": "password"})
    if pw_input is None:
        raise RuntimeError("Couldn't find a password field on the login page.")
    form = pw_input.find_parent("form")
    if form is None:
        raise RuntimeError("Password field isn't inside a <form>.")

    data = {}
    user_field = USER_FIELD_OVERRIDE
    for inp in form.find_all("input"):
        name = inp.get("name")
        itype = (inp.get("type") or "text").lower()
        if not name:
            continue
        if itype == "hidden":                      # CSRF token etc.
            data[name] = inp.get("value", "")
        elif itype == "submit":
            data[name] = inp.get("value", "")
        elif itype in ("text", "email") and user_field is None:
            user_field = name

    if user_field is None:
        raise RuntimeError("Couldn't detect the username field; set PSC_USER_FIELD.")

    data[user_field] = username
    data[pw_input["name"]] = password

    action = urljoin(r.url, form.get("action") or r.url)
    resp = session.post(action, data=data, timeout=30, allow_redirects=True)
    resp.raise_for_status()


def fetch_roster(session: requests.Session) -> str:
    r = session.get(ROSTER_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    if "/login" in r.url or soup.find("input", {"type": "password"}):
        raise RuntimeError("Login appears to have failed (got the login page back).")
    return r.text

# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
DATE_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+\d{{4}})?"   # 7 Nov 2026
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"  # Nov 7, 2026
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"                               # 07/11/2026
    r"|\b\d{4}-\d{2}-\d{2}\b",                                     # 2026-11-07
    re.I,
)
TIME_RE = re.compile(
    r"\b(?:([01]?\d|2[0-3]):([0-5]\d)\s*(am|pm)?|(1[0-2]|0?[1-9])\s*(am|pm))\b",
    re.I,
)


def find_date(text: str, today: date):
    m = DATE_RE.search(text)
    if not m:
        return None
    s = m.group(0)
    explicit_year = bool(re.search(r"\d{4}", s)) or s.count("/") == 2
    try:
        d = dtparse.parse(s, dayfirst=True, default=datetime(today.year, 1, 1)).date()
    except (ValueError, OverflowError):
        return None
    if not explicit_year and d < today - timedelta(days=14):
        d = d.replace(year=d.year + 1)
    return d


def find_time(text: str):
    m = TIME_RE.search(text)
    if not m:
        return None
    if m.group(1) is not None:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        h, mi, ap = int(m.group(4)), 0, m.group(5)
    if ap:
        ap = ap.lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
    return time(h, mi)


def parse_roster(html: str, today: date, debug: bool = False):
    """Return a list of dicts: {date, time, text} for every slot-looking row."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr") or soup.find_all(["li", "article"]) or soup.find_all("div")

    slots = []
    carried_date = None
    for row in rows:
        text = " ".join(row.get_text(" ", strip=True).split())
        if not text:
            continue
        d = find_date(text, today)
        t = find_time(text)
        if d:
            carried_date = d
        use_date = d or carried_date
        # A date with no time is treated as a heading row that applies to
        # the rows below it; a row with a time is a slot.
        if use_date and t:
            slots.append({"date": use_date, "time": t, "text": text[:160]})
        elif debug:
            print(f"  [skipped row] {text[:100]}")
    return slots


def is_wanted(slot) -> bool:
    return slot["date"].weekday() == SATURDAY and slot["time"] >= AFTERNOON_FROM

# ----------------------------------------------------------------------------
# State (so you're only told about *new* slots)
# ----------------------------------------------------------------------------
def slot_keys(slots):
    counts, keys = {}, []
    for s in slots:
        base = f"{s['date'].isoformat()}|{s['time'].strftime('%H:%M')}"
        counts[base] = counts.get(base, 0) + 1
        keys.append(f"{base}|{counts[base]}")
    return keys


def load_state() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(keys) -> None:
    STATE_FILE.write_text(json.dumps(sorted(keys)))

# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------
def describe(slot) -> str:
    return f"{slot['date']:%a %d %b %Y} {slot['time']:%H:%M} - {slot['text']}"


def mac_notify(title: str, message: str) -> bool:
    """Show a macOS notification. Returns True if it appeared to succeed.

    Uses terminal-notifier if installed (brew install terminal-notifier), which
    lets a click open the roster page; otherwise falls back to the built-in
    osascript, which needs nothing extra.
    """
    tn = shutil.which("terminal-notifier")
    try:
        if tn:
            cmd = [tn, "-title", "Paper Court SC", "-subtitle", title,
                   "-message", message, "-open", ROSTER_URL, "-sound", "Glass"]
            subprocess.run(cmd, check=True, timeout=15)
        else:
            # Values are passed as arguments, never spliced into the script text,
            # so quotes in the slot text can't break it.
            script = (
                "on run argv\n"
                "display notification (item 1 of argv) with title \"Paper Court SC\" "
                "subtitle (item 2 of argv) sound name \"Glass\"\n"
                "end run"
            )
            subprocess.run(["osascript", "-e", script, message, title],
                           check=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        print(f"WARNING: couldn't show notification: {e}", file=sys.stderr)
        return False


def notify(slots) -> None:
    title = f"{len(slots)} PRO Saturday-afternoon slot(s) open"
    # Notification banners are small, so keep each line short.
    short = "\n".join(f"{s['date']:%a %d %b} {s['time']:%H:%M}" for s in slots)
    full = "\n".join(describe(s) for s in slots) + f"\n\n{ROSTER_URL}"

    print(title)
    print(full)

    mac_notify(title, short)

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="save roster HTML and show parsing detail")
    ap.add_argument("--dry-run", action="store_true", help="don't notify or update saved state")
    ap.add_argument("--all", action="store_true", help="report all matches, not only new ones")
    args = ap.parse_args()

    username = os.environ.get("PSC_USERNAME")
    password = os.environ.get("PSC_PASSWORD")
    if not username or not password:
        print("Set PSC_USERNAME and PSC_PASSWORD environment variables.", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        login(session, username, password)
        html = fetch_roster(session)
    except (requests.RequestException, RuntimeError) as e:
        print(f"{datetime.now():%Y-%m-%d %H:%M} ERROR: {e}", file=sys.stderr)
        return 1

    if args.debug:
        DEBUG_HTML.write_text(html, encoding="utf-8")
        print(f"Saved roster HTML to {DEBUG_HTML}")

    today = date.today()
    all_slots = parse_roster(html, today, debug=args.debug)
    wanted = [s for s in all_slots if is_wanted(s)]

    if args.debug:
        print(f"Parsed {len(all_slots)} slot rows, {len(wanted)} match Saturday-afternoon:")
        for s in all_slots:
            print(("  * " if is_wanted(s) else "    ") + describe(s))

    keys = slot_keys(wanted)
    seen = load_state()
    new = [s for s, k in zip(wanted, keys) if k not in seen]
    to_report = wanted if args.all else new

    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    if to_report:
        if args.dry_run:
            print(f"{stamp} (dry run) would notify about {len(to_report)} slot(s):")
            for s in to_report:
                print("  " + describe(s))
        else:
            notify(to_report)
    else:
        print(f"{stamp} no new matching slots ({len(wanted)} currently open).")

    if not args.dry_run:
        # Store only what's open now, so a slot that is taken and later
        # re-opened will trigger a fresh alert.
        save_state(keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
