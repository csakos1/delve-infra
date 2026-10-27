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


class Host(Base):
    def setUp(self) -> None:
        super().setUp()
        self._proc = helper.PROC
        self.tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"delve-proc-{os.getpid()}")
        os.makedirs(os.path.join(self.tmp, "sys", "kernel", "random"), exist_ok=True)
        for src, dst in (("proc-uptime.txt", "uptime"), ("proc-stat-excerpt.txt", "stat"),
                         ("proc-boot-id.txt", "sys/kernel/random/boot_id")):
            with open(os.path.join(self.tmp, dst), "w", encoding="ascii") as f:
                f.write(fixture(src))
        helper.PROC = self.tmp

    def tearDown(self) -> None:
        helper.PROC = self._proc
        super().tearDown()

    def test_host_status(self) -> None:
        fake = self.use({})
        r = self.ask('{"verb": "host.status"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"], {"uptime_s": 135358, "booted_at": 1792443359,
                                     "boot_id": "8ea7721c-4790-4a12-a981-b52a87f45566"})
        self.assertEqual(fake.calls, [])  # files only, no command

    def test_host_status_without_btime(self) -> None:
        with open(os.path.join(self.tmp, "stat"), "w", encoding="ascii") as f:
            f.write("cpu  1 2 3\n")
        r = self.ask('{"verb": "host.status"}')
        self.assertEqual((r["ok"], r["error"]), (False, "failed"))
        self.assertIn("btime", r["message"])


INC_ENV = dict(ENV, HERMES_BIN="/home/agent/.local/bin/hermes")
INC_ARGV = (
    "/usr/bin/systemd-run", "--user", "-M", "agent@", "--pipe", "--wait", "--quiet",
    "--collect", "--", "/home/agent/.local/bin/hermes", "cron", "incidents",
)


class Incidents(Base):
    def ask_inc(self, env: dict[str, str] = INC_ENV) -> dict:
        out, _ = helper.respond(b'{"verb": "agent.incidents"}', env)
        return json.loads(json.dumps(out))

    def test_closed_incident(self) -> None:
        fake = self.use({INC_ARGV: (0, fixture("incidents-closed.txt"), "")})
        r = self.ask_inc()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["incidents"], [{
            "id": "98de85_009110dce5cf", "state": "closed", "job": "98de85fe6a1f",
            "type": "script", "first_seen": "2026-10-16T21:35:16.146881+02:00",
            "last_seen": "2026-10-16T21:35:16.146881+02:00",
            "error": "Script exited with code 2 stderr: cron_failtest: szimulált parse_failed",
        }])
        self.assertNotIn("/home/agent/.hermes/cron/output", json.dumps(r))
        self.assertEqual(fake.calls[0][1], helper.AGENT_TIMEOUT_S)

    def test_open_incident(self) -> None:
        self.use({INC_ARGV: (0, fixture("incidents-detected-synthetic.txt"), "")})
        r = self.ask_inc()
        self.assertEqual([i["state"] for i in r["data"]["incidents"]], ["detected"])

    def test_none(self) -> None:
        self.use({INC_ARGV: (0, fixture("incidents-none-detected.txt"), "")})
        self.assertEqual(self.ask_inc()["data"], {"incidents": []})

    def test_layout_change_fails_loudly(self) -> None:
        broken = fixture("incidents-closed.txt").replace("1 incident(s)", "2 incident(s)")
        self.use({INC_ARGV: (0, broken, "")})
        r = self.ask_inc()
        self.assertEqual((r["ok"], r["error"]), (False, "failed"))
        self.use({INC_ARGV: (0, "something else\n", "")})
        self.assertEqual(self.ask_inc()["error"], "failed")

    def test_command_fails(self) -> None:
        self.use({INC_ARGV: (1, "", "Failed to connect to bus: No medium found\n")})
        r = self.ask_inc()
        self.assertEqual(r["error"], "failed")
        self.assertIn("No medium found", r["message"])

    def test_hermes_bin_required_and_checked(self) -> None:
        self.use({})
        for value in ("", "hermes", "/home/agent/../root/x", "/usr/bin/hermes;id", "/a b"):
            with self.subTest(value=value):
                r = self.ask_inc(dict(ENV, HERMES_BIN=value))
                self.assertEqual(r["error"], "failed")
                self.assertIn("HERMES_BIN", r["message"])


CRON_LIST_ARGV = (
    "/usr/bin/systemd-run", "--user", "-M", "agent@", "--pipe", "--wait", "--quiet",
    "--collect", "--", "/usr/bin/cat", "--", "/home/agent/.hermes/cron/jobs.json",
)


def cron_argv(action: str, job: str, wait: bool = True) -> tuple[str, ...]:
    head = ("/usr/bin/systemd-run", "--user", "-M", "agent@")
    mid = ("--pipe", "--wait", "--quiet", "--collect", "--") if wait else ("--quiet", "--collect", "--")
    return head + mid + ("/home/agent/.local/bin/hermes", "cron", action, job)


class Cron(Base):
    def ask_env(self, raw: str, env: dict[str, str] = INC_ENV) -> dict:
        out, _ = helper.respond(raw.encode(), env)
        return json.loads(json.dumps(out))

    def test_list(self) -> None:
        self.use({CRON_LIST_ARGV: (0, fixture("cron-jobs.json"), "")})
        r = self.ask_env('{"verb": "agent.cron.list"}')
        self.assertTrue(r["ok"], r)
        jobs = {j["name"]: j for j in r["data"]["jobs"]}
        self.assertEqual(set(jobs), {"balaton-water", "arch-updates", "watch:rtx-4070-super",
                                     "agent-mode-sample"})
        w = jobs["watch:rtx-4070-super"]
        self.assertEqual((w["id"], w["state"], w["enabled"], w["no_agent"], w["schedule"]),
                         ("5a1f0c2e9b44", "paused", False, True, "15 8,13,19 * * *"))
        a = jobs["agent-mode-sample"]
        self.assertFalse(a["no_agent"])
        self.assertEqual(len(a["last_error"]), 200)
        self.assertNotIn("SECRET-LOOKING", json.dumps(r))
        self.assertNotIn("prompt", a)

    def test_list_missing_file_and_bad_content(self) -> None:
        self.use({CRON_LIST_ARGV: (1, "", "/usr/bin/cat: /home/agent/.hermes/cron/jobs.json: No such file or directory\n")})
        self.assertEqual(self.ask_env('{"verb": "agent.cron.list"}')["data"], {"jobs": []})
        self.use({CRON_LIST_ARGV: (0, "not json", "")})
        self.assertEqual(self.ask_env('{"verb": "agent.cron.list"}')["error"], "failed")

    def test_list_home_setting(self) -> None:
        argv = CRON_LIST_ARGV[:-1] + ("/srv/agent/.hermes/cron/jobs.json",)
        self.use({argv: (0, '{"jobs": []}', "")})
        r = self.ask_env('{"verb": "agent.cron.list"}', dict(INC_ENV, AGENT_HERMES_HOME="/srv/agent/.hermes"))
        self.assertEqual(r["data"], {"jobs": []})
        r = self.ask_env('{"verb": "agent.cron.list"}', dict(INC_ENV, AGENT_HERMES_HOME="/srv/../etc"))
        self.assertEqual(r["error"], "failed")

    def test_pause_resume_run(self) -> None:
        fake = self.use({
            cron_argv("pause", "5a1f0c2e9b44"): (0, "Paused job: watch:rtx (5a1f0c2e9b44)\n", ""),
            cron_argv("resume", "5a1f0c2e9b44"): (0, "Resumed job\n", ""),
            cron_argv("run", "5a1f0c2e9b44", wait=False): (0, "", "Running as unit: run-u12.service\n"),
        })
        for verb, started in (("pause", False), ("resume", False), ("run", True)):
            with self.subTest(verb=verb):
                r = self.ask_env(json.dumps({"verb": f"agent.cron.{verb}", "arg": "5a1f0c2e9b44"}))
                self.assertTrue(r["ok"], r)
                self.assertEqual(r["data"], {"job_id": "5a1f0c2e9b44", "action": verb, "started": started})
        self.assertEqual(len(fake.calls), 3)

    def test_not_found(self) -> None:
        self.use({cron_argv("pause", "abcdef"): (1, "", "Job not found: abcdef\n")})
        r = self.ask_env('{"verb": "agent.cron.pause", "arg": "abcdef"}')
        self.assertEqual(r["error"], "unit_not_found")

    def test_arg_rules(self) -> None:
        fake = self.use({})
        for raw in ('{"verb": "agent.cron.pause"}',
                    '{"verb": "agent.cron.pause", "arg": "--all"}',
                    '{"verb": "agent.cron.run", "arg": "5A1F0C2E9B44"}',
                    '{"verb": "agent.cron.run", "arg": "abc"}',
                    '{"verb": "agent.cron.run", "arg": ["5a1f0c2e9b44"]}',
                    '{"verb": "agent.cron.run", "arg": "5a1f0c2e9b44", "x": 1}',
                    '{"verb": "agent.cron.list", "arg": "5a1f0c2e9b44"}',
                    '{"verb": "gpu.status", "arg": "5a1f0c2e9b44"}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.ask_env(raw)["error"], "bad_request")
        self.assertEqual(fake.calls, [])


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
