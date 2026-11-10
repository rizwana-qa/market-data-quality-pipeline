"""Tests for remediation N1: the processes that hold the Google Apps Script secrets must not run third-party Python start-up code (.pth files, sitecustomize, usercustomize, PYTHONPATH).
Written BEFORE the workflow was changed. The malicious start-up code is SIMULATED: harmless hooks that append a label (and the DUMMY token, if they can see it) to a temporary file.
Nothing is installed: the poisoned interpreter is a throw-away venv created without pip, and every file lives in the system temp folder. No real secret and no network are used."""
import ast, atexit, contextlib, functools, http.server, io, json, os, re, shlex, shutil, site, subprocess, sys, tempfile, threading, venv
from datetime import date

from wf_support import Runner, REPO, load

da = load("daily_automation")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml")
DAILY = os.path.join(REPO, "src", "daily_automation.py")
REAL_REPORTS = os.path.join(REPO, "reports")
TOKEN = "dummy-token-for-isolation-tests-0001"          # made up, never a real secret
RUN_ID, RUN_URL = "123456789", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
SECRET_STEPS = ["post_to_sheet", "verify_sheet", "send_success_email", "notify_failure"]
SECRET_COMMANDS = ("post", "notify-failure", "self-check")
WORK = tempfile.mkdtemp(prefix="n1_isolation_"); atexit.register(shutil.rmtree, WORK, True)
R = Runner("python isolation checks")

# ---------------------------------------------------------------- reading the workflow
def workflow_steps():
    text = open(WORKFLOW, encoding="utf-8").read(); steps = []
    for block in re.split(r"\n(?=      - name: )", text.split("    steps:\n", 1)[1]):
        env = dict(re.findall(r"^          ([A-Za-z_]+): (.*)$", block, re.M)); run = re.search(r"^        run: \|\n((?:          .*(?:\n|$))+)", block, re.M)
        script = "\n".join(line[10:] for line in run.group(1).splitlines()) if run else ""
        steps.append({"id": re.search(r"^        id: (\S+)", block, re.M).group(1), "has_secrets": any("secrets." in value for value in env.values()),
                      "commands": [c.strip() for c in re.sub(r"\\\n\s*", " ", script).splitlines() if c.strip()]})
    return steps

# ---------------------------------------------------------------- the simulated poisoned interpreter
@functools.lru_cache(maxsize=None)
def poison():
    venv.EnvBuilder(with_pip=False).create(os.path.join(WORK, "venv"))
    py = os.path.join(WORK, "venv", "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
    purelib = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True, check=True).stdout.strip()
    hook = lambda label: f"import os; open(os.environ['EVIL_SINK'], 'a').write('{label} token=' + os.environ.get('GOOGLE_APPS_SCRIPT_TOKEN', '-') + '\\n')"
    open(os.path.join(purelib, "evil.pth"), "w", encoding="utf-8").write(hook("V1") + "\n")                                  # a .pth line that starts with 'import' is executed by the site module
    open(os.path.join(purelib, "sitecustomize.py"), "w", encoding="utf-8").write(hook("V2") + "\n")
    userbase = os.path.join(WORK, "userbase"); os.makedirs(userbase)
    user_site = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"], capture_output=True, text=True, env=dict(os.environ, PYTHONUSERBASE=userbase), check=True).stdout.strip()
    os.makedirs(user_site, exist_ok=True); open(os.path.join(user_site, "usercustomize.py"), "w", encoding="utf-8").write(hook("V3") + "\n")
    pp4 = os.path.join(WORK, "pp4"); os.makedirs(pp4); open(os.path.join(pp4, "sitecustomize.py"), "w", encoding="utf-8").write(hook("V4") + "\n")
    pp5 = os.path.join(WORK, "pp5"); os.makedirs(pp5); open(os.path.join(pp5, "json.py"), "w", encoding="utf-8").write(hook("V5") + "\nraise ImportError('shadow of the standard library')\n")
    vectors = {"V1": "a .pth file in site-packages", "V2": "sitecustomize.py in site-packages", "V4": "PYTHONPATH -> sitecustomize.py", "V5": "PYTHONPATH -> a file that shadows the standard-library json module"}
    configs = [(py, {}), (py, {"PYTHONPATH": pp4}), (py, {"PYTHONPATH": pp5})]                          # V1 and V2 sit in the venv and fire in every venv run
    if site.ENABLE_USER_SITE:                                                                            # a venv has no user site, so V3 needs the ordinary interpreter
        vectors["V3"] = "usercustomize.py in the user site"; configs.append((sys.executable, {"PYTHONUSERBASE": userbase}))
    return {"python": py, "vectors": dict(sorted(vectors.items())), "configs": configs}

def start(interpreter, flags, args, extra_env, cwd=None, url="https://invalid.example/not-a-webhook"):
    """Start a command with dummy secrets; returns (finished process, set of hook labels that fired, text the hooks captured)."""
    sink = os.path.join(WORK, "sink.txt"); open(sink, "w", encoding="utf-8").close(); os.makedirs(cwd or WORK, exist_ok=True)
    env = dict(os.environ, EVIL_SINK=sink, GOOGLE_APPS_SCRIPT_URL=url, GOOGLE_APPS_SCRIPT_TOKEN=TOKEN, PYTHONDONTWRITEBYTECODE="1", **extra_env)
    for name in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"): env.pop(name, None)       # the client appends to these files when it finds them: a test must never write into the real job summary
    done = subprocess.run([interpreter, *flags, *args], env=env, cwd=cwd or WORK, capture_output=True, text=True, timeout=120)
    captured = open(sink, encoding="utf-8").read(); return done, {line.split(" ")[0] for line in captured.splitlines() if line}, captured

# ================================================================ the workflow
@R.register("workflow: exactly four steps hold the secrets; each runs ONE python command, started as 'python3 -I -S src/daily_automation.py <post|notify-failure|self-check>'")
def _():
    steps = workflow_steps(); holders = [s for s in steps if s["has_secrets"]]; assert [s["id"] for s in holders] == SECRET_STEPS, [s["id"] for s in holders]
    for step in holders:
        python_lines = [c for c in step["commands"] if "python" in c]; assert len(python_lines) == 1, (step["id"], python_lines)
        assert re.match(r"python3 -I -S src/daily_automation\.py (post|notify-failure|self-check)\b", python_lines[0]), (step["id"], python_lines[0])

@R.register("workflow: no command that can read the secrets is started without -I -S, in any step")
def _():
    for step in workflow_steps():
        for command in step["commands"]:
            if re.search(r"daily_automation\.py (post|notify-failure|self-check)\b", command): assert re.match(r"python3 -I -S ", command), (step["id"], command)

@R.register("workflow: the steps without secrets are untouched (install, tests, analysis, validate, build-payload keep their plain 'python3' commands)")
def _():
    commands = {s["id"]: s["commands"][0] for s in workflow_steps() if s["commands"]}
    assert commands["install_dependencies"] == "python3 -m pip install --disable-pip-version-check -r requirements.txt" and commands["tests"] == "python3 tests/run_all.py" and commands["analysis"] == "python3 src/run_workflow.py"
    assert commands["validate_outputs"] == "python3 src/daily_automation.py validate --reports-dir reports" and commands["build_payload"].startswith("python3 src/daily_automation.py build-payload --reports-dir reports --out-dir automation_out")

# ================================================================ the code those commands run
@R.register("static: everything reachable from post, notify-failure and self-check imports only the standard library - never dashboard.py (found through the script folder) or a third-party package")
def _():
    tree = ast.parse(open(DAILY, encoding="utf-8").read()); functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    refers = {name: {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & set(functions) for name, node in functions.items()}
    reachable, todo = set(), ["command_post", "command_notify_failure", "command_self_check"]
    while todo:
        name = todo.pop()
        if name not in reachable: reachable.add(name); todo += refers[name]
    assert "_dashboard" not in reachable and "_email_report" not in reachable and not {"validate_outputs", "build_rows", "build_payloads", "build_report_payload", "command_validate", "command_build_payload"} & reachable, sorted(reachable)
    top = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert top <= set(sys.stdlib_module_names), sorted(top - set(sys.stdlib_module_names))
    inside = [(name, (a.name if isinstance(n, ast.Import) else n.module)) for name, node in functions.items() for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])]
    assert inside == [("_dashboard", "dashboard"), ("_email_report", "email_report")], inside        # Stage 4 added the second: the e-mail report code, loaded only by build-payload (never by a command that holds the secrets)

# ================================================================ the simulated attack
EXPECTED = {   # which start-up hook may run with which flags (True = runs)
    "V1": {"": True, "-S": False, "-I": True, "-I -S": False}, "V2": {"": True, "-S": False, "-I": True, "-I -S": False}, "V3": {"": True, "-S": False, "-I": False, "-I -S": False},
    "V4": {"": True, "-S": False, "-I": False, "-I -S": False}, "V5": {"": True, "-S": True, "-I": False, "-I -S": False}}
@R.register("premise: the simulated hooks are real - they run and capture the dummy token when started the old way, and it takes BOTH -I and -S to stop all of them")
def _():
    p = poison(); seen = {label: {} for label in p["vectors"]}
    for flags in ("", "-S", "-I", "-I -S"):
        fired = set()
        for interpreter, extra in p["configs"]:
            done, labels, captured = start(interpreter, flags.split(), [DAILY, "self-check"], extra); fired |= labels
            if flags == "": assert TOKEN in captured, "the control run must show a hook reading the dummy token"
        for label in seen: seen[label][flags] = label in fired
    print("    start-up hook                                                       | no flags | -S      | -I      | -I -S")
    for label, text in p["vectors"].items(): print(f"    {label} {text:66} | " + " | ".join(f"{('RAN' if seen[label][f] else 'blocked'):7}" for f in ('', '-S', '-I', '-I -S')))
    for label in seen: assert seen[label] == EXPECTED[label], (label, seen[label])

@R.register("N1: each secret-bearing workflow step, started exactly as the workflow starts it, runs NO start-up hook and exposes no token - and still reaches its own code")
def _():
    p = poison(); problems = []
    for step in workflow_steps():
        if not step["has_secrets"]: continue
        command = re.sub(r"\$\{?([A-Z_]+)\}?", lambda m: {"GITHUB_RUN_ID": RUN_ID, "RUN_URL": RUN_URL}.get(m.group(1), "success"), step["commands"][0]); tokens = shlex.split(command)
        script = tokens.index("src/daily_automation.py"); flags, rest = tokens[1:script], tokens[script + 1:]
        for interpreter, extra in p["configs"]:
            done, labels, captured = start(interpreter, flags, [DAILY, *rest], extra, cwd=os.path.join(WORK, "cwd"))
            if labels or TOKEN in captured: problems.append(f"{step['id']}: hooks {sorted(labels)} ran with flags {flags or 'none'}; token captured: {TOKEN in captured}")
            elif done.returncode != 1 or "ERROR:" not in done.stderr or "Traceback" in done.stderr: problems.append(f"{step['id']}: did not reach its own code (exit {done.returncode}): {done.stderr.strip()[:140]}")
    assert not problems, "; ".join(problems[:6]) + (f" ... and {len(problems) - 6} more" if len(problems) > 6 else "")

# ================================================================ the commands still work under -I -S (against a local imitation of the web app)
class Imitation:
    def __init__(self):
        self.actions, self.rows, outer = [], [], self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); outer.actions.append(body.get("action")); outer.reply = outer.answer(body)
                self.send_response(302); self.send_header("Location", "/echo"); self.send_header("Content-Length", "0"); self.end_headers()
            def do_GET(self):
                data = json.dumps(outer.reply).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/macros/s/FAKEDEPLOYMENTID0123456789/exec"
    def answer(self, body):
        if body.get("token") != TOKEN: return {"success": False, "error": "UNAUTHORIZED"}
        action = body.get("action")
        if action == "upsert_analysis": self.rows = body["rows"]; return {"success": True, "action": action, "sheet_status": "ok", "rows_inserted": 2, "rows_updated": 0, "write_confirmed": True, "email_status": "not_attempted"}
        if action == "verify_analysis": same = body["rows"] == self.rows; return {"success": same, "matches": same, "rows_found": len(self.rows), "duplicate_keys": 0, "sheet_status": "ok" if same else "failed"}
        if action == "send_success_email": return {"success": True, "sheet_status": "ok", "email_status": "sent"}
        if action == "send_success_report": return {"success": True, "action": action, "sheet_status": "ok", "email_status": "sent", "email_format": "html"}
        if action == "notify_failure": return {"success": True, "sheet_status": body["sheet_status"], "email_status": "sent"}
        if action == "self_check": return {"success": True, "action": action, "properties": {n: True for n in ["SPREADSHEET_ID", "SHEET_NAME", "SHARED_SECRET", "EMAIL_TO", "DATE_HEADER", "ASSET_HEADER"]},
                                           "spreadsheet_found": True, "sheet_found": True, "sheet_name_matches": True, "headers_ok": True, "email_valid": True}
        return {"success": False, "error": "INVALID_REQUEST"}

RUNNER = ("import re, sys; sys.path.insert(0, " + repr(os.path.join(REPO, "src")) + "); import daily_automation as d; "
          "d.WEBHOOK_URL_PATTERN = re.compile(r'http://127\\.0\\.0\\.1:[0-9]+/macros/s/[A-Za-z0-9_-]{20,}/exec'); sys.exit(d.main(sys.argv[1:]))")   # only the address check is relaxed, so that the local imitation can be used

@R.register("compatibility: post (upsert, verify, email, HTML report), notify-failure and self-check all complete with exit 0 under 'python -I -S' inside the poisoned interpreter, and no hook runs")
def _():
    p, server, out = poison(), Imitation(), os.path.join(WORK, "payloads")
    with contextlib.redirect_stdout(io.StringIO()): assert da.main(["build-payload", "--reports-dir", REAL_REPORTS, "--out-dir", out, "--run-id", RUN_ID, "--run-url", RUN_URL], env={}, now=date(2026, 10, 28)) == 0
    commands = [["post", "--action", "upsert_analysis", "--dir", out], ["post", "--action", "verify_analysis", "--dir", out], ["post", "--action", "send_success_email", "--dir", out], ["post", "--action", "send_success_report", "--dir", out, "--artifact-url", RUN_URL + "/artifacts/987654321"], ["self-check"],
                ["notify-failure", "--dir", out, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=failure"]]
    for args in commands:
        done, labels, captured = start(p["python"], ["-I", "-S"], ["-c", RUNNER, *args], {}, url=server.url)
        assert done.returncode == 0, (args, done.returncode, done.stdout[-300:], done.stderr[-300:]); assert not labels and TOKEN not in captured, (args, labels)
    assert server.actions == ["upsert_analysis", "verify_analysis", "send_success_email", "send_success_report", "self_check", "notify_failure"], server.actions
    assert json.load(open(os.path.join(out, "upsert_analysis_result.json"), encoding="utf-8"))["rows_inserted"] == 2 and json.load(open(os.path.join(out, "send_success_email_result.json"), encoding="utf-8"))["email_status"] == "sent"
    server.httpd.shutdown()

R.finish()
