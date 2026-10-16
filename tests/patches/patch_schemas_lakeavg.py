#!/usr/bin/env python3
"""agentcards/schemas.py: water payload bővítése (lake_avg_cm, lake_avg_trend_cm_24h).
Visszafelé kompatibilis, opcionális mezők -> SCHEMA_VERSION marad 1.
Használat: python3 -I patch_schemas_lakeavg.py <schemas.py> [--check]
"""
import shutil
import sys

path = sys.argv[1]
check_only = "--check" in sys.argv
src = open(path, encoding="utf-8").read()

EDITS = [
    (
        'WATER_FIELDS = ("station", "level_cm", "measured_at", "trend_cm_24h",\n'
        '                "threshold_cm", "data_source", "note")\n',
        'WATER_FIELDS = ("station", "level_cm", "measured_at", "trend_cm_24h",\n'
        '                "lake_avg_cm", "lake_avg_trend_cm_24h",\n'
        '                "threshold_cm", "data_source", "note")\n',
    ),
    (
        '        "trend_cm_24h": _num(p.get("trend_cm_24h"), "trend_cm_24h", -300, 300, errs, False),\n',
        '        "trend_cm_24h": _num(p.get("trend_cm_24h"), "trend_cm_24h", -300, 300, errs, False),\n'
        '        "lake_avg_cm": _num(p.get("lake_avg_cm"), "lake_avg_cm", -500, 1500, errs, False),\n'
        '        "lake_avg_trend_cm_24h": _num(p.get("lake_avg_trend_cm_24h"), "lake_avg_trend_cm_24h",\n'
        '                                      -300, 300, errs, False),\n',
    ),
]

if "lake_avg_cm" in src:
    print("MÁR PATCHELVE (lake_avg_cm szerepel) – nincs teendő")
    sys.exit(0)

for i, (old, _) in enumerate(EDITS, 1):
    n = src.count(old)
    if n != 1:
        print(f"HIBA: {i}. csere – {n} találat (várt: 1), semmi nem változott")
        sys.exit(1)

if check_only:
    print("CHECK OK: mindkét csere pontosan 1 helyen illeszkedik")
    sys.exit(0)

shutil.copy2(path, path + ".bak-lakeavg")
for old, new in EDITS:
    src = src.replace(old, new, 1)
compile(src, path, "exec")  # szintaxis-ellenőrzés írás előtt
with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print(f"PATCHELVE: {path} (mentés: {path}.bak-lakeavg)")
