"""Characterisation tests for src/fetch_data.py: what the fetcher does TODAY with Kraken replies, so that the checks the workflow relies on are pinned by tests.

The fetcher's byte-identity is protected by tests/test_workflow_integration.py (re-pinned after the response-identity and numeric-type hardening). These tests assert behaviour that exists:
what it rejects, how it retries, how it splits unfinished candles and checks dates. They deliberately do not assert that the fetcher rejects anything it accepts
today; the conditions the workflow checks elsewhere are tested in tests/test_market_data_pipeline.py. Kraken is mocked everywhere; nothing contacts the network."""
import json
from unittest import mock

import requests

from wf_support import *
import fakehttp

R = Runner("fetch data validation checks")
check = R.register
F = fetch_data
DAILY = midnight(TODAY)                                     # today's UTC midnight in the synthetic scenarios (the unfinished day)
GOOD = [candle_row(DAILY - (40 - i) * DAY, 100 + i) for i in range(40)]


def raw(timestamp=DAILY - DAY, o="100", h="110", l="90", c="105", vwap="100", volume="5", count=10):
    return [timestamp, o, h, l, c, vwap, volume, count]


def parse_message(candle):
    try: F.parse_candles([candle])
    except ValueError as error: return str(error)
    return None


def reply(rows=None, key="XXBTZUSD"):
    return {"error": [], "result": {key: GOOD if rows is None else rows, "last": 1}}


# ---------------------------------------------------------------- parse_candles
@check("parse: a valid candle becomes floats, an integer timestamp (Unix seconds) and an integer trade count, under the documented key names")
def _():
    candle = F.parse_candles([raw()])[0]
    assert list(candle) == ["timestamp", "open", "high", "low", "close", "vwap", "volume", "trade_count"]
    assert (candle["timestamp"], candle["open"], candle["high"], candle["low"], candle["close"], candle["vwap"], candle["volume"], candle["trade_count"]) == (DAILY - DAY, 100.0, 110.0, 90.0, 105.0, 100.0, 5.0, 10)
    assert type(candle["timestamp"]) is int and type(candle["trade_count"]) is int and all(type(candle[k]) is float for k in ("open", "high", "low", "close", "vwap", "volume"))


@check("parse: zero volume is a valid candle")
def _(): assert F.parse_candles([raw(volume="0")])[0]["volume"] == 0.0


def parse_case(candle, fragment):
    def run():
        message = parse_message(candle)
        assert message is not None and fragment in message, (candle, message)
    return run


for label, candle, fragment in (
    ("NaN price", raw(c="nan"), "not finite"), ("infinite price", raw(h="inf"), "not finite"), ("negative price", raw(c="-1"), "negative"), ("negative volume", raw(volume="-1"), "negative"),
    ("non-numeric price", raw(o="abc"), "not a number"), ("missing field (None)", raw(c=None), "missing"), ("empty field", raw(o=""), "missing"),
    ("seven fields", raw()[:7], "expected a list of 8"), ("nine fields", raw() + [1], "expected a list of 8"), ("not a list", {"time": 1}, "expected a list of 8"),
    ("boolean timestamp", [True] + raw()[1:], "not an integer"), ("string timestamp", ["1700000000"] + raw()[1:], "not an integer"), ("float timestamp", [1700000000.0] + raw()[1:], "not an integer"),
    ("timestamp beyond 2100", raw(timestamp=7258118400), "outside the valid range"), ("negative timestamp", raw(timestamp=-1), "outside the valid range"),
    ("float trade count", raw(count=10.0), "trade count is not an integer"), ("boolean trade count", raw(count=True), "trade count is not an integer"), ("string trade count", raw(count="10"), "trade count is not an integer"),
):
    check(f"parse: {label} is rejected with a clear message")(parse_case(candle, fragment))


@check("parse: the error names the candle row, so a bad row can be found")
def _():
    message = None
    try: F.parse_candles([raw(), raw(c="nan")])
    except ValueError as error: message = str(error)
    assert message and "Candle 1" in message and "'close'" in message, message


@check("parse [current behaviour, checked elsewhere]: relations between the fields and the sign of the trade count are not checked by the fetcher itself; the workflow check is tested in tests/test_market_data_pipeline.py")
def _():
    kept = F.parse_candles([raw(h="80", l="90", count=-5)])[0]
    assert (kept["high"], kept["low"], kept["trade_count"]) == (80.0, 90.0, -5)


# ---------------------------------------------------------------- the reply structure
def structure_case(body, fragment):
    return lambda: raises(RuntimeError, lambda: F.extract_raw_candles(body, "XBTUSD"), fragment)


for label, body, fragment in (
    ("a list instead of an object", [1, 2], "unexpected response structure"), ("no 'result'", {"error": []}, "unexpected response structure"), ("no 'error'", {"result": {}}, "unexpected response structure"),
    ("a Kraken error list", {"error": ["EQuery:Unknown asset pair"], "result": {}}, "Kraken returned errors"), ("'result' that is not an object", {"error": [], "result": []}, "not an object"),
    ("two pair keys", {"error": [], "result": {"A": GOOD, "B": GOOD, "last": 1}}, "exactly one pair key"), ("no pair key", {"error": [], "result": {"last": 1}}, "exactly one pair key"),
    ("an empty candle list", reply(rows=[]), "no candles"), ("candles that are not a list", reply(rows="x"), "no candles"),
):
    check(f"structure: {label} is rejected")(structure_case(body, fragment))


@check("structure: the candle list is found under Kraken's own pair name, and the 'last' pointer is not mistaken for candles")
def _(): assert F.extract_raw_candles(reply(), "XBTUSD") == GOOD and F.extract_raw_candles(reply(key="XETHZUSD"), "ETHUSD") == GOOD


# ---------------------------------------------------------------- HTTP: retries, size, malformed bodies (Kraken and the clock mocked)
def ok(): return fakehttp.FakeResponse(json.dumps(reply()).encode())
def status(code): return fakehttp.FakeResponse(b"", status=code)


def download(sequence):
    """Run download_body against a scripted series of responses and exceptions. Returns (outcome, number of requests, waits in seconds)."""
    with mock.patch("requests.get", side_effect=sequence) as get, mock.patch("time.sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
        try: outcome = F.download_body("XBTUSD")
        except RuntimeError as error: outcome = error
    return outcome, get.call_count, [call.args[0] for call in sleep.call_args_list]


def retried_then_ok(first):
    def run():
        outcome, calls, waits = download([first, ok()])
        assert isinstance(outcome, bytes) and calls == 2 and waits == [1], (outcome, calls, waits)
    return run


def not_retried(first):
    def run():
        outcome, calls, waits = download([first, ok()])
        assert isinstance(outcome, RuntimeError) and calls == 1 and waits == [], (outcome, calls, waits)
    return run


@check("http: the request asks for the daily interval (1440) of the requested pair, with a timeout, streaming")
def _():
    with mock.patch("requests.get", side_effect=[ok()]) as get: F.download_body("ETHUSD")
    args, kwargs = get.call_args
    assert args[0] == "https://api.kraken.com/0/public/OHLC" and kwargs["params"] == {"pair": "ETHUSD", "interval": 1440} and kwargs["timeout"] == 15 and kwargs["stream"] is True


@check("http: a first-try success makes one request and waits for nothing")
def _(): outcome, calls, waits = download([ok()]); assert isinstance(outcome, bytes) and calls == 1 and waits == []


for code in (429, 500, 502, 503, 504):
    check(f"http: status {code} is retried and a later success is returned (one wait of 1 second)")(retried_then_ok(status(code)))
for code in (400, 401, 403, 404):
    check(f"http: status {code} is NOT retried (one request, no wait)")(not_retried(status(code)))
for label, error in (("connection error", requests.exceptions.ConnectionError("down")), ("timeout", requests.exceptions.Timeout("slow")), ("chunked-encoding error", requests.exceptions.ChunkedEncodingError("cut"))):
    check(f"http: a {label} is retried")(retried_then_ok(error))
check("http: any other request error (for example too many redirects) is not retried")(not_retried(requests.exceptions.TooManyRedirects("loop")))


@check("http: two failures then a success: three requests, waits of 1 and 2 seconds (exponential)")
def _(): outcome, calls, waits = download([status(503), status(429), ok()]); assert isinstance(outcome, bytes) and calls == 3 and waits == [1, 2]


@check("http: three transient failures give up after exactly three attempts, with the attempt count in the error")
def _():
    outcome, calls, waits = download([status(500)] * 3)
    assert isinstance(outcome, RuntimeError) and "after 3 attempts" in str(outcome) and calls == 3 and waits == [1, 2]


@check("http: an error in the middle of the body (chunked) is retried and then succeeds")
def _():
    class Broken(fakehttp.FakeResponse):
        def iter_content(self, chunk_size=1):
            yield b"abc"
            raise requests.exceptions.ChunkedEncodingError("cut")
    outcome, calls, waits = download([Broken(b"abcdef"), ok()]); assert isinstance(outcome, bytes) and calls == 2 and waits == [1]


@check("http: a body over the size limit is rejected once it exceeds the limit, without a retry")
def _():
    outcome, calls, waits = download([fakehttp.FakeResponse(b"x" * (F.MAX_RESPONSE_BYTES + 1000)), ok()])
    assert isinstance(outcome, RuntimeError) and "size limit" in str(outcome) and calls == 1 and waits == []


@check("http: a body of exactly the size limit is accepted")
def _(): outcome, *_rest = download([fakehttp.FakeResponse(b"x" * F.MAX_RESPONSE_BYTES)]); assert isinstance(outcome, bytes) and len(outcome) == F.MAX_RESPONSE_BYTES


def fetch_body(body, pair="XBTUSD"):
    with mock.patch("requests.get", side_effect=[fakehttp.FakeResponse(body)]): return F.fetch_daily_candles(pair)


for label, body in (("not JSON", b"<html>busy</html>"), ("bytes that are not text", b"\xff\xfe\x00"), ("empty", b"")):
    check(f"fetch: a body that is {label} is rejected as invalid JSON")((lambda body=body: raises(RuntimeError, lambda: fetch_body(body), "valid JSON")))


@check("fetch: a whole valid reply becomes parsed candles in the order Kraken sent them")
def _():
    candles = fetch_body(json.dumps(reply()).encode())
    assert len(candles) == 40 and candles[0]["timestamp"] == GOOD[0][0] and candles[-1]["timestamp"] == GOOD[-1][0]


@check("fetch: a JSON NaN token inside a candle is rejected by the number check")
def _():
    body = b'{"error":[],"result":{"XXBTZUSD":[[%d,NaN,1,1,1,1,1,1]],"last":1}}' % (DAILY - DAY)
    raises(ValueError, lambda: fetch_body(body), "not finite")


# ---------------------------------------------------------------- timestamps, unfinished and future candles
def validate(rows, pair="XBTUSD", now=NOW):
    return F.validate_pair(pair, F.parse_candles(rows), now)


@check("validate: forty consecutive completed days and no unfinished candle: all completed, no problems")
def _(): completed, unfinished, problems = validate(GOOD); assert len(completed) == 40 and unfinished == [] and problems == []


@check("validate: the candle of today's UTC midnight is unfinished and is excluded from the completed days")
def _():
    completed, unfinished, problems = validate(GOOD + [candle_row(DAILY, 999999)])
    assert len(completed) == 40 and [c["timestamp"] for c in unfinished] == [DAILY] and problems == [] and all(c["close"] != 999999.0 for c in completed)


@check("validate: the boundary at UTC midnight - one second before today's midnight is off-boundary, today's midnight is unfinished, one day later is a future candle")
def _():
    assert any("off-boundary" in p for p in validate(GOOD + [candle_row(DAILY - 1, 1)])[2])
    completed, unfinished, problems = validate(GOOD + [candle_row(DAILY, 1)]); assert len(unfinished) == 1 and problems == []
    assert any("after today's UTC day" in p for p in validate(GOOD + [candle_row(DAILY + DAY, 1)])[2])


@check("validate: the split follows the clock - one second before UTC midnight the day is unfinished, at midnight it is completed")
def _():
    rows = GOOD[:-1]
    day = rows[-1][0] + DAY
    before = F.validate_pair("XBTUSD", F.parse_candles(rows + [candle_row(day, 1)]), utc(2026, 3, 19, 23, 59, 59))
    after = F.validate_pair("XBTUSD", F.parse_candles(rows + [candle_row(day, 1)]), utc(2026, 3, 20, 0, 0, 0))
    assert len(before[1]) == 1 and len(after[0]) == len(rows) + 1 and after[1] == []


@check("validate: exactly 31 completed days are enough; 30 are not")
def _(): assert validate(GOOD[-31:])[2] == [] and any("only 30 completed" in p for p in validate(GOOD[-30:])[2])


@check("validate: a missing day, and several missing days over a longer gap, are reported with their counts")
def _():
    one, many = validate(GOOD[:10] + GOOD[11:])[2], validate(GOOD[:10] + GOOD[14:])[2]
    assert len(one) == 1 and "1 missing day(s)" in one[0] and len(many) == 1 and "4 missing day(s)" in many[0]


@check("validate: a duplicate timestamp next to its twin is reported")
def _(): assert any("duplicate timestamp" in p for p in validate(GOOD[:20] + [GOOD[19]] + GOOD[20:])[2])


@check("validate: a NON-adjacent duplicate (an earlier day again later in the list) is reported as out-of-order")
def _(): assert any("out-of-order" in p for p in validate(GOOD[:20] + [GOOD[5]] + GOOD[20:])[2])


@check("validate: candles in the wrong order are reported and not mistaken for missing days")
def _():
    rows = list(GOOD); rows[10], rows[11] = rows[11], rows[10]
    problems = validate(rows)[2]; assert any("out-of-order" in p for p in problems) and not any("missing" in p for p in problems)


@check("validate: a timestamp that is not midnight UTC is reported")
def _(): assert any("off-boundary" in p for p in validate(GOOD[:5] + [candle_row(GOOD[5][0] + 3600, 1)] + GOOD[6:])[2])


@check("validate: problems carry the pair name, so BTC and ETH problems can be told apart")
def _(): problems = validate(GOOD[:10] + GOOD[11:], pair="ETHUSD")[2]; assert problems and all(p.startswith("ETHUSD:") for p in problems)


# ---------------------------------------------------------------- BTC / ETH alignment
@check("align: identical completed dates pass; a series one day later is reported for BOTH sides")
def _():
    a, b = F.parse_candles(GOOD), F.parse_candles([candle_row(r[0] + DAY, 1) for r in GOOD])
    assert F.check_pair_alignment("BTC", a, "ETH", a) == []
    problems = F.check_pair_alignment("BTC", a, "ETH", b); assert len(problems) == 2 and "BTC has 1" in problems[0] and "ETH has 1" in problems[1]


# ---------------------------------------------------------------- Kraken response identity: the candles must come under the key of the REQUESTED pair (verified against the saved real replies)
REAL_BODY = {pair: json.loads(raw_bytes) for pair, raw_bytes in REAL.items()}
EXPECTED_KEY = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD"}
OTHER = {"XBTUSD": "ETHUSD", "ETHUSD": "XBTUSD"}


def real_candles(pair): return REAL_BODY[pair]["result"][EXPECTED_KEY[pair]]


def keyed(pair, **result):
    """A reply for `pair` whose result holds exactly the given keys (the 'last' cursor is added unless given)."""
    result.setdefault("last", 1); return {"error": [], "result": result}


@check("identity: the saved real replies use exactly the keys XXBTZUSD (for XBTUSD) and XETHZUSD (for ETHUSD), plus the 'last' cursor - the mapping the fetcher relies on")
def _():
    for pair, key in EXPECTED_KEY.items(): assert sorted(REAL_BODY[pair]["result"]) == sorted([key, "last"]), (pair, list(REAL_BODY[pair]["result"]))


def accepts_real_reply(pair):
    def run():
        candles = fetch_body(REAL[pair], pair)
        assert len(candles) == len(real_candles(pair)) and candles[0]["timestamp"] == real_candles(pair)[0][0]
    return run


def rejects_other_reply(pair):
    return lambda: raises(RuntimeError, lambda: F.extract_raw_candles(REAL_BODY[OTHER[pair]], pair), pair)


for pair in EXPECTED_KEY:
    check(f"identity: the real {pair} reply under its own key is accepted, in full ({len(real_candles(pair))} candles, parsed)")(accepts_real_reply(pair))
    check(f"identity: a {pair} request that receives the OTHER asset's real reply ({EXPECTED_KEY[OTHER[pair]]}) is rejected, naming the pair")(rejects_other_reply(pair))


def identity_message(pair, body):
    try: F.extract_raw_candles(body, pair)
    except RuntimeError as error: return str(error)
    return None


@check("identity: the rejection message names the requested pair, the unexpected key and the expected key")
def _():
    message = identity_message("XBTUSD", REAL_BODY["ETHUSD"])
    assert message and "XBTUSD" in message and "XETHZUSD" in message and "XXBTZUSD" in message, message
    message = identity_message("ETHUSD", REAL_BODY["XBTUSD"])
    assert message and "ETHUSD" in message and "XXBTZUSD" in message and "XETHZUSD" in message, message


@check("identity: the other asset's candles are never relabelled - even a complete valid ETH series under the BTC request ends in an error, never in candles")
def _():
    with mock.patch("requests.get", side_effect=[fakehttp.FakeResponse(REAL["ETHUSD"])]): raises(RuntimeError, lambda: F.fetch_daily_candles("XBTUSD"), "XETHZUSD")
    with mock.patch("requests.get", side_effect=[fakehttp.FakeResponse(REAL["XBTUSD"])]): raises(RuntimeError, lambda: F.fetch_daily_candles("ETHUSD"), "XXBTZUSD")


def rejects_key(key): return lambda: raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", **{key: GOOD}), "XBTUSD"), "unexpected")


for label, key in (("a made-up key", "FOOBAR"), ("the requested pair's own name as the key", "XBTUSD"), ("a lower-case key", "xxbtzusd"), ("a key with trailing text", "XXBTZUSD "), ("an unrelated asset", "XXRPZUSD")):
    check(f"identity: {label} ({key!r}) under a BTC request is rejected")(rejects_key(key))


@check("identity: the expected key plus ANY other key is rejected (never 'the first one wins'), and so are two unexpected keys; nothing is guessed")
def _():
    raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", XXBTZUSD=GOOD, XETHZUSD=GOOD), "XBTUSD"), "exactly one pair key")
    raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", XXBTZUSD=GOOD, EXTRA=[]), "XBTUSD"), "exactly one pair key")
    raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", A=GOOD, B=GOOD), "XBTUSD"), "exactly one pair key")


@check("identity: no candle key at all (only the 'last' cursor), or a null/empty list under the expected key, is rejected")
def _():
    raises(RuntimeError, lambda: F.extract_raw_candles({"error": [], "result": {"last": 1}}, "XBTUSD"), "exactly one pair key")
    raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", XXBTZUSD=None), "XBTUSD"), "no candles")
    raises(RuntimeError, lambda: F.extract_raw_candles(keyed("XBTUSD", XXBTZUSD=[]), "XBTUSD"), "no candles")


@check("identity: the 'last' cursor never causes a false rejection - whatever its value or type, and when it is absent - and is never mistaken for candles")
def _():
    for last in (1, 1792972800, "1792972800", None, 0):
        assert F.extract_raw_candles(keyed("XBTUSD", XXBTZUSD=GOOD, last=last), "XBTUSD") == GOOD, last
    assert F.extract_raw_candles({"error": [], "result": {"XXBTZUSD": GOOD}}, "XBTUSD") == GOOD
    raises(RuntimeError, lambda: F.extract_raw_candles({"error": [], "result": {"last": GOOD}}, "XBTUSD"), "exactly one pair key")


@check("identity: a pair the fetcher has no verified key for is rejected rather than accepted under whatever key arrives")
def _(): raises(RuntimeError, lambda: F.extract_raw_candles(keyed("SOLUSD", XSOLZUSD=GOOD), "SOLUSD"), "SOLUSD")


@check("identity: the earlier checks still come first - a Kraken error list and a broken structure are reported as before")
def _():
    raises(RuntimeError, lambda: F.extract_raw_candles({"error": ["EGeneral:Invalid arguments"], "result": {}}, "XBTUSD"), "Kraken returned errors")
    raises(RuntimeError, lambda: F.extract_raw_candles({"result": {}}, "XBTUSD"), "unexpected response structure")


# ---------------------------------------------------------------- numeric types at the PARSING boundary (not only in the workflow's later plausibility check)
NUMERIC_FIELDS = ("open", "high", "low", "close", "vwap", "volume")


def field_position(name): return {"open": 1, "high": 2, "low": 3, "close": 4, "vwap": 5, "volume": 6, "trade_count": 7}[name]


def candle_with(name, value):
    row = raw(); row[field_position(name)] = value; return row


def boolean_rejected(name, boolean):
    def run():
        message = parse_message(candle_with(name, boolean))
        assert message is not None and ("trade count" if name == "trade_count" else f"'{name}'") in message and ("not an integer" if name == "trade_count" else "boolean") in message, message
    return run


for name in NUMERIC_FIELDS + ("trade_count",):
    for boolean in (True, False):
        check(f"numeric: a boolean {name} ({boolean}) is rejected by parse_candles")(boolean_rejected(name, boolean))


@check("numeric: a JSON true/false in a Kraken reply never becomes a price - the whole fetch is rejected (the real fetch path, JSON text included)")
def _():
    for name in NUMERIC_FIELDS + ("trade_count",):
        for token in (b"true", b"false"):
            row = json.dumps(candle_with(name, "@@")).encode().replace(b'"@@"', token)
            body = b'{"error":[],"result":{"XXBTZUSD":[' + row + b'],"last":1}}'
            raises(ValueError, lambda: fetch_body(body), "boolean" if name != "trade_count" else "trade count")


def rejected_in_every_field(value, fragment):
    def run():
        for name in NUMERIC_FIELDS: raises(ValueError, lambda: F.parse_candles([candle_with(name, value)]), fragment)
    return run


for label, value in (("NaN", "NaN"), ("nan", "nan"), ("positive infinity", "inf"), ("negative infinity", "-inf"), ("the word Infinity", "Infinity")):
    check(f"numeric: {label} as text is rejected as not finite, for every price and the volume")(rejected_in_every_field(value, "not finite"))
for label, value in (("Python float NaN", float("nan")), ("Python float infinity", float("inf")), ("Python float negative infinity", float("-inf"))):
    check(f"numeric: {label} (as a JSON number token) is rejected")(rejected_in_every_field(value, "not finite"))


for label, value in (("a half exponent", "1e"), ("hexadecimal", "0x10"), ("an underscore", "1_000"), ("leading whitespace", " 5"), ("trailing whitespace", "5 "), ("a decimal comma", "1,5"), ("a double sign", "--1"),
                     ("a sign after the digits", "5-"), ("arabic-indic digits", chr(0x661) + chr(0x662)), ("a currency symbol", "$5"), ("a unit", "5 USD"), ("text", "abc"), ("a lone dot", "."), ("a lone sign", "+")):
    check(f"numeric: {label} ({ascii(value)}) is not accepted as a number, for every price and the volume")(rejected_in_every_field(value, "not a number"))


for label, value in (("None", None), ("a list", [1]), ("an object", {"a": 1}), ("an empty string", "")):
    check(f"numeric: {label} is rejected as missing or not a number")(rejected_in_every_field(value, "Candle 0"))


@check("numeric: valid numeric strings as Kraken sends them are accepted and keep their value (decimals, zero, long decimals, exponents, a leading plus, a trailing dot)")
def _():
    for text, number in (("67123.4", 67123.4), ("0", 0.0), ("0.00000000", 0.0), ("2320.45", 2320.45), ("1e-05", 1e-05), ("1.5E3", 1500.0), ("+5", 5.0), ("5.", 5.0), (".5", 0.5), ("502.74230778", 502.74230778), ("123", 123.0)):
        candle = F.parse_candles([raw(o=text, h=text, l=text, c=text, vwap=text, volume=text)])[0]
        assert [candle[k] for k in ("open", "high", "low", "close", "vwap", "volume")] == [number] * 6, text


@check("numeric: the existing sign rules are unchanged - a negative price or volume is still rejected as negative, and zero volume stays valid")
def _():
    raises(ValueError, lambda: F.parse_candles([raw(c="-1")]), "negative"); raises(ValueError, lambda: F.parse_candles([raw(volume="-0.5")]), "negative"); assert F.parse_candles([raw(volume="0")])[0]["volume"] == 0.0


@check("numeric: JSON numbers (not strings) that are real numbers are still accepted, as before - only booleans and non-finite values are refused")
def _(): candle = F.parse_candles([[DAILY - DAY, 100, 110.5, 90, 105, 100, 5, 10]])[0]; assert (candle["open"], candle["high"], candle["low"]) == (100.0, 110.5, 90.0) and type(candle["open"]) is float


def real_candles_parse(pair):
    def run():
        candles = F.parse_candles(real_candles(pair))
        assert len(candles) == len(real_candles(pair)) and all(type(c[k]) is float for c in candles for k in NUMERIC_FIELDS) and all(type(c["trade_count"]) is int for c in candles)
    return run


for pair in EXPECTED_KEY:
    check(f"numeric: every candle of the saved real {pair} reply is accepted by the stricter parsing (all values keep their numeric type)")(real_candles_parse(pair))


@check("the fetcher is the accepted baseline: byte-identical to the protected hash (this suite never changes it)")
def _(): assert hashlib.sha256(open(os.path.join(SRC, "fetch_data.py"), "rb").read()).hexdigest() == "ee4f045d9b4455ce13ed1245e04951dfe2942a35debbaa65cefc77dd838147d9"


R.finish()
