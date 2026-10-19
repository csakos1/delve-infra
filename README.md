# Delve — infrastructure

[![ci](https://github.com/csakos1/delve-infra/actions/workflows/ci.yml/badge.svg)](https://github.com/csakos1/delve-infra/actions/workflows/ci.yml)

Infrastructure-as-code for an always-on, **fully local** personal AI agent on a single
Arch Linux workstation: a 27B model served by `llama.cpp` across two consumer GPUs,
[Hermes Agent](https://github.com/NousResearch/hermes-agent) as the agent runtime,
running as an unprivileged Linux user, with deterministic background jobs that publish
structured "cards" for **Delve**, the (planned) Flutter phone client.

This repository holds the sanitized, versioned copy of everything that lives on the
host: systemd units, the agent's curated configuration, a small Hermes plugin, a
no-agent cron script, privilege rules, operational scripts, and the tests and protocol
fixtures that pin the behaviour down.

> **Status (2026-10):** inference, agent runtime, API server, plugin and cron job are
> running. The full stack (units, drop-in, boot ordering, plugin, cron job) is
> reboot-tested (2026-10-19). The FastAPI
> backend and the Flutter client are the next steps and will live in their own
> repositories.

## Contents

- [Architecture](#architecture)
- [Security model](#security-model)
- [Repository layout](#repository-layout)
- [Installation order](#installation-order)
- [Tests](#tests)
- [Regression checklist](#regression-checklist)
- [Measurements](#measurements)
- [Known limitations](#known-limitations)
- Further reading: [`docs/architecture.md`](docs/architecture.md),
  [`docs/decisions.md`](docs/decisions.md), [`docs/protocol.md`](docs/protocol.md)

## Architecture

```
 phone client (planned)  ⇄  WebSocket + push, over Tailscale only  ⇄  backend (planned, FastAPI + Postgres)
                                                                          ⇅ HTTP + SSE, 127.0.0.1, Bearer
                              Hermes gateway / API server  (user: agent, 127.0.0.1:8642)
                                │  systemd user unit + drop-in, linger, ExecStartPre: wait-llama.sh
                                ├─ Runs API (SSE events, approvals, stop)  ·  Sessions API
                                ├─ SOUL.md "Operating rules" (loaded on every run)
                                ├─ plugin agentcards → publish_water_card → cards/events.jsonl
                                └─ cron scheduler
                                     └─ balaton-water (no-agent, 3×/day) → scripts/water_level.py
                                          └─ public lake gauge table → validate → cards/events.jsonl
                                                          ⇅ HTTP, 127.0.0.1
                              llama-server  (user: <operator>, 127.0.0.1:8080)
                                   Qwen3.8-27B UD-IQ4_XS · 64K context · 1 slot · RTX 4070 SUPER + RTX 3050
```

| Component | Runs as | Listens on | Started by |
|---|---|---|---|
| `llama-server` | operator user | `127.0.0.1:8080` | systemd **user** unit, linger |
| Hermes gateway + API server | dedicated `agent` user | `127.0.0.1:8642` | systemd user unit + drop-in, linger |
| `balaton-water` job | `agent` (inside the gateway's scheduler) | – | Hermes cron, `--no-agent` |
| backend / client | – | Tailscale only | planned |

Everything starts from systemd at boot without a graphical login. The gateway waits
for the model server's health endpoint before it starts (`bin/wait-llama.sh`).

Details: [`docs/architecture.md`](docs/architecture.md).

## Security model

The agent has a real shell. The design assumes the model **will** occasionally do the
wrong thing and puts the boundary where it cannot be talked around:

1. **The Linux user is the sandbox.** Hermes runs as `agent` with no password and no
   general sudo. It cannot read the operator's home. The only sudo rule is a
   byte-exact whitelist of read-only `smartctl` invocations on four disks addressed by
   `/dev/disk/by-id/…` (`etc/sudoers.d/agent.template`).
2. **Hermes approval layers**, in order: built-in hardline rules (never bypassable) →
   runtime self-protection → sudo-stdin guard → the user deny list → "dangerous"
   patterns that require explicit human approval (`approvals.mode: manual`, 300 s).
   The deny list is a guard rail, not a boundary: it is tested deterministically
   (`tests/deny-matrix.py`, 19 cases) against Hermes' own matcher.
3. **Localhost only.** Both the model server and the Hermes API server bind to
   `127.0.0.1`. Only the (planned) backend will be exposed, and only on the tailnet.
4. **Behavioural rules** in `hermes/SOUL.md`: state facts about the system only from
   tool output; never modify an approved command (in testing the model escalated
   `rm -r` to `rm -rf` on its own); no retry in another form after a block; say so
   explicitly if a requested value or command was changed.
5. **Plugin tools have no path parameters.** `publish_water_card` writes to a fixed
   JSONL file (`O_APPEND|O_NOFOLLOW`, `flock`, `0600`) after strict schema validation.
6. **Secrets never enter command lines** (the agent can read the journal, including
   sudo's full command lines), and never enter this repository: see
   [Repository hygiene](#repository-hygiene).

## Repository layout

```
bin/
  render-sudoers            fills etc/sudoers.d/agent.template from site.env, checks disks exist
  wait-llama.sh             ExecStartPre: wait up to 120 s for llama-server /health
etc/
  gai.conf.fragment         prefer IPv4 in getaddrinfo()
  ssh/sshd_config.d/        key-only SSH
  sudoers.d/agent.template  smartctl whitelist with @DISK_n@ placeholders
hermes/
  SOUL.md                   identity + "Operating rules"
  config.fragments.yaml     curated, secret-free excerpt of the agent's config.yaml
  cron/balaton-water.sh     idempotent creation of the cron job
  plugins/agentcards/       Hermes plugin: card schema, validator, JSONL store, tool
  scripts/water_level.py    no-agent cron script (stdlib only)
ops/
  config-check.py           dump / verify the live config against the fragment
  deny-check.sh             run the deny matrix as the agent user
  post-boot-check.sh        full health check after a reboot
systemd/
  llama-server.service      user unit for the model server
  hermes-gateway.override.conf  drop-in for the Hermes-generated gateway unit
  reference/                the generated unit, for reference only (not edited)
tests/
  test_*.py                 offline, stdlib-only tests (run in CI)
  deny-matrix.py, lib.sh, toolcall_test.py, longctx_test.py   on-host tests
  fixtures/                 recorded API protocol, runs, cards, toolsets (sanitized);
                            synthetic gauge table + its generator
site.env.example            host-specific values (disk ids) – copy to site.env
```

## Installation order

This is a record of a working setup, not a turnkey installer. Paths use
`/home/<operator>` for the inference user; host-specific values come from `site.env`.

1. **NVIDIA driver + CUDA**, then `llama.cpp` built from source at a pinned commit
   (CUDA, `sm_86;sm_89`). Model: Qwen3.8-27B, Unsloth UD-IQ4_XS GGUF.
2. **`systemd/llama-server.service`** as a user unit of the operator, `loginctl
   enable-linger`. Check: `curl -s 127.0.0.1:8080/health`.
3. **`agent` user** (no password, member of `systemd-journal`, linger), Hermes Agent
   installed into it (pinned version, "Blank Slate", custom OpenAI-compatible endpoint).
4. **Configuration:** apply the keys in `hermes/config.fragments.yaml`, then verify:
   `$PY -I ops/config-check.py --fragment hermes/config.fragments.yaml` (as `agent`,
   with the Hermes Python) → `N/N match`. Secrets go to `~agent/.hermes/.env` (0600).
5. **Gateway:** let Hermes generate its unit, then install
   `systemd/hermes-gateway.override.conf` as a drop-in and `bin/wait-llama.sh`.
6. **Plugin:** copy `hermes/plugins/agentcards/` to `~agent/.hermes/plugins/`,
   `hermes plugins enable agentcards`, restart the gateway.
7. **Behavioural rules:** `hermes/SOUL.md` → `~agent/.hermes/SOUL.md`.
8. **Cron:** `hermes/scripts/water_level.py` → `~agent/.hermes/scripts/`, then
   `sh hermes/cron/balaton-water.sh` as `agent`.
9. **Privileges:** copy `site.env.example` to `site.env`, fill in the disk ids, then
   ```sh
   bin/render-sudoers > /tmp/agent.sudoers && sudo visudo -cf /tmp/agent.sudoers \
     && sudo install -m 0440 -o root -g root /tmp/agent.sudoers /etc/sudoers.d/agent
   ```
10. **Host:** `etc/gai.conf.fragment`, `etc/ssh/sshd_config.d/10-no-password.conf`.
11. **Reboot without logging in**, connect over SSH, run `ops/post-boot-check.sh`.

## Tests

| Test | Where | What it proves |
|---|---|---|
| `tests/test_agentcards.py` | CI + host | plugin tool: validation, event building, JSONL store (temp dir) |
| `tests/test_water_schema.py` | CI + host | `water` payload validator: ranges, types, time window, unknown fields |
| `tests/test_water_level.py` | CI + host | gauge table parser against the synthetic fixture + 13 malformed variants |
| fixture reproducibility | CI | `make_tohid_synthetic.py` regenerates a byte-identical fixture |
| script dry-run | CI | `water_level.py --dry-run` produces a valid v1 `water` event |
| gitleaks (push range; full history weekly and on demand) | CI | no secrets in any commit |
| `ops/deny-check.sh` → `tests/deny-matrix.py` | host | 19 commands × expected outcome through Hermes' own matcher (`pass` / `appr` / `deny` / `hard`) |
| `tests/lib.sh` | host | replay of the API protocol cases (run, approve, deny, timeout, stop, sessions) |
| `tests/toolcall_test.py`, `tests/longctx_test.py` | host | tool-calling reliability and long-context speed against `llama-server` directly |
| `ops/post-boot-check.sh` | host | after a real reboot: units, GPUs, health, plugin, cron, routing, open incidents, one live run |

The offline tests use only the standard library and run from any directory:

```sh
for t in test_water_level test_water_schema test_agentcards; do python3 -I tests/$t.py; done
```

The on-host tests need the live Hermes install (its Python and modules) or the running
model server, so they are not part of CI.

### Repository hygiene

- A versioned pre-commit hook (`.githooks/pre-commit`, enabled with
  `git config core.hooksPath .githooks`) blocks a commit on any of: a local denylist
  kept **outside** the repo (addresses, account names, disk serials, home paths),
  any 64-hex-character string (the API key format), or a gitleaks finding.
- Host-specific values are placeholders (`@DISK_1@`, `%h`, `$HOME`, `/home/<operator>`)
  filled from a git-ignored `site.env`. Rendered output must be **byte-identical** to
  the live file.
- Recorded fixtures were collected in a staging directory outside the repo, scanned as
  a whole, and sanitized before import. Third-party page content is not stored; the
  parser is tested against a generated look-alike.

## Regression checklist

Run after any Hermes, `llama.cpp`, kernel or NVIDIA/CUDA update:

1. `ops/deny-check.sh` → 19/19.
2. `/health` reports the expected Hermes version; capabilities unchanged.
3. `hermes plugins doctor agentcards`; `/v1/toolsets` lists `agentcards` enabled.
4. `tools.tool_search.enabled` is still `"off"`; a card run takes one tool round.
5. Protocol cases via `tests/lib.sh`, compared with `tests/fixtures/protocol/`.
6. `toolcall_test.py`, `longctx_test.py` against the model server.
7. Real reboot + `ops/post-boot-check.sh` → no failed units, `open incidents: 0`.
8. `hermes doctor`.
9. Offline tests + `water_level.py --dry-run --force`.
10. Cron job active, a manual run `succeeded`, no open incidents.
11. `ops/config-check.py --fragment …` → all keys match; rendered sudoers byte-identical.

## Measurements

Two GPUs, layer split 11:6 (RTX 4070 SUPER 12 GB on PCIe Gen4 x16, also driving the
desktop; RTX 3050 8 GB on a chipset x4 slot), 64K context, q8_0 KV cache, one slot.

| Metric | Value |
|---|---|
| Generation, short context | ~20 tok/s |
| Generation at ~50K context | 15.7 tok/s |
| Prompt processing | ~1100–1270 tok/s |
| Cold start of the model at boot | ~9 s |
| Card run, all tools eager | 17.1K input tokens, 1 tool round |
| Same run with Hermes Tool Search | 34.5K input tokens, 3 tool rounds → Tool Search disabled |

## Known limitations

- **One slot.** `-np 1` is required to give a single session the 64K context Hermes
  needs; agent-mode cron jobs and interactive runs compete for it. The water-level job
  is deliberately no-agent and uses no LLM.
- **The 27B model is good at short, well-scoped tasks** and weaker in long tool chains,
  and it may alter a requested command or value despite instructions. Hence few steps,
  eager tools, strict validation and the behavioural rules.
- **The deny list matches strings**, not semantics. It is tested, but it is not a
  security boundary.
- **Pinned versions.** Hermes and `llama.cpp` are updated deliberately, followed by the
  regression checklist.
- **The data source is a scraped HTML table.** A format change makes the script fail
  with exit code 2, which opens a Hermes cron incident instead of publishing bad data.

## License

[MIT](LICENSE). Hermes Agent is MIT-licensed as well; Qwen model weights and
`llama.cpp` are under their own licenses.
