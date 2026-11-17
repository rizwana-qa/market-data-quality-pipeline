"""Shared candle validation rules and the expected-instance ledger.

One implementation of the candle rules serves the producer (run_workflow.py, which judges the candles it fetched) and the consumers (dashboard.py and
daily_automation.py, which judge the CSV that was written). Nothing here reads a file, the clock or the network, and nothing here changes a value.

A rule instance is one rule applied to one place: (rule id, scope, asset, date, field). The ledger enumerates every instance that must be judged BEFORE any
judging happens (the plan), then counts the results against that plan: evaluated, missing, duplicate and unexpected instances are all visible, and an instance
that was never judged is never PASS.

VWAP semantics. Kraken documents the candle layout but not how a day without trades is represented, and the saved replies contain no such day. The rules below
are therefore PROJECT ASSUMPTIONS, not a Kraken guarantee:
  A1  a day without trades is volume 0, trade count 0, VWAP 0 (accepted, and recorded as assumption A1 wherever it is used)
  A2  with positive volume the VWAP lies within the candle's low and high, inclusive
Positive volume with a trade count of 0 is not understood, so it is INCONCLUSIVE and blocks the output (fail closed) until the source semantics are established.
Comparisons use Decimal built from the exact text that the CSV stores (repr of the float), with no arithmetic and no tolerance, so the producer and every
consumer compare identical values.
"""

import hashlib
import json
import math
from decimal import Decimal

CATALOGUE_VERSION = "1"
PASS, FAIL, BLOCKED, NOT_RUN, INCONCLUSIVE = "PASS", "FAIL", "BLOCKED", "NOT RUN", "INCONCLUSIVE"
OUTCOMES = (PASS, FAIL, BLOCKED, NOT_RUN, INCONCLUSIVE)
ERROR, WARNING = "ERROR", "WARNING"
ASSUMPTION_A1 = "A1"
ASSUMPTIONS = {
    "A1": "A day without trades is represented as volume 0, trade count 0 and VWAP 0 (project assumption; not documented by Kraken and not seen in the saved replies)",
}

# (rule id, field, severity): the candle rules, in evaluation order. Every candle of every asset is judged by each of them.
CANDLE_RULES = (
    ("CND-PRICE-OPEN", "open", ERROR),
    ("CND-PRICE-HIGH", "high", ERROR),
    ("CND-PRICE-LOW", "low", ERROR),
    ("CND-PRICE-CLOSE", "close", ERROR),
    ("CND-VOL", "volume", ERROR),
    ("CND-COUNT", "trade_count", ERROR),
    ("CND-ORDER", "high/low", ERROR),
    ("CND-RANGE-OPEN", "open", ERROR),
    ("CND-RANGE-CLOSE", "close", ERROR),
    ("CND-VWAP", "vwap", ERROR),
)
CROSS_RULE = ("XPR-1", "window", ERROR)           # BTC and ETH must not carry identical data (the same reply served for both)
PRICE_RULE_IDS = {"open": "CND-PRICE-OPEN", "high": "CND-PRICE-HIGH", "low": "CND-PRICE-LOW", "close": "CND-PRICE-CLOSE"}
SEVERITY_OF = {rule_id: severity for rule_id, _field, severity in CANDLE_RULES}
SEVERITY_OF[CROSS_RULE[0]] = CROSS_RULE[2]
FIELD_OF = {rule_id: field for rule_id, field, _severity in CANDLE_RULES}
FIELD_OF[CROSS_RULE[0]] = CROSS_RULE[1]
# which CSV columns each rule covers (used to count the fields whose rules all passed)
COLUMNS_OF_RULE = {
    "CND-PRICE-OPEN": ("open",), "CND-PRICE-HIGH": ("high",), "CND-PRICE-LOW": ("low",), "CND-PRICE-CLOSE": ("close",),
    "CND-VOL": ("volume",), "CND-COUNT": ("trade_count",), "CND-ORDER": ("high", "low"), "CND-RANGE-OPEN": ("open", "high", "low"),
    "CND-RANGE-CLOSE": ("close", "high", "low"), "CND-VWAP": ("vwap", "volume", "trade_count", "high", "low"),
}


CSV_ONLY_COLUMNS = ("date", "btc_return", "eth_return")        # judged by the CSV contract (consecutive real dates, stored returns recomputed), not by a candle rule
MARKET_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "trade_count")


def fields_validated(candle_summary, csv_contract_passed):
    """(validated, total): how many of the 17 CSV columns are validated.

    A candle column (an asset's open, high, low, close, volume, VWAP or trade count) counts only if the candle ledger is complete (nothing missing, duplicated or unexpected) and no non-PASS
    instance of a rule that covers that column exists for that asset; the identical-series rule invalidates every candle column. The three other columns (date and the two returns) count
    only if the CSV contract check passed. The total is always 17.
    """
    complete = not (candle_summary["missing"] or candle_summary["duplicate"] or candle_summary["unexpected"]) and candle_summary["expected_instances"] > 0
    bad, everything = set(), False
    for entry in candle_summary["non_pass"]:
        if entry["rule_id"] == CROSS_RULE[0]:
            everything = True
        for field in COLUMNS_OF_RULE.get(entry["rule_id"], ()):
            bad.add((entry["asset"].lower(), field))
    validated = 0
    for asset in ("btc", "eth"):
        for field in MARKET_FIELDS:
            if complete and not everything and (asset, field) not in bad:
                validated += 1
    validated += len(CSV_ONLY_COLUMNS) if csv_contract_passed else 0
    return validated, len(MARKET_FIELDS) * 2 + len(CSV_ONLY_COLUMNS)


def is_number(value):
    """A real number (int or float). A bool is not one here, although Python treats True as 1 and would let it pass every numeric check."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def usable_price(value):
    return is_number(value) and math.isfinite(value) and value > 0


def decimal_of(value):
    """The exact decimal text of a float (repr round-trips the double, and the CSV stores that same text), as a Decimal. Strings are taken as they are."""
    return Decimal(value) if isinstance(value, str) else Decimal(repr(value))


def instance_key(rule_id, scope, asset, date, field):
    return (rule_id, scope, asset or "", date or "", field or "")


def result(rule_id, scope, asset, date, field, outcome, expected, actual, explanation, message=None, case=None, assumption=None):
    found = {"rule_id": rule_id, "severity": SEVERITY_OF.get(rule_id, ERROR), "outcome": outcome, "scope": scope, "asset": asset or "", "date": date or "",
             "field": field or "", "expected": expected, "actual": actual, "explanation": explanation, "message": message if message is not None else explanation}
    if case is not None:
        found["case"] = case
    if assumption is not None:
        found["assumption"] = assumption
    return found


def key_of(found):
    return instance_key(found["rule_id"], found["scope"], found["asset"], found["date"], found["field"])


# ---------------------------------------------------------------- the candle rules
def evaluate_candle(candle, asset, date, where):
    """The ten results (one per candle rule, in CANDLE_RULES order) for one candle. `where` is the text that starts every message, e.g. 'XBTUSD 2026-10-26'.

    A rule whose inputs were already judged unusable by an earlier rule is BLOCKED, not PASS: nothing is repaired or skipped silently.
    """
    out = {}

    def put(rule_id, outcome, expected, actual, explanation, message=None, case=None, assumption=None):
        out[rule_id] = result(rule_id, "candle", asset, date, FIELD_OF[rule_id], outcome, expected, actual, explanation, message, case, assumption)

    prices_usable = True
    for field in ("open", "high", "low", "close"):
        value = candle.get(field)
        rule_id = PRICE_RULE_IDS[field]
        if usable_price(value):
            put(rule_id, PASS, "finite and above zero", repr(value), f"{field} is a positive number")
        else:
            prices_usable = False
            message = f"{where}: {field} must be a positive number, found {value!r}"
            put(rule_id, FAIL, "finite and above zero", repr(value), message, message)

    volume = candle.get("volume")
    volume_ok = is_number(volume) and math.isfinite(volume) and volume >= 0
    if volume_ok:
        put("CND-VOL", PASS, "finite and not negative", repr(volume), "volume is a number that is not negative")
    else:
        message = f"{where}: volume must be a number that is not negative, found {volume!r}"
        put("CND-VOL", FAIL, "finite and not negative", repr(volume), message, message)

    trade_count = candle.get("trade_count")
    count_ok = isinstance(trade_count, int) and not isinstance(trade_count, bool) and trade_count >= 0
    if count_ok:
        put("CND-COUNT", PASS, "whole number, not negative", repr(trade_count), "trade count is a whole number that is not negative")
    else:
        if not isinstance(trade_count, int) or isinstance(trade_count, bool):
            message = f"{where}: trade count must be a whole number, found {trade_count!r}"
        else:
            message = f"{where}: trade count is negative ({trade_count})"
        put("CND-COUNT", FAIL, "whole number, not negative", repr(trade_count), message, message)

    blocked = "an earlier price rule failed for this candle"
    if not prices_usable:
        for rule_id in ("CND-ORDER", "CND-RANGE-OPEN", "CND-RANGE-CLOSE"):
            put(rule_id, BLOCKED, "prices usable", "not evaluated", blocked)
        order_ok = False
    else:
        high, low = candle["high"], candle["low"]
        order_ok = high >= low
        if order_ok:
            put("CND-ORDER", PASS, "high not below low", f"high {high!r}, low {low!r}", "high is not below low")
        else:
            message = f"{where}: high ({high}) is below low ({low})"
            put("CND-ORDER", FAIL, "high not below low", f"high {high!r}, low {low!r}", message, message)
        for rule_id, field in (("CND-RANGE-OPEN", "open"), ("CND-RANGE-CLOSE", "close")):
            if not order_ok:
                put(rule_id, BLOCKED, "within [low, high]", "not evaluated", "high is below low for this candle")
            elif candle[field] > high:
                message = f"{where}: {field} ({candle[field]}) is above high ({high})"
                put(rule_id, FAIL, "within [low, high]", repr(candle[field]), message, message)
            elif candle[field] < low:
                message = f"{where}: {field} ({candle[field]}) is below low ({low})"
                put(rule_id, FAIL, "within [low, high]", repr(candle[field]), message, message)
            else:
                put(rule_id, PASS, "within [low, high]", repr(candle[field]), f"{field} lies within low and high")

    _evaluate_vwap(candle, where, put, prices_usable and order_ok, volume_ok, count_ok)
    return [out[rule_id] for rule_id, _field, _severity in CANDLE_RULES]


def _evaluate_vwap(candle, where, put, range_usable, volume_ok, count_ok):
    vwap = candle.get("vwap")
    if not is_number(vwap) or not math.isfinite(vwap) or vwap < 0:
        message = f"{where}: vwap must be a finite number that is not negative, found {vwap!r}"
        put("CND-VWAP", FAIL, "finite and not negative", repr(vwap), message, message, case=1)
        return
    if not volume_ok or not count_ok:
        put("CND-VWAP", BLOCKED, "judged with volume and trade count", "not evaluated", "volume or trade count was judged unusable for this candle")
        return
    volume, count, w = decimal_of(candle["volume"]), candle["trade_count"], decimal_of(vwap)
    if volume > 0:
        if w == 0:
            message = f"{where}: vwap is 0 although the volume is positive ({candle['volume']!r})"
            put("CND-VWAP", FAIL, "vwap above zero when the volume is positive", repr(vwap), message, message, case=3)
        elif count == 0:
            message = (f"{where}: the volume is positive ({candle['volume']!r}) but the trade count is 0; the source semantics of this combination are not established, "
                       "so the candle is inconclusive and the output is blocked (manual review required)")
            put("CND-VWAP", INCONCLUSIVE, "positive volume with at least one trade", "trade count 0", message, message, case=5)
        elif not range_usable:
            put("CND-VWAP", BLOCKED, "vwap within [low, high]", "not evaluated", "low and high were judged unusable for this candle")
        elif not (decimal_of(candle["low"]) <= w <= decimal_of(candle["high"])):
            message = f"{where}: vwap ({vwap}) is outside the range low ({candle['low']}) to high ({candle['high']})"
            put("CND-VWAP", FAIL, "low <= vwap <= high (inclusive, no tolerance)", repr(vwap), message, message, case=4)
        else:
            put("CND-VWAP", PASS, "low <= vwap <= high (inclusive, no tolerance)", repr(vwap), "vwap lies within low and high", case=2)
    elif count > 0:
        message = f"{where}: the volume is 0 but the trade count is {count}"
        put("CND-VWAP", FAIL, "volume 0 implies trade count 0", f"trade count {count}", message, message, case=7)
    elif w == 0:
        put("CND-VWAP", PASS, "assumption A1: no trades means volume 0, trade count 0, vwap 0", "volume 0, trade count 0, vwap 0",
            "a day without trades (project assumption A1)", case=6, assumption=ASSUMPTION_A1)
    else:
        message = f"{where}: the volume and the trade count are 0 but the vwap is {vwap}"
        put("CND-VWAP", FAIL, "no trades implies vwap 0 (assumption A1)", repr(vwap), message, message, case=8)


def cross_pair_result(first_window, second_window, fields, label):
    """XPR-1: the same reply may have been served for both pairs if every compared field is identical on every day of the window."""
    identical = bool(first_window) and all(all(a.get(field) == b.get(field) for field in fields) for a, b in zip(first_window, second_window))
    if identical:
        message = (f"BTC and ETH have identical open, high, low, close, volume and trade-count values on all {len(first_window)} days of the analysed window: "
                   "the same reply may have been served for both pairs")
        return result("XPR-1", "window", label, "", "window", FAIL, "the two assets differ on at least one day", f"identical on {len(first_window)} days", message, message)
    return result("XPR-1", "window", label, "", "window", PASS, "the two assets differ on at least one day", "differ", "the two windows are not identical")


# ---------------------------------------------------------------- the ledger
def plan_candle_instances(assets, dates):
    """Every candle instance that must be judged: each rule, for each asset, on each date. Built from the window alone, never from results."""
    plan = []
    for asset in assets:
        for date in dates:
            for rule_id, field, _severity in CANDLE_RULES:
                plan.append(instance_key(rule_id, "candle", asset, date, field))
    return plan


def plan_cross_instance(label):
    return instance_key(CROSS_RULE[0], "window", label, "", CROSS_RULE[1])


class Ledger:
    """Counts results against a plan that was fixed before judging began."""

    def __init__(self, plan, family):
        self.family = family
        self.plan = sorted(set(plan))
        self._planned = set(self.plan)
        self._results = {}
        self.duplicates = []
        self.unexpected = []
        self.missing = []
        self._final = False

    def record(self, found):
        key = key_of(found)
        if key not in self._planned:
            self.unexpected.append(found)
        elif key in self._results:
            self.duplicates.append(found)
        else:
            self._results[key] = found
        return found

    def finalize(self, unjudged_outcome):
        """Give every planned instance that has no result an explicit outcome (BLOCKED or NOT RUN). Never PASS."""
        if self._final:
            return self
        for key in self.plan:
            if key not in self._results:
                rule_id, scope, asset, date, field = key
                self.missing.append(key)
                self._results[key] = result(rule_id, scope, asset, date, field, unjudged_outcome, "judged", "no result",
                                            "this instance was not judged" + (" because an earlier check failed" if unjudged_outcome == BLOCKED else " (the stage did not run)"))
        self._final = True
        return self

    def results(self):
        return [self._results[key] for key in self.plan if key in self._results]

    def summary(self):
        """The ledger as plain data: per-rule counts, every non-PASS instance, the plan digest and the instance digest."""
        if not self._final:
            raise ValueError("finalize the ledger before summarising it")
        rules, non_pass = {}, []
        for key in self.plan:
            found = self._results[key]
            row = rules.setdefault(key[0], _blank_row(found))
            row["expected_instances"] += 1
            counter = {PASS: "pass", FAIL: "fail", BLOCKED: "blocked", INCONCLUSIVE: "inconclusive", NOT_RUN: "not_run"}[found["outcome"]]
            row[counter] += 1
            if key in self.missing:
                row["missing"] += 1
            else:
                row["evaluated"] += 1
            if found["outcome"] != PASS:
                non_pass.append({name: found[name] for name in ("rule_id", "severity", "outcome", "scope", "asset", "date", "field", "expected", "actual", "explanation")})
        for found in self.duplicates:
            rules.setdefault(found["rule_id"], _blank_row(found))
        duplicate_count, unexpected_count = {}, {}
        for found in self.duplicates:
            duplicate_count[found["rule_id"]] = duplicate_count.get(found["rule_id"], 0) + 1
        for found in self.unexpected:
            unexpected_count[found["rule_id"]] = unexpected_count.get(found["rule_id"], 0) + 1
            rules.setdefault(found["rule_id"], _blank_row(found))
        for rule_id, row in rules.items():
            row["duplicate"] = duplicate_count.get(rule_id, 0)
            row["unexpected"] = unexpected_count.get(rule_id, 0)
        pairs = [[list(key), self._results[key]["outcome"]] for key in self.plan]
        return {
            "family": self.family, "catalogue_version": CATALOGUE_VERSION, "expected_instances": len(self.plan), "evaluated": len(self.plan) - len(self.missing),
            "missing": len(self.missing), "duplicate": len(self.duplicates), "unexpected": len(self.unexpected),
            "plan_digest": digest([list(key) for key in self.plan]), "instance_digest": digest(pairs),
            "rules": [rules[rule_id] for rule_id in sorted(rules)], "non_pass": non_pass,
            "assumptions_applied": sorted({found["assumption"] for found in self._results.values() if found.get("assumption")}),
        }


def _blank_row(found):
    return {"rule_id": found["rule_id"], "severity": found["severity"], "expected_instances": 0, "evaluated": 0, "pass": 0, "fail": 0, "blocked": 0,
            "inconclusive": 0, "not_run": 0, "missing": 0}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def ledger_overall(summary):
    """The outcome of one ledger. PASS needs every planned instance evaluated exactly once with no non-PASS ERROR outcome and no duplicate or unexpected result."""
    if summary["duplicate"] or summary["unexpected"] or any(row["fail"] for row in summary["rules"] if row["severity"] == ERROR):
        return FAIL
    if summary["expected_instances"] == 0:
        return NOT_RUN
    errors = [row for row in summary["rules"] if row["severity"] == ERROR]
    if any(row["inconclusive"] for row in errors):
        return INCONCLUSIVE
    if any(row["blocked"] for row in errors):
        return BLOCKED
    if summary["missing"] or any(row["not_run"] for row in errors):
        return NOT_RUN
    if any(row["evaluated"] != row["expected_instances"] or row["pass"] != row["expected_instances"] for row in errors):
        return INCONCLUSIVE
    return PASS


# ---------------------------------------------------------------- the window, for the producer and for the consumers
def evaluate_window(windows, where_of=None):
    """Judge every candle of every asset. `windows` maps an asset label to a list of (date text, candle dict). Returns (ledger, results in judging order).

    The plan is fixed first, from the asset labels and dates alone. The cross-pair rule is planned and judged when exactly two assets are given.
    """
    assets = list(windows)
    plan, dates_of = [], {}
    for asset in assets:
        dates_of[asset] = [date for date, _candle in windows[asset]]
        plan += plan_candle_instances([asset], dates_of[asset])
    label = "+".join(assets)
    if len(assets) == 2:
        plan.append(plan_cross_instance(label))
    ledger = Ledger(plan, "candle")
    ordered = []
    for asset in assets:
        for date, candle in windows[asset]:
            where = where_of(asset, date) if where_of else f"{asset} {date}"
            for found in evaluate_candle(candle, asset, date, where):
                ordered.append(ledger.record(found))
    if len(assets) == 2:
        first, second = ([candle for _date, candle in windows[asset]] for asset in assets)
        ordered.append(ledger.record(cross_pair_result(first, second, ("open", "high", "low", "close", "volume", "trade_count"), label)))
    ledger.finalize(BLOCKED)
    return ledger, ordered


def problems_of(ordered):
    """The legacy list of problem messages: one per FAIL or INCONCLUSIVE result, in judging order. A BLOCKED result is a consequence, not a separate problem."""
    return [found["message"] for found in ordered if found["outcome"] in (FAIL, INCONCLUSIVE)]
