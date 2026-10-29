"""Run the whole workflow: fetch, validate, analyse and write the report.

Usage: python src/run_workflow.py

Writes reports/report_<last completed UTC date>.md and reports/report_<date>_data.csv (17 columns: date, both closes and
returns, then open, high, low, volume, VWAP and trade count for Bitcoin and for Ethereum; returns are fractions, 0.01 = 1%).
Exit code 0: both files were written (an old copy that could not be deleted, *.bak, only causes a warning).
Exit code 1: something failed and the old files were put back; if putting them back also fails, the old copies stay as *.bak files.
"""

import csv
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
SOURCE_URL = "https://api.kraken.com/0/public/OHLC"
CSV_MARKET_FIELDS = ["open", "high", "low", "volume", "vwap", "trade_count"]  # appended after the original five CSV columns, once per asset


def percent(value, digits=2):
    return f"{value * 100:+.{digits}f}%"


def drawdown_text(drawdown):
    worst, peak, trough = drawdown
    if peak is None:
        return "0.00% (no fall from a peak)"
    return f"{worst * 100:.2f}% ({format_utc_date(peak)} to {format_utc_date(trough)})"


def build_report(results, fetched_at, excluded_dates):
    timestamps, return_dates = results["timestamps"], results["return_dates"]
    first_close, last = format_utc_date(timestamps[0]), format_utc_date(timestamps[-1])
    first_return = format_utc_date(return_dates[0])
    n = analysis.RETURN_COUNT
    btc, eth = results["returns"]["BTC"], results["returns"]["ETH"]
    vol, parts = results["volatility"], results["correlation_components"]
    rho, ratio, share = results["correlation"], results["volatility_ratio"], results["down_day_overlap_share"]
    rho_text = "undefined (a series is constant)" if rho is None else f"{rho:.6f}"
    ratio_text = "not computable (BTC volatility is zero)" if ratio is None else f"{ratio:.3f}"
    share_text = "not assessable (an asset had no down days)" if share is None else f"{share:.2f}"

    lines = [
        "# Bitcoin vs Ethereum: how did daily returns move together over the last 30 days?",
        "",
        "> This report is educational, not financial advice. A correlation does not establish causation, and past behaviour does not predict the future.",
        "",
        "## Data and method",
        "",
        f"- Source: Kraken public OHLC endpoint ({SOURCE_URL}), interval 1440 minutes (one candle per UTC day), pairs XBTUSD and ETHUSD",
        f"- Data fetched: {fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"- Return dates: {first_return} to {last} ({n} observations from {analysis.CLOSE_COUNT} daily closes, {first_close} to {last})",
        f"- Unfinished candle excluded: {', '.join(excluded_dates) if excluded_dates else 'none'}",
        "- Both assets were validated and aligned by date; the fetch and validation checks passed before any calculation",
        "- Price used: each day's closing price in USD (the last trade of that UTC day), not the volume-weighted price",
        "- Daily simple return: r_t = C_t / C_(t-1) - 1, dated by the later day",
        "- Correlation: Pearson correlation of the 30 aligned daily returns",
        "- Daily volatility: sample standard deviation of the 30 daily returns (divides by n - 1)",
        f"- Annualised volatility: daily volatility × √{analysis.ANNUALISATION_DAYS} (illustrative; assumes independent daily returns)",
        "- Maximum drawdown: the largest fall of the closing price from its running peak within the 31 closes",
        "",
        "## Metrics",
        "",
        "| Metric | BTC | ETH |",
        "|---|---|---|",
        f"| Daily volatility (sample standard deviation) | {vol['BTC']['daily'] * 100:.4f}% | {vol['ETH']['daily'] * 100:.4f}% |",
        f"| Annualised volatility (daily × √365, illustrative) | {vol['BTC']['annualised'] * 100:.2f}% | {vol['ETH']['annualised'] * 100:.2f}% |",
        f"| Negative-return days | {results['negative_days']['BTC']} of {n} ({results['negative_days']['BTC'] / n * 100:.1f}%) | {results['negative_days']['ETH']} of {n} ({results['negative_days']['ETH'] / n * 100:.1f}%) |",
        f"| Worst daily return | {percent(results['worst_day']['BTC'][0])} on {format_utc_date(results['worst_day']['BTC'][1])} | {percent(results['worst_day']['ETH'][0])} on {format_utc_date(results['worst_day']['ETH'][1])} |",
        f"| Maximum drawdown (31 closes) | {drawdown_text(results['max_drawdown']['BTC'])} | {drawdown_text(results['max_drawdown']['ETH'])} |",
        f"| Cumulative return over the window (context) | {percent(results['cumulative_return']['BTC'])} | {percent(results['cumulative_return']['ETH'])} |",
        "",
        f"- Pearson correlation of the {n} aligned daily returns: {rho_text}",
        f"- ETH / BTC daily volatility ratio: {ratio_text}",
        f"- Days on which both assets fell: {results['joint_negative_days']} of {n}; overlap share: {share_text}",
        "",
        "## Observation rules",
        "",
        "These rules are descriptive conventions chosen before any result was seen. They are not financial standards and not trading signals, and they describe this 30-day sample only.",
        "",
        "| Rule | Thresholds | Outcome |",
        "|---|---|---|",
        f"| R1 co-movement | size of the correlation: below {analysis.CORRELATION_WEAK_BELOW:.2f}; from {analysis.CORRELATION_WEAK_BELOW:.2f} up to {analysis.CORRELATION_STRONG_FROM:.2f}; {analysis.CORRELATION_STRONG_FROM:.2f} or more | {results['rules']['R1']} |",
        f"| R2 relative volatility | ETH / BTC daily volatility: {analysis.VOLATILITY_RATIO_HIGH:.2f} or more; {analysis.VOLATILITY_RATIO_LOW:.2f} or less; otherwise | {results['rules']['R2']} |",
        f"| R3 down-day overlap | share of the down days of the asset with fewer of them that were also down days for the other: {analysis.DOWN_DAY_OVERLAP_HIGH:.2f} or more; {analysis.DOWN_DAY_OVERLAP_LOW:.2f} or less; otherwise | {results['rules']['R3']} |",
        "",
        "## Interpretation",
        "",
        f"- R1: the correlation of daily returns was {rho_text}, which the rule describes as: {results['rules']['R1']}.",
        f"- R2: the ETH / BTC daily volatility ratio was {ratio_text}, which the rule describes as: {results['rules']['R2']}.",
        f"- R3: both assets fell on {results['joint_negative_days']} of {n} days (overlap share {share_text}), which the rule describes as: {results['rules']['R3']}.",
        "- These labels describe this sample of 30 days only. They say nothing about why prices moved or about future prices.",
        "",
        "## Data table",
        "",
        "Closing prices in USD; returns as percentages (the CSV holds them as fractions, 0.012345 = +1.2345%).",
        "",
        "| Date | BTC close | ETH close | BTC return | ETH return |",
        "|---|---|---|---|---|",
    ]
    for index, timestamp in enumerate(timestamps):
        if index == 0:
            btc_cell = eth_cell = "–"  # the first close has no previous day, so no return
        else:
            btc_cell, eth_cell = percent(btc[index - 1], 4), percent(eth[index - 1], 4)
        lines.append(
            f"| {format_utc_date(timestamp)} | {results['closes']['BTC'][index]:.2f} | {results['closes']['ETH'][index]:.2f} | "
            f"{btc_cell} | {eth_cell} |"
        )
    lines += [
        "",
        "## Manual verification",
        "",
        "Check the correlation yourself. The 30 BTC and 30 ETH returns are in the CSV file next to this report.",
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
        "## Limitations",
        "",
        "- Only 30 observations from one exchange (Kraken) and USD-quoted prices: a short sample that may not be representative.",
        "- The thresholds are conventions, and the annualisation assumes independent daily returns.",
        "- The data fetcher excludes the unfinished day using the computer's own UTC clock. A wrong clock could make an unfinished candle count as complete, or stop the run.",
        "- The fetcher has no overall download deadline: a very slow server could make a run take a long time.",
        "- The fetcher follows redirects, and it ignores any Retry-After instruction when retrying.",
        "- These limits were identified during development and deliberately left unchanged.",
        "",
        "> Educational use only. Not financial advice.",
        "",
    ]
    return "\n".join(lines)


def candles_for_dates(candles, timestamps):
    """The candles for exactly these dates, in the order of the dates. A missing date is an error: nothing is skipped or repaired."""
    candle_by_date = {candle["timestamp"]: candle for candle in candles}
    for timestamp in timestamps:
        if timestamp not in candle_by_date:
            raise ValueError(f"no candle for {format_utc_date(timestamp)}")
    return [candle_by_date[timestamp] for timestamp in timestamps]


def market_columns(candle):
    """The six market fields of one candle, in CSV order (the close is already in an earlier column)."""
    return [repr(candle["open"]), repr(candle["high"]), repr(candle["low"]), repr(candle["volume"]), repr(candle["vwap"]), str(candle["trade_count"])]


def build_csv(results, btc_candles, eth_candles):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "btc_close", "eth_close", "btc_return", "eth_return"]
                    + [f"btc_{field}" for field in CSV_MARKET_FIELDS] + [f"eth_{field}" for field in CSV_MARKET_FIELDS])
    for index, timestamp in enumerate(results["timestamps"]):
        btc_return = "" if index == 0 else repr(results["returns"]["BTC"][index - 1])
        eth_return = "" if index == 0 else repr(results["returns"]["ETH"][index - 1])
        writer.writerow([format_utc_date(timestamp), repr(results["closes"]["BTC"][index]), repr(results["closes"]["ETH"][index]), btc_return, eth_return]
                        + market_columns(btc_candles[index]) + market_columns(eth_candles[index]))
    return buffer.getvalue()


def replace_with_rollback(staged):
    """Move each finished temporary file over its final name; if anything fails, put every old file back.

    Returns the old copies (.bak) that could not be deleted afterwards. That is not a failure: the new files are already in place.
    """
    done = []
    try:
        for temporary, final in staged:
            backup = None
            if final.exists():
                backup = final.with_name(final.name + ".bak")
                os.replace(final, backup)
            done.append((final, backup))
            os.replace(temporary, final)
    except OSError as error:
        stuck = []
        for final, backup in reversed(done):
            try:
                if backup is not None:
                    os.replace(backup, final)
                else:
                    final.unlink(missing_ok=True)
            except OSError:
                stuck.append(backup if backup is not None else final)
        if stuck:
            raise OSError(f"{error}; restoring the old files also failed for: {', '.join(str(path) for path in stuck)} "
                          "(old copies are the .bak files in the report folder)") from error
        raise
    leftovers = []
    for _, backup in done:
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                leftovers.append(backup)
    return leftovers


def write_outputs(report_dir, files):
    """Write every (file name, text) pair. Real files are replaced only after all temporary files were written.

    Returns the old copies that could not be deleted (see replace_with_rollback).
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, text in files:
            temporary = report_dir / (name + ".tmp")
            staged.append((temporary, report_dir / name))
            temporary.write_text(text, encoding="utf-8", newline="\n")
        return replace_with_rollback(staged)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


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
        results = analysis.analyse(timestamps, btc_closes, eth_closes)
        btc_candles = candles_for_dates(completed_by_pair[first_pair], timestamps)
        eth_candles = candles_for_dates(completed_by_pair[second_pair], timestamps)
        last_date = format_utc_date(timestamps[-1])
        files = [
            (f"report_{last_date}.md", build_report(results, now, sorted(excluded_dates))),
            (f"report_{last_date}_data.csv", build_csv(results, btc_candles, eth_candles)),
        ]
        leftover_backups = write_outputs(report_dir, files)
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    for backup in leftover_backups:
        print(f"WARNING: could not delete the old copy {backup}; it can be deleted by hand", file=sys.stderr)
    print(f"\nReport written: {report_dir / files[0][0]}")
    print(f"Data written:   {report_dir / files[1][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
