"""Tests for the final detailed report (design v6, the production default of src/interactive_report.py): the coral-led, standalone, Plotly-based market intelligence report.

Everything is rendered offline from the committed fixtures: tests/fixtures/report_2026-11-12_data.csv (a supplied CSV) and the committed pipeline run reports/report_2026-10-27*
(an automated fetch whose pipeline report records the fetch time). The dashboard's own chart / table / export logic is executed in Node against the page's own data.
What these tests cannot show: how a browser paints the page (checked separately in a real browser at 1440, 768 and 390 pixels) and how a screen reader announces it.
"""
import hashlib
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta

from wf_support import Runner, raises, REPO, load

da = load("daily_automation")
ir = load("interactive_report")
dashboard = load("dashboard")
analysis = load("analysis")
R = Runner("interactive report v6 checks")
FIXTURES = os.path.join(REPO, "tests", "fixtures")
COMMITTED = {name: os.path.join(REPO, "reports", name) for name in ("report_2026-10-27.md", "report_2026-10-27_data.csv")}
V4_SHA256 = "03e6809778045d533cac44e06db0069ac0ef6df92b7d1f91ff753de30e1f01ae"
PROV = {"kind": "kraken_automated", "fetched_at_utc": "2026-10-28 06:56:33"}


def read(path, binary=False):
    with open(path, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8", "newline": ""})) as handle:
        return handle.read()


CSV_GOLDEN = read(os.path.join(FIXTURES, "report_2026-11-12_data.csv"))
GOLDEN = dashboard.parse_dataset(CSV_GOLDEN)
CSV_REAL = read(COMMITTED["report_2026-10-27_data.csv"])
REAL = dashboard.parse_dataset(CSV_REAL)
OUT = ir.render(GOLDEN).decode("utf-8")
OUT_AUTO = ir.render(REAL, PROV).decode("utf-8")
PLOTLY_TEXT = read(str(dashboard.PLOTLY_PATH))
TEMPLATE = read(str(ir.TEMPLATE_PATHS[ir.DESIGN_V6]))
CSS = TEMPLATE[TEMPLATE.index("<style>"):TEMPLATE.index("</style>")]


def synthetic(btc, eth, volume=lambda i: 100.0 + i, wide=0.02):
    start, rows = date(2026, 1, 1), []
    for index in range(31):
        row = [(start + timedelta(days=index)).isoformat(), repr(btc[index]), repr(eth[index])]
        row += ["" if index == 0 else repr(btc[index] / btc[index - 1] - 1), "" if index == 0 else repr(eth[index] / eth[index - 1] - 1)]
        for series in (btc, eth):
            previous = series[index - 1] if index else series[index]
            high, low = max(previous, series[index]) * (1 + wide), min(previous, series[index]) * (1 - wide)
            row += [repr(previous), repr(high), repr(low), repr(float(volume(index))), repr((high + low + series[index]) / 3), str(1000 + index)]
        rows.append(",".join(row))
    return dashboard.parse_dataset(",".join(dashboard.EXPECTED_COLUMNS) + "\n" + "\n".join(rows) + "\n")


def page_text(page):
    body = page[page.index("<body"):page.index('<script id="dashboard-data"')]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def body_html(page):
    return page[page.index("<body"):page.index('<script id="dashboard-data"')]


def data_of(page):
    return json.loads(re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>', page, re.S).group(1))


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


# ------------------------------------------------------------------------------------------------------------ default, determinism, standalone
@R.register("the final design is the production default: render() and the pipeline's own render function give v6, the output is deterministic, and the v4 golden fixture is untouched")
def _():
    assert ir.DEFAULT_DESIGN == ir.DESIGN_V6 and ir.render(GOLDEN) == ir.render(GOLDEN, design="v6") == ir.render(dashboard.parse_dataset(CSV_GOLDEN))
    assert da.render_interactive_report(GOLDEN) == ir.render(GOLDEN) and da.render_interactive_report(REAL, PROV) == ir.render(REAL, PROV)
    assert hashlib.sha256(read(os.path.join(FIXTURES, "market_intelligence_report_2026-11-12_v4_release.html"), True)).hexdigest() == V4_SHA256


@R.register("standalone and safe: three script elements in the dashboard's order, the vendored Plotly.js byte for byte, a strict CSP whose script hashes describe the exact final scripts, no request of any kind from the page's own code")
def _():
    scripts = dashboard.SCRIPT_ELEMENT.findall(OUT)
    assert [a.strip() for a, _ in scripts] == dashboard.EXPECTED_SCRIPT_ATTRIBUTES and scripts[1][1] == PLOTLY_TEXT
    assert hashlib.sha256(read(str(dashboard.PLOTLY_PATH), True)).hexdigest() == dashboard.PLOTLY_SHA256
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', OUT).group(1)
    assert csp == dashboard.content_security_policy(OUT.replace(csp, "__CSP__")) and "default-src 'none'" in csp and "connect-src 'none'" in csp and "img-src blob:" in csp
    own = OUT.replace(PLOTLY_TEXT, "").replace("url(#", "")          # fragment references to the in-page gradients of the rail artwork are not requests
    for forbidden in ("<link", "@import", "url(", "<img", "<iframe", " src=", "fetch(", "XMLHttpRequest", "WebSocket", "http://", "https://"):
        assert forbidden not in own, forbidden
    assert "\r" not in scripts[2][1] and len(OUT_AUTO) < da.MAX_INTERACTIVE_REPORT_BYTES and len(OUT_AUTO) < da.MAX_PAGES_REPORT_BYTES and len(OUT_AUTO) < da.MAX_DASHBOARD_BYTES


@R.register("the dashboard's validated logic is reused, not copied: the page's logic block is the dashboard template's block exactly, the chart data has the dashboard's shape, and the first close is not counted as a return")
def _():
    logic = ir._dashboard_logic(); template = read(str(dashboard.TEMPLATE_PATH))
    assert logic == template[template.index("/* LOGIC-START */"):template.index("/* LOGIC-END */") + len("/* LOGIC-END */")] and OUT.count(logic) == 1 and "chartSpecs" in logic and "exportCsv" in logic
    payload = data_of(OUT); assert set(payload["windows"]) == {"7", "14", "30"} and payload["columns"] == dashboard.EXPECTED_COLUMNS and len(payload["rows"]) == 31
    w = payload["windows"]["30"]
    assert len(w["dates"]) == 31 and len(w["return_dates"]) == 30 and len(w["return_pct"]["BTC"]) == 30 and w["return_dates"][0] == w["dates"][1] and w["index"]["BTC"][0] == 100.0
    reference = dashboard.build_payload(GOLDEN, {"data_fetched": "x", "source_confirmed": True, "pairs_confirmed": True}, __import__("datetime").datetime(2026, 11, 13, tzinfo=__import__("datetime").timezone.utc), "report_2026-11-12_data.csv", "r.md")
    for k in ("7", "14", "30"):
        for key in ("dates", "return_dates", "index", "close_usd", "return_pct", "drawdown_pct", "display", "rules"):
            assert payload["windows"][k][key] == reference["windows"][k][key], (k, key)
    assert payload["meta"] == {"csv_file": "report_2026-11-12_data.csv"}


# ------------------------------------------------------------------------------------------------------------ structure and wording
@R.register("eight sections in the approved order with matching navigation, the required section 06 title, no 'Smart AI' wording, no AI claim, and no redundant dashboard-download button")
def _():
    ids = ["s-exec", "s-perf", "s-risk", "s-activity", "s-charts", "s-rules", "s-quality", "s-data"]
    assert re.findall(r'<section class="section" id="(s-[a-z]+)"', OUT) == ids and re.findall(r'<li><a href="#(s-[a-z]+)"[^>]*>', OUT) == ids
    kickers = re.findall(r'<div class="kicker">([^<]+)</div>', OUT); assert [k[:2] for k in kickers] == ["01", "02", "03", "04", "05", "06", "07", "08"], kickers
    assert "<h2 id=\"h-rules\">Market Intelligence and Rule-Based Analysis</h2>" in OUT
    text = page_text(OUT).lower()
    for banned in ("smart ai", "ai analysis", "ai-generated", "ai-powered", "machine learning", "open detailed interactive report", "download dashboard"):
        assert banned not in text, banned
    assert "dashboard.html" not in OUT.replace(PLOTLY_TEXT, "") and "This report does not currently use an AI model." in OUT and 'class="button"' not in OUT
    assert da.PAGES_REPORT_URL.endswith("/reports/{day}/market_intelligence_report.html")             # the address the e-mail links to is unchanged


@R.register("section 01: the executive assessment, latest closes, performance overview and monitoring priorities are present; each key finding has observed data, evidence, interpretation, implication and monitoring, and at most three are shown")
def _():
    assert OUT.index("Overall market assessment") < OUT.index(">Key Findings</h3>") < OUT.index(">Performance Overview</h3>") < OUT.index("Recommended monitoring priorities")
    cards = re.findall(r'<article class="finding".*?</article>', OUT, re.S); assert len(cards) == 3
    for card in cards:
        for label in ("Supporting evidence", "Interpretation", "Decision implication", "Recommended monitoring", "Why this finding is shown"): assert label in card, label
        assert re.search(r'<h3 id="kf-\d" class="f-title">[^<]+</h3>', card)
    assert "$71,370.70" in OUT and "$2,017.15" in OUT and "BTC ahead by 5.07 pp" in OUT and "0.92 · strong positive" in OUT and "BTC −15.06% · ETH −19.18%" in OUT
    assert OUT.count('class="finding"') == 3 and "of up to 3 findings met the existing evidence rules" not in page_text(OUT)          # three met the rules: no shortfall note


@R.register("key findings come only from the project's existing thresholds and are never padded: the real 2026-10-27 run yields two, a market with no rule met yields none, and ranking follows the stated strength")
def _():
    real_cards = re.findall(r'<h3 id="kf-\d" class="f-title">([^<]+)</h3>', OUT_AUTO)
    assert len(real_cards) == 2 and real_cards[0].startswith("Bitcoin outperformed Ethereum by 8.36 percentage points") and "1.32 times as volatile" in real_cards[1] and "2 of up to 3 findings met the existing evidence rules" in OUT_AUTO
    flat = synthetic([100.0 - 1.0 * i for i in range(31)], [100.0 - 1.03 * i for i in range(31)])
    page = ir.render(flat).decode("utf-8")
    assert 'class="finding"' not in page and "No finding met the existing evidence rules in this period." in page
    scores = [float(x) for x in re.findall(r"It is ([0-9.]+) times that level", OUT)]; assert scores == sorted(scores, reverse=True) and len(scores) == 3
    for text in re.findall(r'<article class="finding".*?</article>', OUT, re.S): assert "proposed" not in text.lower() and "exploratory" not in text.lower()


@R.register("unapproved thresholds stay unapproved: the 10% drawdown and 2.5-standard-deviation levels appear only under the 'Exploratory (proposed)' tab, labelled not approved, and never select a key finding or touch R1-R3")
def _():
    assert ir.PROPOSED_DRAWDOWN == 0.10 and ir.PROPOSED_Z == 2.5
    flat = synthetic([100.0 - 1.0 * i for i in range(31)], [100.0 - 1.03 * i for i in range(31)]); page = ir.render(flat).decode("utf-8")
    exp = page[page.index('id="p-exp"'):page.index("</section>", page.index('id="p-exp"'))]
    assert "Exploratory · proposed thresholds · not approved" in exp and "Current drawdown:" in exp and "proposed level: 10% or more" in exp and "They are not project rules" in exp
    outside = page.replace(exp, "")
    assert "proposed level" not in outside and "not approved" not in outside.replace("Exploratory (proposed)", "")
    metrics_panel = outside[outside.index('id="p-metrics"'):outside.index('id="p-interp"')]
    assert "R1 co-movement" in metrics_panel and "10%" not in metrics_panel and "2.5" not in metrics_panel
    assert 'id="t-exp"' in page and 'aria-controls="p-exp"' in page and "Large daily move:" in ir.render(GOLDEN).decode("utf-8") and ir.render(GOLDEN).decode("utf-8").count("proposed level") >= 1


# ------------------------------------------------------------------------------------------------------------ figures
@R.register("figures: the headline numbers of the fixture are reproduced from the CSV (closes, returns over 7/15/30, drawdown, volatility, volume ratios, VWAP, range position, correlation) and every table value is the generator's own metric")
def _():
    m = ir.compute(GOLDEN); closes = GOLDEN["closes"]
    for asset, last, r7, r15, r30 in (("BTC", 71370.7, -6.07, -12.20, -14.12), ("ETH", 2017.15, -9.75, -14.43, -19.18)):
        assert closes[asset][-1] == last and (round(m[asset]["r7"] * 100, 2), round(m[asset]["r15"] * 100, 2), round(m[asset]["r30"] * 100, 2)) == (r7, r15, r30), asset
    text = page_text(OUT)
    for needle in ("$71,370.70", "$2,017.15", "−6.07%", "−9.75%", "−14.12%", "−19.18%", "43.44%", "63.61%", "36.30%", "49.05%", "1.52×", "0.82×", "−0.90%", "+0.01%", "11%", "44%", "$298.60M", "$52.17M", "173,085", "54,629", "$1,725", "$955"):
        assert needle in text, needle
    cum = dashboard.window_metrics(GOLDEN, 30)["cumulative_return"]; payload = data_of(OUT)
    assert [round(v, 6) for v in payload["by_window"]["BTC"]] == [round(m["BTC"]["r7"] * 100, 6), round(m["BTC"]["r15"] * 100, 6), round(m["BTC"]["r30"] * 100, 6)] and abs(payload["by_window"]["BTC"][2] - cum["BTC"] * 100) < 1e-9
    assert abs(payload["windows"]["30"]["index"]["ETH"][-1] - 100 - cum["ETH"] * 100) < 1e-9


@R.register("activity and units: turnover is volume x VWAP in USD, trade counts and VWAP offsets come from the CSV columns, the unusual sessions use the existing 1.25x level, and the largest daily gain and loss carry their dates")
def _():
    activity = ir._daily_activity(GOLDEN); c = dashboard.CANDLE_COLUMNS["BTC"]; rows = GOLDEN["rows"]
    assert abs(activity["BTC"]["turnover_musd"][-1] - float(rows[-1][c["volume"]]) * float(rows[-1][c["vwap"]]) / 1e6) < 1e-9 and activity["BTC"]["trades"][-1] == int(rows[-1][c["trade_count"]]) == 173085
    assert abs(activity["BTC"]["vwap_rel_pct"][-1] - (float(rows[-1][c["close"]]) / float(rows[-1][c["vwap"]]) - 1) * 100) < 1e-9
    sessions = ir._unusual_sessions(GOLDEN, activity); assert sessions and all(s["ratio"] >= ir.HIGHLIGHT_RATIO for s in sessions) and {(s["asset"], s["date"]) for s in sessions} >= {("BTC", "2026-11-12"), ("ETH", "2026-10-29")}
    ex = ir._daily_extremes(GOLDEN); assert ex["BTC"]["best"][1] == "2026-11-04" and ex["BTC"]["worst"][1] == "2026-11-05" and round(ex["ETH"]["worst"][0] * 100, 2) == -7.76
    for needle in ("Largest daily gain", "Largest daily loss", "Kraken's OHLC layout", "volume is in the base asset", "Unusual Trading Sessions", "do not identify buyers, sellers, intent or manipulation"): assert needle in OUT, needle
    text = page_text(OUT).lower()
    for hit in re.finditer("institutional", text): assert text[max(0, hit.start() - 14):hit.start()].endswith(("not confirmed ", "not establish ")), text[max(0, hit.start() - 40):hit.end() + 20]          # only as a disclaimer


@R.register("rule-based analysis: R1-R3 values and outcomes equal the dashboard's own, the correlation interval is the Fisher interval, uncertainty and limits are stated, and no prediction, buy/sell or causal claim appears")
def _():
    d = dashboard.window_display(GOLDEN, 30); rules = dashboard.window_metrics(GOLDEN, 30)["rules"]
    panel = OUT[OUT.index('id="p-metrics"'):OUT.index('id="p-interp"')]
    for needle in (d["correlation"], d["ratio"], rules["R1"], rules["R2"], rules["R3"], d["joint"], f"share {d['overlap']}"): assert htmllib.escape(needle, quote=True) in panel, needle
    lo, hi = ir._fisher_interval(ir.compute(GOLDEN)["pair"]["corr"]); assert 0.83 < lo < 0.84 and 0.96 < hi < 0.97 and f"{lo:.2f} to {hi:.2f}" in OUT
    unc = OUT[OUT.index('id="p-unc"'):OUT.index('id="p-mon"')]
    for needle in ("does not establish causation", "Sample size", "Not in the data", "No statement about who traded, why prices moved or where they will go"): assert needle in unc, needle
    text = page_text(OUT).lower()
    for banned in ("you should buy", "you should sell", "buy signal", "sell signal", "will rise", "will fall", "guaranteed", "price target"): assert banned not in text, banned


# ------------------------------------------------------------------------------------------------------------ data quality and provenance
@R.register("data quality: the validation ledger shown is the dataset's own (planned, evaluated, pass, fail, missing, duplicate), the period and counts are the file's, and no generation timestamp is invented")
def _():
    ledger = REAL["candle_ledger"]; page = OUT_AUTO
    assert f"{ledger['evaluated']} of {ledger['expected_instances']} planned candle-rule checks were evaluated" in page and "621 of 621" in page
    for r in ledger["rules"]: assert f"<td>{r['rule_id']}</td><td>{r['expected_instances']}</td><td>{r['evaluated']}</td>" in page, r["rule_id"]
    assert f"missing {ledger['missing']}, duplicate {ledger['duplicate']}, unexpected {ledger['unexpected']}" in page and "27 Sep 2026 – 27 Oct 2026" in page and "31 daily closes and 30 daily returns" in page
    assert "No generation time is shown" in page and not re.search(r"Report generated[^<]*\d{2}:\d{2}", page) and "report_2026-10-27_data.csv · 17 columns · 31 rows" in page
    assert "All dates and day boundaries are UTC" in page


@R.register("provenance: an automated run states the recorded UTC fetch time (Data Quality provenance, source and footer; not the introduction) and an automated origin; a supplied CSV states none and says so; an invalid or invented time is refused")
def _():
    assert OUT_AUTO.count("2026-10-28 06:56:33") >= 2 and "2026-10-28 06:56:33 UTC, as recorded by the pipeline's own report." in htmllib.unescape(OUT_AUTO) and "Fetched from Kraken by the daily workflow." in OUT_AUTO and "real-time market feed" in OUT_AUTO
    assert "Supplied manually as a CSV file." in OUT and "Not recorded for a manually supplied CSV; no fetch time is stated." in OUT and not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", page_text(OUT))
    for bad in ({"kind": "kraken_automated", "fetched_at_utc": None}, {"kind": "kraken_automated", "fetched_at_utc": "2026-10-28 25:61:00"}, {"kind": "supplied_csv", "fetched_at_utc": "2026-10-28 06:56:33"}, {"kind": "other", "fetched_at_utc": None}, "now"):
        raises(ir.InteractiveReportError, lambda bad=bad: ir.render(REAL, bad))


@R.register("pipeline path: build-interactive-report writes the v6 report in a new folder with the fetch time the pipeline's own report recorded, byte for byte what the generator produces, and refuses what it refused before")
def _():
    reports, out_root = tempfile.mkdtemp(prefix="ir6_"), tempfile.mkdtemp(prefix="ir6_")
    for name, source in COMMITTED.items(): shutil.copy(source, os.path.join(reports, name))
    out = os.path.join(out_root, "interactive_out"); name = "market_intelligence_report_2026-10-27.html"
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-10-27"], env={}) == 0
    built = read(os.path.join(out, name), True); assert os.listdir(out) == [name] and built == ir.render(REAL, PROV) and b"Fetch time not recorded" not in built
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", out, "--date", "2026-10-27"], env={}) == 1
    assert da.main(["build-interactive-report", "--reports-dir", reports, "--out-dir", os.path.join(out_root, "b"), "--date", "2026-10-26"], env={}) == 1


# ------------------------------------------------------------------------------------------------------------ the dashboard logic, executed (Node)
NODE_CHECK = r"""
const fs = require('fs'), vm = require('vm');
const [page, csvPath] = process.argv.slice(2);
const html = fs.readFileSync(page, 'utf8'), csv = fs.readFileSync(csvPath, 'utf8');
const logic = html.slice(html.indexOf('/* LOGIC-START */'), html.indexOf('/* LOGIC-END */') + 15);
const data = JSON.parse(html.match(/<script id="dashboard-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const L = vm.runInNewContext(logic + '\nDashboardLogic', {});
const out = {};
out.export30 = L.exportCsv(data, 30) === csv;
out.name30 = L.exportFileName(data, 30);
out.rows = [7, 14, 30].map(k => L.exportCsv(data, k).trim().split('\n').length);
out.header = L.exportCsv(data, 7).split('\n')[0] === csv.split('\n')[0];
out.last7 = L.exportCsv(data, 7).trim().split('\n').slice(1).join('\n') === csv.trim().split('\n').slice(-8).join('\n');
const p = L.paginate(L.selectRows(data, 30), 4, 10); out.page4 = [p.first, p.last, p.pages];
out.toggle = JSON.stringify(L.toggleAsset({BTC: true, ETH: false}, 'BTC'));
L.COLORS.BTC = '#3478C8'; L.COLORS.ETH = '#DD6551';
const specs = L.chartSpecs(data, 30, {BTC: true, ETH: true});
out.charts = Object.keys(specs).sort().join(',');
out.colors = specs.price.data.map(t => t.line.color).join(',');
out.scatterPoints = specs.scatter.data[0].x.length;
out.one = L.chartSpecs(data, 7, {BTC: true, ETH: false}).price.data.length;
out.noscatter = L.chartSpecs(data, 30, {BTC: true, ETH: false}).scatter.data.length;
console.log(JSON.stringify(out));
"""


@R.register("the dashboard's logic, run in Node on the page's own data: the 30-day export equals the source CSV byte for byte, the 7- and 14-day exports are its tail, the file name follows the contract, pagination and asset rules hold, and the charts take the report's colours")
def _():
    node = shutil.which("node"); assert node, "Node.js is required (the Apps Script suites need it too)"
    folder = tempfile.mkdtemp(prefix="ir6_node_"); page, csv_path = os.path.join(folder, "page.html"), os.path.join(folder, "data.csv")
    open(page, "w", encoding="utf-8", newline="").write(OUT); open(csv_path, "w", encoding="utf-8", newline="").write(CSV_GOLDEN)
    done = subprocess.run([node, "-", page, csv_path], input=NODE_CHECK, capture_output=True, text=True, timeout=120); assert done.returncode == 0, done.stderr[-400:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["export30"] is True and out["header"] is True and out["last7"] is True and out["name30"] == "report_2026-11-12_data_last30days.csv" and out["rows"] == [9, 16, 32]
    assert out["page4"] == [31, 31, 4] and out["toggle"] == '{"BTC":true,"ETH":false}' and out["charts"] == "drawdown,price,returns,scatter" and out["colors"] == "#3478C8,#DD6551" and out["scatterPoints"] == 30 and out["one"] == 1 and out["noscatter"] == 0


@R.register("data table metadata: all 17 columns have a label with its unit, a display kind and a group; the column filters select the right BTC / ETH / price / activity columns; sorting, formats and the export hint are in the page")
def _():
    t = ir._column_meta(); cols = dashboard.EXPECTED_COLUMNS
    assert len(t["labels"]) == len(t["kinds"]) == len(t["groups"]) == 17 and t["labels"][0] == "Date (UTC)" and t["labels"][1] == "BTC close (USD)" and t["labels"][3] == "BTC return (%)" and t["labels"][8] == "BTC volume (BTC)" and t["labels"][14] == "ETH volume (ETH)" and t["labels"][9] == "BTC VWAP (USD)" and t["labels"][16] == "ETH trades (count)"
    assert t["kinds"][0] == "date" and t["kinds"][3] == "pct" and t["kinds"][8] == "base" and t["kinds"][10] == "count" and t["kinds"][5] == "usd"
    assert {cols[i][:3] for i in t["filters"]["BTC"] if i} == {"btc"} and {cols[i][:3] for i in t["filters"]["ETH"] if i} == {"eth"} and len(t["filters"]["BTC"]) == len(t["filters"]["ETH"]) == 9 and t["filters"]["all"] is None
    assert [cols[i] for i in t["filters"]["activity"]][1:] == [cols[i] for i in sorted(dashboard.VOLUME_COLUMNS + dashboard.TRADE_COUNT_COLUMNS)] and t["groups"][0] == "Date" and t["groups"][1] == "Closing prices" and t["groups"][5] == "BTC candle" and t["groups"][11] == "ETH candle"
    for needle in ("aria-sort", "th-btn", "L.paginate", "L.exportCsv(D, state.tk)", "L.exportFileName(D, state.tk)", "the CSV export holds the exact CSV values", "scope = \"colgroup\""): assert needle in OUT, needle
    assert data_of(OUT)["table"] == t


# ------------------------------------------------------------------------------------------------------------ design, accessibility, responsiveness
@R.register("design tokens: documented palette (page #F2F4F7, navy #142C54 / #203D68, coral #E8796B, BTC #3478C8) with one BTC / ETH colour system shared by the CSS, the legend dots and the chart script")
def _():
    t = tokens()
    assert (t["page"], t["card"], t["navy"], t["navy2"], t["coral"], t["coral-soft"], t["btc"], t["eth"], t["amber"], t["border"]) == ("#F2F4F7", "#FFFFFF", "#142C54", "#203D68", "#E8796B", "#FCE8E5", "#3478C8", "#E8796B", "#D99A32", "#E1E5ED")
    assert 'L.COLORS.BTC = "#3478C8"; L.COLORS.ETH = "#DD6551"' in OUT and t["eth-mark"] == "#DD6551"
    assert len({t["page"], t["card"], t["coral-soft"], t["soft"]}) == 4 and re.search(r"\.tbl th \{[^}]*background: var\(--coral-soft\)", CSS) and re.search(r"\.tbl td \{[^}]*background: var\(--card\)", CSS) and "tbody tr:nth-child(even) td { background: var(--soft)" in CSS
    assert re.search(r"\.topbar \{[^}]*backdrop-filter: blur", CSS) and re.search(r"\.rail \{[^}]*linear-gradient\(182deg, var\(--rail-top\) 0%, var\(--rail-mid\) 46%, var\(--rail-bot\) 100%\)", CSS) and re.search(r"\.nav a\[aria-current=\"true\"\] \{[^}]*linear-gradient\(100deg, #C94B39 0%, #B03D6A 55%, #7F3FA0 100%\)", CSS)
    assert t["coral-deep"] == "#C4503E" and re.search(r"\.tbl\.data thead tr\.grp th \{[^}]*background: var\(--navy\)", CSS)


@R.register("contrast (WCAG AA): body, secondary, coral and BTC / ETH ink text >= 4.5:1 on white, the page and the pale-coral surface; text on the navy surfaces >= 4.5:1; chart marks >= 3:1; status never relies on colour alone")
def _():
    t = tokens()
    for name in ("text", "text2", "coral-ink", "btc-ink", "amber-ink", "green", "red", "navy"):
        for bg in ("card", "page", "coral-soft"):
            assert contrast(t[name], t[bg]) >= 4.5, (name, bg, contrast(t[name], t[bg]))
    assert contrast(t["amber-ink"], t["amber-soft"]) >= 4.5 and contrast(t["green"], t["green-soft"]) >= 4.5 and contrast(t["red"], t["red-soft"]) >= 4.5
    for name in ("on-navy", "on-navy2", "coral-on-navy", "green-on-navy", "red-on-navy"):
        for bg in ("navy", "navy2"): assert contrast(t[name], t[bg]) >= 4.5, (name, bg)
    assert contrast("#FFFFFF", t["navy"]) >= 4.5 and contrast("#FFFFFF", t["coral-ink"]) >= 4.5 and contrast(t["navy"], t["coral-soft"]) >= 4.5
    assert contrast(t["btc"], t["card"]) >= 3.0 and contrast(t["eth-mark"], t["card"]) >= 3.0
    assert "▼" in OUT and "▲" in OUT and ir._chg(-0.0346) == "▼ −3.46%" and ir._chg(0.01) == "▲ +1.00%" and ir._chg(0.0) == "0.00%" and "▼ −" in OUT        # direction is in the text as well as in the colour
    assert not re.search(r"The secondary text colour of the brief", CSS) and "#667085" not in CSS


@R.register("typography and layout: no text below 11 px, 15 px body, right-aligned numeric columns, tabular figures, three breakpoints, tables scroll inside their own frames, charts and KPI cards reflow")
def _():
    sizes = [float(x) for x in re.findall(r"font-size: ([0-9.]+)px", CSS)] + [float(x) for x in re.findall(r"font: [0-9]+ ([0-9.]+)px", CSS)]
    assert sizes and min(sizes) >= 11, sorted(sizes)[:4]
    assert "font: 15px/1.6" in CSS and "tabular-nums" in CSS and re.search(r"\.tbl\.num th:not\(:first-child\), \.tbl\.num td:not\(:first-child\) \{ text-align: right", CSS) and "text-align: right" in CSS
    for query in ("@media (max-width: 1180px)", "@media (max-width: 900px)", "@media (max-width: 600px)"): assert query in CSS, query
    assert re.search(r"\.table-wrap \{[^}]*overflow-x: auto", CSS) and OUT.count('class="table-wrap"') == OUT.count('role="region" tabindex="0"') >= 1 and "displayModeBar: NARROW ? false" in OUT and "grid-template-columns: minmax(0,1fr)" in CSS
    assert "min-width: 0" in CSS and OUT.count('<meta name="viewport"') == 1


@R.register("accessibility: skip link, labelled navigation with aria-current, one h1 and a named h2 per section, captioned tables with column scopes, WAI-ARIA tabs with arrow keys, pressed states, visible focus, reduced motion")
def _():
    body = body_html(OUT)
    assert '<a class="skip" href="#main">' in OUT and 'id="main" tabindex="-1"' in OUT and '<nav class="nav" aria-label="Report sections">' in OUT and "aria-current" in OUT and "IntersectionObserver" in OUT
    assert body.count("<h1>") == 1 and len(re.findall(r'aria-labelledby="h-[a-z]+"', body)) == 8 and len(re.findall(r'<h2 id="h-[a-z]+"', body)) == 8
    assert body.count("<caption class=\"sr\">") == body.count("<table") and body.count('<th scope="col">') >= 20 and 'scope = "colgroup"' in OUT
    assert body.count('role="tab"') == 5 and body.count('role="tabpanel"') == 5 and "ArrowRight" in OUT and "ArrowLeft" in OUT and "Home" in OUT and "aria-selected" in OUT and body.count("hidden>") == 5          # four hidden tab panels and the closed drawer scrim
    assert all('type="button"' in b for b in re.findall(r"<button [^>]*>", body)) and body.count("aria-pressed") >= 14
    assert ":focus-visible" in CSS and "prefers-reduced-motion: reduce" in CSS and "transition-duration: .01ms" in CSS and "REDUCED" in OUT and 'lang="en"' in OUT
    ids = re.findall(r' id="([^"]+)"', body); assert len(ids) == len(set(ids)), "duplicate id"
    for chart in re.findall(r'<div class="chart" id="(chart-[a-z]+)" role="img" aria-label="([^"]+)"', body): assert chart[1]
    ids8 = [c for c in re.findall(r'<div class="chart" id="(chart-[a-z]+)"', body)]; assert len(ids8) == 9 and set(ids8) >= {"chart-windows", "chart-cumulative", "chart-dist", "chart-activity", "chart-price", "chart-returns", "chart-scatter", "chart-drawdown"} and "chart-overview" in ids8          # the eight report charts plus the executive overview chart


@R.register("existing pipeline contracts: the staging check accepts this report, the stored report byte-compares with a fresh render, and the e-mail components were not touched by this design")
def _():
    reports, out_root = tempfile.mkdtemp(prefix="ir6_"), tempfile.mkdtemp(prefix="ir6_")
    for name, source in COMMITTED.items(): shutil.copy(source, os.path.join(reports, name))
    assert da.render_interactive_report(REAL, PROV) == ir.render(REAL, PROV)
    source = read(os.path.join(REPO, "src", "interactive_report.py"))
    assert "email_report" not in source and "Code.gs" not in source and "report_url" not in source and "send_success" not in source
    assert "apps_script" not in TEMPLATE and "Gmail" not in TEMPLATE


# ------------------------------------------------------------------------------------------------------------ dashboard shell (navy rail, glass top bar, drawer, executive first screen)
@R.register("dashboard shell: a navy left rail with the eight section links and icons (coral active state), a glass top bar with the report date, a drawer that opens from a labelled menu button on small screens, Escape and the scrim close it")
def _():
    body = body_html(OUT)
    rail = body[body.index('<aside class="rail"'):body.index("</aside>")]
    assert re.findall(r'<li><a href="#(s-[a-z]+)"[^>]*><svg class="ic"', rail) == ["s-exec", "s-perf", "s-risk", "s-activity", "s-charts", "s-rules", "s-quality", "s-data"] and rail.count("aria-hidden=\"true\"") >= 9
    assert '<div class="topbar">' in body and "Report: 2026-11-12 (UTC)" in body and 'id="menu-btn" aria-controls="rail" aria-expanded="false" aria-label="Open report navigation"' in body and '<div class="scrim" id="scrim" hidden></div>' in body
    assert re.search(r"@media \(max-width: 900px\) \{[^@]*\.rail \{ position: fixed[^}]*translateX\(-104%\)[^}]*visibility: hidden", CSS) and ".rail.open" in CSS
    for token in ("function drawer(open)", "aria-expanded", 'ev.key === "Escape"', "scrim.addEventListener", "menu.focus()", "links[0].focus()"): assert token in OUT, token
    assert "Report: 2026-10-27 (UTC)" in OUT_AUTO and "svg" in rail and "<img" not in OUT and "url(" not in OUT.replace(PLOTLY_TEXT, "").replace("url(#", "")


@R.register("executive first screen: four KPI cards (BTC and ETH 30-return performance with latest close, correlation, maximum drawdown) with sparklines drawn from the existing chart series, then the assessment with a rule-based status pill and three evidence columns beside the overview chart")
def _():
    body = body_html(OUT); sec = body[body.index('id="s-exec"'):body.index('id="s-perf"')]
    assert sec.index('class="kpi-row four lead-row"') < sec.index('class="exec-grid"') < sec.index('class="ov-grid three"') < sec.index(">Findings in Detail</h3>") and sec.count('class="kpi ') == 4
    kpis = sec[:sec.index('class="exec-grid"')]
    assert kpis.count('<svg class="spark"') == 3 and kpis.count("<polyline") == 4 and "Latest close $71,370.70" in sec and "Latest close $2,017.15" in sec and "▼ −14.12%" in sec and "▼ −19.18%" in sec
    payload = data_of(OUT)["windows"]["30"]; first = re.search(r'<svg class="spark".*?points="([^"]+)"', sec).group(1).split(" ")
    assert len(first) == 31 and first[0].startswith("0.0,") and first[-1].startswith("120.0,")                      # the 31 closes of the 30-return window, in order
    assert re.search(r'class="pill" title="Observation rule R2[^"]*">R2 · ETH noticeably more volatile than BTC</span>', sec) and ir.drawdown_note and sec.count("<h4>") == 3
    for h in ("Key finding", "Decision implication", "Recommended monitoring"): assert f"<h4>{h}</h4>" in sec, h
    assert 'id="chart-overview"' in sec and sec.count("data-ovw=") == 3 and "Observed" not in sec[:0]
    flat = ir.render(synthetic([100.0 - 1.0 * i for i in range(31)], [100.0 - 1.03 * i for i in range(31)])).decode("utf-8")
    assert "No finding met the existing evidence rules in this period.</p></div><div><h4>Decision implication</h4><p>None is stated without evidence." in flat


@R.register("no new ratings or metrics from the reference image: no Sharpe, Sortino, VaR, beta or 'Moderate Risk' rating, no marketing banner, no settings or help items, no decorative image; the status pills come from existing rule outcomes and the validation ledger")
def _():
    low = page_text(OUT).lower()
    for banned in (r"sharpe", r"sortino", r"var", r"value at risk", r"beta", r"moderate risk", r"professional market analysis", r"powered by advanced analytics", r"settings", r"help"): assert not re.search(banned, low), banned
    assert "<img" not in OUT and "<canvas" not in OUT.replace(PLOTLY_TEXT, "")
    start = OUT.index('<div class="dq-card">'); dq = OUT[start:OUT.index('<div class="grid-2">', start)]
    assert '<span class="pill ok">All checks passed</span>' in dq and "All <b>621</b> planned validation checks were evaluated and passed." in dq and "Validation summary" in dq
    ledger = dict(REAL["candle_ledger"]); ledger["rules"] = [dict(r) for r in ledger["rules"]]; assert ir.dashboard.__name__ == "dashboard"
    bad = dashboard.parse_dataset(CSV_REAL); bad["candle_ledger"] = dict(ledger, rules=[dict(ledger["rules"][0], **{"fail": 1, "pass": ledger["rules"][0]["pass"] - 1})] + ledger["rules"][1:])
    assert '<span class="pill warn">Checks need attention' in ir.render(bad, PROV).decode("utf-8")


@R.register("colour direction (visual direction, not a pixel quota): filled coral is the deeper accessible shade with white text >= 4.5:1, rail text and navy panels >= 4.5:1, coral text only in the dark-enough ink, glass limited to the top bar, KPI/finding cards and assessment tiles, tables and charts solid")
def _():
    t = tokens()
    assert contrast("#FFFFFF", "#C94B39") >= 4.5 and contrast("#FFFFFF", "#B23F30") >= 4.5 and contrast("#FFFFFF", t["coral-deep"]) >= 4.5, "white text on the deeper filled coral (buttons, pills, tabs)"
    assert contrast(t["on-navy"], t["navy"]) >= 4.5 and contrast(t["on-navy2"], t["navy2"]) >= 4.5 and contrast(t["coral-on-navy"], t["navy"]) >= 4.5
    for color_use in re.findall(r"color: (#[0-9A-Fa-f]{6})", CSS):
        if abs(luminance(color_use) - luminance(t["coral"])) < 0.02 and color_use.upper() == t["coral"]: raise AssertionError("coral #E8796B used as text colour")
    glass = re.findall(r"([.\w: -]+) \{[^}]*backdrop-filter: blur", CSS); names = " ".join(glass)
    for solid in (".table-wrap", ".tbl", ".chart-card", ".tabpanel", ".facts", ".explain"): assert solid not in names, solid
    assert ".topbar" in names and ".kpi" in names and ".assess-stats li" in names and ".nav a" not in names          # the navigation items are flat, not double-shaded
    ref = os.path.join(REPO, "docs", "design-reference", "coral-dashboard-reference.webp")
    assert os.path.isfile(ref) and hashlib.sha256(read(ref, True)).hexdigest() == "171fc9872b82f72efbd634eca739c5d188fcf9546a73bf3580c759462b158658"      # the supplied screenshot, byte for byte


# ------------------------------------------------------------------------------------------------------------ editorial pass: headings, subtitles, terminology, rail identity
@R.register("editorial: main section headings are in Title Case with a sentence-case subtitle, the kicker and navigation labels agree, and the headings no longer repeat each other")
def _():
    body = body_html(OUT)
    heads = dict(zip(re.findall(r'<h2 id="(h-[a-z]+)"', body), [htmllib.unescape(x) for x in re.findall(r'<h2 id="h-[a-z]+">([^<]+)</h2>', body)]))
    assert list(heads.values()) == ["Market Overview", "Market Performance", "Risk and Volatility", "Trading Activity", "Interactive Charts", "Market Intelligence and Rule-Based Analysis", "Data Quality and Methodology", "Daily Market Data"], heads
    for section in re.findall(r'<section class="section" id="s-[a-z]+".*?</section>', body, re.S):
        assert re.search(r'</h2>\s*<p class="intro">', section), "every section has a subtitle"
    nav = [htmllib.unescape(x) for x in re.findall(r'<li><a href="#s-[a-z]+"[^>]*><svg.*?</svg><span>([^<]+)</span></a></li>', body)]
    assert nav == ["Overview", "Performance", "Risk", "Trading Activity", "Charts", "Rule-Based Analysis", "Data Quality", "Market Data"], nav
    kick = [k for k in re.findall(r'<div class="kicker">([^<]+)</div>', body)]; assert [k.split(" · ")[1] for k in kick] == ["Overview", "Performance", "Risk", "Trading Activity", "Charts", "Rule-Based Analysis", "Data Quality", "Market Data"], kick
    assert "Market performance, key relationships and notable observations across the reporting period." in body and "What the data shows this period" not in OUT and "Daily data, all 17 columns" not in OUT and "all 17 columns of the source file" in body
    for title in re.findall(r'class="chart-title"[^>]*>([^<]+)<', body): assert title == title.title().replace("Of", "of").replace("Since The", "Since the").replace("From", "from") or title[0].isupper(), title
    assert ">Key Findings</h3>" in body and ">Findings in Detail</h3>" in body and ">Performance Overview</h3>" in body and ">Unusual Trading Sessions</h3>" in body and ">Rule-Based Findings</h3>" in body
    assert "Reporting date: <strong>12 November 2026</strong> (UTC)" in OUT and "AS OF" not in body and "UTC DAY" not in body


@R.register("introduction panel: the title, the Kraken-based description, the reporting date derived from the dataset, and the disclaimer replace the old paragraph and chips; no fetch time and no second author credit sit on the right-hand side")
def _():
    head = re.search(r'<header class="pagehead">.*?</header>', OUT_AUTO, re.S).group(0)
    text = [htmllib.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<(?:h1|p)[ >].*?</(?:h1|p)>", head, re.S)]
    assert text == ["Bitcoin & Ethereum Market Intelligence", "Validated data. Quantitative analysis. Market intelligence.",
                    "Transforming historical market data into meaningful insights through analysis of price behaviour, momentum, relative strength, volatility and risk signals.",
                    "Identifying significant market movements, asset divergences and emerging conditions that warrant attention.",
                    "Reporting date: 27 October 2026 (UTC)",
                    "Based on validated historical Kraken data, fetched on 28 October 2026 at 06:56:33 UTC. Not a real-time feed or investment advice."], text      # the fetch sentence is built from the recorded provenance
    assert "Rolling 30-day" not in OUT_AUTO and "An in-depth assessment" not in OUT_AUTO and "Evidence-led" not in OUT_AUTO and 'class="chips"' not in OUT_AUTO and "<button" not in head and 'class="eyebrow"' not in head and "<a " not in head      # the old wording is gone; no pills, buttons or links
    other = ir.render(REAL, {"kind": "kraken_automated", "fetched_at_utc": "2026-11-17 14:46:44"}).decode("utf-8")
    assert "fetched on 17 November 2026 at 14:46:44 UTC." in re.search(r'<header class="pagehead">.*?</header>', other, re.S).group(0) and "28 October 2026 at" not in other      # the sentence follows the provenance, it is not fixed text
    supplied = re.search(r'<header class="pagehead">.*?</header>', OUT, re.S).group(0)
    assert "Based on validated historical market data from a supplied CSV file; no fetch time is recorded. Not a real-time feed or investment advice." in supplied and "Kraken" not in supplied and not re.search(r"\d{2}:\d{2}:\d{2}", supplied)      # a supplied CSV states no fetch time and no Kraken origin
    assert not re.search(r"\bAI\b|artificial intelligence|machine learning|predictive|autonomous|\bagent", head, re.I)      # the introduction claims no capability the pipeline does not have
    assert OUT_AUTO.count("Created by Rizwana Zahoor") == 1 and OUT_AUTO.index("Created by Rizwana Zahoor") < OUT_AUTO.index('<div class="topbar">') and "<footer" in OUT_AUTO and "Created by" not in OUT_AUTO[OUT_AUTO.index("<footer"):]      # only the left navigation pane carries the author
    assert "Reporting date: <strong>12 November 2026</strong> (UTC)" in OUT and "27 October 2026" not in re.search(r'<header class="pagehead">.*?</header>', OUT, re.S).group(0)      # the date follows the dataset, not the template


@R.register("terminology checked against the source: 'trades' is Kraken's candle count (number of trades), VWAP is the volume-weighted average price in USD, volume is in the base asset; 'grades' appears nowhere and the activity section uses trades and trade count consistently")
def _():
    src = read(os.path.join(REPO, "src", "fetch_data.py")); assert "[time, open, high, low, close, vwap, volume, count]" in src and "trade count is not an integer" in src
    assert "grade" not in OUT.replace(PLOTLY_TEXT, "").lower() and "grade" not in TEMPLATE.lower()
    act = OUT[OUT.index('id="s-activity"'):OUT.index('id="s-charts"')]
    assert "number of trades" in act and "volume-weighted average price (VWAP)" in act and "volume is in the base asset" in act and "Trade count" in act and "Number of trades recorded per day" in OUT and "Estimated daily turnover in million USD (volume × VWAP)" in act
    assert dashboard.CANDLE_COLUMNS["BTC"]["trade_count"] == 10 and ir._column_meta()["labels"][10] == "BTC trades (count)"


@R.register("rule-based wording: the disclosure describes the current approach (documented, deterministic rules, traceable evidence) and says the report does not currently use an AI model, with no AI findings, roadmap claims or placeholders; the tabbed structure stays open to further analysis views")
def _():
    sec = OUT[OUT.index('id="s-rules"'):OUT.index('id="s-quality"')]
    assert "<strong>Rule-based analysis.</strong>" in sec and "This report does not currently use an AI model." in sec and "so each one can be traced to its evidence" in sec and "none is a prediction or investment advice" in sec
    head = sec[:sec.index('<div class="tabs"')].lower()          # the heading, subtitle and disclosure
    for banned in ("agent", "roadmap", "coming soon", "future", "ai-generated", "ai generated", "machine learning", "llm", "placeholder", "planned"): assert banned not in head, banned
    assert "agent" not in sec.lower() and "ai-generated" not in sec.lower() and "roadmap" not in sec.lower()
    assert sec.count('role="tab"') == 5 and sec.count('role="tabpanel"') == 5 and re.search(r'<div class="tabs" role="tablist"', sec)          # one more tab/panel pair can be added without changing the layout or the script


@R.register("data quality wording: no repeated source/period/provenance sentences (each fact appears once in its own row), the validation summary and ledger agree, and every provenance and limitation statement is kept")
def _():
    sec = OUT_AUTO[OUT_AUTO.index('id="s-quality"'):OUT_AUTO.index('id="s-data"')]
    facts = re.findall(r"<dt>([^<]+)</dt><dd>(.*?)</dd>", sec, re.S); labels = [f[0] for f in facts]
    assert labels == ["Source", "Pairs", "Data origin", "Data fetched (UTC)", "UTC basis", "Observation period", "Report generated", "Input file", "Validation results"], labels
    text = " ".join(f[1] for f in facts)
    assert text.count("2026-10-28 06:56:33") == 1 and text.count("31 daily closes") == 1 and text.count("XBT/USD") == 1 and text.count("completed UTC day") == 1
    assert "All <b>621</b> planned validation checks were evaluated and passed." in sec and "Every stored return equals close ÷ previous close − 1" in sec and "not consolidated global market data" in sec and "not affiliated with Kraken" in sec
    sup = OUT[OUT.index('id="s-quality"'):OUT.index('id="s-data"')]
    assert "The file records no fetch time; its dates are taken as UTC days." in sup and "Supplied manually as a CSV file" in sup


@R.register("rail artwork and bottom strip: a large orange/gold 3D-style BTC coin and violet ETH crystal on two glass platforms over a dense dotted wave field with coral, violet and blue glows, drawn as one inline SVG with a fixed aspect that is never hidden (it scales with the window height and the rail scrolls below that); a two-tone rail (dark navy menu zone, lighter blue-violet art zone); a bottom strip with a divider holds the Kraken lines, then a second divider and 'Created by Rizwana Zahoor'")
def _():
    rail = OUT[OUT.index('<aside class="rail"'):OUT.index("</aside>")]
    assert rail.index('<nav class="nav"') < rail.index('<div class="rail-art">') < rail.index('<div class="rail-strip">') < rail.index('<p class="rail-info">') < rail.index('<p class="rail-author">Created by Rizwana Zahoor</p>')
    assert "<b>Kraken spot</b> · XBT/USD · ETH/USD" in rail and "Daily candles, one per UTC day" in rail and "rail-foot" not in OUT and 'class="coins"' not in OUT
    art = rail[rail.index('<svg class="rail-art-svg"'):rail.index("</svg>", rail.index('<svg class="rail-art-svg"')) + 6]
    assert 'viewBox="0 0 240 292"' in art and 'preserveAspectRatio="xMidYMax meet"' in art and 'role="img" aria-label="Bitcoin and Ethereum"' in art and "<title>Bitcoin and Ethereum</title>" in art
    assert art.count("<linearGradient") == 9 and art.count("<radialGradient") == 4 and art.count("<circle") >= 700 and art.count("<polygon") >= 15 and art.count('fill="url(#sTop)"') == 2          # nine linear and four radial gradients, the wave field, two glass platforms
    assert set(re.findall(r"url\(#([A-Za-z]+)\)", art)) <= set(re.findall(r'<(?:linear|radial)Gradient id="([A-Za-z]+)"', art))
    assert "M16.5 9.5h4.6" in art and 'fill="url(#cFace)"' in art and "#F68A45" in art and "#6A55E8" in art and "#FF8E6E" in art and "#6C8CFF" in art and "#9A86FF" in art       # the B of the orange coin, the violet crystal facets, the coral, blue and violet dots
    for banned in ("animate", "<filter", "<image", "http", "<script", "style="): assert banned not in art, banned
    assert "<img" not in OUT and len(art) < 45000
    art_css = re.search(r"\.rail-art \{[^}]*\}", CSS).group(0)
    assert "flex: 0 1 292px" in art_css and "min-height: 176px" in art_css and "pointer-events: none" in art_css and "mask-image: linear-gradient(180deg, #000 0%, #000 82%, transparent 100%)" in art_css and "var(--art-a)" in art_css and "var(--art-b)" in art_css
    assert "display: none" not in art_css and not re.search(r"\.rail-art \{ display: none", CSS)          # the artwork is never hidden: below 700 px of height it shrinks to 150 px and the rail scrolls
    assert "@media (max-height: 960px)" in CSS and "@media (max-height: 700px) { .nav a { min-height: 38px; padding: 6px 14px; } .rail-art { min-height: 150px; } }" in CSS
    assert re.search(r"\.rail \{[^}]*overflow-y: auto", CSS)
    strip = re.search(r"\.rail-strip \{[^}]*\}", CSS).group(0); assert "border-top: 1px solid rgba(255,255,255,.2)" in strip and "padding: 12px 20px 16px" in strip and "margin: 0 -14px" in strip
    author = re.search(r"\.rail-author \{[^}]*\}", CSS).group(0); assert "border-top: 1px solid rgba(255,255,255,.16)" in author and "padding-top: 10px" in author and "font-size: 12.5px" in author and "color: #FFFFFF" in author
    assert 'L.COLORS.BTC = "#3478C8"; L.COLORS.ETH = "#DD6551"' in OUT          # the chart colours are unchanged: the artwork colours are decorative only
    tk = tokens()
    assert (tk["rail-top"], tk["rail-mid"], tk["rail-bot"], tk["art-a"], tk["art-b"], tk["plum"], tk["plum2"]) == ("#0E1C46", "#12245A", "#1B2A70", "#2A3A9C", "#3E3584", "#7F3FA0", "#B03D6A")
    rail_css = re.search(r"\.rail \{[^}]*\}", CSS).group(0)
    assert rail_css.count("radial-gradient") == 2 and "rgba(128,98,196,.38)" in rail_css and "rgba(58,88,200,.4)" in rail_css and "linear-gradient(100deg, rgba(6,14,44,.35) 0%, rgba(6,14,44,0) 45%, rgba(70,96,210,.2) 100%)" in rail_css          # the lilac and blue sheen on the right edge and the left-to-right shading of the reference
    assert contrast(tk["art-a"], tk["rail-top"]) >= 1.5 and contrast(tk["art-b"], tk["rail-top"]) >= 1.5          # the two tones are visibly different: dark navy menu zone, lighter blue-violet art zone
    for bg in ("rail-top", "rail-mid", "rail-bot", "art-a", "art-b"):
        for fg in ("#FFFFFF", "#E3EAFA", "#C9D6F5"): assert contrast(fg, tk[bg]) >= 4.5, (fg, bg, contrast(fg, tk[bg]))          # labels, info text, icon tint on every rail stop


# ------------------------------------------------------------------------------------------------------------ navigation and overview refinement
@R.register("navigation: flat items with white 15 px labels (weight 500) and 24 px icons with a light-blue tint, one coral-to-plum active pill with white text (>= 4.5:1 on every gradient stop) and a visible white boundary (>= 3:1 against the rail), Overview first and active when the report opens")
def _():
    tk = tokens()
    nav_css = re.search(r"\.nav a \{[^}]*\}", CSS).group(0)
    assert "color: #FFFFFF" in nav_css and "font-size: 15px" in nav_css and "font-weight: 500" in nav_css and "background: transparent" in nav_css and "backdrop-filter" not in nav_css and "min-height: 52px" in nav_css and "border-radius: 14px" in nav_css
    assert re.search(r"\.nav a \.ic \{ width: 24px; height: 24px; color: #C9D6F5; stroke-width: 1.8; \}", CSS) and ".nav .ic polyline { fill: none; }" in CSS
    active = re.search(r'\.nav a\[aria-current="true"\] \{[^}]*\}', CSS).group(0)
    assert "linear-gradient(100deg, #C94B39 0%, #B03D6A 55%, #7F3FA0 100%)" in active and "border-color: rgba(255,255,255,.6)" in active and "box-shadow: 0 8px 20px rgba(201,75,57,.45)" in active and "color" not in active.replace("border-color", "")   # the label stays white (inherited from .nav a)
    assert re.search(r'\.nav a\[aria-current="true"\] \.ic \{ color: #FFFFFF; \}', CSS)
    for stop in ("#C94B39", "#B03D6A", "#7F3FA0"): assert contrast("#FFFFFF", stop) >= 4.5, (stop, contrast("#FFFFFF", stop))
    def blend(top, alpha, under):
        return "#" + "".join(f"{round(int(top[i:i + 2], 16) * alpha + int(under[i:i + 2], 16) * (1 - alpha)):02X}" for i in (1, 3, 5))
    for rail_stop in ("rail-top", "rail-mid", "rail-bot"): assert contrast(blend("#FFFFFF", .6, tk[rail_stop]), tk[rail_stop]) >= 3.0, rail_stop          # the pill boundary against the rail
    assert contrast("#C94B39", tk["rail-top"]) >= 3.0                                                                                          # the coral end of the pill against the rail where it sits (top of the rail)
    links = re.findall(r'<li><a href="#(s-[a-z]+)"([^>]*)><svg', OUT)
    assert links[0][0] == "s-exec" and 'aria-current="true"' in links[0][1] and all(not a.strip() for _, a in links[1:]) and "<span>Overview</span>" in OUT and "<span>Executive</span>" not in OUT
    assert 'if (seen[links[i].getAttribute("href").slice(1)])' in OUT and "aria-current" in OUT          # scrolling still moves the active state
    assert "<span>Rule-Based Analysis</span>" in OUT and re.search(r"--rail: 240px", CSS)


@R.register("overview as a summary of the report: KPI cards, assessment and performance chart, then risk snapshot, market activity, key findings with the rule outcomes, the latest data rows and the data-quality state; every value is an existing figure and every 'View details' link reaches an existing section")
def _():
    body = body_html(OUT); sec = body[body.index('id="s-exec"'):body.index('id="s-perf"')]
    grid = sec[sec.index('class="ov-grid three"'):sec.index(">Findings in Detail</h3>")]
    for title in ("Risk Snapshot", "Market Activity", "Key Findings", "Latest Market Data"): assert f'<h3 class="card-title">{title}</h3>' in grid, title
    m = ir.compute(GOLDEN); ex = ir._daily_extremes(GOLDEN)
    for needle in (f"BTC {ir.pct(m['BTC']['vol30'])} · ETH {ir.pct(m['ETH']['vol30'])}", f"BTC {ir.pct(m['BTC']['atr_pct'])} · ETH {ir.pct(m['ETH']['atr_pct'])}", f"BTC {ir.pct(m['BTC']['maxdd'])} · ETH {ir.pct(m['ETH']['maxdd'])}",
                   f"BTC {ir._chg(ex['BTC']['worst'][0])} · ETH {ir._chg(ex['ETH']['worst'][0])}", "BTC 17 of 30 · ETH 16 of 30", f"BTC {m['BTC']['rsi']:.1f} · ETH {m['ETH']['rsi']:.1f}"):
        assert htmllib.escape(needle, quote=True) in grid, needle
    ids = set(re.findall(r' id="([^"]+)"', body))
    links = re.findall(r'<a class="viewlink" href="#([a-z0-9-]+)">', grid); assert links == ["s-risk", "s-activity", "kf-1", "s-data", "s-quality"] and set(links) <= ids
    assert len(re.findall(r'<li class="sig">', grid)) == 3 and re.findall(r'<b>(R[123])</b>', grid) == ["R1", "R2", "R3"] and all(htmllib.escape(v, quote=True) in grid for v in ir.dashboard.window_metrics(GOLDEN, 30)["rules"].values())
    bars = re.findall(r'<rect class="(btc|eth)" x="[0-9.]+" y="([0-9.]+)" width="3.8" height="([0-9.]+)"', grid); assert len(bars) == 62 and {b[0] for b in bars} == {"btc", "eth"}
    act = ir._daily_activity(GOLDEN); top = max(max(act["BTC"]["turnover_musd"]), max(act["ETH"]["turnover_musd"])); heights = [float(b[2]) for b in bars]
    assert abs(max(heights) - 84.0) < 0.06 and abs(heights[-2] - act["BTC"]["turnover_musd"][-1] / top * 84) < 0.06 and abs(heights[-1] - act["ETH"]["turnover_musd"][-1] / top * 84) < 0.06          # one shared scale, the same series as section 04
    mini = grid[grid.index('<h3 class="card-title">Latest Market Data</h3>'):]
    rows = re.findall(r"<tr><td>([^<]+)</td><td>\$([0-9,.]+)</td><td>\$([0-9,.]+)</td>", mini); assert [r[0] for r in rows] == ["12 Nov 2026", "11 Nov 2026", "10 Nov 2026"] and rows[0][1] == "71,370.70" and rows[0][2] == "2,017.15"
    assert '<span class="pill ok">All checks passed</span>' in grid and "All <b>621</b> planned validation checks were evaluated and passed." in grid
    low = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", grid)).lower()
    for banned in ("sharpe", "sortino", "beta", "var (", "moderate risk", "score", "rating"): assert banned not in low, banned
    flat = ir.render(synthetic([100.0 - 1.0 * i for i in range(31)], [100.0 - 1.03 * i for i in range(31)])).decode("utf-8")
    assert "No finding met the existing evidence rules in this period.</span></li>" in flat
    assert all(v == d for v, d in zip(ir._dq_state(GOLDEN["candle_ledger"])[:1], (True,))) and ir._dq_state(dict(GOLDEN["candle_ledger"], missing=1))[1] == "warn"


R.finish()
