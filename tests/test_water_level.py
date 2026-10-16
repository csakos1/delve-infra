#!/usr/bin/env python3
"""Offline teszt a water_level.py parszolójához.
Használat: python3 test_water_level.py [water_level.py útvonal] [tohid.html fixture]
"""
import importlib.util
import os
import sys

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.hermes/scripts/water_level.py")
FIXTURE = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/hermes-proto/tohid.html")

spec = importlib.util.spec_from_file_location("water_level", SCRIPT)
wl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wl)

ROW = ('<tr>' + '<td width="1"><div><font size="1">{}</font></div></td>' * 9 + '</tr>\r\n')


def page(rows, observed="16 October  2026"):
    body = "".join(ROW.format(*r) for r in rows)
    return (f'<table><tr><td colspan="9"><font>Observed on: {observed}</font></td></tr>\r\n'
            f'{body}</table>')


SIO = ["442550", "Si&oacute;fok", "Balaton", "34", "33", "32", "<b>-2</b>", "13.6", "//"]
AVG = ["442600", "Balaton average", "Balaton", "31", "//", "30", "<b>-1</b>", "//", "//"]

results = []


def case(name, fn):
    try:
        fn()
        results.append((name, True, ""))
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))


def raises_parse(html):
    try:
        p = wl.parse(html)
        wl.check(p)
        wl.to_payload(p)
    except wl.ParseError:
        return
    raise AssertionError("nem dobott ParseError-t")


def t_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        p = wl.parse(f.read())
    assert p["observed"].isoformat() == "2026-10-16", p["observed"]
    assert p["stations"]["442550"] == {"yesterday_morning": 34, "yesterday_evening": 33,
                                       "today_morning": 32, "change_24h": -2}, p["stations"]["442550"]
    assert p["stations"]["442600"] == {"yesterday_morning": 31, "yesterday_evening": None,
                                       "today_morning": 30, "change_24h": -1}, p["stations"]["442600"]
    assert wl.check(p) == []


def t_payload():
    p = wl.to_payload(wl.parse(page([SIO, AVG])))
    assert p == {"station": "Siófok", "level_cm": 32, "measured_at": "2026-10-16T07:00:00+02:00",
                 "data_source": "hydroinfo.hu (OVSZ) tohid", "trend_cm_24h": -2,
                 "lake_avg_cm": 30, "lake_avg_trend_cm_24h": -1}, p


def t_winter_offset():
    p = wl.to_payload(wl.parse(page([SIO, AVG], observed="15 January 2027")))
    assert p["measured_at"] == "2027-01-15T07:00:00+01:00", p["measured_at"]


def t_avg_missing_value():
    a = AVG[:5] + ["//", "//"] + AVG[7:]
    p = wl.to_payload(wl.parse(page([SIO, a])))
    assert "lake_avg_cm" not in p and "lake_avg_trend_cm_24h" not in p, p


def t_inconsistent_warns():
    s = SIO[:6] + ["-5"] + SIO[7:]
    w = wl.check(wl.parse(page([s, AVG])))
    assert len(w) == 1 and "442550" in w[0], w


case("fixture parse (élő HTML)", t_fixture)
case("payload + measured_at nyári idő", t_payload)
case("measured_at téli idő (+01:00)", t_winter_offset)
case("átlag hiányzik -> mezők kimaradnak", t_avg_missing_value)
case("ellentmondó változás -> figyelmeztetés", t_inconsistent_warns)
case("hiányzó átlag-sor -> hiba", lambda: raises_parse(page([SIO])))
case("Siófok ma '//' -> hiba", lambda: raises_parse(page([SIO[:5] + ["//"] + SIO[6:], AVG])))
case("nem szám -> hiba", lambda: raises_parse(page([SIO[:5] + ["3x"] + SIO[6:], AVG])))
case("tartományon kívül -> hiba", lambda: raises_parse(page([SIO[:5] + ["999"] + SIO[6:], AVG])))
case("ismeretlen hónap -> hiba", lambda: raises_parse(page([SIO, AVG], observed="16 Oktober 2026")))
case("nincs dátum -> hiba", lambda: raises_parse(page([SIO, AVG]).replace("Observed on:", "")))
case("duplikált sor -> hiba", lambda: raises_parse(page([SIO, SIO, AVG])))
case("cellaszám eltér -> hiba", lambda: raises_parse(page([SIO, AVG]).replace(
    '<td width="1"><div><font size="1">//</font></div></td></tr>', "</tr>", 1)))
case("váratlan név -> hiba", lambda: raises_parse(page([["442550", "Keszthely"] + SIO[2:], AVG])))

ok = sum(1 for _, r, _ in results if r)
for name, r, msg in results:
    print(f"{'OK  ' if r else 'FAIL'} {name}" + (f"  -- {msg}" if msg else ""))
print(f"{ok}/{len(results)} OK")
sys.exit(0 if ok == len(results) else 1)
