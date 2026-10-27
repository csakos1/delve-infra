#!/usr/bin/env python3
"""Dump or verify the curated subset of a Hermes config.yaml.

Run as the agent user with the Hermes Python (ruamel.yaml comes from the Hermes venv):

  config-check.py --dump [--config PATH]
      Print the curated keys of the live config, secrets redacted,
      /home/<user>/ replaced with /home/<operator>/.

  config-check.py --fragment PATH [--config PATH]
      Check that every leaf in the fragment equals the live value
      (after the same sanitizing). A fragment value of "<redacted>"
      only requires the key to be present and non-empty.
      Exit 0 = all match, 1 = drift, 2 = usage/load error.
"""
import argparse
import os
import re
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

DUMP_PATHS = [
    "model",
    "custom_providers",
    "network",
    "plugins.enabled",
    "plugins.disabled",
    "platform_toolsets.cli",
    "platform_toolsets.api_server",
    "approvals",
    "checkpoints",
    "tools.tool_search",
    "compression.enabled",
    "file_read_max_chars",
]

SECRET_KEY = re.compile(r"(key|token|secret|password)", re.I)
HOME_RE = re.compile(r"/home/[^/\s]+/")
REDACTED = "<redacted>"
MISSING = object()


def load_yaml():
    """Return a ruamel.yaml round-trip YAML instance.

    Hermes ships ruamel.yaml (no PyYAML) in its venv under
    installs/<id>/environments/<id>/venv; the ids change on update,
    so the venv is located by glob and the most recent one is used.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        pattern = "installs/*/environments/*/venv/lib/python*/site-packages"
        candidates = [p for p in HERMES_HOME.glob(pattern)
                      if (p / "ruamel" / "yaml" / "__init__.py").is_file()]
        if not candidates:
            raise ImportError(f"ruamel.yaml not found under {HERMES_HOME}/{pattern}")
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if len(candidates) > 1:
            print(f"note: {len(candidates)} Hermes venvs, using {candidates[0]}",
                  file=sys.stderr)
        sys.path.insert(0, str(candidates[0]))
        from ruamel.yaml import YAML
    y = YAML(typ="rt", pure=True)
    y.default_flow_style = False
    y.allow_unicode = True
    return y


def sanitize(value, key=""):
    # Converts ruamel CommentedMap/CommentedSeq into plain dict/list as well.
    # Keys are sanitized too (e.g. the model path used as a mapping key).
    if isinstance(value, dict):
        return {HOME_RE.sub("/home/<operator>/", str(k)): sanitize(v, str(k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key) for v in value]
    if isinstance(value, str):
        # *_env keys hold the NAME of an environment variable, not a secret.
        if value and SECRET_KEY.search(key) and not key.endswith("_env"):
            return REDACTED
        return HOME_RE.sub("/home/<operator>/", value)
    return value


def get(cfg, parts):
    cur = cfg
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return MISSING
        cur = cur[p]
    return cur


def leaves(node, prefix=()):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from leaves(v, prefix + (str(k),))
    else:
        yield prefix, sanitize(node)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERMES_HOME / "config.yaml"))
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dump", action="store_true")
    mode.add_argument("--fragment")
    args = ap.parse_args()

    try:
        yaml = load_yaml()
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.load(f) or {}
    except Exception as e:  # noqa: BLE001
        print(f"load_failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.dump:
        out = {"_top_level_keys": sorted(map(str, cfg.keys()))}
        for path in DUMP_PATHS:
            parts = path.split(".")
            val = get(cfg, parts)
            out[path] = "<absent>" if val is MISSING else sanitize(val, parts[-1])
        yaml.dump(out, sys.stdout)
        return 0

    try:
        with open(args.fragment, encoding="utf-8") as f:
            frag = yaml.load(f) or {}
    except Exception as e:  # noqa: BLE001
        print(f"fragment_load_failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    # Compare against the sanitized config so that placeholder keys in the
    # fragment (/home/<operator>/...) resolve.
    cfg = sanitize(cfg)
    drift = 0
    total = 0
    for parts, expected in leaves(frag):
        total += 1
        name = ".".join(parts)
        live = get(cfg, parts)
        if live is MISSING:
            print(f"DIFF {name}: absent in live config")
            drift += 1
            continue
        if expected == REDACTED:
            ok = live not in (None, "", [], {})
        else:
            ok = live == expected
        if ok:
            print(f"OK   {name}")
        else:
            print(f"DIFF {name}: fragment={expected!r} live={live!r}")
            drift += 1
    print(f"{total - drift}/{total} match")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
