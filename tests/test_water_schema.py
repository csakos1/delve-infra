#!/usr/bin/env python3
"""Offline teszt: water séma új mezői (lake_avg_cm, lake_avg_trend_cm_24h).
Használat: python3 -I test_water_schema.py [plugin könyvtár]
"""
import os
import sys
from datetime import datetime, timedelta, timezone

PLUGIN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hermes", "plugins", "agentcards")
sys.path.insert(0, PLUGIN)
import schemas  # noqa: E402

NOW = datetime.now(timezone.utc)
M = (NOW - timedelta(hours=2)).isoformat()
BASE = {"station": "Siófok", "level_cm": 32, "measured_at": M,
        "trend_cm_24h": -2, "data_source": "hydroinfo.hu (OVSZ) tohid"}

results = []


def case(name, payload, ok_expected):
    try:
        out = schemas.validate_water(payload, NOW)
        ok = ok_expected
        detail = "" if ok else f"elfogadta: {out}"
    except schemas.CardValidationError as e:
        ok = not ok_expected
        detail = "" if ok else f"elutasította: {e.errors}"
    results.append((name, ok, detail))


case("régi payload (új mezők nélkül)", dict(BASE), True)
case("teljes új payload", dict(BASE, lake_avg_cm=30, lake_avg_trend_cm_24h=-1), True)
case("negatív átlag", dict(BASE, lake_avg_cm=-5), True)
case("lake_avg_cm string", dict(BASE, lake_avg_cm="30"), False)
case("lake_avg_cm bool", dict(BASE, lake_avg_cm=True), False)
case("lake_avg_cm NaN", dict(BASE, lake_avg_cm=float("nan")), False)
case("lake_avg_cm tartományon kívül", dict(BASE, lake_avg_cm=2000), False)
case("lake_avg_trend tartományon kívül", dict(BASE, lake_avg_trend_cm_24h=500), False)
case("elírt mezőnév (lake_avg_change_cm_24h)", dict(BASE, lake_avg_change_cm_24h=-1), False)

# build_event végig, origin=script
try:
    ev = schemas.build_event("water", dict(BASE, lake_avg_cm=30, lake_avg_trend_cm_24h=-1),
                             "info", "script")
    ok = ev["payload"].get("lake_avg_cm") == 30 and ev["origin"] == "script" and ev["v"] == 1
    results.append(("build_event (script, v1)", ok, "" if ok else str(ev)))
except Exception as e:
    results.append(("build_event (script, v1)", False, f"{type(e).__name__}: {e}"))

n_ok = sum(1 for _, r, _ in results if r)
for name, r, d in results:
    print(f"{'OK  ' if r else 'FAIL'} {name}" + (f"  -- {d}" if d else ""))
print(f"{n_ok}/{len(results)} OK")
sys.exit(0 if n_ok == len(results) else 1)
