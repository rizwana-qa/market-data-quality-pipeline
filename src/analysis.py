"""Calculations for the Bitcoin/Ethereum daily-return analysis.

Everything here is plain arithmetic on prices that were already fetched and validated:
no network, no files.
"""

import math
import statistics
from datetime import datetime, timezone

SECONDS_PER_DAY = 86400
RETURN_COUNT = 30  # the window: 30 daily returns ...
CLOSE_COUNT = RETURN_COUNT + 1  # ... which need 31 consecutive daily closing prices
ANNUALISATION_DAYS = 365  # crypto trades every day

# Observation-rule thresholds. They are descriptive conventions fixed before any result was seen.
# They are not financial standards and not trading signals.
CORRELATION_WEAK_BELOW = 0.30
CORRELATION_STRONG_FROM = 0.70
VOLATILITY_RATIO_HIGH = 1.25
VOLATILITY_RATIO_LOW = 0.80
DOWN_DAY_OVERLAP_HIGH = 0.75
DOWN_DAY_OVERLAP_LOW = 0.50


def format_date(unix_seconds):
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def _require_positive(closes):
    for close in closes:
        if not (math.isfinite(close) and close > 0):
            raise ValueError(f"closing prices must be positive numbers, found {close!r}")


def select_aligned_closes(first_candles, second_candles, count=CLOSE_COUNT):
    """Return (timestamps, first_closes, second_closes) for the last `count` consecutive UTC days.

    Both series must end on exactly the same `count` dates. A duplicate, a gap, a different date
    or a non-positive price is an error: observations are never skipped or repaired.
    """
    dates_by_series = []
    closes_by_series = []
    for label, candles in (("first", first_candles), ("second", second_candles)):
        close_by_date = {}
        for candle in candles:
            if candle["timestamp"] in close_by_date:
                raise ValueError(f"the {label} series has a duplicate date: {format_date(candle['timestamp'])}")
            close_by_date[candle["timestamp"]] = candle["close"]
        latest = sorted(close_by_date)[-count:]
        if len(latest) < count:
            raise ValueError(f"the {label} series has only {len(latest)} completed days, {count} are needed")
        dates_by_series.append(latest)
        closes_by_series.append([close_by_date[date] for date in latest])

    dates = dates_by_series[0]
    if dates != dates_by_series[1]:
        raise ValueError("the two series do not end on the same dates")
    for earlier, later in zip(dates, dates[1:]):
        if later - earlier != SECONDS_PER_DAY:
            raise ValueError(f"gap in the daily data between {format_date(earlier)} and {format_date(later)}")
    for closes in closes_by_series:
        _require_positive(closes)
    return dates, closes_by_series[0], closes_by_series[1]


def simple_returns(closes):
    """Daily simple returns: each close divided by the previous close, minus 1."""
    _require_positive(closes)
    return [later / earlier - 1 for earlier, later in zip(closes, closes[1:])]


def pearson_correlation(x, y):
    """Pearson correlation of two equal-length series, or None when either one is constant."""
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("correlation needs two series of the same length with at least 2 values")
    if len(set(x)) == 1 or len(set(y)) == 1:
        return None
    return statistics.correlation(x, y)


def correlation_components(x, y):
    """The sums behind the correlation, so that it can be checked by hand: rho = sxy / sqrt(sxx * syy)."""
    n = len(x)
    mean_x = math.fsum(x) / n
    mean_y = math.fsum(y) / n
    return {
        "n": n,
        "mean_x": mean_x,
        "mean_y": mean_y,
        "sxx": math.fsum((a - mean_x) ** 2 for a in x),
        "syy": math.fsum((b - mean_y) ** 2 for b in y),
        "sxy": math.fsum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)),
    }


def daily_volatility(returns):
    """Sample standard deviation (divides by n - 1) of the daily returns."""
    return statistics.stdev(returns)


def annualised_volatility(daily):
    return daily * math.sqrt(ANNUALISATION_DAYS)


def volatility_ratio(second_daily, first_daily):
    """Second asset's daily volatility divided by the first's; None when the first one is zero."""
    return None if first_daily == 0 else second_daily / first_daily


def count_negative_days(returns):
    return sum(1 for value in returns if value < 0)


def count_joint_negative_days(first_returns, second_returns):
    return sum(1 for a, b in zip(first_returns, second_returns) if a < 0 and b < 0)


def worst_day(dates, returns):
    """The lowest daily return and its date (the first one if several are equal)."""
    index = min(range(len(returns)), key=returns.__getitem__)
    return returns[index], dates[index]


def max_drawdown(dates, closes):
    """Largest fall from a running peak: (drawdown <= 0, peak date, trough date); dates are None if there was no fall."""
    peak_index = 0
    worst, worst_peak, worst_trough = 0.0, None, None
    for index, close in enumerate(closes):
        if close > closes[peak_index]:
            peak_index = index
        drawdown = close / closes[peak_index] - 1
        if drawdown < worst:
            worst, worst_peak, worst_trough = drawdown, dates[peak_index], dates[index]
    return worst, worst_peak, worst_trough


def cumulative_return(closes):
    return closes[-1] / closes[0] - 1


def classify_correlation(rho):
    """Rule R1: how strong is the co-movement, judged by the size of the correlation."""
    if rho is None:
        return "not computable"
    if rho == 0:
        return "weak (no linear direction)"
    size = abs(rho)
    if size < CORRELATION_WEAK_BELOW:
        strength = "weak"
    elif size < CORRELATION_STRONG_FROM:
        strength = "moderate"
    else:
        strength = "strong"
    return f"{strength} {'positive' if rho > 0 else 'negative'}"


def classify_volatility_ratio(ratio):
    """Rule R2: is the second asset (ETH) clearly more or less volatile than the first (BTC)?"""
    if ratio is None:
        return "not computable"
    if ratio >= VOLATILITY_RATIO_HIGH:
        return "ETH noticeably more volatile than BTC"
    if ratio <= VOLATILITY_RATIO_LOW:
        return "ETH noticeably less volatile than BTC"
    return "similar volatility (within about ±25%)"


def classify_down_day_overlap(first_negative_days, second_negative_days, both_negative_days):
    """Rule R3: of the down days of the asset with fewer of them, what share was also a down day for the other?

    Returns (label, share); the share is None when one asset had no down days.
    """
    fewer = min(first_negative_days, second_negative_days)
    if fewer == 0:
        return "not assessable", None
    share = both_negative_days / fewer
    if share >= DOWN_DAY_OVERLAP_HIGH:
        return "down days largely coincide", share
    if share <= DOWN_DAY_OVERLAP_LOW:
        return "down days often occur separately", share
    return "down days partly coincide", share


def analyse(timestamps, btc_closes, eth_closes):
    """Compute every metric and observation label from exactly CLOSE_COUNT aligned daily closes."""
    if len(timestamps) != CLOSE_COUNT or len(btc_closes) != CLOSE_COUNT or len(eth_closes) != CLOSE_COUNT:
        raise ValueError(f"exactly {CLOSE_COUNT} aligned closing prices are needed")
    return_dates = timestamps[1:]
    btc_returns = simple_returns(btc_closes)
    eth_returns = simple_returns(eth_closes)

    correlation = pearson_correlation(btc_returns, eth_returns)
    btc_daily = daily_volatility(btc_returns)
    eth_daily = daily_volatility(eth_returns)
    ratio = volatility_ratio(eth_daily, btc_daily)
    btc_negative = count_negative_days(btc_returns)
    eth_negative = count_negative_days(eth_returns)
    joint = count_joint_negative_days(btc_returns, eth_returns)
    overlap_label, overlap_share = classify_down_day_overlap(btc_negative, eth_negative, joint)

    return {
        "timestamps": timestamps,
        "return_dates": return_dates,
        "closes": {"BTC": btc_closes, "ETH": eth_closes},
        "returns": {"BTC": btc_returns, "ETH": eth_returns},
        "correlation": correlation,
        "correlation_components": correlation_components(btc_returns, eth_returns),
        "volatility": {
            "BTC": {"daily": btc_daily, "annualised": annualised_volatility(btc_daily)},
            "ETH": {"daily": eth_daily, "annualised": annualised_volatility(eth_daily)},
        },
        "volatility_ratio": ratio,
        "negative_days": {"BTC": btc_negative, "ETH": eth_negative},
        "joint_negative_days": joint,
        "down_day_overlap_share": overlap_share,
        "worst_day": {"BTC": worst_day(return_dates, btc_returns), "ETH": worst_day(return_dates, eth_returns)},
        "max_drawdown": {"BTC": max_drawdown(timestamps, btc_closes), "ETH": max_drawdown(timestamps, eth_closes)},
        "cumulative_return": {"BTC": cumulative_return(btc_closes), "ETH": cumulative_return(eth_closes)},
        "rules": {
            "R1": classify_correlation(correlation),
            "R2": classify_volatility_ratio(ratio),
            "R3": overlap_label,
        },
    }
