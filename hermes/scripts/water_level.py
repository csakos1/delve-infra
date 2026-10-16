#!/usr/bin/env python3
"""Balaton vízállás -> `water` kártya (no-agent script, csak stdlib).

Forrás: OVSZ hydroinfo tó-tábla (statikus HTML), állomáskódra horgonyozva:
  442550 = Siófok, 442600 = Balaton average.
Oszlopok: kód, név, tó, tegnap reggel, tegnap este, ma reggel, 24 h változás,
reggeli vízhő, jég. Hiányzó adat: "//".

Működés: lekérés -> parszolás -> ellenőrzés -> dedup (observed dátum) ->
schemas.build_event("water", ..., "info", "script") -> store.append_event.
Hiba esetén NEM publikál, csak stderr-re logol.

Exit kódok: 0 = publikált / nincs új adat, 1 = lekérési hiba,
2 = parszolási hiba, 3 = validálási / tárolási / elavult-adat hiba.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

URL = "https://www.hydroinfo.hu/tables/ENG/tohid.html"
TZ = ZoneInfo("Europe/Budapest")
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
PLUGIN_DIR = os.path.join(HERMES_HOME, "plugins", "agentcards")
STATE_PATH = os.path.join(HERMES_HOME, "cards", "water_state.json")

SIOFOK, LAKE_AVG = "442550", "442600"
STATIONS = {SIOFOK: "Siófok", LAKE_AVG: "Balaton average"}
READING_HOUR = 7            # konvenció: "reggeli" leolvasás 07:00 helyi idő
MAX_BYTES = 256 * 1024
LEVEL_RANGE = (-100, 300)   # cm, épségellenőrzés (rekord: ~23..~192 cm)
CHANGE_RANGE = (-50, 50)    # cm / 24 h
MAX_AGE_DAYS = 2            # ennél régebbi observed dátum = elavult forrás
DATA_SOURCE = "hydroinfo.hu (OVSZ) tohid"
USER_AGENT = "agentcards-water/0.1 (personal use, few requests/day)"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_OBS = re.compile(r"Observed on:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_INT = re.compile(r"[+-]?\d+")


class ParseError(Exception):
    pass


LOG_PATH = os.path.join(HERMES_HOME, "logs", "water_level.log")
LOG_MAX_BYTES = 256 * 1024


def log(msg):
    """stderr + saját logfájl (a Hermes no-agent cron a stderr-t nem rögzíti)."""
    print(f"water_level: {msg}", file=sys.stderr)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now(TZ).isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass  # a log hibája soha ne buktassa a futást


def _text(cell):
    return " ".join(html.unescape(_TAG.sub("", cell)).replace("\xa0", " ").split())


def _num(s, field):
    if s == "//":
        return None
    if not _INT.fullmatch(s):
        raise ParseError(f"{field}: nem egész szám: {s!r}")
    return int(s)


def parse(page):
    m = _OBS.search(page)
    if not m:
        raise ParseError("nincs 'Observed on' dátum")
    day, mon, year = m.groups()
    month = MONTHS.get(mon.lower())
    if month is None:
        raise ParseError(f"ismeretlen hónapnév: {mon!r}")
    try:
        observed = dt.date(int(year), month, int(day))
    except ValueError as e:
        raise ParseError(f"érvénytelen dátum: {e}") from None

    rows = {}
    for tr in _TR.findall(page):
        cells = [_text(c) for c in _TD.findall(tr)]
        if cells and cells[0] in STATIONS:
            if cells[0] in rows:
                raise ParseError(f"duplikált sor: {cells[0]}")
            rows[cells[0]] = cells

    stations = {}
    for code, name in STATIONS.items():
        c = rows.get(code)
        if c is None:
            raise ParseError(f"hiányzó sor: {code}")
        if len(c) != 9:
            raise ParseError(f"{code}: {len(c)} cella (várt: 9)")
        if c[1] != name or c[2] != "Balaton":
            raise ParseError(f"{code}: váratlan név/tó: {c[1]!r}/{c[2]!r}")
        stations[code] = {
            "yesterday_morning": _num(c[3], f"{code}.yesterday_morning"),
            "yesterday_evening": _num(c[4], f"{code}.yesterday_evening"),
            "today_morning": _num(c[5], f"{code}.today_morning"),
            "change_24h": _num(c[6], f"{code}.change_24h"),
        }
    return {"observed": observed, "stations": stations}


def check(parsed):
    """Tartomány: hiba (ParseError). Konzisztencia: csak figyelmeztetés."""
    warnings = []
    for code, st in parsed["stations"].items():
        for k in ("yesterday_morning", "yesterday_evening", "today_morning"):
            v = st[k]
            if v is not None and not LEVEL_RANGE[0] <= v <= LEVEL_RANGE[1]:
                raise ParseError(f"{code}.{k}={v} tartományon kívül")
        ch = st["change_24h"]
        if ch is not None and not CHANGE_RANGE[0] <= ch <= CHANGE_RANGE[1]:
            raise ParseError(f"{code}.change_24h={ch} tartományon kívül")
        y, t = st["yesterday_morning"], st["today_morning"]
        if None not in (y, t, ch) and t - y != ch:
            warnings.append(f"{code}: ma-tegnap={t - y}, közölt változás={ch}")
    return warnings


def measured_at(observed):
    return dt.datetime.combine(observed, dt.time(READING_HOUR), TZ).isoformat()


def to_payload(parsed):
    s, a = parsed["stations"][SIOFOK], parsed["stations"][LAKE_AVG]
    if s["today_morning"] is None:
        raise ParseError("Siófok mai reggeli értéke hiányzik")
    p = {"station": "Siófok", "level_cm": s["today_morning"],
         "measured_at": measured_at(parsed["observed"]), "data_source": DATA_SOURCE}
    if s["change_24h"] is not None:
        p["trend_cm_24h"] = s["change_24h"]
    if a["today_morning"] is not None:
        p["lake_avg_cm"] = a["today_morning"]
    if a["change_24h"] is not None:
        p["lake_avg_trend_cm_24h"] = a["change_24h"]
    return p


def fetch(url=URL, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        log(f"fetched, last-modified={r.headers.get('Last-Modified')}")
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError("túl nagy válasz")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("iso-8859-2")


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"warning: állapotfájl olvashatatlan ({e}), üresnek veszem")
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="HTML fájlból olvas (offline), frissességi ellenőrzés nélkül")
    ap.add_argument("--dry-run", action="store_true", help="nem ír JSONL-t és állapotot")
    ap.add_argument("--force", action="store_true", help="dedup figyelmen kívül hagyása")
    ap.add_argument("--state", default=STATE_PATH)
    args = ap.parse_args(argv)

    try:
        if args.file:
            with open(args.file, encoding="utf-8", errors="replace") as f:
                page = f.read()
        else:
            page = fetch()
    except Exception as e:
        log(f"fetch_failed: {type(e).__name__}: {e}")
        return 1

    try:
        parsed = parse(page)
        warnings = check(parsed)
        payload = to_payload(parsed)
    except ParseError as e:
        log(f"parse_failed: {e}")
        return 2
    for w in warnings:
        log(f"warning: {w}")
    observed = parsed["observed"].isoformat()

    if not args.file:
        age = (dt.datetime.now(TZ).date() - parsed["observed"]).days
        if age < 0 or age > MAX_AGE_DAYS:
            log(f"stale_source: observed={observed}, kor={age} nap")
            return 3

    state = load_state(args.state)
    if not args.force and state.get("last_observed") == observed:
        log(f"skip: observed={observed} már publikálva ({state.get('event_id')})")
        return 0

    sys.path.insert(0, PLUGIN_DIR)
    try:
        import schemas  # noqa: E402  (agentcards, csak stdlib)
        if not args.dry_run:
            import store  # noqa: E402
    except ImportError as e:
        log(f"import_failed: {e} (PLUGIN_DIR={PLUGIN_DIR})")
        if args.dry_run:
            print(json.dumps({"payload": payload}, ensure_ascii=False))
        return 3

    try:
        ev = schemas.build_event("water", payload, "info", "script")
    except schemas.CardValidationError as e:
        log(f"validation_failed: {getattr(e, 'errors', e)}")
        if args.dry_run:
            print(json.dumps({"payload": payload}, ensure_ascii=False))
        return 3

    if args.dry_run:
        print(json.dumps(ev, ensure_ascii=False))
        return 0

    try:
        store.append_event(ev)
        save_state(args.state, {"last_observed": observed, "event_id": ev.get("id"),
                                "published_ts": ev.get("ts")})
    except OSError as e:
        log(f"store_failed: {type(e).__name__}: {e}")
        return 3
    log(f"published {ev.get('id')} observed={observed} payload={json.dumps(payload, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
