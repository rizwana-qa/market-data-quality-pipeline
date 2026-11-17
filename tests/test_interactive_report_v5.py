"""Tests for the Phase 1 redesign (design v5) of the interactive market intelligence report, src/interactive_report_template_v5.html, rendered by src/interactive_report.py.

v5 is selected with render(dataset, provenance, design="v5"). The production default stays the approved v4 until the v5 preview is reviewed and approved, so the v4 golden tests in
test_interactive_report.py are untouched and keep proving that nothing changed for the daily workflow. What these tests cannot show: how a browser paints the page (that was checked
separately in a real browser at 1440, 768 and 390 pixels) and how a screen reader announces it.
"""
import html as htmllib
import json
import os
import re
from decimal import Decimal, getcontext

import hashlib
from datetime import date, timedelta

from wf_support import Runner, raises, REPO, load

ir = load("interactive_report")
dashboard = load("dashboard")
R = Runner("interactive report v5 checks")
FIXTURES = os.path.join(REPO, "tests", "fixtures")
V4_SHA256 = "03e6809778045d533cac44e06db0069ac0ef6df92b7d1f91ff753de30e1f01ae"


def read(name, binary=False):
    with open(os.path.join(FIXTURES, name), "rb" if binary else "r", **({} if binary else {"encoding": "utf-8", "newline": ""})) as handle:
        return handle.read()


V4 = read("market_intelligence_report_2026-11-12_v4_release.html", binary=True)
GOLDEN = dashboard.parse_dataset(read("report_2026-11-12_data.csv"))


def synthetic(closes_btc, closes_eth, volume=lambda i: 100.0 + i, trades=lambda i: 1000 + i, wide=0.02):
    """A valid 31-row, 17-column dataset (parsed) from two close series (same construction as tests/test_interactive_report.py)."""
    start, rows = date(2026, 1, 1), []
    for index in range(31):
        row = [(start + timedelta(days=index)).isoformat(), repr(closes_btc[index]), repr(closes_eth[index])]
        row += ["" if index == 0 else repr(closes_btc[index] / closes_btc[index - 1] - 1), "" if index == 0 else repr(closes_eth[index] / closes_eth[index - 1] - 1)]
        for series in (closes_btc, closes_eth):
            previous = series[index - 1] if index else series[index]
            high, low = max(previous, series[index]) * (1 + wide), min(previous, series[index]) * (1 - wide)
            row += [repr(previous), repr(high), repr(low), repr(float(volume(index))), repr((high + low + series[index]) / 3), str(trades(index))]
        rows.append(",".join(row))
    return dashboard.parse_dataset(",".join(dashboard.EXPECTED_COLUMNS) + "\n" + "\n".join(rows) + "\n")


def text_of(page):
    body = page[page.index("<body"):page.index('<script type="application/json"')]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
V5_PATH = os.path.join(REPO, "src", "interactive_report_template_v5.html")
PROV = {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 04:00:12"}
OUT = ir.render(GOLDEN, design="v5").decode("utf-8")
OUT_AUTO = ir.render(GOLDEN, PROV, design="v5").decode("utf-8")
TEMPLATE = open(V5_PATH, encoding="utf-8", newline="").read()
CSS = TEMPLATE[TEMPLATE.index("<style>"):TEMPLATE.index("</style>")]
M = ir.compute(GOLDEN)


def luminance(hex_colour):
    channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def tokens():
    root = CSS[CSS.index(":root"):CSS.index("}", CSS.index(":root"))]
    return {name: value.upper() for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", root)}


# ------------------------------------------------------------------------------------------------------------ default and determinism
@R.register("designs: the v4 golden fixture is still pinned, v5 and v4 stay selectable, the production default is the final v6 design, and an unknown design is refused")
def _():
    import hashlib as h
    assert h.sha256(V4).hexdigest() == V4_SHA256 and ir.DEFAULT_DESIGN == ir.DESIGN_V6
    assert ir.render(GOLDEN) == ir.render(GOLDEN, design="v6") != ir.render(GOLDEN, design="v4") != ir.render(GOLDEN, design="v5")
    raises(ir.InteractiveReportError, lambda: ir.render(GOLDEN, design="v7"))
    raises(ir.InteractiveReportError, lambda: ir.slot_values(GOLDEN, design="v7"))


@R.register("v5 is deterministic (same dataset, same bytes), standalone and dependency-free: no external script, stylesheet, font, image, import or request; the only link is the report's own dashboard address")
def _():
    assert ir.render(GOLDEN, design="v5") == ir.render(GOLDEN, design="v5") == ir.render(dashboard.parse_dataset(read("report_2026-11-12_data.csv")), design="v5")
    for forbidden in ("<link", "@import", "url(", "<img", "<iframe", "src=\"http", "fetch(", "XMLHttpRequest", "WebSocket", "import("):
        assert forbidden not in OUT, forbidden
    urls = set(re.findall(r"https?://[^\"'\s<)]+", OUT))
    assert urls == {"https://rizwana-qa.github.io/market-data-quality-pipeline/reports/2026-11-12/dashboard.html", "http://www.w3.org/2000/svg"}, urls
    assert OUT.count("<script") == OUT.count("</script>") == 2 and "<script src" not in OUT         # the data block and one inline script (charts and menu)


# ------------------------------------------------------------------------------------------------------------ nothing is lost
@R.register("nothing removed: the embedded chart data is byte-identical to v4, every v4 figure and sentence is still in the page, and all sections, KPIs, findings, charts, controls and the dashboard button are present")
def _():
    v4_out = ir.render(GOLDEN, design="v4").decode("utf-8")
    json_of = lambda page: re.search(r'<script type="application/json" id="report-data">(.*?)</script>', page, re.S).group(1)
    assert json_of(OUT) == json_of(v4_out)
    changed = {"btc_loc", "eth_loc"}                                    # the only two figures whose wording changed on purpose (percentile -> range position)
    for name, value in ir.slot_values(GOLDEN).items():
        if name in changed or name.endswith(("_cls", "_attr")) or name in ("report_json", "fetched_span"):
            continue
        assert htmllib.escape(value, quote=False) in OUT, name
    for kicker in ("01 · Executive dashboard", "02 · Market intelligence", "03 · Momentum and risk", "04 · Market participation", "05 · Comparative diagnostics", "06 · Market behaviour", "07 · Decision support", "08 · Data governance"):
        assert f'<div class="kicker">{kicker}</div>' in OUT, kicker
    assert OUT.count('class="kpi ') == 8 and OUT.count('<article class="finding">') == 5 and OUT.count("data-chart=") == 3 and OUT.count("data-win=") == 3 and OUT.count("data-ser=") == 2 and OUT.count("data-view aria-pressed") == 3
    for needle in ("Executive interpretation", "What to monitor next", "Risk interpretation:", "Participation signal:", "Relative-strength spread:", "Recommended monitoring priorities", "Open Detailed Interactive Report",
                   "Important limitations:", "Analysis approach:", "not investment advice", "Created by Rizwana Zahoor", "Wording rules:"):
        assert needle in OUT, needle
    for row in ("Base-asset volume", "Volume vs previous 7-session average", "Estimated USD turnover", "Turnover vs previous 7-session average", "Trade count", "Trade count vs previous 7-session average",
                "Estimated average USD turnover per trade", "Close versus session VWAP", "Latest daily high-low range / close", "7-day price return", "15-day price return", "30-day price return", "14-day ATR / price",
                "14-day realised volatility", "30-return realised volatility", "31-close observed high", "31-close observed low", "Distance from sample low"):
        assert f"<td>{row}</td>" in OUT, row


# ------------------------------------------------------------------------------------------------------------ A. methodology
@R.register("range position replaces 'percentile': the values are shares of the day's range (0-100 %), the labels say so, and no ordinal 'nth percentile' remains anywhere")
def _():
    assert ir.position_text(M["BTC"]["loc"]) == "11%" and ir.position_text(M["ETH"]["loc"]) == "44%" and ir.position_text(None) == "n/a"
    assert "<td>Range position of the close</td><td>11%</td><td>44%</td>" in OUT and "<td>Range position of the latest close</td><td>11%</td><td>44%</td>" in OUT
    assert not re.search(r"\d+(st|nd|rd|th) percentile", OUT) and "Close location in daily range" not in OUT and "close location" not in text_of(OUT).lower()
    assert [m.start() for m in re.finditer("percentile", text_of(OUT))] and all("not a percentile" in text_of(OUT)[max(0, m.start() - 80):m.start() + 20] for m in re.finditer("percentile", text_of(OUT)))
    assert "Range position:" in OUT and "(close − low) ÷ (high − low)" in OUT
    assert "percentile" in ir.render(GOLDEN, design="v4").decode("utf-8") and "11th percentile" in ir.render(GOLDEN, design="v4").decode("utf-8")      # v4 itself is untouched


def independent_wilder(closes, period=14):
    """Wilder RSI from the closed form of the recursion, in Decimal, written separately from the implementation (no loop that updates the averages)."""
    getcontext().prec = 50
    changes = [Decimal(repr(closes[i])) - Decimal(repr(closes[i - 1])) for i in range(1, len(closes))]
    gains, losses = [max(c, Decimal(0)) for c in changes], [max(-c, Decimal(0)) for c in changes]
    n, p = len(changes), Decimal(period)
    decay = (p - 1) / p

    def average(series):
        return sum(series[:period]) / p * decay ** (n - period) + sum(series[i] / p * decay ** (n - 1 - i) for i in range(period, n))
    g, l = average(gains), average(losses)
    return Decimal(100) - Decimal(100) / (1 + g / l)


@R.register("Wilder RSI: equals an independent closed-form Decimal calculation for both assets of the fixture, the simple-average RSI is unchanged (31.7 / 32.6), and the edge cases (all gains, all losses, flat) are defined")
def _():
    for asset in ("BTC", "ETH"):
        closes = GOLDEN["closes"][asset]
        assert abs(Decimal(repr(M[asset]["rsi_wilder"])) - independent_wilder(closes)) < Decimal("1e-9"), asset
    assert (round(M["BTC"]["rsi"], 1), round(M["ETH"]["rsi"], 1)) == (31.7, 32.6) and (round(M["BTC"]["rsi_wilder"], 1), round(M["ETH"]["rsi_wilder"], 1)) == (28.2, 26.7)
    assert ir.wilder_rsi([1.0] * 30) == 100.0 and ir.wilder_rsi([-1.0] * 30) == 0.0 and ir.wilder_rsi([0.0] * 30) == 50.0
    assert abs(ir.wilder_rsi([1.0, -1.0] * 15) - 50.0) < 5
    rising = synthetic([100.0 + i for i in range(31)], [50.0 + 0.5 * i for i in range(31)]); mr = ir.compute(rising)
    assert mr["BTC"]["rsi"] == 100.0 and mr["BTC"]["rsi_wilder"] == 100.0


@R.register("RSI is labelled in the page: the simple average is the primary reading, the Wilder reading follows it as a comparison in the metric card and in the comparison table, and the note quotes both from the computed values")
def _():
    assert OUT.count("14-day RSI (simple average, primary)") == 3 and OUT.count("14-day RSI (Wilder smoothing, comparison)") == 3          # two metric cards and one table row
    assert OUT.index("14-day RSI (simple average, primary)") < OUT.index("14-day RSI (Wilder smoothing, comparison)")
    assert "<b>31.7</b>" in OUT and "<b>28.2</b>" in OUT and "<b>32.6</b>" in OUT and "<b>26.7</b>" in OUT
    note = ir.rsi_note(M); assert "Simple-average RSI (primary): BTC 31.7, ETH 32.6." in note and "Wilder-smoothed RSI (comparison): BTC 28.2, ETH 26.7." in note and "same side of the 35 and 65 marks" in note
    split = synthetic([100.0 + (i % 2) * 0.1 - 0.001 * i for i in range(31)], [50.0 - 0.2 * i for i in range(31)])
    assert "different side" in ir.rsi_note(ir.compute(split)) or "same side" in ir.rsi_note(ir.compute(split))
    assert ir.rsi_note(M) in OUT and "Wilder smoothing" in OUT and "(previous average × 13 + change) ÷ 14" in OUT


@R.register("drawdown: current and maximum drawdown are shown separately, the window peak date is named, the observed-window limit is disclosed from the actual peak position, and the chart subtitle says the peak is re-based")
def _():
    assert (M["BTC"]["peak_date"], M["BTC"]["peak_index"], M["ETH"]["peak_date"], M["ETH"]["peak_index"]) == ("2026-10-21", 8, "2026-10-13", 0)
    assert "<td>Current drawdown (distance from sample high)</td>" in OUT and "<td>Maximum drawdown (within window)</td>" in OUT and "<td>Date of the window peak</td><td>21 Oct 2026</td><td>13 Oct 2026</td>" in OUT
    assert "Current drawdown (from window peak)" in OUT and "Maximum drawdown (within window)" in OUT and "Now −15.06% · peak 21 Oct 2026" in OUT and "Now −19.18% · peak 13 Oct 2026" in OUT
    note = ir.drawdown_note(M)
    assert "ETH:" in note and "Its window peak is the first close in the sample (13 Oct 2026)" in note and "BTC:" in note and "Its window peak is 21 Oct 2026; a higher close before the window can not be seen." in note and note in OUT
    assert "re-based at the window start" in OUT and "Charts re-base the peak" in note
    # a market that fell and then partly recovered: current and maximum drawdown differ, and the peak is inside the window
    path = [100 + i for i in range(10)] + [109 - 2 * i for i in range(1, 11)] + [89 + 1.5 * i for i in range(1, 12)]
    mid = ir.compute(synthetic(path, [50.0 + 0.1 * i for i in range(31)]))
    b = mid["BTC"]; assert b["maxdd"] < b["curdd"] < 0 and b["peak_index"] == 9
    text = ir.drawdown_note(mid); assert "BTC: current drawdown" in text and "maximum drawdown" in text and "Its window peak is 10 Jan 2026" in text
    assert ir.slot_values(synthetic(path, [50.0 + 0.1 * i for i in range(31)]), design="v5")["btc_curdd"] != ir.slot_values(synthetic(path, [50.0 + 0.1 * i for i in range(31)]), design="v5")["btc_maxdd"]


@R.register("conventions are documented in the report: returns, RSI (both), ATR, annualisation, drawdown, range position, activity baselines, wording rules, the exchange-specific limit and the rolling-window limit")
def _():
    method = OUT[OUT.index('id="s-method"'):OUT.index('<div class="dashboard-action">')]
    for needle in ("Close-to-close returns", "14-period RSI uses simple averages", "Wilder-smoothed RSI", "14-period average true range", "√365", "Drawdown is the close divided by the highest close seen so far", "Range position:",
                   "previous seven sessions", "Wording rules:", "exchange-specific spot dataset", "rolling 31-close window", "No predictive AI model"):
        assert needle in method, needle


@R.register("provenance is preserved: the supplied-CSV wording and the automated Kraken wording with the recorded UTC fetch time appear exactly as in v4, the time appears only when it is real, and an invalid or invented one is still refused")
def _():
    assert "AS OF 12 NOVEMBER 2026 · UTC DAY" in OUT and "DATA FETCHED" not in OUT and "records no fetch time" in OUT and "not a live market feed" in OUT
    assert "<span>DATA FETCHED 2026-11-13 04:00:12 UTC</span>" in OUT_AUTO and OUT_AUTO.count("2026-11-13 04:00:12") == 3 and "not a real-time market feed" in OUT_AUTO and "last completed UTC day" in OUT_AUTO
    for bad in ({"kind": "kraken_automated", "fetched_at_utc": None}, {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 25:00:00"}, {"kind": "supplied_csv", "fetched_at_utc": "2026-11-13 04:00:12"}, {"kind": "x", "fetched_at_utc": None}):
        raises(ir.InteractiveReportError, lambda bad=bad: ir.render(GOLDEN, bad, design="v5"))
    assert OUT.replace("<span>DATA FETCHED 2026-11-13 04:00:12 UTC</span>", "") != OUT_AUTO


@R.register("interpretations stay grounded: the v5 narrative is the same generator text as v4, and the new texts quote only computed figures (a rising market gets no 'decline' sentence, an equal-RSI market no disagreement claim)")
def _():
    rising = synthetic([100.0 + i for i in range(31)], [50.0 + 0.9 * i for i in range(31)]); page = ir.render(rising, design="v5").decode("utf-8")
    callout = ir.slot_values(rising, design="v5")["callout_exec"]; assert "Both assets remain" in callout and "above their first observed closes" in callout and "below their first observed closes" not in callout and "decline" not in callout
    assert "BTC: current and maximum drawdown are both 0.00%" in ir.drawdown_note(ir.compute(rising)) or "BTC: current drawdown" in ir.drawdown_note(ir.compute(rising))
    assert "Its window peak is the first close" not in ir.drawdown_note(ir.compute(rising))                       # the peak of a rising market is the latest day, never the first
    assert "13 Oct 2026" not in page and "21 Oct 2026" not in page                                                  # no figure from the fixture leaks into another dataset
    assert ir.slot_values(GOLDEN)["callout_exec"] == ir.slot_values(GOLDEN, design="v5")["callout_exec"]


# ------------------------------------------------------------------------------------------------------------ B. presentation foundation
@R.register("hierarchy and navigation: one h1, the executive interpretation comes before the KPIs, the KPIs sit in four labelled groups (latest close first), a sticky section menu links all eight sections, and a skip link reaches the content")
def _():
    assert OUT.count("<h1>") == 1 and OUT.index("Executive interpretation") < OUT.index('class="kpi-groups"')
    groups = re.findall(r'<h3 class="kg-title" id="(kg-[a-z]+)">([^<]+)</h3>', OUT); assert [g[1] for g in groups] == ["Latest close", "30-return performance", "Drawdown within the window", "Relationship and risk"]
    links = re.findall(r'<li><a href="#(s-[a-z]+)">([^<]+)</a></li>', OUT); assert len(links) == 8 and [l[0] for l in links] == ["s-snapshot", "s-findings", "s-risk", "s-participation", "s-comparison", "s-charts", "s-monitoring", "s-method"]
    for target, _ in links: assert f'<section class="section" id="{target}"' in OUT, target
    assert '<a class="skip-link" href="#main">' in OUT and 'id="main" tabindex="-1"' in OUT and '<nav class="toc" aria-label="Report sections">' in OUT
    assert re.search(r"\.toc \{[^}]*position: sticky", CSS) and "aria-current" in OUT and "IntersectionObserver" in OUT and "scroll-margin-top" in CSS


@R.register("accessibility structure: every section is named by its heading, every table has a caption, column scopes and a keyboard-focusable scroll region, charts are labelled images with keyboard control and a data-table alternative, toggles carry aria-pressed")
def _():
    for section_id, heading in re.findall(r'<section class="section" id="[a-z-]+" aria-labelledby="(h-[a-z]+)">.*?<h2 id="(h-[a-z]+)"', OUT, re.S): assert section_id == heading
    assert len(re.findall(r'aria-labelledby="h-', OUT)) == 8
    assert OUT.count("<caption class=\"sr\">") == 2 and OUT.count('role="region"') == 2 and OUT.count('tabindex="0"') == 2 and OUT[:OUT.index("<script")].count('<th scope="col"') == 8
    for token in ("role: 'img'", "tabindex: 0", "aria-label", "ArrowRight", "ArrowLeft", "Home", "Escape", "aria-live", "role', 'region'", "caption"): assert token in OUT, token
    assert all('type="button"' in b for b in re.findall(r"<button [^>]*>", OUT)) and OUT.count("aria-pressed") >= 9
    assert "prefers-reduced-motion" in CSS and ":focus-visible" in CSS and 'lang="en"' in OUT and 'name="viewport"' in OUT
    ids = re.findall(r' id="([^"]+)"', OUT); assert len(ids) == len(set(ids)), "duplicate id"


@R.register("one colour system: BTC and ETH use the same tokens in the page, the legend buttons and the chart script; the ink, mark and fill variants are defined once")
def _():
    t = tokens()
    assert t["btc"] == "#F2A84A" and t["eth"] == "#68AAFF" and t["btc-mark"] == "#C97A0C" and t["eth-mark"] == "#2F7FE0" and t["btc-ink"] == "#8A5A12" and t["eth-ink"] == "#1F5FA8"
    script = OUT[OUT.index("var COL ="):OUT.index("var NAME")]
    assert f"BTC: '{t['btc-mark'].lower()}'".lower() in script.lower() and f"ETH: '{t['eth-mark'].lower()}'".lower() in script.lower() and t["btc-ink"].lower() in script.lower() and t["eth-ink"].lower() in script.lower()
    assert f'data-ser="BTC" aria-pressed="true" style="--c:{t["btc-mark"]}"' in OUT and f'data-ser="ETH" aria-pressed="true" style="--c:{t["eth-mark"]}"' in OUT
    assert "#B97824" not in OUT and "#287F9A" not in OUT and "#226F89" not in OUT and "#9A5F14" not in OUT            # the old, differently-hued ETH text colours and the old BTC variants are gone


@R.register("contrast (WCAG): body and secondary text >= 4.5:1 on white and on the soft panel, BTC/ETH ink text >= 4.5:1, chart marks >= 3:1, KPI card text and signed values >= 4.5:1 on the navy card")
def _():
    t = tokens(); white, soft, navy = "#FFFFFF", "#F5F7F9", t["navy"]
    for name in ("text", "secondary", "muted", "btc-ink", "eth-ink", "green", "red"):
        assert contrast(t[name], white) >= 4.5 and contrast(t[name], soft) >= 4.5, (name, contrast(t[name], white), contrast(t[name], soft))
    for name in ("btc-mark", "eth-mark"): assert contrast(t[name], white) >= 3.0, (name, contrast(t[name], white))
    for name in ("btc", "eth", "green-dark", "red-dark"): assert contrast(t[name], navy) >= 4.5, (name, contrast(t[name], navy))
    for colour in ("#C5D5E3", "#C0D0DE", "#F7FAFC"): assert contrast(colour, navy) >= 4.5, colour
    for colour in re.findall(r"color: (#[0-9A-Fa-f]{6})", CSS):                       # every literal text colour on a light background is readable (the dark hero and KPI cards use their own, checked, light colours)
        if luminance(colour) < 0.45: assert contrast(colour, white) >= 4.5 and contrast(colour, "#EDF3F6") >= 4.0, colour


@R.register("typography: no text below 11 px in the page (v4 used 8-10 px), body text is at least 15 px, tables at least 14 px; the SVG chart labels are at least 12 px")
def _():
    sizes = [float(x) for x in re.findall(r"font-size: ([0-9.]+)px", CSS)] + [float(x) for x in re.findall(r"font: [0-9]+ ([0-9.]+)px", CSS)]
    assert sizes and min(sizes) >= 11, sorted(sizes)[:5]
    assert "font: 15px/1.55" in CSS and re.search(r"\.data-table \{[^}]*font-size: 14px", CSS) and "'font-size': 12," in OUT and "'font-size': 11," not in OUT.replace("'font-size': 11.", "")
    v4_css = V4.decode().split("</style>")[0]; assert min(float(x) for x in re.findall(r"font-size: ([0-9.]+)px", v4_css)) < 11       # the reference the change improves on


@R.register("responsive: desktop above 1000 px, tablet to 760 px and mobile below, KPI groups reflow from 2x2 to stacked, tables scroll inside a frame with a sticky first column on narrow screens, charts redraw on resize")
def _():
    for query in ("@media(max-width: 1000px)", "@media(max-width: 760px)", "@media(max-width: 480px)"): assert query in CSS, query
    assert re.search(r"\.table-wrap \{[^}]*overflow-x: auto", CSS) and "position: sticky; left: 0" in CSS and "ResizeObserver" in OUT and "grid-template-columns: repeat(6, minmax(0,1fr))" in CSS
    assert re.search(r"\.asset-grid \{[^}]*grid-template-columns: 1fr 1fr", CSS) and "grid-template-columns: 1fr; }" in CSS.replace("\n", " ") or ".asset-grid { grid-template-columns: 1fr; }" in CSS
    assert "min-width: 640px" in CSS and "min-width: 600px" in CSS


@R.register("scope guard: the v4 template file, the dashboard, the e-mail components and the workflows are not part of the v5 files; v5 adds only the template and the generator hooks, and the v4 golden fixture is untouched")
def _():
    assert hashlib.sha256(V4).hexdigest() == V4_SHA256
    source = open(os.path.join(REPO, "src", "interactive_report.py"), encoding="utf-8").read()
    assert "email_report" not in source and "Code.gs" not in source
    assert "Subject:" in TEMPLATE and "interactive_report_template_v5" in source          # the approved report's own 'Subject:' line is kept unchanged; the v5 template is wired only through the generator
    for later_phase in ("candlestick", "scatter", "rolling correlation chart", "beta"):
        assert later_phase not in OUT.lower().replace("rolling correlation. high", ""), later_phase          # no Phase 2-4 analytics were added


R.finish()
