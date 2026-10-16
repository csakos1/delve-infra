"""agentcards – append-only JSONL eseménytár (flock). Csak stdlib."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

MAX_LINE = 8192


def default_events_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "cards" / "events.jsonl"


def append_event(event: dict, path=None) -> Path:
    p = Path(path) if path else default_events_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError(f"event too large ({len(data)} > {MAX_LINE} bytes)")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return p
