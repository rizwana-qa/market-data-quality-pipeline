"""The interactive BTC/ETH market intelligence report (HTML), generated from one validated CSV dataset.

The design is frozen: interactive_report_template.html is the approved v4 report with every data-dependent token replaced by a named {{slot}}; everything else (layout, CSS,
JavaScript, charts, tooltips, controls and all fixed wording) is byte-for-byte the approved file. One computed data object fills every slot, including the embedded `report-data`
JSON that drives the charts, so the visible figures and the charts can not disagree. The output is deterministic: the same dataset always gives the same bytes (no clock, no
random value, no network).

Metric definitions (each one verified against the figures of the approved 12 November 2026 report; see tests/test_interactive_report.py):
  return over n          close[-1] / close[-1-n] - 1, for n = 7, 15 and 30 intervals
  RSI (14)               100 - 100 / (1 + average gain / average loss) over the last 14 close-to-close changes, plain (simple) averages
  ATR (14)               plain average of the last 14 true ranges; true range = max(high - low, |high - previous close|, |low - previous close|)
  volatility (n)         sample standard deviation of the last n daily returns times the square root of 365
  turnover               volume times VWAP;  ratios compare the latest day with the plain average of the 7 days before it
  average trade notional turnover divided by the trade count
  RSI, Wilder comparison (v5 only)   first averages = plain means of the first 14 changes, then average = (previous x 13 + change) / 14 over all 30 changes; shown next to, never instead of, the simple-average RSI
  close vs VWAP          close / VWAP - 1;  close location (v4 'percentile', v5 'range position'): (close - low) / (high - low) x 100, the position inside the day's range;  range: (high - low) / close
  current drawdown       latest close / highest close of the window - 1 (the 'distance from sample high');  the window peak is the first date the window high was closed
  maximum drawdown       lowest value of close / running closing-price peak - 1 over the 31 closes;  distance from high/low: latest close against the 31-close extreme
  correlation            Pearson correlation of the 30 daily returns (analysis.pearson_correlation)
Presentation rules that are not metrics (documented, deliberately plain):
  a ratio is highlighted when it is at least HIGHLIGHT_RATIO (the project's existing 'clearly higher' mark, +25 %); a performance is 'material' from MATERIAL_MOVE (5 %);
  the close is 'near' the daily low or high within the lowest or highest quarter of the range; 'approximately at VWAP' means within 0.05 %.
The data-dependent sentences are chosen from fixed neutral patterns by comparing the computed numbers; they state comparisons and never predict.
"""

import hashlib
import html
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path

import analysis
import dashboard

TEMPLATE_PATH = Path(__file__).resolve().parent / "interactive_report_template.html"
DESIGN_V4, DESIGN_V5, DESIGN_V6 = "v4", "v5", "v6"
TEMPLATE_PATHS = {DESIGN_V4: TEMPLATE_PATH, DESIGN_V5: Path(__file__).resolve().parent / "interactive_report_template_v5.html", DESIGN_V6: Path(__file__).resolve().parent / "interactive_report_template_v6.html"}
DEFAULT_DESIGN = DESIGN_V6       # the final coral-led design. v4 (the approved golden fixture) and v5 stay selectable with design=... for comparison and for their tests
REPORT_NAME = "market_intelligence_report_{day}.html"
ASSETS = ("BTC", "ETH")
EXPECTED_ROWS = 31
WINDOWS = (7, 14, 30)
RSI_PERIOD = ATR_PERIOD = 14
BASELINE_SESSIONS = 7
DAYS_PER_YEAR = 365
HIGHLIGHT_RATIO = analysis.VOLATILITY_RATIO_HIGH
MATERIAL_MOVE = 0.05
NEAR_EDGE_PERCENT = 25.0
VWAP_NEAR = 0.0005
RSI_LOW, RSI_HIGH = 35.0, 65.0
SLOT = re.compile(r"\{\{([a-z0-9_]+)\}\}")
SOURCE_KRAKEN, SOURCE_SUPPLIED = "kraken_automated", "supplied_csv"
FETCH_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
FETCHED_SPAN = re.compile(r"\n    <span>DATA FETCHED \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC</span>")
MINUS = "−"


class InteractiveReportError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- metrics
def _mean(values):
    return sum(values) / len(values)


def _rsi_value(gain, loss):
    return 50.0 if gain == 0 and loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def wilder_rsi(changes, period=RSI_PERIOD):
    """RSI with Wilder smoothing, shown only as a comparison: the first average gain and loss are the plain averages of the first `period` changes, and every later change is
    folded in as average = (previous average x (period - 1) + change) / period. Needs at least `period` changes; the report passes all 30 close-to-close changes."""
    gains, losses = [max(c, 0.0) for c in changes], [max(-c, 0.0) for c in changes]
    gain, loss = _mean(gains[:period]), _mean(losses[:period])
    for i in range(period, len(changes)):
        gain, loss = (gain * (period - 1) + gains[i]) / period, (loss * (period - 1) + losses[i]) / period
    return _rsi_value(gain, loss)


def _asset_metrics(dataset, asset):
    columns = dashboard.CANDLE_COLUMNS[asset]
    rows = dataset["rows"]
    close = dataset["closes"][asset]
    daily = dataset["returns"][asset]
    pick = lambda field: [float(row[columns[field]]) for row in rows]       # noqa: E731
    high, low, volume, vwap = pick("high"), pick("low"), pick("volume"), pick("vwap")
    trades = [int(row[columns["trade_count"]]) for row in rows]
    last = len(close) - 1
    out = {"asset": asset, "close": close[last], "daily": daily[last]}
    for n in (7, 15, 30):
        out[f"r{n}"] = close[last] / close[last - n] - 1
    changes = [close[i] - close[i - 1] for i in range(last - RSI_PERIOD + 1, last + 1)]
    gain, loss = sum(c for c in changes if c > 0) / RSI_PERIOD, sum(-c for c in changes if c < 0) / RSI_PERIOD
    out["rsi"] = _rsi_value(gain, loss)
    out["rsi_wilder"] = wilder_rsi([close[i] - close[i - 1] for i in range(1, last + 1)])
    true_ranges = [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(last - ATR_PERIOD + 1, last + 1)]
    out["atr"] = _mean(true_ranges)
    out["atr_pct"] = out["atr"] / close[last]
    returns = [value for value in daily if value is not None]
    out["vol14"] = statistics.stdev(returns[-14:]) * math.sqrt(DAYS_PER_YEAR)
    out["vol30"] = statistics.stdev(returns[-30:]) * math.sqrt(DAYS_PER_YEAR)
    peak, worst = close[0], 0.0
    for value in close:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    out["maxdd"] = worst
    out["high31"], out["low31"] = max(close), min(close)
    out["disthigh"], out["distlow"] = close[last] / out["high31"] - 1, close[last] / out["low31"] - 1
    out["curdd"] = out["disthigh"]                                         # current drawdown: the latest close against the highest close of the window
    out["peak_index"] = close.index(out["high31"])                         # the first date on which the window high was closed
    out["peak_date"] = dataset["dates"][out["peak_index"]]
    turnover = [volume[i] * vwap[i] for i in range(len(rows))]
    out["volume"], out["turnover"], out["trades"] = volume[last], turnover[last], trades[last]
    baseline = slice(last - BASELINE_SESSIONS, last)
    out["volratio"] = volume[last] / _mean(volume[baseline]) if _mean(volume[baseline]) else None
    out["turnratio"] = turnover[last] / _mean(turnover[baseline]) if _mean(turnover[baseline]) else None
    out["traderatio"] = trades[last] / _mean(trades[baseline]) if _mean(trades[baseline]) else None
    out["notional"] = turnover[last] / trades[last] if trades[last] else None
    out["vwaprel"] = close[last] / vwap[last] - 1 if vwap[last] else None
    out["loc"] = (close[last] - low[last]) / (high[last] - low[last]) * 100 if high[last] > low[last] else None
    out["range"] = (high[last] - low[last]) / close[last]
    return out


def compute(dataset):
    """The one data object behind every slot: both assets' metrics plus the pair figures."""
    if len(dataset["rows"]) != EXPECTED_ROWS:
        raise InteractiveReportError(f"the report needs exactly {EXPECTED_ROWS} daily rows, the CSV has {len(dataset['rows'])}")
    m = {asset: _asset_metrics(dataset, asset) for asset in ASSETS}
    returns = {asset: [v for v in dataset["returns"][asset] if v is not None] for asset in ASSETS}
    rho = analysis.pearson_correlation(returns["BTC"][-30:], returns["ETH"][-30:])
    if rho is None:
        raise InteractiveReportError("the correlation of the 30 daily returns can not be computed")
    m["pair"] = {"corr": rho, "vol_ratio": m["ETH"]["vol30"] / m["BTC"]["vol30"] if m["BTC"]["vol30"] else None, "date": dataset["dates"][-1]}
    return m


# ---------------------------------------------------------------------------------------------------------------- formatting
def _number(value, decimals):
    return f"{value:,.{decimals}f}"


def pct(value, signed=False, decimals=2):
    """Percent text from a fraction. Zero (as shown) has no sign; a negative uses the true minus sign."""
    if value is None:
        return "n/a"
    text = f"{abs(value) * 100:,.{decimals}f}"
    shown = float(text.replace(",", ""))
    if value < 0 and shown != 0:
        return MINUS + text + "%"
    return ("+" if signed and value > 0 and shown != 0 else "") + text + "%"


def _signed(value):
    return pct(value, signed=True)


def _cls(value):
    """' negative' or ' positive' from the value as shown (two decimals), '' for zero."""
    if value is None:
        return ""
    shown = round(value * 100, 2)
    return " negative" if shown < 0 else " positive" if shown > 0 else ""


def _ratio_cls(value):
    return " positive" if value is not None and value >= HIGHLIGHT_RATIO else ""


def _attr(value):
    """The same classes as a whole class attribute for a cell that has no other class ('' when there is none)."""
    cls = _cls(value).strip()
    return f' class="{cls}"' if cls else ""


def _ratio_attr(value):
    cls = _ratio_cls(value).strip()
    return f' class="{cls}"' if cls else ""


def usd(value, decimals=2):
    return "n/a" if value is None else "$" + _number(value, decimals)


def ratio_x(value):
    return "n/a" if value is None else f"{value:.2f}×"


def ordinal(number):
    number = int(round(number))
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _abs_pct(value):
    return f"{abs(value) * 100:.2f}%"


def _sign_of(value):
    shown = round(value * 100, 2)
    return -1 if shown < 0 else 1 if shown > 0 else 0


def _date_parts(iso):
    year, month, day = (int(part) for part in iso.split("-"))
    names = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    return day, names[month - 1], year


# ---------------------------------------------------------------------------------------------------------------- neutral sentence patterns
def _leader(m, key):
    """(leader, other, gap in percentage points) of a performance key, or (None, None, 0.0) when equal as shown."""
    gap = (m["BTC"][key] - m["ETH"][key]) * 100
    if round(gap, 2) == 0:
        return None, None, 0.0
    return ("BTC", "ETH", gap) if gap > 0 else ("ETH", "BTC", -gap)


def _higher(m, key):
    """(higher, lower) asset of a fraction-valued metric, or (None, None) when both are equal as shown."""
    a, b = m["BTC"][key], m["ETH"][key]
    if round(a * 100, 2) == round(b * 100, 2):
        return None, None
    return ("BTC", "ETH") if a > b else ("ETH", "BTC")


def _activity(a):
    vol, turn = a["volratio"], a["turnratio"]
    if vol is None or turn is None:
        return "mixed"
    return "above" if vol > 1 and turn > 1 else "below" if vol < 1 and turn < 1 else "mixed"


def _vwap_state(a):
    rel = a["vwaprel"]
    if rel is None:
        return "near"
    return "near" if abs(rel) < VWAP_NEAR else "below" if rel < 0 else "above"


def _location(a):
    loc = a["loc"]
    if loc is None:
        return "within its daily range"
    return "near its daily low" if loc <= NEAR_EDGE_PERCENT else "near its daily high" if loc >= 100 - NEAR_EDGE_PERCENT else "in the middle of its daily range"


def _verb(value):
    sign = _sign_of(value)
    return ("declined", "decline") if sign < 0 else ("gained", "gain") if sign > 0 else ("changed by", "change")


def _text_both_below(m):
    signs = [_sign_of(m[asset]["r30"]) for asset in ASSETS]
    if all(s < 0 for s in signs):
        return "Both assets remain " + ("materially " if all(m[a]["r30"] <= -MATERIAL_MOVE for a in ASSETS) else "") + "below their first observed closes."
    if all(s > 0 for s in signs):
        return "Both assets remain " + ("materially " if all(m[a]["r30"] >= MATERIAL_MOVE for a in ASSETS) else "") + "above their first observed closes."
    if all(s == 0 for s in signs):
        return "Both assets are at their first observed closes."
    word = lambda s: "above" if s > 0 else "below" if s < 0 else "at"       # noqa: E731
    return f"BTC is {word(signs[0])} its first observed close, while ETH is {word(signs[1])} it."


def _text_participation_differs(m):
    return _activity(m["BTC"]) != _activity(m["ETH"])


def _callout(m):
    sentences = [_text_both_below(m)]
    leader, other, _ = _leader(m, "r30")
    sentences.append(f"{leader} has outperformed {other} over the 30-return window." if leader else "BTC and ETH performed equally over the 30-return window.")
    daily = {a: m[a]["daily"] for a in ASSETS}
    signs = {a: _sign_of(daily[a]) for a in ASSETS}
    elevated = lambda a: f" accompanied by elevated {a} spot volume" if m[a]["volratio"] is not None and m[a]["volratio"] >= HIGHLIGHT_RATIO else ""      # noqa: E731
    if signs["BTC"] < 0 and signs["ETH"] < 0:
        worse = None if round(daily["BTC"] * 100, 2) == round(daily["ETH"] * 100, 2) else min(ASSETS, key=lambda a: daily[a])
        sentences.append(f"The latest session shows a sharper {worse} decline{elevated(worse)}." if worse else "BTC and ETH declined by the same amount in the latest session.")
    elif signs["BTC"] > 0 and signs["ETH"] > 0:
        better = None if round(daily["BTC"] * 100, 2) == round(daily["ETH"] * 100, 2) else max(ASSETS, key=lambda a: daily[a])
        sentences.append(f"The latest session shows a stronger {better} gain{elevated(better)}." if better else "BTC and ETH gained by the same amount in the latest session.")
    elif signs["BTC"] == 0 and signs["ETH"] == 0:
        sentences.append("Both assets were unchanged in the latest session.")
    else:
        state = {1: "rose", -1: "fell", 0: "was unchanged"}
        sentences.append(f"In the latest session, BTC {state[signs['BTC']]} while ETH {state[signs['ETH']]}.")
    higher, _ = _higher(m, "atr_pct")
    sentences.append(f"{higher}'s recent price-range volatility is higher relative to its price." if higher else "BTC and ETH show similar recent price-range volatility relative to price.")
    pattern = "different" if _text_participation_differs(m) else "similar"
    if all(_sign_of(m[a]["r30"]) < 0 and signs[a] < 0 for a in ASSETS):
        sentences.append(f"The evidence indicates broad market weakness with {pattern} asset-level participation patterns, not a confirmed market reversal.")
    elif all(_sign_of(m[a]["r30"]) > 0 and signs[a] > 0 for a in ASSETS):
        sentences.append(f"The evidence indicates broad market strength with {pattern} asset-level participation patterns, not a confirmed market continuation.")
    else:
        sentences.append("The evidence indicates mixed asset-level conditions, not a confirmed market direction.")
    return " ".join(sentences)


def _finding1(m):
    leader, other, gap = _leader(m, "r30")
    word = {a: _verb(m[a]["r30"]) for a in ASSETS}
    first = f"BTC {word['BTC'][0]} {_abs_pct(m['BTC']['r30'])} over the 30-return period, compared with ETH's {_abs_pct(m['ETH']['r30'])} {word['ETH'][1]}."
    if not leader:
        return "BTC and ETH performed equally", first + " BTC and ETH performed equally during this observation window."
    return f"{leader} demonstrated relative resilience", first + f" {leader} outperformed {other} by approximately {gap:.2f} percentage points, indicating stronger relative performance during this observation window."


def _finding2(m):
    signs = [_sign_of(m[a]["r30"]) for a in ASSETS]
    title = "Both assets remain under downside pressure" if all(s < 0 for s in signs) else "Both assets remain above their first observed closes" if all(s > 0 for s in signs) else "Drawdown profiles differ between the assets"
    text = f"Maximum drawdown reached {_abs_pct(m['BTC']['maxdd'])} for BTC and {_abs_pct(m['ETH']['maxdd'])} for ETH."
    at_low = [a for a in ASSETS if m[a]["close"] == m[a]["low31"]]
    if len(at_low) == 2:
        text += " Both assets finished at their lowest observed closing prices in the sample, indicating that recovery from the observed closing-price peaks had not occurred by the reporting date."
    elif at_low:
        text += f" {at_low[0]} finished at its lowest observed closing price in the sample."
    else:
        text += " Neither asset finished at its lowest observed closing price in the sample."
    return title, text


def _finding3(m):
    higher, lower = _higher(m, "vol30")
    if not higher:
        return "BTC and ETH show similar realised volatility", f"The 30-return annualised realised volatility was {_abs_pct(m['BTC']['vol30'])} for BTC and {_abs_pct(m['ETH']['vol30'])} for ETH."
    times = m[higher]["vol30"] / m[lower]["vol30"] if m[lower]["vol30"] else None
    text = f"{higher}'s 30-return annualised realised volatility was {_abs_pct(m[higher]['vol30'])}, compared with {_abs_pct(m[lower]['vol30'])} for {lower}."
    if times is not None:
        text += f" {higher}'s volatility was approximately {times:.2f} times higher, indicating greater historical return variability in this sample."
    return f"{higher} carries higher realised volatility", text


def _finding4(m):
    rho = m["pair"]["corr"]
    shown = f"{abs(rho):.2f}" if round(rho, 2) == 0 or rho > 0 else MINUS + f"{abs(rho):.2f}"
    head = f"The correlation between BTC and ETH daily returns was approximately {shown}."
    size = abs(rho)
    strength = "weak" if size < analysis.CORRELATION_WEAK_BELOW else "moderate" if size < analysis.CORRELATION_STRONG_FROM else "strong"
    if rho < 0 and strength != "weak":
        return f"The assets show {strength} inverse co-movement", head + " Their returns tended to move in opposite directions during this sample."
    if strength == "strong":
        return "The assets show strong market co-movement", head + " Their returns moved closely together during this sample, suggesting limited historical diversification between the two assets over this particular window."
    if strength == "moderate":
        return "The assets show moderate market co-movement", head + " Their returns moved together to a moderate degree during this sample."
    return "The assets show weak market co-movement", head + " Their returns showed little linear relationship during this sample."


def _finding5(m):
    def clause(asset):
        a, state = m[asset], _activity(m[asset])
        vwap = {"near": "approximately at VWAP", "below": "below VWAP", "above": "above VWAP"}[_vwap_state(a)]
        if state == "above":
            return f"{asset} recorded above-average volume and turnover while closing {_location(a)} and {vwap}."
        if state == "below":
            return f"{asset} activity was below its recent average, while its close was {vwap}."
        return f"{asset} volume and turnover were mixed relative to their recent averages, while its close was {vwap}."
    title = "Latest-session participation patterns differed" if _text_participation_differs(m) else "Latest-session participation patterns were similar"
    return title, f"{clause('BTC')} {clause('ETH')} These observations describe trading conditions, not confirmed institutional buying or selling."


def _participation(m):
    def clause(asset):
        a, state = m[asset], _activity(m[asset])
        near = {"near": "near VWAP", "below": "below VWAP", "above": "above VWAP"}[_vwap_state(a)]
        session = {-1: "negative", 1: "positive", 0: "flat"}[_sign_of(a["daily"])]
        if state == "above":
            return f"{asset} experienced higher-than-average volume and turnover during a {session} session, finishing {_location(a)} and {near}."
        if state == "below":
            return f"{asset}'s volume and turnover were below their recent averages, while its close was {near}."
        return f"{asset}'s volume and turnover were mixed relative to their recent averages, while its close was {near}."
    kind = "different" if _text_participation_differs(m) else "similar"
    return f"{clause('BTC')} {clause('ETH')} This describes {kind} participation patterns but does not establish institutional selling or buying."


def _risk(m):
    atr_high, atr_low = _higher(m, "atr_pct")
    vol_high, _ = _higher(m, "vol30")
    if not atr_high or not vol_high:
        first = "The ATR-to-price ratio and realised volatility are similar for BTC and ETH in this sample."
    elif atr_high == vol_high:
        first = f"{atr_high} has a higher ATR-to-price ratio and realised volatility than {atr_low} in this sample. This indicates greater recent price variability relative to its price."
    else:
        first = f"{atr_high} has a higher ATR-to-price ratio, while {vol_high} has higher realised volatility in this sample. Recent price variability relative to price therefore differs by measure."
    low, high = m["BTC"]["rsi"], m["ETH"]["rsi"]
    if max(low, high) <= RSI_LOW:
        second = "Both RSI readings are near the lower end of the scale, describing weak momentum rather than predicting a rebound."
    elif min(low, high) >= RSI_HIGH:
        second = "Both RSI readings are near the upper end of the scale, describing strong momentum rather than predicting a reversal."
    else:
        second = f"The RSI readings are {low:.1f} for BTC and {high:.1f} for ETH, describing momentum rather than predicting direction."
    return f"{first} {second}"


def _spread(m):
    l30, o30, g30 = _leader(m, "r30")
    l7, o7, g7 = _leader(m, "r7")
    tail = " Relative strength here means comparative asset performance, not the RSI oscillator."
    if not l30:
        return "BTC and ETH performed equally over the 30-return window." + tail
    if l7 == l30:
        return f"{l30} outperformed {o30} by approximately {g30:.2f} percentage points over the 30-return window and {g7:.2f} points over the latest seven-return window." + tail
    first = f"{l30} outperformed {o30} by approximately {g30:.2f} percentage points over the 30-return window"
    if not l7:
        return first + ", while the two performed equally over the latest seven-return window." + tail
    return first + f", while {l7} outperformed {o7} by {g7:.2f} points over the latest seven-return window." + tail


def normalise_provenance(provenance):
    """Where the data came from, as stated in the report. None means a manually supplied CSV: the report then names no fetch time and never implies live data.
    An automated Kraken fetch must carry the fetch time that the pipeline itself recorded (UTC, 'YYYY-MM-DD HH:MM:SS'); a supplied CSV must not carry one. Nothing is ever invented here."""
    if provenance is None:
        return {"kind": SOURCE_SUPPLIED, "fetched_at_utc": None}
    if not isinstance(provenance, dict) or set(provenance) != {"kind", "fetched_at_utc"}:
        raise InteractiveReportError("the provenance must be None or a dict with exactly the keys kind and fetched_at_utc")
    kind, fetched = provenance["kind"], provenance["fetched_at_utc"]
    if kind == SOURCE_SUPPLIED:
        if fetched is not None:
            raise InteractiveReportError("a supplied CSV has no recorded fetch time; none may be stated for it")
    elif kind == SOURCE_KRAKEN:
        if not isinstance(fetched, str) or not FETCH_TIME.fullmatch(fetched):
            raise InteractiveReportError("an automated Kraken fetch needs its recorded UTC fetch time (YYYY-MM-DD HH:MM:SS)")
        try:
            datetime.strptime(fetched, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise InteractiveReportError("the recorded fetch time is not a real UTC date and time")
    else:
        raise InteractiveReportError("unknown data source kind")
    return {"kind": kind, "fetched_at_utc": fetched}


def _source_note(p):
    if p["kind"] == SOURCE_KRAKEN:
        return (f"Kraken public OHLC endpoint: daily candles, one per UTC day, for XBT/USD and ETH/USD, fetched automatically by the daily workflow on {p['fetched_at_utc']} UTC. "
                "The data contain 31 daily observations and 30 aligned return observations. All dates and day boundaries are UTC, and the latest day is the last completed UTC day.")
    return ("Supplied CSV representing Kraken spot OHLC daily records for XBT/USD and ETH/USD. The file contains 31 daily observations and 30 aligned return observations. "
            "It was supplied manually and records no fetch time; its dates are taken as UTC days.")


def _footer_note(p, as_of_long):
    if p["kind"] == SOURCE_KRAKEN:
        return f"This report contains data for {as_of_long}, a completed UTC day, fetched automatically from Kraken on {p['fetched_at_utc']} UTC. It describes that day and is not a real-time market feed."
    return f"This report contains data for {as_of_long} and is based on the supplied historical CSV. It is not a live market feed."


def _intro_source_note(p):
    """The first sentence of the introduction's disclaimer: the data source and, for an automated run, the recorded fetch time (never invented: a supplied CSV states none)."""
    if p["kind"] == SOURCE_KRAKEN:
        day, month, year = _date_parts(p["fetched_at_utc"][:10])
        return f"Based on validated historical Kraken data, fetched on {day} {month} {year} at {p['fetched_at_utc'][11:]} UTC."
    return "Based on validated historical market data from a supplied CSV file; no fetch time is recorded."


def _fetched_span(p):
    return f"\n    <span>DATA FETCHED {p['fetched_at_utc']} UTC</span>" if p["kind"] == SOURCE_KRAKEN else ""


def _g(number):
    return f"{number:g}"


def threshold_note():
    """The wording rules, written from the constants that the sentences are actually chosen with, so that the disclosure can never differ from the implementation."""
    return (f"These rules choose descriptive words only; they are presentation conventions, not market standards or signals. "
            f"Both assets are called materially above or below their first observed closes only when both 30-return moves are at least {_g(MATERIAL_MOVE * 100)}% in size. "
            f"Activity is called elevated, and a ratio is highlighted, from {_g(HIGHLIGHT_RATIO)} times the average of the previous seven sessions; above-average or below-average means more or less than that average. "
            f"A close is called near the daily low or high when it lies in the lowest or highest {_g(NEAR_EDGE_PERCENT)}% of the day's range, and approximately at VWAP when it is less than {_g(VWAP_NEAR * 100)}% away from it. "
            f"RSI is described as near the lower or upper end of the scale at {_g(RSI_LOW)} or below, or {_g(RSI_HIGH)} or above. "
            f"Co-movement is called strong from a correlation of {analysis.CORRELATION_STRONG_FROM:.2f} and moderate from {analysis.CORRELATION_WEAK_BELOW:.2f}, in size.")


def _monitor(m):
    leader, _, _ = _leader(m, "r30")
    higher, _ = _higher(m, "vol30")
    first = f"Monitor whether {leader}'s relative outperformance persists" if leader else "Monitor whether the relative performance of BTC and ETH persists"
    second = f"whether {higher}'s higher volatility moderates" if higher else "whether volatility moderates"
    return (f"{first}, {second}, and whether trading participation confirms or contradicts subsequent price movements. Look for agreement across multiple indicators "
            "rather than treating an isolated technical reading as a directional signal.")


# ---------------------------------------------------------------------------------------------------------------- v5 methodology texts (all figures come from the same computed object)
def _short_date(iso):
    day, month, year = _date_parts(iso)
    return f"{day} {month[:3]} {year}"


def _rsi_zone(value):
    return "low" if value <= RSI_LOW else "high" if value >= RSI_HIGH else "middle"


def rsi_note(m):
    """The Wilder RSI comparison, from the computed values: both readings per asset and whether the two conventions agree about the RSI_LOW / RSI_HIGH marks."""
    same = all(_rsi_zone(m[a]["rsi"]) == _rsi_zone(m[a]["rsi_wilder"]) for a in ASSETS)
    agreement = (f"Both conventions place each asset on the same side of the {_g(RSI_LOW)} and {_g(RSI_HIGH)} marks." if same
                 else f"The two conventions place at least one asset on a different side of the {_g(RSI_LOW)} and {_g(RSI_HIGH)} marks.")
    return (f"Simple-average RSI (primary): BTC {m['BTC']['rsi']:.1f}, ETH {m['ETH']['rsi']:.1f}. Wilder-smoothed RSI (comparison): BTC {m['BTC']['rsi_wilder']:.1f}, ETH {m['ETH']['rsi_wilder']:.1f}. "
            f"{agreement} Both use the same {EXPECTED_ROWS - 1} close-to-close changes; they differ only in how earlier changes are weighted.")


def drawdown_note(m):
    """What the drawdown figures are, how current and maximum drawdown differ, and the limit of an observed window. Peak dates and values are the computed ones."""
    sentences = [f"Drawdown is the close divided by the highest close seen so far in the {EXPECTED_ROWS}-close window, minus 1. Maximum drawdown is the deepest such value in the window; current drawdown is the value on the latest date."]
    for asset in ASSETS:
        a = m[asset]
        same = round(a["curdd"] * 100, 2) == round(a["maxdd"] * 100, 2)
        state = (f"{asset}: current and maximum drawdown are both {pct(a['maxdd'])} (the latest close is the deepest point since the window peak)." if same
                 else f"{asset}: current drawdown {pct(a['curdd'])}, maximum drawdown {pct(a['maxdd'])}.")
        peak = (f"Its window peak is the first close in the sample ({_short_date(a['peak_date'])}), so a higher close before the window can not be seen and the drawdown may understate the decline from an earlier high."
                if a["peak_index"] == 0 else f"Its window peak is {_short_date(a['peak_date'])}; a higher close before the window can not be seen.")
        sentences.append(f"{state} {peak}")
    sentences.append("Charts re-base the peak to the first close of the selected window, so a 7- or 14-day view can differ from the 30-return figures.")
    return " ".join(sentences)


def position_text(value):
    """Where the close sits in the day's high-low range, as a share of the range (0 % = the low, 100 % = the high). It is a position, not a percentile of any distribution."""
    return "n/a" if value is None else f"{value:.0f}%"


# ---------------------------------------------------------------------------------------------------------------- chart data
def _window_json(dataset, k):
    dates = dataset["dates"][-(k + 1):]
    out = {"returns": k, "dates": dates, "return_dates": dates[1:], "index": {}, "close_usd": {}, "return_pct": {}, "drawdown_pct": {}}
    for asset in ASSETS:
        closes = dataset["closes"][asset][-(k + 1):]
        peak, drawdown = closes[0], []
        for value in closes:
            peak = max(peak, value)
            drawdown.append(round((value / peak - 1) * 100, 4) + 0.0)
        out["index"][asset] = [round(value / closes[0] * 100, 4) + 0.0 for value in closes]
        out["close_usd"][asset] = [float(value) for value in closes]
        out["return_pct"][asset] = [round((closes[i] / closes[i - 1] - 1) * 100, 4) + 0.0 for i in range(1, len(closes))]
        out["drawdown_pct"][asset] = drawdown
    return out


def report_json(dataset):
    """The embedded chart data: three windows, compact and ordered, safe inside a script element."""
    text = json.dumps({str(k): _window_json(dataset, k) for k in WINDOWS}, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# ---------------------------------------------------------------------------------------------------------------- slots and rendering
def slot_values(dataset, m=None, provenance=None, design=DESIGN_V4):
    """The named-slot values of the slot-based designs v4 and v5 (v6 builds its sections directly, see render_v6)."""
    m = m or compute(dataset)
    values = _base_slot_values(dataset, m, provenance)
    if design == DESIGN_V5:
        values.update({"rsi_note": rsi_note(m), "drawdown_note": drawdown_note(m)})
        for asset in ASSETS:
            a, p = m[asset], asset.lower() + "_"
            values[p + "loc"] = position_text(a["loc"])
            values[p + "rsi_w"] = f"{a['rsi_wilder']:.1f}"
            values[p + "curdd"], values[p + "curdd_cls"], values[p + "curdd_attr"] = _signed(a["curdd"]), _cls(a["curdd"]), _attr(a["curdd"])
            values[p + "peak"] = _short_date(a["peak_date"])
    elif design != DESIGN_V4:
        raise InteractiveReportError("unknown report design")
    return values


def _base_slot_values(dataset, m, provenance):
    source = normalise_provenance(provenance)
    day, month, year = _date_parts(m["pair"]["date"])
    values = {
        "date": m["pair"]["date"], "date_short": f"{day} {month[:3]} {year}", "as_of_upper": f"{day} {month} {year}".upper(),
        "corr": f"{abs(m['pair']['corr']):.2f}" if m["pair"]["corr"] >= 0 or round(m["pair"]["corr"], 2) == 0 else MINUS + f"{abs(m['pair']['corr']):.2f}",
        "vol_ratio_x": ratio_x(m["pair"]["vol_ratio"]), "callout_exec": _callout(m), "risk_text": _risk(m), "participation_text": _participation(m),
        "spread_text": _spread(m), "monitor_text": _monitor(m), "report_json": report_json(dataset),
        "source_note": _source_note(source), "footer_note": _footer_note(source, f"{day} {month} {year}"), "fetched_span": _fetched_span(source), "threshold_note": threshold_note(),
    }
    for number, (title, text) in enumerate((_finding1(m), _finding2(m), _finding3(m), _finding4(m), _finding5(m)), start=1):
        values[f"f{number}_title"], values[f"f{number}_text"] = title, text
    for asset in ASSETS:
        a, p = m[asset], asset.lower() + "_"
        for key in ("r7", "r15", "r30", "daily", "vwaprel", "disthigh", "distlow", "maxdd"):
            values[p + key] = _signed(a[key])
            values[p + key + "_cls"] = _cls(a[key])
            values[p + key + "_attr"] = _attr(a[key])
        for key in ("volratio", "turnratio", "traderatio"):
            values[p + key] = ratio_x(a[key])
            values[p + key + "_cls"] = _ratio_cls(a[key])
            values[p + key + "_attr"] = _ratio_attr(a[key])
        values.update({p + "close": usd(a["close"]), p + "rsi": f"{a['rsi']:.1f}", p + "atr": usd(a["atr"]), p + "atr_pct": pct(a["atr_pct"]), p + "vol14": pct(a["vol14"]),
                       p + "vol30": pct(a["vol30"]), p + "volume": f"{_number(a['volume'], 2)} {asset}", p + "turnover": usd(a["turnover"] / 1e6) + "M",
                       p + "trades": _number(a["trades"], 0), p + "notional": usd(a["notional"], 0), p + "loc": "n/a" if a["loc"] is None else ordinal(a["loc"]) + " percentile",
                       p + "range": pct(a["range"]), p + "high31": usd(a["high31"]), p + "low31": usd(a["low31"])})
    return values


def render_template(template, values):
    """Fill every {{slot}}: a missing value, a value that is never used or a leftover brace pair is an error. Text slots are HTML-escaped; class and JSON slots are checked instead."""
    used = set(SLOT.findall(template))
    unused = {name for name in set(values) - used if not name.endswith(("_cls", "_attr"))}      # the two class spellings of a value are both computed; the template uses one of them
    if used - set(values) or unused:
        raise InteractiveReportError("the template slots and the computed values differ: missing " + ", ".join(sorted(used - set(values))) + "; unused " + ", ".join(sorted(unused)))

    def fill(match):
        name, value = match.group(1), values[match.group(1)]
        if not isinstance(value, str):
            raise InteractiveReportError(f"slot {name} is not text")
        if name.endswith(("_cls", "_attr")):
            if value not in ("", " negative", " positive", ' class="negative"', ' class="positive"'):
                raise InteractiveReportError(f"slot {name} has an unexpected class")
            return value
        if name == "report_json":
            return value
        if name == "fetched_span":
            if value != "" and not FETCHED_SPAN.fullmatch(value):
                raise InteractiveReportError("slot fetched_span has unexpected content")
            return value
        return html.escape(value, quote=False)

    text = SLOT.sub(fill, template)
    leftover = re.findall(r"\{\{[^}]*\}\}", text.replace(values["report_json"], ""))
    if leftover:
        raise InteractiveReportError("unresolved placeholder in the report: " + leftover[0])
    return text


def render(dataset, provenance=None, design=DEFAULT_DESIGN):
    """The report for one validated dataset (dashboard.parse_dataset) as UTF-8 bytes. provenance: None (a manually supplied CSV) or {'kind': 'kraken_automated', 'fetched_at_utc': 'YYYY-MM-DD HH:MM:SS'}.
    design: v6, the final coral-led report (default), or v4 (the approved golden design) / v5 (the Phase 1 design), kept for comparison."""
    if design not in TEMPLATE_PATHS:
        raise InteractiveReportError("unknown report design")
    if design == DESIGN_V6:
        return render_v6(dataset, provenance)
    template = TEMPLATE_PATHS[design].read_text(encoding="utf-8")
    return render_template(template, slot_values(dataset, provenance=provenance, design=design)).encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------- design v6: the final coral-led report
# One standalone file: the eight sections below, the dashboard's own validated chart / table / export logic (extracted from src/dashboard_template.html, not copied) and the
# vendored Plotly.js, under the same strict Content-Security-Policy as dashboard.html. Every figure comes from the existing functions (dashboard.window_*, analysis.*, compute()).
PROPOSED_DRAWDOWN = 0.10     # PROPOSED, NOT APPROVED: shown only as an exploratory observation, never as a rule or a key finding
PROPOSED_Z = 2.5             # PROPOSED, NOT APPROVED: same
CSV_NAME_FORMAT = "report_{day}_data.csv"
NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum"}
LOGIC_START, LOGIC_END = "/* LOGIC-START */", "/* LOGIC-END */"
V6_PLACEHOLDERS = ("__CSP__", "__ASOF__", "__ISODATE__", "__SOURCE__", "__SECTIONS__", "__FOOTER__", "__LOGIC__", "__DATA_JSON__", "__PLOTLY_JS__")


def _e(text):
    return html.escape(str(text), quote=True)


def _short_date_v6(iso):
    day, month, year = _date_parts(iso)
    return f"{day} {month[:3]} {year}"


def _tone(value):
    if value is None:
        return ""
    return "neg" if round(value * 100, 2) < 0 else "pos" if round(value * 100, 2) > 0 else ""


def _arrow(value):
    if value is None:
        return ""
    return "▼ " if round(value * 100, 2) < 0 else "▲ " if round(value * 100, 2) > 0 else ""


def _chg(value, decimals=2):
    """A signed percentage with its direction in text as well as in colour."""
    return _arrow(value) + pct(value, signed=True, decimals=decimals)


def _dashboard_logic():
    """The dashboard's own validated logic block (chart specifications, row selection, pagination, CSV export), taken from its template so that a fix there reaches this report too."""
    text = dashboard.TEMPLATE_PATH.read_text(encoding="utf-8")
    if text.count(LOGIC_START) != 1 or text.count(LOGIC_END) != 1:
        raise InteractiveReportError("the dashboard template does not contain its logic block exactly once")
    return text[text.index(LOGIC_START):text.index(LOGIC_END) + len(LOGIC_END)]


def _dashboard_windows(dataset):
    """The chart data of the dashboard (same functions, same shape as dashboard.build_payload) without any clock value."""
    out = {}
    for k in dashboard.available_windows(dataset):
        w = dashboard.window_slice(dataset, k)
        out[str(k)] = {"returns": k, "dates": w["dates"], "return_dates": w["return_dates"],
                       "index": {a: dashboard.indexed_series(w["closes"][a]) for a in ASSETS}, "close_usd": w["closes"],
                       "return_pct": {a: [v * 100 for v in w["returns"][a]] for a in ASSETS},
                       "drawdown_pct": {a: dashboard.drawdown_series(w["closes"][a]) for a in ASSETS},
                       "display": dashboard.window_display(dataset, k), "rules": dashboard.window_metrics(dataset, k)["rules"]}
    return out


def _column_meta():
    """Labels with units, display kinds, groups and the column filters of the data table. Kraken's OHLC layout is [time, open, high, low, close, vwap, volume, count]: volume is in the base asset,
    VWAP and the prices in USD (the CSV keeps the project's field names; none is renamed)."""
    labels, kinds, groups = [], [], []
    for index, name in enumerate(dashboard.EXPECTED_COLUMNS):
        asset = name[:3].upper() if name[:4] in ("btc_", "eth_") else ""
        field = name[4:] if asset else name
        if field == "date":
            labels.append("Date (UTC)"), kinds.append("date")
        elif field in ("close", "open", "high", "low", "vwap"):
            labels.append(f"{asset} {field.upper() if field == 'vwap' else field} (USD)"), kinds.append("usd")
        elif field == "return":
            labels.append(f"{asset} return (%)"), kinds.append("pct")
        elif field == "volume":
            labels.append(f"{asset} volume ({asset})"), kinds.append("base")
        else:
            labels.append(f"{asset} trades (count)"), kinds.append("count")
        groups.append("Date" if index == 0 else "Closing prices" if index in (1, 2) else "Daily returns" if index in (3, 4) else f"{asset} candle")
    btc = [0, 1, 3] + sorted(set(dashboard.CANDLE_COLUMNS["BTC"].values()) - {1})
    eth = [0, 2, 4] + sorted(set(dashboard.CANDLE_COLUMNS["ETH"].values()) - {2})
    filters = {"all": None, "BTC": sorted(set(btc)), "ETH": sorted(set(eth)), "prices": [0] + sorted(dashboard.PRICE_COLUMNS), "activity": [0] + sorted(dashboard.VOLUME_COLUMNS + dashboard.TRADE_COUNT_COLUMNS)}
    return {"labels": labels, "kinds": kinds, "groups": groups, "filters": filters}


# ---- additional figures (plain arithmetic on the validated CSV columns; units checked above)
def _daily_activity(dataset):
    out = {}
    for asset in ASSETS:
        c = dashboard.CANDLE_COLUMNS[asset]
        rows = dataset["rows"]
        volume, vwap, close = ([float(r[c[f]]) for r in rows] for f in ("volume", "vwap", "close"))
        out[asset] = {"turnover_musd": [v * w / 1e6 for v, w in zip(volume, vwap)], "trades": [int(r[c["trade_count"]]) for r in rows], "vwap_rel_pct": [(cl / w - 1) * 100 for cl, w in zip(close, vwap)]}
    return out


def _unusual_sessions(dataset, activity):
    found = []
    for asset in ASSETS:
        turnover = activity[asset]["turnover_musd"]
        for i in range(BASELINE_SESSIONS, len(turnover)):
            base = _mean(turnover[i - BASELINE_SESSIONS:i])
            if base and turnover[i] / base >= HIGHLIGHT_RATIO:
                found.append({"asset": asset, "date": dataset["dates"][i], "ratio": turnover[i] / base, "turnover": turnover[i], "ret": dataset["returns"][asset][i]})
    return sorted(found, key=lambda f: (f["date"], f["asset"]))


def _daily_extremes(dataset):
    out = {}
    for asset in ASSETS:
        pairs = [(v, d) for v, d in zip(dataset["returns"][asset], dataset["dates"]) if v is not None]
        out[asset] = {"best": max(pairs), "worst": min(pairs)}
    return out


def _z_moves(dataset):
    out = {}
    for asset in ASSETS:
        pairs = [(v, d) for v, d in zip(dataset["returns"][asset], dataset["dates"]) if v is not None]
        mean, sd = _mean([v for v, _ in pairs]), statistics.stdev([v for v, _ in pairs])
        out[asset] = sorted(({"date": d, "ret": v, "z": (v - mean) / sd} for v, d in pairs), key=lambda m: -abs(m["z"]))[:3]
    return out


def _fisher_interval(rho, n=30):
    z, se = math.atanh(rho), 1 / math.sqrt(n - 3)
    return math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)


# ---- key findings: ONLY the project's existing, approved thresholds select them (5 percentage points, R2 limits, 1.25x, R1 'weak')
def _key_findings(dataset, m):
    out = []
    period = f"{_short_date_v6(dataset['dates'][0])} to {_short_date_v6(dataset['dates'][-1])}"
    leader, other, gap = _leader(m, "r30")
    if leader and gap / 100 >= MATERIAL_MOVE:
        out.append({"label": "Relative performance", "score": gap / 100 / MATERIAL_MOVE, "rule": f"30-return gap of at least {_g(MATERIAL_MOVE * 100)} percentage points (existing 'material' move)",
                    "finding": f"{NAMES[leader]} outperformed {NAMES[other]} by {gap:.2f} percentage points over the 30 returns ({period}).",
                    "evidence": [f"{a}: 7-day {_chg(m[a]['r7'])}, 15-day {_chg(m[a]['r15'])}, 30-day {_chg(m[a]['r30'])}" for a in ASSETS],
                    "interpretation": "Relative resilience over this window; the data say nothing about whether it persists.",
                    "implication": f"Over this period, {NAMES[leader]}'s path was the stronger of the two.",
                    "action": "Track the BTC–ETH gap across the 7-, 15- and 30-return windows in the next reports."})
    ratio = m["pair"]["vol_ratio"]
    if ratio and (ratio >= analysis.VOLATILITY_RATIO_HIGH or ratio <= analysis.VOLATILITY_RATIO_LOW):
        high, low = ("ETH", "BTC") if ratio >= 1 else ("BTC", "ETH")
        out.append({"label": "Volatility", "score": ratio / analysis.VOLATILITY_RATIO_HIGH if ratio >= 1 else analysis.VOLATILITY_RATIO_LOW / ratio,
                    "rule": f"ETH/BTC volatility ratio at or beyond {analysis.VOLATILITY_RATIO_HIGH:.2f} or {analysis.VOLATILITY_RATIO_LOW:.2f} (existing rule R2)",
                    "finding": f"{NAMES[high]} was {ratio if high == 'ETH' else 1 / ratio:.2f} times as volatile as {NAMES[low]} over the 30 returns.",
                    "evidence": [f"{a}: realised volatility {pct(m[a]['vol30'])} (30 returns), {pct(m[a]['vol14'])} (14); ATR / price {pct(m[a]['atr_pct'])}" for a in ASSETS],
                    "interpretation": "Greater historical return variability in this sample.",
                    "implication": "Equal amounts in each asset did not carry equal measured variability in this window.",
                    "action": "Watch whether the 14-day volatility moves back toward the 30-return figure."})
    active = [a for a in ASSETS if max(m[a]["volratio"] or 0, m[a]["turnratio"] or 0) >= HIGHLIGHT_RATIO]
    if active:
        top = max(active, key=lambda a: max(m[a]["volratio"], m[a]["turnratio"]))
        a = m[top]
        day = "down" if _sign_of(a["daily"]) < 0 else "up" if _sign_of(a["daily"]) > 0 else "flat"
        out.append({"label": "Trading activity", "score": max(a["volratio"], a["turnratio"]) / HIGHLIGHT_RATIO,
                    "rule": f"latest-session volume or turnover at least {_g(HIGHLIGHT_RATIO)} times the previous seven sessions (existing highlight level)",
                    "finding": f"{NAMES[top]}'s latest session traded {ratio_x(a['volratio'])} its seven-session average volume ({ratio_x(a['turnratio'])} turnover) on a {day} day of {_chg(a['daily'])}.",
                    "evidence": [f"{x}: volume {ratio_x(m[x]['volratio'])}, turnover {ratio_x(m[x]['turnratio'])}, trades {ratio_x(m[x]['traderatio'])} of the 7-session average; close {_chg(m[x]['vwaprel'])} vs VWAP; range position {position_text(m[x]['loc'])}" for x in ASSETS],
                    "interpretation": f"Activity was elevated while {top} closed {_location(a)}. Daily candles do not show who traded or in which direction.",
                    "implication": "Higher participation on a falling or rising day is a condition to watch, not a signal on its own.",
                    "action": "Check whether the elevated activity persists in the next sessions and whether the close moves back toward VWAP."})
    rho = m["pair"]["corr"]
    if abs(rho) < analysis.CORRELATION_WEAK_BELOW:
        out.append({"label": "Co-movement", "score": analysis.CORRELATION_WEAK_BELOW / max(abs(rho), 1e-9), "rule": f"correlation size below {analysis.CORRELATION_WEAK_BELOW:.2f} (existing rule R1, 'weak')",
                    "finding": f"Daily returns showed weak co-movement (correlation {rho:.2f}).", "evidence": [f"Pearson correlation of the 30 aligned daily returns: {rho:.4f}"],
                    "interpretation": "The two assets moved more independently than usual in this window.", "implication": "Observed diversification between them was higher in this sample.",
                    "action": "Confirm with the next windows before relying on it."})
    return sorted(out, key=lambda c: -c["score"])[:3]


def _exploratory(dataset, m, z_moves):
    """Observations selected by PROPOSED thresholds that have not been approved. They are labelled as exploratory wherever they appear and are never mixed into the R1-R3 findings."""
    items = []
    worst = min(ASSETS, key=lambda a: m[a]["curdd"])
    if -m[worst]["curdd"] >= PROPOSED_DRAWDOWN:
        items.append(f"Current drawdown: {NAMES[worst]} closed {pct(m[worst]['curdd'])} from its window peak of {usd(m[worst]['high31'])} on {_short_date_v6(m[worst]['peak_date'])} (proposed level: {_g(PROPOSED_DRAWDOWN * 100)}% or more).")
    big = [(a, mv) for a in ASSETS for mv in z_moves[a] if abs(mv["z"]) >= PROPOSED_Z]
    for asset, mv in sorted(big, key=lambda x: (x[1]["date"], x[0])):
        items.append(f"Large daily move: {NAMES[asset]} {_chg(mv['ret'])} on {_short_date_v6(mv['date'])}, {abs(mv['z']):.1f} sample standard deviations from the window mean (proposed level: {_g(PROPOSED_Z)} or more).")
    return items


# ---- HTML fragments
ICONS = {"corr": '<circle cx="9" cy="12" r="5"/><circle cx="15" cy="12" r="5"/>', "drawdown": '<path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6z"/><path d="M12 8v5M12 16v.5"/>'}


def _icon(name):
    return f'<svg class="ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{ICONS[name]}</svg>'


def _spark(series, classes):
    """A small inline-SVG line of existing chart series (no new metric): every series is scaled into the same box."""
    values = [v for s in series for v in s]
    low, span = min(values), (max(values) - min(values)) or 1.0
    lines = "".join('<polyline class="{}" points="{}"/>'.format(k, " ".join(f"{i * 120 / (len(s) - 1):.1f},{30 - (v - low) / span * 28:.1f}" for i, v in enumerate(s))) for s, k in zip(series, classes))
    return f'<svg class="spark" viewBox="0 0 120 32" preserveAspectRatio="none" aria-hidden="true" focusable="false">{lines}</svg>'


def _tbl(head, rows, cls="", caption=""):
    th = "".join(f'<th scope="col">{_e(h)}</th>' for h in head)
    body = []
    for row in rows:
        cells = []
        for cell in row:
            text, klass = cell if isinstance(cell, tuple) else (cell, "")
            cells.append(f'<td class="{klass}">{_e(text)}</td>' if klass else f"<td>{_e(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    cap = f'<caption class="sr">{_e(caption)}</caption>' if caption else ""
    return f'<div class="table-wrap" role="region" tabindex="0" aria-label="{_e(caption or "table")}"><table class="tbl {cls}">{cap}<thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _kpi(label, value, note="", cls="", asset=""):
    return (f'<div class="kpi {asset}"><div class="kpi-label">{_e(label)}</div><div class="kpi-value {cls}">{_e(value)}</div>' + (f'<div class="kpi-note">{_e(note)}</div>' if note else "") + "</div>")


def _finding_card(n, c):
    evidence = "".join(f"<li>{_e(x)}</li>" for x in c["evidence"])
    return f"""
      <article class="finding" aria-labelledby="kf-{n}">
        <div class="f-head"><span class="f-num" aria-hidden="true">0{n}</span><span class="chip">{_e(c['label'])}</span></div>
        <h3 id="kf-{n}" class="f-title">{_e(c['finding'])}</h3>
        <dl class="f-grid">
          <div><dt>Supporting evidence</dt><dd><ul>{evidence}</ul></dd></div>
          <div><dt>Interpretation</dt><dd>{_e(c['interpretation'])}</dd></div>
          <div><dt>Decision implication</dt><dd>{_e(c['implication'])}</dd></div>
          <div><dt>Recommended monitoring</dt><dd>{_e(c['action'])}</dd></div>
        </dl>
        <details class="why"><summary>Why this finding is shown</summary><p>Selected because of an existing, documented rule: {_e(c['rule'])}. It is {c['score']:.2f} times that level. At most three findings are shown, ranked by that ratio; none is added to fill space.</p></details>
      </article>"""


def _dq_state(ledger):
    """The validation summary shown in the overview and in section 07, derived from the dataset's own candle-rule ledger."""
    rules = ledger["rules"]
    clean = (ledger["evaluated"] == ledger["expected_instances"] and not any(r["fail"] or r["inconclusive"] for r in rules)
             and not (ledger["missing"] or ledger["duplicate"] or ledger["unexpected"]))
    summary = (f"All <b>{ledger['evaluated']}</b> planned validation checks were evaluated and passed." if clean
               else f"<b>{ledger['evaluated']} of {ledger['expected_instances']}</b> planned validation checks were evaluated; see the validation table.")
    return clean, ("ok" if clean else "warn"), ("All checks passed" if clean else "Checks need attention: see the validation table"), summary


SIGNAL_ICONS = {"Relative performance": '<polyline points="3,17 9,11 13,15 21,6"/><polyline points="15,6 21,6 21,12"/>', "Volatility": '<path d="M5 20V10M12 20V4M19 20v-7"/>',
                "Trading activity": '<path d="M4 18h3V9H4zM10.5 18h3V5h-3zM17 18h3v-6h-3z"/>', "Co-movement": '<circle cx="9" cy="12" r="5"/><circle cx="15" cy="12" r="5"/>'}


def _activity_bars(activity, dates):
    """Estimated daily turnover (volume x VWAP) of both assets on one shared scale, as a self-contained SVG: the same series as the activity chart in section 04."""
    series = {a: activity[a]["turnover_musd"] for a in ASSETS}
    top = max(max(s) for s in series.values()) or 1.0
    step, bars = 320 / len(dates), []
    for i in range(len(dates)):
        for k, a in enumerate(ASSETS):
            h = series[a][i] / top * 84
            bars.append(f'<rect class="{a.lower()}" x="{i * step + k * 4.2 + 0.6:.1f}" y="{92 - h:.1f}" width="3.8" height="{h:.1f}" rx="0.8"/>')
    return (f'<svg class="bars" viewBox="0 0 320 112" role="img" aria-label="Estimated daily turnover in million USD for BTC and ETH, {len(dates)} days, on one shared scale" focusable="false">'
            f'<title>Estimated daily turnover in million USD, BTC and ETH, shared scale (maximum {top:,.1f}M)</title>{"".join(bars)}'
            f'<text x="0" y="108">{_short_date_v6(dates[0])}</text><text x="320" y="108" text-anchor="end">{_short_date_v6(dates[-1])}</text></svg>')


def _overview_cards(dataset, m, key, extremes, activity, wm30, ledger):
    """The summary cards of the overview: risk snapshot, market activity, key findings with the rule outcomes, the latest data rows and the data-quality state. Every value is an existing figure."""
    dates = dataset["dates"]
    ex = extremes
    tiles = [("Realised volatility (30 returns)", f"BTC {pct(m['BTC']['vol30'])} · ETH {pct(m['ETH']['vol30'])}"), ("ATR (14) / price", f"BTC {pct(m['BTC']['atr_pct'])} · ETH {pct(m['ETH']['atr_pct'])}"),
             ("Maximum drawdown", f"BTC {pct(m['BTC']['maxdd'])} · ETH {pct(m['ETH']['maxdd'])}"), ("Largest daily loss", f"BTC {_chg(ex['BTC']['worst'][0])} · ETH {_chg(ex['ETH']['worst'][0])}"),
             ("Negative-return days", f"BTC {sum(1 for v in dataset['returns']['BTC'] if v is not None and v < 0)} of {len(dates) - 1} · ETH {sum(1 for v in dataset['returns']['ETH'] if v is not None and v < 0)} of {len(dates) - 1}"),
             ("RSI (14), simple average", f"BTC {m['BTC']['rsi']:.1f} · ETH {m['ETH']['rsi']:.1f}")]
    tiles_html = "".join(f'<div class="tile"><span class="t-label">{_e(a)}</span><b class="t-val">{_e(b)}</b></div>' for a, b in tiles)
    risk = (f'<div class="ov-card"><div class="ov-head"><h3 class="card-title">Risk Snapshot</h3><a class="viewlink" href="#s-risk">View details <span aria-hidden="true">→</span></a></div><div class="tiles">{tiles_html}</div></div>')
    act = (f'<div class="ov-card"><div class="ov-head"><h3 class="card-title">Market Activity</h3><a class="viewlink" href="#s-activity">View details <span aria-hidden="true">→</span></a></div>'
           f'<p class="ov-sub">Estimated daily turnover, million USD (volume × VWAP), shared scale</p>{_activity_bars(activity, dates)}'
           f'<p class="ov-foot"><span class="dot btc"></span>BTC latest session {_e(ratio_x(m["BTC"]["turnratio"]))} · <span class="dot eth"></span>ETH {_e(ratio_x(m["ETH"]["turnratio"]))} of the 7-session average</p></div>')
    if key:
        sigs = "".join(f'<li class="sig"><span class="sig-ic"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{SIGNAL_ICONS.get(c["label"], SIGNAL_ICONS["Volatility"])}</svg></span><a href="#kf-{i + 1}">{_e(c["finding"])}</a></li>' for i, c in enumerate(key))
    else:
        sigs = '<li class="sig"><span>No finding met the existing evidence rules in this period.</span></li>'
    rules = "".join(f'<li><b>{k}</b> {_e(wm30["rules"][k])}</li>' for k in ("R1", "R2", "R3"))
    sig_card = (f'<div class="ov-card"><div class="ov-head"><h3 class="card-title">Key Findings</h3><a class="viewlink" href="#kf-1">View details <span aria-hidden="true">→</span></a></div>'
                f'<ul class="sigs">{sigs}</ul><ul class="rule-pills" aria-label="Observation rule outcomes">{rules}</ul></div>')
    last = []
    for i in range(len(dates) - 1, len(dates) - 4, -1):
        r = dataset["returns"]
        last.append([_short_date_v6(dates[i]), usd(dataset["closes"]["BTC"][i]), usd(dataset["closes"]["ETH"][i]), (_chg(r["BTC"][i]), _tone(r["BTC"][i])), (_chg(r["ETH"][i]), _tone(r["ETH"][i]))])
    data_card = (f'<div class="ov-card"><div class="ov-head"><h3 class="card-title">Latest Market Data</h3><a class="viewlink" href="#s-data">View details <span aria-hidden="true">→</span></a></div>'
                 f'{_tbl(["Date", "BTC close", "ETH close", "BTC return", "ETH return"], last, "num", "The three latest daily observations")}</div>')
    clean, klass, text, summary = _dq_state(ledger)
    dq = (f'<div class="ov-card dq-mini"><div class="a-head"><div class="eyebrow">Data quality</div><span class="pill {klass}">{_e(text)}</span></div><p>{summary}</p>'
          f'<a class="viewlink" href="#s-quality">View details <span aria-hidden="true">→</span></a></div>')
    return f'<div class="ov-grid three">{risk}{act}{sig_card}</div><div class="ov-grid two">{data_card}{dq}</div>'


def _sections_v6(dataset, m, provenance, activity, extremes, z_moves, sessions, key, exploratory, windows):
    wm30, d30 = dashboard.window_metrics(dataset, 30), dashboard.window_display(dataset, 30)
    ledger, dates = dataset["candle_ledger"], dataset["dates"]
    lo, hi = _fisher_interval(m["pair"]["corr"])
    period = f"{_short_date_v6(dates[0])} – {_short_date_v6(dates[-1])}"
    fetched = provenance["fetched_at_utc"]

    lead, _lead_other, lead_gap = _leader(m, "r30")
    w30 = windows["30"]
    perf_rows = [[NAMES[a], usd(m[a]["close"]), (_chg(m[a]["daily"]), _tone(m[a]["daily"])), (_chg(m[a]["r7"]), _tone(m[a]["r7"])), (_chg(m[a]["r15"]), _tone(m[a]["r15"])),
                  (_chg(m[a]["r30"]), _tone(m[a]["r30"])), (_chg(m[a]["curdd"]), _tone(m[a]["curdd"]))] for a in ASSETS]
    cards = "".join(_finding_card(i + 1, c) for i, c in enumerate(key))
    note = (f'<p class="note">{len(key)} of up to 3 findings met the existing evidence rules in this period; none is added to fill space.</p>' if 0 < len(key) < 3
            else '<p class="note">No finding met the existing evidence rules in this period.</p>' if not key else "")
    actions = "".join(f"<li><strong>{_e(c['label'])}:</strong> {_e(c['action'])}</li>" for c in key)
    top = key[0] if key else None
    columns = ((("Key finding", top["finding"]), ("Decision implication", top["implication"]), ("Recommended monitoring", top["action"])) if top
               else (("Key finding", "No finding met the existing evidence rules in this period."), ("Decision implication", "None is stated without evidence."), ("Recommended monitoring", _monitor(m))))
    cols_html = "".join(f"<div><h4>{_e(h)}</h4><p>{_e(x)}</p></div>" for h, x in columns)

    def asset_kpi(asset):
        a = m[asset]
        return (f'<div class="kpi {asset.lower()}"><div class="kpi-top"><span class="coin {asset.lower()}">{asset}</span><span class="kpi-label">{NAMES[asset]} · 30-return performance</span></div>'
                f'<div class="kpi-value {_tone(a["r30"])}">{_e(_chg(a["r30"]))}</div><div class="kpi-note">Latest close {_e(usd(a["close"]))} · day <span class="{_tone(a["daily"])}">{_e(_chg(a["daily"]))}</span></div>'
                f'{_spark([w30["index"][asset]], [asset.lower()])}</div>')

    corr_text = f"{m['pair']['corr']:.2f}"
    dd_text = f"BTC {pct(m['BTC']['maxdd'])} · ETH {pct(m['ETH']['maxdd'])}"
    kpi_row = (asset_kpi("BTC") + asset_kpi("ETH")
               + f'<div class="kpi corr"><div class="kpi-top"><span class="coin">{_icon("corr")}</span><span class="kpi-label">BTC / ETH correlation</span></div><div class="kpi-value">{_e(corr_text)}</div><div class="kpi-note">30 aligned daily returns · rule R1: {_e(wm30["rules"]["R1"])}</div></div>'
               + f'<div class="kpi risk"><div class="kpi-top"><span class="coin">{_icon("drawdown")}</span><span class="kpi-label">Maximum drawdown · window</span></div><div class="kpi-value neg"><span class="nw">BTC {_e(pct(m['BTC']['maxdd']))}</span> <span class="nw">ETH {_e(pct(m['ETH']['maxdd']))}</span></div><div class="kpi-note">From the highest close inside the window</div>{_spark([w30["drawdown_pct"]["BTC"], w30["drawdown_pct"]["ETH"]], ["btc", "eth"])}</div>')
    gap_text = f"{lead} ahead by {lead_gap:.2f} pp" if lead else "BTC and ETH equal"
    overview_html = _overview_cards(dataset, m, key, extremes, activity, wm30, ledger)
    sec1 = f"""
<section class="section" id="s-exec" aria-labelledby="h-exec">
  <div class="kicker">01 · Overview</div>
  <h2 id="h-exec">Market Overview</h2>
  <p class="intro">Market performance, key relationships and notable observations across the reporting period.</p>
  <div class="kpi-row four lead-row">{kpi_row}</div>
  <div class="exec-grid">
    <div class="assessment">
      <div class="a-head"><div class="eyebrow">Overall market assessment</div><span class="pill" title="Observation rule R2, applied to the 30 returns">R2 · {_e(wm30['rules']['R2'])}</span></div>
      <p>{_e(_callout(m))}</p>
      <ul class="assess-stats">
        <li><span>30-return gap</span><b>{_e(gap_text)}</b></li>
        <li><span>Correlation (30 returns)</span><b>{_e(corr_text)} · {_e(wm30['rules']['R1'])}</b></li>
        <li><span>ETH / BTC volatility</span><b>{_e(ratio_x(m['pair']['vol_ratio']))} · {_e(wm30['rules']['R2'])}</b></li>
        <li><span>Maximum drawdown</span><b>{_e(dd_text)}</b></li>
      </ul>
      <div class="a-cols">{cols_html}</div>
    </div>
    <figure class="chart-card ovw">
      <figcaption><span class="chart-title">Market Performance</span><span class="chart-sub">Index (first close of the window = 100)</span></figcaption>
      <div class="seg" role="group" aria-label="Overview window"><button type="button" data-ovw="7" aria-pressed="false">7D</button><button type="button" data-ovw="14" aria-pressed="false">14D</button><button type="button" data-ovw="30" aria-pressed="true">30D</button></div>
      <div class="chart" id="chart-overview" role="img" aria-label="Normalised closing prices of BTC and ETH, first close of the window = 100"></div>
    </figure>
  </div>
  {overview_html}
  <h3 class="sub">Findings in Detail</h3>
  <div class="findings">{cards}</div>
  {note}
  <h3 class="sub">Performance Overview</h3>
  {_tbl(["Asset", "Latest close", "Day", "7-day", "15-day", "30-day", "Current drawdown"], perf_rows, "num", "BTC and ETH performance overview")}
  <div class="callout"><div class="eyebrow">Recommended monitoring priorities</div><ul class="plain">{actions}</ul><p>{_e(_monitor(m))}</p></div>
</section>"""

    spread_rows = []
    for n in (7, 15, 30):
        g = m["BTC"][f"r{n}"] - m["ETH"][f"r{n}"]
        spread_rows.append([f"{n} returns", (_chg(m["BTC"][f"r{n}"]), _tone(m["BTC"][f"r{n}"])), (_chg(m["ETH"][f"r{n}"]), _tone(m["ETH"][f"r{n}"])), "Equal" if round(g * 100, 2) == 0 else f"{'BTC' if g > 0 else 'ETH'} by {abs(g) * 100:.2f} pp"])
    sec2 = f"""
<section class="section" id="s-perf" aria-labelledby="h-perf">
  <div class="kicker">02 · Performance</div>
  <h2 id="h-perf">Market Performance</h2>
  <p class="intro">Bitcoin and Ethereum returns over 7, 15 and 30 days, and the gap between them. The data contain 31 daily closes and therefore 30 daily returns (a "30-return" window): the first close is the starting level and has no return of its own. Each return compares the latest close with the close 7, 15 or 30 days earlier, and gaps are in percentage points (pp).</p>
  <div class="grid-2">
    <div>{_tbl(["Window", "BTC", "ETH", "Leader and gap"], spread_rows, "num", "Returns by window")}
      <div class="callout"><div class="eyebrow">Relative strength</div><p>{_e(_spread(m))}</p></div></div>
    <figure class="chart-card"><figcaption><span class="chart-title">Returns by Window</span><span class="chart-sub">Percent change of the latest close against the close 7, 15 and 30 days earlier</span></figcaption><div class="chart" id="chart-windows" role="img" aria-label="Grouped bars of BTC and ETH returns over 7, 15 and 30 intervals"></div></figure>
  </div>
  <figure class="chart-card wide"><figcaption><span class="chart-title">Cumulative Return Since the First Close</span><span class="chart-sub">Percent change of each close against the first close of the 31-close window</span></figcaption><div class="chart" id="chart-cumulative" role="img" aria-label="Cumulative return of BTC and ETH since the first close"></div></figure>
  <p class="note">Daily returns, the normalised price path and the drawdown are in the interactive charts (section 05), with window and asset controls.</p>
</section>"""

    ex = extremes
    risk_kpis = "".join([
        _kpi("Realised volatility · 30 returns", f"BTC {pct(m['BTC']['vol30'])} · ETH {pct(m['ETH']['vol30'])}", "Annualised: sample standard deviation × √365"),
        _kpi("Realised volatility · 14 returns", f"BTC {pct(m['BTC']['vol14'])} · ETH {pct(m['ETH']['vol14'])}", "Annualised"),
        _kpi("ETH / BTC volatility", ratio_x(m["pair"]["vol_ratio"]), f"Rule R2: {wm30['rules']['R2']}"),
        _kpi("ATR (14) / price", f"BTC {pct(m['BTC']['atr_pct'])} · ETH {pct(m['ETH']['atr_pct'])}", f"ATR: BTC {usd(m['BTC']['atr'])}, ETH {usd(m['ETH']['atr'])}"),
        _kpi("Maximum drawdown · window", f"BTC {pct(m['BTC']['maxdd'])} · ETH {pct(m['ETH']['maxdd'])}", f"Window peaks {_short_date_v6(m['BTC']['peak_date'])} / {_short_date_v6(m['ETH']['peak_date'])}", "neg"),
        _kpi("Largest daily gain", f"BTC {_chg(ex['BTC']['best'][0])} · ETH {_chg(ex['ETH']['best'][0])}", f"BTC {_short_date_v6(ex['BTC']['best'][1])}, ETH {_short_date_v6(ex['ETH']['best'][1])}", "pos"),
        _kpi("Largest daily loss", f"BTC {_chg(ex['BTC']['worst'][0])} · ETH {_chg(ex['ETH']['worst'][0])}", f"BTC {_short_date_v6(ex['BTC']['worst'][1])}, ETH {_short_date_v6(ex['ETH']['worst'][1])}", "neg"),
        _kpi("Negative-return days", f"BTC {d30['negative_BTC'].split(' (')[0]} · ETH {d30['negative_ETH'].split(' (')[0]}", f"Shared down days {d30['joint']} · rule R3: {wm30['rules']['R3']}"),
        _kpi("RSI (14)", f"BTC {m['BTC']['rsi']:.1f} · ETH {m['ETH']['rsi']:.1f}", f"Simple average (primary). Wilder comparison: BTC {m['BTC']['rsi_wilder']:.1f}, ETH {m['ETH']['rsi_wilder']:.1f}"),
    ])
    sec3 = f"""
<section class="section" id="s-risk" aria-labelledby="h-risk">
  <div class="kicker">03 · Risk</div>
  <h2 id="h-risk">Risk and Volatility</h2>
  <p class="intro">Historical variability, drawdown and downside behaviour in this 30-day window. These are observations of what happened, not predictions.</p>
  <div class="kpi-row three">{risk_kpis}</div>
  <div class="grid-2">
    <figure class="chart-card"><figcaption><span class="chart-title">Daily Return Distribution</span><span class="chart-sub">The 30 daily returns of each asset, grouped in 1-percentage-point bins</span></figcaption><div class="chart" id="chart-dist" role="img" aria-label="Histogram of BTC and ETH daily returns"></div></figure>
    <div>
      <details class="explain" open><summary>How drawdown is measured</summary><p>{_e(drawdown_note(m))}</p></details>
      <details class="explain"><summary>RSI conventions</summary><p>{_e(rsi_note(m))}</p></details>
      <details class="explain"><summary>Realised volatility and ATR</summary><p>Realised volatility is the sample standard deviation of the daily returns times √365 (14 and 30 returns). ATR (14) is the plain average of the last 14 true ranges; ATR / price scales it by the latest close. Both describe past variability only.</p></details>
    </div>
  </div>
  <div class="callout warn"><div class="eyebrow">Risk interpretation</div><p>{_e(_risk(m))}</p></div>
</section>"""

    def flag(v, level):
        return "hl" if v is not None and v >= level else ""
    part_rows = [
        ["Base-asset volume", f"{_number(m['BTC']['volume'], 2)} BTC", f"{_number(m['ETH']['volume'], 2)} ETH", "Units traded in the daily candle (Kraken reports volume in the base asset)"],
        ["Volume vs previous 7-session average", (ratio_x(m["BTC"]["volratio"]), flag(m["BTC"]["volratio"], HIGHLIGHT_RATIO)), (ratio_x(m["ETH"]["volratio"]), flag(m["ETH"]["volratio"], HIGHLIGHT_RATIO)), "Participation relative to the recent baseline"],
        ["Estimated USD turnover", usd(m["BTC"]["turnover"] / 1e6) + "M", usd(m["ETH"]["turnover"] / 1e6) + "M", "Base volume × VWAP (USD); an estimate, not an exchange figure"],
        ["Turnover vs previous 7-session average", (ratio_x(m["BTC"]["turnratio"]), flag(m["BTC"]["turnratio"], HIGHLIGHT_RATIO)), (ratio_x(m["ETH"]["turnratio"]), flag(m["ETH"]["turnratio"], HIGHLIGHT_RATIO)), "Value traded relative to the baseline"],
        ["Trade count", _number(m["BTC"]["trades"], 0), _number(m["ETH"]["trades"], 0), "Recorded trades in the daily candle"],
        ["Trade count vs previous 7-session average", ratio_x(m["BTC"]["traderatio"]), ratio_x(m["ETH"]["traderatio"]), "Transaction activity relative to the baseline"],
        ["Estimated average USD per trade", usd(m["BTC"]["notional"], 0), usd(m["ETH"]["notional"], 0), "Turnover ÷ trade count; not an order size"],
        ["Close versus session VWAP", (_chg(m["BTC"]["vwaprel"]), _tone(m["BTC"]["vwaprel"])), (_chg(m["ETH"]["vwaprel"]), _tone(m["ETH"]["vwaprel"])), "Close relative to the volume-weighted average price"],
        ["Range position of the close", position_text(m["BTC"]["loc"]), position_text(m["ETH"]["loc"]), "0% = the day's low, 100% = the day's high"],
        ["Daily high-low range / close", pct(m["BTC"]["range"]), pct(m["ETH"]["range"]), "Daily price excursion (OHLC range)"],
    ]
    sess_rows = [[s["asset"], _short_date_v6(s["date"]), ratio_x(s["ratio"]), f"${s['turnover']:,.1f}M", (_chg(s["ret"]), _tone(s["ret"]))] for s in sessions]
    sessions_html = (_tbl(["Asset", "Date", "Turnover vs prior 7 sessions", "Turnover", "Daily return"], sess_rows, "num", "Sessions with turnover at least 1.25 times the previous seven")
                     if sess_rows else f"<p class=\"note\">No session reached {_g(HIGHLIGHT_RATIO)} times its prior seven-session turnover.</p>")
    sec4 = f"""
<section class="section" id="s-activity" aria-labelledby="h-activity">
  <div class="kicker">04 · Trading Activity</div>
  <h2 id="h-activity">Trading Activity</h2>
  <p class="intro">Trading volume, number of trades and volume-weighted average price (VWAP): the latest session compared with the seven sessions before it. Following Kraken's OHLC layout (open, high, low, close, VWAP, volume, trade count), prices and VWAP are in US dollars and volume is in the base asset (BTC or ETH). On every candle VWAP lies within the day's low-high range (validation rule CND-VWAP).</p>
  {_tbl(["Latest-session measure", "BTC", "ETH", "What it shows"], part_rows, "num meaning4", "Latest-session trading activity")}
  <div class="callout"><div class="eyebrow">Participation</div><p>{_e(_participation(m))}</p></div>
  <figure class="chart-card wide">
    <figcaption><span class="chart-title">Daily Trading Activity</span><span class="chart-sub" id="act-sub">Estimated daily turnover in million USD (volume × VWAP)</span></figcaption>
    <div class="seg" role="group" aria-label="Activity measure"><button type="button" data-act="turnover_musd" aria-pressed="true">Turnover</button><button type="button" data-act="trades" aria-pressed="false">Trade count</button><button type="button" data-act="vwap_rel_pct" aria-pressed="false">Close vs VWAP</button></div>
    <div class="chart" id="chart-activity" role="img" aria-label="Daily trading activity for BTC and ETH"></div>
  </figure>
  <h3 class="sub">Unusual Trading Sessions</h3>
  {sessions_html}
  <p class="note">A session is flagged as unusual when its estimated turnover is at least {_g(HIGHLIGHT_RATIO)} times the plain average of the seven sessions before it (the project's existing highlight level). These measures describe trading conditions; they do not identify buyers, sellers, intent or manipulation.</p>
</section>"""

    sec5 = """
<section class="section" id="s-charts" aria-labelledby="h-charts">
  <div class="kicker">05 · Charts</div>
  <h2 id="h-charts">Interactive Charts</h2>
  <p class="intro">Compare Bitcoin and Ethereum over 7, 14 or 30 days. Choose the observation window and the assets, hover or tap a chart for exact values, and use the chart toolbar to zoom or save an image.</p>
  <div class="controls" role="group" aria-label="Chart controls">
    <div class="ctl"><span class="ctl-label" id="lbl-win">Window</span><div class="seg" role="group" aria-labelledby="lbl-win"><button type="button" data-win="7" aria-pressed="false">7 days</button><button type="button" data-win="14" aria-pressed="false">14 days</button><button type="button" data-win="30" aria-pressed="true">30 days</button></div></div>
    <div class="ctl"><span class="ctl-label" id="lbl-ast">Assets</span><div class="seg" role="group" aria-labelledby="lbl-ast"><button type="button" data-asset="BTC" aria-pressed="true"><span class="dot btc"></span>BTC</button><button type="button" data-asset="ETH" aria-pressed="true"><span class="dot eth"></span>ETH</button></div></div>
    <p class="period" id="period-text"></p>
  </div>
  <div class="chart-grid">
    <figure class="chart-card wide"><figcaption><span class="chart-title">Normalised Closing Price</span><span class="chart-sub">Index (first close of the window = 100); hover for the USD close</span></figcaption><div class="chart" id="chart-price" role="img" aria-label="Normalised closing prices"></div></figure>
    <figure class="chart-card wide"><figcaption><span class="chart-title">Daily Returns</span><span class="chart-sub">Percent change, close ÷ previous close − 1</span></figcaption><div class="chart" id="chart-returns" role="img" aria-label="Daily returns"></div></figure>
    <figure class="chart-card"><figcaption><span class="chart-title">Return Relationship</span><span class="chart-sub">One point per day: BTC return (x) against ETH return (y)</span></figcaption><div class="chart" id="chart-scatter" role="img" aria-label="Scatter of daily returns"></div></figure>
    <figure class="chart-card"><figcaption><span class="chart-title">Drawdown from Running Peak</span><span class="chart-sub">Percent below the highest close so far in the selected window</span></figcaption><div class="chart" id="chart-drawdown" role="img" aria-label="Drawdown"></div></figure>
  </div>
</section>"""

    rules_rows = [
        ["R1 co-movement", f"correlation size: below {analysis.CORRELATION_WEAK_BELOW:.2f}; {analysis.CORRELATION_WEAK_BELOW:.2f} to {analysis.CORRELATION_STRONG_FROM:.2f}; {analysis.CORRELATION_STRONG_FROM:.2f} or more", d30["correlation"], wm30["rules"]["R1"]],
        ["R2 relative volatility", f"ETH/BTC daily volatility: {analysis.VOLATILITY_RATIO_HIGH:.2f} or more; {analysis.VOLATILITY_RATIO_LOW:.2f} or less; otherwise", d30["ratio"], wm30["rules"]["R2"]],
        ["R3 down-day overlap", f"share: {analysis.DOWN_DAY_OVERLAP_HIGH:.2f} or more; {analysis.DOWN_DAY_OVERLAP_LOW:.2f} or less; otherwise", f"{d30['joint']} days, share {d30['overlap']}", wm30["rules"]["R3"]],
    ]
    findings = "".join(f"<li><strong>{_e(t)}.</strong> {_e(x)}</li>" for t, x in (_finding1(m), _finding2(m), _finding3(m), _finding4(m), _finding5(m)))
    statements = "".join(f"<li>{_e(x)}</li>" for x in dashboard.window_insights(dataset, 30))
    expl = ("".join(f"<li>{_e(x)}</li>" for x in exploratory) if exploratory else "<li>No observation reached the proposed levels in this period.</li>")
    sec6 = f"""
<section class="section" id="s-rules" aria-labelledby="h-rules">
  <div class="kicker">06 · Rule-Based Analysis</div>
  <h2 id="h-rules">Market Intelligence and Rule-Based Analysis</h2>
  <p class="intro">Documented rules applied to the calculated figures, with the evidence, uncertainty and monitoring points shown separately.</p>
  <div class="disclosure" role="note"><strong>Rule-based analysis.</strong> Every figure is calculated from the data, and every statement is produced by fixed, documented rules applied to those figures, so each one can be traced to its evidence. This report does not currently use an AI model. Calculations, rule outcomes and interpretations are shown separately; none is a prediction or investment advice.</div>
  <div class="tabs" role="tablist" aria-label="Rule-based analysis views">
    <button type="button" role="tab" id="t-metrics" aria-controls="p-metrics" aria-selected="true">Calculated metrics and rules</button>
    <button type="button" role="tab" id="t-interp" aria-controls="p-interp" aria-selected="false" tabindex="-1">Rule-based interpretation</button>
    <button type="button" role="tab" id="t-unc" aria-controls="p-unc" aria-selected="false" tabindex="-1">Uncertainty and limits</button>
    <button type="button" role="tab" id="t-mon" aria-controls="p-mon" aria-selected="false" tabindex="-1">Monitoring</button>
    <button type="button" role="tab" id="t-exp" aria-controls="p-exp" aria-selected="false" tabindex="-1">Exploratory (proposed)</button>
  </div>
  <div class="tabpanel" id="p-metrics" role="tabpanel" aria-labelledby="t-metrics">
    <h3 class="sub first">Observation Rules R1 to R3 (30 Returns)</h3>
    {_tbl(["Rule", "Thresholds", "Calculated value", "Rule outcome"], rules_rows, "", "Observation rules R1 to R3")}
    <p class="note">The rules were fixed before any result was seen. They are descriptive conventions for this 30-day sample, not market standards and not trading signals.</p>
  </div>
  <div class="tabpanel" id="p-interp" role="tabpanel" aria-labelledby="t-interp" hidden>
    <h3 class="sub first">Rule-Based Findings</h3><p class="note">Written from fixed patterns and the calculated figures.</p><ol class="plain-ol">{findings}</ol>
    <h3 class="sub">Rule Statements</h3><ul class="plain">{statements}</ul>
  </div>
  <div class="tabpanel" id="p-unc" role="tabpanel" aria-labelledby="t-unc" hidden>
    <ul class="plain">
      <li><strong>Sample size:</strong> 30 daily returns from one exchange. Every statistic describes this window only.</li>
      <li><strong>Correlation:</strong> {m['pair']['corr']:.2f}; an approximate 95% interval is {lo:.2f} to {hi:.2f} (Fisher transformation, n = 30, calculated for this report). A correlation does not establish causation.</li>
      <li><strong>Concentrated variability:</strong> a few sessions can carry a large share of the measured volatility and correlation; read them with the largest daily moves in section 03.</li>
      <li><strong>Drawdown:</strong> measured from the highest close inside the window; a higher earlier close is not visible.</li>
      <li><strong>RSI:</strong> the simple-average and Wilder conventions differ (BTC {m['BTC']['rsi']:.1f} / {m['BTC']['rsi_wilder']:.1f}, ETH {m['ETH']['rsi']:.1f} / {m['ETH']['rsi_wilder']:.1f}).</li>
      <li><strong>Not in the data:</strong> order books, spreads, trade direction, derivatives, funding, liquidations, ETF or on-chain flows, macro context and sentiment. No statement about who traded, why prices moved or where they will go can be made from this file.</li>
    </ul>
  </div>
  <div class="tabpanel" id="p-mon" role="tabpanel" aria-labelledby="t-mon" hidden>
    <ol class="plain-ol">
      <li><strong>Relative resilience:</strong> the BTC–ETH gap across 7, 15 and 30 returns; persistence matters more than one session.</li>
      <li><strong>Participation:</strong> price moves together with volume, turnover and trade-count ratios.</li>
      <li><strong>Risk normalisation:</strong> ATR as a share of price and realised volatility before comparing the two assets.</li>
      <li><strong>Session pressure:</strong> range position and close versus VWAP.</li>
      <li><strong>Co-movement:</strong> correlation across windows; a short sample does not establish future correlation.</li>
    </ol>
  </div>
  <div class="tabpanel" id="p-exp" role="tabpanel" aria-labelledby="t-exp" hidden>
    <div class="callout warn"><div class="eyebrow">Exploratory · proposed thresholds · not approved</div><p>The levels below ({_g(PROPOSED_DRAWDOWN * 100)}% current drawdown and {_g(PROPOSED_Z)} standard deviations for a daily move) are proposals under review. They are not project rules, they do not select the key findings, and they are kept apart from R1 to R3.</p></div>
    <ul class="plain">{expl}</ul>
  </div>
</section>"""

    rule_rows = [[r["rule_id"], str(r["expected_instances"]), str(r["evaluated"]), (str(r["pass"]), "pos" if r["pass"] else ""), (str(r["fail"]), "neg" if r["fail"] else ""), str(r["inconclusive"])] for r in ledger["rules"]]
    if provenance["kind"] == SOURCE_KRAKEN:
        origin = "Fetched from Kraken by the daily workflow."
        fetched_text = f"{fetched} UTC, as recorded by the pipeline's own report."
        utc_text = "All dates and day boundaries are UTC, and the latest day is a completed UTC day."
        source_text = "Kraken public OHLC endpoint: daily candles, one per UTC day."
    else:
        origin = "Supplied manually as a CSV file."
        fetched_text = "Not recorded for a manually supplied CSV; no fetch time is stated."
        utc_text = "The file records no fetch time; its dates are taken as UTC days."
        source_text = "A supplied CSV representing Kraken spot daily candles."
    rules_passed, rules_failed, rules_open = (sum(r[k] for r in ledger["rules"]) for k in ("pass", "fail", "inconclusive"))
    dq_clean, dq_class, dq_text, dq_summary = _dq_state(ledger)
    sec7 = f"""
<section class="section" id="s-quality" aria-labelledby="h-quality">
  <div class="kicker">07 · Data Quality</div>
  <h2 id="h-quality">Data Quality and Methodology</h2>
  <p class="intro">Where the data comes from, how it was validated, and how each figure is calculated.</p>
  <div class="dq-card"><div class="a-head"><div class="eyebrow">Validation summary</div><span class="pill {dq_class}">{_e(dq_text)}</span></div><p>{dq_summary} Period: {_e(period)} (UTC).</p></div>
  <div class="grid-2">
    <dl class="facts">
      <div><dt>Source</dt><dd>{_e(source_text)}</dd></div>
      <div><dt>Pairs</dt><dd>XBT/USD (Bitcoin) and ETH/USD (Ethereum)</dd></div>
      <div><dt>Data origin</dt><dd>{_e(origin)}</dd></div>
      <div><dt>Data fetched (UTC)</dt><dd>{_e(fetched_text)}</dd></div>
      <div><dt>UTC basis</dt><dd>{_e(utc_text)}</dd></div>
      <div><dt>Observation period</dt><dd>{_e(period)}: {len(dataset['rows'])} daily closes and {len(dataset['rows']) - 1} daily returns ({_e(_short_date_v6(dates[1]))} – {_e(_short_date_v6(dates[-1]))}).</dd></div>
      <div><dt>Report generated</dt><dd>No generation time is shown: the report is built deterministically, so identical data always produce an identical report.</dd></div>
      <div><dt>Input file</dt><dd>{_e(CSV_NAME_FORMAT.format(day=dates[-1]))} · 17 columns · {len(dataset['rows'])} rows</dd></div>
      <div><dt>Validation results</dt><dd>{ledger['evaluated']} of {ledger['expected_instances']} planned candle-rule checks were evaluated: {rules_passed} passed, {rules_failed} failed and {rules_open} inconclusive; missing {ledger['missing']}, duplicate {ledger['duplicate']}, unexpected {ledger['unexpected']}. Every stored return equals close ÷ previous close − 1 (tolerance 1e-12).</dd></div>
    </dl>
    <div>{_tbl(["Rule", "Planned", "Evaluated", "Pass", "Fail", "Inconclusive"], rule_rows, "num", "Candle validation ledger")}</div>
  </div>
  <details class="explain" open><summary>Calculation definitions</summary>
    <ul class="plain">
      <li><strong>Returns:</strong> close ÷ close n days earlier − 1 (n = 7, 15, 30); a daily return is close ÷ previous close − 1. The first close of the file has no return.</li>
      <li><strong>Cumulative return:</strong> the latest close ÷ the first close − 1.</li>
      <li><strong>Realised volatility:</strong> sample standard deviation of the daily returns × √365 (14 and 30 returns).</li>
      <li><strong>ATR (14):</strong> plain average of the last 14 true ranges; true range = max(high − low, |high − previous close|, |low − previous close|).</li>
      <li><strong>RSI (14):</strong> simple averages of gains and losses (primary); the Wilder-smoothed value is shown as a comparison.</li>
      <li><strong>Drawdown:</strong> close ÷ highest close so far in the window − 1; maximum = deepest value, current = latest value.</li>
      <li><strong>Turnover:</strong> base volume × VWAP (USD); ratios use the plain average of the seven previous sessions.</li>
      <li><strong>Range position:</strong> (close − low) ÷ (high − low); 0% = the day's low.</li>
      <li><strong>Correlation:</strong> Pearson correlation of the aligned daily returns.</li>
    </ul></details>
  <details class="explain"><summary>Wording rules and thresholds</summary><p>{_e(threshold_note())}</p></details>
  <p class="note"><strong>Limitations:</strong> this is exchange-specific spot data, not consolidated global market data; the sample is 30 daily returns from one exchange; and the analysis is descriptive only and is not investment advice. An independent educational project, not affiliated with Kraken.</p>
</section>"""

    sec8 = """
<section class="section" id="s-data" aria-labelledby="h-data">
  <div class="kicker">08 · Market Data</div>
  <h2 id="h-data">Daily Market Data</h2>
  <p class="intro">The underlying daily market observations behind every figure in this report: prices, returns, volume, VWAP and trade counts for Bitcoin and Ethereum, with all 17 columns of the source file. Filter, sort and export the table below.</p>
  <div class="controls" role="group" aria-label="Table controls">
    <div class="ctl"><span class="ctl-label" id="lbl-rows">Period</span><div class="seg" role="group" aria-labelledby="lbl-rows"><button type="button" data-twin="7" aria-pressed="false">7 days</button><button type="button" data-twin="14" aria-pressed="false">14 days</button><button type="button" data-twin="30" aria-pressed="true">30 days</button></div></div>
    <div class="ctl"><span class="ctl-label" id="lbl-cols">Columns</span><div class="seg" role="group" aria-labelledby="lbl-cols"><button type="button" data-cols="all" aria-pressed="true">All</button><button type="button" data-cols="BTC" aria-pressed="false">BTC</button><button type="button" data-cols="ETH" aria-pressed="false">ETH</button><button type="button" data-cols="prices" aria-pressed="false">Prices</button><button type="button" data-cols="activity" aria-pressed="false">Activity</button></div></div>
    <button type="button" class="btn-primary" id="export-csv">Export CSV (selected period)</button>
  </div>
  <p class="note" id="table-caption"></p>
  <div class="table-wrap" role="region" tabindex="0" aria-label="Daily data table, scrolls sideways on narrow screens"><table class="tbl data num" id="data-table"><caption class="sr">Daily data of the selected period; use the column buttons to sort</caption><thead></thead><tbody></tbody></table></div>
  <div class="pager"><button type="button" id="page-prev">Previous</button><span id="page-status" aria-live="polite"></span><button type="button" id="page-next">Next</button></div>
</section>"""
    return sec1 + sec2 + sec3 + sec4 + sec5 + sec6 + sec7 + sec8


def render_v6(dataset, provenance=None):
    """The final detailed report as UTF-8 bytes. Deterministic: no clock, no randomness, no network."""
    m = compute(dataset)
    provenance = normalise_provenance(provenance)
    template = TEMPLATE_PATHS[DESIGN_V6].read_text(encoding="utf-8")
    if any(template.count(p) != 1 for p in V6_PLACEHOLDERS):
        raise InteractiveReportError("the v6 template must contain each placeholder exactly once")
    dates = dataset["dates"]
    activity = _daily_activity(dataset)
    key = _key_findings(dataset, m)
    z_moves = _z_moves(dataset)
    windows = _dashboard_windows(dataset)
    sections = _sections_v6(dataset, m, provenance, activity, _daily_extremes(dataset), z_moves, _unusual_sessions(dataset, activity), key, _exploratory(dataset, m, z_moves), windows)
    day, month, year = _date_parts(dates[-1])
    footer =_footer_note(provenance, f"{day} {month} {year}")
    plotly_bytes = dashboard.PLOTLY_PATH.read_bytes()
    if hashlib.sha256(plotly_bytes).hexdigest() != dashboard.PLOTLY_SHA256:
        raise InteractiveReportError("the vendored Plotly.js file does not match the approved SHA-256 and will not be embedded")
    plotly = plotly_bytes.decode("utf-8")
    if "</script" in plotly.lower():
        raise InteractiveReportError("the vendored Plotly.js file contains a closing script tag and cannot be embedded")
    payload = {"meta": {"csv_file": CSV_NAME_FORMAT.format(day=dates[-1])}, "windows": windows, "columns": list(dashboard.EXPECTED_COLUMNS), "rows": dataset["rows"], "table": _column_meta(),
               "activity": {"dates": dates, **activity}, "by_window": {a: [m[a]["r7"] * 100, m[a]["r15"] * 100, m[a]["r30"] * 100] for a in ASSETS}}
    page = template
    for name, value in (("__ASOF__", _e(f"{day} {month} {year}")), ("__ISODATE__", _e(dates[-1])), ("__SOURCE__", _e(_intro_source_note(provenance))),
                        ("__SECTIONS__", sections), ("__FOOTER__", _e(footer))):
        page = page.replace(name, value)
    left = [p for p in re.findall(r"__[A-Z_]+__", page) if p in V6_PLACEHOLDERS and p not in ("__CSP__", "__LOGIC__", "__DATA_JSON__", "__PLOTLY_JS__")]
    if left:
        raise InteractiveReportError("unresolved placeholder in the v6 report: " + left[0])
    page = page.replace("__LOGIC__", _dashboard_logic())
    before, rest = page.split("__DATA_JSON__")
    middle, after = rest.split("__PLOTLY_JS__")
    page = before + dashboard.json_for_script(payload) + middle + plotly + after
    if page.count("__CSP__") != 1:
        raise InteractiveReportError("the v6 template must contain the CSP placeholder exactly once")
    policy = dashboard.content_security_policy(page)
    page = page.replace("__CSP__", policy, 1)
    if dashboard.content_security_policy(page) != policy:
        raise InteractiveReportError("the script text changed while the policy was inserted")
    return page.encode("utf-8")


def report_file_name(day):
    return REPORT_NAME.format(day=day)
