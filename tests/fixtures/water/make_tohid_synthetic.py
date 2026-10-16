#!/usr/bin/env python3
"""Generate tests/fixtures/water/tohid-synthetic.html.

A synthetic stand-in for the OVSZ hydroinfo lake table
(https://www.hydroinfo.hu/tables/ENG/tohid.html), which is third-party
content and therefore not stored in this repository.

The markup mirrors the real page's structure as observed in 2026-10, including
its quirks, because those are what the parser has to survive:
  * CRLF line endings, ASCII only, HTML entities (named and numeric),
  * "Observed on: <d> <Month>  <yyyy>" with a double space,
  * two header rows whose opening <tr> is missing,
  * an empty spacer row, cells padded with stray spaces in the tag,
  * the 24 h change wrapped in <b>, sometimes inside <font color=...>,
  * "//" for missing data, &nbsp; in the footer.
Station names, values and the date are invented. Only the two anchor station
codes the script relies on (442550 Siofok, 442600 Balaton average) are real.

Run from anywhere:  python3 -I tests/fixtures/water/make_tohid_synthetic.py
"""
from pathlib import Path

OUT = Path(__file__).with_name("tohid-synthetic.html")
FONT = '<font face="Verdana, Arial, Helvetica, sans-serif" size="{size}"{color}>'
OBSERVED = "3 June  2025"   # double space as on the real page


def font(text, size=1, color=None):
    c = f' color="{color}"' if color else ""
    return FONT.format(size=size, color=c) + text + "</font>"


def td(attrs, text, align="center", size=1, color=None):
    return f'<td {attrs}><div align="{align}">{font(text, size, color)}</div></td>'


def station(code, name, lake, ym, ye, tm, ch, temp, ice, color=None):
    pad = lambda v: '  ' if v != "//" else ''   # noqa: E731  (real page: "  >" on numeric cells)
    return [
        "<tr>",
        td('width="75" height="16"', code),
        td('width="140" height="16"', name),
        td('width="100" height="16"', lake),
        f'<td width="45" height="16"{pad(ym)}>' + f'<div align="center">{font(ym)}</div></td>',
        f'<td width="45" height="16"{pad(ye)}>' + f'<div align="center">{font(ye)}</div></td>',
        f'<td width="45" height="16"{pad(tm)}>' + f'<div align="center">{font(tm)}</div></td>',
        td('width="58" height="16"', f"<b>{ch}</b>", color=color),
        td('width="73" height="16"', temp),
        td('width="91" height="16"', ice),
        "</tr>",
    ]


lines = [
    "<html>", "<body>",
    '<table border="3" cellpadding="0" cellspacing="0"  align="center">',
    "<tr>",
    td('colspan="9" height="16"', "<strong>Hydrological data</strong>", size=2),
    "</tr>", "<tr>",
    td('colspan="9" height="16"', f"Observed on: {OBSERVED}"),
    "</tr>", "<tr>",
    td('width="75" rowspan="3"', "<b>Station code</b>"),
    td('width="140" rowspan="3"', "<b>Station name</b>"),
    td('width="100" rowspan="3"', "<b>Lake name</b>"),
    td('colspan="3" height="16"', "<b>Water level [cm]</b>"),
    td('rowspan="3" width="58"', "<b>Change within 24 hours [cm]</b>"),
    td('rowspan="3" width="73"', "<b>Morning water temperature [&deg;C]</b>"),
    td('rowspan="3" width="91"', "<b>Morning ice conditions</b>"),
    "</tr>",
    # quirk: no opening <tr> for the next two header rows
    td('colspan="2" height="16"', "yesterday"),
    td('width="45" height="16" ', "today"),
    "</tr>",
    td('width="45" height="16"', "morning"),
    td('width="45" height="16"', "evening"),
    td('width="45" height="16"', "morning"),
    "</tr>",
    '<tr><td colspan="9"></td> </tr>',
]
# Non-anchor rows (must be ignored), incl. a numeric entity and a negative level.
lines += station("900101", "P&eacute;ldafalu-&#337;rh&aacute;z", "Example lake",
                 "-6", "-5", "-6", "0", "18.2", "//")
lines += station("442550", "Si&oacute;fok", "Balaton",
                 "58", "57", "55", "-3", "21.4", "//", color="#003399")
lines += station("900102", "Pr&oacute;ba r&eacute;v", "Balaton",
                 "54", "54", "53", "-1", "//", "//", color="#003399")
lines += station("442600", "Balaton average", "Balaton",
                 "54", "//", "52", "-2", "//", "//", color="#003399")
lines += [
    "<tr>" + td('colspan="9" height="16"', "&nbsp;&nbsp;&nbsp;synthetic test fixture, not real data",
                align="left") + "</tr>",
    "</table>", "</body>", "</html>", "",
]

data = "\r\n".join(lines) + "\r\n"
data.encode("ascii")  # the real page is pure ASCII; fail loudly otherwise
OUT.write_bytes(data.encode("ascii"))
print(f"wrote {OUT} ({len(data)} bytes)")
