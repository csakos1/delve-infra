"""Recipe watchers offline tests (Delve W5): selectors, JSON paths, recipe validation,
extraction, the candidate finder (structure), robots.txt, the run cycle with recipes and
conditions, watch_manage with candidates and the preview gate, the renderer wrapper - and,
when Playwright and a Chromium are present, the renderer itself against local servers
(JavaScript-built list, redirect to a loopback address refused). Synthetic pages only.
Exit 0 = all OK.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLUGIN = os.path.join(ROOT, "hermes", "plugins", "agentcards")
sys.path.insert(0, PLUGIN)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="recipes-test-home-")
import recipes  # noqa: E402
import watch_tools  # noqa: E402
import watchlib  # noqa: E402

watchlib.SLEEP = lambda _s: None

SHOP = "https://shop.example.com/cat/kartyak"
NEXT = "https://ads.example.com/search?q=rtx"
PRODUCT = "https://store.example.com/p/lack-50319029/"
API = "https://api.example.com/v1/releases"
SHELL = "https://app.example.com/"


def shop_html(n: int = 4, extra: str = "") -> str:
    cards = "".join(
        f'<li class="card product{" featured" if i == 1 else ""}">'
        f'<a class="card-link" href="/p/{i}"><h3 class="name">Kártya {i} RTX 40{i}0</h3></a>'
        f'<div class="price-box"><span class="price">{i * 10} 000 Ft</span></div>'
        f'<span class="city">Budapest {i}.</span><p class="desc-placeholder">Leírás {i}: jó állapot, '
        f'garanciával.</p></li>'
        for i in range(1, n + 1))
    nav = "".join(f'<li class="nav-item"><a href="/c/{i}">Menü {i}</a></li>' for i in range(6))
    return (f"<html><head><title>Bolt</title></head><body><ul class='nav'>{nav}</ul>"
            f"<main><ul class='products'>{cards}</ul>{extra}</main></body></html>")


def next_html() -> str:
    data = {"props": {"pageProps": {"adList": {"ads": [
        {"list_id": 100 + i, "subject": f"RTX 4070 hirdetés {i}", "url": f"https://ads.example.com/a/{i}.htm",
         "price": {"label": f"{i}50 000 Ft", "value": i * 100000 + 50000}, "region": "Pest",
         "list_time": {"label": "okt 23., 20:17", "value": "1792779479"},
         "body": f"Hirdetés szövege {i}, nem volt bányászva, számla van."} for i in range(1, 6)]},
        "menu": [{"label": "Póló"}, {"label": "Ing"}, {"label": "Cipő"}]}}}
    return ("<html><head><title>Hirdetések</title></head><body><div id='app'>…</div>"
            f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(data)}</script>"
            "</body></html>")


def product_html(availability: str = "InStock", price: str = "8990") -> str:
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "name": "Termékek"}, {"@type": "ListItem", "name": "Asztalok"}]},
          {"@type": "Product", "name": "LACK Dohányzóasztal", "offers": {
              "@type": "Offer", "price": price, "priceCurrency": "HUF",
              "availability": f"https://schema.org/{availability}"}}]
    blocks = "".join(f"<script type='application/ld+json'>{json.dumps(b)}</script>" for b in ld)
    return (f"<html><head><title>LACK</title>{blocks}</head><body><h1>LACK</h1>"
            f"<div class='stock'>{'Raktáron' if availability == 'InStock' else 'Elfogyott'}</div>"
            f"<span class='price'>{price} Ft</span></body></html>")


def api_json(n: int = 3) -> str:
    return json.dumps([{"id": 900 + i, "name": f"b{11960 + i}", "url": f"{API}/{900 + i}",
                        "html_url": f"https://github.example.com/rel/b{11960 + i}",
                        "body": f"Release notes {i}: fixes and new models.",
                        "published_at": "2026-10-28T12:00:00Z",
                        "assets": [{"name": f"a{j}.zip", "url": f"{API}/assets/{j}"} for j in range(5)]}
                       for i in range(n, 0, -1)])


SHELL_HTML = ("<html><head><title>App</title><script src='/a.js'></script><script src='/b.js'>"
              "</script><script>window.__INITIAL_STATE__={}</script></head><body>"
              "<div id='root'></div></body></html>")


def public(host, port):
    return ["93.184.216.34"]


class FakeFetcher:
    """url -> (content_type, text) or an exception; robots.txt from `robots` (else 404)."""

    def __init__(self, pages: dict, robots: dict | None = None) -> None:
        self.pages, self.robots = pages, robots or {}
        self.calls: list[str] = []

    def get(self, url, accept=None):
        self.calls.append(url)
        watchlib.check_url(url, public)
        if url.endswith("/robots.txt"):
            host = url.split("/")[2]
            r = self.robots.get(host)
            if isinstance(r, Exception):
                raise r
            if r is None:
                raise watchlib.FetchError(f"HTTP 404 for {url}", "http_404")
            return watchlib.Page(url, url, 200, "text/plain", r)
        if url not in self.pages:
            raise watchlib.FetchError(f"HTTP 404 for {url}", "http_404")
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        ctype, text = v
        return watchlib.Page(url, url, 200, ctype, text() if callable(v[1]) else text)


class NoRenderer(recipes.Renderer):
    def __init__(self):
        super().__init__(python="/nonexistent/python")


class FakeRenderer(recipes.Renderer):
    def __init__(self, html: str, json_responses=None):
        super().__init__(python="/nonexistent/python")
        self.html, self.json_responses, self.calls = html, json_responses or [], []
        self.status = 200

    def available(self):
        return True

    def render(self, url, wait_for=None, capture_json=False):
        self.calls.append((url, wait_for, capture_json))
        return {"ok": True, "final_url": url, "status": self.status, "html": self.html,
                "json": self.json_responses if capture_json else []}


class Selectors(unittest.TestCase):
    def setUp(self):
        self.root = watchlib.parse_html(shop_html())

    def test_select(self):
        self.assertEqual(len(recipes.select(self.root, "ul.products > li.card.product")), 4)
        self.assertEqual(len(recipes.select(self.root, "main li.card.featured")), 1)
        self.assertEqual(len(recipes.select(self.root, "ul.products > li")), 4)
        self.assertEqual(len(recipes.select(self.root, "main > li")), 0)        # not a child
        self.assertEqual(len(recipes.select(self.root, "a[href^=/p/]")), 4)
        self.assertEqual(len(recipes.select(self.root, "a[href*=c/]")), 6)
        self.assertEqual(len(recipes.select(self.root, "a[href='/p/2']")), 1)
        for bad in ("", "ul >", "> li", "li:hover", "a" * 400, "div ~ p"):
            with self.subTest(bad=bad), self.assertRaises(watchlib.ConfigError):
                recipes.select(self.root, bad)

    def test_fields(self):
        li = recipes.select(self.root, "li.card")[1]
        base = SHOP
        self.assertEqual(recipes.field_value(li, "h3.name", base), "Kártya 2 RTX 4020")
        self.assertEqual(recipes.field_value(li, "a@href", base), "https://shop.example.com/p/2")
        self.assertEqual(recipes.field_value(li, "@class", base), "card product")
        self.assertIn("Leírás 2", recipes.field_value(li, "", base))
        self.assertIsNone(recipes.field_value(li, "h4", base))
        with self.assertRaises(watchlib.ConfigError):
            recipes.check_field("a@bad attr", "link")


class JsonPaths(unittest.TestCase):
    def test_paths(self):
        d = {"a": {"b": [{"c": 1}, {"c": 2}, {"x": 3}]}, "@graph": [{"k": "v"}], "8000": [5]}
        self.assertEqual(recipes.json_items(d, "$.a.b[*].c"), [1, 2])
        self.assertEqual(recipes.json_items(d, "a.b"), d["a"]["b"])
        self.assertEqual(recipes.json_get(d, recipes.parse_path("$.@graph[0].k")), "v")
        self.assertEqual(recipes.json_get(d, recipes.parse_path("$['8000'][0]")), 5)
        self.assertIsNone(recipes.json_items(d, "$.a.zzz[*]"))
        self.assertIsNone(recipes.json_get(d, recipes.parse_path("$.a.b[9].c")))
        for bad in ("$.a[*].b[*]", "$..a", "$.a[x]"):
            with self.subTest(bad=bad), self.assertRaises(watchlib.ConfigError):
                recipes.json_items(d, bad)

    def test_scalar(self):
        self.assertEqual(recipes._scalar({"label": "550 000 Ft", "value": 550000}), "550 000 Ft")
        self.assertEqual(recipes._scalar(8990.0), "8990")
        self.assertIsNone(recipes._scalar(True))
        self.assertIsNone(recipes._scalar([1]))


class Validation(unittest.TestCase):
    def test_ok(self):
        x = recipes.validate_extract({"mode": "list", "item": "li.card",
                                      "fields": {"title": "h3", "link": "a@href"}})
        self.assertEqual(x["item"], "li.card")
        x = recipes.validate_extract({"mode": "json", "from": "api", "api_url": API,
                                      "items": "$[*]", "fields": {"title": "name"}})
        self.assertEqual(x["from"], "api")
        x = recipes.validate_extract({"mode": "condition", "from": "ld_product", "test": "below",
                                      "value": 8000})
        self.assertEqual(x["value"], 8000)

    def test_errors(self):
        bad = [
            "x", {"mode": "grep"}, {"mode": "list", "item": "li"},                  # no title
            {"mode": "list", "item": "li:hover", "fields": {"title": "a"}},
            {"mode": "list", "item": "li", "fields": {"title": "a", "colour": "b"}},
            {"mode": "json", "from": "file", "items": "$[*]", "fields": {"title": "n"}},
            {"mode": "json", "from": "api", "items": "$[*]", "fields": {"title": "n"}},
            {"mode": "json", "from": "script", "items": "$[*]", "fields": {"title": "n"}},
            {"mode": "json", "items": "", "fields": {"title": "n"}},
            {"mode": "condition", "from": "css", "test": "contains", "value": "x"},  # no selector
            {"mode": "condition", "from": "css", "selector": ".s", "test": "in_stock"},
            {"mode": "condition", "from": "ld_product", "test": "below", "value": "olcsó"},
            {"mode": "condition", "from": "page_text", "test": "contains", "value": ""},
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(watchlib.ConfigError):
                recipes.validate_extract(b)


class Extract(unittest.TestCase):
    def test_list(self):
        root = watchlib.parse_html(shop_html(4) + "")
        items = recipes.extract_list(root, SHOP, {"item": "li.card", "fields": {
            "title": "h3", "link": "a@href", "price": ".price", "location": ".city"}})
        self.assertEqual(len(items), 4)
        i = items[2]
        self.assertEqual((i["title"], i["url"], i["price"], i["price_value"], i["location"]),
                         ("Kártya 3 RTX 4030", "https://shop.example.com/p/3", "30 000 Ft", 30000,
                          "Budapest 3."))
        self.assertTrue(i["item_id"].startswith("rc:"))
        self.assertIn("garanciával", i["text"])

    def test_list_defaults_and_dedup(self):
        html = shop_html(3).replace("</ul></main>", '<li class="card"><a href="/p/1"><h3>Kártya 1 RTX 4010</h3>'
                                    "</a></li></ul></main>")
        items = recipes.extract_list(watchlib.parse_html(html), SHOP,
                                     {"item": "li.card", "fields": {"title": "h3"}})
        self.assertEqual(len(items), 3)                                      # same link, same id
        self.assertEqual(items[0]["url"], "https://shop.example.com/p/1")    # first link
        self.assertEqual(items[0]["price_value"], 10000)                     # price from the text

    def test_json(self):
        loaded = recipes.Loaded(watchlib.Page(NEXT, NEXT, 200, "text/html", next_html()))
        spec = recipes.validate_extract({"mode": "json", "from": "next_data",
                                         "items": "$.props.pageProps.adList.ads[*]",
                                         "fields": {"title": "subject", "link": "url", "price": "price",
                                                    "text": "body", "id": "list_id"}})
        items = recipes.extract_json(recipes.json_source(loaded, spec, None), NEXT, spec)
        self.assertEqual(len(items), 5)
        self.assertEqual((items[0]["price"], items[0]["price_value"]), ("150 000 Ft", 150000))
        self.assertIn("bányászva", items[0]["text"])
        spec["items"] = "$.props.pageProps.gone[*]"
        with self.assertRaises(recipes.RecipeError):
            recipes.extract_json(recipes.json_source(loaded, spec, None), NEXT, spec)

    def test_condition(self):
        def ev(html, spec):
            loaded = recipes.Loaded(watchlib.Page(PRODUCT, PRODUCT, 200, "text/html", html))
            return recipes.evaluate_condition(loaded, recipes.validate_extract(spec))[0]
        ld = {"mode": "condition", "from": "ld_product"}
        self.assertTrue(ev(product_html("InStock"), {**ld, "test": "in_stock"}))
        self.assertFalse(ev(product_html("OutOfStock"), {**ld, "test": "in_stock"}))
        self.assertTrue(ev(product_html(price="8990"), {**ld, "test": "below", "value": 9000}))
        self.assertFalse(ev(product_html(price="8990"), {**ld, "test": "below", "value": 8990}))
        css = {"mode": "condition", "from": "css", "selector": "div.stock"}
        self.assertTrue(ev(product_html("InStock"), {**css, "test": "contains", "value": "raktaron"}))
        self.assertTrue(ev(product_html("OutOfStock"), {**css, "test": "absent", "value": "Raktáron"}))
        self.assertTrue(ev(product_html(), {"mode": "condition", "from": "page_text",
                                            "test": "contains", "value": "LACK"}))
        self.assertTrue(ev(product_html(price="7990"), {"mode": "condition", "from": "css",
                                                        "selector": "span.price", "test": "below",
                                                        "value": 8000}))
        with self.assertRaises(recipes.RecipeError):
            ev("<html><body>semmi</body></html>", {**ld, "test": "in_stock"})


class Structure(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="structure-")

    def run_structure(self, url, pages, renderer=None, render=None, robots=None):
        return recipes.structure(url, FakeFetcher(pages, robots), renderer or NoRenderer(), render,
                                 self.base)

    def test_list_page(self):
        out = self.run_structure(SHOP, {SHOP: ("text/html", shop_html(6))})
        first = out["candidates"][0]
        self.assertEqual(first["kind"], "list")
        self.assertEqual(first["extract"]["item"], "ul.products > li.card.product")
        self.assertEqual(first["items"], 6)
        self.assertEqual(first["with_price"], 6)
        f = first["extract"]["fields"]
        self.assertEqual((f["title"], f["link"], f["location"]), ("h3.name", "a.card-link@href", "span.city"))
        self.assertEqual(first["sample"][0]["location"], "Budapest 1.")     # not the "placeholder"
        self.assertEqual(first["sample"][0]["price"], "10 000 Ft")
        self.assertFalse(any("Menü" in c["sample"][0].get("title", "") for c in out["candidates"][:1]))
        self.assertEqual(out["robots"], "allowed")
        self.assertLess(len(json.dumps(out, ensure_ascii=False)), 7000)
        c = recipes.candidate_from_cache(SHOP, 1, self.base)
        self.assertEqual(c["extract"], first["extract"])
        with self.assertRaises(watchlib.ConfigError):
            recipes.candidate_from_cache(SHOP, 99, self.base)
        with self.assertRaises(watchlib.ConfigError):
            recipes.candidate_from_cache(NEXT, 1, self.base)

    def test_embedded_json_first(self):
        out = self.run_structure(NEXT, {NEXT: ("text/html", next_html())})
        first = out["candidates"][0]
        self.assertEqual((first["kind"], first["extract"]["from"], first["extract"]["items"]),
                         ("json", "next_data", "$.props.pageProps.adList.ads[*]"))
        f = first["extract"]["fields"]
        self.assertEqual((f["title"], f["link"], f["price"], f["text"], f["location"], f["id"]),
                         ("subject", "url", "price", "body", "region", "list_id"))
        self.assertEqual(first["sample"][0]["price"], "150 000 Ft")

    def test_product_conditions_first(self):
        out = self.run_structure(PRODUCT, {PRODUCT: ("text/html", product_html())})
        kinds = [c["kind"] for c in out["candidates"]]
        self.assertEqual(kinds[:2], ["condition", "condition"])
        self.assertEqual(out["candidates"][0]["extract"]["test"], "in_stock")
        self.assertIn("InStock", out["candidates"][0]["now"])

    def test_api_json(self):
        out = self.run_structure(API, {API: ("application/json", api_json(3))})
        first = out["candidates"][0]
        self.assertEqual((first["extract"]["from"], first["extract"]["items"]), ("page", "$[*]"))
        self.assertEqual(first["extract"]["fields"]["link"], "html_url")
        self.assertIsNone(out["title"])

    def test_shell_without_renderer(self):
        out = self.run_structure(SHELL, {SHELL: ("text/html", SHELL_HTML)})
        self.assertFalse(out["rendered"])
        self.assertIn("JavaScript", out["note"])
        self.assertEqual(out["candidates"], [])

    def test_shell_rendered_with_api(self):
        rendered = shop_html(3)
        rend = FakeRenderer(rendered, [{"url": API, "bytes": 100, "body": api_json(4)}])
        out = self.run_structure(SHELL, {SHELL: ("text/html", SHELL_HTML)}, rend)
        self.assertTrue(out["rendered"])
        self.assertEqual(rend.calls, [(SHELL, None, True)])
        froms = [(c["kind"], c["extract"].get("from"), c.get("render")) for c in out["candidates"]]
        self.assertIn(("json", "api", True), froms)              # the API refuses plain HTTP here
        self.assertIn(("list", None, True), froms)                # the DOM: rendered
        out = self.run_structure(SHELL, {SHELL: ("text/html", SHELL_HTML),
                                         API: ("application/json", api_json(4))}, rend)
        froms = [(c["kind"], c["extract"].get("from"), c.get("render")) for c in out["candidates"]]
        self.assertIn(("json", "api", None), froms)              # readable without the browser
        self.assertEqual(recipes.candidate_from_cache(SHELL, [c for c in out["candidates"]
                                                              if c["kind"] == "list"][0]["candidate"],
                                                      self.base)["render"], True)

    def test_blocked_fetch_falls_back_to_render(self):
        rend = FakeRenderer(shop_html(3))
        out = self.run_structure(SHOP, {SHOP: watchlib.FetchError("HTTP 403", "http_403")}, rend)
        self.assertTrue(out["rendered"])
        with self.assertRaises(watchlib.FetchError):
            self.run_structure(SHOP, {SHOP: watchlib.FetchError("HTTP 403", "http_403")}, rend, False)

    def test_error_page_in_the_browser(self):
        rend = FakeRenderer("<pre>rate limit</pre>")
        rend.status = 403
        with self.assertRaises(watchlib.FetchError) as cm:
            self.run_structure(SHELL, {SHELL: ("text/html", SHELL_HTML)}, rend)
        self.assertEqual(cm.exception.code, "http_403")

    def test_robots_reported(self):
        out = self.run_structure(SHOP, {SHOP: ("text/html", shop_html())},
                                 robots={"shop.example.com": "User-agent: *\nDisallow: /cat/\n"})
        self.assertEqual(out["robots"], "disallowed")


class Robots(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="robots-")

    def test_rules_none_unreachable_cache(self):
        f = FakeFetcher({}, {"shop.example.com": "User-agent: *\nDisallow: /cat/\n\nUser-agent: delve-watch\n"
                                                 "Disallow: /private/\n"})
        self.assertTrue(recipes.robots_check(SHOP, f, self.base)["allowed"])     # own group wins
        self.assertFalse(recipes.robots_check("https://shop.example.com/private/x", f, self.base)["allowed"])
        n = len(f.calls)
        recipes.robots_check(SHOP, f, self.base)
        self.assertEqual(len(f.calls), n)                                        # cached
        self.assertTrue(recipes.robots_check(NEXT, f, self.base)["allowed"])     # 404 = no rules
        f.robots["store.example.com"] = watchlib.FetchError("HTTP 503", "http_503")
        v = recipes.robots_check(PRODUCT, f, self.base)
        self.assertEqual((v["allowed"], v["robots"]), (False, "unreachable"))
        with self.assertRaises(watchlib.FetchError) as cm:
            recipes.require_robots(PRODUCT, f, self.base)
        self.assertEqual(cm.exception.code, "robots_disallowed")

    def test_stale_cache_used_when_unreachable(self):
        f = FakeFetcher({}, {"shop.example.com": "User-agent: *\nDisallow: /x/\n"})
        recipes.robots_check(SHOP, f, self.base, now=1000.0)
        f.robots["shop.example.com"] = watchlib.FetchError("timeout", "fetch_failed")
        v = recipes.robots_check(SHOP, f, self.base, now=1000.0 + 2 * recipes.ROBOTS_MAX_AGE_S)
        self.assertEqual((v["allowed"], v["robots"]), (True, "rules"))


class RunCycle(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="run-")
        self.cards: list[dict] = []
        self.pages: dict = {SHOP: ("text/html", shop_html(3))}

    def save(self, **kw):
        cfg = watchlib.validate_config({"slug": "bolt", "name": "Bolt", "source": "page", "url": SHOP,
                                        "schedule": "every 4h", **kw})
        watchlib.save_config(cfg, self.base)
        return cfg

    def go(self, **kw):
        return watchlib.run_watch("bolt", fetcher=FakeFetcher(self.pages), publish=self.cards.append,
                                  base=self.base, **kw)

    def candidates(self):
        return [c["payload"] for c in self.cards if c["type"] == "watch_candidate"]

    def test_list_recipe(self):
        spec = {"mode": "list", "item": "li.card", "fields": {"title": "h3", "link": "a@href",
                                                              "price": ".price"}}
        self.save(extract=spec, max_price=35000)
        out = self.go()
        self.assertEqual((out["items"], out["matched"], out["baseline"]), (3, 3, True))
        self.assertEqual(self.candidates(), [])
        self.pages[SHOP] = ("text/html", shop_html(5))
        out = self.go()
        self.assertEqual(out["new"], 0)                          # 4 and 5: 40k, 50k > max_price
        self.pages[SHOP] = ("text/html", shop_html(3).replace("Kártya 3", "Kártya 3b")
                            .replace('href="/p/3"', 'href="/p/33"'))
        self.go()
        cands = self.candidates()
        self.assertEqual(len(cands), 1)
        self.assertEqual((cands[0]["url"], cands[0]["price_value"]),
                         ("https://shop.example.com/p/33", 30000))
        self.assertIn("garanciával", cands[0]["excerpt"])        # the item text goes to the judge
        def_card = [c for c in self.cards if c["type"] == "watch_def"][0]["payload"]
        self.assertNotIn("extract", def_card)                    # the card schema is unchanged

    def test_recipe_empty_after_items(self):
        self.save(extract={"mode": "list", "item": "li.card", "fields": {"title": "h3"}})
        self.go()
        self.pages[SHOP] = ("text/html", "<html><body><div class='new-layout'></div></body></html>")
        with self.assertRaises(watchlib.ParseError) as cm:
            self.go()
        self.assertEqual(cm.exception.code, "recipe_empty")
        err = [c for c in self.cards if c["type"] == "watch_run"][-1]["payload"]
        self.assertEqual((err["status"], err["error_code"]), ("error", "recipe_empty"))

    def test_recipe_change_restarts_baseline(self):
        self.save(extract={"mode": "list", "item": "li.card", "fields": {"title": "h3"}})
        self.go()
        self.save(extract={"mode": "list", "item": "li.card", "fields": {"title": "a"}})
        out = self.go()
        self.assertTrue(out["baseline"])
        self.assertEqual(self.candidates(), [])

    def test_robots_disallowed_run(self):
        self.save(extract={"mode": "list", "item": "li.card", "fields": {"title": "h3"}})
        f = FakeFetcher(self.pages, {"shop.example.com": "User-agent: *\nDisallow: /\n"})
        with self.assertRaises(watchlib.FetchError) as cm:
            watchlib.run_watch("bolt", fetcher=f, publish=self.cards.append, base=self.base)
        self.assertEqual(cm.exception.code, "robots_disallowed")
        self.assertNotIn(SHOP, f.calls)

    def test_condition_edges(self):
        self.pages = {PRODUCT: ("text/html", product_html("OutOfStock"))}
        cfg = watchlib.validate_config({"slug": "lack", "name": "LACK raktáron", "source": "page",
                                        "url": PRODUCT, "schedule": "every 4h",
                                        "extract": {"mode": "condition", "from": "ld_product",
                                                    "test": "in_stock"}})
        self.assertTrue(cfg["notify_existing"])
        watchlib.save_config(cfg, self.base)

        def run(avail):
            self.pages[PRODUCT] = ("text/html", product_html(avail))
            return watchlib.run_watch("lack", fetcher=FakeFetcher(self.pages),
                                      publish=self.cards.append, base=self.base)
        run("OutOfStock")
        self.assertEqual(self.candidates(), [])
        run("InStock")
        run("InStock")                                           # still true: quiet
        self.assertEqual(len(self.candidates()), 1)
        self.assertIn("InStock", self.candidates()[0]["title"])
        run("OutOfStock")
        run("InStock")                                           # true again: a new item
        ids = [c["item_id"] for c in self.candidates()]
        self.assertEqual(ids, ["cd:1", "cd:2"])

    def test_condition_true_on_first_run_is_news(self):
        self.pages = {PRODUCT: ("text/html", product_html("InStock"))}
        cfg = watchlib.validate_config({"slug": "lack", "name": "LACK", "source": "page", "url": PRODUCT,
                                        "schedule": "every 4h",
                                        "extract": {"mode": "condition", "from": "ld_product",
                                                    "test": "in_stock"}})
        watchlib.save_config(cfg, self.base)
        watchlib.run_watch("lack", fetcher=FakeFetcher(self.pages), publish=self.cards.append,
                           base=self.base)
        self.assertEqual(len(self.candidates()), 1)

    def test_rendered_watcher(self):
        self.save(extract={"mode": "list", "item": "li.card", "fields": {"title": "h3"}}, render=True)
        rend = FakeRenderer(shop_html(2))
        out = self.go(renderer=rend)
        self.assertEqual(out["items"], 2)
        self.assertEqual(rend.calls, [(SHOP, None, False)])

    def test_api_through_the_browser(self):
        spec = {"mode": "json", "from": "api", "api_url": API + "?page=1", "items": "$[*]",
                "fields": {"title": "name", "link": "html_url"}}
        self.save(extract=spec, render=True)
        rend = FakeRenderer(SHELL_HTML, [{"url": API + "?page=1", "bytes": 10, "body": api_json(2)}])
        out = self.go(renderer=rend)
        self.assertEqual(out["items"], 2)
        self.assertEqual(rend.calls, [(SHOP, None, True)])
        rend.json_responses = [{"url": API + "?page=2", "bytes": 10, "body": api_json(3)}]
        self.assertEqual(self.go(renderer=rend)["items"], 3)      # same path, other query
        rend.json_responses = []
        with self.assertRaises(recipes.RecipeError):
            self.go(renderer=rend)

    def test_config_rules(self):
        with self.assertRaises(watchlib.ConfigError):
            watchlib.validate_config({"slug": "x1", "name": "x", "source": "hardverapro",
                                      "url": "https://hardverapro.hu/aprok/x", "schedule": "every 4h",
                                      "render": True})
        with self.assertRaises(watchlib.ConfigError):        # page without words or recipe
            watchlib.validate_config({"slug": "x1", "name": "x", "source": "page", "url": SHOP,
                                      "schedule": "every 4h"})
        old = {"slug": "x1", "name": "x", "source": "page", "url": SHOP, "schedule": "every 4h",
               "include": ["rtx"]}
        before = watchlib.def_hash(watchlib.validate_config(old))
        self.assertEqual(before, watchlib.def_hash(watchlib.validate_config(dict(old))))


class FakeCron(watch_tools.Cron):
    def __init__(self):
        self.calls: list[dict] = []

    def parse_schedule(self, s):
        if s.startswith("every "):
            return {"kind": "interval", "minutes": 240}
        return {"kind": "cron", "expr": s}

    def call(self, **kw):
        self.calls.append(kw)
        if kw["action"] == "create":
            return {"success": True, "job_id": "00000000abcd", "next_run_at": "2026-10-28T08:00:00+01:00"}
        if kw["action"] == "list":
            return {"success": True, "jobs": []}
        return {"success": True}


class Manage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="manage-")
        self.base = os.path.join(self.tmp, "watches")
        self.pages = {SHOP: ("text/html", shop_html(4)), PRODUCT: ("text/html", product_html("OutOfStock"))}
        self.cards: list[dict] = []
        self.cron = FakeCron()
        self.m = watch_tools.WatchManager(self.cron, self.base, os.path.join(self.tmp, "scripts"),
                                          fetcher_factory=lambda slug: FakeFetcher(self.pages),
                                          publish=self.cards.append, renderer=NoRenderer())

    def call(self, **a):
        return json.loads(self.m.handle(a))

    def structure(self, url):
        return json.loads(watch_tools.handle_page_fetch({"url": url, "mode": "structure"},
                                                        fetcher_factory=lambda: FakeFetcher(self.pages),
                                                        renderer=NoRenderer(), base=self.base))

    def test_candidate_preview_create(self):
        out = self.structure(SHOP)
        self.assertTrue(out["success"], out)
        self.assertIn("watch_manage", out["how"])
        req = {"name": "Kártyák 35k alatt", "url": SHOP, "candidate": 1, "max_price": 35000,
               "schedule": "15 8,13,19 * * *"}
        out = self.call(action="create", **req)
        self.assertEqual(out["error"], "preview_required")
        out = self.call(action="preview", **req)
        self.assertTrue(out["success"], out)
        self.assertEqual((out["now"]["items"], out["now"]["matched"]), (4, 3))
        self.assertEqual(out["definition"]["extract"]["item"], "ul.products > li.card.product")
        self.assertIn("ask", out["note"])
        out = self.call(action="create", **req)
        self.assertTrue(out["success"], out)
        cfg = watchlib.load_config(out["slug"], self.base)
        self.assertEqual((cfg["source"], cfg["extract"]["mode"]), ("page", "list"))
        self.assertNotIn("render", cfg)
        out = self.call(action="create", **{**req, "max_price": 20000})     # a different definition
        self.assertEqual(out["error"], "preview_required")

    def test_condition_with_value_and_update_gate(self):
        self.structure(PRODUCT)
        req = {"name": "LACK 8000 alatt", "url": PRODUCT, "candidate": 2,
               "extract": {"value": 8000}, "schedule": "every 4h"}
        out = self.call(action="preview", **req)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["definition"]["extract"], {"mode": "condition", "from": "ld_product",
                                                        "test": "below", "value": 8000})
        self.assertFalse(out["now"]["condition_true_now"])
        slug = self.call(action="create", **req)["slug"]
        out = self.call(action="update", slug=slug, candidate=1)             # in_stock instead
        self.assertEqual(out["error"], "preview_required")
        self.assertTrue(self.call(action="preview", url=PRODUCT, candidate=1, name="x")["success"])
        out = self.call(action="update", slug=slug, candidate=1)
        self.assertTrue(out["success"], out)
        self.assertEqual(watchlib.load_config(slug, self.base)["extract"]["test"], "in_stock")
        out = self.call(action="update", slug=slug, schedule="every 6h")    # no recipe change
        self.assertTrue(out["success"], out)

    def test_placeholder_value_rejected(self):
        self.structure(PRODUCT)
        out = self.call(action="preview", name="x", url=PRODUCT, candidate=2)
        self.assertEqual(out["error"], "bad_config")                         # "<a price>"

    def test_recipe_finding_nothing_is_refused(self):
        out = self.call(action="preview", name="x", url=SHOP,
                        extract={"mode": "list", "item": "div.gone", "fields": {"title": "a"}})
        self.assertEqual(out["error"], "recipe_empty")

    def test_candidate_needs_structure(self):
        out = self.call(action="preview", name="x", url=PRODUCT, candidate=1)
        self.assertEqual(out["error"], "bad_config")


class RendererWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="renderer-")

    def fake(self, answer: str, exit_code: int = 0) -> recipes.Renderer:
        script = os.path.join(self.tmp, "fake_render.py")
        with open(script, "w") as f:
            f.write(f"import sys\nsys.stdout.write({answer!r})\nsys.exit({exit_code})\n")
        return recipes.Renderer(python=sys.executable, script=script,
                                lock_path=os.path.join(self.tmp, "lock"))

    def test_answers(self):
        r = self.fake(json.dumps({"ok": True, "final_url": SHOP, "status": 200, "html": "<p>x</p>"}))
        self.assertEqual(r.render(SHOP)["html"], "<p>x</p>")
        for answer, cls, code in (
                (json.dumps({"ok": False, "code": "blocked_url", "error": "nope"}), watchlib.BlockedURL, "blocked_url"),
                (json.dumps({"ok": False, "code": "render_timeout", "error": "slow"}), watchlib.FetchError, "render_timeout"),
                (json.dumps({"ok": False, "code": "Weird Code!", "error": "x"}), watchlib.FetchError, "render_failed"),
                ("not json", watchlib.FetchError, "render_failed")):
            with self.subTest(code=code), self.assertRaises(cls) as cm:
                self.fake(answer, 1).render(SHOP)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(watchlib.FetchError) as cm:
            recipes.Renderer(python="/nonexistent").render(SHOP)
        self.assertEqual(cm.exception.code, "render_unavailable")


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


@unittest.skipUnless(_chromium() and _playwright(), "needs Playwright and a Chromium")
class RendererLive(unittest.TestCase):
    """render_page.py for real, against two local servers: 'site.test' (mapped to one of them
    for the test) and an "internal" one that must never be reached."""

    @classmethod
    def setUpClass(cls):
        cls.hits: list[str] = []
        hits = cls.hits

        class Internal(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"SECRET")

            def log_message(self, *a):
                pass

        cls.internal = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Internal)
        ip = cls.internal.server_address[1]

        class Site(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/":
                    body = (
                        "<html><head><title>Shop</title></head><body><ul id='list'></ul><script>"
                        "fetch('/api/items').then(r=>r.json()).then(d=>{for(const i of d.items){"
                        "const li=document.createElement('li');li.className='card';"
                        "li.innerHTML='<a href=\"/p/'+i.id+'\"><h3>'+i.name+'</h3></a><span class=price>'"
                        "+i.price+' Ft</span>';document.getElementById('list').appendChild(li);}});"
                        f"fetch('http://127.0.0.1:{ip}/from-page').catch(e=>0);"
                        f"new Image().src='http://127.0.0.1:{ip}/img';</script></body></html>")
                    self._send(200, "text/html; charset=utf-8", body.encode())
                elif self.path == "/api/items":
                    self._send(200, "application/json", json.dumps({"items": [
                        {"id": i, "name": f"Item {i}", "price": 1000 * i} for i in range(1, 5)]}).encode())
                elif self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{ip}/redirected")
                    self.end_headers()
                else:
                    self._send(404, "text/plain", b"no")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        for s in (cls.internal, cls.site):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.port = cls.site.server_address[1]
        cls.env_before = {k: os.environ.get(k) for k in ("DELVE_CHROMIUM", "DELVE_RENDER_TEST_HOSTS")}
        os.environ["DELVE_CHROMIUM"] = _chromium()
        os.environ["DELVE_RENDER_TEST_HOSTS"] = f"site.test=127.0.0.1:{cls.port}"
        cls.tmp = tempfile.mkdtemp(prefix="render-live-")
        cls.r = recipes.Renderer(python=sys.executable, lock_path=os.path.join(cls.tmp, "lock"))

    @classmethod
    def tearDownClass(cls):
        for s in (cls.internal, cls.site):
            s.shutdown()
        for k, v in cls.env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_javascript_list_and_api(self):
        t = time.monotonic()
        out = self.r.render(f"http://site.test:{self.port}/", capture_json=True)
        self.assertLess(time.monotonic() - t, 60)
        items = recipes.extract_list(watchlib.parse_html(out["html"]), out["final_url"],
                                     {"item": "li.card", "fields": {"title": "h3", "link": "a@href"}})
        self.assertEqual([i["title"] for i in items], [f"Item {i}" for i in range(1, 5)])
        self.assertEqual(json.loads(out["json"][0]["body"])["items"][0]["name"], "Item 1")
        self.assertTrue(any(b.startswith("127.0.0.1:") for b in out["blocked"]))
        self.assertEqual(self.hits, [])

    def test_redirect_to_loopback_refused(self):
        with self.assertRaises(watchlib.BlockedURL):
            self.r.render(f"http://site.test:{self.port}/redir")
        with self.assertRaises(watchlib.BlockedURL):
            self.r.render(f"http://127.0.0.1:{self.port}/")
        self.assertEqual(self.hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
