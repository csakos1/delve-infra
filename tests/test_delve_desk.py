"""bin/delve-desk offline tests: the two verbs' argv, the checks on every value, the
apps' icons, the backend's answers and the loop's pauses.

No session bus and no backend needed: the module's RUN and FETCH hooks are replaced by
fakes. Exit 0 = all OK.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
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


def png(width: int = 96, height: int = 96, tail: bytes = b"") -> bytes:
    """The start of a PNG of that size (the agent reads no further than the header)."""
    header = struct.pack(">II5s", width, height, b"\x08\x06\x00\x00\x00")
    return desk.PNG_MAGIC + struct.pack(">I", 13) + b"IHDR" + header + b"\x00" * 4 + tail


def name_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


class AppIcons(Base):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = os.path.join(self._tmp.name, "delve-desk")  # made on first use
        self.served: dict[str, tuple[int, bytes]] = {}
        self.asked: list[tuple[str, str, float]] = []
        self.down: BaseException | None = None

        def fetch(url: str, token: str, timeout: float) -> tuple[int, bytes]:
            self.asked.append((url, token, timeout))
            if self.down is not None:
                raise self.down
            return self.served.get(url.rsplit("/", 1)[1], (404, b""))

        desk.FETCH = fetch
        self.icons = desk.Icons(self.dir, "http://127.0.0.1:8700", TOKEN)

    def test_fetched_once_kept_and_handed_to_the_daemon(self) -> None:
        data = png()
        name = name_of(data)
        self.served[name] = (200, data)
        d = desk.Desk(self.icons)
        d.handle(show(icon=name))
        path = os.path.join(self.dir, f"{name}.png")
        self.assertEqual(self.bus.calls[0][10:13], ["Messenger", "0", path])  # app_icon
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        self.assertEqual(
            self.asked,
            [(f"http://127.0.0.1:8700/api/v1/desk/icon/{name}", TOKEN, desk.ICON_TIMEOUT_S)],
        )
        d.handle(show(icon=name, body="second"))
        self.assertEqual(self.bus.calls[1][12], path)
        self.assertEqual(len(self.asked), 1)  # the kept file, no second request
        self.assertEqual(os.listdir(self.dir), [f"{name}.png"])  # no .part left

    def test_no_icon_is_no_reason_not_to_show(self) -> None:
        d = desk.Desk(self.icons)
        data = png()
        name = name_of(data)
        for case in (None, "", 7, "short", "g" * 32, "../" + "a" * 29, name.upper()):
            d.handle(show(icon=case))
        self.assertEqual(self.asked, [])  # not names: nothing to ask for
        d.handle(show(icon=name))  # the backend no longer has it (404)
        self.served[name] = (200, png(tail=b"another picture"))  # not what the name says
        d.handle(show(icon=name))
        self.served[name] = (200, b"GIF89a" + b"\x00" * 40)
        d.handle(show(icon=name))
        for size in ((0, 96), (96, 513), (4096, 4096)):
            big = png(*size)
            self.served[name_of(big)] = (200, big)
            d.handle(show(icon=name_of(big)))
        huge = png(tail=b"\x00" * desk.ICON_MAX)
        self.served[name_of(huge)] = (200, huge)
        d.handle(show(icon=name_of(huge)))
        self.served[name] = (200, data)
        self.down = urllib.error.URLError("connection refused")
        d.handle(show(icon=name))
        self.assertEqual({call[12] for call in self.bus.calls}, {""})
        self.assertEqual(len(self.bus.calls), 7 + 3 + 3 + 1 + 1)  # every one was shown
        self.assertFalse(os.path.exists(self.dir))
        self.assertEqual(self.logged, [])
        # without a place to keep them the agent asks for none
        nowhere = desk.Desk(desk.Icons(None, "http://127.0.0.1:8700", TOKEN))
        self.down = None
        asked = len(self.asked)
        nowhere.handle(show(icon=name))
        self.assertEqual((self.bus.calls[-1][12], len(self.asked)), ("", asked))

    def test_only_the_newest_are_kept(self) -> None:
        old, desk.ICONS_KEPT = desk.ICONS_KEPT, 3
        self.addCleanup(setattr, desk, "ICONS_KEPT", old)
        names = []
        for i in range(5):
            data = png(tail=bytes([i]))
            names.append(name_of(data))
            self.served[names[-1]] = (200, data)
            path = self.icons.path(names[-1])
            os.utime(path, (1000 + i, 1000 + i))
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(f"{n}.png" for n in names[2:]))

    def test_where_the_icons_go(self) -> None:
        self.assertEqual(desk.icon_dir({"RUNTIME_DIRECTORY": "/run/user/1000/delve-desk"}),
                         "/run/user/1000/delve-desk")  # fmt: skip
        self.assertEqual(desk.icon_dir({"RUNTIME_DIRECTORY": "/a/b:/c/d"}), "/a/b")
        self.assertEqual(desk.icon_dir({"XDG_RUNTIME_DIR": "/run/user/1000"}),
                         "/run/user/1000/delve-desk")  # fmt: skip
        self.assertIsNone(desk.icon_dir({}))
        self.assertIsNone(desk.icon_dir({"RUNTIME_DIRECTORY": "relative"}))


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
        self.assertIn("RuntimeDirectory=delve-desk\n", unit)  # where the icons are kept
        self.assertIn("RuntimeDirectoryPreserve=yes\n", unit)
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
