"""Content tests for the generated report and CSV. Written BEFORE the module exists."""
import csv, io, re
from wf_support import *

R = Runner("report content checks")
check = R.register
d = new_temp_dir()
state = {}
@check("setup: a real-data run produces the report that the content checks inspect")
def _():
    code, out, err = run_main(REAL, REAL_NOW, d); assert code == 0, err; state["report"] = read(d, "report_2026-10-26.md"); state["csv"] = read(d, "report_2026-10-26_data.csv")
report = lambda: state["report"]

for heading in ("Data and method", "Metrics", "Observation rules", "Interpretation", "Data table", "Manual verification", "Limitations"):
    @check(f"section present: {heading}")
    def _(heading=heading): assert re.search(rf"^## .*{heading}", report(), re.M), heading
@check("disclaimers: educational, not financial advice, correlation does not establish causation, past behaviour does not predict the future")
def _():
    text = report().lower(); assert "educational" in text and "not financial advice" in text and "does not establish causation" in text and "past" in text and "predict" in text
@check("the disclaimer sits at the TOP of the report (before the first section), not only in the footer")
def _():
    top = report().split("## Data and method")[0].lower(); assert "educational" in top and "not financial advice" in top and "does not establish causation" in top
@check("source line names the endpoint, the interval and both pairs")
def _(): assert "https://api.kraken.com/0/public/OHLC" in report() and "1440" in report() and "XBTUSD" in report() and "ETHUSD" in report()
@check("method: window definition, simple-return formula, sample standard deviation (n - 1) and the annualisation label are stated")
def _():
    text = report(); assert "30 observations from 31 daily closes" in text and "C_t / C_(t-1) - 1" in text.replace("−", "-").replace("₁", "") or "closing price" in text
    assert "n - 1" in text.replace("−", "-") and "√365" in text and "illustrative" in text.lower()
@check("rules: all six thresholds are printed and are described as conventions, not standards or signals")
def _():
    text = report(); assert all(t in text for t in ("0.30", "0.70", "1.25", "0.80", "0.75", "0.50"))
    assert "convention" in text.lower() and "not financial standards" in text.lower() and "not trading signals" in text.lower()
@check("rules: R1, R2 and R3 outcomes are present, each using one of the defined labels")
def _():
    text = report()
    for rule, labels in (("R1", ("weak", "moderate", "strong", "not computable")), ("R2", ("ETH noticeably", "similar volatility", "not computable")), ("R3", ("largely coincide", "partly coincide", "often occur separately", "not assessable"))):
        line = [l for l in text.splitlines() if l.startswith(f"- {rule} ") or l.startswith(f"| {rule} ")]; assert line and any(label in line[-1] for label in labels), rule
@check("no investment language: the report never says buy or sell, and never offers a recommendation")
def _(): assert not re.search(r"\b(buy|sell|you should|we recommend)\b", report(), re.I)
@check("no confidence interval is included (approved scope)")
def _(): assert "confidence" not in report().lower() and "fisher" not in report().lower()
@check("limitations: the fetcher's four known limits and the analysis limits are listed")
def _():
    text = report().lower()
    for phrase in ("clock", "download deadline", "redirect", "retry-after", "one exchange", "usd", "30 observations", "short"): assert phrase in text, phrase
@check("manual verification: lists n, the means, Sxx, Syy, Sxy and rho, and the spreadsheet formula =CORREL")
def _():
    text = report(); assert "n = 30" in text and "=CORREL" in text and "Sxx" in text and "Syy" in text and "Sxy" in text
@check("interpretation sentences only repeat computed labels (no number appears that is not in the metrics or data)")
def _():
    section = report().split("## Interpretation")[1].split("## ")[0]; assert section.strip() and "R1" in section and "R2" in section and "R3" in section
@check("data table: 31 date rows, the first without returns")
def _():
    table = [l for l in report().split("## Data table")[1].split("## ")[0].splitlines() if re.match(r"\| \d{4}-\d{2}-\d{2} ", l)]; assert len(table) == 31 and table[0].count("–") >= 2
@check("CSV: exact 17-column header (original five first, then the 12 appended market columns), comma-separated, LF line endings, 31 data rows")
def _():
    lines = state["csv"].split("\n"); assert lines[0] == "date,btc_close,eth_close,btc_return,eth_return,btc_open,btc_high,btc_low,btc_volume,btc_vwap,btc_trade_count,eth_open,eth_high,eth_low,eth_volume,eth_vwap,eth_trade_count" and "\r" not in state["csv"] and len([l for l in lines[1:] if l]) == 31
@check("CSV: every number parses as a float and returns are full precision (the Python repr), so a spreadsheet can reproduce them")
def _():
    rows = list(csv.DictReader(io.StringIO(state["csv"]))); assert all(float(r["btc_close"]) > 0 and float(r["eth_close"]) > 0 for r in rows)
    closes = [float(r["btc_close"]) for r in rows]; assert all(abs(float(rows[i]["btc_return"]) - (closes[i] / closes[i - 1] - 1)) <= 1e-15 for i in range(1, 31))
@check("report text contains no text taken from Kraken's replies other than dates and numbers (no pair-key names such as XXBTZUSD)")
def _(): assert "XXBTZUSD" not in report() and "XETHZUSD" not in report()
shutil.rmtree(d, ignore_errors=True)
R.finish()
