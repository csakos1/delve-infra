#!/usr/bin/env python3
"""Offline teszt: updates_check.py parszoló + az `updates` kártyaséma.
Használat: python3 -I tests/test_updates_check.py

A fixture (tests/fixtures/updates/checkupdates.txt) egy valódi `checkupdates` kimenet
(2026-10-21, 44 frissítés). Exit 0 = minden OK.
"""
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "updates" / "checkupdates.txt"
sys.path.insert(0, str(REPO / "hermes" / "plugins" / "agentcards"))
import schemas  # noqa: E402

spec = importlib.util.spec_from_file_location("updates_check",
                                              REPO / "hermes" / "scripts" / "updates_check.py")
uc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uc)

NOW = dt.datetime.now(dt.timezone.utc)
results = []


def case(name, fn):
    try:
        detail = fn()
        results.append((name, detail is None, detail or ""))
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"{type(e).__name__}: {e}"))


def t_fixture():
    names = uc.parse(FIXTURE.read_text(encoding="utf-8"))
    if len(names) != 44:
        return f"{len(names)} csomag, 44 várt"
    if names[:2] != ["ca-certificates-mozilla", "clang"] or names[-1] != "vamp-plugin-sdk":
        return f"sorrend: {names[:2]} … {names[-1]}"
    if "discord" not in names or "luajit" not in names:  # epoch- és +hash-es verziók
        return "epoch/+hash sor hiányzik"
    return None


def t_payload():
    names = uc.parse(FIXTURE.read_text(encoding="utf-8"))
    ev = schemas.build_event("updates", uc.to_payload(names, NOW), "info", "script")
    p = ev["payload"]
    if p["count"] != 44 or len(p["packages"]) != 20 or ev["origin"] != "script":
        return f"payload: {p}"
    return None


def t_empty():
    if uc.parse("") != [] or uc.parse("\n\n") != []:
        return "üres kimenet nem üres lista"
    p = schemas.validate_updates(uc.to_payload([], NOW), NOW)
    return None if p["count"] == 0 and p["packages"] == [] else f"{p}"


def t_bad_line():
    try:
        uc.parse("==> ERROR: Cannot fetch updates\n")
    except uc.ParseError:
        return None
    return "a hibasort elfogadta"


def rejects(payload):
    def fn():
        try:
            out = schemas.validate_updates(payload, NOW)
        except schemas.CardValidationError:
            return None
        return f"elfogadta: {out}"
    return fn


OK = {"count": 2, "packages": ["a", "b"], "checked_at": NOW.isoformat()}
case("valódi checkupdates-kimenet", t_fixture)
case("kártya: 44 db, 20 név, script", t_payload)
case("nincs frissítés", t_empty)
case("hibasor -> ParseError", t_bad_line)
case("count bool", rejects(dict(OK, count=True)))
case("count string", rejects(dict(OK, count="2")))
case("count negatív", rejects(dict(OK, count=-1)))
case("21 név", rejects(dict(OK, count=30, packages=[f"p{i}" for i in range(21)])))
case("több név, mint count", rejects(dict(OK, count=1)))
case("név szóközzel", rejects(dict(OK, packages=["a b", "c"])))
case("ismeretlen mező", rejects(dict(OK, extra=1)))
case("checked_at időzóna nélkül", rejects(dict(OK, checked_at="2026-10-21T07:15:00")))
case("checked_at a jövőben", rejects(dict(OK, checked_at=(NOW + dt.timedelta(hours=1)).isoformat())))

n_ok = sum(1 for _, r, _ in results if r)
for name, r, d in results:
    print(f"{'OK  ' if r else 'FAIL'} {name}" + (f"  -- {d}" if d else ""))
print(f"{n_ok}/{len(results)} OK")
sys.exit(0 if n_ok == len(results) and os.path.exists(FIXTURE) else 1)
