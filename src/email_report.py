"""The data and the three small PNG charts of the HTML success e-mail (action send_success_report).

Everything here is calculated from the validated dataset with the project's own functions (dashboard.window_metrics, window_slice, drawdown_series, indexed_series); no financial method is repeated.
build_report() returns a plain dict (strict JSON) that daily_automation.py puts into the request body. The web app builds the HTML itself from these validated fields and never accepts HTML from here.

The charts are drawn with the standard library only: a 600 x 240 palette image written as a PNG (zlib + struct), with anti-aliased lines and bars. They carry no text; titles, legends and the
scale are written by the web app as HTML text next to the picture. The result is deterministic: the same data gives the same bytes on the same zlib. The module reads nothing and writes nothing.
"""

import base64
import binascii
import json
import math
import re
import statistics
import struct
import zlib
from datetime import datetime, timezone

import analysis
import candle_rules
import dashboard
import run_evidence

WINDOW_DAYS = 30
CHART_IDS = ("indexed", "returns", "drawdown")                  # price indexed to 100, daily returns, drawdown from the highest close
CHART_WIDTH, CHART_HEIGHT = 600, 240
MAX_CHART_BYTES = 6000                                          # one PNG (about 2.5 KB in practice); the whole request has to fit the web app's 20,000-character limit
MAX_REPORT_CHARS = 15000                                        # the report alone, as JSON
MAX_REQUEST_CHARS = 19000                                       # the whole request body, with the token and the artifact link: below the web app's limit of 20,000
REQUEST_EXTRAS_CHARS = 400                                      # what is added when the request is posted: token, artifact link, sheet_update
MAX_LATEST_CHARS = 40
TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"

BTC_COLOUR = (247, 147, 26)                                     # the Bitcoin orange
ETH_COLOUR = (98, 126, 234)                                     # the Ethereum blue
WHITE, GRID, AXIS = (255, 255, 255), (226, 229, 234), (150, 156, 168)
MARGIN = 8                                                      # pixels between the plot and the edge of the picture
LINE_HALF_WIDTH = 1.2
LEVEL_FLOORS = (0.12, 0.45, 0.80)                               # anti-aliasing: coverage below the first floor is background, then a light, a medium and the full colour
LEVEL_STRENGTH = (0.30, 0.62, 1.0)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHART_KEYS = {"id", "png_base64", "width", "height", "low", "high", "btc_last", "eth_last"}
ASSET_KEYS = {"return_7d", "return_14d", "return_30d", "volatility_annualised", "worst_day", "negative_days", "drawdown_peak", "drawdown_trough"}
PAIR_KEYS = {"volatility_ratio", "joint_negative_days", "overlap_share"}
META_KEYS = {"rows", "first_date", "data_fetched", "generated_at"}
REPORT_KEYS = {"meta", "assets", "pair", "coverage", "charts"}
OPTIONAL_KEYS = {"comparison", "validation", "ohlc", "intelligence"}
COMPARISON_KEYS = {"stronger", "spread_pp", "correlation_7d", "correlation_14d"}
VALIDATION_KEYS = {"run_id", "overall", "fields_total", "fields_validated", "instances_expected", "instances_evaluated", "instances_passed", "checks_total", "checks_passed", "assumptions", "exceptions"}
EXCEPTION_KEYS = {"rule_id", "severity", "outcome", "field", "explanation"}
KNOWN_ASSUMPTIONS = ("A1",)
MAX_EXCEPTIONS = 5
MAX_EXCEPTION_TEXT = 200
EQUAL_BELOW_PP = 0.005                                          # two 30-day returns that differ by less than half a hundredth of a percentage point are reported as equal (they print identically at two decimals)
RULE_ID_TEXT = re.compile(r"[A-Z]{2,4}(-[A-Z0-9]{1,8}){1,3}")
DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


class EmailReportError(Exception):
    """The report could not be built or is not valid. The message is one line and safe to print."""


# ---------------------------------------------------------------- the PNG writer and the drawing
def _check_series(btc, eth):
    for values in (btc, eth):
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            raise EmailReportError("a chart needs two series of at least two numbers")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise EmailReportError("a chart series holds a value that is not a finite number")
    if len(btc) != len(eth):
        raise EmailReportError("the two chart series must have the same length")


def _scale(values, include_zero):
    """A scale with round gridline steps that covers every value with some room: returns (low, high, step)."""
    low, high = min(values), max(values)
    if include_zero:
        low, high = min(low, 0.0), max(high, 0.0)
    span = high - low
    pad = span * 0.08 if span > 0 else max(abs(high) * 0.05, 1.0)
    low, high = low - pad, high + pad
    raw = (high - low) / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(factor * magnitude for factor in (1, 2, 5, 10) if raw <= factor * magnitude * (1 + 1e-12))
    return math.floor(low / step + 1e-9) * step, math.ceil(high / step - 1e-9) * step, step


def _stroke(cover, width, height, points):
    """Anti-aliased polyline: each pixel gets the coverage of its centre's distance to the nearest segment."""
    half = LINE_HALF_WIDTH
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        length_squared = dx * dx + dy * dy
        left, right = max(0, math.floor(min(x0, x1) - half - 1)), min(width - 1, math.ceil(max(x0, x1) + half + 1))
        top, bottom = max(0, math.floor(min(y0, y1) - half - 1)), min(height - 1, math.ceil(max(y0, y1) + half + 1))
        for py in range(top, bottom + 1):
            cy = py + 0.5
            for px in range(left, right + 1):
                cx = px + 0.5
                t = ((cx - x0) * dx + (cy - y0) * dy) / length_squared if length_squared else 0.0
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                coverage = half + 0.5 - math.hypot(cx - (x0 + t * dx), cy - (y0 + t * dy))
                if coverage > 0.0:
                    index = py * width + px
                    cover[index] = max(cover[index], min(coverage, 1.0))


def _bar(cover, width, height, x0, x1, y0, y1):
    """Anti-aliased rectangle: each pixel gets the share of its area that lies inside."""
    for py in range(max(0, math.floor(y0)), min(height, math.ceil(y1))):
        vertical = min(py + 1, y1) - max(py, y0)
        for px in range(max(0, math.floor(x0)), min(width, math.ceil(x1))):
            coverage = (min(px + 1, x1) - max(px, x0)) * vertical
            if coverage > 0.0:
                index = py * width + px
                cover[index] = max(cover[index], min(coverage, 1.0))


def _level(coverage):
    return sum(1 for floor in LEVEL_FLOORS if coverage >= floor)


def _blend(colour, strength):
    return tuple(round(255 + (channel - 255) * strength) for channel in colour)


def _png(width, height, palette, pixels):
    """A palette PNG with 4 bits per pixel (at most 16 colours), filter type 0, one IDAT chunk."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        line = pixels[y * width:(y + 1) * width]
        rows.extend((line[x] << 4) | (line[x + 1] if x + 1 < width else 0) for x in range(0, width, 2))
    palette_bytes = b"".join(bytes(colour) for colour in palette)
    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 4, 3, 0, 0, 0)) + chunk(b"PLTE", palette_bytes)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def render_chart(kind, btc, eth):
    """One chart as (PNG bytes, low, high); low and high are the ends of the vertical scale. kind: indexed (lines), drawdown (lines) or returns (bars from the zero line)."""
    if kind not in CHART_IDS:
        raise EmailReportError("unknown chart kind")
    _check_series(btc, eth)
    width, height, count = CHART_WIDTH, CHART_HEIGHT, len(btc)
    low, high, step = _scale(list(btc) + list(eth), include_zero=kind == "returns")
    plot_left, plot_right, plot_top, plot_bottom = MARGIN, width - MARGIN, MARGIN, height - MARGIN

    def y_of(value):
        return plot_bottom - (value - low) / (high - low) * (plot_bottom - plot_top)

    covers = {"btc": [0.0] * (width * height), "eth": [0.0] * (width * height)}
    if kind == "returns":
        slot = (plot_right - plot_left) / count
        gap = slot * 0.12
        zero = y_of(0.0)
        for name, values, offset in (("btc", btc, 0), ("eth", eth, 1)):
            for i, value in enumerate(values):
                x0 = plot_left + i * slot + (gap if offset == 0 else slot / 2)
                x1 = plot_left + i * slot + (slot / 2 if offset == 0 else slot - gap)
                top, bottom = (y_of(value), zero) if value >= 0 else (zero, y_of(value))
                _bar(covers[name], width, height, x0, x1, top, bottom)
    else:
        for name, values in (("btc", btc), ("eth", eth)):
            _stroke(covers[name], width, height, [(plot_left + i * (plot_right - plot_left) / (count - 1), y_of(value)) for i, value in enumerate(values)])

    palette = [WHITE, GRID, AXIS] + [_blend(BTC_COLOUR, s) for s in LEVEL_STRENGTH] + [_blend(ETH_COLOUR, s) for s in LEVEL_STRENGTH]
    pixels = bytearray(width * height)
    tick = math.ceil(low / step - 1e-9)
    while tick * step <= high + step * 1e-9:                                           # horizontal gridlines at round values; the zero line is darker
        y = min(height - 1, max(0, round(y_of(tick * step) - 0.5)))
        colour_index = 2 if abs(tick * step) < step * 1e-9 else 1
        pixels[y * width:(y + 1) * width] = bytes([colour_index]) * width
        tick += 1
    for index in range(width * height):
        btc_level, eth_level = _level(covers["btc"][index]), _level(covers["eth"][index])
        if eth_level and eth_level >= btc_level:
            pixels[index] = 5 + eth_level
        elif btc_level:
            pixels[index] = 2 + btc_level
    return _png(width, height, palette, pixels), low, high


def _chart_entry(kind, btc, eth):
    data, low, high = render_chart(kind, btc, eth)
    if len(data) > MAX_CHART_BYTES:
        raise EmailReportError(f"the {kind} chart is {len(data)} bytes, more than the limit of {MAX_CHART_BYTES}")
    return {"id": kind, "png_base64": base64.b64encode(data).decode("ascii"), "width": CHART_WIDTH, "height": CHART_HEIGHT,
            "low": round(low, 4), "high": round(high, 4), "btc_last": round(btc[-1], 2), "eth_last": round(eth[-1], 2)}


# ---------------------------------------------------------------- the figures
def _is_number_text(text):
    return bool(dashboard.NUMBER_TEXT.match(text)) and math.isfinite(float(text))


def _coverage(dataset):
    rows, latest, entries = dataset["rows"], dataset["rows"][-1], []
    for index, name in enumerate(dashboard.EXPECTED_COLUMNS):
        valid = sum(1 for row in rows if (DATE_TEXT.fullmatch(row[index]) if index == 0 else _is_number_text(row[index])))
        entries.append({"name": name, "valid_rows": valid, "latest": latest[index]})
    return entries


def _asset_figures(asset, metrics, shorter_windows):
    drawdown, peak, trough = metrics["max_drawdown"][asset]
    worst_value, worst_date = metrics["worst_day"][asset]
    return {
        "return_7d": shorter_windows[7]["cumulative_return"][asset],
        "return_14d": shorter_windows[14]["cumulative_return"][asset],
        "return_30d": metrics["cumulative_return"][asset],
        "volatility_annualised": metrics["volatility"][asset]["annualised"],
        "worst_day": {"value": worst_value, "date": worst_date},
        "negative_days": metrics["negative_days"][asset],
        "drawdown_peak": peak,
        "drawdown_trough": trough,
    }


def comparison_of(btc_30d, eth_30d, correlation_7d, correlation_14d):
    """The relative performance of the two assets over the 30-day window, and the optional short-window correlations. All calculated here; the web app only shows them.

    spread_pp = (ETH 30-day return - BTC 30-day return) x 100: percentage points, signed (positive means ETH was ahead), rounded to 6 decimals. The inputs are the two published 30-day returns
    (the return_30d figures). stronger is the asset with the larger 30-day return, or 'equal' when the spread is below 0.005 points. The 7- and 14-day correlations are the existing Pearson
    figures of those windows (None when a series is constant); they rest on 7 and 14 observations and are context for analysts, never a headline.
    """
    spread_pp = round((eth_30d - btc_30d) * 100, 6)
    stronger = "equal" if abs(spread_pp) < EQUAL_BELOW_PP else "ETH" if spread_pp > 0 else "BTC"
    short = lambda value: None if value is None else round(value, 6)
    return {"stronger": stronger, "spread_pp": spread_pp, "correlation_7d": short(correlation_7d), "correlation_14d": short(correlation_14d)}


MATERIAL_DRAWDOWN_PP = 2.0                                      # two maximum drawdowns that differ by this many percentage points or more are called materially different
INTEL_MAX_TEXT, INTEL_MAX_ROWS, INTEL_MAX_CELL = 420, 5, 70
SCENARIO_NAMES = ("Bullish", "Neutral", "Bearish")
INTEL_SECTIONS = ("correlation", "relative_strength", "risk")
INTEL_DISCLAIMER = ("Historical observations are measured from the validated Kraken dataset. The scenarios are illustrative, conditional descriptions built from those measurements; "
                    "they are not price predictions, targets or investment recommendations.")
UNAVAILABLE_NOTE = "Unavailable: the validated dataset does not hold enough complete, aligned daily observations for this calculation."


def _pc(value, signed=True):
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def intelligence_figures(dataset):
    """The numbers behind the market intelligence section, or None when the dataset cannot support them (fewer than 31 daily closes, or a missing or non-finite value in the window).

    All of it is deterministic Python on the validated dataset: Pearson correlation of the aligned daily percentage returns (never of price levels), the 7-, 14- and 30-return performance,
    the sample standard deviation of the daily returns (annualised as in the rest of the report), the largest fall from a previous high within the window and the current distance below
    the window high. Nothing is estimated or filled in: a missing observation makes the whole calculation unavailable.
    """
    if len(dataset["rows"]) < WINDOW_DAYS + 1:
        return None
    window = dashboard.window_slice(dataset, WINDOW_DAYS)
    returns, closes = window["returns"], window["closes"]
    series = [*returns["BTC"], *returns["ETH"], *closes["BTC"], *closes["ETH"]]
    if len(returns["BTC"]) != len(returns["ETH"]) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in series):
        return None
    figures = {"observations": len(returns["BTC"]), "start": window["return_dates"][0], "end": window["return_dates"][-1], "close_start": window["dates"][0],
               "correlation": analysis.pearson_correlation(returns["BTC"], returns["ETH"]), "assets": {}}
    for asset in dashboard.ASSETS:
        values, closing = returns[asset], closes[asset]
        daily = analysis.daily_volatility(values)
        figures["assets"][asset] = {
            "r7": analysis.cumulative_return(closing[-8:]), "r14": analysis.cumulative_return(closing[-15:]), "r30": analysis.cumulative_return(closing),
            "daily_vol": daily, "annual_vol": analysis.annualised_volatility(daily), "max_dd": analysis.max_drawdown(window["dates"], closing),
            "current_dd": closing[-1] / max(closing) - 1, "positive_days": sum(1 for v in values if v > 0), "mean": statistics.fmean(values), "low_close": min(closing), "high_close": max(closing)}
    figures["vol_ratio"] = analysis.volatility_ratio(figures["assets"]["ETH"]["daily_vol"], figures["assets"]["BTC"]["daily_vol"])
    negative = {a: sum(1 for v in returns[a] if v < 0) for a in dashboard.ASSETS}
    figures["joint_negative"] = analysis.count_joint_negative_days(returns["BTC"], returns["ETH"])
    figures["overlap_share"] = analysis.classify_down_day_overlap(negative["BTC"], negative["ETH"], figures["joint_negative"])[1]
    spread = round((figures["assets"]["ETH"]["r30"] - figures["assets"]["BTC"]["r30"]) * 100, 6)
    figures["spread_pp"] = spread
    figures["stronger"] = "equal" if abs(spread) < EQUAL_BELOW_PP else "ETH" if spread > 0 else "BTC"
    return figures


def _usd(value):
    return f"${value:,.2f}"


def intelligence_of(dataset):
    """The 'intelligence' part of the e-mail report: ready-to-show rows and sentences for the five subsections, built only from intelligence_figures(). Historical observations and the forward-looking
    scenarios are kept apart, correlation is never called causation, and a calculation that the data cannot support says 'Unavailable' instead of a number."""
    f = intelligence_figures(dataset)
    if f is None:
        none = {"rows": [], "text": UNAVAILABLE_NOTE}
        return {"correlation": none, "relative_strength": none, "risk": none,
                "scenarios": [{"name": n, "evidence": UNAVAILABLE_NOTE, "supports": "Unavailable.", "weakens": "Unavailable."} for n in SCENARIO_NAMES],
                "interpretation": {"divergence": UNAVAILABLE_NOTE, "risk_signal": UNAVAILABLE_NOTE, "monitor": UNAVAILABLE_NOTE}, "disclaimer": INTEL_DISCLAIMER}
    b, e, n = f["assets"]["BTC"], f["assets"]["ETH"], f["observations"]
    rho = f["correlation"]
    label = analysis.classify_correlation(rho)
    period = f"{f['start']} to {f['end']}"
    if rho is None:
        corr_text = f"Unavailable: a return series is constant over the {n} aligned daily returns, so the correlation is not defined. Correlation describes co-movement only and is not causation."
        corr_value = "Unavailable"
    else:
        shape = "weak linear co-movement" if abs(rho) < analysis.CORRELATION_WEAK_BELOW else f"{label} co-movement"
        corr_text = (f"Over {n} aligned daily percentage returns ({period}) the assets show {shape} (Pearson {rho:.2f}). It is calculated on returns, not price levels; "
                     "it describes this sample only and is not causation.")
        corr_value = f"{rho:.2f}"
    correlation = {"rows": [["Window", f"{n} daily returns, {period}"], ["Valid observations", f"{n} of {n}"], ["Pearson correlation of returns", corr_value], ["Classification (rule R1)", label]], "text": corr_text}
    if f["stronger"] == "equal":
        lead = "The two 30-return performances are equal to two decimals, so neither asset was relatively stronger."
    else:
        lead = f"{f['stronger']} was the relatively stronger asset over the window ({_pc(f['assets'][f['stronger']]['r30'])} against {_pc(f['assets']['ETH' if f['stronger'] == 'BTC' else 'BTC']['r30'])}, a gap of {abs(f['spread_pp']):.2f} percentage points)."
    mom = lambda a: "positive" if a["r7"] > 0 else "negative" if a["r7"] < 0 else "flat"
    strength = {"rows": [["Performance, 30 returns", f"BTC {_pc(b['r30'])} / ETH {_pc(e['r30'])}"], ["Performance, 14 returns", f"BTC {_pc(b['r14'])} / ETH {_pc(e['r14'])}"],
                         ["Performance, 7 returns", f"BTC {_pc(b['r7'])} / ETH {_pc(e['r7'])}"], ["Relatively stronger", f["stronger"] if f["stronger"] != "equal" else "Neither (equal)"]],
                "text": f"{lead} Recent 7-return momentum is {mom(b)} for BTC and {mom(e)} for ETH. This describes what has happened; it does not imply that it will continue."}
    ratio = f["vol_ratio"]
    if ratio is None:
        vol_line = "Unavailable: the BTC volatility is zero, so the ratio is not defined."
    elif ratio >= analysis.VOLATILITY_RATIO_HIGH:
        vol_line = f"ETH's realised volatility was materially higher ({ratio:.2f} times BTC's)."
    elif ratio <= analysis.VOLATILITY_RATIO_LOW:
        vol_line = f"BTC's realised volatility was materially higher (ETH was {ratio:.2f} times BTC's)."
    else:
        vol_line = f"Realised volatility was similar (ETH {ratio:.2f} times BTC)."
    dd = lambda a: a["max_dd"][0]
    gap = abs(dd(b) - dd(e)) * 100
    if dd(b) == 0 and dd(e) == 0:
        dd_line = "Neither asset fell from a previous high within the window."
    elif gap >= MATERIAL_DRAWDOWN_PP:
        dd_line = f"{'ETH' if dd(e) < dd(b) else 'BTC'} had the materially deeper maximum drawdown ({gap:.2f} percentage points deeper)."
    else:
        dd_line = f"The maximum drawdowns were similar (within {MATERIAL_DRAWDOWN_PP:.0f} percentage points)."
    risk = {"rows": [["Realised volatility, annualised", f"BTC {_pc(b['annual_vol'], False)} / ETH {_pc(e['annual_vol'], False)}"], ["ETH / BTC volatility ratio", "Unavailable" if ratio is None else f"{ratio:.2f}"],
                     ["Maximum drawdown", f"BTC {_pc(dd(b), False)} / ETH {_pc(dd(e), False)}"], ["Distance below window high", f"BTC {_pc(-b['current_dd'], False)} / ETH {_pc(-e['current_dd'], False)}"]],
            "text": (f"{vol_line} {dd_line} Method: sample standard deviation of {n} daily returns ({period}) annualised over 365 days; maximum drawdown is the largest fall of the daily close from a "
                     f"previous high among the {n + 1} closes from {f['close_start']}; a gap of {MATERIAL_DRAWDOWN_PP:.0f} points or more is called material.")}
    drift = lambda a: "not defined (zero volatility)" if a["daily_vol"] == 0 else f"{abs(a['mean']) / a['daily_vol']:.2f}"
    scenarios = [
        {"name": "Bullish",
         "evidence": f"7-return performance: BTC {_pc(b['r7'])}, ETH {_pc(e['r7'])}. Positive days of {n}: BTC {b['positive_days']}, ETH {e['positive_days']}. Mean daily return: BTC {_pc(b['mean'])}, ETH {_pc(e['mean'])}.",
         "supports": f"Both 7-return performances stay positive and daily volatility stays at or below its window level (BTC {_pc(b['daily_vol'], False)}, ETH {_pc(e['daily_vol'], False)}).",
         "weakens": f"Either 7-return performance turns negative, or a close falls below the window low (BTC {_usd(b['low_close'])}, ETH {_usd(e['low_close'])})."},
        {"name": "Neutral",
         "evidence": f"30-return performance: BTC {_pc(b['r30'])}, ETH {_pc(e['r30'])}. The mean daily return is {drift(b)} (BTC) and {drift(e)} (ETH) daily standard deviations in size.",
         "supports": f"Daily returns stay within one daily standard deviation of zero (BTC {_pc(b['daily_vol'], False)}, ETH {_pc(e['daily_vol'], False)}), so no direction builds.",
         "weakens": "Consecutive daily moves beyond those bands in the same direction, or a 7-return performance larger in size than the 30-return performance."},
        {"name": "Bearish",
         "evidence": f"Distance below window high: BTC {_pc(-b['current_dd'], False)}, ETH {_pc(-e['current_dd'], False)}. Both fell on {f['joint_negative']} of {n} days"
                     + ("" if f["overlap_share"] is None else f" (down-day overlap {f['overlap_share']:.2f})") + ".",
         "supports": "Both 7-return performances stay negative and down days keep coinciding.",
         "weakens": f"Either 7-return performance turns positive, or closes recover above the window high (BTC {_usd(b['high_close'])}, ETH {_usd(e['high_close'])})."}]
    if rho is None:
        diverge = "No similarity or divergence can be stated because the correlation is not defined for this window."
    elif abs(rho) >= analysis.CORRELATION_STRONG_FROM:
        diverge = f"The most significant feature is similarity: the daily returns moved together ({label}, {rho:.2f})" + (f", while {f['stronger']} led by {abs(f['spread_pp']):.2f} percentage points over {n} returns." if f["stronger"] != "equal" else ".")
    else:
        diverge = f"The most significant feature is divergence: the co-movement was only {label} ({rho:.2f})" + (f" and {f['stronger']} led by {abs(f['spread_pp']):.2f} percentage points." if f["stronger"] != "equal" else ".")
    deeper = "ETH" if dd(e) < dd(b) else "BTC"
    worst = f["assets"][deeper]
    risk_signal = ("No asset fell from a previous high within the window." if dd(b) == 0 and dd(e) == 0 else
                   f"The principal risk signal is {deeper}'s maximum drawdown of {_pc(dd(worst), False)} (currently {_pc(-worst['current_dd'], False)} below its window high; annualised volatility {_pc(worst['annual_vol'], False)}).")
    if rho is None or abs(rho) < analysis.CORRELATION_STRONG_FROM:
        monitor = f"Monitor the BTC-ETH correlation of returns (now {'unavailable' if rho is None else format(rho, '.2f')}) for a move back toward or away from strong co-movement."
    elif ratio is not None and (ratio >= analysis.VOLATILITY_RATIO_HIGH or ratio <= analysis.VOLATILITY_RATIO_LOW):
        monitor = f"Monitor the ETH / BTC volatility ratio (now {ratio:.2f}) for a return to the {analysis.VOLATILITY_RATIO_LOW:.2f} to {analysis.VOLATILITY_RATIO_HIGH:.2f} band."
    else:
        leader = "BTC" if f["stronger"] in ("BTC", "equal") else "ETH"
        monitor = f"Monitor the 7-return performance of {leader} against its 30-return performance ({_pc(f['assets'][leader]['r7'])} against {_pc(f['assets'][leader]['r30'])})."
    return {"correlation": correlation, "relative_strength": strength, "risk": risk, "scenarios": scenarios,
            "interpretation": {"divergence": diverge, "risk_signal": risk_signal, "monitor": monitor}, "disclaimer": INTEL_DISCLAIMER}


def _check_intelligence(part):
    _keys(part, {"correlation", "relative_strength", "risk", "scenarios", "interpretation", "disclaimer"}, "intelligence")
    safe = lambda value, limit: isinstance(value, str) and 1 <= len(value) <= limit and value.isprintable() and not re.search(r"[<>]", value)
    for name in INTEL_SECTIONS:
        _keys(part[name], {"rows", "text"}, f"intelligence.{name}")
        rows = part[name]["rows"]
        if not isinstance(rows, list) or len(rows) > INTEL_MAX_ROWS or not safe(part[name]["text"], INTEL_MAX_TEXT + 160):
            raise EmailReportError(f"intelligence.{name} is not valid")
        for row in rows:
            if not isinstance(row, list) or len(row) != 2 or not all(safe(cell, INTEL_MAX_CELL) for cell in row):
                raise EmailReportError(f"intelligence.{name} has an invalid row")
    scenarios = part["scenarios"]
    if not isinstance(scenarios, list) or [s.get("name") if isinstance(s, dict) else None for s in scenarios] != list(SCENARIO_NAMES):
        raise EmailReportError("intelligence.scenarios must be Bullish, Neutral and Bearish in that order")
    for scenario in scenarios:
        _keys(scenario, {"name", "evidence", "supports", "weakens"}, "an intelligence scenario")
        if not all(safe(scenario[key], INTEL_MAX_TEXT) for key in ("evidence", "supports", "weakens")):
            raise EmailReportError("an intelligence scenario is not valid")
    _keys(part["interpretation"], {"divergence", "risk_signal", "monitor"}, "intelligence.interpretation")
    if not all(safe(part["interpretation"][key], INTEL_MAX_TEXT) for key in part["interpretation"]) or not safe(part["disclaimer"], INTEL_MAX_TEXT):
        raise EmailReportError("intelligence.interpretation is not valid")


def _plain(text, limit):
    """Text that is safe to show: no control characters and no angle brackets, shortened, never empty."""
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(text)).strip()
    return (cleaned[:limit].strip() or "no detail")


def _exception_entry(rule_id, severity, outcome, field, explanation):
    return {"rule_id": rule_id, "severity": severity, "outcome": outcome, "field": _plain(field or "window", 40), "explanation": _plain(explanation, MAX_EXCEPTION_TEXT)}


def build_validation(outputs):
    """The evidence-backed validation result for the e-mail, or None when there is no evidence to base one on (then the e-mail says only what completeness supports).

    It combines the producer's envelope from the manifest, the consumer's own recomputation of the candle ledger from the CSV, and the consumer's verification checks. The overall is PASS only
    when every one of them is a complete PASS, all 17 fields are validated, every planned instance was evaluated exactly once and passed, and no exception exists; anything else is the worst
    state found (never PASS), and missing or incomplete evidence is INCONCLUSIVE at best.
    """
    manifest, verification = outputs.get("manifest"), outputs.get("verification")
    if not isinstance(manifest, dict) or not isinstance(verification, dict):
        return None
    dataset, envelope = outputs["dataset"], manifest["envelope"]
    consumer = dataset["candle_ledger"]
    csv_ok = any(check["id"] == "CON-CSV" and check["outcome"] == "PASS" for check in verification["checks"])
    validated, total = candle_rules.fields_validated(consumer, csv_ok)
    passed = sum(row["pass"] for row in consumer["rules"])
    checks_total, checks_passed = len(verification["checks"]), sum(1 for check in verification["checks"] if check["outcome"] == "PASS")
    assumptions = sorted(set(consumer["assumptions_applied"]) | set(envelope.get("assumptions_applied", [])))
    unknown = [a for a in assumptions if a not in KNOWN_ASSUMPTIONS]
    overall = run_evidence.combine([verification["overall"], run_evidence.envelope_overall(envelope), candle_rules.ledger_overall(consumer)])
    exceptions = []
    for family in envelope["families"].values():
        exceptions += [_exception_entry(e["rule_id"], e["severity"], e["outcome"], e.get("field") or e.get("scope"), e.get("explanation") or e.get("actual")) for e in family["non_pass"]]
    exceptions += [_exception_entry(e["rule_id"], e["severity"], e["outcome"], e.get("field") or e.get("scope"), e.get("explanation") or e.get("actual")) for e in consumer["non_pass"]]
    exceptions += [_exception_entry(check["id"], "ERROR", check["outcome"], "run", check["explanation"]) for check in verification["checks"] if check["outcome"] != "PASS"]
    complete = (validated == total and consumer["expected_instances"] > 0 and consumer["evaluated"] == consumer["expected_instances"] and passed == consumer["expected_instances"]
                and checks_total > 0 and checks_passed == checks_total and not unknown and not exceptions)
    if overall == candle_rules.PASS and not complete:
        overall = candle_rules.INCONCLUSIVE
    if overall != candle_rules.PASS and not exceptions:
        exceptions.append(_exception_entry("MAN-EVIDENCE", "ERROR", overall, "run", "the validation evidence is not a complete PASS"))
    return {"run_id": manifest["run_id"], "overall": overall, "fields_total": total, "fields_validated": validated, "instances_expected": consumer["expected_instances"],
            "instances_evaluated": consumer["evaluated"], "instances_passed": passed, "checks_total": checks_total, "checks_passed": checks_passed,
            "assumptions": [a for a in assumptions if a in KNOWN_ASSUMPTIONS], "exceptions": exceptions[:MAX_EXCEPTIONS]}


def _fetched_time(report_text):
    found = dashboard.FETCHED_LINE.search(report_text)
    if found is None:
        raise EmailReportError("the report has no 'Data fetched' line")
    return found.group(1) + " UTC"


def _ohlc(dataset):
    """Open, high, low and close of the latest day of the validated CSV, for both assets, exactly as the CSV holds them (no calculation, no rounding)."""
    last = dataset["rows"][-1]
    part = {"date": dataset["dates"][-1]}
    for asset in dashboard.ASSETS:
        columns = dashboard.CANDLE_COLUMNS[asset]
        part[asset.lower()] = {name: float(last[columns[name]]) for name in ("open", "high", "low", "close")}
    return part


def build_report(outputs, generated_at):
    """The report for the e-mail, from the validated outputs of daily_automation.validate_outputs. generated_at is a timezone-aware datetime."""
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise EmailReportError("the generation time must be a timezone-aware datetime")
    dataset, metrics = outputs["dataset"], outputs["metrics"]
    shorter = {k: dashboard.window_metrics(dataset, k) for k in (7, 14)}
    window = dashboard.window_slice(dataset, WINDOW_DAYS)
    series = {                                                                      # (BTC values, ETH values) for each chart
        "indexed": [dashboard.indexed_series(window["closes"][asset]) for asset in dashboard.ASSETS],
        "returns": [[value * 100 for value in window["returns"][asset]] for asset in dashboard.ASSETS],
        "drawdown": [dashboard.drawdown_series(window["closes"][asset]) for asset in dashboard.ASSETS],
    }
    report = {
        "meta": {"rows": len(dataset["rows"]), "first_date": dataset["dates"][0], "data_fetched": _fetched_time(outputs["report_text"]),
                 "generated_at": generated_at.astimezone(timezone.utc).strftime(TIME_FORMAT)},
        "assets": {"btc": _asset_figures("BTC", metrics, shorter), "eth": _asset_figures("ETH", metrics, shorter)},
        "pair": {"volatility_ratio": metrics["volatility_ratio"], "joint_negative_days": metrics["joint_negative_days"], "overlap_share": metrics["down_day_overlap_share"]},
        "coverage": _coverage(dataset),
        "charts": [_chart_entry(kind, *series[kind]) for kind in CHART_IDS],
        "comparison": comparison_of(metrics["cumulative_return"]["BTC"], metrics["cumulative_return"]["ETH"], shorter[7]["correlation"], shorter[14]["correlation"]),
        "ohlc": _ohlc(dataset),
        "intelligence": intelligence_of(dataset),
    }
    validation = build_validation(outputs)
    if validation is not None:                                                      # without evidence there is no validation part, and the e-mail never shows a PASS
        report["validation"] = validation
    check_report(report)
    return report


# ---------------------------------------------------------------- checking a report before it is sent
def _png_problem(data):
    """Why these bytes are not the expected chart file, or None: signature, every chunk's CRC, a 600 x 240 palette header, a palette, pixel data, and the end chunk last."""
    if len(data) > MAX_CHART_BYTES:
        return f"larger than {MAX_CHART_BYTES} bytes"
    if data[:8] != PNG_SIGNATURE:
        return "not a PNG file (wrong signature)"
    position, tags = 8, []
    while position < len(data):
        if position + 12 > len(data):
            return "truncated"
        length, tag = struct.unpack(">I4s", data[position:position + 8])
        end = position + 12 + length
        if end > len(data):
            return "truncated"
        body = data[position + 8:position + 8 + length]
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(tag + body) & 0xFFFFFFFF:
            return "a chunk fails its checksum"
        if tag == b"IHDR":
            if length != 13 or struct.unpack(">IIBBBBB", body)[:2] != (CHART_WIDTH, CHART_HEIGHT) or body[9] != 3:
                return "not a 600 x 240 palette image"
        tags.append(tag)
        position = end
    if not tags or tags[0] != b"IHDR" or tags[-1] != b"IEND" or b"PLTE" not in tags or b"IDAT" not in tags:
        return "missing a required part"
    return None


def _finite(value, label, low=None, high=None, integer=False, nullable=False):
    if value is None and nullable:
        return
    ok = isinstance(value, int) and not isinstance(value, bool) if integer else isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok or not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        raise EmailReportError(f"{label} is not a valid number")


def _date(value, label, nullable=False):
    if value is None and nullable:
        return
    try:
        if not isinstance(value, str) or DATE_TEXT.fullmatch(value) is None:
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise EmailReportError(f"{label} is not a valid date")


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise EmailReportError(f"{label} does not have exactly the expected fields")


def _check_comparison(comparison):
    _keys(comparison, COMPARISON_KEYS, "comparison")
    _finite(comparison["spread_pp"], "comparison.spread_pp", low=-100000, high=100000)
    stronger, spread = comparison["stronger"], comparison["spread_pp"]
    if stronger not in ("BTC", "ETH", "equal") or (stronger == "ETH" and not spread > 0) or (stronger == "BTC" and not spread < 0) or (stronger == "equal" and abs(spread) >= EQUAL_BELOW_PP):
        raise EmailReportError("comparison.stronger does not agree with comparison.spread_pp")
    for key in ("correlation_7d", "correlation_14d"):
        _finite(comparison[key], f"comparison.{key}", low=-1, high=1, nullable=True)


def _check_validation(validation):
    _keys(validation, VALIDATION_KEYS, "validation")
    if validation["overall"] not in (candle_rules.PASS, candle_rules.FAIL, candle_rules.BLOCKED, candle_rules.NOT_RUN, candle_rules.INCONCLUSIVE):
        raise EmailReportError("validation.overall is not one of the five outcomes")
    if not isinstance(validation["run_id"], str) or not run_evidence.valid_identity(validation["run_id"], 1):
        raise EmailReportError("validation.run_id is not a run id")
    for key in ("fields_total", "fields_validated", "instances_expected", "instances_evaluated", "instances_passed", "checks_total", "checks_passed"):
        _finite(validation[key], f"validation.{key}", low=0, integer=True)
    if (validation["fields_total"] != len(dashboard.EXPECTED_COLUMNS) or validation["fields_validated"] > validation["fields_total"] or validation["instances_passed"] > validation["instances_evaluated"]
            or validation["instances_evaluated"] > validation["instances_expected"] or validation["checks_passed"] > validation["checks_total"]):
        raise EmailReportError("the validation counts are not consistent")
    if not isinstance(validation["assumptions"], list) or any(a not in KNOWN_ASSUMPTIONS for a in validation["assumptions"]) or not isinstance(validation["exceptions"], list) or len(validation["exceptions"]) > MAX_EXCEPTIONS:
        raise EmailReportError("validation.assumptions or validation.exceptions is not valid")
    for entry in validation["exceptions"]:
        _keys(entry, EXCEPTION_KEYS, "a validation exception")
        if (not isinstance(entry["rule_id"], str) or not RULE_ID_TEXT.fullmatch(entry["rule_id"]) or entry["severity"] not in ("ERROR", "WARNING") or entry["outcome"] not in ("FAIL", "BLOCKED", "NOT RUN", "INCONCLUSIVE")
                or not isinstance(entry["field"], str) or not 1 <= len(entry["field"]) <= 40 or not isinstance(entry["explanation"], str) or not 1 <= len(entry["explanation"]) <= MAX_EXCEPTION_TEXT
                or re.search(r"[\x00-\x1f\x7f<>]", entry["field"] + entry["explanation"])):
            raise EmailReportError("a validation exception is not valid")
    if validation["overall"] == candle_rules.PASS and not (
            validation["fields_validated"] == validation["fields_total"] and validation["instances_expected"] > 0 and validation["instances_evaluated"] == validation["instances_expected"]
            and validation["instances_passed"] == validation["instances_expected"] and validation["checks_total"] > 0 and validation["checks_passed"] == validation["checks_total"] and not validation["exceptions"]):
        raise EmailReportError("a validation PASS needs complete evidence: every field, instance and check passed and no exception")


def _check_ohlc(ohlc):
    _keys(ohlc, {"date", "btc", "eth"}, "ohlc")
    _date(ohlc["date"], "ohlc.date")
    for asset in ("btc", "eth"):
        values = ohlc[asset]
        _keys(values, {"open", "high", "low", "close"}, f"ohlc.{asset}")
        for name in ("open", "high", "low", "close"):
            _finite(values[name], f"ohlc.{asset}.{name}", low=0)
            if values[name] <= 0:
                raise EmailReportError(f"ohlc.{asset}.{name} must be above zero")
        if not (values["low"] <= values["open"] <= values["high"] and values["low"] <= values["close"] <= values["high"]):
            raise EmailReportError(f"ohlc.{asset}: open and close must lie between low and high")


def check_report(report):
    """Raises EmailReportError (one line) unless the report is complete, plausible, safe to send and small enough for the web app's request limit."""
    if not isinstance(report, dict) or not REPORT_KEYS <= set(report) or set(report) - REPORT_KEYS - OPTIONAL_KEYS:
        raise EmailReportError("the report does not have exactly the expected fields")
    _keys(report["meta"], META_KEYS, "meta")
    _finite(report["meta"]["rows"], "meta.rows", low=dashboard.WINDOWS[0] + 1, integer=True)
    _date(report["meta"]["first_date"], "meta.first_date")
    for key in ("data_fetched", "generated_at"):
        try:
            datetime.strptime(report["meta"][key], TIME_FORMAT)
        except (TypeError, ValueError):
            raise EmailReportError(f"meta.{key} is not a UTC time in the expected format")
    _keys(report["assets"], {"btc", "eth"}, "assets")
    for asset, figures in report["assets"].items():
        _keys(figures, ASSET_KEYS, f"assets.{asset}")
        for key in ("return_7d", "return_14d", "return_30d"):
            _finite(figures[key], f"assets.{asset}.{key}", low=-1)
        _finite(figures["volatility_annualised"], f"assets.{asset}.volatility_annualised", low=0)
        _keys(figures["worst_day"], {"value", "date"}, f"assets.{asset}.worst_day")
        _finite(figures["worst_day"]["value"], f"assets.{asset}.worst_day.value", low=-1)
        _date(figures["worst_day"]["date"], f"assets.{asset}.worst_day.date")
        _finite(figures["negative_days"], f"assets.{asset}.negative_days", low=0, high=WINDOW_DAYS, integer=True)
        for key in ("drawdown_peak", "drawdown_trough"):
            _date(figures[key], f"assets.{asset}.{key}", nullable=True)
    if "ohlc" in report:
        _check_ohlc(report["ohlc"])
    if "intelligence" in report:
        _check_intelligence(report["intelligence"])
    _keys(report["pair"], PAIR_KEYS, "pair")
    _finite(report["pair"]["volatility_ratio"], "pair.volatility_ratio", low=0, nullable=True)
    _finite(report["pair"]["joint_negative_days"], "pair.joint_negative_days", low=0, high=WINDOW_DAYS, integer=True)
    _finite(report["pair"]["overlap_share"], "pair.overlap_share", low=0, high=1, nullable=True)
    coverage = report["coverage"]
    if not isinstance(coverage, list) or [entry.get("name") if isinstance(entry, dict) else None for entry in coverage] != list(dashboard.EXPECTED_COLUMNS):
        raise EmailReportError("coverage must list the 17 variables in the CSV order")
    for entry in coverage:
        _keys(entry, {"name", "valid_rows", "latest"}, "a coverage entry")
        _finite(entry["valid_rows"], f"coverage {entry['name']} valid_rows", low=0, high=report["meta"]["rows"], integer=True)
        if not isinstance(entry["latest"], str) or len(entry["latest"]) > MAX_LATEST_CHARS or not entry["latest"].isprintable():
            raise EmailReportError(f"coverage {entry['name']} latest value is not short printable text")
    charts = report["charts"]
    if not isinstance(charts, list) or len(charts) != len(CHART_IDS):
        raise EmailReportError(f"the report needs exactly {len(CHART_IDS)} charts")
    for chart, expected in zip(charts, CHART_IDS):
        _keys(chart, CHART_KEYS, "a chart")
        if chart["id"] != expected or chart["width"] != CHART_WIDTH or chart["height"] != CHART_HEIGHT or isinstance(chart["width"], bool) or isinstance(chart["height"], bool):
            raise EmailReportError(f"the {expected} chart is missing, out of order or not {CHART_WIDTH} x {CHART_HEIGHT}")
        for key in ("low", "high", "btc_last", "eth_last"):
            _finite(chart[key], f"chart {expected} {key}")
        if not chart["low"] < chart["high"]:
            raise EmailReportError(f"the {expected} chart scale is empty")
        try:
            data = base64.b64decode(chart["png_base64"], validate=True) if isinstance(chart["png_base64"], str) else b""
        except (binascii.Error, ValueError):
            raise EmailReportError(f"the {expected} chart is not valid base64")
        problem = _png_problem(data)
        if problem:
            raise EmailReportError(f"the {expected} chart is not a valid image: {problem}")
    if "comparison" in report:
        _check_comparison(report["comparison"])
    if "validation" in report:
        _check_validation(report["validation"])
    try:
        size = len(json.dumps(report, allow_nan=False))
    except ValueError:
        raise EmailReportError("the report holds a value that is not strict JSON")
    if size > MAX_REPORT_CHARS:
        raise EmailReportError(f"the report is {size} characters, more than the limit of {MAX_REPORT_CHARS}")
