"""Regression tests for finding C1: the test suite must never write into the real GitHub Actions job summary or step-output file.
GitHub Actions gives every step two files, named by GITHUB_STEP_SUMMARY (shown as the run's job summary) and GITHUB_OUTPUT (step outputs). The client (src/daily_automation.py) appends to them whenever it finds
those variables in its environment. A test that starts the client as a child process with an environment copied from its parent therefore writes SIMULATED results (an upsert that "inserted 2 rows", a success
email that was "sent" ...) into the REAL job summary, next to the real results of the real steps.
Everything here uses temporary files in place of the real ones, and controlled environment variables; no real file, secret or network is involved."""
import atexit, contextlib, io, json, os, shutil, subprocess, sys, tempfile
from datetime import date

from wf_support import Runner, REPO, load

da = load("daily_automation")
REAL_REPORTS = os.path.join(REPO, "reports")
WORK = tempfile.mkdtemp(prefix="ci_env_"); atexit.register(shutil.rmtree, WORK, True)
R = Runner("job-summary isolation checks")
VARIABLES = ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT")
SENTINEL = b"untouched sentinel\n"

def watched_files(name):
    """Two temporary files standing in for the runner's summary and output files; each starts with a sentinel so that ANY write is visible."""
    folder = os.path.join(WORK, name); os.makedirs(folder); paths = {variable: os.path.join(folder, variable.lower() + ".txt") for variable in VARIABLES}
    for path in paths.values(): open(path, "wb").write(SENTINEL)
    return paths
def appended(paths): return {variable: len(open(path, "rb").read()) - len(SENTINEL) for variable, path in paths.items()}      # bytes added to each file (0 = untouched)
def untouched(paths): return all(open(path, "rb").read() == SENTINEL for path in paths.values())

PYTHON_PROBE = ("import json, os, sys\n"
                "seen = {name: (name in os.environ) for name in ('GITHUB_STEP_SUMMARY', 'GITHUB_OUTPUT', 'PYTHONUTF8', 'PATH')}\n"
                "open(os.path.join('observed', os.path.basename(sys.argv[0]) + '.json'), 'w').write(json.dumps(seen))\n")
NODE_PROBE = ('import fs from "node:fs";\n'
              'const seen = Object.fromEntries(["GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "PYTHONUTF8", "PATH"].map((name) => [name, name in process.env]));\n'
              'fs.writeFileSync("observed/probe.mjs.json", JSON.stringify(seen));\n')

@R.register("run_all: no suite it starts - Python or Node - can see GITHUB_STEP_SUMMARY or GITHUB_OUTPUT, even when the process running run_all has both; the rest of the environment is passed on")
def _():
    import run_all                                                         # tests/ is on sys.path; the module only runs when called
    probe_repo = os.path.join(WORK, "probe_repo"); os.makedirs(os.path.join(probe_repo, "tests")); os.makedirs(os.path.join(probe_repo, "observed"))
    open(os.path.join(probe_repo, "tests", "probe.py"), "w", encoding="utf-8").write(PYTHON_PROBE); open(os.path.join(probe_repo, "tests", "probe.mjs"), "w", encoding="utf-8").write(NODE_PROBE)
    node = shutil.which("node"); files = watched_files("run_all")
    saved_environment = {name: os.environ.get(name) for name in VARIABLES}; saved_module = (run_all.REPO, run_all.PYTHON_SUITES, run_all.NODE_SUITES)
    try:
        os.environ.update(GITHUB_STEP_SUMMARY=files["GITHUB_STEP_SUMMARY"], GITHUB_OUTPUT=files["GITHUB_OUTPUT"])
        run_all.REPO, run_all.PYTHON_SUITES, run_all.NODE_SUITES = probe_repo, ["probe.py"], (["probe.mjs"] if node else [])
        with contextlib.redirect_stdout(io.StringIO()): code = run_all.main()
    finally:
        run_all.REPO, run_all.PYTHON_SUITES, run_all.NODE_SUITES = saved_module
        for name, value in saved_environment.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value
    expected = {"GITHUB_STEP_SUMMARY": False, "GITHUB_OUTPUT": False, "PYTHONUTF8": True, "PATH": True}
    seen = {probe: json.load(open(os.path.join(probe_repo, "observed", probe + ".json"), encoding="utf-8")) for probe in (["probe.py"] + (["probe.mjs"] if node else []))}
    assert code == 0, f"the probe suites did not run (exit {code})"
    for probe, observed in seen.items(): assert observed == expected, f"{probe} saw {observed}"
    assert untouched(files), f"run_all wrote to the watched files: {appended(files)}"

@R.register("the isolation suite - the one that starts the real client as child processes - leaves both watched files untouched and still passes 7/7, when it is started with both variables set")
def _():
    files = watched_files("isolation_suite")
    env = dict(os.environ, GITHUB_STEP_SUMMARY=files["GITHUB_STEP_SUMMARY"], GITHUB_OUTPUT=files["GITHUB_OUTPUT"], PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, os.path.join(REPO, "tests", "test_python_isolation.py")], cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0 and "7/7 python isolation checks passed" in done.stdout, f"the suite itself no longer passes (exit {done.returncode}): {done.stdout[-300:]}"
    assert untouched(files), f"the suite appended to the watched files: {appended(files)} bytes ({open(files['GITHUB_STEP_SUMMARY'], encoding='utf-8').read().count('### ')} summary sections)"

@R.register("production behaviour is intact: the client still appends to the summary file and the output file named in the environment it is given, and does nothing when they are absent")
def _():
    files = watched_files("production")
    da.append_summary({"GITHUB_STEP_SUMMARY": files["GITHUB_STEP_SUMMARY"]}, ["### Daily Market Analysis: probe", "- line"]); added = open(files["GITHUB_STEP_SUMMARY"], "rb").read()[len(SENTINEL):]
    assert added == b"### Daily Market Analysis: probe\n- line\n", added
    with contextlib.redirect_stdout(io.StringIO()): code = da.main(["validate", "--reports-dir", REAL_REPORTS], env={"GITHUB_OUTPUT": files["GITHUB_OUTPUT"]}, now=date(2026, 10, 28))
    assert code == 0 and open(files["GITHUB_OUTPUT"], "rb").read()[len(SENTINEL):] == b"analysis_date=2026-10-27\n"
    before = appended(files); da.append_summary({}, ["nothing should happen"]); assert appended(files) == before, "no variable, no write"

R.finish()
