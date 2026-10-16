#!/usr/bin/env python3
"""Offline teszt a water_level.py parszolójához.
Használat: python3 -I tests/test_water_level.py [water_level.py útvonal] [tohid.html fixture]

Alapértelmezés: a repóbeli script és a szintetikus fixture
(tests/fixtures/water/tohid-synthetic.html) -> pontos értékeket ellenőriz.
Más fixture megadásakor (pl. élő oldal mentése) csak szerkezeti ellenőrzés.
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO / "tests" / "fixtures" / "water" / "tohid-synthetic.html"
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "hermes" / "scripts" / "water_level.py")
FIXTURE = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_FIXTURE)
SYNTHETIC = Path(FIXTURE).resolve() == DEFAULT_FIXTURE

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
    if SYNTHETIC:
        assert p["observed"].isoformat() == "2025-06-03", p["observed"]
        assert p["stations"]["442550"] == {"yesterday_morning": 58, "yesterday_evening": 57,
                                           "today_morning": 55, "change_24h": -3}, p["stations"]["442550"]
        assert p["stations"]["442600"] == {"yesterday_morning": 54, "yesterday_evening": None,
                                           "today_morning": 52, "change_24h": -2}, p["stations"]["442600"]
        assert wl.check(p) == []
    else:
        # élő oldal: az értékek naponta változnak, csak a szerkezetet nézzük
        assert isinstance(p["observed"], dt.date), p["observed"]
        assert set(p["stations"]) == {"442550", "442600"}, p["stations"]
        assert p["stations"]["442550"]["today_morning"] is not None, p["stations"]["442550"]
        wl.check(p)  # tartományhiba -> ParseError; ellentmondás csak figyelmeztetés
        wl.to_payload(p)


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


case(f"fixture parse ({'szintetikus' if SYNTHETIC else 'külső'}: {Path(FIXTURE).name})", t_fixture)
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
