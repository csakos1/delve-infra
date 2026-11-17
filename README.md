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
> reboot-tested (2026-10-19). The backend
> ([`csakos1/delve-backend`](https://github.com/csakos1/delve-backend), FastAPI +
> PostgreSQL) runs as a hardened system service behind `tailscale serve` and is
> reboot-tested too (2026-10-19). The Flutter client is the next step and will live in its
> own repository.

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
 phone client (planned)  ⇄  HTTPS + WSS, tailscale serve, tailnet only  ⇄  delve-backend (user: delve, 127.0.0.1:8700)
                                                                          ⇅ HTTP + SSE, 127.0.0.1, Bearer
                              Hermes gateway / API server  (user: agent, 127.0.0.1:8642)
                                │  systemd user unit + drop-in, linger, ExecStartPre: wait-llama.sh
                                ├─ Runs API (SSE events, approvals, stop)  ·  Sessions API
                                ├─ SOUL.md "Operating rules" (loaded on every run)
                                ├─ plugin agentcards → publish_water_card → cards/events.jsonl
                                │                    page_fetch (one public page, bounded)
                                │                    watch_manage → watches/<slug>.json + cron job
                                └─ cron scheduler
                                     ├─ balaton-water (no-agent, 3×/day) → scripts/water_level.py
                                     │    └─ public lake gauge table → validate → cards/events.jsonl
                                     ├─ arch-updates (no-agent, daily 07:15) → scripts/updates_check.py
                                     │    └─ checkupdates → validate → cards/events.jsonl
                                     └─ watch:<slug> (no-agent, ≥30 min) → scripts/watch_<slug>.py
                                          └─ watchlib: page → items → filters → new? → cards/events.jsonl
                                                          ⇅ HTTP, 127.0.0.1
                              llama-server  (user: <operator>, 127.0.0.1:8080)
                                   Qwen3.8-27B UD-IQ4_XS · 64K context · 1 slot · RTX 4070 SUPER + RTX 3050
```

| Component | Runs as | Listens on | Started by |
|---|---|---|---|
| `llama-server` | operator user | `127.0.0.1:8080` | systemd **user** unit, linger |
| Hermes gateway + API server | dedicated `agent` user | `127.0.0.1:8642` | systemd user unit + drop-in, linger |
| `balaton-water`, `arch-updates`, `watch:*` jobs | `agent` (inside the gateway's scheduler) | – | Hermes cron, `--no-agent` |
| `delve-backend` | system user `delve` | `127.0.0.1:8700`; tailnet via `tailscale serve` | system unit (`systemd/delve-backend.service`) |
| phone client | – | – | planned |

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
   `127.0.0.1`. Only the backend is exposed, and only on the tailnet (`tailscale serve`).
   The app's terminal reaches OpenSSH on port 2222, from the tailnet only, with its own
   key restricted to the tailnet range and without forwarding (D-48); the backend is not
   on that path.
4. **Behavioural rules** in `hermes/SOUL.md`: state facts about the system only from
   tool output; never modify an approved command (in testing the model escalated
   `rm -r` to `rm -rf` on its own); no retry in another form after a block; say so
   explicitly if a requested value or command was changed.
5. **Plugin tools have no path parameters.** `publish_water_card` writes to a fixed
   JSONL file (`O_APPEND|O_NOFOLLOW`, `flock`, `0644`) after strict schema validation.
   `page_fetch` and the watchers take a URL but reach only public http(s) addresses
   (no loopback, private, link-local or tailnet answers, checked on every redirect;
   `docs/decisions.md` D-41); a page loaded in the headless Chromium reaches the network
   only through a proxy that applies the same rule to every connection (D-46); watchers
   obey robots.txt (D-47). `watch_manage` writes only its own files under
   `~/.hermes/watches/` and `scripts/watch_<slug>.py`, from a fixed template; a watcher on
   any other site is a recipe (data) for a fixed engine, picked from candidates with real
   samples and created only after a preview (D-44, D-45).
6. **Secrets never enter command lines** (the agent can read the journal, including
   sudo's full command lines), and never enter this repository: see
   [Repository hygiene](#repository-hygiene).

## Repository layout

```
bin/
  delve-env                 writes /etc/delve/env from a development env file
  delve-helper              root helper for the backend: fixed verbs over a unix socket
  delve-desk                desktop agent in the operator's session: shows the phone's
                            notifications with their apps' icons (two verbs, asks the
                            backend on the loopback)
  render-sudoers            fills etc/sudoers.d/agent.template from site.env, checks disks exist
  wait-llama.sh             ExecStartPre: wait up to 120 s for llama-server /health
etc/
  gai.conf.fragment         prefer IPv4 in getaddrinfo()
  ssh/sshd_config.d/        key-only SSH; 30-delve.conf: OpenSSH on 22 and 2222 (the app's terminal)
  sysusers.d/delve.conf     the backend's system user
  tmpfiles.d/delve-inbox.conf  the attachment inbox: 2750 delve:agent
  tmpfiles.d/delve-persona.conf  the personal profile's directory: 2750 delve:agent
  tmpfiles.d/delve-news.conf  the News feed list the backend writes: 2750 delve:agent
  delve/helper.env.example  users and units the helper controls
  delve/desk.env.example    the desktop agent's token (the operator's ~/.config/delve/desk.env)
  sudoers.d/agent.template  smartctl whitelist with @DISK_n@ placeholders
llama/
  presets/                  one environment file per model for llama-server.service
hermes/
  SOUL.md                   identity + "Operating rules"
  config.fragments.yaml     curated, secret-free excerpt of the agent's config.yaml
  cron/balaton-water.sh     idempotent creation of the water-level cron job
  cron/arch-updates.sh      idempotent creation of the package-update cron job
  cron/news.sh              idempotent creation of the News collector's cron job
  plugins/agentcards/       Hermes plugin: card schema, validator, JSONL store, tools
                            (publish_water_card, page_fetch, watch_manage), watchlib.py,
                            recipes.py (recipes on any page), render_page.py (headless
                            Chromium behind a filtering proxy; runs in its own venv),
                            newslib.py (the News collector: feeds -> batch files)
  scripts/water_level.py    no-agent cron script: lake gauge → `water` card (stdlib only)
  scripts/updates_check.py  no-agent cron script: checkupdates → `updates` card (stdlib only)
  scripts/news_collect.py   no-agent cron script: the News feeds → `cards/news/batch-*.json`
ops/
  config-check.py           dump / verify the live config against the fragment
  deny-check.sh             run the deny matrix as the agent user
  post-boot-check.sh        full health check after a reboot
persona.example/           the format of the personal profile, with an invented person
systemd/
  llama-server.service      user unit for the model server (loads the active preset)
  delve-backend.service     system unit for the backend, hardened (loopback only)
  delve-backend.service.d/  drop-ins: fcm.conf (outbound HTTPS), cards.conf (card store),
                            inbox.conf (attachment inbox), persona.conf (personal profile),
                            news.conf (the News feed list)
  delve-helper.socket, delve-helper@.service   socket-activated helper, one request each
  delve-desk.service        user unit for the desktop agent (graphical session)
  hermes-gateway.override.conf  drop-in for the Hermes-generated gateway unit
  reference/                the generated unit, for reference only (not edited)
tests/
  test_*.py                 offline, stdlib-only tests (run in CI)
  fixtures/watch/           synthetic hardverapro / cinemacity pages + their generator
  deny-matrix.py, lib.sh, toolcall_test.py, longctx_test.py   on-host tests
  fixtures/                 recorded API protocol, runs, cards, toolsets, system samples (sanitized);
                            synthetic gauge table + its generator
site.env.example            host-specific values (disk ids) – copy to site.env
```

## Installation order

This is a record of a working setup, not a turnkey installer. Paths use
`/home/<operator>` for the inference user; host-specific values come from `site.env`.

1. **NVIDIA driver + CUDA**, then `llama.cpp` built from source at a pinned commit
   (CUDA, `sm_86;sm_89`). Model: Qwen3.8-27B, Unsloth UD-IQ4_XS GGUF.
2. **`systemd/llama-server.service`** as a user unit of the operator, `loginctl
   enable-linger`. The model and its settings are a **preset**: copy
   `llama/presets/qwen38-27b-iq4xs.env.example` to `~/.config/delve/llama/qwen38-27b-iq4xs.env`
   (fill in the paths) and
   `ln -sfn qwen38-27b-iq4xs.env ~/.config/delve/llama/active.env`; the unit loads whatever
   `active.env` points to. One file per model; the Delve app switches between them (D-49).
   Check: `curl -s 127.0.0.1:8080/health`.
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
8. **Cron:** `hermes/scripts/water_level.py` and `hermes/scripts/updates_check.py` →
   `~agent/.hermes/scripts/`, then `sh hermes/cron/balaton-water.sh` and
   `sh hermes/cron/arch-updates.sh` as `agent` (the latter needs `pacman-contrib`).
9. **Privileges:** copy `site.env.example` to `site.env`, fill in the disk ids, then
   ```sh
   bin/render-sudoers > /tmp/agent.sudoers && sudo visudo -cf /tmp/agent.sudoers \
     && sudo install -m 0440 -o root -g root /tmp/agent.sudoers /etc/sudoers.d/agent
   ```
10. **Host:** `etc/gai.conf.fragment`, `etc/ssh/sshd_config.d/10-no-password.conf`.
11. **Backend user and settings:**
    `sudo install -D -m 0644 etc/sysusers.d/delve.conf /etc/sysusers.d/delve.conf`
    (Arch has no `/etc/sysusers.d` by default), `sudo systemd-sysusers`; PostgreSQL role and database `delve` (peer auth over the
    unix socket, `listen_addresses = ''`); then `sudo bin/delve-env <dev.env>` writes
    `/etc/delve/env` (root:delve 0640) from a working development env file.
12. **Backend:** from a `delve-backend` checkout, `sudo deploy/install.sh` builds
    `/opt/delve/releases/<sha>`, migrates as `delve`, switches `/opt/delve/current` and
    installs the `delve` CLI wrapper (`sudo delve pair`). Then
    `systemd/delve-backend.service` → `/etc/systemd/system/`,
    `sudo systemctl enable --now delve-backend`; check
    `curl -s 127.0.0.1:8700/api/v1/health` and `systemd-analyze security delve-backend`.
13. **Phone access:** `sudo tailscale serve --bg --https=443 http://127.0.0.1:8700`
    (MagicDNS + HTTPS certificates enabled in the tailnet); the app pairs with a code from
    `sudo delve pair`.
14. **Push notifications (optional):** a Firebase project with an Android app
    (the app's application id); its service account key →
    `sudo install -D -o root -g delve -m 0640 <key>.json /etc/delve/fcm.json`; add
    `FCM_SERVICE_ACCOUNT_FILE=/etc/delve/fcm.json` and `FCM_PROJECT_ID=<id>` to
    `/etc/delve/env` (`bin/delve-env` keeps them on a later `FORCE=1` run); install
    `systemd/delve-backend.service.d/fcm.conf` → `/etc/systemd/system/delve-backend.service.d/`,
    `sudo systemctl daemon-reload && sudo systemctl restart delve-backend`; the startup log
    line says `"push": true`, and `systemd-analyze security delve-backend` stays about 1.2.
15. **Cards for the backend:** copy `hermes/plugins/agentcards/` to
    `~agent/.hermes/plugins/agentcards/` again (the store writes `cards/` as 0755 and
    `events.jsonl` as 0644 - the agent's home stays 0700) and restart the gateway; fix an
    existing install at once with
    `sudo chmod 0755 ~agent/.hermes/cards && sudo chmod 0644 ~agent/.hermes/cards/events.jsonl`;
    install `systemd/delve-backend.service.d/cards.conf` →
    `/etc/systemd/system/delve-backend.service.d/`, `daemon-reload`, restart the backend; its
    log says `"cards ingested"`.
16. **Privileged helper:** the backend's unit has no capabilities, no devices and its own
    user namespace, so `sudo` and `nvidia-smi` do not work from it. A root helper with fixed
    verbs (`llama.status|start|stop`, `llama.models`, `llama.select <preset>`,
    `hermes.status`, `gpu.status`, `host.status`, `agent.incidents`) answers one JSON line
    per connection on `/run/delve-helper.sock`
    (group `delve`, 0660). `agent.incidents` runs `hermes cron incidents` in the agent's user
    manager (`systemd-run --user -M agent@`, as `systemctl` reaches it), so `HERMES_BIN` in
    `/etc/delve/helper.env` has to name the agent's `hermes` (`command -v hermes` as `agent`):
    ```sh
    sudo install -D -m 0755 bin/delve-helper /usr/local/lib/delve/delve-helper
    sudo install -m 0644 etc/delve/helper.env.example /etc/delve/helper.env   # set LLAMA_USER
    sudo install -m 0644 systemd/delve-helper.socket systemd/delve-helper@.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now delve-helper.socket
    sudo delve helper gpu.status      # from delve-backend: connects as the delve user
    ```
    Each request is one `delve-helper@….service` instance; its journal line is
    `verb=… result=… ms=…` (`journalctl -u 'delve-helper@*' -n 20`). If a verb fails with
    `permission denied` / `Operation not permitted`, run the same request without the
    unit's hardening to tell the two apart:
    `echo '{"verb":"llama.status"}' | sudo systemd-run --pipe --wait -q -p EnvironmentFile=/etc/delve/helper.env /usr/bin/python3 -I /usr/local/lib/delve/delve-helper`.
17. **Attachments:** images reach the model natively only with `model.supports_vision: true`
    in the agent's config (in `hermes/config.fragments.yaml`; restart the gateway after
    adding it). Files go to an inbox the backend writes and the agent reads: a setgid
    directory owned by `delve` with the agent's own group, so neither a new group nor a new
    login of `agent` is needed:
    ```sh
    sudo install -m 0644 etc/tmpfiles.d/delve-inbox.conf /etc/tmpfiles.d/
    sudo systemd-tmpfiles --create delve-inbox.conf
    sudo install -m 0644 systemd/delve-backend.service.d/inbox.conf /etc/systemd/system/delve-backend.service.d/
    sudo systemctl daemon-reload && sudo systemctl restart delve-backend
    sudo stat -c '%n %U:%G %A' /var/lib/delve-inbox      # delve:agent drwxr-s---
    ```
    The backend writes `<id>/<name>` (0750/0640 through its `UMask=0027`; inside its user
    namespace the group shows as 65534, on the host it is `agent`) and names the path in the
    run's instructions; the agent reads it with `read_file`, without approval.
18. **Watchers and page reading (Delve W):** copy `hermes/plugins/agentcards/` to
    `~agent/.hermes/plugins/agentcards/` again, apply the `compression` and
    `file_read_max_chars` keys of `hermes/config.fragments.yaml`, `hermes/SOUL.md` →
    `~agent/.hermes/SOUL.md`, restart the gateway; `/v1/toolsets` lists `agentcards` with
    `page_fetch` and `watch_manage`. A watcher is created from chat (`watch_manage`), which
    writes `~/.hermes/watches/<slug>.json`, `~/.hermes/scripts/watch_<slug>.py` and the cron
    job `watch:<slug>` (no-agent, `deliver: local`); its log is `~/.hermes/logs/watch.log`.
    The helper's `agent.cron.list|pause|resume|run` verbs (step 16) let the backend show and
    control them: reinstall `bin/delve-helper` and check `sudo delve helper agent.cron.list`.
    **Any page (W5):** `sudo pacman -S chromium`, then as the agent user
    `python3 -m venv ~/.hermes/delve-render/venv && ~/.hermes/delve-render/venv/bin/pip
    install playwright` (no `playwright install`: the system Chromium is used). Check:
    `page_fetch` with `mode=structure` on a public page returns numbered candidates; on a
    JavaScript page it says `"rendered": true`. Without the venv everything else works and
    rendering fails with `render_unavailable`.
19. **Terminal (Delve T, SSH on 2222):** on the tailnet address port 22 is answered by
    Tailscale SSH, so the app's terminal uses the real OpenSSH on 2222 (D-48):
    ```sh
    sudo install -m 0644 etc/ssh/sshd_config.d/30-delve.conf /etc/ssh/sshd_config.d/
    sudo sshd -t && sudo systemctl reload sshd
    sudo sshd -T | grep -i '^port '            # 22 and 2222
    sudo firewall-cmd --get-zone-of-interface=tailscale0   # trusted; no 2222 in public
    ```
    The app creates its key and shows one line to copy (More → Settings → Terminal); paste
    it into `~/.ssh/authorized_keys` of the operator from another session (Termux over
    Tailscale SSH on 22). The line keeps
    `from="100.64.0.0/10",no-agent-forwarding,no-X11-forwarding,no-port-forwarding`. The app
    pins the host key on first use (compare with
    `ssh-keyscan -p 2222 -t ed25519 127.0.0.1 | ssh-keygen -lf -`) and starts
    `env LANG=en_US.UTF-8 tmux -u new-session -A -s <name>` (an exec channel has no locale);
    the same session is reachable on the desktop with `tmux attach -t <name>`.
20. **Desktop agent (Delve DA2, the phone's notifications on the desktop):** needs a
    notification daemon in the session (`busctl --user status
    org.freedesktop.Notifications`). `sudo install -m 0755 bin/delve-desk
    /usr/local/lib/delve/delve-desk`; `sudo delve token create --name desk --role desk`
    prints the agent's token once; `install -D -m 0600 etc/delve/desk.env.example
    ~/.config/delve/desk.env` and put the token in it; check with
    `(set -a; . ~/.config/delve/desk.env; python3 -I bin/delve-desk --check)` - it names
    the daemon, asks the backend once, says where it keeps the apps' icons
    (`$XDG_RUNTIME_DIR/delve-desk`, D-55) and shows a test notification for 3 seconds. Then
    `install -D -m 0644 systemd/delve-desk.service
    ~/.config/systemd/user/delve-desk.service`, `systemctl --user daemon-reload`,
    `systemctl --user enable --now delve-desk.service`; `journalctl --user -u delve-desk
    -n 5` says `started: asking http://127.0.0.1:8700`. The unit belongs to the graphical
    session: it runs while the operator is logged in and has nothing to show otherwise
    (D-52..D-55).
21. **Personal profile (Delve P1):** what the agent is told about the person it works for
    lives in `/var/lib/delve-persona` - in no repository. The backend writes it (the app
    edits it there), the agent reads it through its group, like the inbox:
    ```sh
    sudo install -m 0644 etc/tmpfiles.d/delve-persona.conf /etc/tmpfiles.d/
    sudo systemd-tmpfiles --create delve-persona.conf
    sudo install -m 0644 systemd/delve-backend.service.d/persona.conf /etc/systemd/system/delve-backend.service.d/
    sudo systemctl daemon-reload && sudo systemctl restart delve-backend
    sudo stat -c '%n %U:%G %A' /var/lib/delve-persona /var/lib/delve-persona/topics   # delve:agent drwxr-s---
    ```
    Start from your own files in the format of `persona.example/` (an invented person):
    `sudo install -o delve -g agent -m 0640 profile.md /var/lib/delve-persona/` and
    `sudo install -o delve -g agent -m 0640 topics/*.md /var/lib/delve-persona/topics/`.
    A chat started after that gets the core in its instructions and the list of topic
    files; a chat that is already running keeps what it started with (D-56, D-57).
22. **News (Delve N1):** an hourly no-agent job reads the feeds the backend lists and
    hands the new items back as batch files; the backend scores and stores them (D-58).
    The list of feeds and topics says what the operator reads - it lives in the backend's
    database and in `/var/lib/delve-news`, in no repository:
    ```sh
    sudo install -m 0644 etc/tmpfiles.d/delve-news.conf /etc/tmpfiles.d/
    sudo systemd-tmpfiles --create delve-news.conf
    sudo install -m 0644 systemd/delve-backend.service.d/news.conf /etc/systemd/system/delve-backend.service.d/
    sudo systemctl daemon-reload && sudo systemctl restart delve-backend
    ```
    Copy `hermes/plugins/agentcards/` to `~agent/.hermes/plugins/` and
    `hermes/scripts/news_collect.py` to `~agent/.hermes/scripts/`, restart the gateway, then
    `sh hermes/cron/news.sh` as `agent`. The backend writes `sources.json` once it has
    sources (`sudo delve news import < news.json`, the format in the backend's
    `deploy/news.example.json`); until then a run does nothing. Check with
    `python3 -I ~/.hermes/scripts/news_collect.py --dry-run` as `agent` and
    `ls ~agent/.hermes/cards/news/`.
23. **Reboot without logging in**, connect over SSH, run `ops/post-boot-check.sh`.

## Tests

| Test | Where | What it proves |
|---|---|---|
| `tests/test_agentcards.py` | CI + host | plugin tool: validation, event building, JSONL store (temp dir) |
| `tests/test_delve_helper.py` | CI + host | privileged helper: request parsing, verb whitelist, job-id argument rules, exact commands, `systemctl show` / `nvidia-smi` / `hermes cron incidents` / `jobs.json` / `/proc` parsing on the samples in `tests/fixtures/system/` |
| `tests/test_delve_desk.py` | CI + host | desktop agent: the exact `busctl` argv of the two verbs, text shown and not interpreted (leading dash, markup, control characters, lengths), replace and close by id, bad commands, the loopback-only setting, the backend's answers, the loop's pauses, nothing of a notification in the log |
| `tests/test_updates_check.py` | CI + host | `checkupdates` parser on a real 44-package output, `updates` payload validator |
| `tests/test_water_schema.py` | CI + host | `water` payload validator: ranges, types, time window, unknown fields |
| `tests/test_water_level.py` | CI + host | gauge table parser against the synthetic fixture + 13 malformed variants |
| `tests/test_watch.py` | CI + host | watchers: public-address guard (incl. redirects), hardverapro / cinemacity / generic parsers on synthetic pages, filters, config rules, the run cycle (baseline, new items, cap, errors, `watch_*` cards), `page_fetch`, `watch_manage` against a fake Hermes cron |
| `tests/test_news.py` | CI + host | News collector: the sources file, RSS 2.0 / RSS 1.0 / Atom parsing (a DTD refused), Google News titles and publishers, URL normalisation, the time window, the run cycle (due sources, 304, a 429 with `Retry-After`, seen items, the time budget, removed sources), the batch file and the state, real HTTP on the loopback with the public-address guard on the feed and on a redirect |
| `tests/test_recipes.py` | CI + host | recipes: selectors, JSON paths, recipe validation, list / JSON / condition extraction, the candidate finder on synthetic list / `__NEXT_DATA__` / product / API / JavaScript-shell pages, robots.txt (rules, 4xx, unreachable, cache), the run cycle (baseline, `recipe_empty`, recipe change, condition edges), `watch_manage` with candidates and the preview gate, the renderer wrapper; with Playwright and a Chromium also `render_page.py` itself (a JavaScript-built list, its API captured, a redirect and a fetch to loopback refused) |
| fixture reproducibility | CI | `make_tohid_synthetic.py` and `make_watch_synthetic.py` regenerate byte-identical fixtures |
| script dry-run | CI | `water_level.py --dry-run` and `updates_check.py --dry-run` produce valid v1 `water` / `updates` events |
| gitleaks (push range; full history weekly and on demand) | CI | no secrets in any commit |
| `ops/deny-check.sh` → `tests/deny-matrix.py` | host | 19 commands × expected outcome through Hermes' own matcher (`pass` / `appr` / `deny` / `hard`) |
| `tests/lib.sh` | host | replay of the API protocol cases (run, approve, deny, timeout, stop, sessions) |
| `tests/toolcall_test.py`, `tests/longctx_test.py` | host | tool-calling reliability and long-context speed against `llama-server` directly |
| `ops/post-boot-check.sh` | host | after a real reboot: units, GPUs, health, plugin, cron, routing, open incidents, one live run |

The offline tests use only the standard library and run from any directory:

```sh
for t in test_water_level test_water_schema test_agentcards test_delve_helper test_delve_desk test_updates_check test_watch test_recipes test_news; do python3 -I tests/$t.py; done
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
3. `hermes plugins doctor agentcards`; `/v1/toolsets` lists `agentcards` enabled with
   `publish_water_card`, `page_fetch`, `watch_manage`; `watch_manage action=list` answers
   (it calls Hermes' `tools.cronjob_tools.cronjob` in-process - an internal entry point).
4. `tools.tool_search.enabled` is still `"off"`; a card run takes one tool round.
5. Protocol cases via `tests/lib.sh`, compared with `tests/fixtures/protocol/`.
6. `toolcall_test.py`, `longctx_test.py` against the model server.
7. Real reboot + `ops/post-boot-check.sh` → no failed units, `open incidents: 0`.
8. `hermes doctor`.
9. Offline tests + `water_level.py --dry-run --force`.
10. Cron job active, a manual run `succeeded`, no open incidents.
11. `ops/config-check.py --fragment …` → all keys match; rendered sudoers byte-identical.
12. After a Chromium or Playwright update: `tests/test_recipes.py` as the agent user with
    `~/.hermes/delve-render/venv/bin/python` (the live renderer tests run, not skip).

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
- **Watchers scrape pages.** A layout change of a watched site makes its watcher fail
  with exit code 2 (an error `watch_run` card and a cron incident), never with made-up
  items; `hardverapro` only reads the first result page (robots.txt forbids paging).
- **The data source is a scraped HTML table.** A format change makes the script fail
  with exit code 2, which opens a Hermes cron incident instead of publishing bad data.
- **Watchers on any page read the first screen of one URL.** No login, no captcha, no
  infinite scrolling or paging; a site that redesigns its page breaks the recipe
  (`recipe_empty`, fixed from chat with a new candidate).

## License

[MIT](LICENSE). Hermes Agent is MIT-licensed as well; Qwen model weights and
`llama.cpp` are under their own licenses.
