# Architecture

## Goals

- A personal agent that is **always on** and runs **entirely on local hardware**:
  model, agent runtime, scheduled jobs.
- Full agent functionality from a phone, anywhere: streaming, visible tool steps,
  approve/deny for dangerous commands — including approvals answered hours later.
- Proactive output: the agent and deterministic scripts publish typed "cards"
  (lake water level today; disk health and system status next).
- Testable and operable like a small production system: pinned versions, recorded
  protocol fixtures, deterministic tests, a post-boot health check, regression list.

Non-goals: multiple users, public internet exposure, GUI automation, writing a custom
agent runtime.

## Process and user layout

```
user <operator> (uid 1000)                       user agent (uid 1001)
──────────────────────────                       ──────────────────────────────────────
systemd --user                                   systemd --user           (linger)
  llama-server.service      (linger)               hermes-gateway.service
    127.0.0.1:8080                                   + override.conf (drop-in)
    Qwen3.8-27B, -c 65536 -np 1                      ExecStartPre=wait-llama.sh
    2 GPUs, layer split 11:6                         127.0.0.1:8642  API server
                                                     ├─ agent loop  → terminal (local backend)
                                                     ├─ plugin agentcards
                                                     ├─ cron scheduler
                                                     └─ ~/.hermes/{config.yaml,.env,SOUL.md,cards/,logs/}
```

- The model server belongs to the operator because the model files and the build live
  there. The agent user only reaches it over HTTP on localhost.
- The agent user cannot read the operator's home; the operator cannot read the agent's.
  Files are exchanged through `/tmp` (tmpfs, sticky bit). Operator-side scripts in
  `ops/` reach the agent with `sudo machinectl shell agent@ …`.
- Hermes' terminal backend is `local`: commands run as `agent`. No container — the user
  account is the isolation boundary, and it is simpler to reason about than a container
  with mounted host paths.

## Boot sequence

1. `systemd --user` for both users starts at boot thanks to linger — no login needed.
2. `llama-server` loads the model (~9 s cold).
3. `hermes-gateway` runs `wait-llama.sh` first: up to 60 × 2 s polling of
   `127.0.0.1:8080/health`, logging `healthy after ~Ns` or failing after 120 s.
   `TimeoutStartSec=180` covers the wait; `RestartSec=15` paces retries if the model
   server stays down.
4. The gateway starts the API server, the cron scheduler and the plugin.
5. `ops/post-boot-check.sh` (run over SSH) verifies the whole chain.

The Hermes-generated gateway unit is never edited; all local changes are in the
drop-in, so a Hermes update can regenerate the unit without losing them.

## Agent turn and approvals

```
client ─POST /v1/runs─▶ Hermes ─▶ model ─▶ tool call
                                   │
                    ┌──────────────┴──────────────────────────────────────────┐
                    │ terminal command checks, in order                       │
                    │ 1 hardline (built-in)      → blocked, never bypassable  │
                    │ 2 runtime self-protection  → blocked                    │
                    │ 3 sudo stdin guard         → blocked                    │
                    │ 4 user deny list           → blocked                    │
                    │ 5 dangerous patterns       → approval.request (SSE)     │
                    │ otherwise                  → runs                       │
                    └─────────────────────────────────────────────────────────┘
approval.request ─▶ client ─POST /v1/runs/{id}/approval {"choice":"once|deny"}─▶ Hermes
no answer in 300 s ─▶ command blocked, run continues, model is told
```

- Deny-list patterns are `fnmatch` patterns matched against the **whole, lower-cased**
  command (and normalized variants), hence the leading `*`.
- Plugin tools bypass the approval layers (the tool registry dispatches them directly).
  That is why plugin tools take no paths and validate every field.
- Hermes also offers `always` (permanent allow). The client will never send it; the
  backend will drop it.

## Cards

A card is one JSON line in `~agent/.hermes/cards/events.jsonl`:

```json
{"v":1,"id":"<uuid4 hex>","type":"water","severity":"info","ts":"2026-10-16T19:26:01Z",
 "origin":"script","payload":{"station":"Siófok","level_cm":32.0,
 "measured_at":"2026-10-16T05:00:00Z","trend_cm_24h":-2.0,"lake_avg_cm":30.0,
 "lake_avg_trend_cm_24h":-1.0,"data_source":"hydroinfo.hu (OVSZ) tohid"}}
```

- One shared stdlib-only module (`schemas.py`) validates payloads and builds events; a
  second (`store.py`) appends with `O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW`, an exclusive
  `flock`, mode `0600`, max 8 KiB per line. Both the plugin tool and the cron script use
  them, so there is one schema and one writer implementation.
- Validation rejects unknown fields, booleans or strings where numbers are expected,
  NaN/inf, naive timestamps, timestamps more than 15 minutes in the future or 30 days in
  the past, and out-of-range values. Times are normalized to UTC, numbers to float.
- The `id` is the consumer's dedup key. Schema changes are additive (optional fields);
  `v` changes only on a breaking change.

### Water-level job

```
Hermes cron (52 8,12,20 * * *, --no-agent)
  └─ water_level.py
       fetch public HTML table (UA identifies the script, 20 s timeout, 256 KiB cap)
       parse: anchor on station codes, verify name/lake/9 cells, reject duplicates
       check: level and 24 h change ranges; inconsistent change = warning only
       freshness: observed date ≤ 2 days old
       dedup: once per observed date (water_state.json)
       build_event("water", …, "info", "script") → append_event
```

Contract for no-agent scripts: stdout stays empty, diagnostics go to stderr **and** an
own log file (Hermes does not keep stderr of successful runs), and any failure exits
non-zero — Hermes then records the stderr and opens a cron incident. Exit codes:
`0` published or nothing new, `1` fetch, `2` parse, `3` validation/storage/stale data.

## Network

- Model server and API server listen on `127.0.0.1` only.
- Remote access is over Tailscale (SSH with keys only today; the backend later).
- The host prefers IPv4 (`gai.conf` precedence plus Hermes `network.force_ipv4`).
- Outbound traffic of the agent works both through a VPN interface and directly; the
  post-boot check reports which route the agent user's traffic takes.

## Planned: backend and client

The backend is a **client** of the Hermes API server — Hermes is not imported and no
gateway adapter is written. It will own the parts Hermes does not provide:

- durable approvals (pending approvals survive the 300 s Hermes timeout; a late approval
  starts a new run in the same session and is auto-approved only if the command hash
  matches);
- a single global run queue (one model slot);
- a Postgres mirror of conversations, events, tool steps, approvals and cards;
- a WebSocket protocol and push notifications for the phone;
- health monitoring and post-boot reporting.

The observed API contract it builds on is in [`protocol.md`](protocol.md).
