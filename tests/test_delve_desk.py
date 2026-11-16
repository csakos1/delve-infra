"""bin/delve-desk offline tests: the two verbs' argv, the checks on every value, the
backend's answers and the loop's pauses.

No session bus and no backend needed: the module's RUN and FETCH hooks are replaced by
fakes. Exit 0 = all OK.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from collections.abc import Sequence
from typing import Any

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_loader = importlib.machinery.SourceFileLoader("delve_desk", os.path.join(ROOT, "bin", "delve-desk"))
_spec = importlib.util.spec_from_loader("delve_desk", _loader)
assert _spec is not None
desk = importlib.util.module_from_spec(_spec)
_loader.exec_module(desk)

TOKEN = "dlv_" + "a" * 43
ENV = {"DELVE_DESK_TOKEN": TOKEN}
ID = "0123456789abcdef"
HEAD = [
    "/usr/bin/busctl", "--user", "--timeout=5", "call", "--",
    "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
    "org.freedesktop.Notifications",
]  # fmt: skip


class FakeBus:
    """Answers Notify with rising ids; records every argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.next_id = 41
        self.fail: tuple[int, str] | None = None
        self.answer: str | None = None

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        assert timeout == desk.CALL_TIMEOUT_S
        if self.fail is not None:
            return subprocess.CompletedProcess(list(argv), self.fail[0], "", self.fail[1])
        if argv[8] == "Notify":
            replaces = int(argv[11])
            if replaces == 0:
                self.next_id += 1
            out = self.answer if self.answer is not None else f"u {replaces or self.next_id}\n"
            return subprocess.CompletedProcess(list(argv), 0, out, "")
        return subprocess.CompletedProcess(list(argv), 0, "\n", "")


def show(**fields: Any) -> dict[str, Any]:
    return {
        "verb": "notify.show", "id": ID, "app": "Messenger", "title": "Anna",
        "body": "hello", "category": "msg", "hidden": False, **fields,
    }  # fmt: skip


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._run, self._fetch = desk.RUN, desk.FETCH
        self.bus = FakeBus()
        desk.RUN = self.bus
        self.logged: list[str] = []
        self._log = desk.log
        desk.log = self.logged.append

    def tearDown(self) -> None:
        desk.RUN, desk.FETCH, desk.log = self._run, self._fetch, self._log


class Verbs(Base):
    def test_show_argv(self) -> None:
        d = desk.Desk()
        self.assertEqual(d.handle(show()), "notify.show")
        self.assertEqual(
            self.bus.calls,
            [
                [
                    *HEAD, "Notify", "susssasa{sv}i",
                    "Messenger", "0", "", "Anna", "hello", "0",
                    "3", "urgency", "y", "1", "x-delve-origin", "s", "phone",
                    "category", "s", "im.received",
                    "-1",
                ]
            ],
        )  # fmt: skip
        self.assertEqual(d.shown, {ID: 42})

    def test_same_id_replaces_and_close_closes(self) -> None:
        d = desk.Desk()
        d.handle(show())
        d.handle(show(body="second"))
        self.assertEqual(self.bus.calls[1][11], "42")  # replaces_id
        self.assertEqual(d.shown, {ID: 42})
        self.assertEqual(d.handle({"verb": "notify.close", "id": ID}), "notify.close")
        self.assertEqual(self.bus.calls[2], [*HEAD, "CloseNotification", "u", "42"])
        self.assertEqual(d.shown, {})
        # closing what is not shown (never came, or closed already) calls nothing
        d.handle({"verb": "notify.close", "id": ID})
        self.assertEqual(len(self.bus.calls), 3)

    def test_text_is_shown_not_interpreted(self) -> None:
        d = desk.Desk()
        d.handle(
            show(
                app="My\nApp",
                title="-5° <b>in</b> town --help\nsecond line",
                body='<a href="https://x.example/">click</a> & co\x07\x00\x1b[31m\ttab\nline',
            )
        )
        argv = self.bus.calls[0]
        self.assertEqual(argv[4], "--")  # a leading dash cannot become an option
        self.assertEqual(argv[10], "My App")
        self.assertEqual(argv[13], "-5° <b>in</b> town --help second line")  # summary: plain
        self.assertEqual(
            argv[14],
            '&lt;a href="https://x.example/"&gt;click&lt;/a&gt; &amp; co   [31m\ttab\nline',
        )
        for value in argv:
            self.assertNotIn("\x00", value)

    def test_lengths_types_and_categories(self) -> None:
        d = desk.Desk()
        d.handle(show(app="A" * 100, title="T" * 300, body="B" * 3000, category="email"))
        argv = self.bus.calls[0]
        self.assertEqual((len(argv[10]), len(argv[13]), len(argv[14])), (64, 256, 2000))
        self.assertEqual(argv[-4:], ["category", "s", "email.arrived", "-1"])
        # no title: the app's name; nothing usable at all: still a notification
        d.handle(show(id="f" * 16, app=7, title=None, body=["x"], category="transport"))
        argv = self.bus.calls[1]
        self.assertEqual(argv[10:16], ["Phone", "0", "", "Phone", "", "0"])
        self.assertEqual(argv[16:], ["2", "urgency", "y", "1", "x-delve-origin", "s", "phone", "-1"])
        for bad in ("Msg", "im.received", "a" * 33, 5, None):
            self.bus.calls.clear()
            d.handle(show(id="e" * 16, category=bad))
            self.assertEqual(self.bus.calls[0][16], "2", bad)

    def test_hidden_content(self) -> None:
        d = desk.Desk()
        d.handle(show(app="Messages", title="", body="", hidden=True))
        argv = self.bus.calls[0]
        self.assertEqual(argv[10:15], ["Messages", "0", "", "Messages", desk.HIDDEN_BODY])
        # only the boolean hides: text is not dropped for a truthy string
        d.handle(show(hidden="yes"))
        self.assertEqual(self.bus.calls[1][14], "hello")

    def test_bad_commands(self) -> None:
        d = desk.Desk()
        for bad in (
            None, "notify.show", [], {},
            {"verb": "notify.show"},
            {"verb": "notify.show", "id": "xyz"},
            {"verb": "notify.show", "id": ID.upper()},
            {"verb": "notify.show", "id": ID + "0"},
            {"verb": "notify.show", "id": 5},
            {"verb": "notify.run", "id": ID},
            {"verb": "clipboard.set", "id": ID, "text": "x"},
            {"id": ID},
        ):  # fmt: skip
            with self.assertRaises(desk.DeskError, msg=repr(bad)):
                d.handle(bad)
        self.assertEqual(self.bus.calls, [])

    def test_busctl_failures(self) -> None:
        d = desk.Desk()
        self.bus.fail = (1, "Call failed: The name is not activatable\nmore\n")
        with self.assertRaises(desk.DeskError) as ctx:
            d.handle(show())
        self.assertEqual(
            str(ctx.exception), "Notify failed (1): Call failed: The name is not activatable"
        )
        self.assertEqual(d.shown, {})
        self.bus.fail, self.bus.answer = None, "s \"what\"\n"
        with self.assertRaises(desk.DeskError):
            d.handle(show())
        self.assertEqual(d.shown, {})

    def test_shown_is_bounded(self) -> None:
        d = desk.Desk()
        for i in range(desk.SHOWN_MAX + 5):
            d.handle(show(id=f"{i:016x}"))
        self.assertEqual(len(d.shown), desk.SHOWN_MAX)
        self.assertNotIn(f"{0:016x}", d.shown)
        self.assertIn(f"{desk.SHOWN_MAX + 4:016x}", d.shown)

    def test_one_failure_does_not_stop_the_rest(self) -> None:
        d = desk.Desk()
        done, failed = desk.carry_out(d, [show(), {"verb": "nope", "id": ID}, show(id="a" * 16)])
        self.assertEqual((done, failed), (2, 1))
        self.assertEqual(self.logged, ["command failed: unknown verb"])


class Backend(Base):
    def test_settings(self) -> None:
        self.assertEqual(desk.settings(ENV), ("http://127.0.0.1:8700", TOKEN))
        for url in ("http://127.0.0.1:8701/", "http://localhost:8700", "http://[::1]:8700"):
            self.assertEqual(
                desk.settings({**ENV, "DELVE_DESK_URL": url}), (url.rstrip("/"), TOKEN), url
            )
        for url in (
            "https://127.0.0.1:8700", "http://192.168.1.5:8700", "http://example.org",
            "http://user@127.0.0.1:8700", "http://127.0.0.1:8700/api", "http://127.0.0.1:8700/?x=1",
            "http://127.0.0.1.example.org:8700", "file:///etc/passwd",
        ):  # fmt: skip
            with self.assertRaises(desk.DeskError, msg=url):
                desk.settings({**ENV, "DELVE_DESK_URL": url})
        for token in ("", "dlv_short", "x" * 50, "dlv_" + "a" * 43 + " b", "dlv_" + "a" * 200):
            with self.assertRaises(desk.DeskError, msg=token):
                desk.settings({"DELVE_DESK_TOKEN": token})

    def answers(self, *items: Any) -> list[tuple[str, str, float]]:
        queue = list(items)
        seen: list[tuple[str, str, float]] = []

        def fetch(url: str, token: str, timeout: float) -> tuple[int, bytes]:
            seen.append((url, token, timeout))
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item  # type: ignore[no-any-return]

        desk.FETCH = fetch
        return seen

    def test_poll(self) -> None:
        body = json.dumps({"commands": [show()]}).encode()
        seen = self.answers((200, body))
        self.assertEqual(desk.poll("http://127.0.0.1:8700", TOKEN), [show()])
        self.assertEqual(seen, [("http://127.0.0.1:8700/api/v1/desk/next?wait=25", TOKEN, 40)])

        self.answers((401, b""))
        with self.assertRaises(desk.Rejected):
            desk.poll("http://127.0.0.1:8700", TOKEN)
        for answer in (
            (503, b""), (200, b"not json"), (200, b"[]"), (200, b'{"commands": {}}'),
            (200, b'{"other": []}'), (200, b"\xff\xfe"),
            (200, json.dumps({"commands": [1] * (desk.COMMANDS_MAX + 1)}).encode()),
            (200, b" " * (desk.RESPONSE_MAX + 1)),
            urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
            TimeoutError("timed out"),
        ):  # fmt: skip
            self.answers(answer)
            with self.assertRaises(desk.DeskError, msg=repr(answer)[:60]) as ctx:
                desk.poll("http://127.0.0.1:8700", TOKEN)
            self.assertNotIsInstance(ctx.exception, desk.Rejected)

    def test_loop_pauses_and_recovers(self) -> None:
        body = json.dumps({"commands": [show(), {"verb": "notify.close", "id": ID}]}).encode()
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.answers(refused, refused, refused, refused, refused, (401, b""), (200, body), (200, b'{"commands": []}'))
        slept: list[float] = []
        self.assertEqual(desk.serve(ENV, sleep=slept.append, rounds=8), 0)
        self.assertEqual(slept, [2, 5, 10, 30, 30, 60])
        self.assertEqual(
            self.logged,
            [
                "started: asking http://127.0.0.1:8700",
                "backend not reachable: [Errno 111] Connection refused",  # once per outage
                "the backend rejects the token (role desk? revoked?)",
                "backend reachable again",
                "commands: 2 done, 0 failed, 0 shown",
            ],
        )
        self.assertEqual([c[8] for c in self.bus.calls], ["Notify", "CloseNotification"])

    def test_nothing_of_a_notification_is_logged(self) -> None:
        body = json.dumps({"commands": [show(app="SECRETAPP", title="SECRET", body="SECRET")]})
        self.answers((200, body.encode()))
        self.bus.fail = (1, "Call failed: no daemon")
        desk.serve(ENV, sleep=lambda _s: None, rounds=1)
        self.assertTrue(self.logged)
        self.assertNotIn("SECRET", " ".join(self.logged))

    def test_not_started_without_settings(self) -> None:
        self.assertEqual(desk.serve({}, rounds=1), 2)
        self.assertEqual(len(self.logged), 1)
        self.assertTrue(self.logged[0].startswith("not started: DELVE_DESK_TOKEN"))
        self.assertEqual(desk.main(["--what"]), 2)


class Files(unittest.TestCase):
    def test_unit_and_example(self) -> None:
        with open(os.path.join(ROOT, "systemd", "delve-desk.service"), encoding="utf-8") as f:
            unit = f.read()
        self.assertIn("ExecStart=/usr/bin/python3 -I /usr/local/lib/delve/delve-desk\n", unit)
        self.assertIn("EnvironmentFile=%h/.config/delve/desk.env\n", unit)
        self.assertIn("WantedBy=graphical-session.target\n", unit)
        self.assertNotIn("PrivateNetwork", unit.replace("# Not PrivateNetwork", ""))
        with open(os.path.join(ROOT, "etc", "delve", "desk.env.example"), encoding="utf-8") as f:
            example = dict(
                line.split("=", 1) for line in f.read().splitlines() if line and line[0] != "#"
            )
        self.assertEqual(list(example), ["DELVE_DESK_TOKEN"])
        with self.assertRaises(desk.DeskError):  # the placeholder is not a token
            desk.settings(example)
        with open(os.path.join(ROOT, "bin", "delve-desk"), encoding="utf-8") as f:
            self.assertEqual(f.readline(), "#!/usr/bin/python3 -I\n")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=1)
