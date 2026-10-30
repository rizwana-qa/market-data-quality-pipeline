"""Enhanced report: the same 30-day Bitcoin/Ethereum analysis, with an executive summary, all nine data fields and charts.

Usage: python src/report_enhanced.py

It fetches and validates the data exactly like run_workflow.py (same fetch_data.py functions), calculates with analysis.py,
and writes five files to reports/ (<date> is the last completed UTC day):
    enhanced_report_<date>.md             the report
    enhanced_report_<date>_data.csv       Date, Open, High, Low, Close, Daily Return %, Volume, VWAP, Trade count for both assets
    enhanced_report_<date>_<chart>.svg    three charts: indexed_price, return_scatter, drawdown
The reports written by run_workflow.py are never touched. Exit codes are the same as run_workflow.py.
"""

import csv
import io
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import analysis
from fetch_data import (
    PAIRS,
    check_pair_alignment,
    fetch_daily_candles,
    format_utc_date,
    print_pair_summary,
    print_validation_failures,
    validate_pair,
)
from run_workflow import DEFAULT_REPORT_DIR, SOURCE_URL, drawdown_text, percent, write_outputs

QUESTION = ("How has the relationship between Bitcoin and Ethereum daily price movements behaved over the most recent 30 days, "
            "and what do their correlation, volatility, and downside-risk patterns reveal?")
ASSETS = ("BTC", "ETH")
ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum"}
COLORS = {"BTC": "#0072B2", "ETH": "#D55E00"}
CHART_NAMES = ("indexed_price", "return_scatter", "drawdown")
TABLE_HEADER = "| Date | Open | High | Low | Close | Daily Return % | Volume | VWAP | Trades |"
CSV_FIELDS = ("open", "high", "low", "close", "daily_return_pct", "volume", "vwap", "trade_count")
CHART_WIDTH, CHART_HEIGHT = 720, 380
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 70, 20, 50, 60


# ---------------------------------------------------------------- small calculations
def window_candles(candles, timestamps):
    """The candles for exactly these dates, in the order of the dates."""
    by_date = {candle["timestamp"]: candle for candle in candles}
    window = []
    for timestamp in timestamps:
        if timestamp not in by_date:
            raise ValueError(f"the candle for {format_utc_date(timestamp)} is missing")
        window.append(by_date[timestamp])
    return window


def check_window_plausibility(windows):
    """Problems that make the report window implausible (an empty list means all is well).

    Per candle: every price field and the volume must be a finite number that is not negative, the trade count must not be
    negative, low must not exceed high, and open, close and VWAP must lie between low and high (inclusive, no tolerance).
    The VWAP range check is skipped on a day with zero volume, because VWAP is undefined without volume.
    Across the window: BTC and ETH must not have identical closing prices on every day.

    Limited scope of the last check: it detects identical closing prices only. It does not detect near-identical series,
    swapped data with different numbers, or data that is plausible but wrong.
    """
    problems = []
    for asset in ASSETS:
        for candle in windows[asset]:
            where = f"{asset} {format_utc_date(candle['timestamp'])}"
            malformed = False
            for field in ("open", "high", "low", "close", "vwap", "volume"):
                if not math.isfinite(candle[field]):
                    problems.append(f"{where}: {field} is not a finite number ({candle[field]})")
                    malformed = True
                elif candle[field] < 0:
                    problems.append(f"{where}: negative {field} ({candle[field]})")
                    malformed = True
            if candle["trade_count"] < 0:
                problems.append(f"{where}: negative trade count ({candle['trade_count']})")
                malformed = True
            if malformed:
                continue
            if candle["low"] > candle["high"]:
                problems.append(f"{where}: low ({candle['low']}) is above high ({candle['high']})")
                continue
            fields = ("open", "close", "vwap") if candle["volume"] > 0 else ("open", "close")
            for field in fields:
                if not candle["low"] <= candle[field] <= candle["high"]:
                    problems.append(f"{where}: {field} ({candle[field]}) is outside the low-high range")
    if [candle["close"] for candle in windows["BTC"]] == [candle["close"] for candle in windows["ETH"]]:
        problems.append("BTC and ETH have identical closing prices on every day: the two series look like the same data")
    return problems


def indexed_series(closes):
    """Closing prices rebased so that the first close is 100."""
    return [close / closes[0] * 100 for close in closes]


def drawdown_series(closes):
    """For each day: how far the close is below the highest close so far (0 or negative, as a fraction)."""
    peak = closes[0]
    series = []
    for close in closes:
        peak = max(peak, close)
        series.append(close / peak - 1)
    return series


def activity_summary(candles):
    """Volume, trade count and price-range facts over the candles."""
    total_volume = sum(candle["volume"] for candle in candles)
    if not math.isfinite(total_volume):
        raise ValueError("the total volume over the window is not a finite number")
    total_trades = sum(candle["trade_count"] for candle in candles)
    highest = max(candles, key=lambda candle: candle["high"])
    lowest = min(candles, key=lambda candle: candle["low"])
    return {
        "total_volume": total_volume,
        "mean_volume": total_volume / len(candles),
        "total_trades": total_trades,
        "mean_trades": total_trades / len(candles),
        "highest_high": (highest["high"], highest["timestamp"]),
        "lowest_low": (lowest["low"], lowest["timestamp"]),
    }


# ---------------------------------------------------------------- charts (plain SVG text, no libraries)
def _svg_open(title, description):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" width="{CHART_WIDTH}" height="{CHART_HEIGHT}" role="img" aria-labelledby="chart-title chart-desc">',
        f'<title id="chart-title">{escape(title)}</title>',
        f'<desc id="chart-desc">{escape(description)}</desc>',
        f'<rect width="{CHART_WIDTH}" height="{CHART_HEIGHT}" fill="#ffffff"/>',
        f'<text x="{MARGIN_LEFT}" y="22" font-family="sans-serif" font-size="15" font-weight="bold" fill="#222222">{escape(title)}</text>',
    ]


def _scale(values):
    """(low, high) of the values with a little padding; a flat series gets a visible range."""
    low, high = min(values), max(values)
    if low == high:
        low, high = low - 1, high + 1
    padding = (high - low) * 0.05
    return low - padding, high + padding


def _axis_parts(low, high, y_of, y_label):
    parts = []
    for step in range(5):
        value = low + (high - low) * step / 4
        y = y_of(value)
        parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y:.1f}" x2="{CHART_WIDTH - MARGIN_RIGHT}" y2="{y:.1f}" stroke="#dddddd"/>')
        parts.append(f'<text x="{MARGIN_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11" fill="#444444">{value:.1f}</text>')
    parts.append(f'<text x="16" y="{(MARGIN_TOP + CHART_HEIGHT - MARGIN_BOTTOM) / 2:.0f}" transform="rotate(-90 16 {(MARGIN_TOP + CHART_HEIGHT - MARGIN_BOTTOM) / 2:.0f})" '
                 f'text-anchor="middle" font-family="sans-serif" font-size="12" fill="#444444">{escape(y_label)}</text>')
    return parts


def line_chart_svg(title, description, labels, series, y_label):
    """One polyline per (name, values, colour) in `series`; `labels` are the x positions (dates)."""
    low, high = _scale([value for _, values, _ in series for value in values])
    plot_width, plot_height = CHART_WIDTH - MARGIN_LEFT - MARGIN_RIGHT, CHART_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    x_of = lambda index: MARGIN_LEFT + plot_width * index / max(len(labels) - 1, 1)
    y_of = lambda value: MARGIN_TOP + plot_height * (high - value) / (high - low)
    parts = _svg_open(title, description) + _axis_parts(low, high, y_of, y_label)
    for index, anchor in ((0, "start"), (len(labels) // 2, "middle"), (len(labels) - 1, "end")):
        parts.append(f'<text x="{x_of(index):.1f}" y="{CHART_HEIGHT - MARGIN_BOTTOM + 20}" text-anchor="{anchor}" font-family="sans-serif" font-size="11" fill="#444444">{escape(labels[index])}</text>')
    for position, (name, values, color) in enumerate(series):
        points = " ".join(f"{x_of(index):.1f},{y_of(value):.1f}" for index, value in enumerate(values))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        legend_x = MARGIN_LEFT + 90 * position
        parts.append(f'<line x1="{legend_x}" y1="{CHART_HEIGHT - 14}" x2="{legend_x + 24}" y2="{CHART_HEIGHT - 14}" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{legend_x + 30}" y="{CHART_HEIGHT - 10}" font-family="sans-serif" font-size="12" fill="#222222">{escape(name)}</text>')
    return "\n".join(parts + ["</svg>", ""])


def scatter_svg(title, description, xs, ys, x_label, y_label):
    """One circle per (x, y) pair, with the zero lines drawn."""
    x_low, x_high = _scale(list(xs) + [0])
    y_low, y_high = _scale(list(ys) + [0])
    plot_width, plot_height = CHART_WIDTH - MARGIN_LEFT - MARGIN_RIGHT, CHART_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    x_of = lambda value: MARGIN_LEFT + plot_width * (value - x_low) / (x_high - x_low)
    y_of = lambda value: MARGIN_TOP + plot_height * (y_high - value) / (y_high - y_low)
    parts = _svg_open(title, description) + _axis_parts(y_low, y_high, y_of, y_label)
    parts.append(f'<line x1="{x_of(0):.1f}" y1="{MARGIN_TOP}" x2="{x_of(0):.1f}" y2="{CHART_HEIGHT - MARGIN_BOTTOM}" stroke="#888888"/>')
    parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y_of(0):.1f}" x2="{CHART_WIDTH - MARGIN_RIGHT}" y2="{y_of(0):.1f}" stroke="#888888"/>')
    for step in range(5):
        value = x_low + (x_high - x_low) * step / 4
        parts.append(f'<text x="{x_of(value):.1f}" y="{CHART_HEIGHT - MARGIN_BOTTOM + 20}" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#444444">{value:.1f}</text>')
    parts.append(f'<text x="{MARGIN_LEFT + plot_width / 2:.0f}" y="{CHART_HEIGHT - 12}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#444444">{escape(x_label)}</text>')
    for x, y in zip(xs, ys):
        parts.append(f'<circle cx="{x_of(x):.1f}" cy="{y_of(y):.1f}" r="4" fill="{COLORS["ETH"]}" fill-opacity="0.7"/>')
    return "\n".join(parts + ["</svg>", ""])


def build_charts(results):
    """The three charts, built only from the analysed data. Returns {chart name: SVG text}."""
    dates = [format_utc_date(timestamp) for timestamp in results["timestamps"]]
    closes, returns, rho = results["closes"], results["returns"], results["correlation"]
    indexed = {asset: indexed_series(closes[asset]) for asset in ASSETS}
    drawdowns = {asset: [value * 100 for value in drawdown_series(closes[asset])] for asset in ASSETS}
    rho_text = "undefined" if rho is None else f"{rho:.2f}"
    return {
        "indexed_price": line_chart_svg(
            "Closing prices, first close = 100",
            f"BTC ends at {indexed['BTC'][-1]:.2f} and ETH at {indexed['ETH'][-1]:.2f} on {dates[-1]} (first close = 100 on {dates[0]}).",
            dates, [(asset, indexed[asset], COLORS[asset]) for asset in ASSETS], "Index (first close = 100)"),
        "return_scatter": scatter_svg(
            "Daily returns: BTC against ETH",
            f"{len(returns['BTC'])} days, one circle per day. Correlation of the daily returns: {rho_text}.",
            [value * 100 for value in returns["BTC"]], [value * 100 for value in returns["ETH"]],
            "BTC daily return (%)", "ETH daily return (%)"),
        "drawdown": line_chart_svg(
            "Fall of the close from its running peak",
            f"Lowest point: BTC {min(drawdowns['BTC']):.2f}%, ETH {min(drawdowns['ETH']):.2f}%. "
            f"On {dates[-1]}: BTC {drawdowns['BTC'][-1]:.2f}%, ETH {drawdowns['ETH'][-1]:.2f}%.",
            dates, [(asset, drawdowns[asset], COLORS[asset]) for asset in ASSETS], "Below running peak (%)"),
    }


# ---------------------------------------------------------------- the report text
def depth_phrase(eth_value, btc_value):
    """How Ethereum's fall compares with Bitcoin's (both are 0 or negative; the lower one is deeper)."""
    if eth_value < btc_value:
        return "deeper than"
    if eth_value > btc_value:
        return "shallower than"
    return "as deep as"


def price(value):
    return f"{value:,.2f}"


def data_table(asset, candles, returns):
    lines = [f"### {ASSET_NAMES[asset]} ({asset})", "", TABLE_HEADER, "|---|---|---|---|---|---|---|---|---|"]
    for index, candle in enumerate(candles):
        daily_return = "–" if index == 0 else percent(returns[index - 1], 4)  # the first close has no previous day
        lines.append(
            f"| {format_utc_date(candle['timestamp'])} | {price(candle['open'])} | {price(candle['high'])} | {price(candle['low'])} | "
            f"{price(candle['close'])} | {daily_return} | {candle['volume']:,.4f} | {price(candle['vwap'])} | {candle['trade_count']:,} |"
        )
    return lines + [""]


def build_report(results, windows, fetched_at, excluded_dates, chart_files):
    timestamps, return_dates = results["timestamps"], results["return_dates"]
    first_close, last = format_utc_date(timestamps[0]), format_utc_date(timestamps[-1])
    first_return = format_utc_date(return_dates[0])
    n = analysis.RETURN_COUNT
    returns, vol, parts, rules = results["returns"], results["volatility"], results["correlation_components"], results["rules"]
    rho, ratio, share, joint = results["correlation"], results["volatility_ratio"], results["down_day_overlap_share"], results["joint_negative_days"]
    worst, drawdown, negative, cumulative = results["worst_day"], results["max_drawdown"], results["negative_days"], results["cumulative_return"]
    rho_text = "undefined (a series is constant)" if rho is None else f"{rho:.6f}"
    ratio_text = "not computable (BTC volatility is zero)" if ratio is None else f"{ratio:.3f}"
    share_text = "not assessable (an asset had no down days)" if share is None else f"{share:.2f}"
    activity = {asset: activity_summary(windows[asset]) for asset in ASSETS}
    worst_phrase = depth_phrase(worst["ETH"][0], worst["BTC"][0])
    drawdown_phrase = depth_phrase(drawdown["ETH"][0], drawdown["BTC"][0])
    excluded = ", ".join(excluded_dates) if excluded_dates else "none"

    lines = [
        "# Bitcoin vs Ethereum: how did daily returns move together over the last 30 days?",
        "",
        "> This report is educational, not financial advice. A correlation does not establish causation, and past behaviour does not predict the future.",
        "",
        "## Executive summary",
        "",
        f"**Question.** {QUESTION}",
        "",
        f"**Answer in brief.** Over the {n} daily returns from {first_return} to {last}, Bitcoin and Ethereum showed a {rules['R1']} co-movement "
        f"(correlation of daily returns: {rho_text}). The ETH / BTC daily volatility ratio was {ratio_text} ({rules['R2']}). "
        f"Both assets fell on {joint} of {n} days ({rules['R3']}; overlap share {share_text}). "
        f"Ethereum's worst day ({percent(worst['ETH'][0])}) was {worst_phrase} Bitcoin's ({percent(worst['BTC'][0])}), and its maximum drawdown "
        f"({drawdown['ETH'][0] * 100:.2f}%) was {drawdown_phrase} Bitcoin's ({drawdown['BTC'][0] * 100:.2f}%).",
        "",
        "| Key figure | BTC | ETH |",
        "|---|---|---|",
        f"| Cumulative return over the window | {percent(cumulative['BTC'])} | {percent(cumulative['ETH'])} |",
        f"| Daily volatility | {vol['BTC']['daily'] * 100:.4f}% | {vol['ETH']['daily'] * 100:.4f}% |",
        f"| Negative-return days | {negative['BTC']} of {n} | {negative['ETH']} of {n} |",
        f"| Worst daily return | {percent(worst['BTC'][0])} on {format_utc_date(worst['BTC'][1])} | {percent(worst['ETH'][0])} on {format_utc_date(worst['ETH'][1])} |",
        f"| Maximum drawdown | {drawdown_text(drawdown['BTC'])} | {drawdown_text(drawdown['ETH'])} |",
        "",
        "These labels come from three descriptive rules (R1 to R3, see Methodology) and describe this 30-day sample only. This is educational, not financial advice.",
        "",
        "## Methodology",
        "",
        f"- Source: Kraken public OHLC endpoint ({SOURCE_URL}), interval 1440 minutes (one candle per UTC day), pairs XBTUSD and ETHUSD; no account or API key",
        f"- Data fetched: {fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"- Return dates: {first_return} to {last} ({n} observations from {analysis.CLOSE_COUNT} daily closes, {first_close} to {last})",
        f"- Unfinished candle excluded: {excluded}",
        "- Both assets were validated and aligned by date; the fetch and validation checks passed before any calculation",
        "- Fields per asset and day: Open, High, Low, Close, Daily Return %, Volume (in BTC or ETH), VWAP (volume-weighted average price) and Trade count",
        "- Metrics use each day's closing price (the last trade of that UTC day); Open, High, Low, VWAP, Volume and Trade count are shown for context",
        "- Daily simple return: r_t = C_t / C_(t-1) - 1, dated by the later day",
        "- Correlation: Pearson correlation of the 30 aligned daily returns",
        "- Daily volatility: sample standard deviation of the 30 daily returns (divides by n - 1)",
        f"- Annualised volatility: daily volatility × √{analysis.ANNUALISATION_DAYS} (illustrative; assumes independent daily returns)",
        "- Maximum drawdown: the largest fall of the closing price from its running peak within the 31 closes",
        "",
        "Observation rules: descriptive conventions chosen before any result was seen. They are not financial standards and not trading signals.",
        "",
        "| Rule | Thresholds | Outcome |",
        "|---|---|---|",
        f"| R1 co-movement | size of the correlation: below {analysis.CORRELATION_WEAK_BELOW:.2f}; from {analysis.CORRELATION_WEAK_BELOW:.2f} up to {analysis.CORRELATION_STRONG_FROM:.2f}; {analysis.CORRELATION_STRONG_FROM:.2f} or more | {rules['R1']} |",
        f"| R2 relative volatility | ETH / BTC daily volatility: {analysis.VOLATILITY_RATIO_HIGH:.2f} or more; {analysis.VOLATILITY_RATIO_LOW:.2f} or less; otherwise | {rules['R2']} |",
        f"| R3 down-day overlap | share of the down days of the asset with fewer of them that were also down days for the other: {analysis.DOWN_DAY_OVERLAP_HIGH:.2f} or more; {analysis.DOWN_DAY_OVERLAP_LOW:.2f} or less; otherwise | {rules['R3']} |",
        "",
        "## Results",
        "",
        "### Key metrics",
        "",
        "| Metric | BTC | ETH |",
        "|---|---|---|",
        f"| Daily volatility (sample standard deviation) | {vol['BTC']['daily'] * 100:.4f}% | {vol['ETH']['daily'] * 100:.4f}% |",
        f"| Annualised volatility (illustrative) | {vol['BTC']['annualised'] * 100:.2f}% | {vol['ETH']['annualised'] * 100:.2f}% |",
        f"| Negative-return days | {negative['BTC']} of {n} ({negative['BTC'] / n * 100:.1f}%) | {negative['ETH']} of {n} ({negative['ETH'] / n * 100:.1f}%) |",
        f"| Worst daily return | {percent(worst['BTC'][0])} on {format_utc_date(worst['BTC'][1])} | {percent(worst['ETH'][0])} on {format_utc_date(worst['ETH'][1])} |",
        f"| Maximum drawdown (31 closes) | {drawdown_text(drawdown['BTC'])} | {drawdown_text(drawdown['ETH'])} |",
        f"| Cumulative return over the window (context) | {percent(cumulative['BTC'])} | {percent(cumulative['ETH'])} |",
        "",
        f"- Pearson correlation of the {n} aligned daily returns: {rho_text}",
        f"- ETH / BTC daily volatility ratio: {ratio_text}",
        f"- Days on which both assets fell: {joint} of {n}; overlap share: {share_text}",
        "",
        "### Market activity (31 days)",
        "",
        "| Measure | BTC | ETH |",
        "|---|---|---|",
        f"| Total volume | {activity['BTC']['total_volume']:,.2f} BTC | {activity['ETH']['total_volume']:,.2f} ETH |",
        f"| Mean daily volume | {activity['BTC']['mean_volume']:,.2f} BTC | {activity['ETH']['mean_volume']:,.2f} ETH |",
        f"| Total trades | {activity['BTC']['total_trades']:,} | {activity['ETH']['total_trades']:,} |",
        f"| Mean daily trades | {activity['BTC']['mean_trades']:,.1f} | {activity['ETH']['mean_trades']:,.1f} |",
        f"| Highest high (USD) | {price(activity['BTC']['highest_high'][0])} on {format_utc_date(activity['BTC']['highest_high'][1])} | {price(activity['ETH']['highest_high'][0])} on {format_utc_date(activity['ETH']['highest_high'][1])} |",
        f"| Lowest low (USD) | {price(activity['BTC']['lowest_low'][0])} on {format_utc_date(activity['BTC']['lowest_low'][1])} | {price(activity['ETH']['lowest_low'][0])} on {format_utc_date(activity['ETH']['lowest_low'][1])} |",
        "",
        "### Charts",
        "",
        "Drawn from the data in Appendix A and the CSV file next to this report.",
        "",
        f"![Closing prices of BTC and ETH, rebased so the first close is 100]({chart_files['indexed_price']})",
        "",
        f"![Scatter of daily BTC returns against daily ETH returns, one circle per day]({chart_files['return_scatter']})",
        "",
        f"![Fall of each close from its running peak, BTC and ETH]({chart_files['drawdown']})",
        "",
        "## Interpretation",
        "",
        f"- R1: the correlation of daily returns was {rho_text}, which the rule describes as: {rules['R1']}.",
        f"- R2: the ETH / BTC daily volatility ratio was {ratio_text}, which the rule describes as: {rules['R2']}.",
        f"- R3: both assets fell on {joint} of {n} days (overlap share {share_text}), which the rule describes as: {rules['R3']}.",
        f"- Downside: the worst day for Ethereum ({percent(worst['ETH'][0])}) was {worst_phrase} the worst day for Bitcoin ({percent(worst['BTC'][0])}); "
        f"its maximum drawdown ({drawdown['ETH'][0] * 100:.2f}%) was {drawdown_phrase} Bitcoin's ({drawdown['BTC'][0] * 100:.2f}%).",
        "- These labels describe this sample of 30 days only. They do not establish causation and say nothing about why prices moved or about future prices.",
        "",
        "## Limitations",
        "",
        "- Only 30 observations from one exchange (Kraken) and USD-quoted prices: a short sample that may not be representative.",
        "- The thresholds are conventions, and the annualisation assumes independent daily returns.",
        "- The first close has no return, so the Daily Return % of the first day is shown as a dash.",
        "- Volume, VWAP and Trade count are shown for context; they are not used in the correlation, volatility or downside measures.",
        "- The data fetcher excludes the unfinished day using the computer's own UTC clock. A wrong clock could make an unfinished candle count as complete, or stop the run.",
        "- The fetcher has no overall download deadline: a very slow server could make a run take a long time.",
        "- The fetcher follows redirects, and it ignores any Retry-After instruction when retrying.",
        "- These limits were identified during development and deliberately left unchanged.",
        "",
        "## Conclusion",
        "",
        f"In this sample, the co-movement of Bitcoin and Ethereum daily returns was {rules['R1']}, Ethereum's volatility relative to Bitcoin's was labelled \"{rules['R2']}\", "
        f"and their down days were labelled \"{rules['R3']}\". These are descriptions of this sample only, not explanations of the price moves and not forecasts.",
        "",
        "## Appendix A: Data tables",
        "",
        "Prices in USD; Volume in BTC or ETH; returns as percentages (the CSV holds them as percentages without the % sign).",
        "",
    ]
    for asset in ASSETS:
        lines += data_table(asset, windows[asset], returns[asset])
    lines += [
        "## Appendix B: Manual verification",
        "",
        "Check the correlation yourself. The 30 BTC and 30 ETH returns are in the CSV file next to this report (columns btc_daily_return_pct and eth_daily_return_pct, in percent).",
        "",
        "- Spreadsheet: put the two return columns into two ranges and enter =CORREL(range1, range2); it should match the correlation above.",
        "- Calculator: with x = BTC returns and y = ETH returns, use the sums below. They are computed on the returns as fractions.",
        "",
        f"- n = {parts['n']}",
        f"- mean of the BTC returns (x̄) = {parts['mean_x']:.12g}",
        f"- mean of the ETH returns (ȳ) = {parts['mean_y']:.12g}",
        f"- Sxx (sum of squared deviations of the BTC returns) = {parts['sxx']:.12g}",
        f"- Syy (sum of squared deviations of the ETH returns) = {parts['syy']:.12g}",
        f"- Sxy (sum of the products of the deviations) = {parts['sxy']:.12g}",
        f"- ρ = Sxy / √(Sxx · Syy) = {rho_text}",
        "",
        "> Educational use only. Not financial advice.",
        "",
    ]
    return "\n".join(lines)


def build_csv(results, windows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date"] + [f"{asset.lower()}_{field}" for asset in ASSETS for field in CSV_FIELDS])
    for index, timestamp in enumerate(results["timestamps"]):
        row = [format_utc_date(timestamp)]
        for asset in ASSETS:
            candle = windows[asset][index]
            daily_return_pct = "" if index == 0 else repr(results["returns"][asset][index - 1] * 100)
            row += [repr(candle["open"]), repr(candle["high"]), repr(candle["low"]), repr(candle["close"]), daily_return_pct,
                    repr(candle["volume"]), repr(candle["vwap"]), str(candle["trade_count"])]
        writer.writerow(row)
    return buffer.getvalue()


# ---------------------------------------------------------------- run
def main(report_dir=None):
    report_dir = Path(report_dir) if report_dir else DEFAULT_REPORT_DIR
    now = datetime.now(timezone.utc)
    completed_by_pair = {}
    excluded_dates = set()
    problems = []

    for pair in PAIRS:
        try:
            candles = fetch_daily_candles(pair)
        except (RuntimeError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        completed, unfinished, pair_problems = validate_pair(pair, candles, now)
        completed_by_pair[pair] = completed
        excluded_dates |= {format_utc_date(candle["timestamp"]) for candle in unfinished}
        problems += pair_problems
        print_pair_summary(pair, candles, completed, unfinished, pair_problems)

    first_pair, second_pair = PAIRS
    problems += check_pair_alignment(first_pair, completed_by_pair[first_pair], second_pair, completed_by_pair[second_pair])
    if problems:
        print_validation_failures(problems)
        return 1

    try:
        timestamps, btc_closes, eth_closes = analysis.select_aligned_closes(completed_by_pair[first_pair], completed_by_pair[second_pair])
        windows = {"BTC": window_candles(completed_by_pair[first_pair], timestamps), "ETH": window_candles(completed_by_pair[second_pair], timestamps)}
        window_problems = check_window_plausibility(windows)
        if window_problems:
            print_validation_failures(window_problems)
            return 1
        results = analysis.analyse(timestamps, btc_closes, eth_closes)
        prefix = f"enhanced_report_{format_utc_date(timestamps[-1])}"
        chart_files = {name: f"{prefix}_{name}.svg" for name in CHART_NAMES}
        charts = build_charts(results)
        files = [
            (f"{prefix}.md", build_report(results, windows, now, sorted(excluded_dates), chart_files)),
            (f"{prefix}_data.csv", build_csv(results, windows)),
        ] + [(chart_files[name], charts[name]) for name in CHART_NAMES]
        leftover_backups = write_outputs(report_dir, files)
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except OverflowError as error:
        print(f"ERROR: a number in the data is too large to calculate with ({error})", file=sys.stderr)
        return 1

    for backup in leftover_backups:
        print(f"WARNING: could not delete the old copy {backup}; it can be deleted by hand", file=sys.stderr)
    print()
    for name, _ in files:
        print(f"Written: {report_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
