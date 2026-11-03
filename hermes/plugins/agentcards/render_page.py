"""render_page – load one public page in a headless Chromium and print it as JSON (Delve W5).

Runs in its own venv (Playwright), never inside the Hermes gateway:
    <venv>/bin/python -I render_page.py <url> [--wait-for CSS] [--capture-json]
stdout: {"ok": true, "url", "final_url", "status", "title", "html", "json": [...],
         "blocked": [...], "requests": n, "ms": n}  or  {"ok": false, "code", "error"}

Safety: every connection the browser makes - the page, its redirects, scripts, XHR,
WebSockets - goes through a small proxy in this process. The proxy resolves the host once,
applies watchlib's URL rule (only public addresses; no loopback, private, link-local, CGNAT
= the tailnet, ...) to every answer and connects to the checked address, so a redirect or a
DNS answer that changes between the check and the connect cannot reach the machine or the
tailnet. Chromium is told to send loopback through the proxy too (bypass "<-loopback>").
Images, media and fonts are not loaded; service workers are blocked; the page is closed
after at most a few dozen seconds. The Chromium sandbox stays on.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import watchlib  # noqa: E402  (stdlib only: the URL rule and the user agent token)

CHROMIUM = os.environ.get("DELVE_CHROMIUM") or "/usr/bin/chromium"
NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 10_000
WAIT_FOR_TIMEOUT_MS = 15_000
MAX_CONNECTIONS = 300
MAX_TUNNEL_BYTES = 60 * 1024 * 1024
MAX_HTML = 3 * 1024 * 1024
MAX_JSON_RESPONSES = 20
MAX_JSON_BYTES = 512 * 1024
ALLOWED_PORTS = {80, 443, 8080, 8443}
SKIP_TYPES = {"image", "media", "font"}
UA_TOKEN = "delve-watch/0.1 (personal use)"


def _test_hosts() -> dict[str, tuple[str, int]]:
    """Tests only: DELVE_RENDER_TEST_HOSTS='site.test=127.0.0.1:8123,...' maps made-up host
    names to local test servers. Nothing else changes: 127.0.0.1 itself stays blocked."""
    out: dict[str, tuple[str, int]] = {}
    for part in filter(None, (os.environ.get("DELVE_RENDER_TEST_HOSTS") or "").split(",")):
        name, _, target = part.partition("=")
        host, _, port = target.rpartition(":")
        if name.endswith(".test") and host == "127.0.0.1" and port.isdigit():
            out[name.lower()] = (host, int(port))
    return out


class Guard:
    """Resolve + check once per (host, port); remember what was refused."""

    def __init__(self) -> None:
        self.test_hosts = _test_hosts()
        self.lock = threading.Lock()
        self.cache: dict[tuple[str, int], tuple[str, int] | str] = {}
        self.blocked: list[str] = []
        self.connections = 0
        self.bytes = 0

    def target(self, host: str, port: int) -> tuple[str, int]:
        host = host.strip("[]").rstrip(".").lower()
        key = (host, port)
        with self.lock:
            self.connections += 1
            if self.connections > MAX_CONNECTIONS:
                raise watchlib.BlockedURL("too many connections")
            hit = self.cache.get(key)
        if isinstance(hit, tuple):
            return hit
        if isinstance(hit, str):
            raise watchlib.BlockedURL(hit)
        try:
            if host in self.test_hosts:
                res: tuple[str, int] = self.test_hosts[host]
            else:
                if port not in ALLOWED_PORTS:
                    raise watchlib.BlockedURL(f"port {port} is not allowed")
                addrs: list[str] = []

                def resolver(h: str, p: int) -> list[str]:
                    got = watchlib._resolve(h, p)
                    addrs.extend(got)
                    return got
                scheme = "http" if port in (80, 8080) else "https"
                watchlib.check_url(f"{scheme}://{_hostport(host, port)}/", resolver)
                res = (addrs[0].split("%", 1)[0], port)
        except watchlib.WatchError as exc:
            with self.lock:
                self.cache[key] = str(exc)
                if len(self.blocked) < 50:
                    self.blocked.append(f"{host}:{port} ({exc.code})")
            raise
        with self.lock:
            self.cache[key] = res
        return res


def _hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _Handler(socketserver.BaseRequestHandler):
    guard: Guard

    def _read_head(self) -> bytes:
        self.request.settimeout(20)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.request.recv(8192)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                break
        return buf

    def _refuse(self, why: str) -> None:
        body = why.encode("utf-8", "replace")[:300]
        try:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                                 b"Connection: close\r\nContent-Length: " +
                                 str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass

    def handle(self) -> None:
        try:
            head = self._read_head()
        except OSError:
            return
        block, _, early = head.partition(b"\r\n\r\n")
        line, _, header_block = block.partition(b"\r\n")
        parts = line.decode("latin-1").split()
        if len(parts) != 3:
            return
        method, target, version = parts
        try:
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                addr = self.guard.target(host, int(port))
                upstream = socket.create_connection(addr, timeout=20)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if early:
                    upstream.sendall(early)
            else:
                u = urllib.parse.urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    return self._refuse("only absolute http:// requests")
                addr = self.guard.target(u.hostname, u.port or 80)
                upstream = socket.create_connection(addr, timeout=20)
                path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
                headers = [h for h in header_block.split(b"\r\n") if h and not h.lower().startswith(
                    (b"proxy-", b"connection:", b"keep-alive:"))]
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1") +
                                 b"".join(h + b"\r\n" for h in headers) +
                                 b"Connection: close\r\n\r\n" + early)
        except (watchlib.WatchError, ValueError) as exc:
            return self._refuse(str(exc))
        except OSError as exc:
            return self._refuse(f"connect failed: {exc}")
        self._pipe(upstream)

    def _pipe(self, upstream: socket.socket) -> None:
        socks = [self.request, upstream]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is self.request else self.request).sendall(data)
                    with self.guard.lock:
                        self.guard.bytes += len(data)
                        if self.guard.bytes > MAX_TUNNEL_BYTES:
                            return
        except OSError:
            return
        finally:
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_proxy(guard: Guard) -> tuple[_Server, int]:
    handler = type("Handler", (_Handler,), {"guard": guard})
    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def render(url: str, wait_for: str | None = None, capture_json: bool = False) -> dict:
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return {"ok": False, "code": "blocked_url", "error": "only http(s) URLs"}
    guard = Guard()
    if (u.hostname or "").lower() not in guard.test_hosts:
        watchlib.check_url(url)        # the page itself, before a browser starts
    srv, port = start_proxy(guard)
    json_out: list[dict] = []
    counts = {"requests": 0, "skipped": 0}

    def on_route(route, request):
        counts["requests"] += 1
        if request.resource_type in SKIP_TYPES:
            counts["skipped"] += 1
            return route.abort("blockedbyclient")
        if not request.url.startswith(("http://", "https://", "data:", "blob:")):
            return route.abort("blockedbyclient")
        return route.continue_()

    def on_response(resp):
        if len(json_out) >= MAX_JSON_RESPONSES:
            return
        try:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype or resp.status != 200 or resp.request.method != "GET":
                return
            body = resp.body()
        except PwError:
            return
        if len(body) > MAX_JSON_BYTES:
            json_out.append({"url": resp.url[:600], "bytes": len(body), "truncated": True})
            return
        json_out.append({"url": resp.url[:600], "bytes": len(body),
                         "body": body.decode("utf-8", "replace")})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROMIUM, headless=True,
                chromium_sandbox=os.geteuid() != 0,    # root cannot sandbox; production runs as agent
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=["--disable-gpu", "--no-first-run", "--no-default-browser-check",
                      "--disable-extensions", "--disable-sync", "--mute-audio",
                      "--disable-background-networking", "--disable-component-update",
                      "--disable-domain-reliability", "--metrics-recording-only"])
            try:
                ua = re.sub(r"HeadlessChrome", "Chrome", browser.new_page().evaluate(
                    "navigator.userAgent")) + " " + UA_TOKEN
                ctx = browser.new_context(user_agent=ua, locale="hu-HU", service_workers="block",
                                          accept_downloads=False,
                                          viewport={"width": 1280, "height": 2000})
                page = ctx.new_page()
                page.route("**/*", on_route)
                if hasattr(page, "route_web_socket"):
                    page.route_web_socket("**/*", lambda ws: ws.close())
                if capture_json:
                    page.on("response", on_response)
                status = None
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    status = resp.status if resp else None
                except PwTimeout:
                    return {"ok": False, "code": "render_timeout",
                            "error": f"the page did not load in {NAV_TIMEOUT_MS // 1000} s",
                            "blocked": guard.blocked}
                except PwError as exc:
                    msg = str(exc).splitlines()[0][:300]
                    code = "blocked_url" if guard.blocked and "ERR_" in msg else "render_failed"
                    return {"ok": False, "code": code, "error": msg, "blocked": guard.blocked}
                try:
                    if wait_for:
                        page.wait_for_selector(wait_for, timeout=WAIT_FOR_TIMEOUT_MS)
                    else:
                        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except PwTimeout:
                    pass            # long-polling pages never go idle; take what is there
                except PwError as exc:
                    return {"ok": False, "code": "render_failed",
                            "error": str(exc).splitlines()[0][:300]}
                page.wait_for_timeout(500)
                final_url = page.url
                fu = urllib.parse.urlsplit(final_url)
                fkey = f"{(fu.hostname or '').lower()}:{fu.port or (443 if fu.scheme == 'https' else 80)}"
                if any(b.startswith(fkey + " ") for b in guard.blocked):
                    return {"ok": False, "code": "blocked_url",
                            "error": f"the page went to a non-public address ({fkey})",
                            "blocked": guard.blocked}
                html = page.content()
                title = page.title()
            finally:
                browser.close()
    except PwError as exc:
        return {"ok": False, "code": "render_unavailable",
                "error": str(exc).splitlines()[0][:300]}
    finally:
        srv.shutdown()
        srv.server_close()
    if len(html) > MAX_HTML:
        html = html[:MAX_HTML]
    return {"ok": True, "url": url, "final_url": final_url, "status": status, "title": title[:300],
            "html": html, "json": json_out, "blocked": guard.blocked,
            "requests": counts["requests"], "skipped": counts["skipped"],
            "ms": int((time.monotonic() - started) * 1000)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="render one page as JSON")
    ap.add_argument("url")
    ap.add_argument("--wait-for")
    ap.add_argument("--capture-json", action="store_true")
    a = ap.parse_args(argv)
    try:
        out = render(a.url, a.wait_for, a.capture_json)
    except watchlib.WatchError as exc:
        out = {"ok": False, "code": exc.code, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - always answer with JSON
        out = {"ok": False, "code": "render_failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
