"""Tests for src/interactive_report.py: the interactive market intelligence report generated from one validated CSV.

The approved v4 report (tests/fixtures/market_intelligence_report_2026-11-12_v4_release.html, SHA-256 pinned below) is the frozen design reference. Its 31-day source data is
tests/fixtures/report_2026-11-12_data.csv, rebuilt on 2026-11-14 from Kraken's public daily candles with the project's own fetch and CSV code (it is not the original upload; the 31 closes
of both assets equal the closes embedded in the v4 report, and the 12 November volumes and trade counts equal the v4 text). Nothing here uses the network or a secret.
"""
import hashlib, json, os, re
from datetime import date, timedelta

from wf_support import Runner, raises, REPO, load

da = load("daily_automation")
ir = load("interactive_report")
dashboard = load("dashboard")
R = Runner("interactive report checks")
FIXTURES = os.path.join(REPO, "tests", "fixtures")
V4_SHA256 = "03e6809778045d533cac44e06db0069ac0ef6df92b7d1f91ff753de30e1f01ae"


def read(name, binary=False):
    with open(os.path.join(FIXTURES, name), "rb" if binary else "r", **({} if binary else {"encoding": "utf-8", "newline": ""})) as handle:
        return handle.read()


V4 = read("market_intelligence_report_2026-11-12_v4_release.html", binary=True)
GOLDEN = dashboard.parse_dataset(read("report_2026-11-12_data.csv"))


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


@R.register("golden: the approved v4 file is the pinned reference (SHA-256) and the generator reproduces it byte for byte from its 31-day source data")
def _():
    assert hashlib.sha256(V4).hexdigest() == V4_SHA256
    out = ir.render(GOLDEN)
    assert out == V4, next(i for i in range(min(len(out), len(V4))) if out[i] != V4[i])


@R.register("golden: the output is deterministic - the same dataset gives the same bytes on every call, and no clock, random value or environment is read")
def _():
    assert ir.render(GOLDEN) == ir.render(GOLDEN) == ir.render(dashboard.parse_dataset(read("report_2026-11-12_data.csv")))
    import ast
    tree = ast.parse(open(os.path.join(REPO, "src", "interactive_report.py"), encoding="utf-8").read())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"html", "json", "math", "re", "statistics", "pathlib", "analysis", "dashboard"}, imported        # no clock, random, environment or network module


@R.register("design unchanged: with every slot replaced by its golden value the template equals v4, so no byte outside the data slots was edited (CSS, JavaScript, layout, fixed wording)")
def _():
    template = open(ir.TEMPLATE_PATH, encoding="utf-8", newline="").read()
    values = ir.slot_values(GOLDEN)
    assert set(re.findall(r"\{\{([a-z0-9_]+)\}\}", template)) <= set(values)
    assert ir.render_template(template, values).encode("utf-8") == V4
    assert "<script>" in template and template.count("{{") == len(re.findall(r"\{\{[a-z0-9_]+\}\}", template))


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


@R.register("the command line: build-interactive-report writes exactly market_intelligence_report_<date>.html (the golden bytes) in a NEW folder, and refuses a wrong date, a missing CSV and an existing folder")
def _():
    import tempfile
    reports, out_root = tempfile.mkdtemp(prefix="ir_"), tempfile.mkdtemp(prefix="ir_")
    with open(os.path.join(reports, "report_2026-11-12_data.csv"), "wb") as handle:
        handle.write(read("report_2026-11-12_data.csv", binary=True))
    out = os.path.join(out_root, "interactive_out")
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-11-12"], env={}) == 0
    assert os.listdir(out) == ["market_intelligence_report_2026-11-12.html"] and open(os.path.join(out, os.listdir(out)[0]), "rb").read() == V4
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-11-12"], env={}) == 1           # the folder exists and is not empty
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "b"), "--date", "2026-11-11"], env={}) == 1      # the CSV ends on 2026-11-12
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "c"), "--date", "2026-11-13"], env={}) == 1
    assert da.main(["build-interactive-report", "--reports-dir", out_root, "--out-dir", os.path.join(out_root, "d"), "--date", "2026-11-12"], env={}) == 1       # no CSV there
    assert not os.path.exists(os.path.join(out_root, "b")) and not os.path.exists(os.path.join(out_root, "d"))


R.finish()
