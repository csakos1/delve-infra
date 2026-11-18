"""newstext – the article texts of the stories the backend wants (Delve N1c).

Runs in the renderer's venv (Playwright + the system Chromium; trafilatura if installed),
started by ``newslib.texts_main`` under the renderer's lock, never inside the gateway:
    <venv>/bin/python -I newstext.py   < {"v": 1, "budget_s": n, "items": [{id, url, gn, try}]}
stdout, one JSON object a line, written as soon as it is known (a cut run keeps what was
done): {"item": {id, try, status, url, final_url, text, chars, via, ms, error}} ... and
last {"done": {run_s, browser_ms, at, timeout}}. stderr: one progress line an item.

The direct links go first, then the Google News links. A watchdog ends the run
WATCHDOG_S after the budget even if the browser hangs (live, 11-18: a Google News page
that kept reopening a WebSocket held the event loop; WebSockets are not routed any more).

- A Google News link is opened in a headless Chromium that stops at the first main-frame
  navigation off Google: the publisher's page is never loaded by it; its address is the
  result (the N1c fixture: 10/10, ~1.2 s each, one consent page per run - "Reject all").
  Every connection goes through render_page's proxy (public addresses only).
- The publisher's robots.txt is asked with the ``delve-news`` token (2xx = its rules,
  4xx = no rules, 5xx / unreachable = disallowed; cached a day; a redirect to another host
  is asked again).
- The page is fetched with plain HTTP (the collector's honest user agent, the
  public-address guard on every hop, the per-host spacing shared with the watchers); a
  401 / 403 / 429 is tried once more in the browser (the fixture: 1 of 2 helped).
- The text: trafilatura (precision first), the paragraphs of <article> / <main> as the
  fallback; under MIN_CHARS it is ``no_text`` (a script-built page, a bot check).

Statuses: ok, no_text, robots, unresolved (the Google News link led nowhere), blocked,
http_<code>, fetch_failed, too_large, render_failed. Items not started within the budget are
left out (asked again next run).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import sys
import time
import urllib.parse
import urllib.robotparser
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import newslib  # noqa: E402
import render_page  # noqa: E402  (playwright is imported only when a browser is needed)
import watchlib as wl  # noqa: E402

TOKEN = "delve-news"
MIN_CHARS = 500
TEXT_CHARS = 30_000
GN_WAIT_S = 25.0
ROBOTS_MAX_AGE_S = 24 * 3600
RENDER_STATUSES = (401, 403, 429)
WATCHDOG_S = 60.0
SKIP_TYPES = {"image", "media", "font"}
CONSENT_BUTTON = re.compile(
    r"reject all|összes elutasítása|alle ablehnen|tout refuser|rifiuta tutto|rechazar todo", re.I)
ROBOTS_DIR = os.path.join(newslib.STATE_DIR, ".robots")


def _test_hosts() -> dict[str, str]:
    """Tests only (render_page's own rule): made-up *.test names -> 127.0.0.1:port."""
    out: dict[str, str] = {}
    for part in filter(None, (os.environ.get("DELVE_RENDER_TEST_HOSTS") or "").split(",")):
        name, _, target = part.partition("=")
        host, _, port = target.rpartition(":")
        if name.endswith(".test") and host == "127.0.0.1" and port.isdigit():
            out[name.lower()] = target
    return out


def is_google(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in _test_hosts() and host.startswith(("news.", "consent.")):
        return True
    return (host == "google.com" or host.endswith(".google.com")
            or bool(re.match(r"^(www\.|consent\.|news\.)?google\.[a-z.]{2,6}$", host))
            or host.endswith((".gstatic.com", ".googleusercontent.com")))


# --- plain HTTP --------------------------------------------------------------------------


def get(url: str, accept: str) -> newslib.Response:
    """newslib.http_get; a *.test host of the tests goes to its loopback server."""
    tests = _test_hosts()
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if host in tests:
        r = newslib.http_get(p._replace(netloc=tests[host]).geturl(), accept=accept,
                             resolver=lambda h, port: ["93.184.216.34"], host_dir="")
        r.final_url = url if r.final_url.startswith("http://127.0.0.1") else r.final_url
        return r
    return newslib.http_get(url, accept=accept)


def robots(url: str, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    cpath = os.path.join(ROBOTS_DIR, re.sub(r"[^a-z0-9.-]", "_", f"{host}_{p.port or ''}") + ".json")
    entry = None
    try:
        with open(cpath, encoding="utf-8") as f:
            entry = json.load(f)
        if now - entry.get("at", 0) >= ROBOTS_MAX_AGE_S:
            entry = None
    except (OSError, ValueError):
        entry = None
    if entry is None:
        try:
            r = get(f"{p.scheme}://{p.netloc}/robots.txt", "text/plain,*/*;q=0.5")
            if 200 <= r.status < 300 and r.body is not None:
                entry = {"at": now, "kind": "rules",
                         "text": r.body[:500_000].decode("utf-8", "replace")}
            elif 400 <= r.status < 500:
                entry = {"at": now, "kind": "none", "text": ""}
            else:
                return {"allowed": False, "robots": "unreachable", "detail": f"HTTP {r.status}"}
        except wl.BlockedURL:
            raise
        except (wl.WatchError, newslib.NewsError) as exc:
            return {"allowed": False, "robots": "unreachable", "detail": str(exc)[:160]}
        try:
            os.makedirs(ROBOTS_DIR, mode=0o700, exist_ok=True)
            newslib._write_json(cpath, entry, 0o600)
        except OSError:
            pass
    if entry["kind"] == "none":
        return {"allowed": True, "robots": "none"}
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(entry["text"].splitlines())
    return {"allowed": rp.can_fetch(TOKEN, url), "robots": "rules"}


# --- the text ----------------------------------------------------------------------------


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w.-]+)", ctype or "", re.I)
    name = m.group(1) if m else None
    if not name:
        mm = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.I)
        name = mm.group(1).decode("ascii", "replace") if mm else "utf-8"
    try:
        return body.decode(name, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _tidy(text: str) -> str:
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n|\n", text)]
    return "\n\n".join(p for p in paras if p)[:TEXT_CHARS]


def extract(html: str, url: str) -> tuple[str, str]:
    """(text, how): trafilatura when it is there and finds enough, else the paragraphs."""
    best, how = "", "none"
    try:
        import trafilatura  # noqa: PLC0415 - optional; the renderer's venv has it
        got = trafilatura.extract(html, url=url, include_comments=False, include_tables=False,
                                  favor_precision=True) or ""
        best, how = _tidy(got), "trafilatura"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - a page it cannot read is a page without text
        pass
    if len(best) < MIN_CHARS:
        root = wl.parse_html(html)
        art = root.find("article") or root.find("main") or root.find("body") or root
        paras = [p.text() for p in art.find_all("p")]
        mine = _tidy("\n".join(p for p in paras if len(p) >= 60))
        if len(mine) > len(best):
            best, how = mine, "paragraphs"
    return best, how


# --- Google News -------------------------------------------------------------------------


def resolve_gn(urls: list[str], deadline: float) -> tuple[dict[str, dict[str, Any]], int]:
    """{gn url: {target | None, consent, error}} with one browser and one context (a consent
    answer is kept for the run); (results, launch ms)."""
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import sync_playwright

    out: dict[str, dict[str, Any]] = {}
    guard = render_page.Guard()
    srv, port = render_page.start_proxy(guard)
    launch_ms = 0
    try:
        with sync_playwright() as p:
            t0 = time.monotonic()
            browser = p.chromium.launch(
                executable_path=render_page.CHROMIUM, headless=True,
                chromium_sandbox=os.geteuid() != 0,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--disable-gpu", "--no-first-run", "--no-default-browser-check",
                      "--disable-extensions", "--disable-sync", "--mute-audio",
                      "--disable-background-networking", "--disable-component-update",
                      "--disable-domain-reliability", "--metrics-recording-only"])
            launch_ms = int((time.monotonic() - t0) * 1000)
            try:
                ua = re.sub(r"HeadlessChrome", "Chrome", browser.new_page().evaluate(
                    "navigator.userAgent")) + " DelveNews/0.1 (personal news reader)"
                ctx = browser.new_context(user_agent=ua, locale="hu-HU", service_workers="block",
                                          accept_downloads=False)
                for url in urls:
                    if time.monotonic() > deadline:
                        break
                    out[url] = _one_gn(ctx, url, PwError)
            finally:
                browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
    return out, launch_ms


def _one_gn(ctx: Any, url: str, PwError: Any) -> dict[str, Any]:
    page = ctx.new_page()
    hit: dict[str, str] = {}

    def on_route(route: Any, request: Any) -> None:
        try:
            if request.resource_type in SKIP_TYPES:
                return route.abort("blockedbyclient")
            if request.is_navigation_request() and request.frame == page.main_frame:
                host = urllib.parse.urlsplit(request.url).hostname or ""
                if not is_google(host):
                    hit.setdefault("target", request.url)
                    return route.abort("aborted")
            return route.continue_()
        except PwError:
            return None

    page.route("**/*", on_route)  # WebSockets are not routed: see the module's doc
    res: dict[str, Any] = {"consent": False}
    clicked = False
    try:
        try:
            page.goto(url, wait_until="commit", timeout=20_000)
        except PwError as exc:
            if "target" not in hit:
                res["error"] = str(exc).splitlines()[0][:160]
        end = time.monotonic() + GN_WAIT_S
        while "target" not in hit and "error" not in res and time.monotonic() < end:
            page.wait_for_timeout(250)
            host = (urllib.parse.urlsplit(page.url).hostname or "").lower()
            if host.startswith("consent."):
                res["consent"] = True
                if not clicked:
                    clicked = True
                    try:
                        page.get_by_role("button", name=CONSENT_BUTTON).first.click(timeout=4000)
                    except PwError as exc:
                        res["error"] = "consent: " + str(exc).splitlines()[0][:140]
    finally:
        page.close()
    target = hit.get("target")
    if target:
        try:
            wl.check_url(target, (lambda h, p: ["93.184.216.34"])
                         if (urllib.parse.urlsplit(target).hostname or "") in _test_hosts()
                         else wl._resolve)
        except wl.WatchError as exc:
            return {"target": None, "consent": res["consent"], "error": f"blocked: {exc}"[:160]}
    res["target"] = target
    if not target and "error" not in res:
        res["error"] = "no way to the publisher"
    return res


# --- one article ---------------------------------------------------------------------------


def article(url: str) -> dict[str, Any]:
    """robots.txt, plain HTTP, the browser for a refusal, the text."""
    out: dict[str, Any] = {"final_url": url, "via": None, "text": None, "chars": 0}
    try:
        rb = robots(url)
        if not rb["allowed"]:
            return dict(out, status="robots", error=f"robots.txt: {rb['robots']}")
        r = get(url, newslib.ACCEPT_HTML)
        final = r.final_url or url
        out["final_url"] = final
        if urllib.parse.urlsplit(final).hostname != urllib.parse.urlsplit(url).hostname \
                and not robots(final)["allowed"]:
            return dict(out, status="robots", error="robots.txt of the redirect's host")
        if r.status == 200 and r.body is not None:
            text, how = extract(_decode(r.body, r.headers.get("content-type", "")), final)
            out["via"] = f"http/{how}"
        elif r.status in RENDER_STATUSES:
            text, out["via"], err = _rendered(url)
            if err:
                return dict(out, status="render_failed", error=err)
        else:
            return dict(out, status=f"http_{r.status}", error=f"HTTP {r.status}")
    except wl.BlockedURL as exc:
        return dict(out, status="blocked", error=str(exc)[:160])
    except (wl.WatchError, newslib.NewsError) as exc:
        code = exc.code if exc.code in ("too_large",) else "fetch_failed"
        return dict(out, status=code, error=str(exc)[:160])
    if len(text) < MIN_CHARS:
        return dict(out, status="no_text", chars=len(text), error=f"{len(text)} characters")
    return dict(out, status="ok", text=text, chars=len(text))


def _rendered(url: str) -> tuple[str, str, str | None]:
    try:
        rr = render_page.render(url)
    except Exception as exc:  # noqa: BLE001 - measured as a failed render
        return "", "render", f"{type(exc).__name__}: {exc}"[:160]
    if not rr.get("ok"):
        return "", "render", f"{rr.get('code')}: {rr.get('error')}"[:160]
    text, how = extract(rr.get("html") or "", rr.get("final_url") or url)
    return text, f"render/{how}", None


def run(items: list[dict[str, Any]], budget_s: float, emit: Any = None) -> dict[str, Any]:
    """The direct links first, then the Google News ones; each result goes to ``emit`` as
    soon as it is known. {run_s, browser_ms, items}."""
    started = time.monotonic()
    deadline = started + budget_s
    done: list[dict[str, Any]] = []

    def put(res: dict[str, Any]) -> None:
        done.append(res)
        if emit:
            emit(res)

    def one(it: dict[str, Any], url: str) -> None:
        t0 = time.monotonic()
        res = article(url)
        put(dict({"id": it["id"], "try": it["try"], "url": it["url"]}, **res,
                 ms=int((time.monotonic() - t0) * 1000)))

    for it in [i for i in items if not i.get("gn")]:
        if time.monotonic() > deadline:
            break
        one(it, it["url"])
    gn = [i for i in items if i.get("gn")]
    launch_ms = None
    if gn and time.monotonic() < deadline:
        try:
            resolved, launch_ms = resolve_gn([i["url"] for i in gn], deadline)
        except ImportError:
            resolved = {i["url"]: {"target": None, "error": "no playwright in this venv"}
                        for i in gn}
        except Exception as exc:  # noqa: BLE001 - every GN item fails, the others went on
            resolved = {i["url"]: {"target": None, "error": f"{type(exc).__name__}: {exc}"[:160]}
                        for i in gn}
        for it in gn:
            g = resolved.get(it["url"])
            if g is None or time.monotonic() > deadline:
                continue                         # not reached within the budget
            if not g.get("target"):
                put({"id": it["id"], "try": it["try"], "url": it["url"], "status": "unresolved",
                     "final_url": None, "text": None, "chars": 0, "via": None,
                     "error": g.get("error"), "ms": 0})
                continue
            one(it, g["target"])
    return {"run_s": round(time.monotonic() - started, 1), "browser_ms": launch_ms,
            "items": done}


def _line(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _watchdog(budget: float) -> None:
    """After budget + WATCHDOG_S the run ends whatever hangs: the last line says so, and
    the browser goes with the process group when this process leads it."""
    def fire(_sig: int, _frame: Any) -> None:
        try:
            _line({"done": {"run_s": None, "browser_ms": None, "timeout": True,
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}})
        finally:
            if os.getpgid(0) == os.getpid():
                os.killpg(0, signal.SIGKILL)
            os._exit(3)
    signal.signal(signal.SIGALRM, fire)
    signal.alarm(int(budget + WATCHDOG_S))


def main() -> int:
    try:
        req = json.loads(sys.stdin.read(2 * 1024 * 1024))
        items = newslib.validate_wanted({"v": 1, "items": req.get("items")})
        budget = max(1.0, min(float(req.get("budget_s") or newslib.TEXT_BUDGET_S), 900.0))
    except (ValueError, AttributeError, newslib.NewsError) as exc:
        _line({"error": f"bad request: {exc}"[:200]})
        return 2
    _watchdog(budget)

    def emit(res: dict[str, Any]) -> None:
        _line({"item": res})
        sys.stderr.write(f"{res['id']} {res['status']} {res['ms']} ms\n")
        sys.stderr.flush()

    out = run(items, budget, emit)
    signal.alarm(0)
    _line({"done": {"run_s": out["run_s"], "browser_ms": out["browser_ms"], "timeout": False,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
