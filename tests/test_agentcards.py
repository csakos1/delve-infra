"""agentcards offline teszt: séma-validálás + JSONL-store. Exit 0 = minden OK."""
import json, os, stat, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/agentcards"))
import schemas, store  # noqa: E402

now = datetime.now(timezone.utc)
ok_ts = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
base = {"station": "Siófok", "level_cm": 98, "measured_at": ok_ts,
        "trend_cm_24h": -1.5, "threshold_cm": 70, "note": "teszt"}
cases = [
    ("ok",              base,                                              "info",     True),
    ("Z suffix",        {**base, "measured_at": ok_ts.replace("+00:00", "Z")}, "alert", True),
    ("bad severity",    base,                                              "critical", False),
    ("unknown field",   {**base, "path": "/etc/x"},                        "info",     False),
    ("level string",    {**base, "level_cm": "98"},                        "info",     False),
    ("level bool",      {**base, "level_cm": True},                        "info",     False),
    ("level nan",       {**base, "level_cm": float("nan")},                "info",     False),
    ("naive ts",        {**base, "measured_at": "2026-10-16T17:00:00"},    "info",     False),
    ("future ts",       {**base, "measured_at": (now + timedelta(hours=2)).isoformat()}, "warn", False),
    ("missing station", {k: v for k, v in base.items() if k != "station"}, "info",     False),
    ("long note",       {**base, "note": "x" * 281},                       "info",     False),
]
fails = 0
events = []
for name, payload, sev, expect_ok in cases:
    try:
        ev = schemas.build_event("water", dict(payload), sev, "script")
        got, info = True, ev["payload"]
        events.append(ev)
    except schemas.CardValidationError as e:
        got, info = False, e.errors
    ok = got == expect_ok
    fails += not ok
    print(f"{'OK  ' if ok else 'FAIL'} {name:16} valid={got!s:5} {info}")

with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "sub", "events.jsonl")
    for ev in events:
        store.append_event(ev, p)
    lines = [json.loads(l) for l in open(p, encoding="utf-8")]
    mode = stat.S_IMODE(os.stat(p).st_mode)
    store_ok = len(lines) == len(events) and mode == 0o600 and lines[0]["v"] == 1
    fails += not store_ok
    print(f"{'OK  ' if store_ok else 'FAIL'} store            lines={len(lines)} mode={oct(mode)}")

print(f"\n{len(cases) + 1 - fails}/{len(cases) + 1} OK")
sys.exit(1 if fails else 0)
