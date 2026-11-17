"""Tests for increment 1: the shared candle rules (src/candle_rules.py), the VWAP rules and the expected-instance ledger.

Kraken is mocked; nothing contacts the network. Zero-volume semantics are PROJECT ASSUMPTIONS (A1, A2), not Kraken guarantees: the tests state them as such.
The same rules must give the same verdict in the producer (run_workflow) and in the consumer (dashboard.parse_dataset)."""
import csv, io, json
from decimal import Decimal

from wf_support import *
import candle_rules as cr
import dashboard

R = Runner("candle rules and ledger checks")
check = R.register
raw_rows = lambda pair: next(v for k, v in json.loads(REAL[pair])["result"].items() if k != "last")
WINDOW_DAYS = 31


def good(**over):
    base = {"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "vwap": 101.5, "volume": 12.5, "trade_count": 40}
    base.update(over)
    return base


def vwap_result(**over):
    found = cr.evaluate_candle(good(**over), "BTC", "2026-10-01", "XBTUSD 2026-10-01")
    return next(item for item in found if item["rule_id"] == "CND-VWAP")


# ================================================================ the VWAP cases (project assumptions A1 and A2; case 5 is inconclusive)
@check("vwap case 2: positive volume, vwap inside low/high -> PASS (A2)")
def _(): r = vwap_result(); assert r["outcome"] == cr.PASS and r["case"] == 2
@check("vwap case 2: vwap exactly on low or on high is inside (inclusive, no tolerance)")
def _(): assert vwap_result(vwap=90.0)["outcome"] == cr.PASS and vwap_result(vwap=110.0)["outcome"] == cr.PASS
@check("vwap case 4: positive volume, vwap one unit in the last place above high or below low -> FAIL (no tolerance)")
def _():
    import math
    for value in (math.nextafter(110.0, 1e9), math.nextafter(90.0, 0.0)):
        r = vwap_result(vwap=value); assert r["outcome"] == cr.FAIL and r["case"] == 4, value
@check("vwap case 3: positive volume with vwap 0 -> FAIL")
def _(): r = vwap_result(vwap=0.0); assert r["outcome"] == cr.FAIL and r["case"] == 3
@check("vwap case 1: negative, NaN and infinite vwap -> FAIL")
def _():
    for value in (-1.0, float("nan"), float("inf"), float("-inf")): r = vwap_result(vwap=value); assert r["outcome"] == cr.FAIL and r["case"] == 1, value
@check("vwap case 1: a boolean vwap is not a number -> FAIL")
def _(): assert vwap_result(vwap=True)["outcome"] == cr.FAIL
@check("vwap case 5: positive volume with trade count 0 -> INCONCLUSIVE (not assumed valid) and the message says the output is blocked")
def _():
    r = vwap_result(trade_count=0); assert r["outcome"] == cr.INCONCLUSIVE and r["case"] == 5 and r["severity"] == cr.ERROR and "blocked" in r["message"]
@check("vwap case 6: volume 0, trade count 0, vwap 0 -> PASS under the documented project assumption A1, recorded on the result")
def _(): r = vwap_result(volume=0.0, trade_count=0, vwap=0.0); assert r["outcome"] == cr.PASS and r["case"] == 6 and r["assumption"] == "A1" and "A1" in cr.ASSUMPTIONS
@check("vwap case 6: '-0.0' counts as zero (volume and vwap)")
def _(): r = vwap_result(volume=-0.0, trade_count=0, vwap=-0.0); assert r["outcome"] == cr.PASS and r["case"] == 6
@check("vwap case 7: volume 0 with a non-zero trade count -> FAIL")
def _(): r = vwap_result(volume=0.0, trade_count=3, vwap=0.0); assert r["outcome"] == cr.FAIL and r["case"] == 7
@check("vwap case 8: volume 0, trade count 0 but a positive vwap -> FAIL")
def _(): r = vwap_result(volume=0.0, trade_count=0, vwap=101.5); assert r["outcome"] == cr.FAIL and r["case"] == 8
@check("vwap: judged BLOCKED (never PASS) when volume, trade count, low or high was already judged unusable")
def _():
    assert vwap_result(volume=-1.0)["outcome"] == cr.BLOCKED and vwap_result(trade_count=-1)["outcome"] == cr.BLOCKED
    assert vwap_result(high=80.0)["outcome"] == cr.BLOCKED and vwap_result(low=0.0)["outcome"] == cr.BLOCKED
@check("decimal: the comparison uses the exact decimal text of the float (repr), the same text the CSV stores")
def _():
    for value in (0.1, 79954.9, 2534.8411291, 1e-05, 123456789.12345678): assert cr.decimal_of(value) == Decimal(repr(value)) == cr.decimal_of(repr(value))
@check("decimal: the verdict is identical whether the candle comes from floats (producer) or from CSV text parsed back to floats (consumer)")
def _():
    for value in (90.0, 110.0, 101.123456789012, 109.99999999999999):
        text = repr(value); assert cr.evaluate_candle(good(vwap=value), "BTC", "d", "w")[-1]["outcome"] == cr.evaluate_candle(good(vwap=float(text)), "BTC", "d", "w")[-1]["outcome"]

# ================================================================ the other candle rules keep their messages and order
@check("legacy messages: the price, volume, count, order and range messages are unchanged and keep their order")
def _():
    found = cr.evaluate_candle(good(open=0.0, volume=-1.0, trade_count=-2), "BTC", "d", "XBTUSD d")
    assert cr.problems_of(found) == ["XBTUSD d: open must be a positive number, found 0.0", "XBTUSD d: volume must be a number that is not negative, found -1.0", "XBTUSD d: trade count is negative (-2)"]
    assert cr.problems_of(cr.evaluate_candle(good(high=80.0), "BTC", "d", "XBTUSD d")) == ["XBTUSD d: high (80.0) is below low (90.0)"]
    assert cr.problems_of(cr.evaluate_candle(good(open=120.0), "BTC", "d", "XBTUSD d")) == ["XBTUSD d: open (120.0) is above high (110.0)"]
    assert cr.problems_of(cr.evaluate_candle(good(close=80.0), "BTC", "d", "XBTUSD d")) == ["XBTUSD d: close (80.0) is below low (90.0)"]
@check("each candle is judged by exactly ten rules, in the catalogue order; a rule that could not be judged is BLOCKED, never PASS")
def _():
    found = cr.evaluate_candle(good(low=0.0), "BTC", "d", "w"); assert [f["rule_id"] for f in found] == [r[0] for r in cr.CANDLE_RULES] and len(found) == 10
    assert {f["rule_id"]: f["outcome"] for f in found}["CND-ORDER"] == cr.BLOCKED

# ================================================================ the ledger
def real_windows():
    stamp = int(REAL_NOW.timestamp()) // DAY * DAY
    out = {}
    for pair, asset in (("XBTUSD", "BTC"), ("ETHUSD", "ETH")):
        rows = [r for r in raw_rows(pair) if r[0] < stamp][-WINDOW_DAYS:]
        out[asset] = [(datetime.fromtimestamp(r[0], tz=timezone.utc).strftime("%Y-%m-%d"), {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "vwap": float(r[5]), "volume": float(r[6]), "trade_count": int(r[7])}) for r in rows]
    return out
WINDOWS = real_windows()
def copy_windows(): return {asset: [(d, dict(c)) for d, c in rows] for asset, rows in WINDOWS.items()}

@check("ledger: the plan is 10 rules x 2 assets x 31 days + the cross-pair rule = 621 instances, built from the window alone")
def _():
    ledger, _ordered = cr.evaluate_window(WINDOWS); s = ledger.summary()
    assert s["expected_instances"] == 621 and s["evaluated"] == 621 and s["missing"] == 0 and s["duplicate"] == 0 and s["unexpected"] == 0
    assert len(cr.plan_candle_instances(["BTC", "ETH"], [d for d, _ in WINDOWS["BTC"]])) == 620
@check("ledger: the saved real replies pass every instance -> overall PASS, no non-PASS entry, no assumption applied")
def _():
    s = cr.evaluate_window(WINDOWS)[0].summary(); assert cr.ledger_overall(s) == cr.PASS and s["non_pass"] == [] and s["assumptions_applied"] == []
    assert all(row["pass"] == row["expected_instances"] for row in s["rules"])
@check("ledger: the digests are stable for the same window and change when one outcome changes")
def _():
    a = cr.evaluate_window(WINDOWS)[0].summary(); b = cr.evaluate_window(WINDOWS)[0].summary(); assert a["plan_digest"] == b["plan_digest"] and a["instance_digest"] == b["instance_digest"]
    changed = copy_windows(); changed["BTC"][3][1]["vwap"] = 0.0
    c = cr.evaluate_window(changed)[0].summary(); assert c["plan_digest"] == a["plan_digest"] and c["instance_digest"] != a["instance_digest"]
@check("ledger: one rule id appearing once proves nothing - a single failing candle among 31 makes the overall FAIL and is listed with asset, date and field")
def _():
    w = copy_windows(); w["ETH"][10][1]["trade_count"] = -1
    s = cr.evaluate_window(w)[0].summary(); assert cr.ledger_overall(s) == cr.FAIL
    entry = next(e for e in s["non_pass"] if e["rule_id"] == "CND-COUNT"); assert entry["asset"] == "ETH" and entry["date"] == w["ETH"][10][0] and entry["field"] == "trade_count"
@check("ledger: a missing result is finalised as BLOCKED or NOT RUN and is counted as missing, never PASS")
def _():
    plan = cr.plan_candle_instances(["BTC"], ["2026-10-01", "2026-10-02"]); ledger = cr.Ledger(plan, "candle")
    for found in cr.evaluate_candle(good(), "BTC", "2026-10-01", "w"): ledger.record(found)
    s = ledger.finalize(cr.NOT_RUN).summary()
    assert s["missing"] == 10 and s["evaluated"] == 10 and cr.ledger_overall(s) == cr.NOT_RUN and all(e["outcome"] == cr.NOT_RUN for e in s["non_pass"])
    ledger2 = cr.Ledger(plan, "candle")
    for found in cr.evaluate_candle(good(), "BTC", "2026-10-01", "w"): ledger2.record(found)
    assert cr.ledger_overall(ledger2.finalize(cr.BLOCKED).summary()) == cr.BLOCKED
@check("ledger: a duplicate result and an unexpected result are counted and make the overall FAIL")
def _():
    plan = cr.plan_candle_instances(["BTC"], ["2026-10-01"]); ledger = cr.Ledger(plan, "candle"); found = cr.evaluate_candle(good(), "BTC", "2026-10-01", "w")
    for item in found: ledger.record(item)
    ledger.record(found[0]); ledger.record(cr.evaluate_candle(good(), "BTC", "2026-10-09", "w")[0])
    s = ledger.finalize(cr.BLOCKED).summary(); assert s["duplicate"] == 1 and s["unexpected"] == 1 and cr.ledger_overall(s) == cr.FAIL
@check("ledger: an inconclusive ERROR instance makes the overall INCONCLUSIVE; precedence FAIL > INCONCLUSIVE > BLOCKED > NOT RUN")
def _():
    w = copy_windows(); w["BTC"][2][1]["trade_count"] = 0
    assert cr.ledger_overall(cr.evaluate_window(w)[0].summary()) == cr.INCONCLUSIVE
    w["ETH"][4][1]["vwap"] = 0.0; assert cr.ledger_overall(cr.evaluate_window(w)[0].summary()) == cr.FAIL
@check("ledger: the identical-series heuristic is one planned instance and fails when both assets carry the same data")
def _():
    w = copy_windows(); w["ETH"] = [(d, dict(c)) for d, c in w["BTC"]]
    s = cr.evaluate_window(w)[0].summary(); assert cr.ledger_overall(s) == cr.FAIL and any(e["rule_id"] == "XPR-1" for e in s["non_pass"])
@check("ledger: an empty plan is NOT RUN, never PASS; summarising before finalising is refused")
def _():
    assert cr.ledger_overall(cr.Ledger([], "candle").finalize(cr.NOT_RUN).summary()) == cr.NOT_RUN
    raises(ValueError, lambda: cr.Ledger([("a", "b", "c", "d", "e")], "x").summary())
@check("ledger: zero-volume days applied under A1 are listed in assumptions_applied and still PASS")
def _():
    w = copy_windows(); w["BTC"][5][1].update(volume=0.0, trade_count=0, vwap=0.0); w["ETH"][5][1].update(volume=0.0, trade_count=0, vwap=0.0)
    s = cr.evaluate_window(w)[0].summary(); assert cr.ledger_overall(s) == cr.PASS and s["assumptions_applied"] == ["A1"]

# ================================================================ producer and consumer agree (the confirmed defect: write accepted, downstream rejected)
def body_with(pair, from_end, **fields):
    bodies = {k: json.loads(v) for k, v in REAL.items()}; result = bodies[pair]["result"]; rows = result[[k for k in result if k != "last"][0]]
    stamp = int(REAL_NOW.timestamp()) // DAY * DAY; window = [r for r in rows if r[0] < stamp]; row = window[-from_end]
    index = {"open": 1, "high": 2, "low": 3, "close": 4, "vwap": 5, "volume": 6, "trade_count": 7}
    for name, value in fields.items(): row[index[name]] = value
    return {k: json.dumps(v).encode() for k, v in bodies.items()}

def run_and_parse(bodies):
    d = new_temp_dir(); code, out, err = run_main(bodies, REAL_NOW, d); text = None
    if code == 0: text = read(d, "report_2026-10-26_data.csv")
    return code, err, text, d

@check("agreement: a no-trade day (volume 0, trades 0, vwap 0) is written by run_workflow AND accepted by dashboard.parse_dataset (was: written, then rejected)")
def _():
    code, err, text, d = run_and_parse(body_with("XBTUSD", 5, vwap="0.00000", volume="0.00000000", trade_count=0)); assert code == 0, err
    dataset = dashboard.parse_dataset(text); assert cr.ledger_overall(dataset["candle_ledger"]) == cr.PASS and dataset["candle_ledger"]["assumptions_applied"] == ["A1"]
@check("agreement: every blocked VWAP case stops run_workflow (exit 1, nothing written) and the same CSV values are rejected by dashboard.parse_dataset")
def _():
    for label, fields in (("vwap 0 with volume", {"vwap": "0.0"}), ("vwap above high", {"vwap": "9999999.0"}), ("positive volume, no trades", {"trade_count": 0}),
                          ("volume 0, trades > 0", {"volume": "0.00000000", "vwap": "0.0"}), ("no trades, positive vwap", {"volume": "0.00000000", "trade_count": 0})):
        code, err, text, d = run_and_parse(body_with("ETHUSD", 7, **fields)); assert code == 1 and files_in(d) == [] and "ETHUSD" in err and "VALIDATION FAILED" in err, (label, err)
@check("agreement: the old reports stay byte-identical when a VWAP rule blocks the run")
def _():
    d = new_temp_dir(); old = {"report_2026-10-26.md": "OLD", "report_2026-10-26_data.csv": "OLD"}
    for n, v in old.items(): open(os.path.join(d, n), "w", newline="").write(v)
    code, out, err = run_main(body_with("XBTUSD", 5, trade_count=0), REAL_NOW, d); assert code == 1 and {n: read(d, n) for n in files_in(d)} == old
@check("agreement: dashboard.parse_dataset now enforces the OHLC relations and the VWAP range that run_workflow enforces (a hand-edited CSV no longer passes)")
def _():
    code, err, text, d = run_and_parse(REAL); rows = list(csv.reader(io.StringIO(text, newline=""))); head = rows[0]; at = {n: i for i, n in enumerate(head)}
    def edited(row, **cells):
        copy = [list(r) for r in rows]
        for name, value in cells.items(): copy[row][at[name]] = value
        buf = io.StringIO(); csv.writer(buf, lineterminator="\n").writerows(copy); return buf.getvalue()
    for label, cells in (("high below low", {"btc_high": "1.0", "btc_low": "2.0"}), ("open above high", {"btc_open": "9999999.0"}), ("vwap above high", {"eth_vwap": "9999999.0"}),
                         ("vwap 0 with volume", {"btc_vwap": "0.0"}), ("trades 0 with volume", {"btc_trade_count": "0"})):
        raises(dashboard.DashboardInputError, lambda: dashboard.parse_dataset(edited(6, **cells)), "row 6"), label
@check("consumer: the CSV-side ledger of the real replies equals the producer-side ledger (same plan digest, same instance digest, 621 instances)")
def _():
    code, err, text, d = run_and_parse(REAL); consumer = dashboard.parse_dataset(text)["candle_ledger"]; producer = cr.evaluate_window(WINDOWS)[0].summary()
    assert consumer["plan_digest"] == producer["plan_digest"] and consumer["instance_digest"] == producer["instance_digest"] and consumer["expected_instances"] == 621
@check("the 17 CSV columns are all covered by at least one candle rule or a CSV rule (no column is left without a validation rule)")
def _():
    covered = {"date", "btc_close", "eth_close", "btc_return", "eth_return"}  # date and returns are judged by parse_dataset (real consecutive dates; return = close / previous close - 1)
    for rule_id, columns in cr.COLUMNS_OF_RULE.items():
        for asset in ("btc", "eth"):
            for field in columns: covered.add(f"{asset}_{field}")
    assert covered >= set(dashboard.EXPECTED_COLUMNS), set(dashboard.EXPECTED_COLUMNS) - covered
@check("the fetcher is untouched by this increment (hash pin)")
def _(): import hashlib; assert hashlib.sha256(open(os.path.join(SRC, "fetch_data.py"), "rb").read()).hexdigest() == "ee4f045d9b4455ce13ed1245e04951dfe2942a35debbaa65cefc77dd838147d9"

R.finish()
