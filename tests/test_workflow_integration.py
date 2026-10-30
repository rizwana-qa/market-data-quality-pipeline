"""Integration tests for src/run_workflow.py with Kraken mocked (real saved replies and synthetic ones). Written BEFORE the module exists."""
import ast, csv, glob, io, re
from decimal import Decimal
from wf_support import *
import independent_calc as ind

R = Runner("workflow integration checks")
check = R.register
FETCHER_SHA256 = "8f23d9755a81311cce5a5b7598b7dd00645a14a5d8273d0cab8197ddeb95000d"
cleanup = []
def temp_dir(): d = new_temp_dir(); cleanup.append(d); return d
def csv_rows(directory, name):
    return list(csv.DictReader(io.StringIO(read(directory, name))))

@check("fetcher: src/fetch_data.py is byte-identical to the accepted baseline (sha256 8f23d975...)")
def _(): assert hashlib.sha256(open(os.path.join(SRC, "fetch_data.py"), "rb").read()).hexdigest() == FETCHER_SHA256
@check("dependencies: the only third-party import in any src file is 'requests' (so requirements.txt needs just that)")
def _():
    external = set()
    for path in glob.glob(os.path.join(SRC, "*.py")):
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            external |= {n for n in names if n not in sys.stdlib_module_names and not os.path.exists(os.path.join(SRC, n + ".py"))}
    assert external == {"requests"}, external
@check("dependencies: analysis.py imports only math, statistics and datetime")
def _():
    names = set()
    for node in ast.walk(ast.parse(open(os.path.join(SRC, "analysis.py"), encoding="utf-8").read())):
        if isinstance(node, ast.Import): names |= {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom): names.add(node.module.split(".")[0])
    assert names <= {"math", "statistics", "datetime"}, names

# ---------------------------------------------------------------- real saved Kraken replies, end to end
real_dir = temp_dir()
real_run = {}
@check("real replies: exit 0, prints the fetcher's summary lines and where the files were written")
def _():
    code, out, err = run_main(REAL, REAL_NOW, real_dir); real_run.update(code=code, out=out, err=err)
    assert code == 0, (code, err[:200]); assert "XBTUSD: fetched 721 candles" in out and "excluded 1 unfinished candle(s) dated 2026-10-27; 720 completed candles remain" in out
    assert "Report written" in out and "Data written" in out
@check("real replies: exactly two files, named by the last COMPLETED date (2026-10-26), no temporary or backup files left")
def _(): assert files_in(real_dir) == ["report_2026-10-26.md", "report_2026-10-26_data.csv"], files_in(real_dir)
@check("real replies: the CSV has 31 consecutive daily rows 2026-09-26 .. 2026-10-26; the first row has no returns; 30 returns per asset")
def _():
    rows = csv_rows(real_dir, "report_2026-10-26_data.csv")
    assert [r["date"] for r in rows][0] == "2026-09-26" and rows[-1]["date"] == "2026-10-26" and len(rows) == 31
    assert rows[0]["btc_return"] == "" and rows[0]["eth_return"] == "" and all(r["btc_return"] != "" for r in rows[1:])
    dates = [datetime.strptime(r["date"], "%Y-%m-%d") for r in rows]; assert all((b - a).days == 1 for a, b in zip(dates, dates[1:]))
@check("real replies: the unfinished 2026-10-27 candle appears nowhere in the CSV or the report's data table")
def _():
    assert "2026-10-27" not in read(real_dir, "report_2026-10-26_data.csv")
    report = read(real_dir, "report_2026-10-26.md"); table = report.split("## Data table")[1].split("## ")[0]; assert "2026-10-27" not in table
@check("real replies: CSV returns equal the independent 60-digit calculation from the CSV closes (1e-12), for both assets")
def _():
    rows = csv_rows(real_dir, "report_2026-10-26_data.csv")
    for asset in ("btc", "eth"):
        expected = ind.returns([float(r[f"{asset}_close"]) for r in rows])
        assert all(abs(float(r[f"{asset}_return"]) - float(e)) <= 1e-12 for r, e in zip(rows[1:], expected))
@check("real replies: the report's correlation, volatilities and drawdowns equal the independent calculation (to the digits printed)")
def _():
    rows = csv_rows(real_dir, "report_2026-10-26_data.csv"); report = read(real_dir, "report_2026-10-26.md")
    rb, re_ = ind.returns([float(r["btc_close"]) for r in rows]), ind.returns([float(r["eth_close"]) for r in rows])
    rho = re.search(r"ρ = Sxy / √\(Sxx · Syy\) = (-?\d\.\d+)", report); assert rho, "rho line missing"
    assert rho.group(1) == f"{float(ind.correlation(rb, re_)):.6f}", (rho.group(1), float(ind.correlation(rb, re_)))
    sums = ind.sums(rb, re_)
    for label, key in (("x̄", "mean_x"), ("ȳ", "mean_y"), ("Sxx", "sxx"), ("Syy", "syy"), ("Sxy", "sxy")):
        match = re.search(rf"{re.escape(label)}[^=\n]*= (-?[\d.e+-]+)", report); assert match, label
        assert abs(float(match.group(1)) - float(sums[key])) <= 1e-9 * max(1.0, abs(float(sums[key]))), (label, match.group(1), float(sums[key]))
    vol = re.search(r"Daily volatility[^|\n]*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%", report); assert vol
    assert (vol.group(1), vol.group(2)) == (f"{float(ind.sample_std(rb)) * 100:.4f}", f"{float(ind.sample_std(re_)) * 100:.4f}")
    dd = re.search(r"Maximum drawdown[^|\n]*\|\s*(-?[\d.]+)%", report); assert dd and dd.group(1) == f"{float(ind.max_drawdown([float(r['btc_close']) for r in rows])) * 100:.2f}"
@check("real replies: the report states the window, counts and excluded candle")
def _():
    report = read(real_dir, "report_2026-10-26.md")
    assert "Return dates: 2026-09-27 to 2026-10-26 (30 observations from 31 daily closes, 2026-09-26 to 2026-10-26)" in report
    assert "Unfinished candle excluded: 2026-10-27" in report and "Data fetched: 2026-10-27 09:41:00 UTC" in report

# ---------------------------------------------------------------- synthetic scenarios
@check("synthetic: the unfinished 'today' candle (close 999999.0) is excluded: absent from CSV and report; its date is stated")
def _():
    d = temp_dir(); bodies, btc, eth, days_ = synthetic_bodies(); code, out, err = run_main(bodies, NOW, d)
    assert code == 0, err; name = [f for f in files_in(d) if f.endswith(".md")][0]
    assert "999999" not in read(d, name) and "999999" not in read(d, name.replace(".md", "_data.csv")) and "Unfinished candle excluded: 2026-03-20" in read(d, name)
@check("synthetic: the CSV closes are exactly the last 31 completed closes of both assets")
def _():
    d = temp_dir(); bodies, btc, eth, days_ = synthetic_bodies(); run_main(bodies, NOW, d)
    rows = csv_rows(d, "report_2026-03-19_data.csv"); assert [float(r["btc_close"]) for r in rows] == btc[-31:] and [float(r["eth_close"]) for r in rows] == eth[-31:]
@check("failure: ETH fetch fails (HTTP 403) -> exit 1, ERROR on stderr, nothing written, no directory created")
def _():
    d = os.path.join(temp_dir(), "reports"); bodies, *_ = synthetic_bodies(); code, out, err = run_main(bodies, NOW, d, failing_pair="ETHUSD")
    assert code == 1 and err.startswith("ERROR:") and not os.path.exists(d), (code, err[:100])
@check("failure: a missing day inside BTC's data -> exit 1, validation message, nothing written")
def _():
    d = temp_dir(); bodies, *_ = synthetic_bodies(drop_btc_day=30); code, out, err = run_main(bodies, NOW, d)
    assert code == 1 and "missing day" in err and files_in(d) == [], err[:200]
@check("failure: only 30 completed days -> exit 1 (needs 31), nothing written")
def _():
    d = temp_dir(); bodies, *_ = synthetic_bodies(count=30); code, out, err = run_main(bodies, NOW, d)
    assert code == 1 and "need at least 31" in err and files_in(d) == []
@check("failure: ETH data ends a day earlier than BTC's (dates misaligned) -> exit 1, nothing written")
def _():
    d = temp_dir(); bodies, *_ = synthetic_bodies(eth_end_offset=-1); code, out, err = run_main(bodies, NOW, d)
    assert code == 1 and files_in(d) == [] and ("missing from" in err or "after today" in err or "need at least" in err), err[:200]
@check("failure: a zero closing price -> exit 1 with a clear message, nothing written")
def _():
    d = temp_dir(); btc = random_closes(40, 5); btc[35] = 0.0; bodies, *_ = synthetic_bodies(btc=btc); code, out, err = run_main(bodies, NOW, d)
    assert code == 1 and "positive" in err and files_in(d) == [], err[:200]
@check("constant ETH prices: exit 0, the report says correlation is undefined and R1 is not computable")
def _():
    d = temp_dir(); bodies, *_ = synthetic_bodies(eth=[3000.0] * 40); code, out, err = run_main(bodies, NOW, d); assert code == 0, err
    report = read(d, "report_2026-03-19.md"); assert "undefined" in report and "not computable" in report

# ---------------------------------------------------------------- existing files are never damaged
SENTINEL_REPORT, SENTINEL_CSV = "OLD REPORT ✓\n", "old,csv\n"
def seeded_dir():
    d = temp_dir(); open(os.path.join(d, "report_2026-03-19.md"), "w", encoding="utf-8", newline="").write(SENTINEL_REPORT)
    open(os.path.join(d, "report_2026-03-19_data.csv"), "w", encoding="utf-8", newline="").write(SENTINEL_CSV); return d
def untouched(d): return read(d, "report_2026-03-19.md") == SENTINEL_REPORT and read(d, "report_2026-03-19_data.csv") == SENTINEL_CSV and len(files_in(d)) == 2
@check("preserve: a validation failure leaves an existing report and CSV byte-identical")
def _(): d = seeded_dir(); bodies, *_ = synthetic_bodies(drop_btc_day=30); assert run_main(bodies, NOW, d)[0] == 1 and untouched(d)
@check("preserve: a fetch failure leaves existing files byte-identical")
def _(): d = seeded_dir(); bodies, *_ = synthetic_bodies(); assert run_main(bodies, NOW, d, failing_pair="XBTUSD")[0] == 1 and untouched(d)
@check("preserve: an analysis failure (zero close) leaves existing files byte-identical")
def _():
    d = seeded_dir(); btc = random_closes(40, 5); btc[35] = 0.0; bodies, *_ = synthetic_bodies(btc=btc); assert run_main(bodies, NOW, d)[0] == 1 and untouched(d)
for failing_call, description in ((1, "moving the old report aside"), (2, "putting the new report in place"), (3, "moving the old CSV aside"), (4, "putting the new CSV in place")):
    @check(f"preserve: the file replacement fails at call {failing_call} ({description}) -> exit 1, both old files restored byte-identical, no .tmp/.bak left")
    def _(failing_call=failing_call):
        d = seeded_dir(); bodies, *_ = synthetic_bodies(); calls = {"n": 0}; real_replace = os.replace
        def flaky(source, destination):
            calls["n"] += 1
            if calls["n"] == failing_call: raise OSError("simulated disk failure")
            return real_replace(source, destination)
        with mock.patch("os.replace", side_effect=flaky): code, out, err = run_main(bodies, NOW, d)
        assert code == 1 and "ERROR" in err and untouched(d), (code, err[:160], files_in(d))
@check("preserve: the temporary file cannot be written (disk full simulated) -> exit 1, existing files untouched")
def _():
    d = seeded_dir(); bodies, *_ = synthetic_bodies()
    with mock.patch("pathlib.Path.write_text", side_effect=OSError("simulated: no space left")): code, out, err = run_main(bodies, NOW, d)
    assert code == 1 and untouched(d)
@check("success: a second successful run for the same date replaces both files; nothing else remains")
def _():
    d = seeded_dir(); bodies, *_ = synthetic_bodies(); code, out, err = run_main(bodies, NOW, d)
    assert code == 0 and read(d, "report_2026-03-19.md") != SENTINEL_REPORT and read(d, "report_2026-03-19_data.csv").startswith("date,btc_close,eth_close,btc_return,eth_return") and len(files_in(d)) == 2
@check("success: a missing report folder is created")
def _():
    d = os.path.join(temp_dir(), "new_reports"); bodies, *_ = synthetic_bodies(); assert run_main(bodies, NOW, d)[0] == 0 and len(files_in(d)) == 2
@check("safety: file names are built from the date only (pattern report_YYYY-MM-DD.md / _data.csv)")
def _(): assert len(files_in(real_dir)) == 2 and all(re.fullmatch(r"report_\d{4}-\d{2}-\d{2}(_data\.csv|\.md)", f) for f in files_in(real_dir))
for d in cleanup: shutil.rmtree(d, ignore_errors=True)
R.finish()
