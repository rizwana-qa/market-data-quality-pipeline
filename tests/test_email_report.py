"""Stage 4 tests for src/email_report.py: the data and the three PNG charts of the HTML success e-mail, and the request body (send_success_report) built from them. Written BEFORE the module.
Everything is computed from the validated dataset with the project's own functions (dashboard.window_metrics and friends) and compared with the independent Decimal calculator in tests/independent_calc.py, so
no second financial method is introduced. The PNG files are decoded here with a small independent decoder (zlib + struct), which also checks every CRC. Offline: no network, no secret, nothing outside temporary folders.
What these tests cannot show is how Gmail or Outlook DRAW the message; that needs a person looking at a real message."""
import ast, json, math, os, re, struct, sys, zlib
from datetime import date, datetime, timezone
from decimal import Decimal

from wf_support import Runner, REPO, load
import independent_calc as ic

da, er, dashboard = load("daily_automation"), load("email_report"), load("dashboard")
R = Runner("email report checks")
REAL_REPORTS = os.path.join(REPO, "reports"); TODAY = date(2026, 10, 28)
RUN_ID, RUN_URL = "123456789", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
GENERATED = datetime(2026, 10, 28, 2, 16, 5, tzinfo=timezone.utc)
TOKEN_ROOM = 300                                   # a longer token than any realistic one, for the size budget
ARTIFACT_URL = RUN_URL + "/artifacts/987654321"

_cache = {}
def outputs(): return da.validate_outputs(REAL_REPORTS, TODAY)
def report():
    if "report" not in _cache: _cache["report"] = er.build_report(outputs(), GENERATED)
    return _cache["report"]
def decode_png(data):
    """An independent PNG decoder: signature, every chunk CRC, IHDR, PLTE, the inflated pixel rows (filter type 0 only). Returns a dict."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "no PNG signature"
    position, chunks = 8, []
    while position < len(data):
        length, tag = struct.unpack(">I4s", data[position:position + 8]); body = data[position + 8:position + 8 + length]; crc = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f"bad CRC in {tag!r}"; chunks.append((tag, body)); position += 12 + length
    assert position == len(data) and chunks[0][0] == b"IHDR" and chunks[-1][0] == b"IEND", "chunk layout"
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1]); assert (compression, filtering, interlace) == (0, 0, 0)
    palette_bytes = next(body for tag, body in chunks if tag == b"PLTE"); palette = [tuple(palette_bytes[i:i + 3]) for i in range(0, len(palette_bytes), 3)]
    raw = zlib.decompress(b"".join(body for tag, body in chunks if tag == b"IDAT")); stride = 1 + (width * depth + 7) // 8; assert len(raw) == stride * height, "pixel data has the wrong size"
    rows = []
    for y in range(height):
        row = raw[y * stride:(y + 1) * stride]; assert row[0] == 0, "only filter type 0 is expected"
        values = [byte for byte in row[1:]] if depth == 8 else [nibble for byte in row[1:] for nibble in (byte >> 4, byte & 15)]
        rows.append(values[:width])
    return {"width": width, "height": height, "depth": depth, "colour": colour, "palette": palette, "pixels": rows}
def colour_of(image, x, y): return image["palette"][image["pixels"][y][x]]
def count_colour(image, rgb): return sum(1 for row in image["pixels"] for index in row if image["palette"][index] == rgb)
def b64png(chart): import base64; return base64.b64decode(chart["png_base64"], validate=True)
def charts_by_id(): return {chart["id"]: chart for chart in report()["charts"]}

# ================================================================ the report data
@R.register("report meta: row count, first date, the report's own 'Data fetched' time and the generation time stamp (UTC), all from the validated data")
def _():
    meta = report()["meta"]; dataset = outputs()["dataset"]; fetched = re.search(r"^- Data fetched: (.+)$", open(os.path.join(REAL_REPORTS, "report_2026-10-27.md"), encoding="utf-8").read(), re.M).group(1)
    assert set(meta) == {"rows", "first_date", "data_fetched", "generated_at"} and meta["rows"] == 31 == len(dataset["rows"]) and meta["first_date"] == dataset["dates"][0] == "2026-09-27", meta
    assert meta["data_fetched"] == fetched and meta["generated_at"] == "2026-10-28 02:16:05 UTC", meta

@R.register("report assets: 7-, 14- and 30-day returns, annualised volatility, worst day, down days and the drawdown dates agree with the independent Decimal calculator and with the project's own metrics")
def _():
    dataset = outputs()["dataset"]; closes = {"btc": dataset["closes"]["BTC"], "eth": dataset["closes"]["ETH"]}; metrics = outputs()["metrics"]
    for asset, upper in (("btc", "BTC"), ("eth", "ETH")):
        got = report()["assets"][asset]; c = closes[asset]; r = ic.returns(c[-31:])
        assert set(got) == {"return_7d", "return_14d", "return_30d", "volatility_annualised", "worst_day", "negative_days", "drawdown_peak", "drawdown_trough"}, set(got)
        for key, k in (("return_7d", 7), ("return_14d", 14), ("return_30d", 30)): assert abs(Decimal(repr(got[key])) - ic.cumulative(c[-(k + 1):])) < Decimal("1e-12"), (asset, key)
        assert abs(Decimal(repr(got["volatility_annualised"])) - ic.sample_std(r) * Decimal(365).sqrt()) < Decimal("1e-12"), asset
        assert got["negative_days"] == ic.negative_days(r) == metrics["negative_days"][upper]
        worst, day = min(zip((float(v) for v in r), dataset["dates"][-30:])); assert got["worst_day"] == {"value": worst, "date": day} or (abs(got["worst_day"]["value"] - worst) < 1e-12 and got["worst_day"]["date"] == day), (asset, got["worst_day"])
        drawdown, peak, trough = metrics["max_drawdown"][upper]; assert abs(Decimal(repr(drawdown)) - ic.max_drawdown(c[-31:])) < Decimal("1e-12")
        assert got["drawdown_peak"] == peak and got["drawdown_trough"] == trough and (peak is None or (peak in dataset["dates"] and trough in dataset["dates"] and peak <= trough)), (asset, peak, trough)

@R.register("report pair: ETH/BTC volatility ratio, shared down days and overlap share agree with the independent calculator and the project's metrics (the correlation itself travels in the existing summary)")
def _():
    dataset = outputs()["dataset"]; btc, eth = ic.returns(dataset["closes"]["BTC"][-31:]), ic.returns(dataset["closes"]["ETH"][-31:]); pair = report()["pair"]; metrics = outputs()["metrics"]
    assert set(pair) == {"volatility_ratio", "joint_negative_days", "overlap_share"}, set(pair)
    assert abs(Decimal(repr(pair["volatility_ratio"])) - ic.sample_std(eth) / ic.sample_std(btc)) < Decimal("1e-12") and pair["joint_negative_days"] == ic.joint_negative_days(btc, eth) == metrics["joint_negative_days"]
    assert pair["overlap_share"] == metrics["down_day_overlap_share"] and abs(float(ic.correlation(btc, eth)) - metrics["correlation"]) < 1e-12

@R.register("report coverage: all 17 variables in CSV order with their valid-row counts (31 for fifteen, 30 for the two return columns, which have no value on the first day) and the latest value as written in the CSV")
def _():
    coverage = report()["coverage"]; header = open(os.path.join(REAL_REPORTS, "report_2026-10-27_data.csv"), encoding="utf-8").read().splitlines()[0].split(","); last = open(os.path.join(REAL_REPORTS, "report_2026-10-27_data.csv"), encoding="utf-8").read().splitlines()[-1].split(",")
    assert [entry["name"] for entry in coverage] == header == dashboard.EXPECTED_COLUMNS and len(coverage) == 17 and all(set(entry) == {"name", "valid_rows", "latest"} for entry in coverage)
    assert [entry["valid_rows"] for entry in coverage] == [30 if name.endswith("_return") else 31 for name in header], [(e["name"], e["valid_rows"]) for e in coverage]
    assert [entry["latest"] for entry in coverage] == last

@R.register("report coverage is computed from the data, not written down: a blank cell in the validated dataset lowers that variable's count")
def _():
    o = outputs(); o["dataset"]["rows"][5][8] = ""; coverage = er.build_report(o, GENERATED)["coverage"]
    assert coverage[8]["name"] == "btc_volume" and coverage[8]["valid_rows"] == 30 and coverage[7]["valid_rows"] == 31, [(c["name"], c["valid_rows"]) for c in coverage]

@R.register("the report is strict-JSON safe (no NaN or infinity) and holds no secret-like text")
def _():
    text = json.dumps(report(), allow_nan=False); assert "token" not in text.lower() and "secret" not in text.lower() and "script.google.com" not in text

# ================================================================ the PNG charts
@R.register("three charts - indexed prices, daily returns, drawdown - each a valid PNG (signature, every CRC, IHDR 600x240, palette image, inflates to exactly the right size, indices inside the palette) within the size limit")
def _():
    charts = report()["charts"]; assert [c["id"] for c in charts] == ["indexed", "returns", "drawdown"] == list(er.CHART_IDS), [c["id"] for c in charts]
    for chart in charts:
        assert set(chart) == {"id", "png_base64", "width", "height", "low", "high", "btc_last", "eth_last"} and (chart["width"], chart["height"]) == (600, 240) == (er.CHART_WIDTH, er.CHART_HEIGHT)
        data = b64png(chart); image = decode_png(data); assert (image["width"], image["height"], image["colour"]) == (600, 240, 3) and image["depth"] in (4, 8) and len(data) <= er.MAX_CHART_BYTES, (chart["id"], len(data))
        assert max(i for row in image["pixels"] for i in row) < len(image["palette"]) <= 16, chart["id"]

@R.register("the charts show the data: BTC orange and ETH blue both appear in every chart, the corner is background, the scale (low, high) covers every plotted value, and the captions' last values are the real ones")
def _():
    o = outputs(); window = dashboard.window_slice(o["dataset"], 30); btc_idx, eth_idx = dashboard.indexed_series(window["closes"]["BTC"]), dashboard.indexed_series(window["closes"]["ETH"])
    btc_dd, eth_dd = dashboard.drawdown_series(window["closes"]["BTC"]), dashboard.drawdown_series(window["closes"]["ETH"]); btc_ret, eth_ret = [v * 100 for v in window["returns"]["BTC"]], [v * 100 for v in window["returns"]["ETH"]]
    expected = {"indexed": (btc_idx, eth_idx), "returns": (btc_ret, eth_ret), "drawdown": (btc_dd, eth_dd)}
    for chart_id, chart in charts_by_id().items():
        image = decode_png(b64png(chart)); assert colour_of(image, 0, 0) == (255, 255, 255), "the corner must be background"
        assert count_colour(image, er.BTC_COLOUR) > 40 and count_colour(image, er.ETH_COLOUR) > 40, (chart_id, count_colour(image, er.BTC_COLOUR), count_colour(image, er.ETH_COLOUR))
        btc, eth = expected[chart_id]; assert chart["low"] < min(min(btc), min(eth)) + 1e-9 and chart["high"] > max(max(btc), max(eth)) - 1e-9 and chart["low"] < chart["high"], (chart_id, chart["low"], chart["high"])
        assert abs(chart["btc_last"] - btc[-1]) < 0.01 and abs(chart["eth_last"] - eth[-1]) < 0.01, (chart_id, chart["btc_last"], btc[-1])

@R.register("the charts are deterministic and follow the data: identical input gives identical bytes; changing one close in the data changes the indexed and drawdown pictures")
def _():
    again = er.build_report(outputs(), GENERATED)["charts"]; assert [c["png_base64"] for c in again] == [c["png_base64"] for c in report()["charts"]], "same data must give the same bytes"
    o = outputs(); o["dataset"]["closes"]["BTC"][-5] *= 0.8; changed = {c["id"]: c for c in er.build_report(o, GENERATED)["charts"]}; original = charts_by_id()
    assert changed["indexed"]["png_base64"] != original["indexed"]["png_base64"] and changed["drawdown"]["png_base64"] != original["drawdown"]["png_base64"]

@R.register("the renderer copes with flat series, all-negative values, one huge spike and identical series (valid PNGs, a drawn line, no division by zero), and refuses bad input")
def _():
    n = 31; cases = {"flat": ([100.0] * n, [100.0] * n), "negative": ([-1.0 - i * 0.1 for i in range(n)], [-2.0 - i * 0.05 for i in range(n)]), "spike": ([100.0] * 15 + [10000.0] + [100.0] * 15, [100.0 + i for i in range(n)]), "identical": ([100.0 + i for i in range(n)],) * 2}
    for label, (btc, eth) in cases.items():
        for kind in er.CHART_IDS:
            data, low, high = er.render_chart(kind, btc, eth); image = decode_png(data); assert (image["width"], image["height"]) == (600, 240) and low < high and count_colour(image, er.ETH_COLOUR) + count_colour(image, er.BTC_COLOUR) > 20, (label, kind)
    for btc, eth in (([1.0], [1.0]), ([1.0, 2.0], [1.0]), ([1.0, float("nan")], [1.0, 2.0]), ([1.0, float("inf")], [1.0, 2.0]), (["a", "b"], [1.0, 2.0]), ([], [])):
        try: er.render_chart("indexed", btc, eth)
        except er.EmailReportError: continue
        raise AssertionError(f"bad input was accepted: {btc!r} {eth!r}")
    try: er.render_chart("unknown", [1.0, 2.0], [1.0, 2.0])
    except er.EmailReportError: pass
    else: raise AssertionError("an unknown chart kind was accepted")

@R.register("the module needs nothing new and talks to nothing: it imports only the standard library and the project's dashboard/analysis modules, and uses no network, subprocess or file-writing call")
def _():
    source = open(os.path.join(REPO, "src", "email_report.py"), encoding="utf-8").read(); tree = ast.parse(source)
    imported = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert imported <= set(sys.stdlib_module_names) | {"dashboard", "analysis"}, sorted(imported - set(sys.stdlib_module_names))
    for forbidden in ("socket", "urllib", "requests", "http", "subprocess", "os.system", "open(", "eval(", "exec(", "pickle"): assert forbidden not in source, forbidden

# ================================================================ the request body
def payloads():
    if "payloads" not in _cache: o = outputs(); base = da.build_payloads(o, RUN_ID, RUN_URL); _cache["payloads"] = (base, da.build_report_payload(o, base["send_success_email"], GENERATED))
    return _cache["payloads"]

@R.register("send_success_report request: exactly the existing success-email fields plus the report - the same rows, summary and run data - with its own action name; no token, no artifact link and no sheet_update yet (those are added when it is posted)")
def _():
    base, body = payloads(); email = base["send_success_email"]
    assert body["action"] == "send_success_report" and set(body) == set(email) | {"report"} and all(body[k] == email[k] for k in email if k != "action"), set(body) ^ set(email)
    assert "token" not in body and "artifact_url" not in body and "sheet_update" not in body and body["report"] == report(), "the body must only carry the report"
    assert json.dumps(body, allow_nan=False)

@R.register("request size: the whole request, with a 300-character token and the artifact link added, stays under 19,000 characters, i.e. inside the web app's unchanged 20,000-character limit; three charts, each within its limit (sizes are printed)")
def _():
    base, body = payloads(); full = dict(body, token="t" * TOKEN_ROOM, artifact_url=ARTIFACT_URL, sheet_update={"rows_inserted": 2, "rows_updated": 0}); size = len(json.dumps(full)); sizes = [len(b64png(c)) for c in body["report"]["charts"]]
    print(f"    measured: request {size} characters (limit 20000), PNG bytes per chart {sizes}, HTML not involved here"); assert size < er.MAX_REQUEST_CHARS <= 19000 and len(sizes) == 3 and max(sizes) <= er.MAX_CHART_BYTES, (size, sizes)
    assert len(json.dumps(base["send_success_email"])) < 5000, "the existing request must not have grown"

@R.register("the report is validated before it is sent: wrong PNG signature, a missing chart, a fourth chart, an oversized chart, a request over the size budget, a wrong coverage list are each refused (EmailReportError) with a reason")
def _():
    good = report()
    def mutate(change): bad = json.loads(json.dumps(good)); change(bad); return bad
    cases = {"signature": lambda r: r["charts"][0].update(png_base64="AAAA" + r["charts"][0]["png_base64"][4:]), "missing chart": lambda r: r["charts"].pop(), "four charts": lambda r: r["charts"].append(dict(r["charts"][0])),
             "oversized": lambda r: r["charts"][0].update(png_base64=__import__("base64").b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (er.MAX_CHART_BYTES + 10)).decode()), "not base64": lambda r: r["charts"][1].update(png_base64="***"),
             "coverage": lambda r: r["coverage"].pop(), "huge request": lambda r: r["meta"].update(extra="x" * 30000)}
    for label, change in cases.items():
        try: er.check_report(mutate(change))
        except er.EmailReportError as error: assert str(error) and "\n" not in str(error), (label, str(error))
        else: raise AssertionError(f"{label}: an invalid report was accepted")
    er.check_report(good)

# ---------------------------------------------------------------- limits and conversions, each tested where it is checked (found by mutation checking: a check that another check hides is not a tested check)
def tampered(change):
    bad = json.loads(json.dumps(report())); change(bad); return bad

@R.register("the returns chart always has its zero line inside the scale (all-positive, all-negative and alternating series), so the bars have a base; the line charts need not")
def _():
    n = 30
    for label, btc in (("positive", [0.5 + i * 0.1 for i in range(n)]), ("negative", [-0.5 - i * 0.1 for i in range(n)]), ("alternating", [(-1) ** i * 1.5 for i in range(n)])):
        data, low, high = er.render_chart("returns", btc, btc); assert low < 0 < high, (label, low, high)

@R.register("colours: BTC is the Bitcoin orange and ETH the Ethereum blue (the e-mail text says so), and in the bar chart each day's BTC bar is to the left of its ETH bar")
def _():
    assert er.BTC_COLOUR == (247, 147, 26) and er.ETH_COLOUR == (98, 126, 234), (er.BTC_COLOUR, er.ETH_COLOUR)
    image = decode_png(b64png(charts_by_id()["returns"])); slot = (image["width"] - 2 * er.MARGIN) / 30; checked = 0
    for i in range(30):
        columns = range(int(er.MARGIN + i * slot), int(er.MARGIN + (i + 1) * slot) + 1)
        first = {name: next((x for x in columns if any(colour_of(image, x, y) == rgb for y in range(image["height"]))), None) for name, rgb in (("btc", er.BTC_COLOUR), ("eth", er.ETH_COLOUR))}
        if first["btc"] is not None and first["eth"] is not None: assert first["btc"] < first["eth"], (i, first); checked += 1
    assert checked >= 20, checked

@R.register("check_report looks inside each PNG: a damaged byte (the chunk checksum fails), a truncated file and an empty scale are refused")
def _():
    import base64
    def damaged(r):
        data = bytearray(base64.b64decode(r["charts"][0]["png_base64"])); data[data.index(b"IDAT") + 10] ^= 0xFF; r["charts"][0]["png_base64"] = base64.b64encode(bytes(data)).decode()
    def truncated(r): r["charts"][0]["png_base64"] = base64.b64encode(base64.b64decode(r["charts"][0]["png_base64"])[:-20]).decode()
    def empty_scale(r): r["charts"][0].update(low=5.0, high=5.0)
    for label, change in (("damaged", damaged), ("truncated", truncated), ("empty scale", empty_scale)):
        try: er.check_report(tampered(change))
        except er.EmailReportError as error: assert str(error) and "\n" not in str(error), label; continue
        raise AssertionError(f"{label}: an invalid report was accepted")

@R.register("the size limits are enforced one by one: a valid chart over the limit, a report over the character limit and a drawn chart over the limit are each refused by the check that owns the limit")
def _():
    good, old_chart, old_report = report(), er.MAX_CHART_BYTES, er.MAX_REPORT_CHARS
    try:
        er.MAX_CHART_BYTES = 1000
        try: er.check_report(good)
        except er.EmailReportError as error: assert "larger than" in str(error), str(error)
        else: raise AssertionError("a chart over the byte limit was accepted")
        try: er._chart_entry("indexed", [100 + i * 0.5 + (i % 3) for i in range(31)], [100 - i * 0.4 + (i % 4) for i in range(31)])
        except er.EmailReportError as error: assert "more than the limit" in str(error), str(error)
        else: raise AssertionError("a chart drawn over the byte limit was accepted")
        er.MAX_CHART_BYTES = old_chart; er.MAX_REPORT_CHARS = 5000
        try: er.check_report(good)
        except er.EmailReportError as error: assert "more than the limit" in str(error), str(error)
        else: raise AssertionError("a report over the character limit was accepted")
    finally: er.MAX_CHART_BYTES, er.MAX_REPORT_CHARS = old_chart, old_report
    er.check_report(good)

@R.register("the generation time is written in UTC whatever zone it is given in, and a time without a zone is refused")
def _():
    from datetime import timedelta
    meta = er.build_report(outputs(), datetime(2026, 10, 28, 6, 16, 5, tzinfo=timezone(timedelta(hours=4))))["meta"]; assert meta["generated_at"] == "2026-10-28 02:16:05 UTC", meta
    try: er.build_report(outputs(), datetime(2026, 10, 28, 2, 16, 5))
    except er.EmailReportError as error: assert "timezone" in str(error), str(error)
    else: raise AssertionError("a time without a time zone was accepted")

R.finish()
