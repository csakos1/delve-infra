"""recipes – watchers for any public page, described as data (Delve W5).

A recipe (``extract`` in a watcher config) tells a fixed, deterministic engine what to read:

  list       repeated blocks of an HTML page:
             {"mode": "list", "item": "<css>", "fields": {"title": "h3", "link": "a@href",
              "price": ".price", "location": ..., "posted": ..., "text": "", "id": "@data-id"}}
  json       a list inside JSON - the page itself, its __NEXT_DATA__, its JSON-LD, a
             <script type="application/json" id=...>, or an API URL seen while rendering:
             {"mode": "json", "from": "page|next_data|ld_json|script|api", "script_id": ...,
              "api_url": ..., "items": "$.props.pageProps.ads[*]",
              "fields": {"title": "subject", "link": "url", "price": "price.value", ...}}
  condition  one fact of one page, announced each time it turns true:
             {"mode": "condition", "from": "ld_product|css|page_text", "selector": ...,
              "test": "in_stock|contains|absent|below|above", "value": ...}

``render: true`` in the config loads the page in the headless Chromium (render_page.py,
own venv) instead of plain HTTP - for pages built by JavaScript or refused to scripts.

The model never writes these from nothing: ``structure()`` (page_fetch mode "structure")
proposes numbered candidates with real sample items, ``watch_manage`` takes one by number,
and its preview shows what the recipe reads before anything is saved.

Selectors: tag, .class, #id, [attr], [attr=v], [attr*=v], [attr^=v]; descendant (space) and
child (>) combinators. A field is "css" (text of the first match), "css@attr", "@attr" (the
item's own attribute) or "" (the item's whole text). JSON paths: $.a.b[0].c, one [*] in
"items"; field paths are relative to an item.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.robotparser
from typing import Any

try:  # inside the plugin package: the same module objects as watch_tools uses
    from . import watchlib as wl
except ImportError:  # the cron wrapper, render_page.py and the tests: top-level modules
    import watchlib as wl  # type: ignore[no-redef]

MODES = ("list", "json", "condition")
FIELDS = ("title", "link", "price", "location", "posted", "text", "id")
JSON_FROM = ("page", "next_data", "ld_json", "script", "api")
COND_FROM = ("ld_product", "css", "page_text")
COND_TESTS = ("in_stock", "contains", "absent", "below", "above")
IN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly", "presale",
            "preorder")
MAX_ITEMS = 200
ROBOTS_TOKEN = "delve-watch"
ROBOTS_MAX_AGE_S = 24 * 3600
STRUCTURE_MAX_AGE_S = 2 * 3600
PREVIEW_MAX_AGE_S = 2 * 3600
PRICE_RE = re.compile(r"(?:\d{1,3}(?:[  . ]\d{3})+|\d{3,7})(?:[,.]\d{1,2})?\s?"
                      r"(?:Ft|HUF|€|EUR|\$|USD)\b|(?:€|\$)\s?\d[\d,.]*", re.I)
NOISE_CLASS = re.compile(r"\d{3,}|^(active|selected|odd|even|first|last|hidden|visible|"
                         r"is-[\w-]+|js-[\w-]+|ng-[\w-]+|css-[\w-]+|sc-[\w-]+)$")


def generated(name: str) -> bool:
    """A class or id that a build or a page script made up and that changes between builds
    or page loads: 'swiper-wrapper-d1c39cc0b81ecc04', 'Link__dPFjRI', 'list__2Gh7vS',
    'css-1x2y3z', 'ember123', a UUID. Words, BEM names and short numbers stay."""
    if NOISE_CLASS.search(name):
        return True
    if re.search(r"__[A-Za-z0-9]{5,}$", name) and re.search(r"[0-9]|[a-z][A-Z]|[A-Z][a-z][A-Z]",
                                                            name.rsplit("__", 1)[1]):
        return True                          # CSS modules / styled names: Block__hash
    for seg in re.split(r"[-_:.]+", name):
        if len(seg) >= 5 and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg) \
                and len(re.findall(r"\d", seg)) >= 2:
            return True                      # hex / base62 chunks: d1c39cc0b81ecc04, 1x2y3z
    return False


class RecipeError(wl.ParseError):
    code = "recipe_empty"


# --- selectors -------------------------------------------------------------------------

_COMPOUND = re.compile(r"([a-z][a-z0-9-]*|\*)?((?:\.[\w-]+|#[\w-]+|\[[\w-]+(?:[*^]?=[^\]]*)?\])*)")
_PART = re.compile(r"\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:([*^]?=)([^\]]*))?\]")


def _compound(spec: str) -> tuple:
    m = _COMPOUND.fullmatch(spec)
    if not m or not spec:
        raise wl.ConfigError(f"unsupported selector part {spec!r}")
    tag = m.group(1) if m.group(1) not in (None, "*") else None
    classes, ids, attrs = set(), None, []
    for cls, ident, attr, op, val in _PART.findall(m.group(2)):
        if cls:
            classes.add(cls)
        elif ident:
            ids = ident
        else:
            attrs.append((attr, op or None, val.strip("'\"")))
    return tag, classes, ids, tuple(attrs)


def parse_selector(sel: str) -> list[tuple[str, tuple]]:
    if not isinstance(sel, str) or not sel.strip() or len(sel) > 300:
        raise wl.ConfigError("selector missing or too long")
    steps, comb = [], " "
    for tok in sel.replace(">", " > ").split():
        if tok == ">":
            if comb == ">" or not steps:
                raise wl.ConfigError(f"bad selector {sel!r}")
            comb = ">"
            continue
        steps.append((comb, _compound(tok)))
        comb = " "
    if comb == ">" or not steps:
        raise wl.ConfigError(f"bad selector {sel!r}")
    return steps


def _matches(n: wl.Node, c: tuple) -> bool:
    tag, classes, nid, attrs = c
    if (tag is not None and n.tag != tag) or not classes <= n.classes():
        return False
    if nid is not None and n.attrs.get("id") != nid:
        return False
    for name, op, val in attrs:
        have = n.attrs.get(name)
        if have is None or (op == "=" and have != val) or (op == "*=" and val not in have) \
                or (op == "^=" and not have.startswith(val)):
            return False
    return True


def select(root: wl.Node, sel: str) -> list[wl.Node]:
    cur = [root]
    for comb, c in parse_selector(sel):
        nxt, seen = [], set()
        for base in cur:
            pool = [x for x in base.children if isinstance(x, wl.Node)] if comb == ">" \
                else [x for x in base.iter() if x is not base]
            for n in pool:
                if id(n) not in seen and _matches(n, c):
                    seen.add(id(n))
                    nxt.append(n)
        cur = nxt
    return cur


def _split_field(spec: str) -> tuple[str, str | None]:
    spec = spec.strip()
    if spec.startswith("@"):
        return "", spec[1:]
    if "@" in spec:
        css, _, attr = spec.rpartition("@")
        return css.strip(), attr.strip()
    return spec, None


def check_field(spec: Any, name: str) -> str:
    if not isinstance(spec, str) or len(spec) > 200:
        raise wl.ConfigError(f"fields.{name}: a selector string")
    css, attr = _split_field(spec)
    if css and css != ".":
        parse_selector(css)
    if attr is not None and not re.fullmatch(r"[\w-]{1,40}", attr):
        raise wl.ConfigError(f"fields.{name}: bad attribute name")
    return spec.strip()


def field_value(item: wl.Node, spec: str, base: str) -> str | None:
    css, attr = _split_field(spec)
    node: wl.Node | None = item
    if css and css != ".":
        found = select(item, css)
        node = found[0] if found else None
    if node is None:
        return None
    if attr is None:
        return node.text() or None
    val = node.attrs.get(attr)
    if val and attr in ("href", "src", "data-href", "data-url"):
        val = urllib.parse.urljoin(base, val.strip())
    return val.strip() if isinstance(val, str) and val.strip() else None


# --- JSON paths ------------------------------------------------------------------------

_SEG = re.compile(r"\.([^.\[\]]+)|\[(\d+|\*)\]|\['([^']+)'\]")


def parse_path(path: str) -> list:
    if not isinstance(path, str) or len(path) > 300:
        raise wl.ConfigError("JSON path missing or too long")
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    elif p and not p.startswith((".", "[")):
        p = "." + p
    segs, pos = [], 0
    while pos < len(p):
        m = _SEG.match(p, pos)
        if not m:
            raise wl.ConfigError(f"bad JSON path {path!r}")
        key, idx, quoted = m.groups()
        segs.append(key if key is not None else quoted if quoted is not None else
                    ("*" if idx == "*" else int(idx)))
        pos = m.end()
    return segs


def json_get(obj: Any, segs: list) -> Any:
    for s in segs:
        if isinstance(s, int):
            if not isinstance(obj, list) or s >= len(obj):
                return None
            obj = obj[s]
        elif isinstance(obj, dict):
            if s not in obj:
                return None
            obj = obj[s]
        else:
            return None
    return obj


def json_items(obj: Any, path: str) -> list | None:
    segs = parse_path(path)
    if segs.count("*") > 1:
        raise wl.ConfigError("items path: at most one [*]")
    if "*" in segs:
        i = segs.index("*")
        lst = json_get(obj, segs[:i])
        if not isinstance(lst, list):
            return None
        rest = segs[i + 1:]
        return [v for v in (json_get(x, rest) for x in lst) if v is not None]
    got = json_get(obj, segs)
    return got if isinstance(got, list) else None


def _scalar(v: Any) -> str | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, str):
        return " ".join(v.split()) or None
    if isinstance(v, dict):         # {"label": "550 000 Ft", "value": 550000}, {"name": ...}
        for k in ("label", "name", "text", "value", "title"):
            if isinstance(v.get(k), (str, int, float)) and not isinstance(v.get(k), bool):
                return _scalar(v[k])
    return None


# --- validation ------------------------------------------------------------------------


def validate_extract(x: Any) -> dict[str, Any]:
    if not isinstance(x, dict):
        raise wl.ConfigError("extract must be an object")
    mode = x.get("mode")
    if mode not in MODES:
        raise wl.ConfigError(f"extract.mode: one of {', '.join(MODES)}")
    out: dict[str, Any] = {"mode": mode}
    if x.get("wait_for") is not None:
        parse_selector(x["wait_for"])
        out["wait_for"] = x["wait_for"].strip()
    if mode in ("list", "json"):
        fields = x.get("fields") or {}
        if not isinstance(fields, dict) or set(fields) - set(FIELDS):
            raise wl.ConfigError(f"extract.fields: keys among {', '.join(FIELDS)}")
        if mode == "list":
            parse_selector(x.get("item"))
            out["item"] = x["item"].strip()
            out["fields"] = {k: check_field(v, k) for k, v in fields.items()}
        else:
            src = x.get("from", "page")
            if src not in JSON_FROM:
                raise wl.ConfigError(f"extract.from: one of {', '.join(JSON_FROM)}")
            out["from"] = src
            if src == "script":
                if not re.fullmatch(r"[\w.:-]{1,80}", str(x.get("script_id") or "")):
                    raise wl.ConfigError("extract.script_id: the id of the <script> element")
                out["script_id"] = x["script_id"]
            if src == "api":
                u = x.get("api_url")
                if not isinstance(u, str) or not u.startswith(("http://", "https://")) or len(u) > 1000:
                    raise wl.ConfigError("extract.api_url: an http(s) URL")
                out["api_url"] = u
            items = x.get("items")
            if not isinstance(items, str) or not items.strip().strip("$"):
                raise wl.ConfigError("extract.items: a JSON path to the list, e.g. $.data.items[*]")
            parse_path(items)
            out["items"] = items
            for k, v in fields.items():
                if not isinstance(v, str):
                    raise wl.ConfigError(f"fields.{k}: a JSON path string")
                parse_path(v)
            out["fields"] = dict(fields)
        if "title" not in out["fields"]:
            raise wl.ConfigError("extract.fields.title is required")
        return out
    src = x.get("from")
    if src not in COND_FROM:
        raise wl.ConfigError(f"extract.from: one of {', '.join(COND_FROM)}")
    test = x.get("test")
    if test not in COND_TESTS:
        raise wl.ConfigError(f"extract.test: one of {', '.join(COND_TESTS)}")
    out.update({"from": src, "test": test})
    if src == "css":
        parse_selector(x.get("selector"))
        out["selector"] = x["selector"].strip()
    if test == "in_stock" and src != "ld_product":
        raise wl.ConfigError("in_stock needs from=ld_product")
    if test in ("contains", "absent"):
        v = x.get("value")
        if not isinstance(v, str) or not v.strip() or len(v) > 100:
            raise wl.ConfigError("extract.value: the text to look for")
        out["value"] = v.strip()
    if test in ("below", "above"):
        v = x.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise wl.ConfigError("extract.value: a number")
        out["value"] = v
    return out


# --- rendering -------------------------------------------------------------------------

RENDER_HOME = os.path.join(wl.HERMES_HOME, "delve-render")
RENDER_LOCK_WAIT_S = 120
RENDER_TIMEOUT_S = 90


class Renderer:
    """Runs render_page.py in its own venv (Playwright + the system Chromium), one at a
    time on this machine."""

    def __init__(self, python: str | None = None, script: str | None = None,
                 lock_path: str | None = None) -> None:
        self.python = python or os.path.join(RENDER_HOME, "venv", "bin", "python")
        self.script = script or os.path.join(wl.HERE, "render_page.py")
        self.lock_path = lock_path or os.path.join(wl.WATCH_DIR, ".render.lock")

    def available(self) -> bool:
        return os.access(self.python, os.X_OK) and os.path.isfile(self.script)

    def render(self, url: str, wait_for: str | None = None, capture_json: bool = False) -> dict:
        if not self.available():
            raise wl.FetchError("the page renderer is not installed", "render_unavailable")
        cmd = [self.python, "-I", self.script, url]
        if wait_for:
            cmd += ["--wait-for", wait_for]
        if capture_json:
            cmd.append("--capture-json")
        env = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "HERMES_HOME": wl.HERMES_HOME}
        for k in ("DELVE_CHROMIUM", "DELVE_RENDER_TEST_HOSTS"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        os.makedirs(os.path.dirname(self.lock_path), mode=0o700, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + RENDER_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise wl.FetchError("another page is being rendered", "render_busy") from None
                    time.sleep(0.5)
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=RENDER_TIMEOUT_S, env=env)
            except subprocess.TimeoutExpired:
                raise wl.FetchError("rendering took too long", "render_timeout") from None
        finally:
            os.close(fd)
        try:
            out = json.loads(r.stdout.decode("utf-8", "replace"))
        except ValueError:
            tail = r.stderr.decode("utf-8", "replace")[-300:]
            raise wl.FetchError(f"renderer gave no answer: {tail}", "render_failed") from None
        if not out.get("ok"):
            code = str(out.get("code") or "render_failed")
            err = str(out.get("error") or "render failed")
            if code == "blocked_url":
                raise wl.BlockedURL(err)
            raise wl.FetchError(err, code if re.fullmatch(r"[a-z0-9_]{1,40}", code) else "render_failed")
        return out


# --- robots.txt --------------------------------------------------------------------------


def robots_check(url: str, fetcher: Any, base: str | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """RFC 9309 with a 24 h cache per host: 4xx robots.txt = no rules; 5xx or unreachable =
    everything disallowed (an older cached copy is used if there is one)."""
    now = now or time.time()
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    rurl = f"{p.scheme}://{p.netloc}/robots.txt"
    cache_dir = os.path.join(base or wl.WATCH_DIR, ".robots")
    cpath = os.path.join(cache_dir, re.sub(r"[^a-z0-9.-]", "_", f"{host}_{p.port or ''}") + ".json")
    cached = None
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    entry = cached if cached and now - cached.get("at", 0) < ROBOTS_MAX_AGE_S else None
    if entry is None:
        try:
            page = fetcher.get(rurl, accept="text/plain,*/*;q=0.5")
            entry = {"at": now, "kind": "rules", "text": page.text[:500_000]}
        except wl.BlockedURL:
            raise
        except wl.WatchError as exc:
            if exc.code.startswith("http_4"):
                entry = {"at": now, "kind": "none", "text": ""}
            elif cached:
                entry = cached
            else:
                entry = {"at": now, "kind": "unreachable", "text": "", "error": str(exc)[:160]}
        if entry["kind"] != "unreachable":
            try:
                os.makedirs(cache_dir, mode=0o700, exist_ok=True)
                wl._write_json(cpath, entry)
            except OSError:
                pass
    if entry["kind"] == "none":
        return {"allowed": True, "robots": "none"}
    if entry["kind"] == "unreachable":
        return {"allowed": False, "robots": "unreachable", "detail": entry.get("error")}
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(entry["text"].splitlines())
    return {"allowed": rp.can_fetch(ROBOTS_TOKEN, url), "robots": "rules",
            "crawl_delay": rp.crawl_delay(ROBOTS_TOKEN)}


def require_robots(url: str, fetcher: Any, base: str | None = None) -> None:
    v = robots_check(url, fetcher, base)
    if not v["allowed"]:
        why = ("the site's robots.txt cannot be read" if v["robots"] == "unreachable"
               else "the site's robots.txt does not allow automated reading of this page")
        raise wl.FetchError(f"{why}; try a category page or another source", "robots_disallowed")


# --- loading and extracting -------------------------------------------------------------


class Loaded:
    def __init__(self, page: wl.Page, json_responses: list[dict] | None = None,
                 rendered: bool = False) -> None:
        self.page = page
        self.json_responses = json_responses or []
        self.rendered = rendered
        self._root: wl.Node | None = None

    @property
    def root(self) -> wl.Node:
        if self._root is None:
            self._root = wl.parse_html(self.page.text)
        return self._root


def load(url: str, fetcher: Any, render: bool, renderer: Renderer | None,
         wait_for: str | None = None, capture_json: bool = False) -> Loaded:
    if not render:
        return Loaded(fetcher.get(url))
    r = (renderer or Renderer()).render(url, wait_for=wait_for, capture_json=capture_json)
    status = int(r.get("status") or 200)
    if status >= 400:
        raise wl.FetchError(f"HTTP {status} for {url} (in the browser)", f"http_{status}")
    page = wl.Page(url, r.get("final_url") or url, int(r.get("status") or 200), "text/html",
                   r.get("html") or "")
    return Loaded(page, r.get("json") or [], rendered=True)


def scripts_json(root: wl.Node) -> dict[str, Any]:
    """{"next_data": obj, "ld_json": [blocks], "script:<id>": obj}"""
    out: dict[str, Any] = {"ld_json": []}
    for s in root.find_all("script"):
        typ = (s.attrs.get("type") or "").lower()
        raw = "".join(c for c in s.children if isinstance(c, str)).strip()
        if not raw:
            continue
        try:
            if typ == "application/ld+json":
                out["ld_json"].append(json.loads(raw))
            elif s.attrs.get("id") == "__NEXT_DATA__":
                out["next_data"] = json.loads(raw)
            elif typ == "application/json" and s.attrs.get("id"):
                out["script:" + s.attrs["id"]] = json.loads(raw)
        except ValueError:
            continue
    return out


def _item(title: str | None, link: str | None, price: str | None, location: str | None,
          posted: str | None, text: str | None, ident: str | None, base: str) -> dict[str, Any] | None:
    if not title:
        return None
    url = urllib.parse.urljoin(base, link) if link else base
    if not url.startswith(("http://", "https://")):
        url = base
    key = ident or (link and url) or title
    pv = price if price and re.fullmatch(r"\d+(\.\d+)?", price) else None
    return {"item_id": "rc:" + wl.short_id(str(key)), "title": wl._clip(title, 300),
            "url": url[:1000], "price": wl._clip(price, 60) if price else None,
            "price_value": int(float(pv)) if pv else wl.price_value(price),
            "location": wl._clip(location, 120), "posted": wl._clip(posted, 60),
            "text": wl._clip(text, wl.EXCERPT_CHARS)}


def extract_list(root: wl.Node, base: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = select(root, spec["item"])
    f = spec.get("fields", {})
    out, seen = [], set()
    for n in nodes[:MAX_ITEMS]:
        link = field_value(n, f["link"], base) if "link" in f else _first_link(n, base)
        it = _item(field_value(n, f["title"], base),
                   link,
                   field_value(n, f["price"], base) if "price" in f else _price_in(n.text()),
                   field_value(n, f["location"], base) if "location" in f else None,
                   field_value(n, f["posted"], base) if "posted" in f else None,
                   field_value(n, f["text"], base) if "text" in f else n.text(),
                   field_value(n, f["id"], base) if "id" in f else None, base)
        if it and it["item_id"] not in seen:
            seen.add(it["item_id"])
            out.append(it)
    return out


def _first_link(n: wl.Node, base: str) -> str | None:
    if n.tag == "a" and n.attrs.get("href"):
        return urllib.parse.urljoin(base, n.attrs["href"])
    a = n.find("a", attr="href")
    return urllib.parse.urljoin(base, a.attrs["href"]) if a is not None else None


def _price_in(text: str) -> str | None:
    m = PRICE_RE.search(text or "")
    return m.group(0).strip() if m else None


def api_response(responses: list[dict], api_url: str) -> str | None:
    """The body of the captured response for api_url (exact, else the same path)."""
    bodies = [r for r in responses if r.get("body")]
    for r in bodies:
        if r["url"] == api_url:
            return r["body"]
    path = api_url.split("?", 1)[0]
    for r in bodies:
        if r["url"].split("?", 1)[0] == path:
            return r["body"]
    return None


def json_source(loaded: Loaded, spec: dict[str, Any], fetcher: Any) -> Any:
    src = spec["from"]
    if src == "page":
        return loaded.page.json()
    if src == "api":
        return fetcher.get(spec["api_url"], accept="application/json").json()
    found = scripts_json(loaded.root)
    if src == "next_data":
        if "next_data" not in found:
            raise RecipeError("the page has no __NEXT_DATA__ any more")
        return found["next_data"]
    if src == "ld_json":
        return found["ld_json"]
    key = "script:" + spec["script_id"]
    if key not in found:
        raise RecipeError(f"no <script id={spec['script_id']}> JSON on the page any more")
    return found[key]


def extract_json(data: Any, base: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = json_items(data, spec["items"])
    if rows is None:
        raise RecipeError(f"the JSON has no list at {spec['items']} any more")
    f = spec.get("fields", {})

    def get(row: Any, name: str) -> str | None:
        return _scalar(json_get(row, parse_path(f[name]))) if name in f else None

    out, seen = [], set()
    for row in rows[:MAX_ITEMS]:
        it = _item(get(row, "title"), get(row, "link"), get(row, "price"), get(row, "location"),
                   get(row, "posted"), get(row, "text"), get(row, "id"), base)
        if it and it["item_id"] not in seen:
            seen.add(it["item_id"])
            out.append(it)
    return out


def ld_product(blocks: list) -> dict[str, Any] | None:
    stack = list(blocks)
    while stack:
        o = stack.pop(0)
        if isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                offers = o.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                offers = offers if isinstance(offers, dict) else {}
                price = offers.get("price", offers.get("lowPrice"))
                try:
                    pv = float(price) if price not in (None, "") else None
                except (TypeError, ValueError):
                    pv = wl.price_value(str(price))
                avail = str(offers.get("availability") or "")
                return {"name": str(o.get("name") or ""), "price": pv,
                        "currency": offers.get("priceCurrency"),
                        "availability": avail.rsplit("/", 1)[-1] if avail else None}
            if "@graph" in o:
                stack.append(o["@graph"])
    return None


def evaluate_condition(loaded: Loaded, spec: dict[str, Any]) -> tuple[bool, str]:
    """(true?, what was seen)."""
    src, test = spec["from"], spec["test"]
    number: float | None = None
    if src == "ld_product":
        prod = ld_product(scripts_json(loaded.root)["ld_json"])
        if prod is None:
            raise RecipeError("the page has no schema.org Product data any more")
        price = f"{prod['price']:g} {prod['currency'] or ''}".strip() if prod["price"] is not None else "?"
        seen = f"{prod['name']} · {prod['availability'] or '?'} · {price}"
        if test == "in_stock":
            return (prod["availability"] or "").lower() in IN_STOCK, seen
        number = prod["price"]
        text = seen
    elif src == "css":
        nodes = select(loaded.root, spec["selector"])
        text = nodes[0].text() if nodes else ""
        seen = text[:200] or "(not on the page)"
    else:
        text = " ".join(wl.readable_lines(loaded.root))
        seen = ""
    if test in ("contains", "absent"):
        hit = wl.fold(spec["value"]) in wl.fold(text)
        if not seen:
            seen = _around(text, spec["value"]) if hit else f"no “{spec['value']}”"
        return (hit if test == "contains" else not hit), seen
    if number is None:
        number = wl.price_value(text)
        if number is None:
            raise RecipeError("no number on the page where the recipe looks")
    return (number < spec["value"] if test == "below" else number > spec["value"]), seen or str(number)


def _around(text: str, word: str) -> str:
    i = wl.fold(text).find(wl.fold(word))
    return text[max(0, i - 60): i + len(word) + 60].strip() if i >= 0 else ""


def collect(cfg: dict[str, Any], fetcher: Any, renderer: Renderer | None = None,
            state: dict[str, Any] | None = None, base_dir: str | None = None
            ) -> tuple[list[dict[str, Any]], wl.Page]:
    """The items of a 'page' watcher with a recipe and/or rendering. For a condition the
    item exists while the condition holds; each false -> true turn is a new item."""
    spec = cfg.get("extract") or {}
    url = cfg["url"]
    require_robots(url, fetcher, base_dir)
    if spec.get("mode") == "json" and spec.get("from") == "api":
        require_robots(spec["api_url"], fetcher, base_dir)
        if not cfg.get("render"):
            page = fetcher.get(spec["api_url"], accept="application/json")
            return extract_json(page.json(), url, spec), page
        # the API refuses plain HTTP: load the page and take the answer the browser got
        loaded = load(url, fetcher, True, renderer, spec.get("wait_for"), capture_json=True)
        body = api_response(loaded.json_responses, spec["api_url"])
        if body is None:
            raise RecipeError("the page no longer calls that API")
        try:
            data = json.loads(body)
        except ValueError:
            raise RecipeError("the API answer is not JSON any more") from None
        return extract_json(data, loaded.page.final_url, spec), loaded.page
    loaded = load(url, fetcher, bool(cfg.get("render")), renderer, spec.get("wait_for"))
    base = loaded.page.final_url
    if not spec:
        items = [{"item_id": "pg:" + wl.short_id(u), "title": wl._clip(t, 300), "url": u}
                 for t, u in wl.links(loaded.root, base)]
        return items, loaded.page
    if spec["mode"] == "list":
        return extract_list(loaded.root, base, spec), loaded.page
    if spec["mode"] == "json":
        return extract_json(json_source(loaded, spec, fetcher), base, spec), loaded.page
    hit, seen = evaluate_condition(loaded, spec)
    cond = (state or {}).get("cond") or {"true": False, "episode": 0}
    if hit and not cond.get("true"):
        cond = {"true": True, "episode": int(cond.get("episode", 0)) + 1}
    elif not hit:
        cond = {"true": False, "episode": int(cond.get("episode", 0))}
    if state is not None:
        state["cond"] = cond
    items = []
    if hit:
        title = wl.page_title(loaded.root) or cfg.get("name") or url
        items.append({"item_id": f"cd:{cond['episode']}", "title": wl._clip(f"{cfg.get('name') or title}: {seen}", 300),
                      "url": base, "text": wl._clip(seen, wl.EXCERPT_CHARS),
                      "price_value": None})
    return items, loaded.page


# --- structure: candidates for the model ---------------------------------------------------


def _clean_classes(n: wl.Node) -> list[str]:
    return sorted(c for c in n.classes() if not generated(c) and re.fullmatch(r"[\w-]+", c))[:3]


def _sig(n: wl.Node) -> str:
    return n.tag + "".join("." + c for c in _clean_classes(n))


def _own(n: wl.Node) -> str:
    i = n.attrs.get("id")
    if i and re.fullmatch(r"[A-Za-z][\w-]*", i) and not generated(i):
        return "#" + i
    return _sig(n)


def _unique(root: wl.Node, parent: wl.Node, child: str, count: int) -> str:
    chain, p, best = [], parent, None
    for _ in range(5):
        if p is None or p.tag == "#root":
            break
        chain.insert(0, _own(p))
        sel = " ".join(chain) + " > " + child
        n = len(select(root, sel))
        if best is None or abs(n - count) < best[0]:
            best = (abs(n - count), sel)
        if n == count or chain[0].startswith("#"):
            break
        p = p.parent
    return best[1] if best else child


def _first(item: wl.Node, sel: str) -> wl.Node | None:
    try:
        found = select(item, sel)
    except wl.ConfigError:
        return None
    return found[0] if found else None


def _rel(item: wl.Node, node: wl.Node) -> str | None:
    """A selector whose first match inside `item` is `node`: its signature, or an ancestor's
    signature + its own; an ancestor with 'price' in its class is preferred for prices."""
    tries = [_sig(node)]
    a = node.parent
    while a is not None and a is not item:
        tries.append(f"{_sig(a)} {_sig(node)}")
        tries.append(f"{_sig(a)} {node.tag}")
        a = a.parent
    for t in tries:
        if _first(item, t) is node:
            return t
    return None


def _price_node(m: wl.Node) -> wl.Node | None:
    """The deepest element whose text starts with a price (or holds just one)."""
    best = None
    for n in m.iter():
        if n is m:
            continue
        t = n.text()
        pm = PRICE_RE.search(t)
        if pm and len(t) <= len(pm.group(0)) + 25:
            best = n
    if best is not None:            # prefer a wrapper called "...price..."
        a = best
        while a is not None and a is not m:
            if any("price" in c.lower() or "ar" == c.lower() for c in a.classes()) \
                    and len(a.text()) <= 60:
                return a
            a = a.parent
    return best


PLACE_WORDS = {"location", "locations", "city", "cities", "place", "venue", "address", "region"}


def _common(values: list[str | None], share: float = 0.6) -> str | None:
    vals = [v for v in values if v]
    if not vals:
        return None
    best = max(set(vals), key=vals.count)
    return best if vals.count(best) >= share * len(values) else None


def _guess_list_spec(members: list[wl.Node], base: str) -> dict[str, str]:
    titles, prices, links, places = [], [], [], []
    for m in members:
        head = next((h for h in m.iter() if h is not m and h.tag in ("h1", "h2", "h3", "h4", "h5", "h6")
                     and h.text()), None)
        if head is not None:
            titles.append(_rel(m, head))
            a = head.find("a", attr="href") or (head if head.tag == "a" else None)
            if a is None and head.parent is not None and head.parent.tag == "a":
                a = head.parent
            links.append((_rel(m, a) + "@href") if a is not None and _rel(m, a) else None)
        else:
            anchors = [a for a in m.find_all("a", attr="href") if a.text()]
            best = max(anchors, key=lambda a: len(a.text())) if anchors else None
            titles.append(_rel(m, best) if best is not None else None)
            links.append((_rel(m, best) + "@href") if best is not None and _rel(m, best) else None)
        pn = _price_node(m)
        prices.append(_rel(m, pn) if pn is not None else None)
        loc = next((n for n in m.iter() if n is not m and any(
            w in PLACE_WORDS for c in n.classes() for w in re.split(r"[-_]+", c.lower()))
            and n.text() and len(n.text()) < 80), None)
        places.append(_rel(m, loc) if loc is not None else None)
    spec: dict[str, str] = {"title": _common(titles) or "a"}
    for name, vals in (("link", links), ("price", prices), ("location", places)):
        c = _common(vals, 0.4)
        if c:
            spec[name] = c
    spec["text"] = ""
    return spec


def _groups(parent: wl.Node) -> list[tuple[str, list[wl.Node]]]:
    """Repeated children of `parent`: per tag, the classes most of them share."""
    out = []
    by_tag: dict[str, list[wl.Node]] = {}
    for c in parent.children:
        if isinstance(c, wl.Node) and c.tag not in ("script", "style", "template", "br", "hr",
                                                    "option", "meta", "link", "noscript"):
            by_tag.setdefault(c.tag, []).append(c)
    for tag, kids in by_tag.items():
        if len(kids) < 3:
            continue
        freq: dict[str, int] = {}
        for k in kids:
            for c in _clean_classes_all(k):
                freq[c] = freq.get(c, 0) + 1
        common = sorted((c for c, n in freq.items() if n >= 0.6 * len(kids)),
                        key=lambda c: (-freq[c], c))[:2]
        members = [k for k in kids if set(common) <= k.classes()]
        if len(members) >= 3:
            out.append((tag + "".join("." + c for c in sorted(common)), members))
    return out


def _clean_classes_all(n: wl.Node) -> list[str]:
    return [c for c in n.classes() if not generated(c) and re.fullmatch(r"[\w-]+", c)]


def quality(items: list[dict[str, Any]], base: str) -> tuple[float, dict[str, float]]:
    """How useful a candidate's items are: count (capped) x what they carry."""
    if not items:
        return 0.0, {}
    n = len(items)
    r = {"url": sum(1 for i in items if i.get("url") and i["url"] != base) / n,
         "price": sum(1 for i in items if i.get("price_value") is not None) / n,
         "text": sum(1 for i in items if i.get("text") and len(i["text"]) > len(i.get("title") or "") + 20) / n,
         "unique_titles": len({i.get("title") for i in items}) / n}
    q = min(n, 30) * (1 + r["price"] + 0.5 * r["url"] + 0.5 * r["text"]) * r["unique_titles"]
    if r["text"] < 0.3 and r["price"] < 0.3:
        q *= 0.35                # bare links (menus, footers, tag clouds) carry nothing to watch
    return round(q, 1), r


def block_candidates(root: wl.Node, base: str, limit: int = 3) -> list[dict[str, Any]]:
    found = []
    for parent in root.iter():
        for sig, members in _groups(parent):
            with_link = [m for m in members if _first_link(m, base)]
            if len(with_link) < 0.6 * len(members):
                continue
            if sum(len(m.text()) for m in members) / len(members) < 15:
                continue
            spec = {"mode": "list", "item": _unique(root, parent, sig, len(members)),
                    "fields": _guess_list_spec(members, base)}
            try:
                items = extract_list(root, base, spec)
            except wl.ConfigError:
                continue
            if len(items) < 3:
                continue
            q, r = quality(items, base)
            found.append({"kind": "list", "score": q, "extract": spec, "items": len(items),
                          "with_price": sum(1 for i in items if i.get("price_value") is not None),
                          "sample": items[:3], "_nodes": select(root, spec["item"])})
    raw = {id(c): c["score"] for c in found}
    for b in found:            # a list inside the items of a good list is a detail of them
        for a in found:
            if a is b or not b["_nodes"] or raw[id(a)] < 0.25 * raw[id(b)]:
                continue
            outer = {id(n) for n in a["_nodes"]}
            inside = 0
            for n in b["_nodes"]:
                p = n.parent
                while p is not None and id(p) not in outer:
                    p = p.parent
                inside += p is not None
            if inside >= 0.8 * len(b["_nodes"]):
                b["score"] = round(b["score"] * 0.15, 1)
                break
    for c in found:
        c.pop("_nodes")
    found.sort(key=lambda c: -c["score"])
    out, seen = [], set()
    for c in found:
        if c["extract"]["item"] in seen:
            continue
        seen.add(c["extract"]["item"])
        out.append(c)
        if len(out) >= limit:
            break
    return out


TITLE_KEYS = ("name", "title", "subject", "headline", "label", "productName", "displayName")
URL_KEYS = ("html_url", "url", "link", "href", "permalink", "uri", "slug", "path")
PRICE_KEYS = ("price", "lowPrice", "amount", "priceValue", "currentPrice", "salesPrice", "regularPrice")
TEXT_KEYS = ("body", "description", "summary", "content", "text", "excerpt")
PLACE_KEYS = ("location", "city", "address", "place", "venue", "region")
DATE_KEYS = ("published_at", "list_time", "date", "startDate", "created_at", "createdAt",
             "publishedAt", "datePublished", "time")
ID_KEYS = ("id", "list_id", "uid", "sku", "productId", "item_id")


def _key(d: dict, keys: tuple, ok=lambda v: True) -> str | None:
    for k in keys:
        if k in d and _scalar(d[k]) is not None and ok(d[k]):
            return k
    for k, v in d.items():
        if isinstance(v, dict):
            for kk in keys:
                if kk in v and _scalar(v[kk]) is not None and ok(v[kk]):
                    return f"{k}.{kk}"
    return None


def _guess_json_fields(d: dict) -> dict[str, str]:
    f: dict[str, str] = {}
    for name, keys, ok in (("title", TITLE_KEYS, lambda v: isinstance(v, str)),
                           ("link", URL_KEYS, lambda v: isinstance(v, str)),
                           ("price", PRICE_KEYS, lambda v: True),
                           ("text", TEXT_KEYS, lambda v: isinstance(v, str)),
                           ("location", PLACE_KEYS, lambda v: True),
                           ("posted", DATE_KEYS, lambda v: True),
                           ("id", ID_KEYS, lambda v: not isinstance(v, (dict, list)))):
        k = _key(d, keys, ok)
        if k:
            f[name] = k        # {"label": "550 000 Ft", "value": 550000} reads as its label
    return f


def json_candidates(obj: Any, path: str = "$", depth: int = 0, out: list | None = None) -> list:
    out = [] if out is None else out
    if depth > 9 or len(out) > 30:
        return out
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 2 and len(dicts) >= 0.8 * len(obj):
            fields = _guess_json_fields(dicts[0])
            if "title" in fields:
                out.append({"items": path + "[*]", "count": len(dicts), "fields": fields})
        for i, x in enumerate(obj[:3]):
            json_candidates(x, f"{path}[{i}]", depth + 1, out)
    elif isinstance(obj, dict):
        for k, v in list(obj.items())[:200]:
            if isinstance(v, (list, dict)) and re.fullmatch(r"[^.\[\]']+", k):
                json_candidates(v, f"{path}.{k}", depth + 1, out)
    return out


def _json_entries(loaded: Loaded) -> list[tuple[dict[str, Any], Any]]:
    """(partial extract spec, data) for every JSON the page offers."""
    entries: list[tuple[dict[str, Any], Any]] = []
    if "json" in loaded.page.content_type:
        try:
            entries.append(({"from": "page"}, loaded.page.json()))
        except wl.WatchError:
            pass
        return entries
    found = scripts_json(loaded.root)
    if "next_data" in found:
        entries.append(({"from": "next_data"}, found["next_data"]))
    if found["ld_json"]:
        entries.append(({"from": "ld_json"}, found["ld_json"]))
    for k, v in found.items():
        if k.startswith("script:"):
            entries.append(({"from": "script", "script_id": k[7:]}, v))
    for r in loaded.json_responses:
        if r.get("body"):
            try:
                entries.append(({"from": "api", "api_url": r["url"]}, json.loads(r["body"])))
            except ValueError:
                pass
    return entries


def json_list_candidates(loaded: Loaded, fetcher: Any, limit: int = 3) -> list[dict[str, Any]]:
    out = []
    base = loaded.page.final_url
    for partial, data in _json_entries(loaded):
        for c in json_candidates(data):
            spec = {"mode": "json", **partial, "items": c["items"], "fields": c["fields"]}
            try:
                items = extract_json(data, base, spec)
            except wl.WatchError:
                continue
            if len(items) < 2:
                continue
            q, _r = quality(items, base)
            head = c["items"].rsplit("[*]", 1)[0]
            if re.search(r"\[\d+\]", head.replace("$[0]", "$", 1) if partial["from"] == "ld_json" else head):
                q *= 0.2         # a sub-list of one element (e.g. one release's assets)
            out.append({"kind": "json", "score": round(q * 1.1, 1),     # JSON outlives CSS classes
                        "extract": spec, "items": len(items),
                        "with_price": sum(1 for i in items if i.get("price_value") is not None),
                        "sample": items[:3]})
    out.sort(key=lambda c: -c["score"])
    seen, uniq = set(), []
    for c in out:
        key = (c["extract"].get("from"), c["extract"].get("api_url"), c["extract"]["items"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq[:limit]


def condition_candidates(loaded: Loaded) -> list[dict[str, Any]]:
    prod = ld_product(scripts_json(loaded.root)["ld_json"])
    if prod is None:
        return []
    out = []
    if prod["availability"]:
        out.append({"kind": "condition", "extract": {"mode": "condition", "from": "ld_product",
                                                     "test": "in_stock"},
                    "now": f"{prod['name']}: {prod['availability']}"})
    if prod["price"] is not None:
        out.append({"kind": "condition", "extract": {"mode": "condition", "from": "ld_product",
                                                     "test": "below", "value": "<a price>"},
                    "now": f"{prod['name']}: {prod['price']:g} {prod['currency'] or ''}".strip()})
    return out


def looks_like_shell(loaded: Loaded) -> bool:
    root = loaded.root
    body = root.find("body") or root
    scripts = root.find_all("script")
    sbytes = sum(len("".join(c for c in s.children if isinstance(c, str))) for s in scripts)
    return len(body.text()) < 1200 and (sbytes > 0.3 * len(loaded.page.text) or len(scripts) >= 3)


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    keep = {"title": 90, "url": 160, "price": 40, "location": 60, "posted": 40, "text": 120}
    return {k: wl._clip(str(item[k]), n) for k, n in keep.items() if item.get(k)}


def structure_cache_path(url: str, base: str | None = None) -> str:
    return os.path.join(base or wl.WATCH_DIR, ".structure",
                        hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".json")


def structure(url: str, fetcher: Any, renderer: Renderer | None = None, render: bool | None = None,
              base: str | None = None) -> dict[str, Any]:
    """What a watcher could read on this page: numbered candidates (list, JSON, condition),
    each with a ready recipe and real sample items. Saved for 2 h so watch_manage can take
    one by number."""
    renderer = renderer or Renderer()
    out: dict[str, Any] = {"url": url}
    try:
        robots = robots_check(url, fetcher, base)
    except wl.BlockedURL:
        raise
    out["robots"] = "allowed" if robots["allowed"] else (
        "unreachable" if robots["robots"] == "unreachable" else "disallowed")
    loaded, why = None, None
    if render is not True:
        try:
            loaded = Loaded(fetcher.get(url))
            if "json" not in loaded.page.content_type and looks_like_shell(loaded):
                why = "the page is built by JavaScript"
        except wl.BlockedURL:
            raise
        except wl.FetchError as exc:
            if render is False:
                raise
            why = f"plain fetch failed ({exc.code})"
    if render is True or (render is None and why and renderer.available()):
        loaded = load(url, fetcher, True, renderer, capture_json=True)
    if loaded is None:
        raise wl.FetchError(why or "fetch failed", "fetch_failed")
    out.update({"final_url": loaded.page.final_url, "rendered": loaded.rendered,
                "title": wl.page_title(loaded.root)[:160] if "json" not in loaded.page.content_type else None})
    if why and not loaded.rendered:
        out["note"] = f"{why}; the renderer is not installed, so this may be incomplete"
    lists = json_list_candidates(loaded, fetcher)
    conds: list[dict[str, Any]] = []
    if "json" not in loaded.page.content_type:
        lists += block_candidates(loaded.root, loaded.page.final_url)
        conds = condition_candidates(loaded)
    lists = sorted(lists, key=lambda c: -c["score"])[:4]
    cands = conds + lists if conds else lists      # a product page: its stock/price first
    plain_ok: dict[str, bool] = {}
    numbered = []
    for i, c in enumerate(cands, 1):
        c = {"candidate": i, **c}
        api = c["extract"].get("api_url") if c["extract"].get("from") == "api" else None
        if api and api != url and api not in plain_ok:
            try:                        # can a watcher read the API without the browser?
                fetcher.get(api, accept="application/json").json()
                plain_ok[api] = True
            except wl.BlockedURL:
                raise
            except wl.WatchError:
                plain_ok[api] = False
        if loaded.rendered and not (api and plain_ok.get(api, api == url and not why)):
            c["render"] = True
        numbered.append(c)
    try:
        wl._write_json(structure_cache_path(url, base),
                       {"at": time.time(), "url": url,
                        "candidates": [{"candidate": c["candidate"], "extract": c["extract"],
                                        "render": bool(c.get("render"))} for c in numbered]})
    except OSError:
        pass
    out["candidates"] = [{**{k: v for k, v in c.items() if k not in ("sample", "score")},
                          "sample": [_slim(s) for s in c.get("sample", [])]} for c in numbered]
    if not numbered:
        out["text_head"] = wl.readable_lines(loaded.root)[:20] if "json" not in loaded.page.content_type else None
        out["note"] = (out.get("note", "") + " No repeated items found; a condition on the page "
                       "text (from page_text / css) may still work.").strip()
    return out


def candidate_from_cache(url: str, number: int, base: str | None = None) -> dict[str, Any]:
    try:
        with open(structure_cache_path(url, base), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise wl.ConfigError("no recent page_fetch mode=structure for this url; run it first") from None
    if time.time() - data.get("at", 0) > STRUCTURE_MAX_AGE_S:
        raise wl.ConfigError("the structure reading is older than 2 h; run page_fetch mode=structure again")
    for c in data.get("candidates", []):
        if c.get("candidate") == number:
            return c
    raise wl.ConfigError(f"no candidate {number}; see the page_fetch structure answer")


# --- the preview gate --------------------------------------------------------------------

PREVIEW_KEYS = ("source", "url", "include", "any", "exclude", "min_price", "max_price",
                "extract", "render")


def preview_key(cfg: dict[str, Any]) -> str:
    return wl.short_id(json.dumps({k: cfg.get(k) for k in PREVIEW_KEYS}, sort_keys=True,
                                  ensure_ascii=False))


def _previews_path(base: str | None) -> str:
    return os.path.join(base or wl.WATCH_DIR, ".previews.json")


def remember_preview(cfg: dict[str, Any], base: str | None = None, now: float | None = None) -> None:
    now = now or time.time()
    try:
        with open(_previews_path(base), encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    seen = {k: v for k, v in seen.items() if now - v < PREVIEW_MAX_AGE_S}
    seen[preview_key(cfg)] = now
    wl._write_json(_previews_path(base), seen)


def was_previewed(cfg: dict[str, Any], base: str | None = None, now: float | None = None) -> bool:
    now = now or time.time()
    try:
        with open(_previews_path(base), encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        return False
    t = seen.get(preview_key(cfg))
    return isinstance(t, (int, float)) and now - t < PREVIEW_MAX_AGE_S
