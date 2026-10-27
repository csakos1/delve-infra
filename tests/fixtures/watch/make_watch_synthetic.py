#!/usr/bin/env python3
"""Generate the synthetic watcher fixtures in tests/fixtures/watch/.

Stand-ins for third-party pages, which are not stored in this repository:
  * ha-search-synthetic.html - a hardverapro.hu search result page,
  * ha-item-synthetic.html   - a hardverapro.hu ad page,
  * cc-films-synthetic.json  - the Cinema City "bookable films" API answer.

The markup mirrors the structure observed in 2026-10 (the parts the parsers rely on):
``li.media[data-uadid]`` rows with ``.uad-col-title h1 a``, ``.uad-price``, ``.uad-cities``
and ``.uad-col-info time``; a promoted row that repeats later in the list; a "Keresem"
(wanted) ad; a service ad that only matches loosely; the "<n> találat erre:" header; the ad
page's ``.uad-time-location``, ``.uad-details`` table and ``.uad-content`` text. All titles,
names, prices, places, ids and texts are invented.

Run from anywhere:  python3 -I tests/fixtures/watch/make_watch_synthetic.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent

ADS = [  # uadid, title, price, city, time, ribbon
    ("9100001", "Gainward GeForce RTX 4070 SUPER Ghost eladó, garanciával", "299 990 Ft",
     "IV. kerület", "Előresorolva", "Előresorolva"),
    ("9100002", "Palit RTX 4070 Super JetStream – játékra használt", "175 000 Ft",
     "XI. kerület", "ma 17:45", "Friss"),
    ("9100003", "MSI RTX 4070 SUPER Ventus 3X (bányászgépből)", "160 000 Ft",
     "Debrecen, Hajdúsámson", "ma 15:02", ""),
    ("9100004", "Keresek RTX 4070 Super kártyát", "Keresem", "Budapest", "tegnap 21:10", ""),
    ("9100005", "Videokártya pasztázás és padolás – RTX/GTX/RX", "997 Ft", "XI. kerület",
     "Előresorolva", "Előresorolva"),
    ("9100006", "Zotac Gaming GeForce RTX 3060 Twin Edge 12GB", "84 000 Ft",
     "Győr, Mosonmagyaróvár", "okt. 25. 10:31", ""),
]
ORDER = ["9100001", "9100005", "9100002", "9100003", "9100004", "9100001", "9100006"]


def row(uid):
    _, title, price, city, when, ribbon = next(a for a in ADS if a[0] == uid)
    slug = "".join(c if c.isalnum() else "_" for c in title.lower())[:50]
    href = f"https://hardverapro.hu/apro/{slug}/friss.html"
    rib = (f'<div class="uad-corner-ribbon uad-corner-ribbon-new"><span>{ribbon}</span></div>'
           if ribbon else "")
    return f"""
  <li class="media" data-uadid="{uid}">
    <div class="uad-col uad-col-image">
      <a class="uad-image" href="{href}">{rib}
        <img src="//cdn.example.invalid/dl/uad/{uid}/x.jpg/100" alt="" /></a>
    </div>
    <!--<div class="media-body">-->
    <div class="uad-col uad-col-title">
      <h1>
        <a href="{href}">{title}</a>
        <small class="uad-time">&ndash; <time>{when}</time></small>
      </h1>
      <div class="uad-price"><span class="text-nowrap">{price}</span></div>
    </div>
    <div class="uad-col uad-col-price">
      <div class="uad-price"><span class="text-nowrap">{price}</span></div>
    </div>
    <div class="uad-col uad-col-info">
      <div class="uad-user"><span class="uad-user-text"><a href="/tag/seller{uid[-2:]}.html?uadid={uid}">seller{uid[-2:]}</a>
        <span class="uad-rating uad-rating-positive">+12</span></span></div>
      <div class="uad-cities">{city}</div>
      <div class="uad-time"><time>{when}</time></div>
    </div>
  </li>"""


SEARCH = """<!DOCTYPE html>
<html lang="hu"><head><meta charset="utf-8">
<title>Keresés - HardverApró</title>
<script>var x = "<li class='media' data-uadid='1'>";</script>
</head><body>
<form action="/aprok/keres.php" method="get"><input name="stext" value="rtx"></form>
<div class="uad-list"><ul class="list-unstyled">
  <li><h3>7 találat erre: <b>rtx</b></h3></li>
{rows}
</ul></div>
<footer>HardverApró &copy; synthetic</footer>
</body></html>
"""

ITEM = """<!DOCTYPE html>
<html lang="hu"><head><meta charset="utf-8"><title>Palit RTX 4070 Super JetStream - HardverApró</title></head>
<body>
<h1>Palit RTX 4070 Super JetStream &ndash; játékra használt</h1>
<div class="uad-content-block uad-time-location align-items-center">
  <table><tr><td><b><a href="/tag/seller02.html?uadid=9100002">seller02</a></b> (aktív tag)</td></tr>
  <tr><td>+12 pozitív értékelés</td></tr></table>
</div>
<div class="uad-content-block">
  <div class="uad-time-location">
    <span><span class="fas fa-map-marker"></span> XI. kerület </span>
    <span><span class="fas fa-person-carry-box"></span> személyes átvétel vagy postázás </span>
  </div>
</div>
<div class="row no-gutters uad-details">
  <div class="col-md-4"><h2>175 000 Ft</h2></div>
  <div class="col-md-4 text-center"><table class="d-inline text-left">
    <tr><th>Állapot:</th><td>használt</td></tr>
    <tr><th>Szándék:</th><td>kínál</td></tr>
  </table></div>
</div>
<div class="rtif-content uad-content"><div class="mb-3 rtif-content">
  <p class="mgt0">Eladó a képen látható kártya. Csak játékra használtam, soha nem volt bányászva.<br/>
  Garancia 2027 márciusáig, számla megvan.</p>
  <p class="mgt1">Kérem, csak komoly érdeklődő írjon!</p>
</div><span class="dropdown"><a class="dropdown-toggle" href="javascript:;">Megosztás</a>
<div class="dropdown-menu"><a class="dropdown-item" href="https://share.example.invalid/">Share</a></div></span></div>
<script>var shareTitle = "ignore me";</script>
</body></html>
"""

FILMS = {"body": {"films": [
    {"id": "1001s2r", "name": "Kitalált Film Egy", "length": 101,
     "link": "https://www.cinemacity.hu/films/kitalalt-film-egy/1001s2r",
     "releaseDate": "2026-10-22T00:00:00", "attributeIds": ["2d", "original-lang-en"]},
    {"id": "1002s2r", "name": "Kitalált Film Kettő", "length": 95,
     "link": "https://www.cinemacity.hu/films/kitalalt-film-ketto/1002s2r",
     "releaseDate": "2026-11-05T00:00:00", "attributeIds": ["3d"]},
]}}


def main():
    (HERE / "ha-search-synthetic.html").write_text(
        SEARCH.format(rows="".join(row(u) for u in ORDER)), encoding="utf-8")
    (HERE / "ha-item-synthetic.html").write_text(ITEM, encoding="utf-8")
    (HERE / "cc-films-synthetic.json").write_text(
        json.dumps(FILMS, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
