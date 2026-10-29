"""Build a standalone HTML dashboard from an existing report CSV.

Usage: python src/dashboard.py [--reports-dir DIR] [--out-dir DIR]

It reads the newest reports/report_<date>_data.csv (the 17-column file written by run_workflow.py) and the matching report_<date>.md,
checks both, calculates the 7, 14 and 30-day metrics with the existing analysis.py functions, and writes reports/dashboard_<date>.html.
The page contains its own data and a local copy of Plotly.js (src/vendor), so it works offline. No network access is used.
Exit code 0: the dashboard was written. Exit code 1: something was wrong with the input or the output, and nothing was written.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import analysis

SRC_DIR = Path(__file__).resolve().parent
DEFAULT_REPORTS_DIR = SRC_DIR.parent / "reports"
TEMPLATE_PATH = SRC_DIR / "dashboard_template.html"
PLOTLY_PATH = SRC_DIR / "vendor" / "plotly.min.js"
CSP_PLACEHOLDER = "__CSP__"
# The page may run only its own two scripts, identified by the SHA-256 of their exact text (computed below, never typed in), and may not connect anywhere. Plotly writes its own style
# sheets at run time, so inline styles stay allowed; blob: images are needed for the PNG export. No eval, no data: images, no other source.
POLICY_FORMAT = ("default-src 'none'; script-src {scripts}; style-src 'unsafe-inline'; img-src blob:; connect-src 'none'; "
                 "font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
SCRIPT_ELEMENT = re.compile(r"<script((?:\s[^>]*)?)>([\s\S]*?)</script>")
EXPECTED_SCRIPT_ATTRIBUTES = ["id=\"dashboard-data\" type=\"application/json\"", "id=\"plotly-js\"", ""]   # the verified order: data, Plotly, page logic
IS_WINDOWS = os.name == "nt"
PLOTLY_SHA256 = "3b6e15d45dbb7fca5bd2094291e961ddc5472cd887009e6009a56dab668d721f"  # the approved plotly.js 4.1.1 file, see src/vendor/PLOTLY_LICENSE.txt

EXPECTED_COLUMNS = [
    "date", "btc_close", "eth_close", "btc_return", "eth_return",
    "btc_open", "btc_high", "btc_low", "btc_volume", "btc_vwap", "btc_trade_count",
    "eth_open", "eth_high", "eth_low", "eth_volume", "eth_vwap", "eth_trade_count",
]
PRICE_COLUMNS = [1, 2, 5, 6, 7, 9, 11, 12, 13, 15]  # must be finite and above zero
VOLUME_COLUMNS = [8, 14]  # must be finite and not negative
TRADE_COUNT_COLUMNS = [10, 16]  # must be whole numbers, not negative
WINDOWS = (7, 14, 30)  # daily returns; each needs one more closing price than returns
RETURN_TOLERANCE = 1e-12  # a stored return must equal close / previous close - 1 to this accuracy
CSV_NAME = re.compile(r"^report_(\d{4}-\d{2}-\d{2})_data\.csv$")
DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMBER_TEXT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
INTEGER_TEXT = re.compile(r"^\d+$")
FETCHED_LINE = re.compile(r"^- Data fetched: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC$", re.M)
WINDOW_LINE = re.compile(r"^- Return dates: (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}) \((\d+) observations from (\d+) daily closes, (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})\)$", re.M)
SOURCE_LINE = "- Source: Kraken public OHLC endpoint (https://api.kraken.com/0/public/OHLC)"
ENDPOINT = "https://api.kraken.com/0/public/OHLC"
PAIRS_LINE = re.compile(r"^- Source: .*pairs XBTUSD and ETHUSD$", re.M)
ASSETS = ("BTC", "ETH")
# Input limits. The real files are 31 data rows, 5,539 bytes (CSV) and 6,514 bytes (report). 400 rows is about a year of daily candles and below the 720 candles
# that one Kraken OHLC request returns; 400 rows at about 180 bytes is 72 KB, so 256 KiB leaves room for longer numbers. The report has a fixed layout (no per-row text).
MAX_DATA_ROWS = 400
MAX_CSV_BYTES = 256 * 1024
MAX_REPORT_BYTES = 64 * 1024


class DashboardInputError(ValueError):
    """The input files or the output location are not usable. The message says why."""


# ---------------------------------------------------------------- reading and checking the CSV
def read_text_limited(path, limit, label):
    """Read a UTF-8 text file without ever holding more than limit + 1 bytes: refuse a file over the limit instead of reading or truncating it."""
    too_large = DashboardInputError(f"the {label} is larger than the limit of {limit} bytes and will not be read")
    with open(path, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > limit:
            raise too_large
        data = handle.read(limit + 1)  # one byte more than allowed: the file may have grown since its size was checked
    if len(data) > limit:
        raise too_large
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise DashboardInputError(f"the {label} is not valid UTF-8 text")


def read_records(csv_text):
    """The CSV records without trailing blank lines. Stops as soon as there are more than MAX_DATA_ROWS data rows; never truncates."""
    records, blank_lines = [], 0
    try:
        for record in csv.reader(io.StringIO(csv_text.replace("\r\n", "\n"), newline="")):
            if not record:
                blank_lines += 1
                continue
            records.extend([[]] * blank_lines)
            blank_lines = 0
            records.append(record)
            if len(records) > MAX_DATA_ROWS + 1:
                raise DashboardInputError(f"the CSV has more than {MAX_DATA_ROWS} data rows, which is the limit; it will not be truncated")
    except csv.Error as error:
        raise DashboardInputError(f"the CSV could not be read: {error}")
    return records



def _column_number(value, column, row_number, positive):
    if not NUMBER_TEXT.match(value):
        raise DashboardInputError(f"row {row_number}: {EXPECTED_COLUMNS[column]} is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise DashboardInputError(f"row {row_number}: {EXPECTED_COLUMNS[column]} is not finite: {value!r}")
    if positive and number <= 0:
        raise DashboardInputError(f"row {row_number}: {EXPECTED_COLUMNS[column]} must be above zero: {value!r}")
    if not positive and number < 0:
        raise DashboardInputError(f"row {row_number}: {EXPECTED_COLUMNS[column]} must not be negative: {value!r}")
    return number


def parse_dataset(csv_text):
    """Check a 17-column report CSV completely and return its rows and numbers. Raises DashboardInputError on any problem."""
    if not csv_text.strip():
        raise DashboardInputError("the CSV file is empty")
    rows = read_records(csv_text)
    if rows[0] != EXPECTED_COLUMNS:
        raise DashboardInputError(f"the CSV header is not the expected 17-column header (found {len(rows[0])} columns: {','.join(rows[0])[:120]})")
    body = rows[1:]
    if len(body) < WINDOWS[0] + 1:
        raise DashboardInputError(f"the CSV needs at least {WINDOWS[0] + 1} data rows, found {len(body)}")

    dates, closes, returns = [], {"BTC": [], "ETH": []}, {"BTC": [], "ETH": []}
    for number, row in enumerate(body, start=1):
        if len(row) != len(EXPECTED_COLUMNS):
            raise DashboardInputError(f"row {number} has {len(row)} fields, expected {len(EXPECTED_COLUMNS)}")
        if not DATE_TEXT.match(row[0]):
            raise DashboardInputError(f"row {number}: invalid date {row[0]!r}")
        try:
            day = date.fromisoformat(row[0])
        except ValueError:
            raise DashboardInputError(f"row {number}: invalid date {row[0]!r}")
        if dates and day != date.fromisoformat(dates[-1]) + timedelta(days=1):
            raise DashboardInputError(f"row {number}: the date {row[0]} does not follow {dates[-1]} (duplicate, gap or wrong order)")
        dates.append(row[0])
        for column in PRICE_COLUMNS:
            _column_number(row[column], column, number, positive=True)
        for column in VOLUME_COLUMNS:
            _column_number(row[column], column, number, positive=False)
        for column in TRADE_COUNT_COLUMNS:
            if not INTEGER_TEXT.match(row[column]):
                raise DashboardInputError(f"row {number}: {EXPECTED_COLUMNS[column]} must be a whole number, not negative: {row[column]!r}")
        closes["BTC"].append(float(row[1]))
        closes["ETH"].append(float(row[2]))
        for asset, column in (("BTC", 3), ("ETH", 4)):
            if number == 1:
                if row[column] != "":
                    raise DashboardInputError(f"row 1: {EXPECTED_COLUMNS[column]} must be empty (the first day has no previous day)")
                returns[asset].append(None)
                continue
            if not NUMBER_TEXT.match(row[column]) or not math.isfinite(float(row[column])):
                raise DashboardInputError(f"row {number}: {EXPECTED_COLUMNS[column]} is missing or not a finite number: {row[column]!r}")
            value = float(row[column])
            expected = closes[asset][-1] / closes[asset][-2] - 1
            if abs(value - expected) > RETURN_TOLERANCE:
                raise DashboardInputError(f"row {number}: {EXPECTED_COLUMNS[column]} {row[column]} does not equal close / previous close - 1 ({expected!r})")
            returns[asset].append(value)
    return {"rows": body, "dates": dates, "closes": closes, "returns": returns}


# ---------------------------------------------------------------- metrics (the existing analysis.py definitions, applied to a window)
def available_windows(dataset):
    return [k for k in WINDOWS if k + 1 <= len(dataset["rows"])]


def window_slice(dataset, k):
    """The last k+1 rows: k daily returns plus the closing price they start from."""
    if k not in WINDOWS or k + 1 > len(dataset["rows"]):
        raise DashboardInputError(f"a {k}-day window is not available in this file")
    return {
        "dates": dataset["dates"][-(k + 1):],
        "return_dates": dataset["dates"][-k:],
        "closes": {asset: dataset["closes"][asset][-(k + 1):] for asset in ASSETS},
        "returns": {asset: dataset["returns"][asset][-k:] for asset in ASSETS},
    }


def window_metrics(dataset, k):
    window = window_slice(dataset, k)
    btc_returns, eth_returns = window["returns"]["BTC"], window["returns"]["ETH"]
    correlation = analysis.pearson_correlation(btc_returns, eth_returns)
    daily = {"BTC": analysis.daily_volatility(btc_returns), "ETH": analysis.daily_volatility(eth_returns)}
    ratio = analysis.volatility_ratio(daily["ETH"], daily["BTC"])
    negative = {"BTC": analysis.count_negative_days(btc_returns), "ETH": analysis.count_negative_days(eth_returns)}
    joint = analysis.count_joint_negative_days(btc_returns, eth_returns)
    overlap_label, overlap_share = analysis.classify_down_day_overlap(negative["BTC"], negative["ETH"], joint)
    return {
        "correlation": correlation,
        "volatility": {asset: {"daily": daily[asset], "annualised": analysis.annualised_volatility(daily[asset])} for asset in ASSETS},
        "volatility_ratio": ratio,
        "negative_days": negative,
        "joint_negative_days": joint,
        "down_day_overlap_share": overlap_share,
        "worst_day": {asset: analysis.worst_day(window["return_dates"], window["returns"][asset]) for asset in ASSETS},
        "max_drawdown": {asset: analysis.max_drawdown(window["dates"], window["closes"][asset]) for asset in ASSETS},
        "cumulative_return": {asset: analysis.cumulative_return(window["closes"][asset]) for asset in ASSETS},
        "rules": {"R1": analysis.classify_correlation(correlation), "R2": analysis.classify_volatility_ratio(ratio), "R3": overlap_label},
    }


def drawdown_series(closes):
    """For each day: how far the close is below the highest close so far in the window, in percent (0 or negative)."""
    peak = closes[0]
    series = []
    for close in closes:
        peak = max(peak, close)
        series.append((close / peak - 1) * 100)
    return series


def indexed_series(closes):
    """Closing prices rebased so that the first close of the window is 100."""
    return [close / closes[0] * 100 for close in closes]


# ---------------------------------------------------------------- text in the same format as the Markdown report
def format_percent(value, digits=2):
    return f"{value * 100:+.{digits}f}%"


def drawdown_text(drawdown):
    worst, peak, trough = drawdown
    if peak is None:
        return "0.00% (no fall from a peak)"
    return f"{worst * 100:.2f}% ({peak} to {trough})"


def window_display(dataset, k):
    m = window_metrics(dataset, k)
    rho, ratio, share = m["correlation"], m["volatility_ratio"], m["down_day_overlap_share"]
    display = {
        "correlation": "undefined (a series is constant)" if rho is None else f"{rho:.6f}",
        "ratio": "not computable (BTC volatility is zero)" if ratio is None else f"{ratio:.3f}",
        "joint": f"{m['joint_negative_days']} of {k}",
        "overlap": "not assessable (an asset had no down days)" if share is None else f"{share:.2f}",
    }
    for asset in ASSETS:
        display[f"daily_vol_{asset}"] = f"{m['volatility'][asset]['daily'] * 100:.4f}%"
        display[f"annualised_{asset}"] = f"{m['volatility'][asset]['annualised'] * 100:.2f}%"
        display[f"negative_{asset}"] = f"{m['negative_days'][asset]} of {k} ({m['negative_days'][asset] / k * 100:.1f}%)"
        value, day = m["worst_day"][asset]
        display[f"worst_{asset}"] = f"{format_percent(value)} on {day}"
        display[f"max_drawdown_{asset}"] = drawdown_text(m["max_drawdown"][asset])
        display[f"cumulative_{asset}"] = format_percent(m["cumulative_return"][asset])
    return display


def window_insights(dataset, k):
    m, d = window_metrics(dataset, k), window_display(dataset, k)
    window = window_slice(dataset, k)
    worse = "deeper than" if m["max_drawdown"]["ETH"][0] < m["max_drawdown"]["BTC"][0] else "shallower than" if m["max_drawdown"]["ETH"][0] > m["max_drawdown"]["BTC"][0] else "as deep as"
    insights = [
        f"Over the last {k} daily returns ({window['return_dates'][0]} to {window['return_dates'][-1]}), Bitcoin and Ethereum showed a {m['rules']['R1']} co-movement (correlation {d['correlation']}).",
        f"The ETH / BTC daily volatility ratio was {d['ratio']}: {m['rules']['R2']}.",
        f"Both assets fell on {d['joint']} days ({m['rules']['R3']}; overlap share {d['overlap']}).",
        f"Downside: Ethereum's worst day was {d['worst_ETH']} and Bitcoin's was {d['worst_BTC']}; Ethereum's maximum drawdown ({d['max_drawdown_ETH'].split(' ')[0]}) was {worse} Bitcoin's ({d['max_drawdown_BTC'].split(' ')[0]}).",
    ]
    if k < 30:
        insights.append(f"A {k}-day window has few observations, and the observation rules were fixed for 30; read these labels as descriptions of this short window only.")
    return insights


# ---------------------------------------------------------------- the Markdown report: facts and consistency
def read_report_facts(report_text):
    fetched = FETCHED_LINE.search(report_text)
    window = WINDOW_LINE.search(report_text)
    if not fetched:
        raise DashboardInputError("the Markdown report has no valid 'Data fetched: YYYY-MM-DD HH:MM:SS UTC' line")
    if not window:
        raise DashboardInputError("the Markdown report has no valid 'Return dates:' line")
    return {
        "data_fetched": f"{fetched.group(1)} UTC",
        "return_start": window.group(1), "return_end": window.group(2), "close_start": window.group(5), "close_end": window.group(6),
        "source_confirmed": SOURCE_LINE in report_text,
        "pairs_confirmed": SOURCE_LINE in report_text and PAIRS_LINE.search(report_text) is not None,
    }


def check_against_report(dataset, report_text):
    """The dashboard must not contradict the Markdown report: same window, same 30-day figures, same rule labels."""
    facts = read_report_facts(report_text)
    dates = dataset["dates"]
    if 30 not in available_windows(dataset):
        raise DashboardInputError("the report describes 30 daily returns but the CSV has fewer than 31 rows; they cannot be compared")
    if (facts["close_start"], facts["close_end"], facts["return_start"], facts["return_end"]) != (dates[-31], dates[-1], dates[-30], dates[-1]):
        raise DashboardInputError(f"the report's period ({facts['close_start']} to {facts['close_end']}) does not match the CSV ({dates[-31]} to {dates[-1]})")
    display, metrics = window_display(dataset, 30), window_metrics(dataset, 30)
    expected = {
        "correlation": f"- Pearson correlation of the 30 aligned daily returns: {display['correlation']}",
        "ratio": f"- ETH / BTC daily volatility ratio: {display['ratio']}",
        "shared days": f"- Days on which both assets fell: {display['joint']}; overlap share: {display['overlap']}",
    }
    for asset in ASSETS:
        for key in (f"daily_vol_{asset}", f"annualised_{asset}", f"negative_{asset}", f"worst_{asset}", f"max_drawdown_{asset}", f"cumulative_{asset}"):
            expected[key] = display[key]
    for name, text in expected.items():
        if text not in report_text:
            raise DashboardInputError(f"the report disagrees with the CSV on {name} (expected {text!r} in the report)")
    for label in metrics["rules"].values():
        if label not in report_text:
            raise DashboardInputError(f"the report does not contain the rule label {label!r} calculated from the CSV")
    return facts


# ---------------------------------------------------------------- the page
def json_for_script(value):
    """JSON that is safe inside a <script> element: no raw <, > or &, and no line-separator characters."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_payload(dataset, facts, generated_at, csv_name, report_name):
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    dates = dataset["dates"]
    if not facts["source_confirmed"]:
        source = "Data source not confirmed by the report"
    elif facts["pairs_confirmed"]:
        source = f"Kraken public OHLC API ({ENDPOINT}), daily candles, pairs XBTUSD and ETHUSD"
    else:
        source = f"Kraken public OHLC API ({ENDPOINT}), daily candles"
    windows = {}
    for k in available_windows(dataset):
        window = window_slice(dataset, k)
        metrics = window_metrics(dataset, k)
        windows[str(k)] = {
            "returns": k, "dates": window["dates"], "return_dates": window["return_dates"],
            "index": {asset: indexed_series(window["closes"][asset]) for asset in ASSETS},
            "close_usd": window["closes"],
            "return_pct": {asset: [value * 100 for value in window["returns"][asset]] for asset in ASSETS},
            "drawdown_pct": {asset: drawdown_series(window["closes"][asset]) for asset in ASSETS},
            "metrics": json.loads(json.dumps(metrics)),
            "display": window_display(dataset, k), "rules": metrics["rules"], "insights": window_insights(dataset, k),
        }
    return {
        "meta": {
            "title": "Bitcoin vs. Ethereum", "subtitle": "30-Day Market Analysis Dashboard", "source": source,
            "close_period": {"start": dates[0], "end": dates[-1]}, "return_period": {"start": dates[1], "end": dates[-1]},
            "generated_at": stamp, "data_fetched": facts["data_fetched"], "csv_file": csv_name, "report_file": report_name,
            "endpoint": ENDPOINT if facts["source_confirmed"] else None,
            "pairs": ["XBTUSD", "ETHUSD"] if facts["pairs_confirmed"] else None,
        },
        "columns": list(EXPECTED_COLUMNS),
        "rows": dataset["rows"],
        "windows": windows,
    }


def _sha256_source(script_text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(script_text.encode("utf-8")).digest()).decode("ascii") + "'"


def content_security_policy(html):
    """The policy for a page whose scripts are in place: the two hashes are those of the exact final Plotly text and page-logic text."""
    elements = SCRIPT_ELEMENT.findall(html)
    if [attributes.strip() for attributes, _text in elements] != EXPECTED_SCRIPT_ATTRIBUTES:
        raise DashboardInputError("the dashboard template must contain exactly the three expected script elements (data, Plotly, page logic) in that order")
    plotly_text, logic_text = elements[1][1], elements[2][1]
    if "\r" in plotly_text or "\r" in logic_text:  # a browser normalises line endings before hashing, so a hash of text with a carriage return would never match
        raise DashboardInputError("a script contains a carriage return and cannot be covered by a script hash")
    return POLICY_FORMAT.format(scripts=_sha256_source(plotly_text) + " " + _sha256_source(logic_text))


def render_html(payload):
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    plotly_bytes = PLOTLY_PATH.read_bytes()
    if hashlib.sha256(plotly_bytes).hexdigest() != PLOTLY_SHA256:
        raise DashboardInputError("the vendored Plotly.js file does not match the approved SHA-256 and will not be embedded")
    plotly = plotly_bytes.decode("utf-8")
    if "</script" in plotly.lower():
        raise DashboardInputError("the vendored Plotly.js file contains a closing script tag and cannot be embedded")
    for placeholder in ("__DATA_JSON__", "__PLOTLY_JS__", CSP_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise DashboardInputError(f"the dashboard template must contain {placeholder} exactly once")
    before_data, rest = template.split("__DATA_JSON__")
    before_plotly, after_plotly = rest.split("__PLOTLY_JS__")
    html = before_data + json_for_script(payload) + before_plotly + plotly + after_plotly
    if html.count(CSP_PLACEHOLDER) != 1:
        raise DashboardInputError(f"the dashboard template must contain {CSP_PLACEHOLDER} exactly once (it also appeared in the data or the Plotly file)")
    policy = content_security_policy(html)
    html = html.replace(CSP_PLACEHOLDER, policy, 1)
    if content_security_policy(html) != policy:  # the hashes must still describe the final bytes
        raise DashboardInputError("the script text changed while the policy was inserted")
    return html


# ---------------------------------------------------------------- finding the input, writing the output
def find_latest_csv(reports_dir):
    candidates = sorted((m.group(1), name) for name in os.listdir(reports_dir) for m in [CSV_NAME.match(name)] if m)
    if not candidates:
        raise DashboardInputError(f"no report_<date>_data.csv file found in {reports_dir}")
    return Path(reports_dir) / candidates[-1][1], candidates[-1][0]


def _publish_without_replacing(temporary, target):
    """Give the finished temporary file its final name in one step that fails with FileExistsError if the name is already taken."""
    if IS_WINDOWS:
        os.rename(temporary, target)  # on Windows, rename refuses to replace an existing file (os.replace would replace it)
    else:
        os.link(temporary, target)  # on Linux and macOS a hard link also refuses an existing name (rename would replace it)
        try:
            os.unlink(temporary)
        except OSError:
            pass  # the dashboard is complete under its final name; a leftover temporary name is harmless


def _remove_quietly(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def generate(reports_dir, generated_at=None, out_dir=None):
    """Write dashboard_<date>.html for the newest CSV. Never overwrites an existing file. Returns the path written."""
    reports_dir = Path(reports_dir)
    if not reports_dir.is_dir():
        raise DashboardInputError(f"the reports folder does not exist: {reports_dir}")
    csv_path, csv_date = find_latest_csv(reports_dir)
    dataset = parse_dataset(read_text_limited(csv_path, MAX_CSV_BYTES, "CSV file"))
    if dataset["dates"][-1] != csv_date:
        raise DashboardInputError(f"the file name says {csv_date} but the last date in the CSV is {dataset['dates'][-1]}")
    report_path = reports_dir / f"report_{csv_date}.md"
    if not report_path.is_file():
        raise DashboardInputError(f"the matching Markdown report is missing: {report_path.name}")
    facts = check_against_report(dataset, read_text_limited(report_path, MAX_REPORT_BYTES, "Markdown report"))
    payload = build_payload(dataset, facts, generated_at or datetime.now(timezone.utc), csv_path.name, report_path.name)
    html = render_html(payload)

    target = Path(out_dir or reports_dir) / f"dashboard_{csv_date}.html"
    if target.exists():
        raise DashboardInputError(f"{target.name} already exists and will not be overwritten")
    try:  # a uniquely named file that this attempt creates exclusively and therefore owns; it sits next to the target so the final rename stays atomic
        descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    except OSError as error:
        raise DashboardInputError(f"could not create a temporary file for the dashboard: {error}")
    temporary = Path(temporary_name)
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(html)
        os.chmod(temporary, 0o644)  # mkstemp makes the file private to its owner; give the dashboard the usual permissions of an ordinary file
        _publish_without_replacing(temporary, target)
    except FileExistsError:
        _remove_quietly(temporary)
        raise DashboardInputError(f"{target.name} already exists and will not be overwritten")
    except OSError as error:
        _remove_quietly(temporary)
        raise DashboardInputError(f"could not write the dashboard: {error}")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the HTML dashboard from the newest report CSV.")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="folder holding report_<date>_data.csv and report_<date>.md")
    parser.add_argument("--out-dir", default=None, help="folder for the dashboard (default: the reports folder)")
    arguments = parser.parse_args(argv)
    try:
        path = generate(arguments.reports_dir, out_dir=arguments.out_dir)
    except (DashboardInputError, OSError) as error:
        print(f"ERROR: {error}. No dashboard was written.", file=sys.stderr)
        return 1
    print(f"Dashboard written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
