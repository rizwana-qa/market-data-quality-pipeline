"""Stage 1 tests for the dashboard step of the daily workflow (written BEFORE the integration exists).
The workflow will run, on a runner whose checkout also holds COMMITTED historical files:  src/run_workflow.py (fresh report + CSV)  ->  daily_automation.py validate  ->  python3 src/dashboard.py --reports-dir reports --out-dir dashboard_out
->  daily_automation.py stage-deliverables ... --date <validated date>.  Most checks here exercise the EXISTING dashboard code through exactly that command line and must already pass; the two checks that name
'stage-deliverables' specify the NEW contract and fail until it is implemented (Stage 2).
Everything is offline: Kraken is replaced by the saved replies in tests/fixtures (current run, 2026-10-26) and by synthetic replies (an older 'historical' run, 2026-03-19). No network, no secret, nothing outside temporary folders is touched."""
import atexit, contextlib, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from wf_support import Runner, REPO, REAL, REAL_NOW, NOW, synthetic_bodies, run_main, files_in, read, load

dashboard, da = load("dashboard"), load("daily_automation")
R = Runner("dashboard integration checks")
WORK = tempfile.mkdtemp(prefix="di_")
def _remove(function, path, error):
    os.chmod(path, 0o700); function(path)
atexit.register(shutil.rmtree, WORK, False, _remove)
GENERATED_AT = datetime(2026, 10, 27, 9, 45, tzinfo=timezone.utc)
CURRENT, HISTORICAL = "2026-10-26", "2026-03-19"        # the analysis date of the current run, and of an older run that must never be taken for it
COLUMNS = ["date", "btc_close", "eth_close", "btc_return", "eth_return", "btc_open", "btc_high", "btc_low", "btc_volume", "btc_vwap", "btc_trade_count", "eth_open", "eth_high", "eth_low", "eth_volume", "eth_vwap", "eth_trade_count"]

_gold = {}
def gold():
    """Report + CSV + dashboard for the current run and for the historical run, built once; every check works on private copies."""
    if not _gold:
        base = tempfile.mkdtemp(dir=WORK)
        for label, (bodies, now) in {"current": (REAL, REAL_NOW), "historical": (synthetic_bodies()[0], NOW)}.items():
            reports, out = os.path.join(base, label, "reports"), os.path.join(base, label, "dashboard_out")
            code, _stdout, error = run_main(bodies, now, reports); assert code == 0, f"the fixture run for {label} failed: {error[-200:]}"
            os.makedirs(out); dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
        _gold["base"] = base
    return _gold["base"]
def private_copy(label, part):
    target = os.path.join(tempfile.mkdtemp(dir=WORK), part); shutil.copytree(os.path.join(gold(), label, part), target); return target
def digest(path): return hashlib.sha256(open(path, "rb").read()).hexdigest()
def tree(*folders): return {os.path.join(folder, name): digest(os.path.join(folder, name)) for folder in folders if os.path.isdir(folder) for name in sorted(os.listdir(folder))}
def payload_of(html):
    found = re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>', html, re.S); assert found, "the page has no embedded data element"
    return json.loads(found.group(1))
def stage(reports, dash, out, day):
    """The NEW command, called the way the workflow will call it. Returns (exit code, all output)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = da.main(["stage-deliverables", "--reports-dir", reports, "--dashboard-dir", dash, "--out-dir", out, "--date", day], env={}, now=date(2026, 10, 27))
    if code == 2 and "invalid choice" in buffer.getvalue(): raise AssertionError("MISSING FEATURE: daily_automation.py has no stage-deliverables command yet (argparse: invalid choice)")
    return code, buffer.getvalue()
def edit(path, change):
    text = open(path, encoding="utf-8", newline="").read(); new = change(text); assert new != text, "the edit changed nothing (the test would prove nothing)"
    open(path, "w", encoding="utf-8", newline="").write(new)

@R.register("a valid current-run CSV produces dashboard_<analysis date>.html in a SEPARATE output folder; the reports folder gets no dashboard and no temporary file")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out); before = tree(reports)
    path = dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    assert Path(path) == Path(out) / f"dashboard_{CURRENT}.html", path
    assert files_in(out) == [f"dashboard_{CURRENT}.html"], files_in(out)
    assert tree(reports) == before, "the reports folder changed (a dashboard or temporary file was written there)"

@R.register("the dashboard carries the current analysis date, names the CSV and the report of the same run, holds the 17 columns and the run's own data timestamp")
def _():
    reports, out = private_copy("current", "reports"), private_copy("current", "dashboard_out")
    meta = payload_of(read(out, f"dashboard_{CURRENT}.html"))["meta"]; data = payload_of(read(out, f"dashboard_{CURRENT}.html"))
    assert meta["close_period"]["end"] == CURRENT and meta["return_period"]["end"] == CURRENT, meta
    assert meta["csv_file"] == f"report_{CURRENT}_data.csv" and meta["report_file"] == f"report_{CURRENT}.md", meta
    assert data["columns"] == COLUMNS and data["rows"][-1][0] == CURRENT and len(data["rows"]) == 31, (data["columns"], len(data["rows"]))
    assert meta["generated_at"] == "2026-10-27 09:45:00 UTC"
    fetched = re.search(r"^- Data fetched: (.+)$", read(reports, f"report_{CURRENT}.md"), re.M).group(1); assert meta["data_fetched"] == fetched, (meta["data_fetched"], fetched)

@R.register("the exact workflow command (python3 src/dashboard.py --reports-dir reports --out-dir dashboard_out) writes one dashboard and exits 0; run again it exits 1 and leaves the first dashboard unchanged")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}; environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    command = [sys.executable, os.path.join(REPO, "src", "dashboard.py"), "--reports-dir", reports, "--out-dir", out]
    first = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120); assert first.returncode == 0 and "Dashboard written" in first.stdout, (first.returncode, first.stderr[-200:])
    assert files_in(out) == [f"dashboard_{CURRENT}.html"]; fingerprint = digest(os.path.join(out, files_in(out)[0]))
    second = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120); assert second.returncode == 1 and "will not be overwritten" in second.stderr, (second.returncode, second.stderr[-200:])
    assert digest(os.path.join(out, files_in(out)[0])) == fingerprint and files_in(out) == [f"dashboard_{CURRENT}.html"]

@R.register("an existing dashboard is never overwritten, and a refused attempt leaves no temporary file next to it")
def _():
    reports, out = private_copy("current", "reports"), private_copy("current", "dashboard_out"); target = os.path.join(out, f"dashboard_{CURRENT}.html"); before = digest(target)
    try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    except dashboard.DashboardInputError as error: assert "already exists" in str(error), str(error)
    else: raise AssertionError("an existing dashboard was overwritten without an error")
    assert digest(target) == before and files_in(out) == [f"dashboard_{CURRENT}.html"], files_in(out)

@R.register("a missing CSV, an empty reports folder and a missing reports folder fail safely: DashboardInputError, exit code 1 on the command line, nothing written")
def _():
    out = os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    no_csv = private_copy("current", "reports"); os.remove(os.path.join(no_csv, f"report_{CURRENT}_data.csv"))
    empty = os.path.join(tempfile.mkdtemp(dir=WORK), "reports"); os.makedirs(empty)
    for label, reports in (("missing CSV", no_csv), ("empty folder", empty), ("missing folder", os.path.join(WORK, "does_not_exist"))):
        try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
        except dashboard.DashboardInputError: pass
        else: raise AssertionError(f"{label}: no error was raised")
        assert dashboard.main(["--reports-dir", reports, "--out-dir", out]) == 1, label
    assert files_in(out) == [], files_in(out)

@R.register("a missing output folder fails safely and is NOT created by the generator (so the workflow step must create dashboard_out itself, e.g. mkdir -p)")
def _():
    reports = private_copy("current", "reports"); out = os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out")
    try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    except dashboard.DashboardInputError: pass
    else: raise AssertionError("no error for a missing output folder")
    assert not os.path.exists(out) and dashboard.main(["--reports-dir", reports, "--out-dir", out]) == 1

@R.register("an invalid CSV is refused and nothing is written: wrong header, text instead of a number, a gap in the dates, too few rows")
def _():
    name = f"report_{CURRENT}_data.csv"; lines_of = lambda text: text.split("\n")
    breaks = {"wrong header": lambda t: t.replace("btc_close", "btc_closing", 1), "text instead of a number": lambda t: re.sub(r"(\n[^\n]*)$", "", t.rstrip("\n")) + "\n" + ",".join(["2026-10-26", "abc"] + t.rstrip("\n").split("\n")[-1].split(",")[2:]) + "\n",
              "gap in the dates": lambda t: "\n".join(lines_of(t)[:10] + lines_of(t)[11:]), "too few rows": lambda t: "\n".join(lines_of(t)[:6]) + "\n"}
    for label, change in breaks.items():
        reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out); edit(os.path.join(reports, name), change); before = tree(reports)
        try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
        except dashboard.DashboardInputError: pass
        else: raise AssertionError(f"{label}: an invalid CSV produced a dashboard")
        assert files_in(out) == [] and tree(reports) == before, label

@R.register("a report that disagrees with its CSV is refused (changed correlation, or the report of a different run under the current name): the dashboard never contradicts the report")
def _():
    out = os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    changed = private_copy("current", "reports"); edit(os.path.join(changed, f"report_{CURRENT}.md"), lambda t: re.sub(r"(Pearson correlation of the 30 aligned daily returns: )[-0-9.]+", r"\g<1>0.123456", t))
    swapped = private_copy("current", "reports"); shutil.copy(os.path.join(gold(), "historical", "reports", f"report_{HISTORICAL}.md"), os.path.join(swapped, f"report_{CURRENT}.md"))
    for label, reports in (("changed correlation", changed), ("another run's report", swapped)):
        try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
        except dashboard.DashboardInputError as error: assert "report" in str(error).lower(), str(error)
        else: raise AssertionError(f"{label}: a dashboard was produced")
    assert files_in(out) == []

@R.register("a CSV whose file name date differs from its last row is refused (the name and the content must agree on the analysis date)")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    for suffix in (".md", "_data.csv"): os.rename(os.path.join(reports, f"report_{CURRENT}{suffix}"), os.path.join(reports, f"report_2026-10-27{suffix}"))
    try: dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    except dashboard.DashboardInputError as error: assert "2026-10-27" in str(error) and CURRENT in str(error), str(error)
    else: raise AssertionError("a mislabelled CSV produced a dashboard")
    assert files_in(out) == []

@R.register("when the current-run CSV and a historical CSV are both present, the NEWEST (current) one is used; the historical files are not modified")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    for name in os.listdir(os.path.join(gold(), "historical", "reports")): shutil.copy(os.path.join(gold(), "historical", "reports", name), reports)
    old = {name: digest(os.path.join(reports, name)) for name in files_in(reports) if HISTORICAL in name}; assert len(old) == 2, old
    path = dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out); assert Path(path).name == f"dashboard_{CURRENT}.html", path
    assert {name: digest(os.path.join(reports, name)) for name in files_in(reports) if HISTORICAL in name} == old

@R.register("KNOWN LIMIT: dashboard.py alone takes the newest CSV it finds, so with only a historical pair present it builds a HISTORICAL dashboard - the cleanup step and the staging date check exist to stop that being published")
def _():
    reports, out = private_copy("historical", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    path = dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out); assert Path(path).name == f"dashboard_{HISTORICAL}.html" and CURRENT not in Path(path).name, path

@R.register("the existing validate step refuses a reports folder that holds only a historical pair, so in the workflow build_dashboard is never reached with stale input")
def _():
    reports = private_copy("historical", "reports")
    try: da.validate_outputs(reports, date(2026, 10, 27))
    except da.AutomationError as error: assert "2026-10-26" in str(error), str(error)
    else: raise AssertionError("validate accepted a historical pair as the output of the current run")

@R.register("a committed historical dashboard in the reports folder is not mistaken for the new one: the new file is written only to the output folder and the committed copy stays byte-identical")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    committed = os.path.join(reports, f"dashboard_{CURRENT}.html"); open(committed, "w", encoding="utf-8", newline="\n").write("<html>committed historical copy</html>\n"); before = digest(committed)
    path = dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    assert Path(path).parent == Path(out) and digest(committed) == before and digest(path) != before and files_in(out) == [f"dashboard_{CURRENT}.html"]

@R.register("pipeline order on one run: validate_outputs reports the analysis date, and the dashboard built afterwards from the same folder has exactly that date")
def _():
    reports, out = private_copy("current", "reports"), os.path.join(tempfile.mkdtemp(dir=WORK), "dashboard_out"); os.makedirs(out)
    validated = da.validate_outputs(reports, date(2026, 10, 27))["date"]; path = dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
    assert validated == CURRENT and Path(path).name == f"dashboard_{validated}.html" and payload_of(open(path, encoding="utf-8").read())["meta"]["close_period"]["end"] == validated

@R.register("NEW CONTRACT - a dashboard built for the current run is refused by stage-deliverables when the pipeline's validated analysis date is a different day (exit 1, nothing staged)")
def _():
    reports, dash = private_copy("current", "reports"), private_copy("current", "dashboard_out"); out = os.path.join(tempfile.mkdtemp(dir=WORK), "deliverables")
    code, text = stage(reports, dash, out, "2026-10-25"); assert code == 1, f"expected exit 1 (rejected), got {code}: {text.strip()[-160:]}"
    assert "2026-10-25" in text and not os.path.exists(out), (text.strip()[-160:], os.path.exists(out))

@R.register("NEW CONTRACT - a historical CSV and dashboard cannot be staged as the current run: with only the historical files present stage-deliverables refuses the current date (exit 1, nothing staged)")
def _():
    reports, dash = private_copy("historical", "reports"), private_copy("historical", "dashboard_out"); out = os.path.join(tempfile.mkdtemp(dir=WORK), "deliverables")
    code, text = stage(reports, dash, out, CURRENT); assert code == 1, f"expected exit 1 (rejected), got {code}: {text.strip()[-160:]}"
    assert CURRENT in text and not os.path.exists(out), (text.strip()[-160:], os.path.exists(out))

R.finish()
