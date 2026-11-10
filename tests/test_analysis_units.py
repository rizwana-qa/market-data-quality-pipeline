"""Unit tests for src/analysis.py (pure calculations). Written BEFORE the module exists."""
import math, random
from wf_support import *
import independent_calc as ind

A = analysis
R = Runner("analysis unit checks")
check = R.register
T0 = 1_760_000_000 // DAY * DAY
days = lambda count, start=0: [T0 + (start + i) * DAY for i in range(count)]
series = lambda ts, closes: [{"timestamp": t, "close": float(c)} for t, c in zip(ts, closes)]
near = lambda a, b, tol=1e-12: abs(a - b) <= tol * max(1.0, abs(b))

# ------------------------------------------------------------------ window selection and alignment
@check("window: 40 rows -> exactly the last 31 days, closes matched to dates")
def _():
    ts = days(40); dates, a, b = A.select_aligned_closes(series(ts, range(100, 140)), series(ts, range(200, 240)))
    assert dates == ts[-31:] and a == [float(c) for c in range(109, 140)] and b == [float(c) for c in range(209, 240)]
@check("window: exactly 31 rows is accepted")
def _():
    ts = days(31); assert len(A.select_aligned_closes(series(ts, range(31)[::-1] if False else range(1, 32)), series(ts, range(1, 32)))[0]) == 31
@check("window: 30 rows -> ValueError (not enough completed days)")
def _(): ts = days(30); raises(ValueError, lambda: A.select_aligned_closes(series(ts, range(1, 31)), series(ts, range(1, 31))), "only 30")
@check("window: a gap inside the last 31 days -> ValueError naming the gap")
def _():
    ts = days(40); ts_gap = ts[:20] + ts[21:]; raises(ValueError, lambda: A.select_aligned_closes(series(ts_gap, range(39)), series(ts_gap, range(39))), "gap")
@check("window: the two series end on different dates -> ValueError (misalignment)")
def _(): raises(ValueError, lambda: A.select_aligned_closes(series(days(40), range(1, 41)), series(days(40, 1), range(1, 41))), "same dates")
@check("window: same length but shifted by one day inside the window -> ValueError")
def _():
    a = days(40); b = a[:-1] + [a[-1] + DAY]; raises(ValueError, lambda: A.select_aligned_closes(series(a, range(1, 41)), series(b, range(1, 41))))
@check("window: a duplicate date -> ValueError")
def _(): ts = days(40); raises(ValueError, lambda: A.select_aligned_closes(series(ts + [ts[-1]], range(1, 42)), series(ts + [ts[-1]], range(1, 42))), "duplicate")
@check("window: rows given in random order give the same result as sorted rows")
def _():
    ts = days(40); rows = series(ts, range(1, 41)); shuffled = rows[:]; random.Random(4).shuffle(shuffled)
    assert A.select_aligned_closes(shuffled, rows) == A.select_aligned_closes(rows, rows)
@check("window: a gap OLDER than the window does not affect it (only the last 31 days are used)")
def _():
    ts = days(40); old_gap = ts[:5] + ts[6:]; assert len(A.select_aligned_closes(series(old_gap, range(39)[::1] if False else range(1, 40)), series(old_gap, range(1, 40)))[0]) == 31
for label, bad in (("zero", 0.0), ("negative", -5.0), ("NaN", float("nan")), ("infinity", float("inf"))):
    @check(f"window: a {label} closing price -> ValueError")
    def _(bad=bad):
        ts = days(31); closes = list(range(1, 32)); rows = series(ts, closes); rows[10]["close"] = bad
        raises(ValueError, lambda: A.select_aligned_closes(rows, series(ts, closes)), "positive")

# ------------------------------------------------------------------ returns
@check("returns: [100, 110, 99] -> [+0.10, -0.10]")
def _(): r = A.simple_returns([100, 110, 99]); assert near(r[0], 0.1) and near(r[1], -0.1) and len(r) == 2
@check("returns: 31 closes give 30 returns")
def _(): assert len(A.simple_returns(list(range(1, 32)))) == 30
@check("returns: a zero close -> ValueError")
def _(): raises(ValueError, lambda: A.simple_returns([100, 0, 50]))

# ------------------------------------------------------------------ correlation
X, Y = [1, 2, 3, 4, 5], [2, 4, 5, 4, 5]
@check("correlation: hand example Sxy=6, Sxx=10, Syy=6 -> 6/sqrt(60) = 0.7745966692")
def _(): assert near(A.pearson_correlation(X, Y), 6 / math.sqrt(60), 1e-10)
@check("correlation: perfect +1 and -1")
def _(): assert near(A.pearson_correlation([1, 2, 3], [2, 4, 6]), 1.0) and near(A.pearson_correlation([1, 2, 3], [6, 4, 2]), -1.0)
@check("correlation: a constant series -> None (undefined), not an exception")
def _(): assert A.pearson_correlation([1, 2, 3], [5, 5, 5]) is None and A.pearson_correlation([4, 4, 4], [1, 2, 3]) is None
@check("correlation: different lengths or fewer than 2 values -> ValueError")
def _(): raises(ValueError, lambda: A.pearson_correlation([1, 2], [1, 2, 3])); raises(ValueError, lambda: A.pearson_correlation([1], [1]))
@check("correlation components: n=5, means 3 and 4, Sxx=10, Syy=6, Sxy=6")
def _():
    c = A.correlation_components(X, Y)
    assert c["n"] == 5 and near(c["mean_x"], 3) and near(c["mean_y"], 4) and near(c["sxx"], 10) and near(c["syy"], 6) and near(c["sxy"], 6)

# ------------------------------------------------------------------ volatility
@check("volatility: sample standard deviation (n-1) of [2,4,4,4,5,5,7,9] = 2.1380899353, not the population value 2.0")
def _(): assert near(A.daily_volatility([2, 4, 4, 4, 5, 5, 7, 9]), math.sqrt(32 / 7), 1e-10) and not near(A.daily_volatility([2, 4, 4, 4, 5, 5, 7, 9]), 2.0)
@check("volatility: annualised = daily x sqrt(365) = daily x 19.1049731745")
def _(): assert near(A.annualised_volatility(0.02), 0.02 * 19.1049731745, 1e-9)
@check("volatility ratio: ETH/BTC, and None when BTC volatility is zero")
def _(): assert near(A.volatility_ratio(0.03, 0.02), 1.5) and A.volatility_ratio(0.03, 0.0) is None

# ------------------------------------------------------------------ downside measures
@check("negative days: counts only returns below zero (zero is not negative)")
def _(): assert A.count_negative_days([0.1, -0.2, 0.0, -0.01, -0.0]) == 2
@check("joint negative days: both below zero on the same day")
def _(): assert A.count_joint_negative_days([-1, -1, 1, 0], [-1, 1, -1, -1]) == 1
@check("worst day: the minimum return with its date (first one on a tie)")
def _(): assert A.worst_day(["d1", "d2", "d3", "d4"], [0.01, -0.05, -0.05, 0.02]) == (-0.05, "d2")
@check("max drawdown: [100,120,90,95,80,130] -> -33.33% from the 120 peak to the 80 trough, with those dates")
def _():
    worst, peak, trough = A.max_drawdown(list("abcdef"), [100, 120, 90, 95, 80, 130]); assert near(worst, -1 / 3, 1e-12) and (peak, trough) == ("b", "e")
@check("max drawdown: a steadily rising series has no drawdown (0.0, no dates)")
def _(): assert A.max_drawdown([1, 2, 3], [10, 11, 12]) == (0.0, None, None)
@check("max drawdown: a series that only falls -> from the first close to the last")
def _(): worst, peak, trough = A.max_drawdown(["a", "b", "c"], [100, 90, 80]); assert near(worst, -0.2) and (peak, trough) == ("a", "c")
@check("max drawdown: with two equal peaks before the trough, the EARLIER peak date is reported")
def _(): assert A.max_drawdown(list("abc"), [100, 100, 50]) == (-0.5, "a", "c")
@check("max drawdown: the first of two equal troughs is reported")
def _(): assert A.max_drawdown(list("abcde"), [100, 50, 100, 50, 100])[2] == "b"
@check("cumulative return: last close / first close - 1")
def _(): assert near(A.cumulative_return([100, 150, 125]), 0.25)

# ------------------------------------------------------------------ observation rules: labels and exact boundaries
for rho, label in ((0.0, "weak (no linear direction)"), (0.29999, "weak positive"), (0.30, "moderate positive"), (0.69999, "moderate positive"), (0.70, "strong positive"),
                   (1.0, "strong positive"), (-0.29999, "weak negative"), (-0.30, "moderate negative"), (-0.70, "strong negative"), (None, "not computable")):
    @check(f"R1 correlation {rho} -> {label}")
    def _(rho=rho, label=label): assert A.classify_correlation(rho) == label, A.classify_correlation(rho)
for ratio, label in ((1.25, "ETH noticeably more volatile than BTC"), (1.2499, "similar volatility (within about ±25%)"), (0.80, "ETH noticeably less volatile than BTC"),
                     (0.8001, "similar volatility (within about ±25%)"), (1.0, "similar volatility (within about ±25%)"), (None, "not computable")):
    @check(f"R2 volatility ratio {ratio} -> {label}")
    def _(ratio=ratio, label=label): assert A.classify_volatility_ratio(ratio) == label, A.classify_volatility_ratio(ratio)
for counts, label, share in (((8, 8, 6), "down days largely coincide", 0.75), ((100, 100, 74), "down days partly coincide", 0.74), ((8, 8, 5), "down days partly coincide", 0.625),
                             ((8, 8, 4), "down days often occur separately", 0.5), ((100, 100, 51), "down days partly coincide", 0.51), ((10, 4, 3), "down days largely coincide", 0.75),
                             ((0, 5, 0), "not assessable", None), ((5, 0, 0), "not assessable", None)):
    @check(f"R3 down-day overlap (BTC down {counts[0]}, ETH down {counts[1]}, both {counts[2]}) -> {label}")
    def _(counts=counts, label=label, share=share):
        got_label, got_share = A.classify_down_day_overlap(*counts); assert got_label == label and (got_share == share or (share is not None and near(got_share, share))), (got_label, got_share)
@check("thresholds: the named constants are exactly the approved values")
def _():
    assert (A.CORRELATION_WEAK_BELOW, A.CORRELATION_STRONG_FROM, A.VOLATILITY_RATIO_HIGH, A.VOLATILITY_RATIO_LOW, A.DOWN_DAY_OVERLAP_HIGH, A.DOWN_DAY_OVERLAP_LOW) == (0.30, 0.70, 1.25, 0.80, 0.75, 0.50)
    assert (A.RETURN_COUNT, A.CLOSE_COUNT, A.ANNUALISATION_DAYS) == (30, 31, 365)

# ------------------------------------------------------------------ the whole calculation against an independent one (60-digit Decimal)
@check("analyse: refuses anything but exactly 31 closes")
def _(): ts = days(32); raises(ValueError, lambda: A.analyse(ts, list(range(1, 33)), list(range(1, 33))))
@check("analyse vs independent calculation: 200 random 31-close windows agree to 1e-12 (correlation, volatilities, drawdowns, counts, cumulative)")
def _():
    rng = random.Random(99); ts = days(31)
    for trial in range(200):
        btc = random_closes(31, rng.randrange(10**6)); eth = random_closes(31, rng.randrange(10**6), correlated_with=btc)
        result = A.analyse(ts, btc, eth); rb, re_ = ind.returns(btc), ind.returns(eth)
        assert near(result["correlation"], float(ind.correlation(rb, re_))), trial
        assert near(result["volatility"]["BTC"]["daily"], float(ind.sample_std(rb))) and near(result["volatility"]["ETH"]["daily"], float(ind.sample_std(re_)))
        assert near(result["max_drawdown"]["BTC"][0], float(ind.max_drawdown(btc))) and near(result["max_drawdown"]["ETH"][0], float(ind.max_drawdown(eth)))
        assert result["negative_days"] == {"BTC": ind.negative_days(rb), "ETH": ind.negative_days(re_)} and result["joint_negative_days"] == ind.joint_negative_days(rb, re_)
        assert near(result["cumulative_return"]["BTC"], float(ind.cumulative(btc)))
        assert result["return_dates"] == ts[1:] and len(result["returns"]["BTC"]) == 30
@check("analyse: a constant ETH series -> correlation None, R1 'not computable', volatility ratio computed from zero ETH volatility")
def _():
    ts = days(31); result = A.analyse(ts, random_closes(31, 3), [3000.0] * 31)
    assert result["correlation"] is None and result["rules"]["R1"] == "not computable" and result["volatility"]["ETH"]["daily"] == 0.0
@check("analyse: no confidence interval is produced")
def _(): ts = days(31); assert not any("confidence" in key.lower() or "interval" in key.lower() for key in A.analyse(ts, random_closes(31, 3), random_closes(31, 4)))
R.finish()
