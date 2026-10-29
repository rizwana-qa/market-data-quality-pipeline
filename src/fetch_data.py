"""Fetch and clean daily candle data from Kraken's public OHLC endpoint.

Run with: python src/fetch_data.py
"""

import json
import math
import sys
import time
from datetime import datetime, timezone

import requests

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
DAILY_INTERVAL_MINUTES = 1440
REQUEST_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB; a real daily response is about 62 KB
DOWNLOAD_CHUNK_BYTES = 65_536
MAX_ATTEMPTS = 3  # one first try plus two retries
RETRY_BASE_DELAY_SECONDS = 1  # waits 1 s, then 2 s
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SECONDS_PER_DAY = 86400
MIN_COMPLETED_CANDLES = 31  # 30 daily returns need 31 closing prices
MAX_DATES_SHOWN = 10  # keeps long problem lists readable
MAX_PROBLEMS_SHOWN = 20  # the total is always printed, but details stop after this many
MAX_TIMESTAMP = 4102444800  # 2100-01-01 UTC; later values cannot be real candles and break date formatting
PAIRS = ["XBTUSD", "ETHUSD"]  # Kraken calls Bitcoin "XBT"

# Kraken's OHLC candle layout: [time, open, high, low, close, vwap, volume, count]
EXPECTED_FIELDS_PER_CANDLE = 8


def _to_finite_float(value, field_name, row_number):
    """Convert one Kraken string such as "67123.4" into a float, or raise a clear error."""
    if value is None or value == "":
        raise ValueError(f"Candle {row_number}: '{field_name}' is missing")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Candle {row_number}: '{field_name}' is not a number: {value!r}")
    if not math.isfinite(number):  # float() accepts "nan" and "inf", which are not real prices
        raise ValueError(f"Candle {row_number}: '{field_name}' is not finite: {value!r}")
    if number < 0:
        raise ValueError(f"Candle {row_number}: '{field_name}' is negative: {value!r}")
    return number


def parse_candles(raw_candles):
    """Turn Kraken's raw candle lists into a list of dictionaries with proper number types."""
    cleaned_candles = []
    for row_number, candle in enumerate(raw_candles):
        if not isinstance(candle, list) or len(candle) != EXPECTED_FIELDS_PER_CANDLE:
            raise ValueError(
                f"Candle {row_number}: expected a list of {EXPECTED_FIELDS_PER_CANDLE} values, got {candle!r}"
            )
        timestamp, open_, high, low, close, vwap, volume, trade_count = candle

        # bool is a subclass of int in Python, so exclude it explicitly
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise ValueError(f"Candle {row_number}: timestamp is not an integer: {timestamp!r}")
        if not 0 <= timestamp <= MAX_TIMESTAMP:
            raise ValueError(f"Candle {row_number}: timestamp is outside the valid range: {timestamp!r}")
        if not isinstance(trade_count, int) or isinstance(trade_count, bool):
            raise ValueError(f"Candle {row_number}: trade count is not an integer: {trade_count!r}")

        cleaned_candles.append({
            "timestamp": timestamp,  # stays an integer (Unix seconds, UTC)
            "open": _to_finite_float(open_, "open", row_number),
            "high": _to_finite_float(high, "high", row_number),
            "low": _to_finite_float(low, "low", row_number),
            "close": _to_finite_float(close, "close", row_number),
            "vwap": _to_finite_float(vwap, "vwap", row_number),
            "volume": _to_finite_float(volume, "volume", row_number),
            "trade_count": trade_count,
        })
    return cleaned_candles


def read_limited_body(response, pair):
    """Download the response in chunks and stop as soon as it exceeds the size limit."""
    body = bytearray()
    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError(f"{pair}: response exceeds the size limit of {MAX_RESPONSE_BYTES} bytes")
    return bytes(body)


def is_transient(error):
    """True for failures worth retrying: rate limits, server errors, dropped or slow connections."""
    if isinstance(error, requests.exceptions.HTTPError):
        return error.response is not None and error.response.status_code in RETRYABLE_STATUS_CODES
    return isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def download_body(pair):
    """Download one response body, retrying transient failures with exponential backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # stream=True lets us read the body in pieces instead of downloading all of it at once
            with requests.get(
                KRAKEN_OHLC_URL,
                params={"pair": pair, "interval": DAILY_INTERVAL_MINUTES},
                timeout=REQUEST_TIMEOUT_SECONDS,
                stream=True,
            ) as response:
                response.raise_for_status()  # turns HTTP errors such as 429 or 500 into exceptions
                return read_limited_body(response, pair)
        except requests.exceptions.RequestException as error:
            if not is_transient(error):
                raise RuntimeError(f"{pair}: request to Kraken failed: {error!r}")
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"{pair}: request to Kraken failed after {MAX_ATTEMPTS} attempts: {error!r}"
                )
            delay = RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            print(
                f"{pair}: attempt {attempt} of {MAX_ATTEMPTS} failed ({error!r}); retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)


def extract_raw_candles(body, pair):
    """Check the shape of Kraken's JSON reply and return its raw candle rows."""
    if not isinstance(body, dict) or "error" not in body or "result" not in body:
        raise RuntimeError(f"{pair}: unexpected response structure (missing 'error' or 'result')")
    if body["error"]:  # Kraken reports problems in this list, even with HTTP status 200
        raise RuntimeError(f"{pair}: Kraken returned errors: {body['error']}")
    if not isinstance(body["result"], dict):
        raise RuntimeError(f"{pair}: 'result' is not an object")

    # The candle list is stored under Kraken's own pair name (e.g. XXBTZUSD), not the
    # name we requested, so we look for it. 'last' is a separate pointer, not candles.
    candle_keys = [key for key in body["result"] if key != "last"]
    if len(candle_keys) != 1:
        raise RuntimeError(f"{pair}: expected exactly one pair key in result, found {candle_keys}")
    raw_candles = body["result"][candle_keys[0]]
    if not isinstance(raw_candles, list) or not raw_candles:
        raise RuntimeError(f"{pair}: no candles returned under '{candle_keys[0]}'")

    return raw_candles


def fetch_daily_candles(pair):
    """Request daily candles for one pair from Kraken and return them cleaned by parse_candles."""
    body_bytes = download_body(pair)

    try:
        body = json.loads(body_bytes)
    except ValueError:  # also covers bytes that are not valid text
        raise RuntimeError(f"{pair}: Kraken did not return valid JSON")

    return parse_candles(extract_raw_candles(body, pair))


def format_utc_date(unix_seconds):
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def format_utc_time(unix_seconds):
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def summarize_dates(timestamps):
    """Show up to MAX_DATES_SHOWN dates, then say how many more there are."""
    dates = [format_utc_date(timestamp) for timestamp in timestamps]
    summary = ", ".join(dates[:MAX_DATES_SHOWN])
    if len(dates) > MAX_DATES_SHOWN:
        summary += f" ... and {len(dates) - MAX_DATES_SHOWN} more"
    return summary


def split_candles(candles, now):
    """Split candles by today's UTC midnight, taken from the clock (not from the last row).

    completed:  opened before today, so the day is over
    unfinished: opened exactly at today's midnight, so the day is still in progress
    future:     opened after today's midnight, which should never happen
    """
    today_start = int(now.timestamp()) // SECONDS_PER_DAY * SECONDS_PER_DAY
    completed = [candle for candle in candles if candle["timestamp"] < today_start]
    unfinished = [candle for candle in candles if candle["timestamp"] == today_start]
    future = [candle for candle in candles if candle["timestamp"] > today_start]
    return completed, unfinished, future


def check_timestamps(candles):
    """Return a list of problems found in the timestamps (an empty list means all is well)."""
    problems = []
    timestamps = [candle["timestamp"] for candle in candles]

    for timestamp in timestamps:
        if timestamp % SECONDS_PER_DAY != 0:
            problems.append(f"off-boundary timestamp (not midnight UTC): {format_utc_time(timestamp)}")

    # Row order: compare each candle with the one before it in the list.
    for previous, current in zip(timestamps, timestamps[1:]):
        if current == previous:
            problems.append(f"duplicate timestamp: {format_utc_time(current)}")
        elif current < previous:
            problems.append(
                f"out-of-order: {format_utc_time(current)} comes after {format_utc_time(previous)}"
            )

    # Missing days: look at the dates in calendar order, so a date that exists but sits in
    # the wrong row is never reported as missing.
    unique_sorted = sorted(set(timestamps))
    for previous, current in zip(unique_sorted, unique_sorted[1:]):
        step = current - previous
        if step > SECONDS_PER_DAY and step % SECONDS_PER_DAY == 0:
            missing = list(range(previous + SECONDS_PER_DAY, current, SECONDS_PER_DAY))
            problems.append(f"{len(missing)} missing day(s): {summarize_dates(missing)}")
    return problems


def validate_pair(pair, candles, now):
    """Return (completed candles, unfinished candles, list of problems) for one pair."""
    completed, unfinished, future = split_candles(candles, now)
    problems = [
        f"{pair}: candle dated after today's UTC day: {format_utc_time(candle['timestamp'])}"
        for candle in future
    ]
    problems += [f"{pair}: {problem}" for problem in check_timestamps(completed)]
    if len(completed) < MIN_COMPLETED_CANDLES:
        problems.append(
            f"{pair}: only {len(completed)} completed candles, need at least {MIN_COMPLETED_CANDLES}"
        )
    return completed, unfinished, problems


def check_pair_alignment(first_pair, first_candles, second_pair, second_candles):
    """Both pairs must have exactly the same completed-candle timestamps."""
    first_timestamps = {candle["timestamp"] for candle in first_candles}
    second_timestamps = {candle["timestamp"] for candle in second_candles}
    problems = []
    for name, other, only_here in (
        (first_pair, second_pair, sorted(first_timestamps - second_timestamps)),
        (second_pair, first_pair, sorted(second_timestamps - first_timestamps)),
    ):
        if only_here:
            problems.append(
                f"{name} has {len(only_here)} completed date(s) missing from {other}: "
                f"{summarize_dates(only_here)}"
            )
    return problems


def print_pair_summary(pair, candles, completed, unfinished, pair_problems):
    """Print the three status lines for one pair."""
    timestamps = [candle["timestamp"] for candle in candles]
    print(
        f"{pair}: fetched {len(candles)} candles, "
        f"{format_utc_date(min(timestamps))} to {format_utc_date(max(timestamps))} (UTC)"
    )
    unfinished_dates = ", ".join(format_utc_date(c["timestamp"]) for c in unfinished)
    print(
        f"{pair}: excluded {len(unfinished)} unfinished candle(s)"
        f"{' dated ' + unfinished_dates if unfinished else ''}; "
        f"{len(completed)} completed candles remain"
    )
    status = f"FAILED ({len(pair_problems)} issue(s))" if pair_problems else "OK"
    print(f"{pair}: timestamp checks {status}")


def print_validation_failures(problems):
    """Print the total and the first MAX_PROBLEMS_SHOWN problems to stderr (the caller exits)."""
    print(f"\nVALIDATION FAILED: {len(problems)} issue(s) found", file=sys.stderr)
    for problem in problems[:MAX_PROBLEMS_SHOWN]:
        print(f"  - {problem}", file=sys.stderr)
    omitted = len(problems) - MAX_PROBLEMS_SHOWN
    if omitted > 0:
        print(f"  ... and {omitted} more issue(s) not shown", file=sys.stderr)


def main():
    now = datetime.now(timezone.utc)
    completed_by_pair = {}
    problems = []

    for pair in PAIRS:
        try:
            candles = fetch_daily_candles(pair)
        except (RuntimeError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            sys.exit(1)

        completed, unfinished, pair_problems = validate_pair(pair, candles, now)
        completed_by_pair[pair] = completed
        problems += pair_problems

        print_pair_summary(pair, candles, completed, unfinished, pair_problems)

    first_pair, second_pair = PAIRS
    problems += check_pair_alignment(
        first_pair, completed_by_pair[first_pair], second_pair, completed_by_pair[second_pair]
    )

    if problems:
        print_validation_failures(problems)
        sys.exit(1)

    count = len(completed_by_pair[first_pair])
    print(f"\nValidation passed: {first_pair} and {second_pair} have identical timestamps, "
          f"{count} completed candles each")


if __name__ == "__main__":
    main()
