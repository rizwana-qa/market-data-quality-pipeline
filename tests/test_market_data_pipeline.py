"""Integration and unit tests for the market-data plausibility checks of src/run_workflow.py, with Kraken mocked (nothing contacts the network).

Written BEFORE the checks exist. For every condition the workflow must reject, the tests prove that: the run exits with 1 and a readable message that names the pair, the date and the problem;
the financial analysis never runs; no new report or CSV is published; and an existing report and CSV stay byte-identical with no temporary or backup file left.
The checks apply to the exact 31 completed, aligned days that are analysed, so a malformed candle OUTSIDE that window must not stop the run. Valid data must pass unchanged."""
import atexit, csv, shutil
from unittest import mock

from wf_support import *

R = Runner("market data pipeline checks")
check = R.register
cleanup = []
atexit.register(lambda: [shutil.rmtree(directory, ignore_errors=True) for directory in cleanup])
TODAY_TS = midnight(TODAY)
WINDOW = analysis.CLOSE_COUNT                                # 31 completed, aligned days


def temp_dir():
    directory = new_temp_dir(); cleanup.append(directory); return directory


SENTINEL_REPORT, SENTINEL_CSV = "OLD REPORT ✓\n", "old,csv\n"


def seeded_dir():
    directory = temp_dir()
    for name, text in (("report_2026-03-19.md", SENTINEL_REPORT), ("report_2026-03-19_data.csv", SENTINEL_CSV)): open(os.path.join(directory, name), "w", encoding="utf-8", newline="").write(text)
    return directory


def untouched(directory):
    return read(directory, "report_2026-03-19.md") == SENTINEL_REPORT and read(directory, "report_2026-03-19_data.csv") == SENTINEL_CSV and files_in(directory) == ["report_2026-03-19.md", "report_2026-03-19_data.csv"]


# ---------------------------------------------------------------- realistic candles (open, high, low, close all different; BTC and ETH different from each other)
FIELDS = {"timestamp": 0, "open": 1, "high": 2, "low": 3, "close": 4, "vwap": 5, "volume": 6, "trade_count": 7}


def make_rows(base, step, count=40, shape=17, unfinished=1):
    """`count` completed days ending yesterday, then `unfinished` rows for today (Kraken's layout: time, open, high, low, close, vwap, volume, count)."""
    rows, previous = [], float(base)
    for i in range(count + unfinished):
        close = base + ((i * shape) % 29) * step + i * step / 10
        high, low = max(previous, close) + 3 * step, min(previous, close) - 3 * step
        rows.append([TODAY_TS - (count - i) * DAY, f"{previous:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}", f"{close:.2f}", "12.5", 100 + i])
        previous = close
    return rows


def with_field(rows, index, field, value):
    """A copy of the rows with one field of one row replaced."""
    copy = [list(row) for row in rows]; copy[index][FIELDS[field]] = value; return copy


def bodies(btc_rows, eth_rows):
    return {"XBTUSD": kraken_body(btc_rows, "XXBTZUSD"), "ETHUSD": kraken_body(eth_rows, "XETHZUSD")}


def market(count=40, unfinished=1): return make_rows(60000, 40, count, 17, unfinished), make_rows(2000, 3, count, 23, unfinished)


def run(btc_rows, eth_rows, directory=None, now=NOW):
    """Run the real workflow entry point with Kraken mocked. Returns (exit code, stdout, stderr, directory, number of times the analysis ran)."""
    directory = directory or temp_dir()
    with mock.patch.object(analysis, "analyse", wraps=analysis.analyse) as spy:
        code, out, err = run_main(bodies(btc_rows, eth_rows), now, directory)
    return code, out, err, directory, spy.call_count


def assert_rejected(btc_rows, eth_rows, *fragments, validation_failed=True):
    assert_bodies_rejected(bodies(btc_rows, eth_rows), *fragments, validation_failed=validation_failed)


def assert_bodies_rejected(reply_bodies, *fragments, validation_failed=True):
    """The workflow, given these exact Kraken replies, must stop with exit 1 and a readable message; the analysis never runs and nothing is published or damaged."""
    directory = seeded_dir()
    with mock.patch.object(analysis, "analyse", wraps=analysis.analyse) as spy:
        code, out, err = run_main(reply_bodies, NOW, directory)
    analysed = spy.call_count
    assert code == 1, f"exit {code}: {err!r}"
    for fragment in fragments: assert fragment in err, f"{fragment!r} not in {err!r}"
    assert (not validation_failed) or "VALIDATION FAILED" in err, err
    assert analysed == 0, "the financial analysis must not run on invalid data"
    assert "Report written" not in out and "Data written" not in out, out
    assert untouched(directory), f"existing outputs changed or extra files appeared: {files_in(directory)}"


def assert_accepted(btc_rows, eth_rows, now=NOW):
    code, out, err, directory, analysed = run(btc_rows, eth_rows, now=now)
    assert code == 0, f"exit {code}: {err!r}"
    assert analysed == 1 and len(files_in(directory)) == 3 and not [n for n in files_in(directory) if n.endswith((".tmp", ".bak"))], files_in(directory)
    return directory


def date_of(rows, index): return format_date(rows[index][0])
def format_date(timestamp): return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


BTC, ETH = market()
LAST, FIRST_IN_WINDOW, MIDDLE = 39, 40 - WINDOW, 25         # row numbers inside the analysed window (rows 9..39 of 40)


# ================================================================ valid data still passes, unchanged
@check("valid: realistic BTC and ETH candles pass: exit 0, the analysis runs once, exactly the report, the 17-column CSV (31 data rows) and the manifest are written")
def _():
    directory = assert_accepted(BTC, ETH); names = files_in(directory); assert [n.split("_")[0] for n in names] == ["report", "report", "report"], names
    rows = list(csv.reader(open(os.path.join(directory, [n for n in names if n.endswith(".csv")][0]), encoding="utf-8")))
    assert len(rows) == 32 and all(len(row) == 17 for row in rows) and rows[0][0] == "date" and rows[-1][0] == date_of(BTC, LAST)


@check("valid: the saved REAL Kraken replies still pass (exit 0, both files written)")
def _():
    directory = temp_dir(); code, out, err = run_main(REAL, REAL_NOW, directory)
    assert code == 0 and files_in(directory) == ["report_2026-10-26.md", "report_2026-10-26_data.csv", "report_2026-10-26_manifest.json"], (code, err)


@check("valid: exactly 31 completed aligned candles are enough")
def _(): btc, eth = market(count=WINDOW); assert_accepted(btc, eth)


@check("valid: the boundaries are inclusive - open equal to high, close equal to low, a flat candle (all four prices equal) and a day without trades (volume 0, trade count 0, vwap 0: project assumption A1) are all valid")
def _():
    btc = with_field(BTC, LAST, "open", BTC[LAST][FIELDS["high"]])
    btc = with_field(btc, MIDDLE, "close", btc[MIDDLE][FIELDS["low"]])
    for field, value in (("volume", "0"), ("trade_count", 0), ("vwap", "0")): btc = with_field(btc, MIDDLE + 1, field, value)   # a day without trades: volume 0, trades 0, vwap 0 (project assumption A1)
    for field in ("open", "high", "low"): btc = with_field(btc, FIRST_IN_WINDOW, field, btc[FIRST_IN_WINDOW][FIELDS["close"]])
    assert_accepted(btc, ETH)


@check("valid: the same window with ONE day that differs between BTC and ETH is not 'identical series'")
def _():
    eth = [list(row) for row in BTC]; eth = with_field(eth, MIDDLE, "volume", "12.6")
    assert_accepted(BTC, eth)


@check("valid: malformed candles OUTSIDE the analysed 31-day window do not stop the run (relations broken, negative trade count) - only the selected window is checked")
def _():
    btc, eth = BTC, ETH
    for i in range(0, FIRST_IN_WINDOW):                                      # the 9 oldest days are not analysed
        btc = with_field(with_field(btc, i, "high", "1.00"), i, "trade_count", -3); eth = with_field(eth, i, "low", "99999.00")
    directory = assert_accepted(btc, eth)
    assert date_of(btc, FIRST_IN_WINDOW - 1) not in read(directory, [n for n in files_in(directory) if n.endswith(".csv")][0])


@check("valid: one unfinished candle per asset is still simply excluded (the existing behaviour)")
def _(): directory = assert_accepted(BTC, ETH); assert f"Unfinished candle excluded: {date_of(BTC, 40)}" in read(directory, [n for n in files_in(directory) if n.endswith(".md")][0])


# ================================================================ invalid data is rejected before the analysis, nothing is published, old files stay
def add_reject_cases():
    pairs = (("BTC", "XBTUSD"), ("ETH", "ETHUSD"))
    for label, name in pairs:
        for where, index in (("last day", LAST), ("middle day", MIDDLE), ("first day of the window", FIRST_IN_WINDOW)):
            side = (lambda rows: (rows, ETH)) if label == "BTC" else (lambda rows: (BTC, rows))
            source = BTC if label == "BTC" else ETH
            day = date_of(source, index)
            cases = (
                ("a negative trade count", with_field(source, index, "trade_count", -5), (name, day, "trade count", "negative")),
                ("high below low", with_field(with_field(source, index, "high", "10.00"), index, "low", "20.00"), (name, day, "high", "below low")),
                ("close above high", with_field(source, index, "close", "9999999.00"), (name, day, "close", "above high")),
                ("close below low", with_field(source, index, "close", "0.01"), (name, day, "close", "below low")),
                ("open above high", with_field(source, index, "open", "9999999.00"), (name, day, "open", "above high")),
                ("open below low", with_field(source, index, "open", "0.01"), (name, day, "open", "below low")),
            )
            for what, rows, fragments in cases:
                check(f"reject: {what}, {label}, {where}")((lambda rows=rows, side=side, fragments=fragments: assert_rejected(*side(rows), *fragments)))
        for field in ("open", "high", "low", "close"):
            source = BTC if label == "BTC" else ETH
            side = (lambda rows: (rows, ETH)) if label == "BTC" else (lambda rows: (BTC, rows))
            if field == "close":                                # a zero close is stopped earlier, when the window's closing prices are selected (existing behaviour, tested here so that it stays)
                check(f"reject: a zero close, {label} (existing check of the analysis window)")((lambda side=side, source=source: assert_rejected(*side(with_field(source, MIDDLE, "close", "0")), "ERROR", "closing prices must be positive", validation_failed=False)))
            else:
                check(f"reject: a zero {field}, {label}")((lambda field=field, name=name, source=source, side=side: assert_rejected(*side(with_field(source, MIDDLE, field, "0")), name, date_of(source, MIDDLE), field, "positive")))


add_reject_cases()


@check("reject: more than one unfinished candle for BTC - two rows for today's UTC day (a duplicate of the day in progress), before they would be excluded")
def _():
    btc, eth = market(unfinished=1); btc = btc + [list(btc[-1])]
    assert_rejected(btc, eth, "XBTUSD", "unfinished", "2", date_of(btc, -1))


@check("reject: more than one unfinished candle for ETH (and three of them)")
def _():
    btc, eth = market(unfinished=1); eth = eth + [list(eth[-1]), list(eth[-1])]
    assert_rejected(btc, eth, "ETHUSD", "unfinished", "3")


@check("reject: identical BTC and ETH series over the whole analysed window (the same data served for both pairs)")
def _(): assert_rejected(BTC, [list(row) for row in BTC], "identical", "BTC and ETH", "31")


@check("reject: ETH shifted by one day against BTC is still caught by the existing date-alignment check")
def _():
    eth = [[row[0] + DAY] + row[1:] for row in ETH[:-1]]
    assert_rejected(BTC, eth, "missing from")


@check("reject: fewer than 31 completed days is still caught (30 days)")
def _(): btc, eth = market(count=WINDOW - 1); assert_rejected(btc, eth, "only 30 completed")


# the fetcher already refuses these at parse time (an ERROR before validation): they must still end the run safely
for label, field, value, fragment in (("a NaN close", "close", "nan", "not finite"), ("an infinite high", "high", "inf", "not finite"), ("a NaN volume", "volume", "nan", "not finite"), ("an infinite volume", "volume", "inf", "not finite"),
                                       ("a negative close", "close", "-1", "negative"), ("a negative volume", "volume", "-1", "negative")):
    check(f"reject (already at parse time): {label}")((lambda field=field, value=value, fragment=fragment: assert_rejected(with_field(BTC, MIDDLE, field, value), ETH, "ERROR", fragment, validation_failed=False)))


# ================================================================ unit tests of the check itself
def candles(rows):
    return fetch_data.parse_candles([row for row in rows if row[0] < TODAY_TS])


def check_plausibility(btc_candles, eth_candles, dates=None):
    dates = dates or sorted(c["timestamp"] for c in btc_candles)[-WINDOW:]
    return run_workflow.check_candle_plausibility({"XBTUSD": btc_candles, "ETHUSD": eth_candles}, dates)


@check("unit: check_candle_plausibility(candles_by_pair, window_dates) returns an empty list for valid candles and a list of readable strings otherwise")
def _():
    assert check_plausibility(candles(BTC), candles(ETH)) == []
    problems = check_plausibility(candles(with_field(BTC, MIDDLE, "trade_count", -1)), candles(ETH))
    assert isinstance(problems, list) and len(problems) == 1 and isinstance(problems[0], str) and "XBTUSD" in problems[0] and date_of(BTC, MIDDLE) in problems[0]


@check("unit: only the given window dates are checked (an older broken candle is ignored), and a window date with no candle is an error, never skipped")
def _():
    broken = candles(with_field(BTC, 0, "trade_count", -9))
    assert check_plausibility(broken, candles(ETH)) == []
    window = sorted(c["timestamp"] for c in broken)[-WINDOW:]
    raises(ValueError, lambda: run_workflow.check_candle_plausibility({"XBTUSD": broken[1:], "ETHUSD": candles(ETH)}, [broken[0]["timestamp"]] + window[:-1]), "no candle")


@check("unit: several problems in several candles and both pairs are all reported (not just the first)")
def _():
    btc = candles(with_field(with_field(BTC, 12, "trade_count", -1), 30, "high", "1.00")); eth = candles(with_field(ETH, 20, "open", "0"))
    problems = check_plausibility(btc, eth); assert len(problems) >= 3 and sum("XBTUSD" in p for p in problems) >= 2 and sum("ETHUSD" in p for p in problems) >= 1, problems


def with_value(candle_list, index, field, value):
    copy = [dict(c) for c in candle_list]; copy[index][field] = value; return copy


def single_value_case(field, value):
    def run_case():
        problems = check_plausibility(with_value(candles(BTC), 20, field, value), candles(ETH))
        assert problems and all(isinstance(p, str) and "XBTUSD" in p and date_of(BTC, 20) in p for p in problems), problems
    return run_case


for label, field, value in (("a boolean open (True)", "open", True), ("a boolean high", "high", True), ("a boolean volume", "volume", True), ("a boolean trade count", "trade_count", True), ("a boolean close (False)", "close", False),
                            ("a NaN open", "open", float("nan")), ("an infinite high", "high", float("inf")), ("a NaN volume", "volume", float("nan")), ("an infinite volume", "volume", float("inf")), ("a negative infinity low", "low", float("-inf")),
                            ("a negative volume", "volume", -1.0), ("a negative trade count", "trade_count", -1), ("a zero close", "close", 0.0), ("a negative open", "open", -5.0),
                            ("a trade count that is not a whole number", "trade_count", 5.5), ("a text price", "close", "100"), ("a missing price (None)", "open", None)):
    check(f"unit: {label} is a problem")(single_value_case(field, value))


@check("unit: a boolean is never accepted as a number even though Python treats True as 1 (it would otherwise pass the price and volume checks)")
def _():
    assert isinstance(True, int)
    assert check_plausibility(with_value(candles(BTC), 20, "volume", True), candles(ETH)) and check_plausibility(with_value(candles(BTC), 20, "trade_count", True), candles(ETH))


@check("unit: a day without trades (volume 0, trade count 0, vwap 0; project assumption A1) is valid")
def _(): assert check_plausibility(with_value(with_value(with_value(candles(BTC), 20, "volume", 0.0), 20, "trade_count", 0), 20, "vwap", 0.0), candles(ETH)) == []


@check("unit: identical OHLC, volume and trade count on EVERY window day is reported once, with a clear explanation; a single different value is not")
def _():
    same = check_plausibility(candles(BTC), candles(BTC)); assert len(same) == 1 and "identical" in same[0] and "BTC and ETH" in same[0]
    assert check_plausibility(candles(BTC), with_value(candles(BTC), 20, "trade_count", 1)) == []


@check("unit: the identical-series check compares open, high, low, close, volume and trade count - not VWAP (which is not part of the rule)")
def _():
    other = [dict(c, vwap=c["vwap"] + 1.0) for c in candles(BTC)]
    assert any("identical" in p for p in check_plausibility(candles(BTC), other))


# ================================================================ response identity and numeric types stop the WORKFLOW (the fetcher refuses them; nothing is analysed or published)
def with_key(reply_body, key):
    """The same Kraken reply, with its candles filed under another result key."""
    import json
    decoded = json.loads(reply_body); candle_key = [k for k in decoded["result"] if k != "last"][0]
    decoded["result"] = {key if k == candle_key else k: v for k, v in decoded["result"].items()}
    return json.dumps(decoded).encode()


@check("identity: a BTC request that receives the ETH reply (key XETHZUSD) stops the workflow with a message naming the pair and both keys; old outputs untouched")
def _():
    reply = bodies(BTC, ETH); reply["XBTUSD"] = reply["ETHUSD"]
    assert_bodies_rejected(reply, "ERROR", "XBTUSD", "XETHZUSD", "XXBTZUSD", validation_failed=False)


@check("identity: an ETH request that receives the BTC reply (key XXBTZUSD) stops the workflow the same way")
def _():
    reply = bodies(BTC, ETH); reply["ETHUSD"] = reply["XBTUSD"]
    assert_bodies_rejected(reply, "ERROR", "ETHUSD", "XXBTZUSD", "XETHZUSD", validation_failed=False)


@check("identity: the right candles under a wrong key (both directions, and a made-up key) are rejected, never relabelled")
def _():
    for pair, key in (("XBTUSD", "XETHZUSD"), ("ETHUSD", "XXBTZUSD"), ("XBTUSD", "FOOBAR")):
        reply = bodies(BTC, ETH); reply[pair] = with_key(reply[pair], key)
        assert_bodies_rejected(reply, "ERROR", pair, "unexpected", validation_failed=False)


@check("identity: the real saved replies, each under its correct key, still run through the whole workflow (exit 0, the report, the CSV and the manifest)")
def _():
    directory = temp_dir(); code, out, err = run_main(REAL, REAL_NOW, directory)
    assert code == 0 and len(files_in(directory)) == 3 and not [n for n in files_in(directory) if n.endswith((".tmp", ".bak"))], (code, err)


def bool_case(pair_name, field, token):
    def run():
        import json
        reply = bodies(BTC, ETH); decoded = json.loads(reply[pair_name]); key = [k for k in decoded["result"] if k != "last"][0]
        decoded["result"][key][MIDDLE][FIELDS[field]] = "@@"
        reply[pair_name] = json.dumps(decoded).encode().replace(b'"@@"', token)
        assert_bodies_rejected(reply, "ERROR", "Candle", validation_failed=False)
    return run


for pair_name in ("XBTUSD", "ETHUSD"):
    for field in ("open", "high", "low", "close", "volume", "trade_count"):
        check(f"numeric: a JSON boolean {field} for {pair_name} stops the workflow before any analysis")(bool_case(pair_name, field, b"true"))


@check("numeric: text that only Python's float() would accept (underscore, whitespace) stops the workflow")
def _():
    for text in ("1_000", " 5"):
        assert_rejected(with_field(BTC, MIDDLE, "volume", text), ETH, "ERROR", "not a number", validation_failed=False)


R.finish()
