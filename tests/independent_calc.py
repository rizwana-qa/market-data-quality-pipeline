"""Independent re-calculation: different arithmetic from the program (60-digit Decimal, explicit loops, no statistics module, no float sums).
Used by the tests now and by the manual-verification step later."""
from decimal import Decimal, getcontext
getcontext().prec = 60

def dec(x): return Decimal(repr(float(x)))
def returns(closes):
    d = [dec(c) for c in closes]
    return [d[i] / d[i - 1] - 1 for i in range(1, len(d))]
def mean(values): return sum(values, Decimal(0)) / len(values)
def sums(x, y):
    mx, my = mean(x), mean(y)
    return {"n": len(x), "mean_x": mx, "mean_y": my, "sxx": sum(((a - mx) ** 2 for a in x), Decimal(0)),
            "syy": sum(((b - my) ** 2 for b in y), Decimal(0)), "sxy": sum(((a - mx) * (b - my) for a, b in zip(x, y)), Decimal(0))}
def correlation(x, y):
    s = sums(x, y); return s["sxy"] / (s["sxx"] * s["syy"]).sqrt()
def sample_std(values):
    m = mean(values); return (sum(((a - m) ** 2 for a in values), Decimal(0)) / (len(values) - 1)).sqrt()
def max_drawdown(closes):
    c = [dec(v) for v in closes]; running, worst = c[0], Decimal(0)
    for value in c:
        running = max(running, value); worst = min(worst, value / running - 1)
    return worst
def negative_days(r): return sum(1 for a in r if a < 0)
def joint_negative_days(x, y): return sum(1 for a, b in zip(x, y) if a < 0 and b < 0)
def cumulative(closes): return dec(closes[-1]) / dec(closes[0]) - 1
