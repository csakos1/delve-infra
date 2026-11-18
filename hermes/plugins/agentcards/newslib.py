"""newslib – the Delve News collector (stdlib only, Delve N1).

A no-agent cron job (``news``, ``~/.hermes/scripts/news_collect.py``) runs ``main()``
every hour. It reads the feeds the backend lists in ``$DELVE_NEWS_DIR/sources.json``
(``/var/lib/delve-news``, written by the backend, readable by the agent), fetches the ones
that are due, parses RSS 2.0 / RSS 1.0 / Atom, keeps the items inside the source's time
window that were not seen before, and writes one batch file for the backend:
``~/.hermes/cards/news/batch-<UTC stamp>.json`` (the card directory is what the backend
sees read-only at ``/run/delve/cards``). No model is involved; the backend scores, groups
and summarises (D-58).

Feed-reader mode (the user's choice, 2026-11-17): a feed URL the site hands out for
subscribing is read without a robots.txt check - one request an hour, an honest user agent.
Article pages are a different matter (robots.txt with the ``delve-news`` token, N1b).
Every URL - the feed and each redirect - goes through the watchers' public-address guard
(D-41), and the requests to one host are spaced across processes (the watchers' stamp
files). A 429 / 503 is a rate limit: ``Retry-After`` is honoured, the source waits.
ETag / Last-Modified make an unchanged feed a cheap 304.

Article texts (N1c, a second no-agent job ``news-text``, ``news_texts.py`` ->
``texts_main()``): the backend lists the stories it wants a text of in
``$DELVE_NEWS_DIR/wanted.json``; the job hands them to ``newstext.py`` in the renderer's venv
(Playwright for the Google News links, trafilatura for the text) under the renderer's lock,
and writes the answer as ``cards/news/text-<UTC stamp>.json``. An item is asked once per
``try`` (the backend raises it when the user asks again).

Exit codes: 0 ok (single sources may have failed - the batch says which), 4 the sources
file is unreadable or invalid, 3 the batch or the state could not be written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import watchlib as wl  # noqa: E402  (agentcards, same directory: the URL guard)

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
NEWS_DIR = os.environ.get("DELVE_NEWS_DIR") or "/var/lib/delve-news"
STATE_DIR = os.path.join(HERMES_HOME, "news")
OUT_DIR = os.path.join(HERMES_HOME, "cards", "news")
HOST_DIR = os.path.join(HERMES_HOME, "watches", ".hosts")   # shared with the watchers
LOG_PATH = os.path.join(HERMES_HOME, "logs", "news.log")
LOG_MAX_BYTES = 512 * 1024

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) DelveNews/0.1 (personal news reader)"
ACCEPT_FEED = ("application/rss+xml, application/atom+xml, application/rdf+xml;q=0.9, "
               "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5")
FETCH_TIMEOUT_S = 20
FETCH_TOTAL_S = 45             # a whole body, however slowly it trickles in
MAX_BYTES = 3 * 1024 * 1024
HOST_INTERVAL_S = 2.0
# hosts that answered parallel requests with 429 (N0b: reddit.com) get more room
SLOW_HOSTS = {"www.reddit.com": 10.0, "reddit.com": 10.0, "news.google.com": 3.0}
RUN_BUDGET_S = 240.0           # no new source is started after this
DUE_SLACK_S = 120.0            # a source fetched 58 minutes ago is due for an hourly run
RETRY_AFTER_MAX_S = 6 * 3600
RATE_LIMIT_DEFAULT_S = 3600    # a 429 without Retry-After
ITEMS_PER_SOURCE = 60
MAX_ITEMS_PER_BATCH = 2000
TITLE_CHARS = 300
SUMMARY_CHARS = 400
PUBLISHER_CHARS = 80
URL_CHARS = 2000
SEEN_KEEP_DAYS = 14
BATCH_KEEP_DAYS = 3
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,47}$")
KINDS = ("feed", "release")
SLEEP = time.sleep                  # replaced in tests
# N1c: article texts
ACCEPT_HTML = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
TEXT_MAX_ITEMS = 40            # per run
TEXT_BUDGET_S = 180.0          # no new item is started after this
TEXT_TIMEOUT_S = TEXT_BUDGET_S + 120.0
TEXT_LOCK_WAIT_S = 120.0       # the renderer's lock (a watcher may be rendering)
TEXT_KEEP_DAYS = 3             # the state remembers an asked id:try this long
RENDER_HOME = os.path.join(HERMES_HOME, "delve-render")

EXIT_OK, EXIT_STORE, EXIT_CONFIG = 0, 3, 4


class NewsError(Exception):
    code = "failed"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ConfigError(NewsError):
    code = "bad_config"


class FeedError(NewsError):
    code = "parse_failed"


def log(msg: str) -> None:
    """stderr + own log file (Hermes does not keep a no-agent script's stderr)."""
    print(f"news: {msg}", file=sys.stderr)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), mode=0o700, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- the sources file -------------------------------------------------------------------


def validate_sources(data: Any) -> list[dict[str, Any]]:
    """The backend's sources.json -> the list of sources; ConfigError if it is not one."""
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("sources"), list):
        raise ConfigError("sources.json: expected {\"v\": 1, \"sources\": [...]}")
    out, ids = [], set()
    for i, s in enumerate(data["sources"]):
        if not isinstance(s, dict):
            raise ConfigError(f"sources[{i}]: not an object")
        sid, url, kind = s.get("id"), s.get("url"), s.get("kind", "feed")
        if not isinstance(sid, str) or not SOURCE_ID_RE.match(sid) or sid in ids:
            raise ConfigError(f"sources[{i}]: bad or repeated id")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")) \
                or len(url) > URL_CHARS:
            raise ConfigError(f"sources[{i}] {sid}: bad url")
        if kind not in KINDS:
            raise ConfigError(f"sources[{i}] {sid}: kind must be one of {KINDS}")
        window_h, every_min = s.get("window_h", 48), s.get("every_min", 60)
        if not isinstance(window_h, int) or isinstance(window_h, bool) or not 1 <= window_h <= 720:
            raise ConfigError(f"sources[{i}] {sid}: window_h 1..720")
        if not isinstance(every_min, int) or isinstance(every_min, bool) \
                or not 30 <= every_min <= 1440:
            raise ConfigError(f"sources[{i}] {sid}: every_min 30..1440")
        ids.add(sid)
        out.append({"id": sid, "url": url, "kind": kind, "window_h": window_h,
                    "every_min": every_min})
    return out


def load_sources(news_dir: str | None = None) -> list[dict[str, Any]] | None:
    """None when the backend has not written the file yet (nothing to do, not an error)."""
    path = os.path.join(news_dir or NEWS_DIR, "sources.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read(2 * 1024 * 1024)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"sources.json: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"sources.json: not JSON ({exc})") from None
    return validate_sources(data)


# --- state ------------------------------------------------------------------------------


def _state_path(base: str | None = None) -> str:
    return os.path.join(base or STATE_DIR, "state.json")


def load_state(base: str | None = None) -> dict[str, Any]:
    try:
        with open(_state_path(base), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"sources": {}, "seen": {}}
    if not isinstance(st, dict):
        return {"sources": {}, "seen": {}}
    st.setdefault("sources", {})
    st.setdefault("seen", {})
    return st


def _write_json(path: str, data: Any, mode: int) -> None:
    tmp = f"{path}.{os.getpid()}.part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(st: dict[str, Any], base: str | None = None, now: dt.datetime | None = None) -> None:
    now = now or _now()
    cutoff = (now - dt.timedelta(days=SEEN_KEEP_DAYS)).timestamp()
    st["seen"] = {k: v for k, v in st.get("seen", {}).items()
                  if isinstance(v, (int, float)) and v >= cutoff}
    d = base or STATE_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    _write_json(_state_path(base), st, 0o600)


# --- fetching ---------------------------------------------------------------------------


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes | None,
                 final_url: str) -> None:
        self.status, self.headers, self.body, self.final_url = status, headers, body, final_url


def space(host: str, host_dir: str | None = None) -> None:
    """At most one request per host every interval, across processes (the watchers too)."""
    interval = SLOW_HOSTS.get(host, HOST_INTERVAL_S)
    d = host_dir if host_dir is not None else HOST_DIR
    if interval <= 0 or not d:
        return
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, re.sub(r"[^a-z0-9.-]", "_", host))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.pread(fd, 32, 0)
        try:
            last = float(raw.decode("ascii").strip() or 0)
        except ValueError:
            last = 0.0
        wait = last + interval - time.time()
        if wait > 0:
            SLEEP(min(wait, interval))
        os.pwrite(fd, f"{time.time():.3f}".encode("ascii").ljust(32), 0)
    finally:
        os.close(fd)


def http_get(url: str, *, etag: str | None = None, modified: str | None = None,
             resolver: wl.Resolver = wl._resolve, host_dir: str | None = None,
             accept: str = ACCEPT_FEED) -> Response:
    """One GET through the public-address guard (every redirect too). 304 and 4xx/5xx come
    back as a Response, not an exception; a blocked or unreachable URL raises."""
    wl.check_url(url, resolver)
    space((urllib.parse.urlsplit(url).hostname or "").lower(), host_dir)
    headers = {"User-Agent": USER_AGENT, "Accept": accept,
               "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.7"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    redirects = wl._Redirects(resolver)
    redirects.max_redirections = wl.MAX_REDIRECTS
    opener = urllib.request.build_opener(redirects)
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_S) as r:
            end = time.monotonic() + FETCH_TOTAL_S
            chunks, size = [], 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise NewsError(f"larger than {MAX_BYTES} bytes", "too_large")
                if time.monotonic() > end:
                    raise NewsError(f"slower than {FETCH_TOTAL_S} s", "fetch_failed")
            return Response(r.status, _pick(r.headers), b"".join(chunks), r.geturl())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, _pick(exc.headers), None, url)
    except wl.BlockedURL:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, wl.BlockedURL):
            raise reason from None
        raise NewsError(f"{type(exc).__name__}: {reason}", "fetch_failed") from None


def _pick(h: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if h is None:
        return out
    for name in ("ETag", "Last-Modified", "Retry-After", "Content-Type"):
        v = h.get(name)
        if v:
            out[name.lower()] = str(v)[:200]
    return out


def retry_after_s(value: str | None, now: dt.datetime) -> float | None:
    """Retry-After as seconds (a number or an HTTP date), capped; None if absent or bad."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(min(int(value), RETRY_AFTER_MAX_S))
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, min((when - now).total_seconds(), RETRY_AFTER_MAX_S))


# --- parsing ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_DTD_RE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.I)
_ROOT_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(?:rss|feed|RDF)\b")
TRACKING = re.compile(r"^(utm_[a-z_]+|fbclid|gclid|dclid|mc_[a-z]+|ocid|ref|ref_src|cmpid|"
                      r"spm|_ga|igshid|smid|sr_share)$", re.I)


def clean_text(s: Any, limit: int) -> str:
    if not isinstance(s, str):
        return ""
    s = html.unescape(_TAG_RE.sub(" ", html.unescape(s)))
    s = "".join(ch if ch.isprintable() or ch == " " else " " for ch in s)
    s = _WS_RE.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def normalize_url(url: str) -> str:
    """Tracking parameters and the fragment off, the host lower-case, no trailing '/'."""
    p = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not TRACKING.match(k)]
    path = p.path.rstrip("/") if p.path not in ("", "/") else ""
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                                    urllib.parse.urlencode(query), ""))


def item_key(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:32]


def parse_date(s: Any) -> dt.datetime | None:
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    try:
        t = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        t = None
    if t is None:
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, *names: str) -> str | None:
    for name in names:
        c = _child(el, name)
        if c is not None:
            t = "".join(c.itertext()).strip()
            if t:
                return t
    return None


def _atom_link(entry: ET.Element) -> str | None:
    best = None
    for c in entry:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        rel = c.get("rel", "alternate")
        if href and rel == "alternate":
            return href
        if href and best is None:
            best = href
    return best


def parse_feed(body: bytes, base_url: str) -> tuple[str | None, list[dict[str, Any]]]:
    """RSS 2.0, RSS 1.0 (RDF) or Atom -> (feed title, raw entries). FeedError if none."""
    root_at = _ROOT_RE.search(body)
    if _DTD_RE.search(body[: root_at.start() if root_at else len(body)]):
        raise FeedError("feed with a DTD (entities are not accepted)")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not XML: {exc}") from None
    kind = _local(root.tag)
    entries: list[ET.Element] = []
    title = None
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("rss without a channel")
        title = _text(channel, "title")
        entries = [c for c in channel if _local(c.tag) == "item"]
    elif kind == "RDF":
        channel = _child(root, "channel")
        title = _text(channel, "title") if channel is not None else None
        entries = [c for c in root if _local(c.tag) == "item"]
    elif kind == "feed":
        title = _text(root, "title")
        entries = [c for c in root if _local(c.tag) == "entry"]
    else:
        raise FeedError(f"not a feed (root element {kind!r})")
    out = []
    for e in entries:
        if kind == "feed":
            link = _atom_link(e)
            published = _text(e, "published", "updated")
            summary = _text(e, "summary", "content")
            publisher = None
        else:
            link = _text(e, "link") or (e.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"))
            if not link:
                guid = _child(e, "guid")
                if guid is not None and (guid.get("isPermaLink", "true") != "false"):
                    link = (guid.text or "").strip() or None
            published = _text(e, "pubDate", "date", "published", "updated")
            summary = _text(e, "description", "encoded", "summary")
            src = _child(e, "source")
            publisher = "".join(src.itertext()).strip() if src is not None else None
        out.append({"title": _text(e, "title"), "link": link, "published": published,
                    "summary": summary, "publisher": publisher})
    return title, [dict(r, link=urllib.parse.urljoin(base_url, r["link"]) if r["link"] else None)
                   for r in out]


def is_google_news(url: str) -> bool:
    return (urllib.parse.urlsplit(url).hostname or "").lower() == "news.google.com"


def _publisher_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def to_items(source: dict[str, Any], feed_title: str | None, raw: list[dict[str, Any]],
             now: dt.datetime) -> list[dict[str, Any]]:
    """Raw entries -> batch items inside the source's window (dated or not), newest first,
    at most ITEMS_PER_SOURCE."""
    window = dt.timedelta(hours=source["window_h"])
    out = []
    for r in raw:
        link = r.get("link")
        title = clean_text(r.get("title"), TITLE_CHARS)
        if not link or not title or not link.startswith(("https://", "http://")) \
                or len(link) > URL_CHARS:
            continue
        published = parse_date(r.get("published"))
        if published is not None and (published < now - window
                                      or published > now + dt.timedelta(hours=1)):
            continue
        gn = is_google_news(link)
        publisher = clean_text(r.get("publisher"), PUBLISHER_CHARS) or None
        if gn and publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].rstrip()
        if not publisher:
            publisher = clean_text(feed_title, PUBLISHER_CHARS) if not gn and feed_title \
                else _publisher_of(link)
        item = {
            "key": item_key(link),
            "source": source["id"],
            "url": link if gn else normalize_url(link),
            "title": title,
            "publisher": publisher or None,
            "published": _iso(published) if published else None,
            "summary": clean_text(r.get("summary"), SUMMARY_CHARS) or None,
            "gn": gn,
            "kind": source["kind"],
        }
        out.append(item)
    out.sort(key=lambda i: i["published"] or "", reverse=True)
    return out[:ITEMS_PER_SOURCE]


# --- one run ----------------------------------------------------------------------------


def due(source: dict[str, Any], sst: dict[str, Any], now: dt.datetime, force: bool) -> str | None:
    """None when the source should be fetched now, else why not."""
    t = now.timestamp()
    if sst.get("retry_until", 0) > t:
        return "rate_limited"
    if force:
        return None
    if t - sst.get("fetched_at", 0) < source["every_min"] * 60 - DUE_SLACK_S:
        return "not_due"
    return None


def _order(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin by host, so two requests to one host are as far apart as possible."""
    by_host: dict[str, list[dict[str, Any]]] = {}
    for s in sources:
        by_host.setdefault((urllib.parse.urlsplit(s["url"]).hostname or "").lower(), []).append(s)
    queues = list(by_host.values())
    out = []
    while queues:
        for q in list(queues):
            out.append(q.pop(0))
            if not q:
                queues.remove(q)
    return out


Getter = Callable[..., Response]


def fetch_source(source: dict[str, Any], sst: dict[str, Any], now: dt.datetime,
                 get: Getter) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch and parse one source; updates its state in place. -> (report, items)."""
    started = time.monotonic()
    rep: dict[str, Any] = {"id": source["id"], "status": "ok", "http": None, "items": 0,
                           "new": 0, "error_code": None, "error": None, "ms": 0}
    items: list[dict[str, Any]] = []
    same_url = sst.get("url") == source["url"]
    try:
        resp = get(source["url"], etag=sst.get("etag") if same_url else None,
                   modified=sst.get("modified") if same_url else None)
        rep["http"] = resp.status
        sst["fetched_at"] = now.timestamp()
        sst["url"] = source["url"]
        if resp.status == 304:
            rep["status"] = "not_modified"
        elif resp.status in (429, 503):
            wait = retry_after_s(resp.headers.get("retry-after"), now)
            wait = wait if wait is not None else RATE_LIMIT_DEFAULT_S
            sst["retry_until"] = now.timestamp() + wait
            rep.update(status="error", error_code="rate_limited",
                       error=f"HTTP {resp.status}, waits {int(wait)} s")
        elif resp.status != 200 or resp.body is None:
            rep.update(status="error", error_code=f"http_{resp.status}",
                       error=f"HTTP {resp.status}")
        else:
            title, raw = parse_feed(resp.body, resp.final_url)
            items = to_items(source, title, raw, now)
            rep["items"] = len(items)
            sst["etag"] = resp.headers.get("etag")
            sst["modified"] = resp.headers.get("last-modified")
            sst.pop("retry_until", None)
    except wl.BlockedURL as exc:
        rep.update(status="error", error_code="blocked_url", error=str(exc)[:200])
        sst["fetched_at"] = now.timestamp()
    except wl.WatchError as exc:   # dns_failed from the guard
        rep.update(status="error", error_code=exc.code, error=str(exc)[:200])
        sst["fetched_at"] = now.timestamp()
    except NewsError as exc:
        rep.update(status="error", error_code=exc.code, error=str(exc)[:200])
        sst["fetched_at"] = now.timestamp()
    sst["status"] = rep["status"]
    sst["errors_in_row"] = (sst.get("errors_in_row", 0) + 1) if rep["status"] == "error" else 0
    rep["ms"] = int((time.monotonic() - started) * 1000)
    return rep, items


def collect(sources: list[dict[str, Any]], st: dict[str, Any], *, get: Getter = http_get,
            now: dt.datetime | None = None, force: bool = False,
            budget_s: float = RUN_BUDGET_S, clock: Callable[[], float] = time.monotonic
            ) -> dict[str, Any]:
    """One collection: every due source, then the new items (not in ``seen``) of all of
    them. Updates ``st``; returns the batch (without writing it)."""
    now = now or _now()
    started = clock()
    seen: dict[str, float] = st.setdefault("seen", {})
    sst_all: dict[str, Any] = st.setdefault("sources", {})
    for gone in set(sst_all) - {s["id"] for s in sources}:
        del sst_all[gone]
    reports, new_items = [], []
    for s in _order(sources):
        sst = sst_all.setdefault(s["id"], {})
        why = due(s, sst, now, force)
        if why is None and clock() - started > budget_s:
            why = "budget"
        if why is not None:
            if why != "not_due":
                reports.append({"id": s["id"], "status": "skipped", "http": None, "items": 0,
                                "new": 0, "error_code": why, "error": None, "ms": 0})
            continue
        rep, items = fetch_source(s, sst, now, get)
        fresh = []
        for it in items:
            if it["key"] in seen:
                continue
            seen[it["key"]] = now.timestamp()
            fresh.append(it)
        rep["new"] = len(fresh)
        reports.append(rep)
        new_items.extend(fresh)
    return {"v": 1, "at": _iso(now), "run_s": round(clock() - started, 1),
            "sources": reports, "items": new_items[:MAX_ITEMS_PER_BATCH]}


def write_batch(batch: dict[str, Any], out_dir: str | None = None,
                now: dt.datetime | None = None) -> str:
    """The batch file for the backend (0644 in a 0755 directory: the backend reads it
    through the card directory's read-only bind mount). Old batches are removed."""
    d = out_dir or OUT_DIR
    os.makedirs(d, mode=0o755, exist_ok=True)
    os.chmod(d, 0o755)
    now = now or _now()
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(d, f"batch-{stamp}-{os.getpid()}.json")
    _write_json(path, batch, 0o644)
    _clean_old(d, "batch-")
    return path


def _clean_old(d: str, prefix: str) -> None:
    cutoff = time.time() - BATCH_KEEP_DAYS * 86400
    for name in os.listdir(d):
        if name.startswith(prefix) and name.endswith(".json"):
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass


# --- article texts (N1c) ----------------------------------------------------------------


def validate_wanted(data: Any) -> list[dict[str, Any]]:
    """The backend's wanted.json -> [{id, url, gn, try}]; ConfigError if it is not one."""
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("items"), list):
        raise ConfigError("wanted.json: expected {\"v\": 1, \"items\": [...]}")
    out, ids = [], set()
    for i, it in enumerate(data["items"][:200]):
        if not isinstance(it, dict):
            raise ConfigError(f"items[{i}]: not an object")
        iid, url, tries = it.get("id"), it.get("url"), it.get("try", 1)
        if not isinstance(iid, int) or isinstance(iid, bool) or iid < 1 or iid in ids:
            raise ConfigError(f"items[{i}]: bad or repeated id")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")) \
                or len(url) > URL_CHARS:
            raise ConfigError(f"items[{i}] {iid}: bad url")
        if not isinstance(tries, int) or isinstance(tries, bool) or not 1 <= tries <= 1000:
            raise ConfigError(f"items[{i}] {iid}: bad try")
        ids.add(iid)
        out.append({"id": iid, "url": url, "gn": bool(it.get("gn")), "try": tries})
    return out


def load_wanted(news_dir: str | None = None) -> list[dict[str, Any]]:
    path = os.path.join(news_dir or NEWS_DIR, "wanted.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read(1024 * 1024)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(f"wanted.json: {exc}") from None
    try:
        return validate_wanted(json.loads(raw))
    except ValueError as exc:
        raise ConfigError(f"wanted.json: not JSON ({exc})") from None


def to_fetch(wanted: list[dict[str, Any]], st: dict[str, Any], now: dt.datetime,
             limit: int = TEXT_MAX_ITEMS) -> list[dict[str, Any]]:
    """The wanted items not asked yet (id:try), at most ``limit``; old marks are dropped."""
    done: dict[str, float] = st.setdefault("texts", {})
    cutoff = (now - dt.timedelta(days=TEXT_KEEP_DAYS)).timestamp()
    for k in [k for k, v in done.items() if not isinstance(v, (int, float)) or v < cutoff]:
        del done[k]
    return [w for w in wanted if f"{w['id']}:{w['try']}" not in done][:limit]


def run_worker(items: list[dict[str, Any]], *, python: str | None = None,
               script: str | None = None, lock_path: str | None = None,
               budget_s: float = TEXT_BUDGET_S, timeout_s: float = TEXT_TIMEOUT_S,
               lock_wait_s: float = TEXT_LOCK_WAIT_S) -> dict[str, Any]:
    """newstext.py in the renderer's venv, one run, under the renderer's lock, in its own
    process group (a cut run takes its browser along). NewsError (render_unavailable,
    render_busy, text_timeout, text_failed) when there is no answer at all."""
    python = python or os.environ.get("DELVE_NEWS_TEXT_PYTHON") \
        or os.path.join(RENDER_HOME, "venv", "bin", "python")
    script = script or os.environ.get("DELVE_NEWS_TEXT_SCRIPT") or os.path.join(HERE, "newstext.py")
    if not (os.access(python, os.X_OK) and os.path.isfile(script)):
        raise NewsError("the renderer's venv is not installed", "render_unavailable")
    lock_path = lock_path or os.path.join(wl.WATCH_DIR, ".render.lock")
    env = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "HERMES_HOME": HERMES_HOME, "DELVE_NEWS_DIR": NEWS_DIR}
    for k in ("DELVE_CHROMIUM", "DELVE_RENDER_TEST_HOSTS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    cut = False
    try:
        deadline = time.monotonic() + lock_wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise NewsError("the renderer is busy", "render_busy") from None
                time.sleep(0.5)
        payload = json.dumps({"v": 1, "budget_s": budget_s, "items": items}).encode("utf-8")
        proc = subprocess.Popen([python, "-I", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                start_new_session=True)
        try:
            out, err = proc.communicate(payload, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            cut = True
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            out, err = proc.communicate()
    finally:
        os.close(fd)
    return _worker_answer(out, err, cut)


def _worker_answer(out: bytes, err: bytes, cut: bool) -> dict[str, Any]:
    """The worker's lines -> {items, run_s, browser_ms, timeout}; what was done before a cut
    is kept. NewsError text_timeout / text_failed when there is nothing."""
    items: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("item"), dict):
            items.append(obj["item"])
        elif isinstance(obj, dict) and isinstance(obj.get("done"), dict):
            done = obj["done"]
    timeout = cut or bool(done.get("timeout"))
    if not items and not done:
        tail = " ".join(err.decode("utf-8", "replace").split())[-300:]
        if timeout:
            raise NewsError(f"the text worker took too long: {tail}", "text_timeout")
        raise NewsError(f"the text worker gave no answer: {tail}", "text_failed")
    return {"items": items, "run_s": done.get("run_s"), "browser_ms": done.get("browser_ms"),
            "timeout": timeout}


def write_texts(result: dict[str, Any], out_dir: str | None = None,
                now: dt.datetime | None = None) -> str:
    """The texts file for the backend, next to the batches (0644); old ones are removed."""
    d = out_dir or OUT_DIR
    os.makedirs(d, mode=0o755, exist_ok=True)
    os.chmod(d, 0o755)
    now = now or _now()
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(d, f"text-{stamp}-{os.getpid()}.json")
    _write_json(path, {"v": 1, "at": _iso(now), **result}, 0o644)
    _clean_old(d, "text-")
    return path


def texts_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fetch the article texts the backend wants, once")
    ap.add_argument("--dry-run", action="store_true", help="list what would be fetched")
    args = ap.parse_args(argv)
    try:
        wanted = load_wanted()
    except ConfigError as exc:
        log(f"texts: {exc.code}: {exc}")
        return EXIT_CONFIG
    if not wanted:
        return EXIT_OK
    fd = _lock()
    if fd is None:
        log("texts: a collection is running; next time")
        return EXIT_OK
    try:
        now = _now()
        st = load_state()
        todo = to_fetch(wanted, st, now)
        if args.dry_run:
            print(json.dumps({"wanted": len(wanted), "to_fetch": todo}, indent=1))
            return EXIT_OK
        if not todo:
            return EXIT_OK
        try:
            result = run_worker(todo)
        except NewsError as exc:
            log(f"texts: {exc.code}: {exc}")
            return EXIT_OK
        asked = {(w["id"], w["try"]) for w in todo}
        items = [i for i in result["items"] if isinstance(i, dict)
                 and (i.get("id"), i.get("try")) in asked]
        result["items"] = items
        try:
            write_texts(result, now=now)
            for i in items:
                st["texts"][f"{i['id']}:{i['try']}"] = now.timestamp()
            save_state(st)
        except OSError as exc:
            log(f"texts: store_failed: {type(exc).__name__}: {exc}")
            return EXIT_STORE
        by: dict[str, int] = {}
        for i in items:
            by[str(i.get("status"))] = by.get(str(i.get("status")), 0) + 1
        log(f"texts: {len(items)} of {len(todo)} asked ("
            + ", ".join(f"{k} {v}" for k, v in sorted(by.items()))
            + f"), {result.get('run_s')} s"
            + (" - cut at the time limit" if result.get("timeout") else ""))
        return EXIT_OK
    finally:
        os.close(fd)


def _lock(base: str | None = None) -> int | None:
    d = base or STATE_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="collect the Delve News feeds once")
    ap.add_argument("--force", action="store_true", help="fetch every source, due or not")
    ap.add_argument("--dry-run", action="store_true",
                    help="print a summary; no batch, no state")
    args = ap.parse_args(argv)
    try:
        sources = load_sources()
    except ConfigError as exc:
        log(f"{exc.code}: {exc}")
        return EXIT_CONFIG
    if sources is None:
        log(f"no {os.path.join(NEWS_DIR, 'sources.json')} yet (the backend writes it)")
        return EXIT_OK
    fd = _lock()
    if fd is None:
        log("another collection is running")
        return EXIT_OK
    try:
        st = load_state()
        batch = collect(sources, st, force=args.force)
        errors = [r for r in batch["sources"] if r["status"] == "error"]
        summary = (f"{len([r for r in batch['sources'] if r['status'] != 'skipped'])} sources "
                   f"fetched, {len(errors)} failed, {len(batch['items'])} new items, "
                   f"{batch['run_s']} s")
        if args.dry_run:
            print(json.dumps({"summary": summary, "sources": batch["sources"],
                              "items": batch["items"][:20]}, ensure_ascii=False, indent=1))
            return EXIT_OK
        try:
            if batch["sources"]:
                write_batch(batch)
            save_state(st)
        except OSError as exc:
            log(f"store_failed: {type(exc).__name__}: {exc}")
            return EXIT_STORE
        for r in errors:
            log(f"{r['id']}: {r['error_code']}: {r['error']}")
        log(summary)
        return EXIT_OK
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
