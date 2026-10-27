"""watchlib – Delve watchers and page reading (stdlib only).

One module for three callers:
  * the no-agent cron wrapper ``~/.hermes/scripts/watch_<slug>.py`` (``main(slug)``),
  * the plugin tools ``watch_manage`` and ``page_fetch`` (``watch_tools.py``),
  * the offline tests (``tests/test_watch.py``).

A watcher is a JSON config ``~/.hermes/watches/<slug>.json`` plus a state file
``<slug>.state.json``. Each run: fetch the source -> parse the items -> mechanical filters
(title words, price) -> drop items already seen -> publish one ``watch_candidate`` card per
new item and one ``watch_run`` card -> save the state. No LLM is involved; the backend
judges candidates against the free-text ``criteria`` (from the ``watch_def`` card).

The first run is a baseline: the items already listed are recorded as seen and not
announced (unless ``notify_existing``; that is the default for ``stop_after_hit``
watchers, e.g. "tell me when tickets go on sale").

Exit codes of ``main``: 0 ok, 1 fetch failed, 2 parse failed (the site changed or ignored
the search), 3 card/state store failed, 4 bad or missing config.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import http.cookiejar
import ipaddress
import json
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import schemas  # noqa: E402  (agentcards, same directory)

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
WATCH_DIR = os.path.join(HERMES_HOME, "watches")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
LOG_PATH = os.path.join(HERMES_HOME, "logs", "watch.log")
LOG_MAX_BYTES = 512 * 1024

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0 "
              "delve-watch/0.1 (personal use)")
FETCH_TIMEOUT_S = 20
MAX_BYTES = 3 * 1024 * 1024
HOST_INTERVAL_S = 1.2          # >= the strictest crawl-delay seen (hardverapro.hu: 1)
# A browser-like Accept: with "text/html,application/json;q=0.9,..." hardverapro.hu skips its
# cookie test and never starts a session (W1 diag2, 2026-10-27); "*/*" or this one work.
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
SEARCH_RETRY_WAITS_S = (3.0, 5.0)   # hardverapro: session warm-up before searches are served
SLEEP = time.sleep                  # replaced in tests
MAX_REDIRECTS = 8
MAX_CANDIDATES = 10            # per run; the rest is marked seen and counted
MAX_DETAILS = 10               # detail pages fetched per run (only with fetch_details)
MAX_SEEN = 5000
MAX_WATCHES = 20
EXCERPT_CHARS = 1500

EXIT_OK, EXIT_FETCH, EXIT_PARSE, EXIT_STORE, EXIT_CONFIG = 0, 1, 2, 3, 4


class WatchError(Exception):
    exit_code = EXIT_PARSE
    code = "failed"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class FetchError(WatchError):
    exit_code, code = EXIT_FETCH, "fetch_failed"


class BlockedURL(FetchError):
    code = "blocked_url"


class ParseError(WatchError):
    exit_code, code = EXIT_PARSE, "parse_failed"


class StoreError(WatchError):
    exit_code, code = EXIT_STORE, "store_failed"


class ConfigError(WatchError):
    exit_code, code = EXIT_CONFIG, "bad_config"


def log(msg: str) -> None:
    """stderr + own log file (Hermes does not keep a no-agent script's stderr)."""
    print(f"watch: {msg}", file=sys.stderr)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), mode=0o700, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


# --- URL safety ------------------------------------------------------------------------

Resolver = Callable[[str, int], Iterable[str]]


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def check_url(url: str, resolver: Resolver = _resolve) -> str:
    """Only http(s) to public addresses: no loopback, private, link-local, CGNAT (the
    tailnet), multicast or reserved ranges - the backend and llama-server listen on
    127.0.0.1. Every answer of the name must be public. Returns the URL unchanged."""
    if not isinstance(url, str) or len(url) > 2000:
        raise BlockedURL("URL missing or too long")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedURL(f"invalid URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise BlockedURL("URLs with credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("URL without a host")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".ts.net")):
        raise BlockedURL(f"host {host} is not public")
    try:
        addrs = list(resolver(host, port))
    except (OSError, UnicodeError) as exc:
        raise FetchError(f"cannot resolve {host}: {exc}", "dns_failed") from None
    if not addrs:
        raise FetchError(f"cannot resolve {host}", "dns_failed")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"host {host} resolves to a non-public address")
    return url


# --- fetching --------------------------------------------------------------------------


class Page:
    def __init__(self, url: str, final_url: str, status: int, content_type: str, text: str):
        self.url, self.final_url, self.status = url, final_url, status
        self.content_type, self.text = content_type, text

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise ParseError(f"not JSON: {exc}") from None


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolver: Resolver) -> None:
        self.resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        check_url(urllib.parse.urljoin(req.full_url, newurl), self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _charset(content_type: str, raw: bytes) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", raw[:4096], re.I)
        name = m.group(1).decode("ascii", "replace") if m else "utf-8"
    else:
        name = m.group(1)
    try:
        "".encode(name)
        return name
    except LookupError:
        return "utf-8"


class Fetcher:
    """urllib with a persistent cookie jar, per-host spacing and the URL check."""

    def __init__(self, cookie_path: str | None = None, resolver: Resolver = _resolve,
                 interval_s: float = HOST_INTERVAL_S, host_dir: str | None = None) -> None:
        self.resolver = resolver
        self.interval_s = interval_s
        self.host_dir = host_dir if host_dir is not None else os.path.join(WATCH_DIR, ".hosts")
        self.cookie_path = cookie_path
        self.jar = http.cookiejar.MozillaCookieJar(cookie_path) if cookie_path else \
            http.cookiejar.CookieJar()
        if cookie_path and os.path.exists(cookie_path):
            with contextlib.suppress(OSError, http.cookiejar.LoadError):
                self.jar.load(ignore_discard=True)
        redirects = _Redirects(resolver)
        redirects.max_redirections = MAX_REDIRECTS
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), redirects)

    def _space(self, host: str) -> None:
        """At most one request per host every interval_s, across processes."""
        if self.interval_s <= 0 or not self.host_dir:
            return
        os.makedirs(self.host_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.host_dir, re.sub(r"[^a-z0-9.-]", "_", host))
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.pread(fd, 32, 0)
            try:
                last = float(raw.decode("ascii").strip() or 0)
            except ValueError:
                last = 0.0
            wait = last + self.interval_s - time.time()
            if wait > 0:      # capped: a stamp from a clock jump must not stall the run
                time.sleep(min(wait, self.interval_s))
            stamp = f"{time.time():.3f}".encode("ascii").ljust(32)
            os.pwrite(fd, stamp, 0)
        finally:
            os.close(fd)

    def get(self, url: str, accept: str = ACCEPT_HTML) -> Page:
        check_url(url, self.resolver)
        host = urllib.parse.urlsplit(url).hostname or ""
        self._space(host)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": accept,
            "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.7"})
        try:
            with self.opener.open(req, timeout=FETCH_TIMEOUT_S) as r:
                status = r.status
                ctype = r.headers.get("Content-Type", "")
                raw = r.read(MAX_BYTES + 1)
                final = r.geturl()
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code} for {url}", f"http_{exc.code}") from None
        except BlockedURL:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, BlockedURL):
                raise reason from None
            raise FetchError(f"{type(exc).__name__}: {reason}") from None
        if len(raw) > MAX_BYTES:
            raise FetchError(f"response larger than {MAX_BYTES} bytes", "too_large")
        if self.cookie_path:
            with contextlib.suppress(OSError):
                os.makedirs(os.path.dirname(self.cookie_path), mode=0o700, exist_ok=True)
                self.jar.save(ignore_discard=True)
                os.chmod(self.cookie_path, 0o600)
        text = raw.decode(_charset(ctype, raw), errors="replace")
        return Page(url, final, status, ctype, text)


# --- a small DOM -----------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
SKIP_TEXT = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section",
         "article", "header", "footer", "table", "ul", "ol", "dd", "dt", "td", "th"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def iter(self) -> Iterable["Node"]:
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))

    def find_all(self, tag: str | None = None, cls: str | None = None,
                 attr: str | None = None) -> list["Node"]:
        return [n for n in self.iter() if n is not self
                and (tag is None or n.tag == tag)
                and (cls is None or cls in n.classes())
                and (attr is None or attr in n.attrs)]

    def find(self, tag: str | None = None, cls: str | None = None,
             attr: str | None = None) -> "Node | None":
        for n in self.iter():
            if n is not self and (tag is None or n.tag == tag) \
                    and (cls is None or cls in n.classes()) and (attr is None or attr in n.attrs):
                return n
        return None

    def text(self, sep: str = " ") -> str:
        parts: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in SKIP_TEXT:
                    if c.tag in BLOCK:
                        parts.append(sep)
                    walk(c)
                    if c.tag in BLOCK:
                        parts.append(sep)
        walk(self)
        return " ".join("".join(parts).split()) if sep == " " else "".join(parts)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent  # type: ignore[assignment]
        if n is not self.root:
            self.cur = n.parent  # type: ignore[assignment]

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text: str) -> Node:
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


def page_title(root: Node) -> str:
    t = root.find("title")
    return t.text() if t else ""


def readable_lines(root: Node) -> list[str]:
    body = root.find("body") or root
    lines = [" ".join(line.split()) for line in body.text(sep="\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line and (not out or out[-1] != line):
            out.append(line)
    return out


def links(root: Node, base: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in root.find_all("a", attr="href"):
        href = a.attrs["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urllib.parse.urljoin(base, href).split("#", 1)[0]
        text = a.text()
        if not url.startswith(("http://", "https://")) or url in seen or not text:
            continue
        seen.add(url)
        out.append((text, url))
    return out


# --- normalisation and filters ---------------------------------------------------------


def fold(s: str) -> str:
    """Case- and accent-insensitive form: 'Bányászott' -> 'banyaszott'."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def price_value(text: str | None) -> int | None:
    """'299 990 Ft' -> 299990; 'Keresem', 'Csere', '' -> None."""
    if not text:
        return None
    m = re.search(r"\d[\d\s.  ]*", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits and len(digits) <= 10 else None


def item_matches(item: dict[str, Any], cfg: dict[str, Any]) -> bool:
    title = fold(item.get("title") or "")
    if any(fold(w) not in title for w in cfg.get("include") or []):
        return False
    anyw = cfg.get("any") or []
    if anyw and not any(fold(w) in title for w in anyw):
        return False
    if any(fold(w) in title for w in cfg.get("exclude") or []):
        return False
    lo, hi = cfg.get("min_price"), cfg.get("max_price")
    if lo is not None or hi is not None:
        pv = item.get("price_value")
        if pv is None:
            return False
        if (lo is not None and pv < lo) or (hi is not None and pv > hi):
            return False
    return True


def short_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clip(s: str | None, n: int) -> str | None:
    if not s:
        return None
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# --- sources ---------------------------------------------------------------------------


class Source:
    name = ""
    has_details = False
    description = ""

    def validate_url(self, url: str) -> str:
        return url

    def items(self, fetcher: Fetcher, url: str) -> tuple[list[dict[str, Any]], Page]:
        raise NotImplementedError

    def details(self, fetcher: Fetcher, item: dict[str, Any]) -> str | None:
        return None


class HardverApro(Source):
    """hardverapro.hu listing or search pages (``li.media[data-uadid]``)."""
    name = "hardverapro"
    has_details = True
    description = ("hardverapro.hu: a category page (/aprok/...) or the site-wide search "
                   "https://hardverapro.hu/aprok/keres.php?stext=<words>")
    HOSTS = ("hardverapro.hu", "www.hardverapro.hu")

    def validate_url(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in self.HOSTS or not p.path.startswith("/aprok/"):
            raise ConfigError("hardverapro: use an https://hardverapro.hu/aprok/... URL")
        q = urllib.parse.parse_qs(p.query)
        if "offset" in q:
            raise ConfigError("hardverapro: paging (offset=) is not allowed by robots.txt")
        return url

    @staticmethod
    def parse_list(text: str, base: str) -> list[dict[str, Any]]:
        root = parse_html(text)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for li in root.find_all("li", cls="media", attr="data-uadid"):
            uid = li.attrs["data-uadid"].strip()
            if not uid.isdigit() or uid in seen:
                continue           # promoted ads repeat; keep the first
            title_col = li.find(cls="uad-col-title")
            h = title_col.find("h1") if title_col else None
            a = h.find("a", attr="href") if h else None
            if a is None:
                raise ParseError(f"listing {uid}: no title link")
            seen.add(uid)
            price_node = li.find(cls="uad-price")
            price = price_node.text() if price_node else None
            city = li.find(cls="uad-cities")
            info = li.find(cls="uad-col-info")
            when = info.find("time") if info else None
            out.append({
                "item_id": f"ha:{uid}",
                "title": a.text(),
                "url": urllib.parse.urljoin(base, a.attrs["href"]),
                "price": price or None,
                "price_value": price_value(price),
                "wanted": bool(price) and fold(price).startswith(("keres", "csere")),
                "location": city.text() if city else None,
                "posted": when.text() if when else None,
            })
        return out

    def items(self, fetcher, url):
        asked = urllib.parse.urlsplit(url)
        waits = list(SEARCH_RETRY_WAITS_S)
        while True:
            page = fetcher.get(url)
            got = urllib.parse.urlsplit(page.final_url)
            if "keres.php" not in asked.path or "keres.php" in got.path:
                break
            # Observed (W1 diag/diag2): without a session the first search runs a sign-on round
            # (auth.rios.hu) and lands on the index, and the next one or two searches within a
            # few seconds of it are redirected too. A search with no hits is a normal page with
            # no rows, not a redirect. The session cookies are kept (sid 30 days, vid 1 year),
            # so later runs search at once.
            if waits:
                wait = waits.pop(0)
                log(f"hardverapro: search redirected to {got.path}, retrying in {wait:g} s")
                SLEEP(wait)
                continue
            raise ParseError(f"the site ignored the search and redirected to {got.path}; a "
                             "category search needs a filter (e.g. maxprice=) - or use "
                             "https://hardverapro.hu/aprok/keres.php?stext=<words>",
                             "search_ignored")
        if "HardverApró" not in page.text[:20000] and "hardverapro" not in page.text[:20000]:
            raise ParseError("not a HardverApró page")
        items = [i for i in self.parse_list(page.text, page.final_url) if not i["wanted"]]
        return items, page

    @staticmethod
    def parse_detail(text: str) -> str | None:
        root = parse_html(text)
        parts: list[str] = []
        # the seller box is "uad-content-block uad-time-location"; place + delivery is the
        # plain "uad-time-location" below the pictures
        loc = next((n for n in root.find_all("div", cls="uad-time-location")
                    if "uad-content-block" not in n.classes()), None)
        if loc:
            parts.append(loc.text())
        det = root.find(cls="uad-details")
        if det:
            for tr in det.find_all("tr"):
                parts.append(tr.text())
        body = root.find(cls="uad-content")
        if body:   # the ad text; the share menu after it is left out
            inner = body.find("div", cls="rtif-content")
            parts.append((inner or body).text())
        text = " · ".join(p for p in parts if p)
        return _clip(text, EXCERPT_CHARS)

    def details(self, fetcher, item):
        return self.parse_detail(fetcher.get(item["url"]).text)


class CinemaCity(Source):
    """Cinema City Hungary: the film appears in the bookable film list of the site's API."""
    name = "cinemacity"
    description = ("cinemacity.hu film page https://www.cinemacity.hu/films/<name>/<id>; "
                   "a hit = the film can be booked (it is in the site's bookable film list)")
    API = "https://www.cinemacity.hu/hu/data-api-service/v1/quickbook/10102/films/until/{until}"
    URL_RE = re.compile(r"https://www\.cinemacity\.hu/films/[a-z0-9-]+/([0-9a-z]{4,12})/?")

    def validate_url(self, url):
        if not self.URL_RE.fullmatch(url.strip()):
            raise ConfigError("cinemacity: use the film page URL "
                              "https://www.cinemacity.hu/films/<name>/<id>")
        return url.strip()

    @staticmethod
    def parse_films(data: Any) -> list[dict[str, Any]]:
        try:
            films = data["body"]["films"]
        except (KeyError, TypeError):
            raise ParseError("unexpected film list format") from None
        if not isinstance(films, list):
            raise ParseError("unexpected film list format")
        out = []
        for f in films:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str):
                continue
            out.append({"film_id": f["id"].lower(), "title": str(f.get("name") or f["id"]),
                        "url": str(f.get("link") or ""), "release": str(f.get("releaseDate") or "")})
        return out

    def items(self, fetcher, url):
        film_id = self.URL_RE.fullmatch(url.strip()).group(1).lower()  # type: ignore[union-attr]
        until = (dt.date.today() + dt.timedelta(days=400)).isoformat()
        page = fetcher.get(self.API.format(until=until), accept="application/json")
        films = self.parse_films(page.json())
        if not films:
            raise ParseError("empty film list (the API changed?)")
        out = []
        for f in films:
            if f["film_id"] == film_id:
                out.append({"item_id": f"cc:{film_id}", "title": f"{f['title']} – jegyek kaphatók",
                            "url": f["url"] if f["url"].startswith("https://") else url,
                            "posted": f["release"][:10] or None})
        return out, page


class GenericPage(Source):
    """Any public page: the links whose text matches the words become the items."""
    name = "page"
    description = "any public page; items = links whose text contains the include/any words"

    def items(self, fetcher, url):
        page = fetcher.get(url)
        root = parse_html(page.text)
        items = [{"item_id": "pg:" + short_id(u), "title": _clip(t, 300), "url": u}
                 for t, u in links(root, page.final_url)]
        return items, page


SOURCES: dict[str, Source] = {s.name: s for s in (HardverApro(), CinemaCity(), GenericPage())}


def source_for_url(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in HardverApro.HOSTS:
        return "hardverapro"
    if host == "www.cinemacity.hu" and CinemaCity.URL_RE.fullmatch(url):
        return "cinemacity"
    return "page"


# --- config and state ------------------------------------------------------------------

SLUG_RE = schemas.WATCH_SLUG_RE
CONFIG_FIELDS = ("slug", "name", "source", "url", "include", "any", "exclude", "min_price",
                 "max_price", "criteria", "fetch_details", "stop_after_hit",
                 "notify_existing", "schedule")


def config_path(slug: str, base: str | None = None) -> str:
    return os.path.join(base or WATCH_DIR, f"{slug}.json")


def state_path(slug: str, base: str | None = None) -> str:
    return os.path.join(base or WATCH_DIR, f"{slug}.state.json")


def cookie_path(slug: str, base: str | None = None) -> str:
    return os.path.join(base or WATCH_DIR, ".cookies", f"{slug}.txt")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-")[:40].strip("-")
    return s if len(s) >= 2 else "watch-" + short_id(name)[:6]


def def_hash(cfg: dict[str, Any]) -> str:
    keys = ("source", "url", "include", "any", "exclude", "min_price", "max_price",
            "criteria", "fetch_details", "stop_after_hit", "notify_existing", "name", "schedule")
    blob = json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return short_id(blob)


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalise a watcher config; raises ConfigError with all problems."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    payload = {k: cfg.get(k) for k in CONFIG_FIELDS if cfg.get(k) is not None}
    payload["def_hash"] = "0" * 16
    try:
        clean = schemas.validate_watch_def(payload, dt.datetime.now(dt.timezone.utc))
    except schemas.CardValidationError as exc:
        raise ConfigError("; ".join(exc.errors)) from None
    clean.pop("def_hash", None)
    src = SOURCES[clean["source"]]
    clean["url"] = src.validate_url(clean["url"])
    if clean["source"] == "page" and not (clean["include"] or clean["any"]):
        raise ConfigError("page: give at least one include or any word")
    if clean["fetch_details"] and not src.has_details:
        clean["fetch_details"] = False
    lo, hi = clean.get("min_price"), clean.get("max_price")
    if lo is not None and hi is not None and lo > hi:
        raise ConfigError("min_price is above max_price")
    if "notify_existing" not in cfg or cfg.get("notify_existing") is None:
        clean["notify_existing"] = bool(clean["stop_after_hit"])
    for k in ("job_id", "created_at"):
        if isinstance(cfg.get(k), str):
            clean[k] = cfg[k][:64]
    return clean


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config(slug: str, base: str | None = None) -> dict[str, Any]:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ConfigError(f"invalid watcher name {slug!r}")
    try:
        with open(config_path(slug, base), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"no watcher {slug!r}") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"unreadable config: {exc}") from None
    cfg = validate_config(raw)
    if cfg["slug"] != slug:
        raise ConfigError("slug in the file does not match its name")
    return cfg


def save_config(cfg: dict[str, Any], base: str | None = None) -> None:
    _write_json(config_path(cfg["slug"], base), cfg)


def list_configs(base: str | None = None) -> list[dict[str, Any]]:
    d = base or WATCH_DIR
    out = []
    with contextlib.suppress(FileNotFoundError):
        for name in sorted(os.listdir(d)):
            if name.endswith(".json") and not name.endswith(".state.json"):
                slug = name[:-5]
                try:
                    out.append(load_config(slug, base))
                except ConfigError as exc:
                    out.append({"slug": slug, "error": str(exc)})
    return out


def load_state(slug: str, base: str | None = None) -> dict[str, Any]:
    try:
        with open(state_path(slug, base), encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("seen"), dict):
            return st
        log(f"{slug}: state has an unexpected shape, starting over")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log(f"{slug}: unreadable state ({exc}), starting over")
    return {"v": 1, "seen": {}}


def save_state(slug: str, st: dict[str, Any], base: str | None = None) -> None:
    seen = st.get("seen", {})
    if len(seen) > MAX_SEEN:
        keep = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN]
        st["seen"] = dict(keep)
    _write_json(state_path(slug, base), st)


# --- running ---------------------------------------------------------------------------


def _iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect(cfg: dict[str, Any], fetcher: Fetcher) -> tuple[list[dict], list[dict], Page]:
    """Fetch and filter: (all items, matching items, page)."""
    src = SOURCES[cfg["source"]]
    items, page = src.items(fetcher, cfg["url"])
    matched = [i for i in items if item_matches(i, cfg)]
    return items, matched, page


def _candidate_payload(slug: str, item: dict[str, Any], excerpt: str | None) -> dict[str, Any]:
    p = {"slug": slug, "item_id": item["item_id"], "title": _clip(item.get("title"), 300) or "?",
         "url": item["url"], "price": _clip(item.get("price"), 60),
         "price_value": item.get("price_value"), "location": _clip(item.get("location"), 120),
         "posted": _clip(item.get("posted"), 60), "excerpt": excerpt}
    return {k: v for k, v in p.items() if v is not None}


Publisher = Callable[[dict[str, Any]], Any]


def _default_publisher() -> Publisher:
    import store  # noqa: E402  (agentcards)
    return store.append_event


def run_watch(slug: str, *, dry_run: bool = False, fetcher: Fetcher | None = None,
              publish: Publisher | None = None, base: str | None = None,
              now: dt.datetime | None = None) -> dict[str, Any]:
    """One watcher run. Returns a summary; raises WatchError (with exit_code) on failure,
    after publishing an error ``watch_run`` card (not in dry-run)."""
    started = time.monotonic()
    now = now or dt.datetime.now(dt.timezone.utc)
    cfg = load_config(slug, base)
    if publish is None and not dry_run:
        publish = _default_publisher()
    fetcher = fetcher or Fetcher(cookie_path(slug, base))
    st = load_state(slug, base)

    def emit(card_type: str, payload: dict[str, Any], severity: str = "info") -> None:
        if dry_run:
            return
        try:
            ev = schemas.build_event(card_type, payload, severity, "script", now=now)
            publish(ev)  # type: ignore[misc]
        except schemas.CardValidationError as exc:
            raise StoreError(f"{card_type}: {'; '.join(exc.errors)}", "card_invalid") from None
        except (OSError, ValueError) as exc:
            raise StoreError(f"{card_type}: {type(exc).__name__}: {exc}") from None

    h = def_hash(cfg)
    if st.get("def_hash") != h:
        emit("watch_def", {**{k: cfg.get(k) for k in CONFIG_FIELDS if cfg.get(k) is not None},
                           "def_hash": h})
        if st.get("url") not in (None, cfg["url"]):   # a new source: start a new baseline
            st["seen"] = {}
            st.pop("baseline_at", None)

    try:
        items, matched, page = collect(cfg, fetcher)
    except WatchError as exc:
        if not dry_run:
            st["errors"] = int(st.get("errors", 0)) + 1
            st["last_error"] = str(exc)[:300]
            with contextlib.suppress(WatchError):
                emit("watch_run", {"slug": slug, "status": "error", "checked_at": _iso(now),
                                   "error_code": exc.code, "error": str(exc)[:300],
                                   "duration_ms": int((time.monotonic() - started) * 1000)},
                     "warn")
            with contextlib.suppress(OSError):
                save_state(slug, st, base)
        raise

    seen: dict[str, str] = st.setdefault("seen", {})
    new = [i for i in matched if i["item_id"] not in seen]
    baseline = "baseline_at" not in st
    stamp = _iso(now)
    if baseline and not cfg.get("notify_existing"):
        announce: list[dict] = []
    else:
        announce = new[:MAX_CANDIDATES]

    excerpts: dict[str, str | None] = {}
    if cfg.get("fetch_details") and SOURCES[cfg["source"]].has_details:
        for item in announce[:MAX_DETAILS]:
            try:
                excerpts[item["item_id"]] = SOURCES[cfg["source"]].details(fetcher, item)
            except WatchError as exc:
                log(f"{slug}: details of {item['item_id']} failed: {exc}")
                excerpts[item["item_id"]] = None

    summary = {"slug": slug, "status": "ok", "checked_at": stamp, "items": len(items),
               "matched": len(matched), "new": len(new), "candidates": len(announce),
               "baseline": baseline, "final_url": page.final_url[:600]
               if page.final_url.startswith(("http://", "https://")) else None,
               "duration_ms": int((time.monotonic() - started) * 1000)}
    summary = {k: v for k, v in summary.items() if v is not None}
    if dry_run:
        return {**summary, "sample": [
            {k: i.get(k) for k in ("item_id", "title", "price", "location", "posted", "url")
             if i.get(k)} for i in (new or matched)[:10]]}

    for item in announce:
        emit("watch_candidate", _candidate_payload(slug, item, excerpts.get(item["item_id"])))
    emit("watch_run", summary)
    for item in new:
        seen[item["item_id"]] = stamp
    st.update({"v": 1, "def_hash": h, "url": cfg["url"], "last_ok_at": stamp, "errors": 0})
    st.pop("last_error", None)
    st.setdefault("baseline_at", stamp)
    try:
        save_state(slug, st, base)
    except OSError as exc:
        raise StoreError(f"state: {exc}") from None
    log(f"{slug}: ok items={len(items)} matched={len(matched)} new={len(new)} "
        f"announced={len(announce)} baseline={baseline}")
    return summary


def preview(cfg: dict[str, Any], fetcher: Fetcher | None = None) -> dict[str, Any]:
    """Fetch + filter a (not yet saved) config: what the watcher would see now."""
    cfg = validate_config(cfg)
    items, matched, page = collect(cfg, fetcher or Fetcher())
    return {"items": len(items), "matched": len(matched), "final_url": page.final_url,
            "sample": [{k: i.get(k) for k in ("title", "price", "location", "posted", "url")
                        if i.get(k)} for i in matched[:8]]}


# --- page reading (the page_fetch tool) ------------------------------------------------

_COUNT_RE = re.compile(r"(\d[\d\s.]*)\s+találat", re.I)


def read_page(url: str, mode: str = "text", find: str | None = None, max_chars: int = 6000,
              fetcher: Fetcher | None = None) -> dict[str, Any]:
    """A page as compact text, links or (for known sites) listings, within max_chars."""
    max_chars = max(1000, min(int(max_chars or 6000), 12000))
    fetcher = fetcher or Fetcher(os.path.join(WATCH_DIR, ".cookies", "_page_fetch.txt"))
    src_name = source_for_url(url)
    terms = [fold(t) for t in (find or "").split() if t.strip()]

    def keep(s: str) -> bool:
        return not terms or all(t in fold(s) for t in terms)

    if mode == "listings" and src_name in ("hardverapro", "cinemacity"):
        src = SOURCES[src_name]
        if src_name == "cinemacity":
            src.validate_url(url)
        items, page = src.items(fetcher, url)
        total = None
        if src_name == "hardverapro":
            m = _COUNT_RE.search(parse_html(page.text).text())
            total = int(re.sub(r"\D", "", m.group(1))) if m else None
        rows = [i for i in items if keep(i.get("title") or "")]
        out_items, used = [], 0
        for i in rows:
            row = {k: i[k] for k in ("title", "price", "location", "posted", "url") if i.get(k)}
            used += len(json.dumps(row, ensure_ascii=False))
            if used > max_chars:
                break
            out_items.append(row)
        return {"ok": True, "url": url, "final_url": page.final_url, "mode": "listings",
                "site_total": total, "items_on_page": len(items), "matching": len(rows),
                "items": out_items, "truncated": len(out_items) < len(rows),
                "note": "site_total = the site's own result count; only the first page is read"}

    page = fetcher.get(url)
    if "json" in page.content_type:
        text = page.text
        return {"ok": True, "url": url, "final_url": page.final_url, "mode": "json",
                "content": text[:max_chars], "truncated": len(text) > max_chars,
                "total_chars": len(text)}
    root = parse_html(page.text)
    title = page_title(root)
    if mode == "links" or mode == "listings":
        rows = [f"{t[:160]} — {u}" for t, u in links(root, page.final_url) if keep(t)]
    else:
        rows = [line for line in readable_lines(root) if keep(line)]
    out, used = [], 0
    for r in rows:
        if used + len(r) + 1 > max_chars:
            break
        out.append(r)
        used += len(r) + 1
    return {"ok": True, "url": url, "final_url": page.final_url, "status": page.status,
            "title": title, "mode": "links" if mode != "text" else "text",
            "content": "\n".join(out), "lines": len(rows), "truncated": len(out) < len(rows)}


# --- the cron wrapper ------------------------------------------------------------------

WRAPPER = '''#!/usr/bin/env python3
"""Delve watcher {slug!r} (generated by watch_manage; edit the config, not this file)."""
import os, sys
sys.path.insert(0, os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
                                "plugins", "agentcards"))
import watchlib  # noqa: E402
sys.exit(watchlib.main({slug!r}))
'''


def wrapper_name(slug: str) -> str:
    return f"watch_{slug.replace('-', '_')}.py"


def write_wrapper(slug: str, scripts_dir: str | None = None) -> str:
    d = scripts_dir or SCRIPTS_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, wrapper_name(slug))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(WRAPPER.format(slug=slug))
    os.replace(tmp, path)
    return path


def main(slug: str | None = None, argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run one Delve watcher")
    if slug is None:
        ap.add_argument("slug")
    ap.add_argument("--dry-run", action="store_true", help="no cards, no state; print a summary")
    args = ap.parse_args(argv)
    slug = slug or args.slug
    try:
        out = run_watch(slug, dry_run=args.dry_run)
    except WatchError as exc:
        log(f"{slug}: {exc.code}: {exc}")
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - a crash must still leave a trace
        log(f"{slug}: crashed: {type(exc).__name__}: {exc}")
        return EXIT_PARSE
    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
