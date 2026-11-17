"""News collector offline tests (Delve N1): the sources file, RSS 2.0 / RSS 1.0 / Atom
parsing, Google News titles, URL normalisation, the time window, the run cycle (due
sources, 304, rate limits with Retry-After, seen items, the time budget, removed sources),
the batch file and the state, and real HTTP on the loopback (conditional requests, a 429,
the public-address guard on the feed and on a redirect). Only the standard library; no
network. Exit 0 = all OK.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLUGIN = os.path.join(ROOT, "hermes", "plugins", "agentcards")
sys.path.insert(0, PLUGIN)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="news-test-home-")
import newslib  # noqa: E402
import watchlib  # noqa: E402

newslib.SLEEP = lambda _s: None
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc)


def public(host, port):
    return ["93.184.216.34"]


def rfc822(t: dt.datetime) -> str:
    return t.strftime("%a, %d %b %Y %H:%M:%S GMT")


def rss(items: list[tuple[str, str, dt.datetime | None]], title: str = "Kitalált Hírek",
        extra: str = "") -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        + (f"<pubDate>{rfc822(d)}</pubDate>" if d else "")
        + f"<description>&lt;p&gt;Kivonat a(z) {t} hírhez.&lt;/p&gt;</description>{extra}</item>"
        for t, link, d in items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            f"<title>{title}</title>{body}</channel></rss>").encode("utf-8")


GN_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>"America's Cup" - Google News</title>
<item><title>Teams unveil new boats - Sailing Daily</title>
<link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
<pubDate>{rfc822(NOW - dt.timedelta(hours=3))}</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiAAA"&gt;Teams&lt;/a&gt;</description>
<source url="https://sailing.example">Sailing Daily</source></item>
</channel></rss>""".encode()

RDF = f"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://papers.example/"><title>papers</title></channel>
<item rdf:about="https://papers.example/abs/1"><title>A paper on agents</title>
<link>https://papers.example/abs/1</link><dc:date>{NOW.isoformat()}</dc:date>
<description>We study agents.</description></item>
</rdf:RDF>""".encode()

ATOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes from tool</title>
<entry><title>b12477</title><updated>{(NOW - dt.timedelta(days=2)).isoformat()}</updated>
<link rel="alternate" type="text/html" href="https://github.com/acme/tool/releases/tag/b12477"/>
<content type="html">&lt;p&gt;Fixes.&lt;/p&gt;</content></entry>
<entry><title>b12000</title><updated>{(NOW - dt.timedelta(days=30)).isoformat()}</updated>
<link rel="alternate" href="https://github.com/acme/tool/releases/tag/b12000"/></entry>
</feed>""".encode()


def src(sid="hirek", url="https://hirek.example/rss", **kw):
    return {"id": sid, "url": url, "kind": kw.pop("kind", "feed"),
            "window_h": kw.pop("window_h", 48), "every_min": kw.pop("every_min", 60), **kw}


class FakeGet:
    """url -> Response (or a callable); records (url, etag, modified)."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, etag=None, modified=None):
        self.calls.append((url, etag, modified))
        r = self.pages[url]
        return r(etag, modified) if callable(r) else r


def ok(body: bytes, **headers) -> newslib.Response:
    return newslib.Response(200, {k.lower().replace("_", "-"): v for k, v in headers.items()},
                            body, "https://hirek.example/rss")


class SourcesFile(unittest.TestCase):
    def test_valid(self):
        out = newslib.validate_sources({"v": 1, "sources": [
            {"id": "telex", "url": "https://telex.example/rss"},
            {"id": "gh.tool", "url": "https://github.com/a/b/releases.atom", "kind": "release",
             "window_h": 168, "every_min": 180}]})
        self.assertEqual(out[0], src("telex", "https://telex.example/rss"))
        self.assertEqual(out[1]["kind"], "release")

    def test_invalid(self):
        bad = [
            {"v": 2, "sources": []},
            {"v": 1},
            {"v": 1, "sources": [{"id": "Bad Id", "url": "https://x.example/"}]},
            {"v": 1, "sources": [{"id": "a", "url": "ftp://x.example/"}]},
            {"v": 1, "sources": [{"id": "a", "url": "https://x.example/", "kind": "page"}]},
            {"v": 1, "sources": [{"id": "a", "url": "https://x.example/", "window_h": 0}]},
            {"v": 1, "sources": [{"id": "a", "url": "https://x.example/", "every_min": 5}]},
            {"v": 1, "sources": [{"id": "a", "url": "https://x.example/"},
                                 {"id": "a", "url": "https://y.example/"}]},
        ]
        for data in bad:
            with self.assertRaises(newslib.ConfigError, msg=data):
                newslib.validate_sources(data)

    def test_load(self):
        d = tempfile.mkdtemp()
        self.assertIsNone(newslib.load_sources(d))
        with open(os.path.join(d, "sources.json"), "w") as f:
            f.write("{not json")
        with self.assertRaises(newslib.ConfigError):
            newslib.load_sources(d)
        with open(os.path.join(d, "sources.json"), "w") as f:
            json.dump({"v": 1, "sources": [src()]}, f)
        self.assertEqual(newslib.load_sources(d), [src()])


class Parsing(unittest.TestCase):
    def test_rss2(self):
        title, raw = newslib.parse_feed(rss([("Első hír", "https://hirek.example/a?utm_source=rss#x",
                                              NOW)]), "https://hirek.example/rss")
        self.assertEqual(title, "Kitalált Hírek")
        self.assertEqual(raw[0]["link"], "https://hirek.example/a?utm_source=rss#x")
        items = newslib.to_items(src(), title, raw, NOW)
        it = items[0]
        self.assertEqual(it["url"], "https://hirek.example/a")
        self.assertEqual(it["title"], "Első hír")
        self.assertEqual(it["publisher"], "Kitalált Hírek")
        self.assertEqual(it["summary"], "Kivonat a(z) Első hír hírhez.")
        self.assertEqual(it["published"], "2026-11-17T12:00:00Z")
        self.assertEqual(it["key"], newslib.item_key("https://hirek.example/a/"))
        self.assertFalse(it["gn"])

    def test_google_news(self):
        title, raw = newslib.parse_feed(GN_RSS, "https://news.google.com/rss/search?q=x")
        it = newslib.to_items(src("gn-ac", "https://news.google.com/rss/search?q=x"), title, raw, NOW)[0]
        self.assertEqual(it["title"], "Teams unveil new boats")
        self.assertEqual(it["publisher"], "Sailing Daily")
        self.assertTrue(it["gn"])
        self.assertEqual(it["url"], "https://news.google.com/rss/articles/CBMiAAA?oc=5")

    def test_rdf_and_atom(self):
        title, raw = newslib.parse_feed(RDF, "https://papers.example/rss")
        self.assertEqual([r["title"] for r in raw], ["A paper on agents"])
        self.assertEqual(newslib.to_items(src(), title, raw, NOW)[0]["publisher"], "papers")
        title, raw = newslib.parse_feed(ATOM, "https://github.com/acme/tool/releases.atom")
        items = newslib.to_items(src("tool", kind="release", window_h=168), title, raw, NOW)
        self.assertEqual([i["title"] for i in items], ["b12477"])   # 30 days: out of the window
        self.assertEqual(items[0]["url"], "https://github.com/acme/tool/releases/tag/b12477")
        self.assertEqual(items[0]["kind"], "release")

    def test_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
        for body in (bomb, b"<html><body>no</body></html>", b"not xml at all"):
            with self.assertRaises(newslib.FeedError, msg=body):
                newslib.parse_feed(body, "https://x.example/")

    def test_window_and_odd_items(self):
        old = NOW - dt.timedelta(hours=49)
        title, raw = newslib.parse_feed(rss([
            ("Friss", "https://hirek.example/1", NOW - dt.timedelta(hours=1)),
            ("Régi", "https://hirek.example/2", old),
            ("Dátum nélkül", "https://hirek.example/3", None),
            ("Jövőbeli", "https://hirek.example/4", NOW + dt.timedelta(days=2)),
            ("Rossz link", "javascript:alert(1)", NOW),
        ]), "https://hirek.example/rss")
        items = newslib.to_items(src(), title, raw, NOW)
        self.assertEqual([i["title"] for i in items], ["Friss", "Dátum nélkül"])
        self.assertIsNone(items[1]["published"])

    def test_text_and_urls(self):
        self.assertEqual(newslib.clean_text("<b>A&amp;B</b>\n\t  c", 100), "A&B c")
        self.assertEqual(newslib.clean_text("x" * 50, 10), "x" * 9 + "…")
        self.assertEqual(
            newslib.normalize_url("HTTPS://Www.Example.COM/a/b/?fbclid=1&id=7&utm_medium=x#top"),
            "https://www.example.com/a/b?id=7")
        self.assertEqual(newslib.normalize_url("https://example.com/"), "https://example.com")
        self.assertEqual(len(newslib.item_key("https://example.com/a")), 32)

    def test_retry_after(self):
        self.assertEqual(newslib.retry_after_s("120", NOW), 120.0)
        self.assertEqual(newslib.retry_after_s("999999", NOW), newslib.RETRY_AFTER_MAX_S)
        self.assertEqual(newslib.retry_after_s(rfc822(NOW + dt.timedelta(minutes=10)), NOW), 600.0)
        self.assertIsNone(newslib.retry_after_s("soon", NOW))
        self.assertIsNone(newslib.retry_after_s(None, NOW))


class RunCycle(unittest.TestCase):
    def test_first_run_then_only_new_items(self):
        url = "https://hirek.example/rss"
        get = FakeGet({url: ok(rss([("A", "https://hirek.example/a", NOW)]), ETag='"v1"')})
        st = {}
        b1 = newslib.collect([src()], st, get=get, now=NOW)
        self.assertEqual([i["title"] for i in b1["items"]], ["A"])
        self.assertEqual(b1["sources"][0]["new"], 1)
        # an hour later the feed has one more item; the known one is not sent again
        later = NOW + dt.timedelta(hours=1)
        get.pages[url] = ok(rss([("B", "https://hirek.example/b", later),
                                 ("A", "https://hirek.example/a", NOW)]), ETag='"v2"')
        b2 = newslib.collect([src()], st, get=get, now=later)
        self.assertEqual([i["title"] for i in b2["items"]], ["B"])
        self.assertEqual(get.calls[1], (url, '"v1"', None))   # conditional request
        self.assertEqual(st["sources"]["hirek"]["etag"], '"v2"')

    def test_not_due_not_modified_and_force(self):
        url = "https://hirek.example/rss"
        get = FakeGet({url: ok(rss([("A", "https://hirek.example/a", NOW)]))})
        st = {}
        newslib.collect([src()], st, get=get, now=NOW)
        b = newslib.collect([src()], st, get=get, now=NOW + dt.timedelta(minutes=20))
        self.assertEqual(b["sources"], [])       # not due: not even reported
        self.assertEqual(len(get.calls), 1)
        get.pages[url] = newslib.Response(304, {}, None, url)
        b = newslib.collect([src()], st, get=get, now=NOW + dt.timedelta(minutes=58))
        self.assertEqual(b["sources"][0]["status"], "not_modified")
        b = newslib.collect([src()], st, get=get, now=NOW + dt.timedelta(minutes=59), force=True)
        self.assertEqual(len(get.calls), 3)

    def test_rate_limit_waits(self):
        url = "https://www.reddit.com/r/x/top/.rss?t=day"
        s = src("r-x", url)
        get = FakeGet({url: newslib.Response(429, {"retry-after": "7200"}, None, url)})
        st = {}
        b = newslib.collect([s], st, get=get, now=NOW)
        self.assertEqual((b["sources"][0]["status"], b["sources"][0]["error_code"]),
                         ("error", "rate_limited"))
        b = newslib.collect([s], st, get=get, now=NOW + dt.timedelta(hours=1), force=True)
        self.assertEqual(b["sources"][0], {"id": "r-x", "status": "skipped", "http": None,
                                           "items": 0, "new": 0, "error_code": "rate_limited",
                                           "error": None, "ms": 0})
        self.assertEqual(len(get.calls), 1)
        get.pages[url] = ok(rss([("Post", "https://www.reddit.com/r/x/1", NOW)]))
        b = newslib.collect([s], st, get=get, now=NOW + dt.timedelta(hours=2, minutes=1))
        self.assertEqual(b["sources"][0]["status"], "ok")
        self.assertNotIn("retry_until", st["sources"]["r-x"])
        self.assertEqual(st["sources"]["r-x"]["errors_in_row"], 0)

    def test_errors_are_reported_per_source(self):
        good, bad, html_page = ("https://a.example/rss", "https://b.example/rss",
                                "https://c.example/rss")
        get = FakeGet({good: ok(rss([("A", "https://a.example/1", NOW)])),
                       bad: newslib.Response(404, {}, None, bad),
                       html_page: ok(b"<html><body>hello</body></html>")})
        st = {}
        b = newslib.collect([src("a", good), src("b", bad), src("c", html_page)], st, get=get,
                            now=NOW)
        by = {r["id"]: r for r in b["sources"]}
        self.assertEqual(by["a"]["status"], "ok")
        self.assertEqual((by["b"]["status"], by["b"]["error_code"]), ("error", "http_404"))
        self.assertEqual(by["c"]["error_code"], "parse_failed")
        self.assertEqual(st["sources"]["b"]["errors_in_row"], 1)

    def test_budget_and_removed_sources(self):
        urls = [f"https://h{i}.example/rss" for i in range(3)]
        get = FakeGet({u: ok(rss([])) for u in urls})
        ticks = iter([0.0, 0.0, 300.0, 300.0, 300.0, 300.0, 300.0])
        st = {"sources": {"gone": {"fetched_at": 1}}}
        b = newslib.collect([src(f"s{i}", u) for i, u in enumerate(urls)], st, get=get, now=NOW,
                            clock=lambda: next(ticks))
        self.assertEqual([r["status"] for r in b["sources"]], ["ok", "skipped", "skipped"])
        self.assertEqual(b["sources"][1]["error_code"], "budget")
        self.assertNotIn("gone", st["sources"])

    def test_one_host_is_not_asked_twice_in_a_row(self):
        srcs = [src("a1", "https://a.example/1"), src("a2", "https://a.example/2"),
                src("b1", "https://b.example/1")]
        self.assertEqual([s["id"] for s in newslib._order(srcs)], ["a1", "b1", "a2"])

    def test_batch_file_and_state(self):
        out, base = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = os.path.join(out, "batch-20200101T000000Z-1.json")
        with open(old, "w") as f:
            f.write("{}")
        os.utime(old, (1, 1))
        path = newslib.write_batch({"v": 1, "items": []}, out, NOW)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o755)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.basename(path).startswith("batch-20261117T120000Z-"))
        st = {"sources": {}, "seen": {"new": NOW.timestamp(),
                                      "old": (NOW - dt.timedelta(days=20)).timestamp()}}
        newslib.save_state(st, base, NOW)
        self.assertEqual(newslib.load_state(base)["seen"], {"new": NOW.timestamp()})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(base, "state.json")).st_mode), 0o600)

    def test_main(self):
        d, home = tempfile.mkdtemp(), tempfile.mkdtemp()
        saved = (newslib.NEWS_DIR, newslib.STATE_DIR, newslib.OUT_DIR)
        newslib.NEWS_DIR, newslib.STATE_DIR = d, os.path.join(home, "news")
        newslib.OUT_DIR = os.path.join(home, "cards", "news")
        try:
            self.assertEqual(newslib.main([]), 0)          # no sources yet: nothing to do
            with open(os.path.join(d, "sources.json"), "w") as f:
                f.write('{"v": 1, "sources": [{"id": "x"}]}')
            self.assertEqual(newslib.main([]), 4)
            with open(os.path.join(d, "sources.json"), "w") as f:
                json.dump({"v": 1, "sources": []}, f)
            self.assertEqual(newslib.main([]), 0)
            self.assertFalse(os.path.exists(newslib.OUT_DIR))   # nothing fetched, no batch
        finally:
            newslib.NEWS_DIR, newslib.STATE_DIR, newslib.OUT_DIR = saved


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Handler.seen.append((self.path, self.headers.get("If-None-Match"),
                              self.headers.get("User-Agent")))
        if self.path == "/feed":
            if self.headers.get("If-None-Match") == '"e1"':
                self.send_response(304)
                self.end_headers()
                return
            body = rss([("Hír", "https://hirek.example/x", NOW)])
            self.send_response(200)
            self.send_header("ETag", '"e1"')
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/busy":
            self.send_response(429)
            self.send_header("Retry-After", "30")
            self.end_headers()
        elif self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/feed")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class RealHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.hosts = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path, **kw):
        # a resolver that calls the loopback public, so the guard lets the test server through
        return newslib.http_get(self.base + path, resolver=public, host_dir=self.hosts, **kw)

    def test_conditional_and_rate_limit(self):
        r = self.get("/feed")
        self.assertEqual((r.status, r.headers["etag"]), (200, '"e1"'))
        self.assertIn(b"<rss", r.body)
        self.assertEqual(self.get("/feed", etag='"e1"').status, 304)
        busy = self.get("/busy")
        self.assertEqual((busy.status, busy.headers["retry-after"]), (429, "30"))
        self.assertEqual(self.get("/nothing").status, 404)
        self.assertIn("DelveNews/0.1", _Handler.seen[0][2])

    def test_the_guard(self):
        with self.assertRaises(watchlib.BlockedURL):       # the real resolver: loopback
            newslib.http_get(self.base + "/feed", host_dir=self.hosts)
        # a public-looking start that redirects to the loopback: the real resolver on the hop
        resolver_calls = []

        def first_public(host, port):
            resolver_calls.append(host)
            return public(host, port) if len(resolver_calls) == 1 else ["127.0.0.1"]

        with self.assertRaises(watchlib.BlockedURL):
            newslib.http_get(self.base + "/hop", resolver=first_public, host_dir=self.hosts)


if __name__ == "__main__":
    unittest.main(verbosity=1)
