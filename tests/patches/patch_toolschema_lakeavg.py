#!/usr/bin/env python3
"""agentcards/schemas.py: PUBLISH_WATER_CARD_SCHEMA properties bővítése
(lake_avg_cm, lake_avg_trend_cm_24h). Idempotens, exact-string, mentéssel.
Használat: python3 -I patch_toolschema_lakeavg.py <schemas.py> [--check]
"""
import shutil
import sys

path = sys.argv[1]
check_only = "--check" in sys.argv
src = open(path, encoding="utf-8").read()

OLD = ('            "trend_cm_24h": {"type": "number", "description": '
       '"Change over the last 24 h in cm (negative = falling)"},\n')
NEW = OLD + (
    '            "lake_avg_cm": {"type": "number", "description": '
    '"Lake Balaton average water level in cm (official lake-wide average, same gauge datum)"},\n'
    '            "lake_avg_trend_cm_24h": {"type": "number", "description": '
    '"Change of the lake-wide average over the last 24 h in cm (negative = falling)"},\n'
)

if '"lake_avg_cm": {"type"' in src:
    print("MÁR PATCHELVE – nincs teendő")
    sys.exit(0)
n = src.count(OLD)
if n != 1:
    print(f"HIBA: {n} találat (várt: 1), semmi nem változott")
    sys.exit(1)
if check_only:
    print("CHECK OK")
    sys.exit(0)

shutil.copy2(path, path + ".bak-toolschema")
src = src.replace(OLD, NEW, 1)
compile(src, path, "exec")
with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print(f"PATCHELVE: {path} (mentés: {path}.bak-toolschema)")
