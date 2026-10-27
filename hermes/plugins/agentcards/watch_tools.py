"""agentcards – the ``page_fetch`` and ``watch_manage`` tools (Delve W1).

``page_fetch`` reads one public web page and returns a compact, bounded view of it (text,
links, or listings for known sites) - so a question like "how many RTX 3060 are listed?"
takes one or two tool calls instead of curl + python in the terminal (approvals, and the
raw HTML flooding the 64K context).

``watch_manage`` turns a watcher described in chat into a deterministic no-agent cron job:
config file + generated wrapper script + Hermes cron job (``deliver: local``), validated
and test-fetched before anything is scheduled. The model fills in a structured request; it
never writes the watcher's code or the cron job itself.

The cron calls go through Hermes' own ``tools.cronjob_tools.cronjob`` in-process (the same
entry point the ``hermes cron`` CLI uses), so no ``cronjob`` toolset has to be enabled.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
from typing import Any, Callable

try:  # inside the plugin package
    from . import watchlib
except ImportError:  # tests: the directory is on sys.path
    import watchlib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

JOB_PREFIX = "watch:"
MIN_INTERVAL_MIN = 30


def _ok(**kw: Any) -> str:
    return json.dumps({"success": True, **kw}, ensure_ascii=False)


def _fail(error: str, **kw: Any) -> str:
    return json.dumps({"success": False, "error": error, **kw}, ensure_ascii=False)


# --- Hermes cron, injectable for the tests -------------------------------------------


_SKIP_DIRS = {"sessions", "cron", "logs", "cards", "watches", "cache", "node_modules",
              "site-packages", "__pycache__", ".git", "plugins", "scripts", "skills", "backup"}


def _hermes_path() -> None:
    """Inside the gateway Hermes' own modules are importable; a standalone run with the
    Hermes Python (a probe on the host) needs the Hermes source tree on sys.path: the
    newest directory under HERMES_HOME (at most 4 levels deep) with cron/jobs.py and
    tools/cronjob_tools.py."""
    home = watchlib.HERMES_HOME
    found: list[tuple[float, str]] = []
    for root, dirs, _files in os.walk(home):
        depth = root[len(home):].count(os.sep)
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith("backup")
                   and depth < 4]
        if os.path.isfile(os.path.join(root, "cron", "jobs.py")) and \
                os.path.isfile(os.path.join(root, "tools", "cronjob_tools.py")):
            found.append((os.path.getmtime(os.path.join(root, "cron", "jobs.py")), root))
            dirs[:] = []
    if found:
        root = max(found)[1]
        if root not in sys.path:
            sys.path.append(root)


class Cron:
    """Thin wrapper over Hermes' cronjob() and parse_schedule()."""

    def call(self, **kw: Any) -> dict[str, Any]:
        try:
            from tools.cronjob_tools import cronjob  # Hermes, in the gateway process
        except ModuleNotFoundError:
            _hermes_path()
            from tools.cronjob_tools import cronjob
        out = json.loads(cronjob(**kw))
        return out if isinstance(out, dict) else {"success": False, "error": "unexpected reply"}

    def parse_schedule(self, schedule: str) -> dict[str, Any]:
        try:
            from cron.jobs import parse_schedule
        except ModuleNotFoundError:
            _hermes_path()
            from cron.jobs import parse_schedule
        return parse_schedule(schedule)


def check_schedule(schedule: str, cron: Cron) -> str:
    """Recurring, at most every 30 minutes; cron expressions at most hourly."""
    if not isinstance(schedule, str) or not schedule.strip():
        raise watchlib.ConfigError("schedule is required, e.g. 'every 4h' or '0 8,13,19 * * *'")
    try:
        parsed = cron.parse_schedule(schedule.strip())
    except ValueError as exc:
        raise watchlib.ConfigError(str(exc).splitlines()[0]) from None
    kind = parsed.get("kind")
    if kind == "interval":
        if int(parsed.get("minutes") or 0) < MIN_INTERVAL_MIN:
            raise watchlib.ConfigError(f"at most every {MIN_INTERVAL_MIN} minutes")
    elif kind == "cron":
        minute = str(parsed.get("expr", "")).split()[0] if parsed.get("expr") else ""
        if not re.fullmatch(r"\d{1,2}", minute):
            raise watchlib.ConfigError("cron schedules may fire at most hourly: "
                                       "use a single minute, e.g. '15 8,13,19 * * *'")
    else:
        raise watchlib.ConfigError("a watcher repeats: use an interval ('every 4h') or a "
                                   "cron expression, not a one-shot time")
    return schedule.strip()


def _jobs(cron: Cron) -> dict[str, dict[str, Any]]:
    """name -> job for the watcher jobs."""
    out = cron.call(action="list", include_disabled=True)
    if not out.get("success"):
        raise watchlib.WatchError(f"cron list failed: {out.get('error')}", "cron_failed")
    return {j.get("name"): j for j in out.get("jobs", []) if str(j.get("name", "")).startswith(JOB_PREFIX)}


def _job_view(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {k: job.get(k) for k in ("job_id", "schedule", "state", "enabled", "next_run_at",
                                    "last_run_at", "last_status", "last_error") if k in job}


# --- page_fetch ----------------------------------------------------------------------

PAGE_FETCH_SCHEMA = {
    "name": "page_fetch",
    "description": (
        "Read ONE public web page and get a compact view of it: mode 'text' (readable text "
        "lines), 'links' (link text + URL) or 'listings' (structured ads/items for known sites: "
        "hardverapro.hu listing/search pages, cinemacity.hu film pages; other sites fall back "
        "to links). 'find' keeps only lines/links/items containing ALL of its words (case and "
        "accent insensitive). Output is capped (max_chars, default 6000). Use this instead of "
        "curl/wget/python in the terminal for any web page. Only public http(s) addresses work. "
        "For hardverapro.hu searches use https://hardverapro.hu/aprok/keres.php?stext=<words> "
        "(plus filters like maxprice=…); only the first result page may be read."),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) URL of the page"},
            "mode": {"type": "string", "enum": ["text", "links", "listings"],
                     "description": "default text"},
            "find": {"type": "string", "description": "optional words to filter by"},
            "max_chars": {"type": "integer", "description": "1000-12000, default 6000"},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


def handle_page_fetch(args: dict[str, Any], fetcher_factory: Callable[[], Any] | None = None,
                      **_kw: Any) -> str:
    args = dict(args or {})
    url = str(args.get("url") or "").strip()
    mode = args.get("mode") or "text"
    if mode not in ("text", "links", "listings"):
        return _fail("mode must be text, links or listings")
    try:
        out = watchlib.read_page(url, mode=mode, find=args.get("find"),
                                 max_chars=args.get("max_chars") or 6000,
                                 fetcher=fetcher_factory() if fetcher_factory else None)
    except watchlib.WatchError as exc:
        return _fail(exc.code, message=str(exc)[:300])
    out.pop("ok", None)
    return _ok(**out)


# --- watch_manage --------------------------------------------------------------------

WATCH_MANAGE_SCHEMA = {
    "name": "watch_manage",
    "description": (
        "Create and manage WATCHERS: background checks of a web page on a schedule that notify "
        "the user's phone (with the link) about NEW matching items - e.g. new hardverapro.hu ads "
        "for a product, or when cinema tickets for a film go on sale. Runs without the LLM; new "
        "candidates are judged against 'criteria' by the backend. Actions: "
        "'preview' (test a definition without saving: what it would match now), 'create', "
        "'list', 'update' (change filters/criteria/schedule/name of 'slug'), 'pause', 'resume', "
        "'delete', 'run' (check now). Workflow for a new request: preview -> show the user the "
        "count and a few matches -> create. Sources: 'hardverapro' (url = a hardverapro.hu "
        "/aprok/ category page or https://hardverapro.hu/aprok/keres.php?stext=<words>), "
        "'cinemacity' (url = the film page https://www.cinemacity.hu/films/<name>/<id>; a hit "
        "means tickets can be booked; use stop_after_hit), 'page' (any page; items = links "
        "whose text has the words). 'include' = all of these words must be in the title, "
        "'any' = at least one, 'exclude' = none (case and accent insensitive). Put conditions "
        "that need reading the ad (condition, 'not mined', 'with warranty') into 'criteria' "
        "and set fetch_details=true. The first run records the items already listed and only "
        "later ones are announced. Never create cron jobs or watcher scripts by hand."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["preview", "create", "list", "update", "pause", "resume",
                                "delete", "run"]},
            "slug": {"type": "string", "description": "watcher id (from create/list); for "
                     "update/pause/resume/delete/run"},
            "name": {"type": "string", "description": "short human name, e.g. 'RTX 4070 Super ≤180k'"},
            "source": {"type": "string", "enum": list(watchlib.SOURCES)},
            "url": {"type": "string"},
            "include": {"type": "array", "items": {"type": "string"}},
            "any": {"type": "array", "items": {"type": "string"}},
            "exclude": {"type": "array", "items": {"type": "string"}},
            "min_price": {"type": "integer", "description": "HUF"},
            "max_price": {"type": "integer", "description": "HUF"},
            "criteria": {"type": "string", "description": "free-text condition in the user's "
                         "words, judged per new item (max 500 chars)"},
            "fetch_details": {"type": "boolean", "description": "read each new ad's own page "
                              "for the criteria (hardverapro)"},
            "stop_after_hit": {"type": "boolean", "description": "pause after the first hit"},
            "schedule": {"type": "string", "description": "e.g. 'every 4h', '15 8,13,19 * * *' "
                         "(at most every 30 min)"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

DEF_KEYS = ("name", "source", "url", "include", "any", "exclude", "min_price", "max_price",
            "criteria", "fetch_details", "stop_after_hit", "schedule")
UPDATABLE = ("name", "include", "any", "exclude", "min_price", "max_price", "criteria",
             "fetch_details", "stop_after_hit", "schedule")


class WatchManager:
    def __init__(self, cron: Cron | None = None, base: str | None = None,
                 scripts_dir: str | None = None, fetcher_factory: Callable[[str], Any] | None = None,
                 publish: Callable[[dict], Any] | None = None) -> None:
        self.cron = cron or Cron()
        self.base = base
        self.scripts_dir = scripts_dir
        self.fetcher_factory = fetcher_factory
        self.publish = publish

    def _fetcher(self, slug: str | None):
        if self.fetcher_factory:
            return self.fetcher_factory(slug or "")
        return watchlib.Fetcher(watchlib.cookie_path(slug, self.base) if slug else None)

    def _definition(self, args: dict[str, Any], slug: str) -> dict[str, Any]:
        cfg = {k: args.get(k) for k in DEF_KEYS if args.get(k) is not None}
        if "source" not in cfg and isinstance(cfg.get("url"), str):
            cfg["source"] = watchlib.source_for_url(cfg["url"])
        cfg["slug"] = slug
        cfg["schedule"] = check_schedule(cfg.get("schedule", ""), self.cron) \
            if cfg.get("schedule") else "every 4h"
        return watchlib.validate_config(cfg)

    def _slug(self, args: dict[str, Any]) -> str:
        slug = str(args.get("slug") or "").strip()
        if not watchlib.SLUG_RE.fullmatch(slug):
            raise watchlib.ConfigError("give the watcher's slug (see action=list)")
        return slug

    # actions

    def preview(self, args):
        cfg = self._definition(args, "preview")
        out = watchlib.preview(cfg, self._fetcher(None))
        return _ok(action="preview", definition=_public(cfg), now=out,
                   note="Nothing saved. Show the user the count and a few matches, then create.")

    def create(self, args):
        existing = watchlib.list_configs(self.base)
        if len(existing) >= watchlib.MAX_WATCHES:
            return _fail(f"at most {watchlib.MAX_WATCHES} watchers; delete one first")
        if not args.get("name"):
            return _fail("name is required")
        slug = watchlib.slugify(str(args["name"]))
        taken = {c["slug"] for c in existing}
        base_slug, n = slug, 2
        while slug in taken:
            slug = f"{base_slug[:36]}-{n}"
            n += 1
        cfg = self._definition(args, slug)
        cfg["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        fetcher = self._fetcher(slug)
        now = watchlib.preview(cfg, fetcher)          # fails before anything is written
        watchlib.save_config(cfg, self.base)
        wrapper = watchlib.write_wrapper(slug, self.scripts_dir)
        res = self.cron.call(action="create", name=JOB_PREFIX + slug, schedule=cfg["schedule"],
                             script=os.path.basename(wrapper), no_agent=True, deliver="local",
                             failure_deliver="local")
        if not res.get("success"):
            self._remove_files(slug)
            return _fail(f"cron create failed: {res.get('error')}")
        cfg["job_id"] = str(res.get("job_id") or "")[:64]
        watchlib.save_config(cfg, self.base)
        try:      # first run now: the baseline (and the watch_def card for the backend)
            first = watchlib.run_watch(slug, fetcher=fetcher, publish=self.publish, base=self.base)
        except watchlib.WatchError as exc:
            first = {"status": "error", "error": str(exc)[:200]}
        return _ok(action="create", slug=slug, job_id=cfg["job_id"], schedule=cfg["schedule"],
                   next_run_at=res.get("next_run_at"), definition=_public(cfg),
                   matching_now=now["matched"], sample=now["sample"][:5], first_run=first,
                   note=("Items listed now are recorded as seen; only new ones will notify."
                         if not cfg.get("notify_existing") else
                         "Current matches are announced too (stop_after_hit/notify_existing)."))

    def list(self, args):
        jobs = _jobs(self.cron)
        rows = []
        for cfg in watchlib.list_configs(self.base):
            st = watchlib.load_state(cfg["slug"], self.base) if "error" not in cfg else {}
            rows.append({**_public(cfg), "job": _job_view(jobs.get(JOB_PREFIX + cfg["slug"])),
                         "last_ok_at": st.get("last_ok_at"), "last_error": st.get("last_error"),
                         "seen": len(st.get("seen", {}))})
        orphans = [n for n in jobs if n[len(JOB_PREFIX):] not in {r["slug"] for r in rows}]
        return _ok(action="list", watchers=rows, orphan_jobs=orphans)

    def update(self, args):
        slug = self._slug(args)
        cfg = watchlib.load_config(slug, self.base)
        changes = {k: args[k] for k in UPDATABLE if k in args and args[k] is not None}
        if not changes:
            return _fail(f"nothing to update; updatable: {', '.join(UPDATABLE)}")
        if "schedule" in changes:
            changes["schedule"] = check_schedule(changes["schedule"], self.cron)
        new = watchlib.validate_config({**cfg, **changes})
        if new.get("schedule") != cfg.get("schedule") and cfg.get("job_id"):
            res = self.cron.call(action="update", job_id=cfg["job_id"], schedule=new["schedule"])
            if not res.get("success"):
                return _fail(f"cron update failed: {res.get('error')}")
        watchlib.save_config(new, self.base)
        return _ok(action="update", slug=slug, definition=_public(new),
                   note="Applies from the next run; already seen items stay seen.")

    def _job_action(self, args, action):
        slug = self._slug(args)
        cfg = watchlib.load_config(slug, self.base)
        if not cfg.get("job_id"):
            return _fail("this watcher has no cron job")
        res = self.cron.call(action=action, job_id=cfg["job_id"])
        if not res.get("success"):
            return _fail(f"cron {action} failed: {res.get('error')}")
        return _ok(action=action, slug=slug)

    def pause(self, args):
        return self._job_action(args, "pause")

    def resume(self, args):
        return self._job_action(args, "resume")

    def delete(self, args):
        slug = self._slug(args)
        cfg = watchlib.load_config(slug, self.base)
        if cfg.get("job_id"):
            res = self.cron.call(action="remove", job_id=cfg["job_id"])
            if not res.get("success") and "not found" not in str(res.get("error", "")).lower():
                return _fail(f"cron remove failed: {res.get('error')}")
        self._remove_files(slug)
        return _ok(action="delete", slug=slug)

    def run(self, args):
        slug = self._slug(args)
        out = watchlib.run_watch(slug, fetcher=self._fetcher(slug), publish=self.publish,
                                 base=self.base)
        return _ok(action="run", result=out)

    def _remove_files(self, slug: str) -> None:
        for p in (watchlib.config_path(slug, self.base), watchlib.state_path(slug, self.base),
                  watchlib.cookie_path(slug, self.base),
                  os.path.join(self.scripts_dir or watchlib.SCRIPTS_DIR,
                               watchlib.wrapper_name(slug))):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def handle(self, args: dict[str, Any], **_kw: Any) -> str:
        args = dict(args or {})
        action = args.pop("action", None)
        fn = {"preview": self.preview, "create": self.create, "list": self.list,
              "update": self.update, "pause": self.pause, "resume": self.resume,
              "delete": self.delete, "run": self.run}.get(action or "")
        if fn is None:
            return _fail("unknown action")
        try:
            return fn(args)
        except watchlib.WatchError as exc:
            return _fail(exc.code, message=str(exc)[:400])
        except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash the turn
            logger.exception("watch_manage %s failed", action)
            return _fail("internal_error", message=f"{type(exc).__name__}: {exc}"[:300])


def _public(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg[k] for k in ("slug", *DEF_KEYS, "notify_existing", "job_id")
            if k in cfg and cfg[k] not in (None, [], "")}
