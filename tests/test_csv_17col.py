"""Tests for the 17-column original CSV (kept OUTSIDE the project). Written BEFORE the production change.

Target header (exactly): date,btc_close,eth_close,btc_return,eth_return,btc_open,btc_high,btc_low,btc_volume,btc_vwap,btc_trade_count,eth_open,eth_high,eth_low,eth_volume,eth_vwap,eth_trade_count
Columns 1-5 must stay exactly as the original workflow wrote them; columns 6-17 are appended.
The pre-change implementation is kept as evidence and is loaded here as the 'old' reference for regression comparisons."""
import csv, importlib.util, io, re, statistics
import independent_calc as ind
from wf_support import *

R = Runner("17-column CSV checks")
check = R.register
cleanup = []
HEADER = "date,btc_close,eth_close,btc_return,eth_return,btc_open,btc_high,btc_low,btc_volume,btc_vwap,btc_trade_count,eth_open,eth_high,eth_low,eth_volume,eth_vwap,eth_trade_count"
COLS = HEADER.split(",")
OLD_PATH = os.path.join(FIXTURES, "run_workflow_BEFORE.py")
spec = importlib.util.spec_from_file_location("run_workflow_before", OLD_PATH); old_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(old_module)

def run_module(module, bodies, now, d):
    """Run module.main with Kraken faked (same method as wf_support.run_main)."""
    out, err = io.StringIO(), io.StringIO()
    def fake_get(url, params=None, **kwargs): return fakehttp.FakeResponse(bodies[params["pair"]])
    with mock.patch("requests.get", side_effect=fake_get), mock.patch("time.sleep"), mock.patch.object(module, "datetime", wraps=datetime) as clock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        clock.now.return_value = now; code = module.main(d)
    return code, out.getvalue(), err.getvalue()
def fresh(): d = new_temp_dir(); cleanup.append(d); return d
def split_rows(text): return list(csv.reader(io.StringIO(text, newline="")))

# ---------------------------------------------------------------- data
def raw_window(name, count=31):
    body = json.loads(REAL[name]); rows = body["result"][[k for k in body["result"] if k != "last"][0]]
    return [r for r in rows if r[0] < int(REAL_NOW.timestamp()) // DAY * DAY][-count:]
RAW = {"btc": raw_window("XBTUSD"), "eth": raw_window("ETHUSD")}
date_of = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

def rich_rows(base, closes, days):
    """Every field of every day differs from every other field, day and asset, so a swapped or shifted column shows up."""
    return [[ts, f"{base + i}.11", f"{base + 5000 + i}.22", f"{base + 9000 + i}.33", str(c), f"{base + 2 * i}.44", f"{base // 10 + i}.55555555", base * 3 + i] for i, (ts, c) in enumerate(zip(days, closes))]
def rich_bodies(count=40, seed=11):
    btc = random_closes(count, seed); eth = random_closes(count, seed + 1, correlated_with=btc)
    days = [midnight(TODAY) - (count - i) * DAY for i in range(count)]
    btc_rows, eth_rows = rich_rows(1000, btc, days), rich_rows(7000, eth, days)
    btc_rows.append([midnight(TODAY), "999999.0", "999999.0", "999999.0", "999999.0", "999999.0", "999999.0", 999999])
    eth_rows.append([midnight(TODAY), "888888.0", "888888.0", "888888.0", "888888.0", "888888.0", "888888.0", 888888])
    return {"XBTUSD": kraken_body(btc_rows, "XXBTZUSD"), "ETHUSD": kraken_body(eth_rows, "XETHZUSD")}, {"btc": btc_rows[:-1][-31:], "eth": eth_rows[:-1][-31:]}
RICH, RICH_WINDOW = rich_bodies()
state = {}
def real_run():
    if "real" not in state:
        d = fresh(); code, out, err = run_module(run_workflow, REAL, REAL_NOW, d); assert code == 0, err
        state["real"] = (read(d, "report_2026-10-26.md"), read(d, "report_2026-10-26_data.csv"))
    return state["real"]
def rich_run():
    if "rich" not in state:
        d = fresh(); code, out, err = run_module(run_workflow, RICH, NOW, d); assert code == 0, err
        state["rich"] = (read(d, "report_2026-03-19.md"), read(d, "report_2026-03-19_data.csv"))
    return state["rich"]
def old_run(bodies, now, name):
    d = fresh(); code, out, err = run_module(old_module, bodies, now, d); assert code == 0, err; return read(d, f"{name}.md"), read(d, f"{name}_data.csv")

# ================================================================ header and shape
@check("header: the first CSV line is exactly the 17-column target header")
def _(): assert real_run()[1].split("\n")[0] == HEADER, real_run()[1].split("\n")[0]
@check("header: 17 distinct names, in the required order, with the original five first")
def _(): assert len(COLS) == 17 and len(set(COLS)) == 17 and COLS[:5] == ["date", "btc_close", "eth_close", "btc_return", "eth_return"]
@check("shape: every row (header included) has exactly 17 fields, there are 31 data rows, LF line endings, one trailing newline (real replies)")
def _():
    text = real_run()[1]; rows = split_rows(text); assert {len(r) for r in rows} == {17} and len(rows) == 32 and "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")
@check("shape: the same holds for the synthetic data with a different date window")
def _(): text = rich_run()[1]; rows = split_rows(text); assert text.split("\n")[0] == HEADER and {len(r) for r in rows} == {17} and len(rows) == 32

# ================================================================ the original five columns are unchanged
@check("original columns: for the real replies, columns 1-5 of every line equal what the ORIGINAL implementation wrote, character for character")
def _():
    old_text = old_run(REAL, REAL_NOW, "report_2026-10-26")[1]; new_rows, old_rows = split_rows(real_run()[1]), split_rows(old_text)
    assert [r[:5] for r in new_rows] == old_rows and len(old_rows[0]) == 5
@check("original columns: the same holds for the synthetic data (all values distinct)")
def _():
    old_text = old_run(RICH, NOW, "report_2026-03-19")[1]; assert [r[:5] for r in split_rows(rich_run()[1])] == split_rows(old_text)
@check("original columns: legacy-output compatibility - the first five columns, written back out as text, equal the CSV text of the legacy implementation (run_workflow_BEFORE.py, fixed clock, real replies) line by line; no project file is read")
def _():
    legacy_lines = old_run(REAL, REAL_NOW, "report_2026-10-26")[1].rstrip("\n").split("\n")
    current_lines = [",".join(r[:5]) for r in split_rows(real_run()[1])]
    assert current_lines == legacy_lines and len(legacy_lines) == 32 and legacy_lines[0] == "date,btc_close,eth_close,btc_return,eth_return"
@check("original columns: dates run from 2026-09-26 to 2026-10-26, 31 consecutive days, and the unfinished 2026-10-27 candle is absent")
def _():
    rows = split_rows(real_run()[1])[1:]; dates = [r[0] for r in rows]; assert dates == [date_of(r[0]) for r in RAW["btc"]] and dates[0] == "2026-09-26" and dates[-1] == "2026-10-26" and "2026-10-27" not in real_run()[1]

# ================================================================ returns: fractions, first row blank
@check("returns: the first data row has empty return columns (4 and 5) but filled market columns")
def _():
    first = split_rows(real_run()[1])[1]; assert first[3] == "" and first[4] == "" and all(first[i] != "" for i in list(range(0, 3)) + list(range(5, 17))), first
@check("returns: columns 4 and 5 stay FRACTIONS (not multiplied by 100): equal to close/previous close - 1 from an independent 60-digit calculation, and all below 0.2 in size")
def _():
    rows = split_rows(real_run()[1])[1:]; closes = {a: [float(r[i]) for r in rows] for a, i in (("btc", 1), ("eth", 2))}
    for a, col in (("btc", 3), ("eth", 4)):
        expected = ind.returns(closes[a]); got = [float(r[col]) for r in rows[1:]]
        assert all(abs(float(e) - g) < 1e-12 for e, g in zip(expected, got)) and max(abs(g) for g in got) < 0.2, a
@check("returns: full precision text is the Python repr (unchanged formatting): the second row return is 0.00020728039826267697 for BTC")
def _(): assert split_rows(real_run()[1])[2][3] == "0.00020728039826267697" and split_rows(real_run()[1])[2][4] == "-0.0027194983953846563"
@check("Excel workflow: =CORREL(D3:D32,E3:E32) on the new CSV (columns D and E, rows 3 to 32) is 0.926100 and equals the correlation printed in the report")
def _():
    rows = split_rows(real_run()[1]); x = [float(r[3]) for r in rows[2:32]]; y = [float(r[4]) for r in rows[2:32]]   # spreadsheet rows 3..32 = list rows 2..31
    shown = re.search(r"Pearson correlation of the 30 aligned daily returns: (\S+)", real_run()[0]).group(1)
    assert len(x) == 30 and f"{statistics.correlation(x, y):.6f}" == shown == "0.926100", (shown, statistics.correlation(x, y))

# ================================================================ appended market columns: right asset, right date
@check("mapping (real replies): all 12 appended columns of all 31 rows equal the raw Kraken values for that date and asset (open, high, low, volume, vwap, count)")
def _():
    rows = split_rows(real_run()[1])[1:]
    for i, row in enumerate(rows):
        for asset, first in (("btc", 5), ("eth", 11)):
            raw = RAW[asset][i]; assert row[0] == date_of(raw[0])
            got = row[first:first + 6]
            assert [float(got[0]), float(got[1]), float(got[2]), float(got[3]), float(got[4])] == [float(raw[1]), float(raw[2]), float(raw[3]), float(raw[6]), float(raw[5])] and int(got[5]) == raw[7], (i, asset, got, raw)
@check("mapping (real replies): the close columns (2 and 3) are the Kraken close of the same day for BTC and ETH respectively")
def _():
    rows = split_rows(real_run()[1])[1:]; assert all(float(r[1]) == float(RAW["btc"][i][4]) and float(r[2]) == float(RAW["eth"][i][4]) for i, r in enumerate(rows))
@check("mapping (synthetic, all values distinct): every appended cell equals the exact expected text for its asset and date; BTC and ETH are never mixed or shifted")
def _():
    rows = split_rows(rich_run()[1])[1:]
    for i, row in enumerate(rows):
        for asset, first in (("btc", 5), ("eth", 11)):
            raw = RICH_WINDOW[asset][i]
            expected = [repr(float(raw[1])), repr(float(raw[2])), repr(float(raw[3])), repr(float(raw[6])), repr(float(raw[5])), str(raw[7])]
            assert row[first:first + 6] == expected and row[0] == date_of(raw[0]), (i, asset, row[first:first + 6], expected)
@check("mapping (synthetic): no value appears in both the BTC columns and the ETH columns (no mixing, no duplicated block)")
def _():
    for row in split_rows(rich_run()[1])[1:]:
        btc_values = set(row[5:11]) | {row[1]}; eth_values = set(row[11:17]) | {row[2]}; assert not (btc_values & eth_values) and len(set(row[5:17])) == 12, row
@check("mapping by DATE not position: with 40 completed days, row 0 uses the candle of the 10th day (offset 9), and the unfinished candle's values (999999/888888) never appear")
def _():
    text = rich_run()[1]; rows = split_rows(text)[1:]
    assert rows[0][5] == repr(float(f"{1000 + 9}.11")) and rows[0][11] == repr(float(f"{7000 + 9}.11")) and "999999" not in text and "888888" not in text

# ================================================================ unchanged report and behaviour
@check("regression: the report text is byte-for-byte identical to the one the ORIGINAL implementation wrote (real replies)")
def _(): assert real_run()[0] == old_run(REAL, REAL_NOW, "report_2026-10-26")[0]
@check("regression: the report text is byte-for-byte identical to the original's for the synthetic data too")
def _(): assert rich_run()[0] == old_run(RICH, NOW, "report_2026-03-19")[0]
@check("regression: the CSV and the report are still the only two files written, with the original names")
def _():
    d = fresh(); assert run_module(run_workflow, REAL, REAL_NOW, d)[0] == 0 and files_in(d) == ["report_2026-10-26.md", "report_2026-10-26_data.csv"]
@check("misaligned dates [existing behaviour]: a missing BTC day -> exit 1, VALIDATION FAILED, nothing written")
def _():
    bodies = synthetic_bodies(drop_btc_day=20)[0]; d = fresh(); code, out, err = run_module(run_workflow, bodies, NOW, d); assert code == 1 and "VALIDATION FAILED" in err and files_in(d) == [], (code, err[:200], files_in(d))
@check("misaligned dates [existing behaviour]: ETH shifted by one day -> exit 1, VALIDATION FAILED, nothing written")
def _():
    bodies = synthetic_bodies(eth_end_offset=1)[0]; d = fresh(); code, out, err = run_module(run_workflow, bodies, NOW, d); assert code == 1 and "VALIDATION FAILED" in err and files_in(d) == [], (code, err[:200], files_in(d))

# ================================================================ helper functions (unit level)
@check("unit: candles_for_dates returns the candles for exactly the given dates, in that order, all fields untouched")
def _():
    c = fetch_data.parse_candles(json.loads(RICH["XBTUSD"])["result"]["XXBTZUSD"]); stamps = [x["timestamp"] for x in c[5:8]]
    assert run_workflow.candles_for_dates(c, stamps) == c[5:8]
@check("unit: candles_for_dates raises ValueError naming the date when a candle is missing (never skips or repairs)")
def _():
    c = fetch_data.parse_candles(json.loads(RICH["XBTUSD"])["result"]["XXBTZUSD"]); gone = c[3]["timestamp"]; rest = [x for x in c if x["timestamp"] != gone]
    raises(ValueError, lambda: run_workflow.candles_for_dates(rest, [c[2]["timestamp"], gone]), date_of(gone))
@check("unit: build_csv(results, btc_candles, eth_candles) gives the header, 31 rows of 17 fields and the first five columns of the old build_csv(results)")
def _():
    c = {a: fetch_data.parse_candles(json.loads(RICH[p])["result"][k]) for a, p, k in (("btc", "XBTUSD", "XXBTZUSD"), ("eth", "ETHUSD", "XETHZUSD"))}
    completed = {a: [x for x in c[a] if x["timestamp"] < midnight(TODAY)] for a in c}
    stamps, b, e = analysis.select_aligned_closes(completed["btc"], completed["eth"]); results = analysis.analyse(stamps, b, e)
    new = run_workflow.build_csv(results, run_workflow.candles_for_dates(completed["btc"], stamps), run_workflow.candles_for_dates(completed["eth"], stamps))
    assert new.split("\n")[0] == HEADER and {len(r) for r in split_rows(new)} == {17} and [r[:5] for r in split_rows(new)] == split_rows(old_module.build_csv(results))

for d in cleanup: shutil.rmtree(d, ignore_errors=True)
R.finish()
