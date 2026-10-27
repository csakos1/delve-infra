# System samples (for `bin/delve-helper` and the backend's system view)

Taken on the host on 2026-10-21 (llama.cpp e287bd872, Hermes v0.22.2, driver 615.78.08).

- `gpu-running.csv`, `gpu-stopped.csv`: `nvidia-smi --query-gpu=index,name,temperature.gpu,
  memory.used,memory.total,utilization.gpu,power.draw,power.limit --format=csv,noheader,nounits`
  with the model loaded (11.4 / 6.7 GiB) and with `llama-server` stopped (1.2 GiB / 15 MiB
  left over from the desktop). A missing GPU index makes `nvidia-smi` print
  `No devices were found` on stdout and exit 6.
- `llama-show-*.txt`, `hermes-show-running.txt`: what `systemctl --user -M <user>@ show
  --timestamp=unix <unit> -p LoadState,ActiveState,…` prints when run as root. The two live
  runs were taken with and without `--timestamp=unix` and with fewer properties; these files
  combine them (values unchanged; `LoadState=loaded` added, the stopped unit's
  `ActiveEnterTimestamp` converted from `Wed 2026-10-21 00:28:45 CEST`). An unset
  timestamp prints as an empty value.
- `llama-props.json`: `GET /props` without `chat_template` (the operator's home replaced by
  `/home/operator`). The backend reads `model_path`, `model_ftype`, `total_slots` and
  `default_generation_settings.n_ctx` (65536). While the model loads, and when the server
  is down, the request fails.
- `hermes-health-detailed.json`: `GET /health/detailed` (Bearer) of the gateway: `status`
  is `degraded` because the disk check reports 96.4 % used; every other readiness check is
  `ok`. The gateway reports no uptime of its own - it comes from the unit's
  `ActiveEnterTimestamp`.

Timings seen: `systemctl --user -M <user>@ start llama-server` → `/health` 200 after 9.4 s
(model files in the page cache); after a cold boot the model is loaded at ~24 s.

## B4 samples (2026-10-21)

- `incidents-closed.txt`: `hermes cron incidents` as the agent user with one acknowledged
  incident (the cron failure test of 2026-10-16). The listing has no JSON form; the helper
  parses the blocks and checks them against the `N incident(s)` summary line, and leaves
  the `Output:` path out.
- `incidents-detected-synthetic.txt`: the same listing with the state changed to
  `detected` (synthetic - there was no open incident to record).
- `incidents-none-detected.txt`: the answer for `--state detected` with nothing in it.
- `proc-uptime.txt`, `proc-boot-id.txt`: `/proc/uptime`, `/proc/sys/kernel/random/boot_id`;
  `proc-stat-excerpt.txt`: the `btime` line of `/proc/stat` (the CPU lines shortened).
  The backend cannot read these itself (`ProcSubset=pid`), `host.status` does.

`../updates/checkupdates.txt`: `checkupdates` stdout with 44 pending updates (exit 0; exit 2
means none, anything else is an error), the same as the agent user and as the operator.

`cron-jobs.json` (#27, W0): the agent's `~/.hermes/cron/jobs.json` (Hermes v0.22.2) with the
two real no-agent jobs; the W0 probe job is turned into a paused `watch:` job, and an
invented agent-mode job (with a prompt and a long error) checks that `agent.cron.list` never
returns the prompt and clips the error.
