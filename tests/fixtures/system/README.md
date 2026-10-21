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
