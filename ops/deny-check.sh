#!/bin/sh
# Deny/hardline/approval regresszió az agent Hermes-környezetében (csakosból futtatva)
sudo machinectl shell agent@ /usr/bin/bash -lc '
  PY=$(sed -n 2p ~/.hermes/hermes-agent/.hermes/bin/hermes | awk "{print \$2}")
  $PY -I ~/hermes-proto/deny-matrix.py; echo "exit=$?"
' 2>&1 | grep -v 'local host'
