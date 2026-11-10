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
    assert set(re.findall(r"^  (\w+): handle\w+_,$", source, re.M)) == {"upsert_analysis", "verify_analysis", "send_success_email", "send_success_report", "notify_failure", "self_check"} == set(da.POST_ACTIONS) | {"notify_failure", "self_check"}
    report = json.load(open(os.path.join(prepared(), "send_success_report.json"), encoding="utf-8"))               # Stage 4: the new request, as the client builds it, plus what is added when it is posted
    assert keys("handleSuccessReport_") == set(report) | {"token", "sheet_update", "artifact_url"}, (keys("handleSuccessReport_"), set(report))

@R.register("failure notices for the NEW workflow steps are accepted by the REAL Code.gs without any change to it: each is sent with a stage it knows, the real step name is in the mail, and the sheet is not touched")
def _():
    mapping = getattr(da, "SERVER_STAGE", None); assert mapping, "MISSING FEATURE: daily_automation.py has no SERVER_STAGE mapping yet"
    reset(); folder = tempfile.mkdtemp(dir=WORK)
    for number, (step_id, server_stage) in enumerate(sorted(mapping.items())):
        run_id = str(500000 + number); url = RUN_URL.replace(RUN_ID, run_id)                   # a different run each time: the web app reports one failure per run and stage
        code, text = cli(["notify-failure", "--dir", folder, "--run-id", run_id, "--run-url", url, "--outcome", "tests=success", "--outcome", f"{step_id}=failure"]); assert code == 0, (step_id, text)
        mail = state()["mail"][-1]; assert f"FAILED at stage: {server_stage}" in mail["subject"] and step_id in mail["body"] and url in mail["body"], (step_id, mail)
    assert len(state()["mail"]) == len(mapping) and state()["rows"] == [] and state()["writes"] == 0

@R.register("a SUPPRESSED failure notice, against the REAL Code.gs: a later failure of another step that shares the mapped stage, or of the same step, in the same run gets skipped_duplicate, no email goes out, and the client says so (log, job summary, result file) instead of reporting a delivery")
def _():
    reset(); folder = tempfile.mkdtemp(dir=WORK)
    def run(failing, run_id):
        position = da.STAGE_ORDER.index(failing); where = tempfile.mkdtemp(dir=WORK); summary = os.path.join(where, "summary.md"); flags = []
        for name in da.STAGE_ORDER: flags += ["--outcome", f"{name}={'success' if da.STAGE_ORDER.index(name) < position else 'failure' if name == failing else 'skipped'}"]
        code, text = cli(["notify-failure", "--dir", where, "--run-id", run_id, "--run-url", RUN_URL.replace(RUN_ID, run_id)] + flags, GITHUB_STEP_SUMMARY=summary)
        return code, text, open(summary, encoding="utf-8").read(), result(where, "notify_failure")
    mails = lambda: len(state()["mail"])
    code, text, summary, outcome = run("analysis", "800001"); assert code == 0 and mails() == 1 and outcome["email_status"] == "sent" and "SUPPRESSED" not in text + summary and "FAILED at stage: analysis" in state()["mail"][0]["subject"], (text, summary)
    for failing in ("build_dashboard", "clear_stale_outputs", "build_dashboard"):                                  # other steps of the same mapped stage, and a repeat of one of them
        code, text, summary, outcome = run(failing, "800001")
        assert code == 0 and mails() == 1, f"{failing}: a duplicate email must not go out (exit {code}, {mails()} mails)"
        assert outcome["email_status"] == "skipped_duplicate" and outcome["suppressed"] is True and outcome["failed_step"] == failing and outcome["server_stage"] == "analysis", outcome
        assert "SUPPRESSED" in text and failing in text and "SUPPRESSED" in summary and failing in summary, (text, summary)
        assert not re.search(r"\bOK\b|\bsent\b|Failure email: sent", (text + summary).replace("NOT sent", "")), f"{failing}: a delivery is claimed: {text!r} {summary!r}"
    code, text, summary, outcome = run("stage_deliverables", "800001"); assert code == 0 and mails() == 2 and outcome["email_status"] == "sent" and "SUPPRESSED" not in text + summary      # a different stage in the same run is still delivered
    code, text, summary, outcome = run("build_dashboard", "800002"); assert code == 0 and mails() == 3 and outcome["email_status"] == "sent" and "SUPPRESSED" not in text + summary           # a new run is never suppressed
    assert state()["rows"] == [] and state()["writes"] == 0

# ================================================================ Stage 4: the HTML success report against the REAL Code.gs (inline PNG charts, shared duplicate protection, strict validation)
ARTIFACT_URL = RUN_URL + "/artifacts/987654321"
def report_flow(folder, url=ARTIFACT_URL):
    return [cli(["post", "--action", "upsert_analysis", "--dir", folder])[0], cli(["post", "--action", "verify_analysis", "--dir", folder])[0], cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", url])[0]]
def sheet_ready(folder): assert cli(["post", "--action", "upsert_analysis", "--dir", folder])[0] == 0 and cli(["post", "--action", "verify_analysis", "--dir", folder])[0] == 0
def stored_report(folder): return json.load(open(os.path.join(folder, "send_success_report.json"), encoding="utf-8"))
def raw_post(payload):
    """A request sent straight to the web app, bypassing the client's own checks (to judge Code.gs's validation by itself)."""
    request = urllib.request.Request(URL, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"}); return json.loads(OPENER.open(request, timeout=30).read())

@R.register("report flow against the REAL Code.gs: ONE e-mail with an HTML body and the three charts as inline PNG images - the very bytes the client built - the plain-text part, the artifact link and the run link; the result records email_format html")
def _():
    import base64, hashlib
    reset(); folder = prepared(); assert report_flow(folder) == [0, 0, 0]; mail = state()["mail"]; assert len(mail) == 1, len(mail); m = mail[0]
    assert m["subject"] == "Daily Market Analysis OK - 2026-10-27" and m["htmlBody"].startswith("<!DOCTYPE html>") and sorted(m["inlineImages"]) == ["chart_drawdown", "chart_indexed", "chart_returns"]
    charts = stored_report(folder)["report"]["charts"]
    for chart in charts:
        image = m["inlineImages"]["chart_" + chart["id"]]; data = base64.b64decode(chart["png_base64"]); assert image["contentType"] == "image/png" and image["head"] == "89504e470d0a1a0a" and image["size"] == len(data) and image["sha256"] == hashlib.sha256(data).hexdigest(), chart["id"]
    html = m["htmlBody"]; assert ARTIFACT_URL in html and RUN_URL in html and "80,800.90" in html and "2,347.34" in html and "17 of 17" in html and "MISSING" not in html and "0.928089" in html, html[:300]
    assert RUN_URL in m["body"] and ARTIFACT_URL in m["body"] and "strong positive" in m["body"]
    sent = result(folder, "send_success_report"); assert (sent["success"], sent["email_status"], sent["email_format"], sent["sheet_status"]) == (True, "sent", "html", "ok"), sent
    print(f"    measured: HTML {len(html)} characters, charts {[m['inlineImages']['chart_' + c['id']]['size'] for c in charts]} bytes")

@R.register("report request size against the REAL Code.gs: the real request the client posts is under 19,000 characters (the web app's limit is 20,000 and is unchanged), and the web app accepted it")
def _():
    reset(); folder = prepared(); full = json.dumps(dict(stored_report(folder), token=SECRET, artifact_url=ARTIFACT_URL, sheet_update={"rows_inserted": 2, "rows_updated": 0}))
    print(f"    measured: the whole report request is {len(full)} characters (limit 20000)"); assert len(full) < 19000 and report_flow(folder) == [0, 0, 0] and len(state()["mail"]) == 1

@R.register("duplicate protection is shared between the two success actions, against the REAL Code.gs: whichever comes first for a run and date is the only one that sends; another run is sent again")
def _():
    reset(); first = prepared(); assert report_flow(first) == [0, 0, 0]
    assert cli(["post", "--action", "send_success_report", "--dir", first, "--artifact-url", ARTIFACT_URL])[0] == 0 and result(first, "send_success_report")["email_status"] == "skipped_duplicate" and len(state()["mail"]) == 1, "the new action twice"
    assert cli(["post", "--action", "send_success_email", "--dir", first])[0] == 0 and result(first, "send_success_email")["email_status"] == "skipped_duplicate" and len(state()["mail"]) == 1, "the old action after the new one"
    reset(); second = prepared(); assert daily_flow(second) == [0, 0, 0] and len(state()["mail"]) == 1
    assert cli(["post", "--action", "send_success_report", "--dir", second, "--artifact-url", ARTIFACT_URL])[0] == 0 and result(second, "send_success_report")["email_status"] == "skipped_duplicate" and len(state()["mail"]) == 1, "the new action after the old one"
    other = prepared("987654321"); assert report_flow(other, RUN_URL.replace(RUN_ID, "987654321") + "/artifacts/55") == [0, 0, 0] and len(state()["mail"]) == 2, "another run is a different run"

@R.register("the HTML message that CANNOT BE PREPARED (before any send) falls back to the plain-text e-mail, visibly: the step still succeeds and the result and the log say text_fallback; if the plain e-mail fails too the step fails with EMAIL_FAILED and a retry can send it")
def _():
    reset(); folder = prepared(); control("/_mail_failure", {"fail_blob": True}); summary = os.path.join(WORK, "fallback_summary.md")
    sheet_ready(folder); code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL], GITHUB_STEP_SUMMARY=summary); assert code == 0 and "WARNING" in text and "text_fallback" in text, text
    sent, now = result(folder, "send_success_report"), state(); assert sent["email_format"] == "text_fallback" and sent["email_status"] == "sent" and len(now["mail"]) == 1 and "htmlBody" not in now["mail"][0] and "inlineImages" not in now["mail"][0], (sent, now["mail"])
    assert now["mail_attempts"] == [False], "only the plain message was ever handed to the mail service"; assert "text_fallback" in open(summary, encoding="utf-8").read()
    reset(); both = prepared(); control("/_mail_failure", {"message": "Service invoked too many times for one day: email.", "fail_blob": True}); assert report_flow(both) == [0, 0, 1]
    failed = result(both, "send_success_report"); assert (failed["success"], failed["email_status"], failed["error"], failed["sheet_status"]) == (False, "failed", "EMAIL_FAILED", "ok") and state()["mail"] == [] and "email_format" not in failed, failed
    control("/_mail_failure", {}); code, text = cli(["post", "--action", "send_success_report", "--dir", both, "--artifact-url", ARTIFACT_URL]); assert code == 0 and result(both, "send_success_report")["email_format"] == "html" and len(state()["mail"]) == 1, text

@R.register("an HTML send that ERRORS (before or after delivery) is never followed by a plain-text e-mail, against the REAL Code.gs: ONE call to the mail service, the step fails, the result says send_outcome_unknown, and the job summary tells the owner to check the mailbox before repeating")
def _():
    for label, fault, delivered in (("error before delivery", {"message": "The HTML message was refused", "only_html": True}, 0), ("error after delivery", {"message": "the answer was lost", "after_send": True}, 1)):
        reset(); folder = prepared(); control("/_mail_failure", fault); summary = os.path.join(WORK, "unknown_summary.md"); sheet_ready(folder)
        code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL], GITHUB_STEP_SUMMARY=summary)
        now, outcome = state(), result(folder, "send_success_report"); assert code == 1, (label, text)
        assert now["mail_attempts"] == [True] and len(now["mail"]) == delivered, f"{label}: exactly one call to the mail service, delivered {len(now['mail'])} (expected {delivered}): {now['mail_attempts']}"
        assert (outcome["success"], outcome["error"], outcome["email_status"], outcome["sheet_status"]) == (False, "SEND_OUTCOME_UNKNOWN", "send_outcome_unknown", "ok") and "email_format" not in outcome and outcome["attempts"] == 1, (label, outcome)
        assert "send_outcome_unknown" in text and "mailbox" in text and "plain-text" in text and "WARNING" not in text.replace("NOT sent", ""), (label, text)
        written = open(summary, encoding="utf-8").read(); assert "send_outcome_unknown" in written and "mailbox" in written and "Success email: sent" not in written, (label, written)
        assert "the answer was lost" not in text + written and "refused" not in text + written, "the mail service's own text must not be echoed"
    control("/_mail_failure", {}); code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL]); assert code == 0 and result(folder, "send_success_report")["email_format"] == "html", "a deliberate repeat by the owner is handled as new"
    assert len(state()["mail"]) == 2, "the earlier delivered message and the deliberate repeat: the repeat is the owner's decision after reading the warning"

@R.register("strict validation by the REAL Code.gs itself (requests sent straight to it): a report altered after it was built - wrong PNG signature, extra key, impossible figure, missing variable, another run's artifact link - is refused with INVALID_PAYLOAD, nothing is mailed, the sheet stands")
def _():
    reset(); folder = prepared(); sheet_ready(folder); body = dict(stored_report(folder), token=SECRET, artifact_url=ARTIFACT_URL, sheet_update={"rows_inserted": 2, "rows_updated": 0})
    cases = (("signature", lambda d: d["report"]["charts"][0].update(png_base64="AAAA" + d["report"]["charts"][0]["png_base64"][4:])), ("extra key", lambda d: d["report"].update(html="<b>x</b>")),
             ("impossible return", lambda d: d["report"]["assets"]["btc"].update(return_30d=-3)), ("missing variable", lambda d: d["report"]["coverage"].pop()), ("another run's artifact", lambda d: d.update(artifact_url=RUN_URL.replace(RUN_ID, "5") + "/artifacts/1")))
    for name, change in cases:
        data = json.loads(json.dumps(body)); change(data); reply = raw_post(data); assert reply["success"] is False and reply["error"] == "INVALID_PAYLOAD" and reply["email_status"] == "not_attempted", (name, reply)
    assert state()["mail"] == [] and state()["rows"] == row_values(folder), "a refused request must send nothing and change nothing"
    assert raw_post(body)["email_status"] == "sent" and len(state()["mail"]) == 1, "the unaltered request is accepted"

@R.register("the success report is refused by the REAL Code.gs when the sheet does not hold the rows (NOT_VERIFIED), even if the client's own result files claim success - no mail")
def _():
    reset(); folder = prepared()
    for action, content in (("upsert_analysis", {"success": True, "sheet_status": "ok", "write_confirmed": True, "rows_inserted": 2, "rows_updated": 0}), ("verify_analysis", {"success": True, "matches": True, "rows_found": 2, "duplicate_keys": 0})):
        json.dump(content, open(os.path.join(folder, f"{action}_result.json"), "w", encoding="utf-8"))
    code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL]); assert code == 1 and "NOT_VERIFIED" in text, text
    assert result(folder, "send_success_report")["error"] == "NOT_VERIFIED" and result(folder, "send_success_report")["email_status"] == "not_attempted" and state()["mail"] == []

@R.register("a wrong token is refused (UNAUTHORIZED) for the new action too, and the artifact link of another run never reaches the web app")
def _():
    reset(); folder = prepared(); sheet_ready(folder)
    code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL], token="not-the-shared-secret-0000000000"); assert code == 1 and "UNAUTHORIZED" in text, text
    code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", RUN_URL.replace(RUN_ID, "5") + "/artifacts/1"]); assert code == 1 and "artifact" in text.lower(), text
    assert state()["mail"] == [], "nothing may be mailed after a refusal"

@R.register("a confirmed send followed by a FAILING cache write, against the REAL Code.gs through the real client: the e-mail goes out ONCE, the step succeeds with a visible warning that duplicate protection could not be recorded, the result says sent_dedup_failed, nothing is retried - legacy and HTML action")
def _():
    for name in ("send_success_email", "send_success_report"):
        reset(); folder = prepared(); control("/_cache_failure", {"put": "Exception: cache service unavailable"}); summary = os.path.join(WORK, f"dedup_{name}.md"); sheet_ready(folder)
        code, text = cli(["post", "--action", name, "--dir", folder] + (["--artifact-url", ARTIFACT_URL] if name == "send_success_report" else []), GITHUB_STEP_SUMMARY=summary); now, outcome = state(), result(folder, name)
        assert code == 0, (name, text); assert len(now["mail"]) == 1 and now["cache_puts"] == 1, (name, len(now["mail"]), now["cache_puts"])
        assert (outcome["success"], outcome["email_status"], outcome["sheet_status"], outcome["attempts"]) == (True, "sent_dedup_failed", "ok", 1) and "error" not in outcome, (name, outcome)
        assert "WARNING" in text and "WAS SENT" in text and "duplicate protection" in text and "FAILED" not in text and "INTERNAL_ERROR" not in text, (name, text)
        written = open(summary, encoding="utf-8").read(); assert "Success email: sent_dedup_failed" in written and "duplicate protection" in written, (name, written)
        if name == "send_success_report": assert outcome["email_format"] == "html" and now["mail_attempts"] == [True]
        else: assert now["mail_attempts"] == [False]
    control("/_cache_failure", {})

@R.register("a genuine failure stays distinguishable through the real client: a failed send is EMAIL_FAILED (no cache write was even tried), and a cache READ failure before any send is INTERNAL_ERROR that the client repeats - with nothing ever sent, so no duplicate")
def _():
    reset(); folder = prepared(); sheet_ready(folder); control("/_mail_failure", {"message": "down"}); control("/_cache_failure", {"put": "Exception: cache service unavailable"})
    code, text = cli(["post", "--action", "send_success_email", "--dir", folder]); failed = result(folder, "send_success_email")
    assert code == 1 and (failed["success"], failed["error"], failed["email_status"]) == (False, "EMAIL_FAILED", "failed") and state()["mail"] == [] and state()["cache_puts"] == 0, (text, failed, state()["cache_puts"])
    reset(); folder = prepared(); sheet_ready(folder); control("/_cache_failure", {"get": "Exception: cache read failed"})
    code, text = cli(["post", "--action", "send_success_report", "--dir", folder, "--artifact-url", ARTIFACT_URL]); failed = result(folder, "send_success_report")
    assert code == 1 and failed["error"] == "INTERNAL_ERROR" and failed["attempts"] == 3 and state()["mail"] == [] and state()["mail_attempts"] == [], (text, failed)
    control("/_cache_failure", {})

@R.register("a failure notice whose cache write FAILS after the send, against the REAL Code.gs through the real client: the notice goes out ONCE, the step succeeds with a visible warning (the email WAS SENT, duplicate protection not recorded, do not re-run the notification step within 6 hours), nothing is retried")
def _():
    reset(); folder = tempfile.mkdtemp(dir=WORK); summary = os.path.join(WORK, "notice_dedup.md"); control("/_cache_failure", {"put": "Exception: cache service unavailable"})
    code, text = cli(["notify-failure", "--dir", folder, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "checkout=success", "--outcome", "tests=failure"], GITHUB_STEP_SUMMARY=summary); now, outcome = state(), result(folder, "notify_failure")
    assert code == 0, text; assert len(now["mail"]) == 1 and now["cache_puts"] == 1 and "FAILED at stage: tests" in now["mail"][0]["subject"], (len(now["mail"]), now["cache_puts"])
    assert (outcome["success"], outcome["email_status"], outcome["attempts"]) == (True, "sent_dedup_failed", 1) and "error" not in outcome and "suppressed" not in outcome, outcome
    assert "WARNING" in text and "failure notice WAS SENT" in text and "duplicate protection" in text and "6 hours" in text and "SUPPRESSED" not in text and "INTERNAL_ERROR" not in text, text
    written = open(summary, encoding="utf-8").read(); assert "Failure email: sent_dedup_failed" in written and "WAS SENT" in written and "6 hours" in written and "notification step" in written, written
    control("/_cache_failure", {})

@R.register("a genuine failure-notice failure stays distinguishable through the real client: a failed send is EMAIL_FAILED (no cache write tried), and a cache READ failure before the send is INTERNAL_ERROR that the client repeats with nothing ever sent")
def _():
    reset(); folder = tempfile.mkdtemp(dir=WORK); control("/_mail_failure", {"message": "down"}); control("/_cache_failure", {"put": "Exception: cache service unavailable"})
    code, text = cli(["notify-failure", "--dir", folder, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=failure"]); failed = result(folder, "notify_failure")
    assert code == 1 and (failed["success"], failed["error"], failed["email_status"]) == (False, "EMAIL_FAILED", "failed") and state()["mail"] == [] and state()["cache_puts"] == 0 and "WAS SENT" not in text, (text, failed)
    reset(); folder = tempfile.mkdtemp(dir=WORK); control("/_cache_failure", {"get": "Exception: cache read failed"})
    code, text = cli(["notify-failure", "--dir", folder, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=failure"]); failed = result(folder, "notify_failure")
    assert code == 1 and failed["error"] == "INTERNAL_ERROR" and failed["attempts"] == 3 and state()["mail"] == [] and state()["mail_attempts"] == [], (text, failed)
    control("/_cache_failure", {})

if bridge: bridge.kill()
R.finish()
