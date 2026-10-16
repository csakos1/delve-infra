#!/bin/sh
# Vár a llama-server /health-re (betöltés alatt 503 -> curl -f hibát ad), max ~120 s.
i=0
while [ "$i" -lt 60 ]; do
  if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null; then
    echo "wait-llama: llama-server healthy after ~$((i*2))s"
    exit 0
  fi
  i=$((i+1)); sleep 2
done
echo "wait-llama: llama-server not healthy after 120s" >&2
exit 1
