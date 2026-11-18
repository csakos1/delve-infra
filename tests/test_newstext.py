"""News article texts (Delve N1c): the backend's wanted.json, the id:try marks, the
news-text run with a stand-in worker (the texts file, the state, a missing venv, a bad
answer), and the worker itself on loopback servers - robots.txt with the `delve-news`
token (rules, a refusal, unreachable), plain HTTP, the text from <article>, too little
text, an HTTP error, a refusal tried in the browser. With Playwright and a Chromium also
the Google News step for real: a script redirect, a consent page ("Reject all"), a link
that leads nowhere - and the publisher's page is never loaded by the browser.
Only the standard library otherwise; no network. Exit 0 = all OK.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLUGIN = os.path.join(ROOT, "hermes", "plugins", "agentcards")
sys.path.insert(0, PLUGIN)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="newstext-home-")
os.environ["DELVE_NEWS_DIR"] = tempfile.mkdtemp(prefix="newstext-dir-")
import newslib  # noqa: E402
import newstext  # noqa: E402

newslib.SLEEP = lambda _s: None
NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)
PARA = ("A kitalált szerkesztőség hosszú bekezdése a teszthez, hogy legyen mit kinyerni. "
        "Second sentence of the made-up article, long enough for one paragraph. ")
ARTICLE = ("<html><head><meta charset='utf-8'><title>Cikk</title></head><body>"
           "<nav><p>" + "Menü elem, nem a cikk része, de hosszú, hogy számítson. " * 2 + "</p></nav>"
           "<article><h1>Cím</h1>" + "".join(f"<p>{n}. {PARA}</p>" for n in range(1, 6))
           + "</article></body></html>")
SHORT = "<html><body><article><p>" + PARA + "</p></article></body></html>"


def wanted(*items):
    return {"v": 1, "written_at": "x", "items": list(items)}


class Wanted(unittest.TestCase):
    def test_valid_and_invalid(self):
        out = newslib.validate_wanted(wanted({"id": 1, "url": "https://a.example/x", "gn": True},
                                             {"id": 2, "url": "http://b.example/y", "try": 3}))
        self.assertEqual(out, [{"id": 1, "url": "https://a.example/x", "gn": True, "try": 1},
                               {"id": 2, "url": "http://b.example/y", "gn": False, "try": 3}])
        for bad in ({"v": 2, "items": []}, {"v": 1},
                    wanted({"id": "1", "url": "https://a.example"}),
                    wanted({"id": 1, "url": "ftp://a.example"}),
                    wanted({"id": 1, "url": "https://a"}, {"id": 1, "url": "https://b"}),
                    wanted({"id": 1, "url": "https://a", "try": 0})):
            with self.assertRaises(newslib.ConfigError):
                newslib.validate_wanted(bad)

    def test_load(self):
        d = tempfile.mkdtemp()
        self.assertEqual(newslib.load_wanted(d), [])               # not written yet
        with open(os.path.join(d, "wanted.json"), "w") as f:
            f.write("{")
        with self.assertRaises(newslib.ConfigError):
            newslib.load_wanted(d)

    def test_to_fetch_marks_and_limit(self):
        st = {"texts": {"1:1": NOW.timestamp(), "9:1": (NOW - dt.timedelta(days=4)).timestamp(),
                        "8:1": "x"}}
        w = [{"id": 1, "url": "u", "gn": False, "try": 1}, {"id": 1, "url": "u", "gn": False, "try": 2},
             {"id": 2, "url": "u", "gn": False, "try": 1}, {"id": 3, "url": "u", "gn": False, "try": 1}]
        self.assertEqual([(i["id"], i["try"]) for i in newslib.to_fetch(w, st, NOW, limit=2)],
                         [(1, 2), (2, 1)])                          # asked again: a new try
        self.assertEqual(set(st["texts"]), {"1:1"})                  # old and odd marks gone


FAKE_WORKER = r'''
import json, sys
req = json.load(sys.stdin)
mode = open(sys.argv[0] + ".mode").read().strip()
if mode == "garbage":
    print("not json"); sys.exit(1)
items = [{"id": i["id"], "try": i["try"], "status": "ok", "url": i["url"], "final_url": i["url"],
          "text": "x" * 600, "chars": 600, "via": "http/paragraphs", "ms": 1}
         for i in req["items"]]
items.append({"id": 999, "try": 1, "status": "ok"})          # never asked: dropped
print(json.dumps({"run_s": 0.1, "browser_ms": None, "items": items}))
'''


class TextsRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="texts-run-")
        self.script = os.path.join(self.tmp, "fake_worker.py")
        with open(self.script, "w") as f:
            f.write(FAKE_WORKER)
        self.mode("ok")
        os.environ["DELVE_NEWS_TEXT_PYTHON"] = sys.executable
        os.environ["DELVE_NEWS_TEXT_SCRIPT"] = self.script
        self.out = newslib.OUT_DIR
        for name in os.listdir(self.out) if os.path.isdir(self.out) else []:
            os.unlink(os.path.join(self.out, name))
        try:
            os.unlink(newslib._state_path())
        except OSError:
            pass
        newslib._write_json(os.path.join(newslib.NEWS_DIR, "wanted.json"), wanted(
            {"id": 5, "url": "https://a.example/1", "gn": False, "try": 1},
            {"id": 6, "url": "https://news.google.com/rss/articles/X", "gn": True, "try": 1}), 0o644)

    def tearDown(self):
        for k in ("DELVE_NEWS_TEXT_PYTHON", "DELVE_NEWS_TEXT_SCRIPT"):
            os.environ.pop(k, None)

    def mode(self, m):
        with open(self.script + ".mode", "w") as f:
            f.write(m)

    def texts_files(self):
        return sorted(n for n in os.listdir(self.out) if n.startswith("text-")) \
            if os.path.isdir(self.out) else []

    def test_one_run_then_nothing(self):
        self.assertEqual(newslib.texts_main([]), 0)
        files = self.texts_files()
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.out, files[0])) as f:
            data = json.load(f)
        self.assertEqual([(i["id"], i["status"]) for i in data["items"]], [(5, "ok"), (6, "ok")])
        self.assertEqual(oct(os.stat(os.path.join(self.out, files[0])).st_mode & 0o777), "0o644")
        self.assertEqual(set(newslib.load_state()["texts"]), {"5:1", "6:1"})
        self.assertEqual(newslib.texts_main([]), 0)                  # asked already
        self.assertEqual(len(self.texts_files()), 1)

    def test_no_venv_and_a_bad_answer(self):
        os.environ["DELVE_NEWS_TEXT_PYTHON"] = "/nonexistent/python"
        self.assertEqual(newslib.texts_main([]), 0)
        self.assertEqual(self.texts_files(), [])
        self.assertNotIn("texts", newslib.load_state())              # nothing marked: next run
        os.environ["DELVE_NEWS_TEXT_PYTHON"] = sys.executable
        self.mode("garbage")
        with self.assertRaises(newslib.NewsError) as cm:
            newslib.run_worker([{"id": 1, "url": "https://a", "gn": False, "try": 1}],
                               lock_path=os.path.join(self.tmp, "lock"))
        self.assertEqual(cm.exception.code, "text_failed")

    def test_busy_renderer(self):
        import fcntl
        lock = os.path.join(self.tmp, "lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(newslib.NewsError) as cm:
                newslib.run_worker([{"id": 1, "url": "https://a", "gn": False, "try": 1}],
                                   lock_path=lock, lock_wait_s=0.2)
            self.assertEqual(cm.exception.code, "render_busy")
        finally:
            os.close(fd)


class _Site:
    """One loopback server per made-up host: {path: (status, content type, body)}."""

    def __init__(self, routes):
        self.hits: list[tuple[str, str]] = []
        hits = self.hits

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if "://" in path:
                    path = "/" + path.split("://", 1)[1].partition("/")[2]
                hits.append((path, self.headers.get("User-Agent", "")))
                code, ctype, body = routes.get(path, (404, "text/plain", "no"))
                if callable(body):
                    code, body = body(self.headers.get("User-Agent", ""))
                if code == 302:
                    self.send_response(302)
                    self.send_header("Location", body)
                    self.end_headers()
                    return
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


HTML = "text/html; charset=utf-8"


def _browser_only(ua):
    return (200, ARTICLE) if "Chrome" in ua else (403, "go away")


SITES = {
    "pub1": {"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private\n"),
             "/a": (200, HTML, ARTICLE), "/short": (200, HTML, SHORT),
             "/gone": (404, HTML, "no")},
    "pub2": {"/robots.txt": (200, "text/plain", "User-agent: delve-news\nDisallow: /\n\n"
                                                "User-agent: *\nAllow: /\n"),
             "/b": (200, HTML, ARTICLE)},
    "pub3": {"/robots.txt": (503, "text/plain", "busy"), "/c": (200, HTML, ARTICLE)},
    "pub4": {"/d": (200, HTML, _browser_only)},                        # robots.txt: 404
    "news": {"/rss/articles/A": (200, HTML, "<html><head><title>Google News</title><script>"
                                 "setTimeout(function(){location.href='http://pub1.test/a'},200)"
                                 "</script></head><body></body></html>"),
             "/rss/articles/B": (302, HTML, "http://consent.test/?continue=B"),
             "/articles/B2": (200, HTML, "<html><script>location.href='http://pub2.test/b'"
                                         "</script></html>"),
             "/rss/articles/C": (200, HTML, "<html><body>nothing here</body></html>")},
    "consent": {"/": (200, HTML, "<html><body><button onclick=\"location.href="
                                 "'http://news.test/articles/B2'\">Reject all</button>"
                                 "<button>Accept all</button></body></html>")},
}


class Worker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sites = {name: _Site(routes) for name, routes in SITES.items()}
        cls.env_before = os.environ.get("DELVE_RENDER_TEST_HOSTS")
        os.environ["DELVE_RENDER_TEST_HOSTS"] = ",".join(
            f"{n}.test=127.0.0.1:{s.port}" for n, s in cls.sites.items())

    @classmethod
    def tearDownClass(cls):
        for s in cls.sites.values():
            s.srv.shutdown()
        if cls.env_before is None:
            os.environ.pop("DELVE_RENDER_TEST_HOSTS", None)
        else:
            os.environ["DELVE_RENDER_TEST_HOSTS"] = cls.env_before

    def test_robots(self):
        self.assertEqual(newstext.robots("http://pub1.test/a")["allowed"], True)
        self.assertEqual(newstext.robots("http://pub1.test/private/x")["allowed"], False)
        self.assertEqual(newstext.robots("http://pub2.test/b")["allowed"], False)  # our token
        self.assertEqual(newstext.robots("http://pub3.test/c"),
                         {"allowed": False, "robots": "unreachable", "detail": "HTTP 503"})
        self.assertEqual(newstext.robots("http://pub4.test/d"), {"allowed": True, "robots": "none"})
        before = len(self.sites["pub1"].hits)
        newstext.robots("http://pub1.test/other")                     # cached for a day
        self.assertEqual(len(self.sites["pub1"].hits), before)

    def test_articles(self):
        ok = newstext.article("http://pub1.test/a")
        self.assertEqual(ok["status"], "ok")
        self.assertIn("1. A kitalált", ok["text"])
        self.assertNotIn("Menü", ok["text"])                          # the <article> only
        self.assertIn("\n\n2. ", ok["text"])
        self.assertIn(ok["via"], ("http/paragraphs", "http/trafilatura"))
        self.assertEqual(newstext.article("http://pub1.test/short")["status"], "no_text")
        self.assertEqual(newstext.article("http://pub1.test/gone")["status"], "http_404")
        self.assertEqual(newstext.article("http://pub2.test/b")["status"], "robots")
        self.assertEqual(newstext.article("http://pub3.test/c")["status"], "robots")
        ua = [u for p, u in self.sites["pub1"].hits if p == "/a"][0]
        self.assertIn("DelveNews/0.1", ua)
        self.assertNotIn("/b", [p for p, _ in self.sites["pub2"].hits])  # never asked
        # a 403 is tried once more in the browser
        res = newstext.article("http://pub4.test/d")
        if _chromium() and _playwright():
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["via"].startswith("render/"))
        else:
            self.assertEqual(res["status"], "render_failed")

    def test_run_without_a_browser_and_the_budget(self):
        items = [{"id": 1, "url": "http://pub1.test/a", "gn": False, "try": 2},
                 {"id": 2, "url": "http://pub1.test/gone", "gn": False, "try": 1}]
        out = newstext.run(items, 60)
        self.assertEqual([(i["id"], i["try"], i["status"]) for i in out["items"]],
                         [(1, 2, "ok"), (2, 1, "http_404")])
        self.assertEqual(newstext.run(items, 0)["items"], [])          # nothing started

    def test_main_reads_stdin(self):
        import io
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps({"v": 1, "budget_s": 30, "items": [
            {"id": 3, "url": "http://pub1.test/a", "gn": False, "try": 1}]}))
        sys.stdout = io.StringIO()
        try:
            self.assertEqual(newstext.main(), 0)
            out = json.loads(sys.stdout.getvalue())
            sys.stdin = io.StringIO('{"items": [{"id": "x"}]}')
            sys.stdout = io.StringIO()
            self.assertEqual(newstext.main(), 2)
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        self.assertEqual(out["items"][0]["status"], "ok")

    def test_google_news(self):
        if not (_chromium() and _playwright()):
            self.skipTest("needs Playwright and a Chromium")
        os.environ["DELVE_CHROMIUM"] = _chromium()
        newstext.render_page.CHROMIUM = _chromium()
        newstext.GN_WAIT_S = 3.0
        items = [{"id": 1, "url": "http://news.test/rss/articles/A", "gn": True, "try": 1},
                 {"id": 2, "url": "http://news.test/rss/articles/B", "gn": True, "try": 1},
                 {"id": 3, "url": "http://news.test/rss/articles/C", "gn": True, "try": 1}]
        pub1_before = list(self.sites["pub1"].hits)
        out = newstext.run(items, 120)
        by = {i["id"]: i for i in out["items"]}
        self.assertEqual(by[1]["status"], "ok")
        self.assertEqual(by[1]["final_url"], "http://pub1.test/a")
        self.assertEqual(by[2]["status"], "robots")                   # reached past consent
        self.assertEqual(by[2]["final_url"], "http://pub2.test/b")
        self.assertEqual((by[3]["status"], by[3]["error"]), ("unresolved", "no way to the publisher"))
        self.assertIn("/", [p for p, _ in self.sites["consent"].hits])
        # the browser never loaded the publisher's page: only plain HTTP asked pub1
        new = self.sites["pub1"].hits[len(pub1_before):]
        self.assertTrue(new)
        self.assertFalse([u for _, u in new if "Chrome" in u])
        self.assertIsNotNone(out["browser_ms"])


def _chromium() -> str | None:
    for p in (os.environ.get("DELVE_CHROMIUM"), "/usr/bin/chromium"):
        if p and os.access(p, os.X_OK):
            return p
    return None


def _playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=1)
