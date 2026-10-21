"""bin/delve-helper offline tests: request parsing, verbs, parsers on the recorded samples.

No systemd or GPU needed: the module's RUN hook is replaced by a fake that checks the
exact argv and answers from tests/fixtures/system/. Exit 0 = all OK.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from collections.abc import Sequence

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FIX = os.path.join(ROOT, "tests", "fixtures", "system")
_loader = importlib.machinery.SourceFileLoader("delve_helper", os.path.join(ROOT, "bin", "delve-helper"))
_spec = importlib.util.spec_from_loader("delve_helper", _loader)
assert _spec is not None
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

ENV = {"LLAMA_USER": "operator", "AGENT_USER": "agent"}


def fixture(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class FakeRun:
    """Answers argv -> (exit code, stdout, stderr); records every call."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.answers = answers
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        key = tuple(argv)
        if key not in self.answers:
            raise AssertionError(f"unexpected command: {argv}")
        code, out, err = self.answers[key]
        if code == -1:
            raise helper.HelperError("timeout", f"{argv[0]} did not finish in {timeout:g} s")
        return subprocess.CompletedProcess(list(argv), code, out, err)


def show_argv(user: str, unit: str) -> tuple[str, ...]:
    return (
        "/usr/bin/systemctl", "--user", "-M", f"{user}@", "show", "--timestamp=unix", unit,
        "-p", "LoadState,ActiveState,SubState,Result,ActiveEnterTimestamp,"
        "InactiveEnterTimestamp,MainPID,NRestarts",
    )


GPU_ARGV = (
    "/usr/bin/nvidia-smi",
    "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu,"
    "power.draw,power.limit",
    "--format=csv,noheader,nounits",
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = helper.RUN

    def tearDown(self) -> None:
        helper.RUN = self._orig

    def use(self, answers: dict[tuple[str, ...], tuple[int, str, str]]) -> FakeRun:
        fake = FakeRun(answers)
        helper.RUN = fake
        return fake

    def ask(self, raw: bytes | str) -> dict:
        if isinstance(raw, str):
            raw = raw.encode()
        out, _verb = helper.respond(raw, ENV)
        # the response must be one JSON line
        line = json.dumps(out, separators=(",", ":"))
        self.assertNotIn("\n", line)
        return json.loads(line)


class Requests(Base):
    def test_bad_requests(self) -> None:
        self.use({})
        for raw in (b"nope", b"[]", b'{"verb": 1}', b'{"verb": "gpu.status", "x": 1}',
                    b'{"verb": "llama.status", "args": ["--now"]}', b"\xff\xfe", b"{}"):
            with self.subTest(raw=raw):
                r = self.ask(raw)
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], "bad_request")

    def test_too_long(self) -> None:
        self.use({})
        r = self.ask(b'{"verb": "' + b"x" * 2000 + b'"}')
        self.assertEqual(r["error"], "bad_request")

    def test_unknown_verb_runs_nothing(self) -> None:
        fake = self.use({})
        for verb in ("llama.restart", "shell", "gpu.status ; id", "LLAMA.STOP"):
            with self.subTest(verb=verb):
                r = self.ask(json.dumps({"verb": verb}))
                self.assertEqual(r["error"], "unknown_verb")
        self.assertEqual(fake.calls, [])

    def test_settings_validated(self) -> None:
        self.use({})
        for env in ({}, {"LLAMA_USER": "root; id", "AGENT_USER": "agent"},
                    {"LLAMA_USER": "operator", "AGENT_USER": "../x"},
                    {"LLAMA_USER": "operator", "AGENT_USER": "agent", "LLAMA_UNIT": "a b"}):
            with self.subTest(env=env):
                out, _ = helper.respond(b'{"verb": "llama.status"}', env)
                self.assertEqual(out["error"], "failed")


class Llama(Base):
    def test_status_running(self) -> None:
        fake = self.use({show_argv("operator", "llama-server"): (0, fixture("llama-show-running.txt"), "")})
        r = self.ask('{"verb": "llama.status"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"], {
            "active_state": "active", "sub_state": "running", "result": "success",
            "active_since": 1792537434, "inactive_since": 1792535668,
            "main_pid": 1858856, "restarts": 0,
        })
        self.assertEqual(fake.calls[0][1], helper.SHOW_TIMEOUT_S)

    def test_status_stopped(self) -> None:
        self.use({show_argv("operator", "llama-server"): (0, fixture("llama-show-stopped.txt"), "")})
        d = self.ask('{"verb": "llama.status"}')["data"]
        self.assertEqual((d["active_state"], d["sub_state"], d["main_pid"]), ("inactive", "dead", None))
        self.assertEqual(d["inactive_since"], 1792535668)

    def test_stop_then_status(self) -> None:
        stop = ("/usr/bin/systemctl", "--user", "-M", "operator@", "stop", "llama-server")
        fake = self.use({
            stop: (0, "", ""),
            show_argv("operator", "llama-server"): (0, fixture("llama-show-stopped.txt"), ""),
        })
        r = self.ask('{"verb": "llama.stop"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["active_state"], "inactive")
        self.assertEqual([c[0][4] for c in fake.calls], ["stop", "show"])
        self.assertEqual(fake.calls[0][1], helper.CONTROL_TIMEOUT_S)

    def test_start(self) -> None:
        start = ("/usr/bin/systemctl", "--user", "-M", "operator@", "start", "llama-server")
        self.use({
            start: (0, "", ""),
            show_argv("operator", "llama-server"): (0, fixture("llama-show-running.txt"), ""),
        })
        self.assertEqual(self.ask('{"verb": "llama.start"}')["data"]["active_state"], "active")

    def test_control_failure(self) -> None:
        start = ("/usr/bin/systemctl", "--user", "-M", "operator@", "start", "llama-server")
        self.use({start: (1, "", "Failed to connect to bus: No medium found\n")})
        r = self.ask('{"verb": "llama.start"}')
        self.assertEqual((r["error"], r["message"]), ("failed", "Failed to connect to bus: No medium found"))

    def test_control_timeout(self) -> None:
        stop = ("/usr/bin/systemctl", "--user", "-M", "operator@", "stop", "llama-server")
        self.use({stop: (-1, "", "")})
        self.assertEqual(self.ask('{"verb": "llama.stop"}')["error"], "timeout")

    def test_unit_not_found(self) -> None:
        self.use({show_argv("operator", "llama-server"): (0, "LoadState=not-found\nActiveState=inactive\n", "")})
        self.assertEqual(self.ask('{"verb": "llama.status"}')["error"], "unit_not_found")

    def test_custom_unit_names(self) -> None:
        self.use({show_argv("op2", "llm"): (0, fixture("llama-show-running.txt"), "")})
        out, _ = helper.respond(b'{"verb":"llama.status"}', {"LLAMA_USER": "op2", "LLAMA_UNIT": "llm", "AGENT_USER": "agent"})
        self.assertTrue(out["ok"], out)


class Hermes(Base):
    def test_status(self) -> None:
        self.use({show_argv("agent", "hermes-gateway"): (0, fixture("hermes-show-running.txt"), "")})
        d = self.ask('{"verb": "hermes.status"}')["data"]
        self.assertEqual(d["active_since"], 1792535518)
        self.assertIsNone(d["inactive_since"])  # empty value = never


class Gpus(Base):
    def test_running(self) -> None:
        self.use({GPU_ARGV: (0, fixture("gpu-running.csv"), "")})
        g = self.ask('{"verb": "gpu.status"}')["data"]["gpus"]
        self.assertEqual(len(g), 2)
        self.assertEqual(g[0], {
            "index": 0, "name": "NVIDIA GeForce RTX 4070 SUPER", "temp_c": 51.0,
            "mem_used_mib": 11381, "mem_total_mib": 12282, "util_pct": 6.0,
            "power_w": 40.39, "power_limit_w": 220.0,
        })
        self.assertEqual((g[1]["name"], g[1]["mem_used_mib"], g[1]["mem_total_mib"]),
                         ("NVIDIA GeForce RTX 3050", 6693, 8192))

    def test_stopped(self) -> None:
        self.use({GPU_ARGV: (0, fixture("gpu-stopped.csv"), "")})
        g = self.ask('{"verb": "gpu.status"}')["data"]["gpus"]
        self.assertEqual([x["mem_used_mib"] for x in g], [1266, 15])

    def test_not_available_values(self) -> None:
        self.use({GPU_ARGV: (0, "0, NVIDIA GeForce RTX 3050, [N/A], 15, 8192, [Not Supported], [N/A], 130.00\n", "")})
        g = self.ask('{"verb": "gpu.status"}')["data"]["gpus"][0]
        self.assertEqual((g["temp_c"], g["util_pct"], g["power_w"], g["power_limit_w"]), (None, None, None, 130.0))

    def test_no_devices(self) -> None:
        self.use({GPU_ARGV: (6, "No devices were found\n", "")})
        r = self.ask('{"verb": "gpu.status"}')
        self.assertEqual((r["error"], r["message"]), ("failed", "nvidia-smi: No devices were found"))

    def test_malformed_line(self) -> None:
        self.use({GPU_ARGV: (0, "0, NVIDIA, 51\n", "")})
        self.assertEqual(self.ask('{"verb": "gpu.status"}')["error"], "failed")


class Process(unittest.TestCase):
    """The script itself over stdin/stdout, as systemd runs it (no verb that executes)."""

    def run_script(self, stdin: bytes) -> tuple[dict, str]:
        p = subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "bin", "delve-helper")],
            input=stdin, capture_output=True, timeout=20, env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0]), p.stderr.decode()

    def test_unknown_verb(self) -> None:
        out, log = self.run_script(b'{"verb": "reboot"}\n')
        self.assertEqual(out["error"], "unknown_verb")
        self.assertIn("result=unknown_verb", log)

    def test_empty(self) -> None:
        out, _ = self.run_script(b"")
        self.assertEqual(out["error"], "bad_request")

    def test_settings_missing(self) -> None:
        out, _ = self.run_script(b'{"verb": "llama.status"}\n')
        self.assertEqual(out["error"], "failed")
        self.assertIn("LLAMA_USER", out["message"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
