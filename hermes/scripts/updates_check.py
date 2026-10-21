#!/usr/bin/env python3
"""Arch csomagfrissítések -> `updates` kártya (no-agent script, csak stdlib).

Forrás: `checkupdates` (pacman-contrib). A rendszer adatbázisát nem frissíti: egy
ideiglenes másolatot szinkronizál (CHECKUPDATES_DB, alapból /tmp/checkup-db-<uid>).
Kimenet soronként: `név régi -> új`. Kilépési kód: 0 = van frissítés,
2 = nincs frissítés, 1 = hiba.

Működés: checkupdates -> parszolás -> schemas.build_event("updates", ..., "script")
-> store.append_event. Minden futás publikál (naponta egyszer fut, a backend a
legutóbbit olvassa). Hiba esetén NEM publikál, csak stderr-re és a logfájlba ír.

Exit kódok: 0 = publikált, 1 = checkupdates hiba, 2 = parszolási hiba,
3 = validálási / tárolási hiba.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

CHECKUPDATES = "/usr/bin/checkupdates"
TIMEOUT_S = 180
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
PLUGIN_DIR = os.path.join(HERMES_HOME, "plugins", "agentcards")
LOG_PATH = os.path.join(HERMES_HOME, "logs", "updates_check.log")
LOG_MAX_BYTES = 256 * 1024
MAX_PACKAGES = 20
DATA_SOURCE = "checkupdates (pacman-contrib)"

_LINE = re.compile(r"^(?P<name>[A-Za-z0-9@._+-]+)\s+\S+\s+->\s+\S+$")


class ParseError(Exception):
    pass


def log(msg):
    """stderr + saját logfájl (a Hermes no-agent cron a stderr-t nem rögzíti)."""
    print(f"updates_check: {msg}", file=sys.stderr)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass  # a log hibája soha ne buktassa a futást


def parse(text):
    """checkupdates stdout -> csomagnevek a kiírás sorrendjében."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            raise ParseError(f"unexpected line: {line[:80]!r}")
        names.append(m["name"])
    return names


def to_payload(names, checked_at):
    return {
        "count": len(names),
        "packages": names[:MAX_PACKAGES],
        "checked_at": checked_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "data_source": DATA_SOURCE,
    }


def run_checkupdates():
    """-> (exit kód, stdout). 0 és 2 rendben, minden más hiba."""
    p = subprocess.run([CHECKUPDATES], capture_output=True, text=True, timeout=TIMEOUT_S,
                       check=False, stdin=subprocess.DEVNULL)
    if p.returncode not in (0, 2):
        tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"checkupdates exit {p.returncode}: {tail[0][:200]}")
    return p.returncode, p.stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="checkupdates-kimenetet olvas fájlból (offline)")
    ap.add_argument("--dry-run", action="store_true", help="nem ír JSONL-t")
    args = ap.parse_args(argv)

    try:
        if args.file:
            with open(args.file, encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            _, text = run_checkupdates()
    except Exception as e:
        log(f"check_failed: {type(e).__name__}: {e}")
        return 1

    try:
        names = parse(text)
    except ParseError as e:
        log(f"parse_failed: {e}")
        return 2
    payload = to_payload(names, dt.datetime.now(dt.timezone.utc))

    sys.path.insert(0, PLUGIN_DIR)
    try:
        import schemas  # noqa: E402  (agentcards, csak stdlib)
        if not args.dry_run:
            import store  # noqa: E402
    except ImportError as e:
        log(f"import_failed: {e} (PLUGIN_DIR={PLUGIN_DIR})")
        return 3

    try:
        ev = schemas.build_event("updates", payload, "info", "script")
    except schemas.CardValidationError as e:
        log(f"validation_failed: {getattr(e, 'errors', e)}")
        return 3

    if args.dry_run:
        print(json.dumps(ev, ensure_ascii=False))
        return 0

    try:
        store.append_event(ev)
    except OSError as e:
        log(f"store_failed: {type(e).__name__}: {e}")
        return 3
    log(f"published {ev.get('id')} count={payload['count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
