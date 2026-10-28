"""Watchers offline tests (Delve W1): URL guard, parsers on the synthetic fixtures, filters,
config, the run cycle (baseline, new items, errors, cards), page_fetch and watch_manage with
a fake Hermes cron. Only the standard library; no network. Exit 0 = all OK.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLUGIN = os.path.join(ROOT, "hermes", "plugins", "agentcards")
FIX = os.path.join(ROOT, "tests", "fixtures", "watch")
sys.path.insert(0, PLUGIN)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="watch-test-home-")  # logs, default dirs
import schemas  # noqa: E402
import watch_tools  # noqa: E402
import watchlib  # noqa: E402

watchlib.SLEEP = lambda _s: None   # no real waits in the session warm-up retries

HA_SEARCH = "https://hardverapro.hu/aprok/keres.php?stext=rtx"
HA_ITEM_PREFIX = "https://hardverapro.hu/apro/"
CC_FILM = "https://www.cinemacity.hu/films/kitalalt-film-ketto/1002s2r"


def fixture(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def public(host, port):
    return ["93.184.216.34"]


class FakeFetcher:
    """url (or prefix) -> Page; records the URLs."""

    def __init__(self, pages: dict[str, tuple[str, str, str]]) -> None:
        self.pages = pages      # key -> (final_url, content_type, text); key may end with '*'
        self.calls: list[str] = []

    def get(self, url, accept=None):
        self.calls.append(url)
        watchlib.check_url(url, public)
        for key, (final, ctype, text) in self.pages.items():
            if url == key or (key.endswith("*") and url.startswith(key[:-1])):
                return watchlib.Page(url, final or url, 200, ctype, text)
        raise watchlib.FetchError(f"HTTP 404 for {url}", "http_404")


def ha_pages(search_text=None, final=None):
    return {HA_SEARCH: (final or HA_SEARCH, "text/html; charset=utf-8",
                        search_text if search_text is not None else fixture("ha-search-synthetic.html")),
            HA_ITEM_PREFIX + "*": ("", "text/html", fixture("ha-item-synthetic.html"))}


def cc_pages(with_film=True):
    data = json.loads(fixture("cc-films-synthetic.json"))
    if not with_film:
        data["body"]["films"] = [f for f in data["body"]["films"] if f["id"] != "1002s2r"]
    return {watchlib.CinemaCity.API.split("{")[0] + "*": ("", "application/json", json.dumps(data))}


class UrlGuard(unittest.TestCase):
    def test_public_ok(self):
        self.assertEqual(watchlib.check_url("https://example.org/x?a=1", public),
                         "https://example.org/x?a=1")

    def test_blocked(self):
        cases = {
            "http://127.0.0.1:8700/api": ["127.0.0.1"],
            "http://backend.example/": ["10.1.2.3"],
            "http://tail.example/": ["100.101.102.103"],      # CGNAT = the tailnet
            "http://meta.example/": ["169.254.169.254"],
            "http://v6.example/": ["::1"],
            "http://mapped.example/": ["::ffff:127.0.0.1"],
            "http://mixed.example/": ["93.184.216.34", "192.168.1.5"],
        }
        for url, addrs in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(watchlib.BlockedURL):
                    watchlib.check_url(url, lambda h, p, a=addrs: a)
        for url in ("ftp://example.org/", "file:///etc/passwd", "http://localhost:8080/",
                    "http://x.localhost/", "https://user:pw@example.org/", "http:///nohost",
                    "http://box.tail1234.ts.net/"):
            with self.subTest(url=url):
                with self.assertRaises(watchlib.BlockedURL):
                    watchlib.check_url(url, public)

    def test_redirect_to_private_blocked(self):
        h = watchlib._Redirects(lambda host, port: ["127.0.0.1"] if host == "evil.example" else ["93.184.216.34"])
        req = urllib.request.Request("https://example.org/a")
        with self.assertRaises(watchlib.BlockedURL):
            h.redirect_request(req, None, 302, "Found", {}, "http://evil.example/admin")

    def test_spacing(self):
        with tempfile.TemporaryDirectory() as d:
            f = watchlib.Fetcher(None, public, interval_s=0.3, host_dir=d)
            f._space("example.org")
            t = time.monotonic()
            f._space("example.org")
            self.assertGreaterEqual(time.monotonic() - t, 0.2)


class Parsers(unittest.TestCase):
    def test_hardverapro_list(self):
        items = watchlib.HardverApro.parse_list(fixture("ha-search-synthetic.html"), HA_SEARCH)
        ids = [i["item_id"] for i in items]
        self.assertEqual(len(ids), 6)                   # the repeated promoted row once
        self.assertEqual(len(set(ids)), 6)
        by = {i["item_id"]: i for i in items}
        self.assertEqual(by["ha:9100002"]["price_value"], 175000)
        self.assertEqual(by["ha:9100002"]["location"], "XI. kerület")
        self.assertEqual(by["ha:9100002"]["posted"], "ma 17:45")
        self.assertTrue(by["ha:9100004"]["wanted"])
        self.assertTrue(by["ha:9100002"]["url"].startswith("https://hardverapro.hu/apro/"))

    def test_hardverapro_items_drop_wanted_and_detect_ignored_search(self):
        f = FakeFetcher(ha_pages())
        items, _ = watchlib.HardverApro().items(f, HA_SEARCH)
        self.assertNotIn("ha:9100004", [i["item_id"] for i in items])
        f = FakeFetcher(ha_pages(final="https://hardverapro.hu/aprok/hardver/videokartya/index.html"))
        with self.assertRaises(watchlib.ParseError) as cm:
            watchlib.HardverApro().items(f, HA_SEARCH)
        self.assertEqual(cm.exception.code, "search_ignored")
        f = FakeFetcher(ha_pages(search_text="<html><title>Other</title></html>"))
        with self.assertRaises(watchlib.ParseError):
            watchlib.HardverApro().items(f, HA_SEARCH)

    def test_hardverapro_search_retried_once_with_session(self):
        class Seq(FakeFetcher):
            def __init__(self, finals):
                super().__init__(ha_pages())
                self.finals = list(finals)

            def get(self, url, accept=None):
                page = super().get(url, accept)
                if self.finals:
                    page.final_url = self.finals.pop(0)
                return page
        idx = "https://hardverapro.hu/aprok/index.html"
        waits: list[float] = []
        orig, watchlib.SLEEP = watchlib.SLEEP, waits.append   # noqa: F841 (restored below)
        try:
            f = Seq([idx, idx, HA_SEARCH])              # sign-on round, warm-up, real search
            items, _ = watchlib.HardverApro().items(f, HA_SEARCH)
            self.assertEqual(len(f.calls), 3)
            self.assertTrue(items)
            self.assertEqual(waits, list(watchlib.SEARCH_RETRY_WAITS_S))
            f = Seq([idx, idx, idx, idx])
            with self.assertRaises(watchlib.ParseError):
                watchlib.HardverApro().items(f, HA_SEARCH)
            self.assertEqual(len(f.calls), 3)           # two retries, not more
        finally:
            watchlib.SLEEP = orig

    def test_hardverapro_detail(self):
        text = watchlib.HardverApro.parse_detail(fixture("ha-item-synthetic.html"))
        self.assertIn("soha nem volt bányászva", text)
        self.assertIn("Állapot: használt", text)
        self.assertIn("postázás", text)
        self.assertNotIn("seller02", text)              # the seller box is left out
        self.assertNotIn("Megosztás", text)             # and the share menu
        self.assertNotIn("ignore me", text)             # scripts never become text

    def test_cinemacity(self):
        src = watchlib.CinemaCity()
        items, _ = src.items(FakeFetcher(cc_pages(True)), CC_FILM)
        self.assertEqual([i["item_id"] for i in items], ["cc:1002s2r"])
        self.assertTrue(items[0]["url"].startswith("https://www.cinemacity.hu/films/"))
        items, _ = src.items(FakeFetcher(cc_pages(False)), CC_FILM)
        self.assertEqual(items, [])
        with self.assertRaises(watchlib.ParseError):
            src.parse_films({"films": []})
        with self.assertRaises(watchlib.ConfigError):
            src.validate_url("https://example.org/films/x/1001s2r")
        # without a film id: all bookable films, picked by title words
        items, _ = src.items(FakeFetcher(cc_pages(True)), "https://www.cinemacity.hu/")
        self.assertEqual(len(items), 2)
        hits = [i for i in items if watchlib.item_matches(i, {"include": ["ketto"]})]
        self.assertEqual([i["item_id"] for i in hits], ["cc:1002s2r"])

    def test_cinemacity_rules_and_invented_film_page(self):
        with self.assertRaisesRegex(watchlib.ConfigError, "include words"):   # no id, no words
            watchlib.validate_config({"slug": "film", "name": "f", "source": "cinemacity",
                                      "url": "https://www.cinemacity.hu/", "schedule": "every 4h"})
        c = watchlib.validate_config({"slug": "film", "name": "f", "url": "https://www.cinemacity.hu/",
                                      "source": "cinemacity", "include": ["doom"], "schedule": "every 4h"})
        self.assertTrue(c["notify_existing"])               # a film already bookable is news
        made_up = "https://www.cinemacity.hu/films/bosszuallokok-dr-doom-erbedese/9001d2r"
        pages = {**cc_pages(True)}
        with self.assertRaises(watchlib.ConfigError):       # 404 -> refused before anything is saved
            watchlib.preview({**c, "url": made_up}, FakeFetcher(pages))
        pages[CC_FILM] = ("", "text/html", "<html><head><title>Kitalált Film Kettő | Jegyvásárlás a Cinema City mozikban</title></head></html>")
        out = watchlib.preview({**c, "url": CC_FILM, "include": []}, FakeFetcher(pages))
        self.assertEqual(out["matched"], 1)

    def test_generic_links(self):
        html = ('<html><body><a href="/a">RTX 3060 eladó</a><a href="#top">RTX top</a>'
                '<a href="https://other.example/b">semmi</a><a href="/a">RTX 3060 eladó</a></body></html>')
        f = FakeFetcher({"https://shop.example/list": ("", "text/html", html)})
        items, _ = watchlib.GenericPage().items(f, "https://shop.example/list")
        self.assertEqual([i["url"] for i in items], ["https://shop.example/a", "https://other.example/b"])


class Filters(unittest.TestCase):
    def test_word_start(self):
        t = watchlib.fold("ASUS ROG Original RTX4070Super, nem bányászott")
        self.assertFalse(watchlib.has_word(t, "rig"))       # not inside 'original'
        self.assertTrue(watchlib.has_word(t, "orig"))
        self.assertTrue(watchlib.has_word(t, "4070"))       # letter -> digit change
        self.assertTrue(watchlib.has_word(t, "super"))      # digit -> letter change
        self.assertTrue(watchlib.has_word(t, "bányász"))    # prefix of a word, accents folded
        self.assertFalse(watchlib.has_word(t, "ászott"))

    def test_fold_and_price(self):
        self.assertEqual(watchlib.fold("Bányászott ŐSZI"), "banyaszott oszi")
        self.assertEqual(watchlib.price_value("299 990 Ft"), 299990)
        self.assertEqual(watchlib.price_value("1 234 Ft"), 1234)
        self.assertIsNone(watchlib.price_value("Keresem"))

    def test_item_matches(self):
        item = {"title": "MSI RTX 4070 SUPER Ventus (bányászgépből)", "price_value": 160000}
        m = watchlib.item_matches
        self.assertTrue(m(item, {"include": ["rtx", "4070", "super"]}))
        self.assertFalse(m(item, {"include": ["rtx", "3060"]}))
        self.assertTrue(m(item, {"any": ["ventus", "ghost"]}))
        self.assertFalse(m(item, {"exclude": ["banyasz"]}))          # accent-insensitive
        self.assertTrue(m(item, {"max_price": 180000}))
        self.assertFalse(m(item, {"min_price": 170000}))
        self.assertFalse(m({"title": "x"}, {"max_price": 10}))      # no price -> no match


class Config(unittest.TestCase):
    def base(self, **kw):
        cfg = {"slug": "rtx", "name": "RTX", "source": "hardverapro", "url": HA_SEARCH,
               "schedule": "every 4h"}
        cfg.update(kw)
        return cfg

    def test_ok_and_defaults(self):
        c = watchlib.validate_config(self.base())
        self.assertFalse(c["notify_existing"])
        c = watchlib.validate_config(self.base(stop_after_hit=True))
        self.assertTrue(c["notify_existing"])
        c = watchlib.validate_config({**self.base(), "source": "cinemacity", "url": CC_FILM,
                                      "fetch_details": True})
        self.assertFalse(c["fetch_details"])                         # no detail pages there

    def test_errors(self):
        bad = [
            self.base(url="https://example.org/aprok/"),
            self.base(url=HA_SEARCH + "&offset=100"),
            self.base(source="ebay"),
            self.base(slug="Bad Slug"),
            self.base(include=["x"] * 11),
            self.base(min_price=10, max_price=5),
            self.base(criteria="x" * 501),
            self.base(source="page", url="https://shop.example/"),
            {**self.base(), "url": "javascript:alert(1)"},
        ]
        for cfg in bad:
            with self.subTest(cfg=cfg):
                with self.assertRaises(watchlib.ConfigError):
                    watchlib.validate_config(cfg)

    def test_slugify(self):
        self.assertEqual(watchlib.slugify("RTX 4070 Super ≤180k"), "rtx-4070-super-180k")
        self.assertTrue(watchlib.SLUG_RE.fullmatch(watchlib.slugify("Ő")))


class RunCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.cards: list[dict] = []

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, ev):
        # what the backend does: validate the event again with the vendored schemas
        schemas.VALIDATORS[ev["type"]](ev["payload"], dt.datetime.now(dt.timezone.utc))
        self.cards.append(ev)

    def save(self, **kw):
        cfg = {"slug": "rtx", "name": "RTX 4070 Super", "source": "hardverapro",
               "url": HA_SEARCH, "include": ["4070"], "exclude": ["pasztazas"],
               "max_price": 200000, "schedule": "every 4h"}
        cfg.update(kw)
        watchlib.save_config(watchlib.validate_config(cfg), self.base)

    def run_(self, fetcher, **kw):
        return watchlib.run_watch("rtx", fetcher=fetcher, publish=self.publish, base=self.base, **kw)

    def types(self):
        return [c["type"] for c in self.cards]

    def test_baseline_then_new_then_quiet(self):
        self.save()
        out = self.run_(FakeFetcher(ha_pages()))
        self.assertTrue(out["baseline"])
        self.assertEqual(out["matched"], 2)                 # 175k + 160k (299k over max)
        self.assertEqual(self.types(), ["watch_def", "watch_run"])
        # a new ad appears
        page = fixture("ha-search-synthetic.html").replace(
            'data-uadid="9100006"', 'data-uadid="9100007"').replace(
            "Zotac Gaming GeForce RTX 3060 Twin Edge 12GB", "Zotac RTX 4070 Super Twin Edge")
        self.cards.clear()
        out = self.run_(FakeFetcher(ha_pages(page)))
        self.assertFalse(out["baseline"])
        self.assertEqual((out["new"], out["candidates"]), (1, 1))
        self.assertEqual(self.types(), ["watch_candidate", "watch_run"])
        cand = self.cards[0]["payload"]
        self.assertEqual(cand["item_id"], "ha:9100007")
        self.assertEqual(cand["price_value"], 84000)
        self.assertNotIn("excerpt", cand)                    # fetch_details is off
        self.cards.clear()
        out = self.run_(FakeFetcher(ha_pages(page)))
        self.assertEqual(out["new"], 0)
        self.assertEqual(self.types(), ["watch_run"])          # the def is not repeated

    def test_reserved_item_reported_when_available_again(self):
        self.save()
        free = fixture("ha-search-synthetic.html")
        booked = free.replace("175 000 Ft</span>", "175 000 Ft</span> <span>jegelve</span>")
        self.assertNotEqual(free, booked)
        self.run_(FakeFetcher(ha_pages(booked)))           # baseline, the 175k ad is booked
        st = watchlib.load_state("rtx", self.base)
        self.assertEqual(list(st["reserved"]), ["ha:9100002"])
        self.cards.clear()
        self.run_(FakeFetcher(ha_pages(booked)))           # still booked: nothing
        self.assertEqual(self.types(), ["watch_run"])
        self.cards.clear()
        self.run_(FakeFetcher(ha_pages(free)))             # free again
        self.assertEqual(self.types(), ["watch_candidate", "watch_run"])
        cand = self.cards[0]["payload"]
        self.assertEqual((cand["item_id"], cand["change"]), ("ha:9100002", "available"))
        self.assertNotIn("reserved", cand)
        self.assertEqual(watchlib.load_state("rtx", self.base)["reserved"], {})
        self.cards.clear()
        self.run_(FakeFetcher(ha_pages(free)))             # reported once
        self.assertEqual(self.types(), ["watch_run"])

    def test_new_reserved_item_is_marked(self):
        self.save()
        self.run_(FakeFetcher(ha_pages()))
        page = fixture("ha-search-synthetic.html").replace(
            'data-uadid="9100006"', 'data-uadid="9100007"').replace(
            "Zotac Gaming GeForce RTX 3060 Twin Edge 12GB", "Zotac RTX 4070 Super Twin Edge").replace(
            "84 000 Ft</span>", "84 000 Ft jegelve</span>")
        self.cards.clear()
        self.run_(FakeFetcher(ha_pages(page)))
        cands = [c["payload"] for c in self.cards if c["type"] == "watch_candidate"]
        self.assertEqual([(c["item_id"], c.get("reserved")) for c in cands], [("ha:9100007", True)])

    def test_details_and_cap(self):
        self.save(include=["rtx"], max_price=None, exclude=[], fetch_details=True,
                  notify_existing=True)
        f = FakeFetcher(ha_pages())
        orig = watchlib.MAX_CANDIDATES
        watchlib.MAX_CANDIDATES = 2
        try:
            out = self.run_(f)
        finally:
            watchlib.MAX_CANDIDATES = orig
        self.assertEqual((out["new"], out["candidates"]), (5, 2))
        cands = [c["payload"] for c in self.cards if c["type"] == "watch_candidate"]
        self.assertEqual(len(cands), 2)
        self.assertIn("bányászva", cands[0]["excerpt"])
        self.assertEqual(len([u for u in f.calls if u.startswith(HA_ITEM_PREFIX)]), 2)
        st = watchlib.load_state("rtx", self.base)
        self.assertEqual(len(st["seen"]), 5)                 # the rest is marked seen too

    def test_error_card_and_exit_code(self):
        self.save()
        f = FakeFetcher(ha_pages(final="https://hardverapro.hu/aprok/index.html"))
        with self.assertRaises(watchlib.ParseError) as cm:
            self.run_(f)
        self.assertEqual(cm.exception.exit_code, watchlib.EXIT_PARSE)
        run = [c for c in self.cards if c["type"] == "watch_run"][-1]
        self.assertEqual((run["payload"]["status"], run["severity"]), ("error", "warn"))
        self.assertEqual(run["payload"]["error_code"], "search_ignored")
        self.assertEqual(watchlib.load_state("rtx", self.base)["errors"], 1)

    def test_http_error_card(self):
        # the error code carries the status (http_404): the card must still be valid
        self.save()
        with self.assertRaises(watchlib.FetchError):
            self.run_(FakeFetcher({}))
        (run,) = [c for c in self.cards if c["type"] == "watch_run"]
        self.assertEqual(run["payload"]["error_code"], "http_404")

    def test_stop_after_hit_announces_existing(self):
        watchlib.save_config(watchlib.validate_config({
            "slug": "rtx", "name": "Film", "source": "cinemacity", "url": CC_FILM,
            "stop_after_hit": True, "schedule": "every 2h"}), self.base)
        out = self.run_(FakeFetcher(cc_pages(True)))
        self.assertTrue(out["baseline"])
        self.assertEqual(out["candidates"], 1)
        self.assertEqual(self.types(), ["watch_def", "watch_candidate", "watch_run"])
        defn = self.cards[0]["payload"]
        self.assertTrue(defn["stop_after_hit"])

    def test_dry_run_writes_nothing(self):
        self.save()
        out = self.run_(FakeFetcher(ha_pages()), dry_run=True)
        self.assertEqual(self.cards, [])
        self.assertFalse(os.path.exists(watchlib.state_path("rtx", self.base)))
        self.assertTrue(out["sample"])

    def test_url_change_restarts_baseline(self):
        self.save()
        self.run_(FakeFetcher(ha_pages()))
        url2 = HA_SEARCH + "&maxprice=200000"
        self.save(url=url2)
        pages = {url2: ha_pages()[HA_SEARCH]}
        out = self.run_(FakeFetcher(pages))
        self.assertTrue(out["baseline"])

    def test_missing_config(self):
        with self.assertRaises(watchlib.ConfigError) as cm:
            watchlib.run_watch("nope", base=self.base, publish=self.publish)
        self.assertEqual(cm.exception.exit_code, watchlib.EXIT_CONFIG)


class PageFetch(unittest.TestCase):
    def test_listings_text_links(self):
        f = FakeFetcher(ha_pages())
        out = json.loads(watch_tools.handle_page_fetch(
            {"url": HA_SEARCH, "mode": "listings", "find": "4070"}, fetcher_factory=lambda: f))
        self.assertTrue(out["success"])
        self.assertEqual(out["site_total"], 7)
        self.assertEqual(out["items_on_page"], 5)            # wanted ad dropped
        self.assertEqual(out["matching"], 3)
        out = json.loads(watch_tools.handle_page_fetch(
            {"url": HA_SEARCH, "mode": "text", "max_chars": 1000}, fetcher_factory=lambda: f))
        self.assertLessEqual(len(out["content"]), 1000)
        self.assertNotIn("data-uadid", out["content"])
        out = json.loads(watch_tools.handle_page_fetch(
            {"url": HA_SEARCH, "mode": "links", "find": "3060"}, fetcher_factory=lambda: f))
        self.assertIn("Zotac", out["content"])

    def test_blocked(self):
        out = json.loads(watch_tools.handle_page_fetch(
            {"url": "http://127.0.0.1:8700/api/v1/system"},
            fetcher_factory=lambda: watchlib.Fetcher(None, lambda h, p: ["127.0.0.1"], 0, "")))
        self.assertEqual((out["success"], out["error"]), (False, "blocked_url"))


class FakeCron(watch_tools.Cron):
    def __init__(self):
        self.calls: list[dict] = []
        self.jobs: dict[str, dict] = {}

    def parse_schedule(self, s):
        if s.startswith("every "):
            n = int(s[6:-1])
            return {"kind": "interval", "minutes": n * (60 if s.endswith("h") else 1)}
        if len(s.split()) == 5:
            return {"kind": "cron", "expr": s}
        if s.startswith("20"):
            return {"kind": "once"}
        raise ValueError(f"Invalid schedule '{s}'")

    def call(self, **kw):
        self.calls.append(kw)
        a = kw["action"]
        if a == "create":
            jid = f"{len(self.jobs) + 1:012x}"
            self.jobs[jid] = {"job_id": jid, "name": kw["name"], "schedule": kw["schedule"],
                              "state": "scheduled", "enabled": True}
            return {"success": True, "job_id": jid, "next_run_at": "2026-10-28T08:00:00+01:00"}
        if a == "list":
            return {"success": True, "jobs": list(self.jobs.values())}
        if kw.get("job_id") not in self.jobs:
            return {"success": False, "error": "Job not found"}
        if a == "remove":
            del self.jobs[kw["job_id"]]
        elif a == "pause":
            self.jobs[kw["job_id"]]["state"] = "paused"
        return {"success": True}


class Manage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "watches")
        self.scripts = os.path.join(self.tmp.name, "scripts")
        self.cron = FakeCron()
        self.cards: list[dict] = []
        self.pages = ha_pages()
        self.m = watch_tools.WatchManager(self.cron, self.base, self.scripts,
                                          fetcher_factory=lambda slug: FakeFetcher(self.pages),
                                          publish=self.cards.append)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, **args):
        return json.loads(self.m.handle(args))

    def test_preview_create_list_update_pause_delete(self):
        req = {"name": "RTX 4070 Super ≤200k", "url": HA_SEARCH, "include": ["4070"],
               "max_price": 200000, "criteria": "ne legyen bányászott", "fetch_details": True,
               "schedule": "every 4h"}
        out = self.call(action="preview", **req)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["definition"]["source"], "hardverapro")   # inferred from the URL
        self.assertEqual(out["now"]["matched"], 2)
        self.assertEqual(self.cron.calls, [])                          # nothing scheduled

        out = self.call(action="create", **req)
        self.assertTrue(out["success"], out)
        slug = out["slug"]
        self.assertEqual(slug, "rtx-4070-super-200k")
        create = [c for c in self.cron.calls if c["action"] == "create"][0]
        self.assertEqual((create["name"], create["no_agent"], create["deliver"],
                          create["failure_deliver"], create["script"]),
                         (f"watch:{slug}", True, "local", "local", "watch_rtx_4070_super_200k.py"))
        wrapper = os.path.join(self.scripts, create["script"])
        with open(wrapper, encoding="utf-8") as fh:
            src = fh.read()
        compile(src, wrapper, "exec")
        self.assertIn(f"watchlib.main({slug!r})", src)
        self.assertTrue(out["first_run"]["baseline"])
        self.assertEqual([c["type"] for c in self.cards], ["watch_def", "watch_run"])
        self.assertEqual(self.cards[0]["payload"]["criteria"], "ne legyen bányászott")

        out = self.call(action="create", **req)                       # same name -> -2
        self.assertEqual(out["slug"], "rtx-4070-super-200k-2")

        out = self.call(action="list")
        self.assertEqual(len(out["watchers"]), 2)
        self.assertRegex(out["watchers"][0]["last_ok_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")   # local, no Z
        self.assertEqual(out["watchers"][0]["job"]["state"], "scheduled")

        out = self.call(action="update", slug=slug, schedule="15 8,13,19 * * *", max_price=150000)
        self.assertTrue(out["success"], out)
        self.assertEqual(self.cron.calls[-1]["action"], "update")
        self.assertEqual(watchlib.load_config(slug, self.base)["max_price"], 150000)

        self.assertTrue(self.call(action="pause", slug=slug)["success"])
        out = self.call(action="delete", slug=slug)
        self.assertTrue(out["success"], out)
        self.assertFalse(os.path.exists(watchlib.config_path(slug, self.base)))
        self.assertFalse(os.path.exists(wrapper))

    def test_schedule_rules(self):
        for s in ("every 10m", "*/5 * * * *", "0,30 * * * *", "2026-11-01T10:00:00", "soon"):
            with self.subTest(s=s):
                out = self.call(action="create", name="x y", url=HA_SEARCH, schedule=s)
                self.assertFalse(out["success"])
        self.assertEqual([c for c in self.cron.calls if c["action"] == "create"], [])

    def test_failed_fetch_writes_nothing(self):
        self.pages = {}
        out = self.call(action="create", name="nincs", url=HA_SEARCH, schedule="every 4h")
        self.assertEqual((out["success"], out["error"]), (False, "http_404"))
        self.assertFalse(os.path.exists(self.base) and os.listdir(self.base))
        self.assertEqual([c for c in self.cron.calls if c["action"] == "create"], [])

    def test_unknown_action_and_bad_slug(self):
        self.assertFalse(self.call(action="explode")["success"])
        self.assertEqual(self.call(action="pause", slug="../x")["error"], "bad_config")


class Schemas(unittest.TestCase):
    def test_watch_cards(self):
        now = dt.datetime.now(dt.timezone.utc)
        ok_ts = (now - dt.timedelta(minutes=1)).isoformat()
        good = {"slug": "rtx", "item_id": "ha:1", "title": "t", "url": "https://x.example/a"}
        schemas.validate_watch_candidate(good, now)
        self.assertEqual(schemas.validate_watch_candidate(
            {**good, "reserved": True, "change": "available"}, now)["change"], "available")
        for bad in ({**good, "url": "javascript:x"}, {**good, "item_id": "a b"},
                    {**good, "excerpt": "x" * 1501}, {**good, "extra": 1},
                    {**good, "change": "sold"}, {**good, "reserved": "yes"}):
            with self.subTest(bad=bad):
                with self.assertRaises(schemas.CardValidationError):
                    schemas.validate_watch_candidate(bad, now)
        schemas.validate_watch_run({"slug": "rtx", "status": "error", "checked_at": ok_ts,
                                    "error_code": "fetch_failed"}, now)
        with self.assertRaises(schemas.CardValidationError):
            schemas.validate_watch_run({"slug": "rtx", "status": "ok", "checked_at": ok_ts}, now)
        ev = schemas.build_event("watch_candidate", good, "info", "script")
        self.assertEqual(ev["type"], "watch_candidate")


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if r.wasSuccessful() else 1)
