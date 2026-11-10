"""Shared support for the workflow tests. WORKFLOW_SRC selects the code under test (default: this repository's own src folder). Fixtures (saved real Kraken replies) live in tests/fixtures."""
import contextlib, hashlib, importlib, io, json, os, random, shutil, sys, tempfile
from datetime import datetime, timezone, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
SRC = os.environ.get("WORKFLOW_SRC", os.path.join(REPO, "src"))
for path in (HERE, SRC): sys.path.insert(0, path)

class Missing:
    """Stands in for a module that cannot be imported, so each check fails individually with a clear reason."""
    def __init__(self, name, error): self._name, self._error = name, error
    def __getattr__(self, attribute): raise AttributeError(f"module '{self._name}' is not available ({self._error})")

def load(name):
    try: return importlib.import_module(name)
    except BaseException as error: return Missing(name, f"{type(error).__name__}: {str(error)[:70]}")

fetch_data = load("fetch_data")                      # must be importable first: fakehttp looks it up by name
import fakehttp
analysis, run_workflow = load("analysis"), load("run_workflow")

class Runner:
    def __init__(self, title): self.title, self.results = title, []
    def check(self, name, function):
        try:
            outcome = function(); ok, detail = outcome is not False, ""
        except BaseException as error: ok, detail = False, f"{type(error).__name__}: {str(error)[:120]}"
        self.results.append(ok); print("PASS" if ok else "FAIL", name, ("| " + detail) if detail else "")
    def register(self, name):
        def decorator(function): self.check(name, function); return function
        return decorator
    def finish(self):
        print(f"\n{sum(self.results)}/{len(self.results)} {self.title} passed"); sys.exit(0 if all(self.results) else 1)

def raises(error_type, function, contains=""):
    try: function()
    except error_type as error:
        assert contains in str(error), f"message {str(error)!r} does not contain {contains!r}"; return
    raise AssertionError(f"{error_type.__name__} was not raised")

# ---------------------------------------------------------------- synthetic Kraken replies
utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
DAY = 86400
TODAY = utc(2026, 3, 20)                                   # the day in progress in the synthetic scenarios
NOW = utc(2026, 3, 20, 9, 30)
midnight = lambda day: int(day.timestamp())

def candle_row(ts, close, volume="3.25"):
    return [ts, str(close), str(close), str(close), str(close), str(close), volume, 4]
def kraken_body(rows, key="XXBTZUSD"):
    return json.dumps({"error": [], "result": {key: rows, "last": 1}}).encode()

def random_closes(count, seed, correlated_with=None):
    rng, level, closes = random.Random(seed), 60000.0 if correlated_with is None else 3000.0, []
    for i in range(count):
        shock = rng.gauss(0, 0.02)
        move = shock if correlated_with is None else 0.8 * (correlated_with[i] / (correlated_with[i - 1] if i else correlated_with[0]) - 1) + 0.6 * shock
        level *= 1 + move; closes.append(round(level, 2))
    return closes

def synthetic_bodies(count=40, seed=7, btc=None, eth=None, drop_btc_day=None, eth_end_offset=0, marker=True):
    """Both pairs: `count` completed days ending yesterday + an unfinished row for today (close 999999.0 if marker)."""
    btc = btc or random_closes(count, seed); eth = eth or random_closes(count, seed + 1, correlated_with=[c if c > 0 else 1.0 for c in btc])
    days = [midnight(TODAY) - (count - i) * DAY for i in range(count)]
    btc_rows = [candle_row(t, c) for t, c in zip(days, btc)]
    eth_days = [t + eth_end_offset * DAY for t in days]
    eth_rows = [candle_row(t, c) for t, c in zip(eth_days, eth)]
    if drop_btc_day is not None: btc_rows.pop(drop_btc_day)
    if marker:
        btc_rows.append(candle_row(midnight(TODAY), "999999.0", volume="0.001"))
        eth_rows.append(candle_row(midnight(TODAY) + eth_end_offset * DAY, "999999.0", volume="0.001"))
    return {"XBTUSD": kraken_body(btc_rows, "XXBTZUSD"), "ETHUSD": kraken_body(eth_rows, "XETHZUSD")}, btc, eth, days

REAL = {"XBTUSD": open(os.path.join(FIXTURES, "xbt.json"), "rb").read(), "ETHUSD": open(os.path.join(FIXTURES, "eth.json"), "rb").read()}
REAL_NOW = utc(2026, 10, 27, 9, 41)                        # when those two real replies were captured; 2026-10-27 was the unfinished day

def run_main(bodies, now, report_dir, failing_pair=None):
    """Run the workflow with Kraken mocked. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    def fake_get(url, params=None, **kwargs):
        if params["pair"] == failing_pair: return fakehttp.FakeResponse(b"", status=403)
        return fakehttp.FakeResponse(bodies[params["pair"]])
    with mock.patch("requests.get", side_effect=fake_get), mock.patch("time.sleep"), mock.patch.object(run_workflow, "datetime", wraps=datetime) as dt, \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        dt.now.return_value = now
        code = run_workflow.main(report_dir)
    return code, out.getvalue(), err.getvalue()

def new_temp_dir(): return tempfile.mkdtemp(prefix="wf_")
def files_in(directory): return sorted(os.listdir(directory)) if os.path.isdir(directory) else []
def read(directory, name): return open(os.path.join(directory, name), encoding="utf-8", newline="").read()
