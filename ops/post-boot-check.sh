#!/bin/bash
export SYSTEMD_PAGER=
echo "=== uptime ==="; uptime
echo "=== system failed ==="; systemctl --failed --no-legend
echo "=== csakos user failed ==="; systemctl --user --failed --no-legend
echo "=== GPU ==="; nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
echo "=== net ==="; ip -br link | grep -Ei 'proton|pvpn|tailscale'; tailscale status 2>&1 | head -3
echo "=== llama (s boot óta) ==="
journalctl --user -u llama-server -b -o short-monotonic --no-pager | grep -Ei 'Started|listening|model loaded' | head -5
curl -s -m 2 127.0.0.1:8080/health; echo
echo "=== agent: gateway (s boot óta) ==="
sudo machinectl shell agent@ /usr/bin/bash -lc '
  export SYSTEMD_PAGER=
  systemctl --user --failed --no-legend
  systemctl --user show hermes-gateway -p ActiveState -p NRestarts
  journalctl --user -u hermes-gateway -b -o short-monotonic --no-pager | grep -E "Starting|wait-llama|Started|Failed" | head -8
  curl -s -m 2 127.0.0.1:8642/health; echo
  echo "=== 2c: plugin, tool_search, SOUL ==="
  hermes plugins list 2>&1 | grep -i agentcards
  KEY=$(grep "^API_SERVER_KEY=" ~/.hermes/.env | cut -d= -f2-)
  PY=$(sed -n 2p ~/.hermes/hermes-agent/.hermes/bin/hermes | awk "{print \$2}")
  curl -s -H "Authorization: Bearer $KEY" 127.0.0.1:8642/v1/toolsets | $PY -I -c "import json,sys; [print(t[\"name\"], t[\"enabled\"], t[\"tools\"]) for t in json.load(sys.stdin)[\"data\"] if t[\"name\"]==\"agentcards\"]"
  grep -A2 "^tools:" ~/.hermes/config.yaml
  echo "SOUL rules: $(grep -c "^## Operating rules" ~/.hermes/SOUL.md)"
  echo "=== 2c: cron + vízállás (login nélkül = Proton nélkül) ==="
  hermes cron status 2>&1 | grep -E "running|heartbeat"
  hermes cron list | grep -E "\[|Next run"
  ip -br link show proton0 2>/dev/null || echo "proton0: nincs (login nélkül várt)"
  ip route get 1.1.1.1 | head -1
  hermes cron run c4d09b7e1cd7 | tail -1
  tail -2 ~/.hermes/logs/water_level.log
  echo "open incidents: $(hermes cron incidents 2>&1 | grep -E "^  [0-9a-f]+_[0-9a-f]+  " | grep -vc " closed$")"
  cd ~/hermes-proto && source lib.sh >/dev/null && start reboot-a "Futtasd: uptime, és mondd meg egy mondatban az eredményt." && wait && ev reboot-a.events.sse
' 2>&1 | grep -v -E 'Connected to the local host|Connection to the local host'
