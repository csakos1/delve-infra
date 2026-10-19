# Hermes API server — observed contract

Observations from **Hermes Agent v0.22.2** in this setup, used as the contract for the
planned backend. Markers:

- ✅ **verified** live, with a recorded fixture under `tests/fixtures/`
- 📄 **documented** in Hermes' source or docs, not yet seen live

Hermes evolves; re-verify after every update (see the regression checklist in the
README).

## Basics

- Bind `127.0.0.1:8642`. ✅
- `GET /health` needs no auth → `{"status": "ok", "platform": "hermes-agent", "version": "0.22.2"}`. ✅
- Everything else: `Authorization: Bearer <API_SERVER_KEY>`. ✅
- At boot the gateway only starts after the model server is healthy (up to 120 s), so a
  client must wait for `/health` with backoff and then check capabilities.

## `GET /v1/capabilities` ✅

Fixture: `tests/fixtures/protocol/capabilities.json`.

- Shape: `{object: "hermes.api_server.capabilities", auth, runtime, features, endpoints}`.
  The flags are under **`features`**, not at the top level; a flag is either `true`/`false`
  or an object (`runs_idempotency: {supported, durable, retention_seconds}`).
- `runtime.mode: "server_agent"`, `tool_execution: "server"`.
- `true`: run submission, idempotency (`retention_seconds: 86400`), run status, run events
  (SSE), stop, steer, approval response, tool progress events, approval events, session
  chat (+ streaming), session fork, reasoning streaming, skills API, model options.
- `false`: `jobs_admin` (no `/api/jobs`; cron is managed with the CLI), config read/write,
  memory write, audio, realtime voice, CORS.
- No `run_unknown_resolution` capability → the **legacy approval** response format.

A client should fail fast if any capability it depends on is missing or the version is
not the one it was tested against.

## `POST /v1/runs` ✅

```json
{"input": "<text>", "session_id": "<client-generated>", "instructions": "<optional>"}
```

- Header `Idempotency-Key: <uuid>`: durable for 24 h; the same key with a different body
  → `409 idempotency_key_conflict`.
- Response `202 {"run_id": "run_<hex>", "status": "started"}`.
- Without `session_id`, the run id becomes the session id — always send one.
- Without `provider`/`model` the run uses the global route (`runtime.route_source:
  "global"`). With `"provider": "custom:<name>", "model": "<id>"` it uses that
  `custom_providers` entry for this run only; `run.completed.runtime` then reports
  `route_source: "raw_request"` and echoes `requested: {provider, model}` — a client can
  check this to confirm the route took effect. This is how per-conversation thinking
  works here (fixtures `think.*`; see the comment in `hermes/config.fragments.yaml` on
  why the thinking entry needs its own `base_url`).
- A thinking run streams no reasoning: only the answer arrives as `message.delta`, and
  `reasoning.available.text` is the answer too. The reasoning text is in the session
  messages (below).
- `instructions` is an ephemeral system-prompt addition for this run only; persistent
  rules belong in `SOUL.md`.
- Errors seen: `409 permission_denied` (session owned by another surface, e.g. the CLI);
  `429` when the concurrency cap is hit.

## `GET /v1/runs/{id}/events` — SSE ✅

- Framing: `: open`, then blocks of `id: <seq>` + `data: {json}`; `: keepalive` every 10 s;
  `: stream closed` at the end.
- Every event carries `event`, `run_id`, `timestamp`, `seq`. **Key order is not stable**
  (in `approval.request`, `event` is not first) — parse JSON, read by key.
- The stream ends after a terminal event: `run.completed`, `run.cancelled`, `run.failed`,
  `run.interrupted`.
- 📄 Reconnect with `Last-Event-ID`; until verified, recover with `GET /v1/runs/{id}`.

| Event | Fields | Notes |
|---|---|---|
| `tool.started` | `tool`, `preview` | preview is a **summary** (e.g. `"rm -rf /tmp/x + 2 commands"`), not the full command |
| `approval.request` | `request_id`, `command`, `description`, `pattern_key`, `pattern_keys[]`, `allow_permanent`, `allow_session`, `choices` | `command` is **exact**; `choices` includes `always` |
| `approval.responded` | `choice`, `request_id`, `resolved: 1` | only after an explicit answer, never on timeout |
| `tool.completed` | `tool`, `duration`, `error`, `preview` | preview is a JSON **string**, ≤ 500 chars, may be truncated |
| `message.delta` | `delta` | streamed text |
| `reasoning.available` | – | emitted even with thinking disabled |
| `run.completed` | `output`, `usage{input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_write_tokens}`, `runtime`, `completed: true` | `runtime.model` is the model file's absolute path |
| `run.cancelled` | `interrupted: true`, `turn_exit_reason: "interrupted_by_user"` | after stop |
| 📄 `message.interim`, `run.failed`, `run.interrupted`, `subagent.*` | – | must be handled |

### `tool.completed.preview`

| Situation | Content |
|---|---|
| success | `{"output": "...", "exit_code": 0, "error": null}` |
| approved | + `"approval": "...approved by the user."` |
| denied by the user | `"status": "blocked"`, `"user_summary": "You denied this command — it did not run."` |
| approval timeout (300 s) | `"status": "blocked"`, `"user_summary": "No answer within 5 minutes — the command did not run."` |
| stopped | `exit_code: 130` |
| deny list | `BLOCKED: this command matches the user-defined deny rule '<pattern>' …` |
| `publish_water_card` ok | `{"success": true, "card_id": "<hex>", "type": "water", "severity": "info", "ts": "…Z"}` |
| `publish_water_card` invalid | `{"success": false, "error": "validation_failed", "details": [...]}` |

Robust parsing: `json.loads` first; if truncated, regex for `"status": "blocked"` and
`"user_summary"`; otherwise raw text.

Approval reasons: an absolute-path `rm -r` reports `delete in root path` (any absolute
path, not only `/`), a relative one `recursive delete`.

## `GET /v1/runs/{id}` ✅

Statuses seen: `waiting_for_approval` (with an `approval` object: `request_id`,
`command`, `description`, …), `completed`, `cancelled`. 📄 `running`, `stopping`,
`failed`, `interrupted`. This is the recovery path after a client restart.

## `POST /v1/runs/{id}/approval` (legacy) ✅

```json
{"request_id": "<from approval.request>", "choice": "once" | "deny" | "session"}
```

- Success: `resolved: 1`, followed by `approval.responded` on the stream.
- `approval_not_pending` / `approval_not_active` once Hermes stopped waiting.
- `always` is offered by Hermes but a client should never send it.

## Approval timeout ✅

`approvals.timeout: 300`. On expiry there is **no** `approval.responded`; the tool
completes as blocked and the run continues normally — the model reacts to the block and
the run ends with `run.completed`. Detect it as: `approval.request`, then
`tool.completed` for the same tool without an `approval.responded` in between.

## `POST /v1/runs/{id}/stop` ✅

`stopping` → `run.cancelled` within ~1–2 s; a running tool ends with exit code 130.

## Sessions API ✅

- `GET /api/sessions/{id}` → `{"object": "hermes.session", "session": {...}}` — the
  metadata is **wrapped in `session`**: `id`, `title` (filled by title generation ~1.2 s
  after the first run), `message_count`, `tool_call_count`, `api_call_count`, token
  counters (session `input_tokens` counts only the non-cached part; `reasoning_tokens`
  stays 0 even for thinking runs). Fixture: `think.session.json`.
- `GET /api/sessions/{id}/messages?inline_images=false` →
  `{object: "list", session_id, data: [...], pagination: {limit: 500, offset, order,
  returned}}`, no total count. Message fields: `id` (integer), `role`, `content`,
  `reasoning` / `reasoning_content` (equal; the thinking text of that assistant message,
  `null` without thinking), `tool_calls`, `tool_call_id`, `tool_name`, `finish_reason`,
  `display_kind`, `token_count`, `timestamp`. A run that calls tools has several
  assistant messages, each with its own reasoning. Fixture: `think.messages.json`.

## `GET /v1/toolsets` ✅

Fixture: `tests/fixtures/toolsets/toolsets.json`.

`{"object": "list", "platform": "api_server", "data": [{name, label, description, enabled, configured, tools[]}]}`.
Enabled here: `terminal`, `file`, `vision`, `skills`, `agentcards` (11 tools).

## Behaviours a client must design around

| Behaviour | Consequence |
|---|---|
| One model slot (`-np 1`) | serialize runs in the client; a run waiting for approval keeps the slot |
| Agent-mode cron jobs and other surfaces share the slot | a slow run may be queued behind them; no-agent jobs do not compete |
| The model may change a command or value | show the **exact** `command` from `approval.request`; auto-approve only on an exact hash match |
| Deny-list and hardline blocks need no approval | show them as "blocked by rule", without approve buttons |
| Gateway restart loses in-flight runs, and startup can take 120 s+ | health loop, then reconcile open runs via `GET /v1/runs/{id}` |
| Tool Search disabled | tool names in events are the real tools, not a `tool_call` bridge |
