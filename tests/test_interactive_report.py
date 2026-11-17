"""Tests for src/interactive_report.py: the interactive market intelligence report generated from one validated CSV.

The approved v4 report (tests/fixtures/market_intelligence_report_2026-11-12_v4_release.html, SHA-256 pinned below) is the frozen design reference. Its 31-day source data is
tests/fixtures/report_2026-11-12_data.csv, rebuilt on 2026-11-14 from Kraken's public daily candles with the project's own fetch and CSV code (it is not the original upload; the 31 closes
of both assets equal the closes embedded in the v4 report, and the 12 November volumes and trade counts equal the v4 text). Nothing here uses the network or a secret.
"""
import difflib, hashlib, json, os, re
from datetime import date, timedelta

from wf_support import Runner, raises, REPO, load

da = load("daily_automation")
ir = load("interactive_report")
# This file pins the approved v4 design (the golden fixture). The production default is now the final v6 design (tests/test_interactive_report_v6.py), so these checks call the generator with design v4.
_render_any_design = ir.render
ir.render = lambda dataset, provenance=None, design=ir.DESIGN_V4: _render_any_design(dataset, provenance, design)
dashboard = load("dashboard")
R = Runner("interactive report checks")
FIXTURES = os.path.join(REPO, "tests", "fixtures")
V4_SHA256 = "03e6809778045d533cac44e06db0069ac0ef6df92b7d1f91ff753de30e1f01ae"


def read(name, binary=False):
    with open(os.path.join(FIXTURES, name), "rb" if binary else "r", **({} if binary else {"encoding": "utf-8", "newline": ""})) as handle:
        return handle.read()


V4 = read("market_intelligence_report_2026-11-12_v4_release.html", binary=True)
GOLDEN = dashboard.parse_dataset(read("report_2026-11-12_data.csv"))
V4_LINES = V4.decode("utf-8").split("\n")


def changed_blocks(out):
    """The blocks in which a rendered report differs from the approved v4 file, as difflib opcodes (tag, v4 start, v4 end, new start, new end)."""
    return [op for op in difflib.SequenceMatcher(None, V4_LINES, out.split("\n"), autojunk=False).get_opcodes() if op[0] != "equal"]


COMMITTED = {name: os.path.join(REPO, "reports", name) for name in ("report_2026-10-27.md", "report_2026-10-27_data.csv")}


def synthetic(closes_btc, closes_eth, volume=lambda i: 100.0 + i, trades=lambda i: 1000 + i, wide=0.02):
    """A valid 31-row, 17-column dataset (parsed) from two close series; open = previous close, high/low a fixed share around the open and close, VWAP the mean of high, low and close."""
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


def rendered_text(dataset):
    return ir.render(dataset).decode("utf-8")


def text_of(page):
    body = page[page.index("<body"):page.index('<script type="application/json"')]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


FALLING = synthetic([100.0 - 1.0 * i for i in range(31)], [50.0 - 0.4 * i for i in range(31)])
RISING = synthetic([100.0 + 1.0 * i for i in range(31)], [50.0 + 0.9 * i for i in range(31)])
MIXED = synthetic([100.0 + 1.0 * i for i in range(31)], [50.0 - 0.4 * i for i in range(31)])


@R.register("golden: the approved v4 file is the pinned reference (SHA-256); the generator reproduces it byte for byte EXCEPT in exactly three regions (the UTC-day label, the source note and the added wording-rules paragraph), for a manually supplied CSV")
def _():
    assert hashlib.sha256(V4).hexdigest() == V4_SHA256
    out = ir.render(GOLDEN).decode("utf-8")
    ops = changed_blocks(out)
    assert [(tag, i1 + 1, i2) for tag, i1, i2, j1, j2 in ops] == [("replace", 829, 829), ("replace", 1095, 1095), ("insert", 1099, 1098)], ops
    new_lines = out.split("\n")
    assert new_lines[828].strip() == "<span>AS OF 12 NOVEMBER 2026 \u00b7 UTC DAY</span>"
    assert "Supplied CSV representing Kraken spot OHLC daily records for XBT/USD and ETH/USD. The file contains 31 daily observations and 30 aligned return observations." in new_lines[1094]
    assert new_lines[1098].lstrip().startswith('<p class="notes"><strong>Wording rules:</strong>')
    assert "This report contains data for 12 November 2026 and is based on the supplied historical CSV. It is not a live market feed." in out     # the approved v4 footer is kept for a supplied CSV


@R.register("design unchanged: for a supplied CSV and for an automated Kraken fetch every line outside the wording regions is byte-identical to approved v4 - so the CSS, both scripts, the layout, the charts, KPI cards, tables and controls are untouched")
def _():
    prov = {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 04:00:12"}
    for label, provenance, allowed in (("supplied", None, {829, 1095, 1099}), ("automated", prov, {829, 831, 832, 1095, 1099, 1112})):
        out = ir.render(GOLDEN, provenance).decode("utf-8")
        changed = {i + 1 for tag, i1, i2, j1, j2 in changed_blocks(out) for i in (range(i1, i2) if i2 > i1 else [i1])}
        assert changed <= allowed, (label, sorted(changed - allowed))
        for marker_a, marker_b in (("<style>", "</style>"), ('<script>\n(function', "</script>")):          # the stylesheet and the chart script, byte for byte
            va, vb = V4.decode().index(marker_a), V4.decode().index(marker_b, V4.decode().index(marker_a))
            oa, ob = out.index(marker_a), out.index(marker_b, out.index(marker_a))
            assert V4.decode()[va:vb] == out[oa:ob], (label, marker_a)
        for token in ('class="kpi-grid"', 'class="chart-grid"', 'data-win="7"', 'data-ser="BTC"', 'class="plot-wrap"', 'class="dashboard-action"', "@media(max-width: 760px)", "@media(max-width: 480px)"):
            assert out.count(token) == V4.decode().count(token), (label, token)
    template = open(ir.TEMPLATE_PATH, encoding="utf-8", newline="").read()
    assert "<script>" in template and template.count("{{") == len(re.findall(r"\{\{[a-z0-9_]+\}\}", template))


@R.register("golden: the output is deterministic - the same dataset gives the same bytes on every call, and no clock, random value or environment is read")
def _():
    assert ir.render(GOLDEN) == ir.render(GOLDEN) == ir.render(dashboard.parse_dataset(read("report_2026-11-12_data.csv")))
    import ast
    tree = ast.parse(open(os.path.join(REPO, "src", "interactive_report.py"), encoding="utf-8").read())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"hashlib", "html", "json", "math", "re", "statistics", "pathlib", "analysis", "dashboard", "datetime"}, imported        # no random, environment or network module
    clock_calls = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today", "time", "monotonic", "perf_counter", "environ", "getenv")]
    assert not clock_calls, clock_calls          # datetime is imported only to check that a RECORDED fetch time is a real date; the clock is never read


@R.register("metrics: every figure of the approved report is reproduced from the CSV with the documented definitions (returns, RSI, ATR, volatility, turnover, ratios, VWAP, close location, drawdown, correlation)")
def _():
    m = ir.compute(GOLDEN)
    expect = {"BTC": dict(r7=-6.07, r15=-12.20, r30=-14.12, rsi=31.7, atr=2397.36, atr_pct=3.36, vol14=43.44, vol30=36.30, volratio=1.52, turnratio=1.46, traderatio=1.15, vwaprel=-0.90, range=4.33, maxdd=-15.06),
              "ETH": dict(r7=-9.75, r15=-14.43, r30=-19.18, rsi=32.6, atr=97.59, atr_pct=4.84, vol14=63.61, vol30=49.05, volratio=0.82, turnratio=0.76, traderatio=1.07, vwaprel=0.01, range=4.08, maxdd=-19.18)}
    for asset, figures in expect.items():
        for key, value in figures.items():
            actual = m[asset][key]
            shown = actual if key in ("rsi", "atr", "volratio", "turnratio", "traderatio") else actual * 100
            assert round(shown, 1 if key == "rsi" else 2) == value, (asset, key, shown, value)
    assert round(m["pair"]["corr"], 2) == 0.92 and round(m["pair"]["vol_ratio"], 2) == 1.35
    assert round(m["BTC"]["turnover"] / 1e6, 2) == 298.60 and round(m["ETH"]["turnover"] / 1e6, 2) == 52.17 and round(m["BTC"]["notional"]) == 1725 and round(m["ETH"]["notional"]) == 955
    assert ir.ordinal(m["BTC"]["loc"]) == "11th" and ir.ordinal(m["ETH"]["loc"]) == "44th"


@R.register("metric definitions on hand-checkable data: RSI 100 when nothing fell, ATR from the true range, ratio 1.00 for constant volume, maximum drawdown 0 for a rising series, and a plain-average baseline of the 7 days before")
def _():
    m = ir.compute(RISING)["BTC"]
    assert m["rsi"] == 100.0 and m["maxdd"] == 0.0 and m["volratio"] > 1 and m["distlow"] > 0
    flat = ir.compute(synthetic([100.0 + (i % 2) for i in range(31)], [50.0 + (i % 3) for i in range(31)], volume=lambda i: 10.0, trades=lambda i: 500))["BTC"]
    assert round(flat["volratio"], 12) == 1.0 and round(flat["traderatio"], 12) == 1.0
    ds = synthetic([100.0 + (i % 2) for i in range(31)], [50.0 + (i % 3) for i in range(31)])
    btc = ir.compute(ds)["BTC"]
    high, low, close = [float(r[6]) for r in ds["rows"]], [float(r[7]) for r in ds["rows"]], ds["closes"]["BTC"]
    trs = [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(17, 31)]
    assert abs(btc["atr"] - sum(trs) / 14) < 1e-9


@R.register("consistency: the visible figures and the embedded chart data come from the same numbers (latest closes, 30-day return and maximum drawdown agree with the JSON)")
def _():
    page = rendered_text(GOLDEN)
    data = json.loads(re.search(r'id="report-data">(.*?)</script>', page, re.S).group(1))
    assert set(data) == {"7", "14", "30"}
    for asset in ("BTC", "ETH"):
        series = data["30"]["close_usd"][asset]
        assert ("$" + format(series[-1], ",.2f")) in page
        r30 = series[-1] / series[0] - 1
        shown = ir.pct(r30, signed=True)
        assert shown in page and abs(data["30"]["index"][asset][-1] - 100 * (1 + r30)) < 1e-3
        assert abs(min(data["30"]["drawdown_pct"][asset]) / 100 - ir.compute(GOLDEN)[asset]["maxdd"]) < 1e-4
    assert len(data["7"]["dates"]) == 8 and len(data["14"]["return_dates"]) == 14 and data["30"]["dates"][-1] == "2026-11-12"


@R.register("template safety: a missing slot, an unused value, an unresolved placeholder in a value and an invalid class are all refused")
def _():
    values = ir.slot_values(GOLDEN)
    template = open(ir.TEMPLATE_PATH, encoding="utf-8", newline="").read()
    raises(ir.InteractiveReportError, lambda: ir.render_template(template.replace("{{corr}}", "{{unknown_slot}}"), values), "unknown_slot")
    raises(ir.InteractiveReportError, lambda: ir.render_template(template, dict(values, extra_value="x")), "extra_value")
    raises(ir.InteractiveReportError, lambda: ir.render_template(template, dict(values, corr="{{leftover}}")), "unresolved placeholder")
    raises(ir.InteractiveReportError, lambda: ir.render_template(template, dict(values, btc_r30_cls=" evil")), "unexpected class")
    raises(ir.InteractiveReportError, lambda: ir.render_template(template, dict(values, btc_r7_attr=' onclick="x"')), "unexpected class")


@R.register("escaping: text slots are HTML-escaped, and the embedded JSON can never end its script element")
def _():
    template = open(ir.TEMPLATE_PATH, encoding="utf-8", newline="").read()
    page = ir.render_template(template, dict(ir.slot_values(GOLDEN), f1_title="<img src=x onerror=alert(1)> & </script>"))
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; &lt;/script&gt;" in page and "<img src=x" not in page
    assert page.count("</script>") == 2 and "</" not in ir.report_json(GOLDEN) and all(character not in ir.report_json(GOLDEN) for character in "<>&")


@R.register("narrative: a falling market and a rising market get the matching neutral sentences, a mixed market gets mixed ones, and no figure is missing ('n/a', 'None', 'nan')")
def _():
    for name, ds in (("falling", FALLING), ("rising", RISING), ("mixed", MIXED)):
        page = rendered_text(ds)
        assert not re.search(r"\bNone\b|\bnan\b|\binf\b|n/a|\{\{", page), name
        assert page.count("<script") == 2 and "undefined" not in page
    falling, rising, mixed = (text_of(rendered_text(ds)) for ds in (FALLING, RISING, MIXED))
    assert "Both assets remain materially below their first observed closes." in falling and "broad market weakness" in falling and "declined" in falling
    assert "Both assets remain materially above their first observed closes." in rising and "broad market strength" in rising and "gained" in rising and "not a confirmed market reversal" not in rising
    assert "BTC is above its first observed close, while ETH is below it." in mixed and "mixed asset-level conditions" in mixed and "broad market" not in mixed
    for page in (falling, rising, mixed):                      # nothing predictive or advisory is generated beyond the fixed approved wording
        assert not re.search(r"\b(will rise|will fall|expect|forecast|buy|sell|guarantee)\b", page.split("Scope, methodology")[0], re.I)


@R.register("narrative rules: leaders, ties, volatility and correlation wording follow the computed comparison")
def _():
    m = ir.compute(GOLDEN)
    assert ir._leader(m, "r30")[:2] == ("BTC", "ETH") and abs(ir._leader(m, "r30")[2] - 5.07) < 0.005
    assert ir._higher(m, "vol30") == ("ETH", "BTC") and ir._higher(m, "atr_pct") == ("ETH", "BTC")
    tie = {a: dict(m[a], r30=0.05, r7=0.01, vol30=0.3, atr_pct=0.02) for a in ("BTC", "ETH")}
    tie["pair"] = m["pair"]
    assert ir._leader(tie, "r30")[0] is None and ir._higher(tie, "vol30") == (None, None)
    assert "performed equally" in ir._finding1(tie)[0] + ir._finding1(tie)[1] and "similar realised volatility" in ir._finding3(tie)[0]
    for rho, title in ((0.95, "strong market co-movement"), (0.5, "moderate market co-movement"), (0.1, "weak market co-movement"), (-0.5, "moderate inverse co-movement"), (-0.9, "strong inverse co-movement")):
        assert title in ir._finding4(dict(m, pair=dict(m["pair"], corr=rho)))[0], rho
    assert ir._finding4(dict(m, pair=dict(m["pair"], corr=-0.5)))[1].startswith("The correlation between BTC and ETH daily returns was approximately −0.50.")


@R.register("class slots: signs and highlights follow the displayed value - negative and positive on the sign, a ratio highlighted from the project's +25% mark, zero as shown carries no class")
def _():
    assert ir._cls(-0.0607) == " negative" and ir._cls(0.0001) == " positive" and ir._cls(0.00004) == "" and ir._cls(0.0) == "" and ir._cls(None) == ""
    assert ir._ratio_cls(1.52) == " positive" and ir._ratio_cls(1.15) == "" and ir._ratio_cls(1.25) == " positive" and ir._ratio_cls(0.82) == "" and ir._ratio_cls(None) == ""
    assert ir._attr(-0.1) == ' class="negative"' and ir._attr(0.0) == "" and ir._ratio_attr(2.0) == ' class="positive"'
    assert ir.pct(-0.0346, signed=True) == "−3.46%" and ir.pct(0.0001, signed=True) == "+0.01%" and ir.pct(-0.00001, signed=True) == "0.00%" and ir.pct(0.0, signed=True) == "0.00%"
    assert ir.ordinal(11.25) == "11th" and ir.ordinal(43.81) == "44th" and ir.ordinal(1) == "1st" and ir.ordinal(2) == "2nd" and ir.ordinal(3) == "3rd" and ir.ordinal(12) == "12th" and ir.ordinal(21) == "21st"


@R.register("input limits: a dataset that is not exactly 31 days, a constant series (no correlation) and a day with no range are refused or shown as n/a, never a crash")
def _():
    short = dict(GOLDEN, rows=GOLDEN["rows"][:30])
    raises(ir.InteractiveReportError, lambda: ir.compute(short), "31")
    raises((ir.InteractiveReportError, ValueError, ZeroDivisionError), lambda: ir.compute(synthetic([100.0] * 31, [50.0] * 31)))
    assert ir.usd(None) == "n/a" and ir.ratio_x(None) == "n/a" and ir.pct(None) == "n/a"


@R.register("the command line: build-interactive-report writes exactly market_intelligence_report_<date>.html in a NEW folder, stating the fetch time that the pipeline's own report recorded; it refuses a wrong date, a missing CSV, a missing report, a report that says another fetch day and an existing folder")
def _():
    import shutil, tempfile
    reports, out_root = tempfile.mkdtemp(prefix="ir_"), tempfile.mkdtemp(prefix="ir_")
    for name, source in COMMITTED.items(): shutil.copy(source, os.path.join(reports, name))
    out = os.path.join(out_root, "interactive_out"); name = "market_intelligence_report_2026-10-27.html"
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-10-27"], env={}) == 0
    built = open(os.path.join(out, name), "rb").read(); assert os.listdir(out) == [name]
    dataset = dashboard.parse_dataset(open(COMMITTED["report_2026-10-27_data.csv"], encoding="utf-8", newline="").read())
    assert built == ir.render(dataset, {"kind": "kraken_automated", "fetched_at_utc": "2026-10-28 06:56:33"}), "the stated fetch time must be the one in the pipeline's report"
    page = built.decode("utf-8"); assert "DATA FETCHED 2026-10-28 06:56:33 UTC" in page and "fetched automatically by the daily workflow on 2026-10-28 06:56:33 UTC" in page and "supplied" not in text_of(page).lower().split("source:")[1][:400]
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-10-27"], env={}) == 1                 # the folder exists and is not empty
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "b"), "--date", "2026-10-26"], env={}) == 1      # the CSV ends on 2026-10-27
    assert da.main(["build-interactive-report", "--reports-dir", out_root, "--out-dir", os.path.join(out_root, "d"), "--date", "2026-10-27"], env={}) == 1       # no CSV there
    os.remove(os.path.join(reports, "report_2026-10-27.md"))
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "e"), "--date", "2026-10-27"], env={}) == 1       # no report: no fetch time, so nothing is built
    shutil.copy(COMMITTED["report_2026-10-27.md"], os.path.join(reports, "report_2026-10-27.md"))
    md = open(os.path.join(reports, "report_2026-10-27.md"), encoding="utf-8", newline="").read()
    open(os.path.join(reports, "report_2026-10-27.md"), "w", encoding="utf-8", newline="").write(md.replace("2026-10-28 06:56:33 UTC", "2026-10-30 06:56:33 UTC"))
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "f"), "--date", "2026-10-27"], env={}) == 1       # fetched two days after the analysis date: refused
    assert not any(os.path.exists(os.path.join(out_root, x)) for x in "bdef"), "a refused build leaves no folder"


@R.register("provenance: an automated fetch states Kraken, the recorded UTC fetch time (hero line, source note and footer) and the UTC day basis, and never says 'supplied' or 'not a live market feed'; a supplied CSV keeps the approved v4 wording, names no fetch time and never implies live data")
def _():
    fetched = "2026-11-13 04:00:12"
    auto = ir.render(GOLDEN, {"kind": "kraken_automated", "fetched_at_utc": fetched}).decode("utf-8"); sup = ir.render(GOLDEN).decode("utf-8")
    assert auto.count(fetched) == 3 and "DATA FETCHED " + fetched + " UTC" in auto and "fetched automatically by the daily workflow on " + fetched + " UTC" in auto and "fetched automatically from Kraken on " + fetched + " UTC" in auto
    assert "UTC DAY" in auto and "All dates and day boundaries are UTC" in auto and "a completed UTC day" in auto and "is not a real-time market feed" in auto
    assert "Supplied CSV" not in auto and "supplied historical CSV" not in auto and "not a live market feed" not in auto
    assert "Supplied CSV representing Kraken spot OHLC daily records" in sup and "based on the supplied historical CSV. It is not a live market feed." in sup and "records no fetch time" in sup
    assert "DATA FETCHED" not in sup and "fetched" not in text_of(sup).lower().replace("records no fetch time", "") and "fetched automatically" not in sup
    assert ir.render(GOLDEN, None) == ir.render(GOLDEN), "no provenance means a supplied CSV, never an automated fetch"


@R.register("provenance is never invented: a supplied CSV may not carry a fetch time, an automated fetch must carry a real UTC one in the exact format, unknown kinds and extra keys are refused, and a hostile slot value is refused")
def _():
    bad = [{"kind": "supplied_csv", "fetched_at_utc": "2026-11-13 04:00:12"}, {"kind": "kraken_automated", "fetched_at_utc": None}, {"kind": "kraken_automated", "fetched_at_utc": ""},
           {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13T04:00:12"}, {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 04:00"}, {"kind": "kraken_automated", "fetched_at_utc": "2026-13-45 04:00:12"},
           {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 25:00:00"}, {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 04:00:12 UTC"}, {"kind": "kraken_automated", "fetched_at_utc": 20261113},
           {"kind": "kraken_automated", "fetched_at_utc": "<script>x</script>"}, {"kind": "live", "fetched_at_utc": None}, {"kind": "kraken_automated"}, {"kind": "supplied_csv", "fetched_at_utc": None, "extra": 1}, "kraken", 5]
    for item in bad: raises(ir.InteractiveReportError, lambda item=item: ir.render(GOLDEN, item))
    template = open(ir.TEMPLATE_PATH, encoding="utf-8", newline="").read(); values = ir.slot_values(GOLDEN)
    raises(ir.InteractiveReportError, lambda: ir.render_template(template, dict(values, fetched_span="\n    <span>DATA FETCHED 2026-11-13 04:00:12 UTC</span><script>x()</script>")), "unexpected content")
    page = ir.render_template(template, dict(values, source_note="<img src=x onerror=alert(1)>"))
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "<img src=x" not in page


@R.register("threshold transparency: the report's wording-rules paragraph is written from the constants the sentences are chosen with, and states 5 %, 1.25x, the lowest or highest 25 %, 0.05 %, RSI 35 and 65 and the correlation limits 0.70 and 0.30")
def _():
    assert (ir.MATERIAL_MOVE, ir.HIGHLIGHT_RATIO, ir.NEAR_EDGE_PERCENT, ir.VWAP_NEAR, ir.RSI_LOW, ir.RSI_HIGH) == (0.05, 1.25, 25.0, 0.0005, 35.0, 65.0), "a threshold changed: the disclosure and the tests must be reviewed together"
    assert (ir.analysis.CORRELATION_STRONG_FROM, ir.analysis.CORRELATION_WEAK_BELOW) == (0.70, 0.30)
    note = ir.threshold_note()
    for fragment in ("at least 5% in size", "from 1.25 times the average of the previous seven sessions", "lowest or highest 25% of the day's range", "less than 0.05% away", "at 35 or below, or 65 or above",
                     "strong from a correlation of 0.70 and moderate from 0.30", "presentation conventions, not market standards or signals"):
        assert fragment in note, fragment
    for page in (ir.render(GOLDEN).decode(), ir.render(GOLDEN, {"kind": "kraken_automated", "fetched_at_utc": "2026-11-13 04:00:12"}).decode()):
        assert "<strong>Wording rules:</strong> " + note in page, "the paragraph is in the methodology section of every report"
        assert page.index("<strong>Wording rules:</strong>") > page.index("<strong>Activity:</strong>") and page.index("<strong>Wording rules:</strong>") < page.index("<strong>Important limitations:</strong>")


@R.register("threshold boundaries behave exactly as disclosed: 5 % and 1.25x and the 25 % edge and RSI 35/65 are inclusive, 0.05 % from VWAP is not 'approximately at', and just inside or outside each limit changes the wording")
def _():
    m = ir.compute(GOLDEN)
    both = lambda r: ir._text_both_below({a: dict(m[a], r30=r) for a in ("BTC", "ETH")})
    assert "materially below" in both(-0.05) and "materially" not in both(-0.0499) and "materially above" in both(0.05) and "materially" not in both(0.0499)
    mixed = ir._text_both_below({"BTC": dict(m["BTC"], r30=-0.06), "ETH": dict(m["ETH"], r30=-0.04)}); assert "materially" not in mixed, "both assets must reach the limit"
    assert ir._ratio_cls(1.25) == " positive" and ir._ratio_cls(1.2499) == "" and ir._callout({**m, "BTC": dict(m["BTC"], volratio=1.2499), "ETH": dict(m["ETH"], daily=-0.001)}).find("elevated") == -1
    assert "elevated BTC spot volume" in ir._callout({**m, "BTC": dict(m["BTC"], volratio=1.25)})
    assert ir._location({"loc": 25.0}) == "near its daily low" and ir._location({"loc": 25.01}) == "in the middle of its daily range" and ir._location({"loc": 75.0}) == "near its daily high" and ir._location({"loc": 74.99}) == "in the middle of its daily range"
    assert ir._vwap_state({"vwaprel": 0.000499}) == "near" and ir._vwap_state({"vwaprel": 0.0005}) == "above" and ir._vwap_state({"vwaprel": -0.0005}) == "below" and ir._vwap_state({"vwaprel": -0.000499}) == "near"
    risk = lambda a, b: ir._risk({**m, "BTC": dict(m["BTC"], rsi=a), "ETH": dict(m["ETH"], rsi=b)})
    assert "lower end of the scale" in risk(35.0, 34.0) and "lower end" not in risk(35.01, 30.0) and "upper end of the scale" in risk(65.0, 70.0) and "upper end" not in risk(64.99, 70.0)
    assert "The RSI readings are 50.0 for BTC and 50.0 for ETH" in risk(50.0, 50.0)


R.finish()
