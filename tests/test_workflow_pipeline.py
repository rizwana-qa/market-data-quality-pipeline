"""Stage 3 pipeline simulation: the daily workflow's own run scripts, executed in order in a temporary workspace, offline.
What a static check of the YAML cannot show is whether the STEPS WORK TOGETHER: that the cleanup really clears the checked-out stale files, that the dashboard comes from this run's CSV, that the date the validation step
reports is the date the staging step receives, and that exactly the three matching files end up in deliverables/. So the scripts of clear_stale_outputs, build_dashboard and stage_deliverables are taken from
.github/workflows/daily-market-analysis.yml and run VERBATIM with bash (-eo pipefail, as GitHub does). Only two things are stood in for: the analysis (Kraken is replaced by the saved replies in tests/fixtures,
as in the other suites) and the validation step (run in-process with a fixed clock and a GITHUB_OUTPUT file, so that its analysis_date output is read the way GitHub reads it).
`python3` is a small shim to the interpreter that runs this test. This is a local simulation: it is NOT a GitHub Actions run and says nothing about Ubuntu runner behaviour."""
import atexit, hashlib, os, re, shutil, stat, subprocess, sys, tempfile
from datetime import date

from wf_support import Runner, REPO, REAL, REAL_NOW, run_main, load

da = load("daily_automation")
R = Runner("workflow pipeline checks")
WORK = tempfile.mkdtemp(prefix="wp_")
def _remove(function, path, error):
    os.chmod(path, 0o700); function(path)
atexit.register(shutil.rmtree, WORK, False, _remove)
TEXT = open(os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml"), encoding="utf-8", newline="").read()
CURRENT, STALE = "2026-10-26", "2026-10-27"          # this run's analysis date; the day of the files that are COMMITTED in the repository (and so present in every fresh checkout)
NAMES = lambda day: [f"report_{day}.md", f"report_{day}_data.csv", f"dashboard_{day}.html"]

def step_script(step_id):
    for block in re.split(r"\n(?=      - name: )", TEXT.split("    steps:\n", 1)[1]):
        if re.search(rf"^        id: {step_id}\s*$", block, re.M):
            found = re.search(r"^        run: \|\n((?:          .*\n?)+)", block, re.M); assert found, f"step {step_id} has no literal run block"
            return "\n".join(line[10:] for line in found.group(1).rstrip("\n").split("\n")) + "\n"
    raise AssertionError(f"MISSING FEATURE: the workflow has no step with id {step_id!r}")
def find_bash():
    found = shutil.which("bash")
    if found and "system32" not in found.lower(): return found
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.exists(candidate): return candidate
    raise AssertionError("bash was not found, so the workflow's scripts cannot be executed here")

SHIM = os.path.join(WORK, "shim"); os.makedirs(SHIM)
open(os.path.join(SHIM, "python3"), "w", newline="\n").write('#!/bin/sh\nexec "' + sys.executable.replace("\\", "/") + '" "$@"\n'); os.chmod(os.path.join(SHIM, "python3"), 0o755)
def run_step(ws, step_id, extra_env=None):
    """Run the workflow's own script for this step in the workspace. Returns (exit code, stdout + stderr)."""
    script = os.path.join(WORK, f"{step_id}_{len(os.listdir(WORK))}.sh"); open(script, "w", encoding="utf-8", newline="\n").write(step_script(step_id))
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    environment.update(PATH=SHIM + os.pathsep + os.environ.get("PATH", ""), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
    done = subprocess.run([find_bash(), "--noprofile", "--norc", "-eo", "pipefail", script.replace("\\", "/")], cwd=ws, env=environment, capture_output=True, text=True, timeout=300)
    return done.returncode, done.stdout + done.stderr

def checkout():
    """A fresh checkout: the source folder and the COMMITTED historical report, CSV and dashboard of 2026-10-27."""
    ws = tempfile.mkdtemp(dir=WORK); shutil.copytree(os.path.join(REPO, "src"), os.path.join(ws, "src"), ignore=shutil.ignore_patterns("__pycache__")); os.makedirs(os.path.join(ws, "reports"))
    for name in NAMES(STALE): shutil.copy(os.path.join(REPO, "reports", name), os.path.join(ws, "reports", name))
    return ws
def analysis(ws):
    """The analysis step, offline: this run's report and CSV (dated 2026-10-26) are written next to the committed files."""
    code, _out, error = run_main(REAL, REAL_NOW, os.path.join(ws, "reports")); assert code == 0, error[-300:]
def validate(ws):
    """The validation step, in-process with a fixed clock; returns (exit code, the analysis_date output as GitHub would read it from GITHUB_OUTPUT)."""
    output = os.path.join(ws, "github_output.txt"); open(output, "w").close()
    code = da.main(["validate", "--reports-dir", os.path.join(ws, "reports")], env={"GITHUB_OUTPUT": output}, now=date(2026, 10, 27))
    found = re.findall(r"^analysis_date=(.*)$", open(output, encoding="utf-8").read(), re.M); return code, (found[-1] if found else None)
def digest(path): return hashlib.sha256(open(path, "rb").read()).hexdigest()
def listing(folder): return sorted(os.listdir(folder)) if os.path.isdir(folder) else None

@R.register("the whole path works: cleanup, this run's analysis and validation, dashboard, staging - exactly the three files of the validated date end up in deliverables/, byte-identical to their sources")
def _():
    ws = checkout(); code, text = run_step(ws, "clear_stale_outputs"); assert code == 0, text
    assert listing(os.path.join(ws, "reports")) == [], f"the committed stale files are still in the checkout: {listing(os.path.join(ws, 'reports'))}"
    analysis(ws); code, analysis_date = validate(ws); assert code == 0 and analysis_date == CURRENT, (code, analysis_date)
    code, text = run_step(ws, "build_dashboard"); assert code == 0, text
    assert listing(os.path.join(ws, "dashboard_out")) == [f"dashboard_{CURRENT}.html"], listing(os.path.join(ws, "dashboard_out"))
    code, text = run_step(ws, "stage_deliverables", {"ANALYSIS_DATE": analysis_date}); assert code == 0, text
    assert listing(os.path.join(ws, "deliverables")) == sorted(NAMES(CURRENT)), listing(os.path.join(ws, "deliverables"))
    for name in NAMES(CURRENT):
        source = os.path.join(ws, "dashboard_out" if name.startswith("dashboard") else "reports", name); assert digest(os.path.join(ws, "deliverables", name)) == digest(source), name
    assert not any(STALE in name for folder in ("reports", "dashboard_out", "deliverables") for name in listing(os.path.join(ws, folder))), "a stale file of the committed day is present"

@R.register("the committed dashboard is not touched by the pipeline and the dashboard of the run is a NEW file: the staged dashboard names this run's CSV and report")
def _():
    ws = checkout(); committed = digest(os.path.join(ws, "reports", f"dashboard_{STALE}.html")); run_step(ws, "clear_stale_outputs"); analysis(ws); code, analysis_date = validate(ws)
    assert run_step(ws, "build_dashboard")[0] == 0 and run_step(ws, "stage_deliverables", {"ANALYSIS_DATE": analysis_date})[0] == 0
    page = open(os.path.join(ws, "deliverables", f"dashboard_{CURRENT}.html"), encoding="utf-8").read()
    assert f'"csv_file":"report_{CURRENT}_data.csv"' in page and f'"report_file":"report_{CURRENT}.md"' in page and digest(os.path.join(ws, "deliverables", f"dashboard_{CURRENT}.html")) != committed
    assert digest(os.path.join(REPO, "reports", f"dashboard_{STALE}.html")) == committed, "the committed dashboard in the repository changed"

@R.register("a run whose analysis produced nothing cannot be mistaken for success: after the cleanup the validation step fails (nothing to validate), so no later step - dashboard, staging, upload - would run")
def _():
    ws = checkout(); assert run_step(ws, "clear_stale_outputs")[0] == 0
    code, analysis_date = validate(ws); assert code == 1 and analysis_date is None, (code, analysis_date)
    assert listing(os.path.join(ws, "deliverables")) is None and listing(os.path.join(ws, "dashboard_out")) is None

@R.register("stale input is refused even if the cleanup were skipped: the committed (newer-dated, in this fixture) CSV hijacks the dashboard step, and staging refuses it because the dashboard is not this run's - nothing is staged")
def _():
    ws = checkout(); analysis(ws)                                         # NO cleanup: the committed 2026-10-27 files sit next to this run's 2026-10-26 files
    assert run_step(ws, "build_dashboard")[0] == 0 and listing(os.path.join(ws, "dashboard_out")) == [f"dashboard_{STALE}.html"], listing(os.path.join(ws, "dashboard_out"))
    code, text = run_step(ws, "stage_deliverables", {"ANALYSIS_DATE": CURRENT}); assert code != 0 and "ERROR" in text and listing(os.path.join(ws, "deliverables")) is None, (code, text[-300:])

@R.register("the staging step refuses the committed day as the analysis date, and leaves nothing behind (the stale files are gone after the cleanup, so there is nothing to stage)")
def _():
    ws = checkout(); run_step(ws, "clear_stale_outputs"); analysis(ws); assert run_step(ws, "build_dashboard")[0] == 0
    code, text = run_step(ws, "stage_deliverables", {"ANALYSIS_DATE": STALE}); assert code != 0 and "ERROR" in text and listing(os.path.join(ws, "deliverables")) is None, (code, text[-300:])

@R.register("the date reaches the staging command only as a quoted argument: shell metacharacters in ANALYSIS_DATE are never executed (no marker file appears), the command refuses it with one ERROR line, and nothing is staged")
def _():
    ws = checkout(); run_step(ws, "clear_stale_outputs"); analysis(ws); assert run_step(ws, "build_dashboard")[0] == 0; marker = os.path.join(ws, "EXECUTED")
    for hostile in (f"{CURRENT}; touch EXECUTED", "$(touch EXECUTED)", f'{CURRENT}" ; touch EXECUTED ; "', "`touch EXECUTED`", f"{CURRENT}\ntouch EXECUTED", f"{CURRENT}\n::error::injected"):
        code, text = run_step(ws, "stage_deliverables", {"ANALYSIS_DATE": hostile})
        assert code != 0 and not os.path.exists(marker) and listing(os.path.join(ws, "deliverables")) is None, (hostile, code, text[-200:])
        lines = text.splitlines(); assert len(lines) == 1 and lines[0].startswith("ERROR: ") and "::" not in text, (hostile, lines)

R.finish()
