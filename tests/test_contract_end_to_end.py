"""Contract tests: the REAL Python client (src/daily_automation.py) against the REAL web app code (apps_script/Code.gs). Only Google's services are imitated (by tests/apps_script/harness.mjs, through
tests/apps_script/bridge.mjs); every request and every reply crosses a real HTTP connection, including the redirect the real web app answers with. This is as close to the live system as it gets without
Google. It does NOT prove that Google's real services behave like the imitation: only a live run can. No real secret is used (the shared secret below is the made-up one of the Node harness)."""
import atexit, contextlib, io, json, os, re, shutil, subprocess, sys, tempfile, urllib.request
from datetime import date

from wf_support import Runner, REPO, load

da = load("daily_automation")
BRIDGE = os.path.join(REPO, "tests", "apps_script", "bridge.mjs")
CODE_GS = os.path.join(REPO, "apps_script", "Code.gs")
REAL_REPORTS = os.path.join(REPO, "reports")
SECRET = "unit-test-shared-secret-0123456789"                      # the made-up secret that tests/apps_script/harness.mjs configures
RUN_ID = "123456789"; RUN_URL = "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
TODAY = date(2026, 10, 28)                                         # the imitated Google clock is 2026-10-28 02:15:30 UTC, so 2026-10-27 is the last completed day
HEADINGS = ["Run date", "Asset", "Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation", "Validation status", "Report status", "Workflow run ID"]
WORK = tempfile.mkdtemp(prefix="contract_"); atexit.register(shutil.rmtree, WORK, True)
R = Runner("end-to-end contract checks")

# ---------------------------------------------------------------- the bridge to Code.gs
NODE = shutil.which("node")
bridge = subprocess.Popen([NODE or "node", BRIDGE], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) if NODE else None
atexit.register(lambda: bridge and bridge.kill())
PORT = None
if bridge:
    first = bridge.stdout.readline().strip()
    PORT = int(first.split("=")[1]) if first.startswith("PORT=") else None
URL = f"http://127.0.0.1:{PORT}/macros/s/FAKEDEPLOYMENTID0123456789/exec"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # no proxy for the local bridge

def control(path, data=None):
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=None if data is None else json.dumps(data).encode(), method="GET" if data is None else "POST", headers={"Content-Type": "application/json"})
    return json.loads(OPENER.open(request, timeout=30).read())
def reset(**options): control("/_reset", options)
def state(): return control("/_state")

@contextlib.contextmanager
def talking_to_bridge():
    old = da.WEBHOOK_URL_PATTERN, da.pause, da.REQUEST_TIMEOUT
    da.WEBHOOK_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:\d+/macros/s/[A-Za-z0-9_-]{20,}/exec"); da.pause = lambda seconds: None; da.REQUEST_TIMEOUT = 30
    try: yield
    finally: da.WEBHOOK_URL_PATTERN, da.pause, da.REQUEST_TIMEOUT = old

def cli(argv, token=SECRET, **extra_env):
    out, err = io.StringIO(), io.StringIO(); env = {"GOOGLE_APPS_SCRIPT_URL": URL, "GOOGLE_APPS_SCRIPT_TOKEN": token, **extra_env}
    with talking_to_bridge(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err): code = da.main(argv, env=env, now=TODAY)
    return code, out.getvalue() + err.getvalue()

def prepared(run_id=RUN_ID):
    folder = tempfile.mkdtemp(dir=WORK)
    code, text = cli(["build-payload", "--reports-dir", REAL_REPORTS, "--out-dir", folder, "--run-id", run_id, "--run-url", RUN_URL.replace(RUN_ID, run_id)]); assert code == 0, text
    return folder
def result(folder, action): return json.load(open(os.path.join(folder, f"{action}_result.json"), encoding="utf-8"))
def daily_flow(folder):
    return [cli(["post", "--action", action, "--dir", folder])[0] for action in ("upsert_analysis", "verify_analysis", "send_success_email")]
def row_values(folder): return [[row[h] for h in HEADINGS] for row in json.load(open(os.path.join(folder, "upsert_analysis.json"), encoding="utf-8"))["rows"]]
SELF_LABELS = ["authentication", "script properties", "spreadsheet ID", "worksheet Sheet1", "ten headings", "email configuration"]
def self_check_lines(text): return {label: next((l for l in text.splitlines() if l.startswith("self-check:") and label in l), "") for label in SELF_LABELS}

# ================================================================ the checks
@R.register("the bridge to the real Code.gs is up (Node is needed for this suite)")
def _():
    assert NODE and PORT, "node was not found or the bridge did not start"; reset(); assert state()["rows"] == [] and state()["headings"] == HEADINGS

@R.register("daily flow: upsert, verify, success email and self-check all succeed against the REAL Code.gs; the sheet holds exactly the client's two rows (date as text, numbers as numbers)")
def _():
    reset(); folder = prepared(); assert daily_flow(folder) == [0, 0, 0]; code, text = cli(["self-check"]); assert code == 0, text
    up = result(folder, "upsert_analysis"); assert (up["sheet_status"], up["rows_inserted"], up["rows_updated"], up["write_confirmed"]) == ("ok", 2, 0, True), up
    assert result(folder, "verify_analysis")["matches"] is True and result(folder, "verify_analysis")["rows_found"] == 2
    now = state(); assert now["headings"] == HEADINGS and now["rows"] == row_values(folder), (now["rows"], row_values(folder))
    assert isinstance(now["rows"][0][0], str) and now["rows"][0][0] == "2026-10-27" and [r[1] for r in now["rows"]] == ["BTC", "ETH"] and all(isinstance(c, float) for c in now["rows"][0][2:7])
    assert len(now["mail"]) == 1 and now["mail"][0]["subject"] == "Daily Market Analysis OK - 2026-10-27", now["mail"]
    body = now["mail"][0]["body"]; assert "Spreadsheet: updated and verified" in body and RUN_URL in body and "strong positive" in body and "Dubai" in body and "0.928089" in body, body
    assert result(folder, "send_success_email")["email_status"] == "sent" and result(folder, "send_success_email")["sheet_status"] == "ok"

@R.register("duplicate execution: the whole flow run again updates the same two rows, adds none, and sends no second email (skipped_duplicate)")
def _():
    reset(); first = prepared(); assert daily_flow(first) == [0, 0, 0]; second = prepared(); assert daily_flow(second) == [0, 0, 0]
    up = result(second, "upsert_analysis"); assert (up["rows_inserted"], up["rows_updated"]) == (0, 2), up
    now = state(); assert len(now["rows"]) == 2 and now["rows"] == row_values(second); assert result(second, "send_success_email")["email_status"] == "skipped_duplicate" and len(now["mail"]) == 1
    third = prepared("987654321"); assert daily_flow(third) == [0, 0, 0] and len(state()["mail"]) == 2 and len(state()["rows"]) == 2, "a different workflow run id is a different run: it may e-mail again but never adds rows"

@R.register("email failure is reported on its own, against the REAL Code.gs: the sheet stays updated and verified, the email is failed (EMAIL_FAILED), the step exits 1, and a later retry can still send it")
def _():
    reset(); folder = prepared(); control("/_mail_failure", {"message": "Service invoked too many times for one day: email."})
    assert daily_flow(folder) == [0, 0, 1]; sent = result(folder, "send_success_email")
    assert (sent["success"], sent["sheet_status"], sent["email_status"], sent["error"]) == (False, "ok", "failed", "EMAIL_FAILED"), sent
    assert state()["rows"] == row_values(folder) and state()["mail"] == [], "the sheet update stands; no email went out"
    control("/_mail_failure", {}); code, text = cli(["post", "--action", "send_success_email", "--dir", folder]); assert code == 0, text
    assert result(folder, "send_success_email")["email_status"] == "sent" and len(state()["mail"]) == 1

@R.register("invalid authentication: a wrong token is refused (UNAUTHORIZED), the sheet is untouched, nothing is mailed, and the result says the sheet was not attempted")
def _():
    reset(); folder = prepared(); code, text = cli(["post", "--action", "upsert_analysis", "--dir", folder], token="not-the-shared-secret-0000000000"); assert code == 1 and "UNAUTHORIZED" in text, text
    up = result(folder, "upsert_analysis"); assert up["success"] is False and up["error"] == "UNAUTHORIZED" and up["sheet_status"] == "not_attempted", up
    now = state(); assert now["rows"] == [] and now["mail"] == [] and now["writes"] == 0

@R.register("sheet headings that differ: the upsert is refused (HEADER_MISMATCH, sheet failed), nothing is written, the success email is not even requested, and the failure notice is a separate email")
def _():
    reset(headers=HEADINGS[:9] + ["Run ID"]); folder = prepared(); code, text = cli(["post", "--action", "upsert_analysis", "--dir", folder]); assert code == 1 and "HEADER_MISMATCH" in text, text
    up = result(folder, "upsert_analysis"); assert up["error"] == "HEADER_MISMATCH" and up["sheet_status"] == "failed", up; assert state()["rows"] == [] and state()["writes"] == 0
    code, text = cli(["post", "--action", "send_success_email", "--dir", folder]); assert code == 1 and "spreadsheet" in text.lower() and state()["mail"] == [], "no success email after a failed update"
    code, text = cli(["notify-failure", "--dir", folder, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=success", "--outcome", "post_to_sheet=failure"]); assert code == 0, text
    mail = state()["mail"]; assert len(mail) == 1 and "FAILED at stage: post_to_sheet" in mail[0]["subject"] and "Spreadsheet status: failed" in mail[0]["body"] and "HEADER_MISMATCH" in mail[0]["body"], mail

@R.register("server-side guard: even if the client's result files claim success, the web app itself refuses the success email when the sheet does not hold the rows (NOT_VERIFIED, no mail)")
def _():
    reset(); folder = prepared()
    for action, content in (("upsert_analysis", {"success": True, "sheet_status": "ok", "write_confirmed": True, "rows_inserted": 2, "rows_updated": 0}), ("verify_analysis", {"success": True, "matches": True, "rows_found": 2, "duplicate_keys": 0})):
        json.dump(content, open(os.path.join(folder, f"{action}_result.json"), "w", encoding="utf-8"))
    code, text = cli(["post", "--action", "send_success_email", "--dir", folder]); assert code == 1 and "NOT_VERIFIED" in text, text
    assert result(folder, "send_success_email")["error"] == "NOT_VERIFIED" and result(folder, "send_success_email")["email_status"] == "not_attempted" and state()["mail"] == []

@R.register("failure notification is independent of the sheet: it works with a broken sheet configuration, sends one email, and is not repeated for the same run and stage")
def _():
    reset(headers=["x"], properties={"SPREADSHEET_ID": "someone-elses-sheet"}); folder = tempfile.mkdtemp(dir=WORK)
    args = ["notify-failure", "--dir", folder, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "checkout=success", "--outcome", "tests=failure"]
    assert cli(args)[0] == 0; assert cli(args)[0] == 0, "repeating is not an error"
    mail = state()["mail"]; assert len(mail) == 1 and "FAILED at stage: tests" in mail[0]["subject"] and RUN_URL in mail[0]["body"] and "Spreadsheet status: not_attempted" in mail[0]["body"], mail
    assert result(folder, "notify_failure")["email_status"] == "skipped_duplicate" and state()["rows"] == [] and state()["writes"] == 0

SELF_CASES = {
    "everything in order": ({}, {}),
    "SHEET_NAME is a different existing tab": ({"properties": {"SHEET_NAME": "Weekly"}, "extraSheet": "Weekly"}, {"worksheet Sheet1": "FAIL"}),
    "SHEET_NAME is a tab that does not exist": ({"properties": {"SHEET_NAME": "Missing"}}, {"worksheet Sheet1": "FAIL", "ten headings": "NOT CHECKED"}),
    "SPREADSHEET_ID cannot be opened": ({"properties": {"SPREADSHEET_ID": "someone-elses-sheet"}}, {"spreadsheet ID": "FAIL", "worksheet Sheet1": "NOT CHECKED"}),
    "headings differ": ({"headers": HEADINGS[:9] + ["Run ID"]}, {"ten headings": "FAIL", "worksheet Sheet1": "PASS"}),
    "EMAIL_TO is not an address": ({"properties": {"EMAIL_TO": "not-an-address"}}, {"email configuration": "FAIL"}),
    "EMAIL_TO is missing": ({"properties": {"EMAIL_TO": ""}}, {"script properties": "EMAIL_TO", "email configuration": "FAIL"}),
}
@R.register("self-check against the REAL Code.gs (" + str(len(SELF_CASES)) + " configurations): passes only when everything is in order, fails exactly the right check otherwise, and NEVER writes, mails, locks or caches")
def _():
    for name, (options, expectations) in SELF_CASES.items():
        reset(**options); code, text = cli(["self-check"]); lines = self_check_lines(text)
        assert code == (0 if not expectations else 1), (name, text)
        for label, expected in expectations.items(): assert expected in lines[label], (name, label, lines[label])
        now = state(); assert now["writes"] == 0 and now["mail"] == [] and now["lock_waits"] == 0 and now["cache_entries"] == 0 and now["rows"] == [], (name, now)
    reset(); code, text = cli(["self-check"], token="not-the-shared-secret-0000000000"); assert code == 1 and "does not match" in self_check_lines(text)["authentication"], text

@R.register("static contract: the request keys the web app insists on (Code.gs) are exactly the keys the client sends, for the upsert, verify and success-email requests")
def _():
    source = open(CODE_GS, encoding="utf-8").read()
    keys = lambda handler: set(re.findall(r"'([a-z_]+)'", re.search(r"function " + handler + r"\(request[^)]*\) \{\n  requireExactKeys_\(request, \[([^\]]*)\]", source).group(1)))
    payloads = da.build_payloads(da.validate_outputs(REAL_REPORTS, today=TODAY), RUN_ID, RUN_URL)
    assert keys("handleUpsert_") == set(payloads["upsert_analysis"]) | {"token"} and keys("handleVerify_") == set(payloads["verify_analysis"]) | {"token"}, (keys("handleUpsert_"), set(payloads["upsert_analysis"]))
    assert keys("handleSuccessEmail_") == set(payloads["send_success_email"]) | {"token", "sheet_update"}, keys("handleSuccessEmail_")
    assert set(re.findall(r"^  (\w+): handle\w+_,$", source, re.M)) == {"upsert_analysis", "verify_analysis", "send_success_email", "notify_failure", "self_check"} == set(da.POST_ACTIONS) | {"notify_failure", "self_check"}

if bridge: bridge.kill()
R.finish()
