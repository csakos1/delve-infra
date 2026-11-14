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


PRESET_DIR = "/home/operator/.config/delve/llama"
MODELS_ARGV = (
    "/usr/bin/systemd-run", "--user", "-M", "operator@", "--pipe", "--wait", "--quiet",
    "--collect", "--", "/usr/bin/python3", "-I", "/usr/local/lib/delve/delve-helper",
    "--presets", PRESET_DIR,
)


def select_argv(name: str, directory: str = PRESET_DIR) -> tuple[str, ...]:
    return MODELS_ARGV[:9] + ("/usr/bin/ln", "-sfn", "--", f"{name}.env", f"{directory}/active.env")


def scan_json(active: str | None = "qwen-a") -> str:
    row = {"file": "Qwen3.8-27B-UD-IQ4_XS.gguf", "label": None, "note": None, "ctx": 98304,
           "vision": True, "thinking": "toggle", "size_bytes": 14252845984, "usable": True,
           "reason": None}
    return json.dumps({"active": active, "models": [
        dict(row, name="qwen-a"),
        dict(row, name="qwen-b", ctx=65536, label="Qwen 64K"),
        dict(row, name="small", ctx=32768, usable=False, reason="context_below_64k"),
    ]})


class Presets(Base):
    """`llama.models`, `llama.select` and the scan the llama user runs (`--presets`)."""

    def test_models(self) -> None:
        fake = self.use({MODELS_ARGV: (0, scan_json(), "")})
        r = self.ask('{"verb": "llama.models"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["active"], "qwen-a")
        self.assertEqual([m["name"] for m in r["data"]["models"]], ["qwen-a", "qwen-b", "small"])
        self.assertEqual(r["data"]["models"][2]["reason"], "context_below_64k")
        self.assertEqual(len(fake.calls), 1)

    def test_models_output_is_not_trusted(self) -> None:
        rows = [
            {"name": "../x", "usable": True},
            {"name": "ok", "usable": "yes", "vision": 1, "ctx": "big", "size_bytes": True,
             "thinking": "sometimes", "label": "L" * 200, "file": "f.gguf", "extra": "dropped"},
            "junk",
        ]
        self.use({MODELS_ARGV: (0, json.dumps({"active": "gone", "models": rows}), "")})
        d = self.ask('{"verb": "llama.models"}')["data"]
        self.assertIsNone(d["active"])
        self.assertEqual(len(d["models"]), 1)
        m = d["models"][0]
        self.assertEqual((m["usable"], m["vision"], m["ctx"], m["size_bytes"], m["thinking"]),
                         (False, False, None, None, "toggle"))
        self.assertEqual(len(m["label"]), 60)
        self.assertNotIn("extra", m)

    def test_models_failures(self) -> None:
        self.use({MODELS_ARGV: (1, "", "Failed to connect to bus: No medium found\n")})
        r = self.ask('{"verb": "llama.models"}')
        self.assertEqual(r["error"], "failed")
        self.assertIn("No medium found", r["message"])
        self.use({MODELS_ARGV: (0, "not json", "")})
        self.assertEqual(self.ask('{"verb": "llama.models"}')["error"], "failed")

    def test_preset_dir_setting(self) -> None:
        argv = MODELS_ARGV[:-1] + ("/srv/llama/presets",)
        self.use({argv: (0, scan_json(None), "")})
        out, _ = helper.respond(b'{"verb": "llama.models"}', dict(ENV, LLAMA_PRESET_DIR="/srv/llama/presets"))
        self.assertTrue(out["ok"], out)
        out, _ = helper.respond(b'{"verb": "llama.models"}', dict(ENV, LLAMA_PRESET_DIR="/srv/../etc"))
        self.assertEqual(out["error"], "failed")

    def test_select(self) -> None:
        fake = self.use({MODELS_ARGV: (0, scan_json(), ""), select_argv("qwen-b"): (0, "", "")})
        r = self.ask('{"verb": "llama.select", "arg": "qwen-b"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["data"]["active"], r["data"]["previous"]), ("qwen-b", "qwen-a"))
        self.assertEqual(r["data"]["model"]["ctx"], 65536)
        # the listing first, then the link; the unit is never touched
        self.assertEqual([c[0][9] for c in fake.calls], ["/usr/bin/python3", "/usr/bin/ln"])

    def test_select_active_is_a_no_op(self) -> None:
        fake = self.use({MODELS_ARGV: (0, scan_json(), "")})
        r = self.ask('{"verb": "llama.select", "arg": "qwen-a"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(fake.calls), 1)

    def test_select_refused(self) -> None:
        fake = self.use({MODELS_ARGV: (0, scan_json(), "")})
        r = self.ask('{"verb": "llama.select", "arg": "nope"}')
        self.assertEqual(r["error"], "unknown_model")
        r = self.ask('{"verb": "llama.select", "arg": "small"}')
        self.assertEqual(r["error"], "model_not_usable")
        self.assertIn("context_below_64k", r["message"])
        self.assertEqual(len(fake.calls), 2)  # two listings, no link

    def test_select_bad_names_run_nothing(self) -> None:
        fake = self.use({})
        for arg in ("", "A", "../x", "a/b", "a b", "-x", "x" * 41, "a;id", "active.env/.."):
            with self.subTest(arg=arg):
                r = self.ask(json.dumps({"verb": "llama.select", "arg": arg}))
                self.assertEqual(r["error"], "bad_request")
        for raw in ('{"verb": "llama.select"}', '{"verb": "llama.select", "arg": 1}',
                    '{"verb": "llama.models", "arg": "x"}'):
            self.assertEqual(self.ask(raw)["error"], "bad_request")
        self.assertEqual(fake.calls, [])

    def test_select_link_failure(self) -> None:
        self.use({MODELS_ARGV: (0, scan_json(), ""),
                  select_argv("qwen-b"): (1, "", "/usr/bin/ln: failed to create symbolic link\n")})
        r = self.ask('{"verb": "llama.select", "arg": "qwen-b"}')
        self.assertEqual(r["error"], "failed")

    def test_env_file_parsing(self) -> None:
        env = helper.parse_env_file(
            "# comment\n; other\n\nLLAMA_ARG_MODEL=/m/a.gguf\n LLAMA_ARG_CTX_SIZE = 98304 \n"
            'DELVE_LABEL="Qwen  Q6"\nDELVE_NOTE=\'after the 3060\'\nLLAMA_EXTRA_ARGS=\n'
            "bad line\n1X=2\nLLAMA_ARG_TENSOR_SPLIT=38,26\n"
        )
        self.assertEqual(env, {
            "LLAMA_ARG_MODEL": "/m/a.gguf", "LLAMA_ARG_CTX_SIZE": "98304",
            "DELVE_LABEL": "Qwen  Q6", "DELVE_NOTE": "after the 3060", "LLAMA_EXTRA_ARGS": "",
            "LLAMA_ARG_TENSOR_SPLIT": "38,26",
        })

    def test_preset_rows(self) -> None:
        sizes = {"/m/a.gguf": 14_000_000_000, "/m/mmproj.gguf": 900_000_000}
        base = {"LLAMA_ARG_MODEL": "/m/a.gguf", "LLAMA_ARG_CTX_SIZE": "98304"}
        cases = [
            (base, True, None, False),
            (dict(base, LLAMA_ARG_MMPROJ="/m/mmproj.gguf"), True, None, True),
            (dict(base, LLAMA_ARG_MMPROJ="/m/gone.gguf"), False, "mmproj_missing", False),
            (dict(base, LLAMA_ARG_CTX_SIZE="65536"), True, None, False),
            (dict(base, LLAMA_ARG_CTX_SIZE="65535"), False, "context_below_64k", False),
            (dict(base, LLAMA_ARG_CTX_SIZE="lots"), False, "no_context", False),
            ({"LLAMA_ARG_MODEL": "/m/a.gguf"}, False, "no_context", False),
            (dict(base, LLAMA_ARG_MODEL="/m/gone.gguf"), False, "model_missing", False),
            (dict(base, LLAMA_ARG_MODEL="a.gguf"), False, "no_model", False),
            ({}, False, "no_model", False),
        ]
        for env, usable, reason, vision in cases:
            with self.subTest(env=env):
                row = helper.preset_row("p", env, sizes.get)
                self.assertEqual((row["usable"], row["reason"], row["vision"]), (usable, reason, vision))
        row = helper.preset_row("p", dict(base, DELVE_THINKING="Always", DELVE_LABEL=" Qwen   Q6 ",
                                          DELVE_NOTE="x" * 200), sizes.get)
        self.assertEqual((row["thinking"], row["label"], len(row["note"])), ("always", "Qwen Q6", 80))
        self.assertEqual((row["file"], row["size_bytes"], row["ctx"]), ("a.gguf", 14_000_000_000, 98304))
        self.assertEqual(helper.preset_row("p", dict(base, DELVE_THINKING="maybe"), sizes.get)["thinking"], "toggle")
        self.assertNotIn("/m/", json.dumps(row))  # the path stays on the host

    def test_scan_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "Model-Q4_K_M.gguf")
            with open(model, "wb") as f:
                f.write(b"x" * 10)

            def preset(name: str, ctx: int, path: str = model) -> None:
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(f"LLAMA_ARG_MODEL={path}\nLLAMA_ARG_CTX_SIZE={ctx}\n")

            preset("b.env", 65536)
            preset("a.env", 98304)
            preset("gone.env", 98304, os.path.join(d, "missing.gguf"))
            preset("Upper.env", 98304)       # not a preset name
            preset("notes.txt", 98304)       # not a preset file
            os.symlink("a.env", os.path.join(d, "alias.env"))  # a link is not a preset
            self.assertIsNone(helper.scan_presets(d)["active"])
            os.symlink("b.env", os.path.join(d, "active.env"))
            out = helper.scan_presets(d)
            self.assertEqual([m["name"] for m in out["models"]], ["a", "b", "gone"])
            self.assertEqual(out["active"], "b")
            self.assertEqual(out["models"][0]["size_bytes"], 10)
            self.assertEqual(out["models"][2]["reason"], "model_missing")
            os.remove(os.path.join(d, "active.env"))
            os.symlink("/etc/passwd", os.path.join(d, "active.env"))
            self.assertIsNone(helper.scan_presets(d)["active"])
            # what the scan prints is what llama.models accepts
            self.assertEqual(helper.parse_presets(json.dumps(out))["models"], out["models"])
        self.assertEqual(helper.scan_presets(os.path.join(d, "nowhere")), {"active": None, "models": []})

    def test_min_ctx_setting(self) -> None:
        # unset or 65536: the scan command is unchanged; above it, the bar is passed on
        fake = self.use({MODELS_ARGV: (0, scan_json(), ""), MODELS_ARGV + ("131072",): (0, scan_json(), "")})
        for value, argv in (("", MODELS_ARGV), ("65536", MODELS_ARGV), ("131072", MODELS_ARGV + ("131072",))):
            with self.subTest(value=value):
                r, _ = helper.respond(b'{"verb": "llama.models"}', dict(ENV, LLAMA_MIN_CTX=value))
                self.assertTrue(r["ok"], r)
                self.assertEqual(tuple(fake.calls[-1][0]), argv)
        for value in ("65535", "0", "128k", "-131072", "131072.0", "99999999", "１３１０７２"):
            with self.subTest(value=value):
                r, _ = helper.respond(b'{"verb": "llama.models"}', dict(ENV, LLAMA_MIN_CTX=value))
                self.assertEqual(r["error"], "failed")
                self.assertIn("LLAMA_MIN_CTX", r["message"])
        self.assertEqual(helper.llama_min_ctx({}), 65536)
        self.assertEqual(helper.llama_min_ctx({"LLAMA_MIN_CTX": " 131072 "}), 131072)

    def test_min_ctx_raises_the_bar(self) -> None:
        sizes = {"/m/a.gguf": 1}
        base = {"LLAMA_ARG_MODEL": "/m/a.gguf"}
        for ctx, bar, usable in ((98304, 65536, True), (98304, 131072, False), (131072, 131072, True),
                                 (163840, 131072, True), (65536, 1, True), (65535, 1, False)):
            with self.subTest(ctx=ctx, bar=bar):
                row = helper.preset_row("p", dict(base, LLAMA_ARG_CTX_SIZE=str(ctx)), sizes.get, bar)
                self.assertEqual((row["usable"], row["reason"]),
                                 (usable, None if usable else "context_below_64k"))

    def test_scan_mode_takes_the_bar(self) -> None:
        import tempfile

        script = os.path.join(ROOT, "bin", "delve-helper")
        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "Model-Q4_K_M.gguf")
            with open(model, "wb") as f:
                f.write(b"x")
            with open(os.path.join(d, "a.env"), "w", encoding="utf-8") as f:
                f.write(f"LLAMA_ARG_MODEL={model}\nLLAMA_ARG_CTX_SIZE=98304\n")

            def scan(*extra: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, "-I", script, "--presets", d, *extra],
                                      capture_output=True, text=True, timeout=30, check=False,
                                      stdin=subprocess.DEVNULL)

            self.assertTrue(json.loads(scan().stdout)["models"][0]["usable"])
            row = json.loads(scan("131072").stdout)["models"][0]
            self.assertEqual((row["usable"], row["reason"]), (False, "context_below_64k"))
            for bad in ("lots", "1", "99999999"):
                with self.subTest(bad=bad):
                    p = scan(bad)
                    self.assertEqual((p.returncode, p.stdout), (2, ""))

    def test_scan_mode_of_the_script(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(
                [sys.executable, "-I", os.path.join(ROOT, "bin", "delve-helper"), "--presets", d],
                capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL,
            )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"active": None, "models": []})

    def test_shipped_preset_is_usable(self) -> None:
        with open(os.path.join(ROOT, "llama", "presets", "qwen38-27b-iq4xs.env.example"), encoding="utf-8") as f:
            env = helper.parse_env_file(f.read())
        row = helper.preset_row("qwen38-27b-iq4xs", env, lambda _path: 1)
        self.assertEqual((row["usable"], row["ctx"], row["vision"], row["thinking"]),
                         (True, 98304, True, "toggle"))
        self.assertIn("LLAMA_EXTRA_ARGS", env)
        self.assertTrue(helper.PRESET_RE.fullmatch("qwen38-27b-iq4xs"))
        with open(os.path.join(ROOT, "systemd", "llama-server.service"), encoding="utf-8") as f:
            unit = f.read()
        for needed in ("EnvironmentFile=%h/.config/delve/llama/active.env", "$LLAMA_EXTRA_ARGS",
                       "--host 127.0.0.1", "--port 8080", "-np 1"):
            self.assertIn(needed, unit)


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
