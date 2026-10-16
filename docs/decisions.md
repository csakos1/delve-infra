# Decision log

Condensed from the working notes. Each entry: what was decided, and why. Grouped by
area; numbers are stable references (`D-07`), not chronology.

## Model and inference

**D-01 Qwen3.8-27B, official instruct build.** A dense 27B with native tool calling,
hybrid thinking and a long native context. An "abliterated" variant was tried and
dropped: with shell access, the ability to refuse and reliable tool calls matter more
than fewer refusals.

**D-02 `llama.cpp` (`llama-server`) instead of Ollama.** Measured faster on this
two-GPU setup and gives direct control over split, KV cache type, slots and sampling.
Built from source at a pinned commit; not updated casually.

**D-03 Unsloth UD-IQ4_XS, layer split 11:6, q8_0 KV cache, 64K context.** The largest
quality/context combination that fits 12 GB + 8 GB with the desktop running on the
first GPU.

**D-04 One slot (`-np 1`).** Two slots would halve the per-slot context to 32K, below the
64K Hermes requires. Consequence: everything that generates is serialized.

**D-05 Thinking off by default.** Latency and token cost for routine operations; can be
enabled per request later.

## Agent runtime

**D-06 Hermes Agent as the runtime, not a custom loop.** Mature tool layer, approvals,
sessions, cron, plugins, and a versioned HTTP API with capability discovery. The version
is pinned (`v0.22.2`) and updates go through the regression checklist.

**D-07 A dedicated `agent` Linux user is the sandbox; terminal backend `local`.**
Simpler to reason about than a container with mounted host paths, and it composes with
normal Unix permissions, sudoers and systemd. Docker backend rejected.

**D-08 Blank Slate install; only the needed toolsets** (`file`, `terminal`, `vision`,
`skills`, `agentcards` → 11 tools). Fewer tools means a smaller prompt and fewer ways
to go wrong.

**D-09 Tool Search off.** With 11 tools, the search/describe bridge cost 2 extra tool
rounds and doubled input tokens (34.5K vs 17.1K for the same card run).

**D-10 `approvals.mode: manual`, 300 s timeout.** The `smart` mode makes an auxiliary
LLM call, which would occupy the single slot and evict the prompt cache.

**D-11 The repository keeps a curated config fragment, not the config file.** The live
`config.yaml` contains host state and rows for unused platforms; the fragment lists only
the deliberate settings, and `ops/config-check.py` verifies them against the live file.

## Safety

**D-12 The deny list is a guard rail, the user account is the boundary.** Deny patterns
are `fnmatch` on the whole lower-cased command, so they need a leading `*` and still
miss creative variants. They are tested deterministically with Hermes' own matching
functions (`tests/deny-matrix.py`, 19 cases), not with LLM runs.

**D-13 Behavioural rules in `SOUL.md`.** Observed: told to run exactly `rm -r …`, the
model escalated to `rm -rf` on its own; told to publish `severity=critical`, it once
silently sent `alert`. Rules: state system facts only from tool output; never modify
an approved command (no added `-f`/`--force`, no extra chains); after a block, answer
briefly and do not retry in another form; if a requested value or command is changed
anyway, say so. Spot-checked live afterwards (n=1).

**D-14 No permanent allow from the phone.** Clients offer *allow once* and *deny*
(and, secondarily, *this session*); `always` is filtered out even though Hermes
offers it.

**D-15 Narrow, literal sudo.** The only rule: `smartctl` with exactly `-H`, `-a` or
`-a -j` on four disks by `/dev/disk/by-id/…`. Raw device names, reordered flags, test
or settings commands are all rejected by sudo itself.

**D-16 Journal access through the `systemd-journal` group.** The agent can read all
journals, including sudo's full command lines. Accepted with a rule: secrets never go
on a command line. A narrower whitelisted helper is an open follow-up.

**D-17 Cron jobs get no destructive tools.**

## Operations

**D-18 Everything starts from systemd with linger; no login needed after boot.**

**D-19 The generated gateway unit is not edited.** Local changes live in a drop-in:
`ExecStartPre=wait-llama.sh`, `TimeoutStartSec=180` (the wait counts toward the start
timeout), `RestartSec=15`. Hermes' own restart semantics are kept.

**D-20 "Done" means reboot-tested.** Each stage ends with a real reboot, no graphical
login, SSH in, `ops/post-boot-check.sh`. When the machine is not physically reachable,
stages that add no boot-time component may be closed with a restart-based check and the
reboot done later.

**D-21 Determinism first.** Anything that can be a script is a script, not an LLM run —
including tests.

**D-22 IPv4 preferred** (`gai.conf` precedence and Hermes `force_ipv4`).

## Cards and the water-level job

**D-23 One tool per card type, one shared schema module.** `publish_water_card` today,
`publish_disk_card` next; the stdlib-only `schemas.py`/`store.py` are shared by the
plugin and by no-agent scripts.

**D-24 Event format v1** `{v, id, type, severity, ts, origin, session_id?, payload}`;
`id` is the consumer's dedup key; schema growth only through optional fields.

**D-25 The JSONL path is not a tool parameter.** Fixed file, append-only, `flock`,
`O_NOFOLLOW`, `0600`, 8 KiB line cap.

**D-26 Strict validation, recoverable errors.** Invalid input returns
`{"success": false, "error": "validation_failed", "details": [...]}` instead of raising,
so the model can correct itself once.

**D-27 Water level as a no-agent cron script.** Fetching and parsing a table needs no
LLM; it is cheaper, deterministic and does not compete for the model slot.

**D-28 Report, don't interpret.** The card carries the Siófok gauge and the lake average
in cm with their 24 h changes; no thresholds, `severity` always `info`. The reader
judges the situation.

**D-29 `measured_at` = observed date 07:00 Europe/Budapest.** The source gives a date
for "this morning's" reading, not a time.

**D-30 One card per observed date**, deduplicated by a state file; three runs a day only
add robustness against late updates of the source.

**D-31 Script contract:** empty stdout, own log file (Hermes discards stderr on
success), non-zero exit on any failure so Hermes opens a cron incident. A parse failure
never publishes partial data.

## Repository

**D-32 Public, MIT-licensed infra repository; private first, public after a checklist.**
The project doubles as a portfolio piece for testing/operations roles.

**D-33 Never in git:** `.env`, API keys, IP addresses, tailnet and account names, disk
serial numbers, cookie-bearing response headers, the operator's home path.

**D-34 Host-specific values through placeholders + a git-ignored `site.env` + a
renderer.** The rendered result must be byte-identical to the live file
(`bin/render-sudoers` → `IDENTICAL`, `visudo -c` OK).

**D-35 Three-layer pre-commit hook:** a denylist kept outside the repository, a
64-hex-string check, gitleaks. The denylist contains `home/<operator>` rather than the
bare account name — the goal is portability, not hiding a public GitHub handle.

**D-36 Third-party content is not stored.** The gauge-table parser is tested against a
generated look-alike that reproduces the real page's structural quirks; the generator is
committed and CI checks that it reproduces the fixture byte for byte.

**D-37 Collection through a staging directory.** Files from the host are gathered
outside the repository, scanned as one set, sanitized, and only then installed.

## Rejected or deferred

| Option | Outcome | Reason |
|---|---|---|
| Abliterated model | rejected | weaker tool-call reliability, cannot say no |
| Ollama | replaced | slower here, less control |
| GUI automation | rejected | shell and `hyprctl` cover the need |
| Docker terminal backend | rejected | the user account is the sandbox |
| `approvals.mode: smart` | rejected | aux LLM call blocks the only slot |
| Two slots | rejected | 32K per slot < Hermes minimum |
| Tool Search | disabled | +2 rounds, 2× input tokens |
| Importing Hermes into the backend / custom gateway adapter | rejected | the API server is a stable, versioned contract |
| Hermes approval-transport plugin | deferred | legacy approval API is verified; revisit if it is removed |
| Backend running as `agent` | rejected | would see the agent's secrets |
