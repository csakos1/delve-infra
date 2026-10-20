"""agentcards – append-only JSONL eseménytár (flock). Csak stdlib."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

MAX_LINE = 8192
# Readable for the Delve backend, which sees the directory through a read-only bind
# mount (delve-infra systemd/delve-backend.service.d/cards.conf). Other users cannot
# reach it: the agent's home stays 0700. Applied on every write, whatever the umask,
# so an older 0700/0600 install is fixed by the next card.
DIR_MODE = 0o755
FILE_MODE = 0o644


def default_events_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "cards" / "events.jsonl"


def append_event(event: dict, path=None) -> Path:
    p = Path(path) if path else default_events_path()
    p.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if stat.S_IMODE(os.stat(p.parent).st_mode) != DIR_MODE:
        os.chmod(p.parent, DIR_MODE)
    data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError(f"event too large ({len(data)} > {MAX_LINE} bytes)")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != FILE_MODE:
            os.fchmod(fd, FILE_MODE)
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
