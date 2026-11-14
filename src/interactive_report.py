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
  close vs VWAP          close / VWAP - 1;  close location: (close - low) / (high - low) as a percentile of the day's range;  range: (high - low) / close
  maximum drawdown       lowest value of close / running closing-price peak - 1 over the 31 closes;  distance from high/low: latest close against the 31-close extreme
  correlation            Pearson correlation of the 30 daily returns (analysis.pearson_correlation)
Presentation rules that are not metrics (documented, deliberately plain):
  a ratio is highlighted when it is at least HIGHLIGHT_RATIO (the project's existing 'clearly higher' mark, +25 %); a performance is 'material' from MATERIAL_MOVE (5 %);
  the close is 'near' the daily low or high within the lowest or highest quarter of the range; 'approximately at VWAP' means within 0.05 %.
The data-dependent sentences are chosen from fixed neutral patterns by comparing the computed numbers; they state comparisons and never predict.
"""

import html
import json
import math
import re
import statistics
from pathlib import Path

import analysis
import dashboard

TEMPLATE_PATH = Path(__file__).resolve().parent / "interactive_report_template.html"
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
MINUS = "−"


class InteractiveReportError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- metrics
def _mean(values):
    return sum(values) / len(values)


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
    out["rsi"] = 50.0 if gain == 0 and loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
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


def _monitor(m):
    leader, _, _ = _leader(m, "r30")
    higher, _ = _higher(m, "vol30")
    first = f"Monitor whether {leader}'s relative outperformance persists" if leader else "Monitor whether the relative performance of BTC and ETH persists"
    second = f"whether {higher}'s higher volatility moderates" if higher else "whether volatility moderates"
    return (f"{first}, {second}, and whether trading participation confirms or contradicts subsequent price movements. Look for agreement across multiple indicators "
            "rather than treating an isolated technical reading as a directional signal.")


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
def slot_values(dataset, m=None):
    m = m or compute(dataset)
    day, month, year = _date_parts(m["pair"]["date"])
    values = {
        "date": m["pair"]["date"], "date_short": f"{day} {month[:3]} {year}", "as_of_long": f"{day} {month} {year}", "as_of_upper": f"{day} {month} {year}".upper(),
        "corr": f"{abs(m['pair']['corr']):.2f}" if m["pair"]["corr"] >= 0 or round(m["pair"]["corr"], 2) == 0 else MINUS + f"{abs(m['pair']['corr']):.2f}",
        "vol_ratio_x": ratio_x(m["pair"]["vol_ratio"]), "callout_exec": _callout(m), "risk_text": _risk(m), "participation_text": _participation(m),
        "spread_text": _spread(m), "monitor_text": _monitor(m), "report_json": report_json(dataset),
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
        return html.escape(value, quote=False)

    text = SLOT.sub(fill, template)
    leftover = re.findall(r"\{\{[^}]*\}\}", text.replace(values["report_json"], ""))
    if leftover:
        raise InteractiveReportError("unresolved placeholder in the report: " + leftover[0])
    return text


def render(dataset):
    """The report for one validated dataset (dashboard.parse_dataset) as UTF-8 bytes."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return render_template(template, slot_values(dataset)).encode("utf-8")


def report_file_name(day):
    return REPORT_NAME.format(day=day)
