"""Tests for src/daily_automation.py (validation, payload mapping, posting to the Apps Script web app, result checking, failure notice). Written BEFORE the module.
The web app is imitated by a small local HTTP server that behaves like Google's: a POST to /exec answers 302 and the JSON comes from the redirected GET. No real network, no real secret."""
import contextlib, csv, http.server, io, json, os, re, shutil, socketserver, statistics, sys, threading
from datetime import date

from wf_support import Runner, raises, REPO, load, new_temp_dir

da = load("daily_automation")
REAL_REPORTS = os.path.join(REPO, "reports")
TOKEN = "unit-test-token-0123456789abcdef"            # a made-up test value, never a real secret
HEADERS = ["Run date", "Asset", "Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation", "Validation status", "Report status", "Workflow run ID"]
RUN_ID, RUN_URL = "123456789", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
TODAY = date(2026, 10, 28)                                # the day after the committed 2026-10-27 report
R = Runner("daily automation checks")

# ---------------------------------------------------------------- helpers
def fresh_reports(extra=None, remove=(), edit=None):
    """A temporary copy of the committed 2026-10-27 outputs. edit(name, text) -> new text; extra = {name: bytes}."""
    folder = new_temp_dir()
    for name in os.listdir(REAL_REPORTS):
        if name.startswith("report_2026-10-27"): shutil.copy(os.path.join(REAL_REPORTS, name), os.path.join(folder, name))
    for name in remove: os.remove(os.path.join(folder, name))
    for name, data in (extra or {}).items(): open(os.path.join(folder, name), "wb").write(data)
    if edit:
        for name in os.listdir(folder):
            path = os.path.join(folder, name); text = open(path, encoding="utf-8", newline="").read(); new = edit(name, text)
            if new != text: open(path, "w", encoding="utf-8", newline="").write(new)
    return folder

def real_series():
    rows = list(csv.reader(open(os.path.join(REAL_REPORTS, "report_2026-10-27_data.csv"), encoding="utf-8")))[1:]
    return {"dates": [r[0] for r in rows], "BTC": [float(r[1]) for r in rows], "ETH": [float(r[2]) for r in rows], "BTC_ret": [float(r[3]) for r in rows[1:]], "ETH_ret": [float(r[4]) for r in rows[1:]]}

def worst_fall(closes):                                   # independent of analysis.py
    peak, worst = closes[0], 0.0
    for close in closes:
        peak = max(peak, close); worst = min(worst, close / peak - 1)
    return worst

def run_cli(argv, env=None, now=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = da.main(argv, env=dict(env or {}), now=now)
    return code, out.getvalue(), err.getvalue()

# ---------------------------------------------------------------- the imitated web app
PROPERTY_NAMES = ["SPREADSHEET_ID", "SHEET_NAME", "SHARED_SECRET", "EMAIL_TO", "DATE_HEADER", "ASSET_HEADER"]
def self_reply(**changes):
    """What the updated Code.gs answers to self_check when everything is in order."""
    reply = {"success": True, "action": "self_check", "sheet_status": "not_attempted", "email_status": "not_attempted", "request_id": "r-9", "properties": {name: True for name in PROPERTY_NAMES},
             "spreadsheet_found": True, "sheet_found": True, "sheet_name_matches": True, "headers_ok": True, "email_valid": True}
    reply.update(changes); return reply
def failing_reply(**changes): return self_reply(**dict({"success": False, "error": "NOT_CONFIGURED"}, **changes))   # what Code.gs answers when something is not in order
def missing_property(name): return {n: n != name for n in PROPERTY_NAMES}

class FakeScript:
    """responder(index, payload) -> ("json", dict) | ("html", text) | ("http", status, bytes) | ("sleep", seconds, spec) | ("big", size)"""
    def __init__(self, responder):
        self.responder, self.calls, self.stored, self.lock, self.post_count = responder, [], {}, threading.Lock(), 0       # post_count numbers the POSTs only (the redirected GETs are not attempts)
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *args): pass
            def reply(self, status, ctype, body, headers=None):
                self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.end_headers(); self.wfile.write(body)
            def deliver(self, spec):
                if spec[0] == "sleep": threading.Event().wait(spec[1]); spec = spec[2]
                if spec[0] == "json": self.reply(200, "application/json; charset=utf-8", json.dumps(spec[1]).encode())
                elif spec[0] == "html": self.reply(200, "text/html; charset=utf-8", spec[1].encode())
                elif spec[0] == "http": self.reply(spec[1], "text/html", spec[2])
                elif spec[0] == "big": self.reply(200, "application/json", b'{"success":true,"pad":"' + b"x" * spec[1] + b'"}')
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.lock: index = outer.post_count; outer.post_count += 1; outer.calls.append({"method": "POST", "path": self.path, "body": body, "headers": dict(self.headers)})
                try: payload = json.loads(body)
                except ValueError: payload = None
                spec = outer.responder(index, payload); outer.stored[index] = spec
                inner = spec[2] if spec[0] == "sleep" else spec
                if inner[0] == "http" and spec[0] != "sleep": self.deliver(spec)                  # an HTTP error comes straight back from /exec
                else: self.reply(302, "text/html", b"moved", {"Location": f"/echo/{index}"})
            def do_GET(self):
                with outer.lock: outer.calls.append({"method": "GET", "path": self.path, "body": b"", "headers": dict(self.headers)})
                index = int(self.path.rsplit("/", 1)[-1]) if self.path.startswith("/echo/") else -1
                spec = outer.stored.get(index)
                if spec is None: self.reply(404, "text/plain", b"no such echo")
                else: self.deliver(spec)
        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            def handle_error(self, request, client_address): pass          # a client that gave up on purpose (timeout tests) is not an error here
        self.httpd = Server(("127.0.0.1", 0), Handler); self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/macros/s/FAKEDEPLOYMENTID0123456789/exec"
    def posts(self): return [c for c in self.calls if c["method"] == "POST"]
    def stop(self): self.httpd.shutdown(); self.httpd.server_close()

class Model:
    """A tiny model of the real contract: upsert by (date, asset), read-back verification, mail."""
    def __init__(self, token=TOKEN): self.token, self.rows, self.mail, self.fail_mail, self.drop_asset, self.self_check_reply, self.self_check_requests = token, {}, [], False, None, None, []
    def __call__(self, index, payload):
        if not isinstance(payload, dict) or payload.get("token") != self.token: return ("json", {"success": False, "error": "UNAUTHORIZED", "sheet_status": "not_attempted", "email_status": "not_attempted"})
        action = payload.get("action")
        if action == "upsert_analysis":
            inserted = updated = 0
            for row in payload["rows"]:
                if self.drop_asset == row["Asset"]: continue
                key = (row["Run date"], row["Asset"]); updated += key in self.rows; inserted += key not in self.rows; self.rows[key] = row
            return ("json", {"success": True, "action": action, "sheet_status": "ok", "rows_inserted": inserted, "rows_updated": updated, "write_confirmed": True, "email_status": "not_attempted", "request_id": "r-1"})
        if action == "self_check":
            self.self_check_requests.append(payload); return ("json", dict(self.self_check_reply) if self.self_check_reply is not None else self_reply())
        if action == "verify_analysis":
            found = [self.rows.get((r["Run date"], r["Asset"])) for r in payload["rows"]]; count = sum(1 for f in found if f is not None); matches = all(f == r for f, r in zip(found, payload["rows"]))
            return ("json", {"success": matches and count == len(payload["rows"]), "action": action, "rows_found": count, "matches": matches, "duplicate_keys": 0, "sheet_status": "ok" if matches else "failed", "email_status": "not_attempted"})
        if action == "send_success_email":
            if self.fail_mail: return ("json", {"success": False, "error": "EMAIL_FAILED", "sheet_status": "ok", "email_status": "failed"})
            self.mail.append(payload); return ("json", {"success": True, "action": action, "sheet_status": "ok", "email_status": "sent"})
        if action == "notify_failure":
            self.mail.append(payload); return ("json", {"success": True, "action": action, "sheet_status": payload.get("sheet_status"), "email_status": "sent"})
        return ("json", {"success": False, "error": "INVALID_REQUEST"})

@contextlib.contextmanager
def served(responder, allow_local=True, timeout=None):
    server, old_pattern, old_timeout, old_pause, pauses = FakeScript(responder), da.WEBHOOK_URL_PATTERN, da.REQUEST_TIMEOUT, da.pause, []
    if allow_local: da.WEBHOOK_URL_PATTERN = re.compile(r"^http://127\.0\.0\.1:\d+/macros/s/[A-Za-z0-9_-]{20,}/exec$")
    if timeout: da.REQUEST_TIMEOUT = timeout                                          # seconds
    da.pause = pauses.append                              # no real waiting in tests
    server.pauses = pauses
    try: yield server
    finally: da.WEBHOOK_URL_PATTERN, da.REQUEST_TIMEOUT, da.pause = old_pattern, old_timeout, old_pause; server.stop()

def env_for(server, **extra):
    env = {"GOOGLE_APPS_SCRIPT_URL": server.url, "GOOGLE_APPS_SCRIPT_TOKEN": TOKEN}; env.update(extra); return env

def prepared_dir(run_id=RUN_ID):
    reports = fresh_reports(); out = new_temp_dir()
    assert run_cli(["build-payload", "--reports-dir", reports, "--out-dir", out, "--run-id", run_id, "--run-url", RUN_URL], now=TODAY)[0] == 0
    return out

def read_json(folder, name): return json.load(open(os.path.join(folder, name), encoding="utf-8"))

# ================================================================ validation of the generated outputs
@R.register("validate: the committed 2026-10-27 report and CSV pass when run on 2026-10-28 and the analysis date is returned")
def _():
    outputs = da.validate_outputs(REAL_REPORTS, today=TODAY); assert outputs["date"] == "2026-10-27", outputs["date"]

@R.register("validate: reuses the existing parsing/comparison code (dashboard.parse_dataset, check_against_report), not a copy of it")
def _():
    source = open(os.path.join(REPO, "src", "daily_automation.py"), encoding="utf-8").read()
    for needle in ("parse_dataset", "check_against_report", "window_metrics"): assert needle in source, needle
    assert "def pearson" not in source and "def simple_returns" not in source and "stdev(" not in source, "business logic copied into the new module"

@R.register("validate: stale outputs (the newest files are not for the last completed UTC day) are refused")
def _():
    raises(da.AutomationError, lambda: da.validate_outputs(REAL_REPORTS, today=date(2026, 10, 29)), "2026-10-28")
    raises(da.AutomationError, lambda: da.validate_outputs(REAL_REPORTS, today=date(2026, 10, 27)), "2026-10-26")

@R.register("validate: older reports in the folder are ignored; the files of the expected day are the ones checked")
def _():
    folder = fresh_reports(extra={"report_2026-10-26.md": b"old", "report_2026-10-26_data.csv": b"old"}); assert da.validate_outputs(folder, today=TODAY)["date"] == "2026-10-27"

@R.register("validate: a missing report, a missing CSV and an empty folder are refused")
def _():
    for remove in (["report_2026-10-27.md"], ["report_2026-10-27_data.csv"], ["report_2026-10-27.md", "report_2026-10-27_data.csv"]):
        raises(da.AutomationError, lambda: da.validate_outputs(fresh_reports(remove=remove), today=TODAY), "report_2026-10-27")
    raises(da.AutomationError, lambda: da.validate_outputs(new_temp_dir(), today=TODAY), "report_2026-10-27")
    raises(da.AutomationError, lambda: da.validate_outputs(os.path.join(new_temp_dir(), "missing"), today=TODAY), "report")

@R.register("validate: a CSV that fails the 17-column validation (altered close, wrong header, truncated, non-UTF-8, oversized) is refused")
def _():
    def alter(name, text): return text.replace("84444.2,2688.02", "84444.2,2699.02", 1) if name.endswith(".csv") else text
    raises(da.AutomationError, lambda: da.validate_outputs(fresh_reports(edit=alter), today=TODAY), "")
    raises(da.AutomationError, lambda: da.validate_outputs(fresh_reports(edit=lambda n, t: t.replace("eth_vwap", "eth_vw") if n.endswith(".csv") else t), today=TODAY), "header")
    raises(da.AutomationError, lambda: da.validate_outputs(fresh_reports(edit=lambda n, t: "\n".join(t.splitlines()[:5]) + "\n" if n.endswith(".csv") else t), today=TODAY), "")
    folder = fresh_reports(); open(os.path.join(folder, "report_2026-10-27_data.csv"), "wb").write(b"date\xff\xfe,x\n"); raises(da.AutomationError, lambda: da.validate_outputs(folder, today=TODAY), "")
    folder = fresh_reports(); open(os.path.join(folder, "report_2026-10-27_data.csv"), "wb").write(b"x" * (300 * 1024)); raises(da.AutomationError, lambda: da.validate_outputs(folder, today=TODAY), "")

@R.register("validate: a report that disagrees with the CSV (changed correlation, changed window, missing rule label) is refused")
def _():
    for old, new in (("0.928089", "0.928090"), ("2026-09-28 to 2026-10-27", "2026-09-27 to 2026-10-26"), ("strong positive", "weak positive")):
        raises(da.AutomationError, lambda: da.validate_outputs(fresh_reports(edit=lambda n, t: t.replace(old, new) if n.endswith(".md") else t), today=TODAY), "")

@R.register("validate: the CSV's last date must be the date in the file names")
def _():
    folder = fresh_reports(); shutil.copy(os.path.join(folder, "report_2026-10-27.md"), os.path.join(folder, "report_2026-10-28.md")); shutil.copy(os.path.join(folder, "report_2026-10-27_data.csv"), os.path.join(folder, "report_2026-10-28_data.csv"))
    raises(da.AutomationError, lambda: da.validate_outputs(folder, today=date(2026, 10, 29)), "2026-10-28")

# ================================================================ payload mapping
@R.register("payload: one row per asset with exactly the ten headings in the agreed order; BTC then ETH")
def _():
    rows = da.build_rows(da.validate_outputs(REAL_REPORTS, today=TODAY), RUN_ID); assert [r["Asset"] for r in rows] == ["BTC", "ETH"]
    for row in rows: assert list(row) == HEADERS, list(row)
    assert da.SHEET_HEADERS == HEADERS

@R.register("payload: values equal an independent calculation from the CSV (close, daily return as a fraction, sample stdev not annualised, negative drawdown fraction, same correlation on both rows)")
def _():
    s = real_series(); rows = da.build_rows(da.validate_outputs(REAL_REPORTS, today=TODAY), RUN_ID); expected_corr = statistics.correlation(s["BTC_ret"][-30:], s["ETH_ret"][-30:])
    for row, asset in zip(rows, ("BTC", "ETH")):
        ret = s[asset + "_ret"][-30:]
        assert row["Run date"] == "2026-10-27" and row["Workflow run ID"] == RUN_ID
        assert abs(row["Closing price"] - s[asset][-1]) < 1e-9, row["Closing price"]
        assert abs(row["Daily return"] - ret[-1]) < 1e-12 and abs(row["Daily return"]) < 1, "must be a fraction, not a percent"
        assert abs(row["30-day volatility"] - statistics.stdev(ret)) < 1e-12, "daily sample standard deviation, not annualised"
        assert abs(row["30-day drawdown"] - worst_fall(s[asset][-31:])) < 1e-12 and row["30-day drawdown"] <= 0
        assert abs(row["Correlation"] - expected_corr) < 1e-9 and row["Correlation"] == rows[0]["Correlation"]
        assert row["Validation status"] == "PASSED" and row["Report status"] == "GENERATED"
        for key in ("Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation"): assert isinstance(row[key], float), key

@R.register("payload: figures also agree with the Markdown report (correlation 0.928089, BTC drawdown -7.52%, ETH -14.89%)")
def _():
    rows = da.build_rows(da.validate_outputs(REAL_REPORTS, today=TODAY), RUN_ID)
    assert round(rows[0]["Correlation"], 6) == 0.928089 and round(rows[0]["30-day drawdown"], 4) == -0.0752 and round(rows[1]["30-day drawdown"], 4) == -0.1489

@R.register("payload: the three request bodies are built once, contain no token, and are strict JSON (no NaN)")
def _():
    outputs = da.validate_outputs(REAL_REPORTS, today=TODAY); payloads = da.build_payloads(outputs, RUN_ID, RUN_URL)
    assert set(payloads) == {"upsert_analysis", "verify_analysis", "send_success_email"}
    for action, body in payloads.items():
        assert body["action"] == action and body["analysis_date"] == "2026-10-27" and "token" not in body; text = json.dumps(body, allow_nan=False)
        assert TOKEN not in text and "script.google.com" not in text
    assert payloads["upsert_analysis"]["rows"] == payloads["verify_analysis"]["rows"] == payloads["send_success_email"]["rows"]
    email = payloads["send_success_email"]; assert email["run_id"] == RUN_ID and email["workflow_url"] == RUN_URL and email["validation_status"] == "PASSED" and "sheet_update" not in email
    s = email["summary"]; assert set(s) == {"btc", "eth", "correlation", "rules", "observations"} and set(s["rules"]) == {"R1", "R2", "R3"}
    assert s["rules"] == {"R1": "strong positive", "R2": "ETH noticeably more volatile than BTC", "R3": "down days largely coincide"}, s["rules"]
    assert 1 <= len(s["observations"]) <= 8 and all(isinstance(o, str) and 0 < len(o) <= 400 for o in s["observations"])

@R.register("payload: observations are capped in number (8) and length (400 characters) before they are sent")
def _():
    outputs, dashboard = da.validate_outputs(REAL_REPORTS, today=TODAY), da._dashboard(); original = dashboard.window_insights
    dashboard.window_insights = lambda dataset, k: ["x" * 1000] * 12
    try: observations = da.build_payloads(outputs, RUN_ID, RUN_URL)["send_success_email"]["summary"]["observations"]
    finally: dashboard.window_insights = original
    assert len(observations) == 8 and all(0 < len(o) <= 400 for o in observations) and observations[0].endswith("..."), [len(o) for o in observations]

@R.register("payload: a run id that is not digits and a run URL that is not this repository's run page are refused")
def _():
    outputs = da.validate_outputs(REAL_REPORTS, today=TODAY)
    for bad in ("", "abc", "12 3", "1;2", "-5", "1" * 25): raises(da.AutomationError, lambda: da.build_payloads(outputs, bad, RUN_URL), "run id")
    for bad in ("https://evil.example/x", RUN_URL.replace("https", "http"), RUN_URL + "/../x", RUN_URL.replace("rizwana-qa", "other"), RUN_URL + "?a=1", ""): raises(da.AutomationError, lambda: da.build_payloads(outputs, RUN_ID, bad), "run URL")

@R.register("payload: a constant series (correlation undefined) cannot be written as a row and is refused with a clear reason")
def _():
    outputs = da.validate_outputs(REAL_REPORTS, today=TODAY); outputs["metrics"] = dict(outputs["metrics"], correlation=None)
    raises(da.AutomationError, lambda: da.build_rows(outputs, RUN_ID), "correlation")

@R.register("cli build-payload: writes the three JSON files, exits 0, and no file or output contains a token or webhook URL")
def _():
    reports, out = fresh_reports(), new_temp_dir(); code, stdout, stderr = run_cli(["build-payload", "--reports-dir", reports, "--out-dir", out, "--run-id", RUN_ID, "--run-url", RUN_URL], env={"GOOGLE_APPS_SCRIPT_TOKEN": TOKEN, "GOOGLE_APPS_SCRIPT_URL": "https://script.google.com/macros/s/AKfycbzSECRETDEPLOYMENT/exec"}, now=TODAY)
    assert code == 0, (stdout, stderr); assert sorted(os.listdir(out)) == ["send_success_email.json", "upsert_analysis.json", "verify_analysis.json"]
    blob = stdout + stderr + "".join(open(os.path.join(out, f), encoding="utf-8").read() for f in os.listdir(out)); assert TOKEN not in blob and "AKfycbz" not in blob

@R.register("cli validate: exit 0 with a one-line summary for good outputs; exit 1 with the reason on stderr for stale or broken outputs")
def _():
    code, out, err = run_cli(["validate", "--reports-dir", fresh_reports()], now=TODAY); assert code == 0 and "2026-10-27" in out and "PASSED" in out, (out, err)
    code, out, err = run_cli(["validate", "--reports-dir", fresh_reports()], now=date(2026, 10, 30)); assert code == 1 and "2026-10-29" in err, (out, err)
    code, out, err = run_cli(["validate", "--reports-dir", fresh_reports(remove=["report_2026-10-27.md"])], now=TODAY); assert code == 1 and "report_2026-10-27.md" in err

# ================================================================ posting: transport behaviour
@R.register("post: the token travels in the JSON body only; the redirect is followed with a body-less GET; the parsed JSON is returned")
def _():
    with served(Model()) as server:
        response, attempts = da.post_action(server.url, {"action": "upsert_analysis", "analysis_date": "2026-10-27", "rows": da.build_rows(da.validate_outputs(REAL_REPORTS, today=TODAY), RUN_ID)}, TOKEN)
        assert response["success"] is True and attempts == 1; post = server.posts()[0]; assert json.loads(post["body"])["token"] == TOKEN
        assert "authorization" not in {k.lower() for k in post["headers"]} and post["headers"]["Content-Type"].startswith("application/json"), post["headers"]
        gets = [c for c in server.calls if c["method"] == "GET"]; assert len(gets) == 1 and gets[0]["body"] == b"" and TOKEN not in json.dumps(gets[0]["headers"]) and TOKEN not in gets[0]["path"]
        assert TOKEN not in post["path"], "no token in the URL"

@R.register("post: HTTP 200 with success:false is returned as a failure and is NOT retried when the code is final (UNAUTHORIZED, INVALID_PAYLOAD, HEADER_MISMATCH...)")
def _():
    for code in ("UNAUTHORIZED", "INVALID_PAYLOAD", "HEADER_MISMATCH", "NOT_CONFIGURED", "SHEET_NOT_FOUND", "DUPLICATE_EXISTING_ROWS", "INVALID_REQUEST"):
        with served(lambda i, p, code=code: ("json", {"success": False, "error": code})) as server:
            response, attempts = da.post_action(server.url, {"action": "upsert_analysis"}, TOKEN); assert response["success"] is False and response["error"] == code and attempts == 1 and len(server.posts()) == 1, code

@R.register("post: transient failures are retried at most three times in total, with growing waits: 500s, HTML error pages, LOCK_TIMEOUT, timeouts, refused connections")
def _():
    raising = {"http 500": lambda i, p: ("http", 500, b"boom"), "http 503": lambda i, p: ("http", 503, b"busy"), "html page": lambda i, p: ("html", "<html>Sorry, unable to open the file</html>")}
    for name, responder in raising.items():
        with served(responder) as server:
            raises(da.AutomationError, lambda: da.post_action(server.url, {"action": "upsert_analysis"}, TOKEN), "3 attempts")
            assert len(server.posts()) == 3, f"{name}: {len(server.posts())} attempts"; assert server.pauses == [1, 2], server.pauses
    for code in ("LOCK_TIMEOUT", "WRITE_UNCONFIRMED"):                      # a JSON answer is kept: after the last attempt the caller sees the service's own error code
        with served(lambda i, p, c=code: ("json", {"success": False, "error": c})) as server:
            response, attempts = da.post_action(server.url, {"action": "upsert_analysis"}, TOKEN); assert response["error"] == code and attempts == 3 and len(server.posts()) == 3 and server.pauses == [1, 2], (code, attempts)
    with served(lambda i, p: ("sleep", 2.0, ("json", {"success": True})), timeout=0.3) as server:
        raises(da.AutomationError, lambda: da.post_action(server.url, {"action": "x"}, TOKEN), ""); assert len(server.posts()) == 3
    with served(Model()) as server: url = server.url
    da_pattern = da.WEBHOOK_URL_PATTERN; da.WEBHOOK_URL_PATTERN = re.compile(r"^http://127\.0\.0\.1:\d+/macros/s/[A-Za-z0-9_-]{20,}/exec$"); pauses = []; old = da.pause; da.pause = pauses.append
    try: raises(da.AutomationError, lambda: da.post_action(url, {"action": "x"}, TOKEN), ""); assert pauses == [1, 2], pauses
    finally: da.WEBHOOK_URL_PATTERN, da.pause = da_pattern, old

@R.register("post: a transient failure followed by success is accepted (second attempt), and the attempt count is reported")
def _():
    with served(lambda i, p: ("http", 502, b"bad gateway") if i < 1 else ("json", {"success": True, "ok": 1})) as server:
        response, attempts = da.post_action(server.url, {"action": "x"}, TOKEN); assert response["success"] is True and attempts == 2, attempts

@R.register("post: client errors (401, 403, 404, 400) are final - one request only")
def _():
    for status in (400, 401, 403, 404):
        with served(lambda i, p, s=status: ("http", s, b"no")) as server:
            raises(da.AutomationError, lambda: da.post_action(server.url, {"action": "x"}, TOKEN), str(status)); assert len(server.posts()) == 1, status

@R.register("post: a non-JSON 200 reply, a JSON array/scalar, and a reply over 64 KB are refused (never parsed loosely)")
def _():
    for spec in (("html", "<html>login</html>"), ("html", "[1,2]"), ("html", "42"), ("html", ""), ("html", "{'success': true}")):
        with served(lambda i, p, s=spec: s) as server: raises(da.AutomationError, lambda: da.post_action(server.url, {"action": "x"}, TOKEN), "")
    with served(lambda i, p: ("big", 200_000)) as server: raises(da.AutomationError, lambda: da.post_action(server.url, {"action": "x"}, TOKEN), "large")

@R.register("post: the webhook URL must be https://script.google.com/macros/s/<id>/exec - anything else is refused BEFORE any request is made")
def _():
    with served(Model(), allow_local=False) as server:
        for url in (server.url, "http://script.google.com/macros/s/AKfycbzabcdefghijklmnopq/exec", "https://script.google.com.evil.example/macros/s/AKfycbzabcdefghijklmnopq/exec", "https://evil.example/macros/s/AKfycbzabcdefghijklmnopq/exec",
                    "https://user:pw@script.google.com/macros/s/AKfycbzabcdefghijklmnopq/exec", "https://script.google.com/macros/s/AKfycbzabcdefghijklmnopq/exec?x=1", "https://script.google.com/macros/s/AKfycbzabcdefghijklmnopq/dev", "", "file:///etc/passwd", "https://script.google.com/macros/s/short/exec"):
            raises(da.AutomationError, lambda: da.post_action(url, {"action": "x"}, TOKEN), "URL")
        assert server.calls == [], "no request may be made"
    da.validate_webhook_url("https://script.google.com/macros/s/AKfycbzabcdefghijklmnopqrstuvwxyz0123456789/exec")

@R.register("post: errors and output never contain the token or the URL, even when the server echoes them back")
def _():
    with served(lambda i, p: ("json", {"success": False, "error": "INVALID_PAYLOAD", "message": "bad token " + p["token"], "detail": "see " + "ECHO"})) as server:
        code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", prepared_dir()], env=env_for(server)); blob = out + err
        assert code == 1 and TOKEN not in blob and server.url not in blob and "FAKEDEPLOYMENTID" not in blob, blob
    for secret in ('tok"en\\x', "tok en/+="):
        text = f"x {secret} y {json.dumps(secret)} z {secret.replace(' ', '%20')}"; cleaned = da.redact(text, [secret]); assert secret not in cleaned and json.dumps(secret)[1:-1] not in cleaned, cleaned
    assert da.redact("nothing here", [""]) == "nothing here", "an empty secret must not blank the text"

@R.register("post: eight parallel posts all succeed independently (no shared state between calls)")
def _():
    results = []
    with served(Model()) as server:
        def work():
            try: results.append(da.post_action(server.url, {"action": "verify_analysis", "rows": []}, TOKEN)[0]["rows_found"])
            except BaseException as error: results.append(repr(error))
        threads = [threading.Thread(target=work) for _ in range(8)]; [t.start() for t in threads]; [t.join(30) for t in threads]
        assert results == [0] * 8, results

# ================================================================ cli: configuration and the full flow
@R.register("cli post: missing or blank secrets give a clear error naming the variables (not values), exit 1 and no request")
def _():
    with served(Model()) as server:
        for env in ({}, {"GOOGLE_APPS_SCRIPT_URL": server.url}, {"GOOGLE_APPS_SCRIPT_TOKEN": TOKEN}, env_for(server, GOOGLE_APPS_SCRIPT_TOKEN="   "), env_for(server, GOOGLE_APPS_SCRIPT_URL="")):
            code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", prepared_dir()], env=env); assert code == 1 and re.search("GOOGLE_APPS_SCRIPT_(URL|TOKEN)", err), (code, err); assert TOKEN not in out + err
        assert server.calls == []

@R.register("cli post: surrounding whitespace or a trailing newline pasted into a secret is removed before use")
def _():
    with served(Model()) as server:
        code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", prepared_dir()], env=env_for(server, GOOGLE_APPS_SCRIPT_TOKEN=" " + TOKEN + "\r\n")); assert code == 0, (out, err); assert json.loads(server.posts()[0]["body"])["token"] == TOKEN

@R.register("cli flow: validate -> build -> upsert -> verify -> email succeeds; results are written without secrets; spreadsheet and email statuses are reported separately")
def _():
    model = Model()
    with served(model) as server:
        out_dir = prepared_dir(); summary = os.path.join(new_temp_dir(), "summary.md"); env = env_for(server, GITHUB_STEP_SUMMARY=summary)
        outputs = []
        for action in ("upsert_analysis", "verify_analysis", "send_success_email"):
            code, out, err = run_cli(["post", "--action", action, "--dir", out_dir], env=env); outputs.append(out + err); assert code == 0, (action, out, err)
        assert len(model.rows) == 2 and len(model.mail) == 1
        up, ver, mail = (read_json(out_dir, f"{a}_result.json") for a in ("upsert_analysis", "verify_analysis", "send_success_email"))
        assert up["rows_inserted"] == 2 and up["sheet_status"] == "ok" and ver["matches"] is True and ver["rows_found"] == 2 and mail["email_status"] == "sent" and mail["sheet_status"] == "ok"
        sent = model.mail[0]; assert sent["sheet_update"] == {"rows_inserted": 2, "rows_updated": 0} and sent["run_id"] == RUN_ID and sent["workflow_url"] == RUN_URL
        blob = "".join(outputs) + "".join(open(os.path.join(out_dir, f), encoding="utf-8").read() for f in os.listdir(out_dir)) + open(summary, encoding="utf-8").read()
        assert TOKEN not in blob and server.url not in blob and "FAKEDEPLOYMENTID" not in blob
        text = open(summary, encoding="utf-8").read(); assert re.search(r"Spreadsheet[^\n]*ok", text) and re.search(r"(Success )?[Ee]mail[^\n]*sent", text), text

@R.register("duplicate execution: running the whole flow twice leaves two rows and reports updates, not inserts, the second time")
def _():
    model = Model()
    with served(model) as server:
        for round_number in (1, 2):
            out_dir = prepared_dir(); env = env_for(server)
            assert run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env)[0] == 0
            up = read_json(out_dir, "upsert_analysis_result.json"); assert (up["rows_inserted"], up["rows_updated"]) == ((2, 0) if round_number == 1 else (0, 2)), up
        assert len(model.rows) == 2

@R.register("success is judged by the JSON content, not by HTTP 200: success:false, sheet not ok, unconfirmed write, wrong row counts, missing fields all fail the step")
def _():
    base = {"success": True, "action": "upsert_analysis", "sheet_status": "ok", "rows_inserted": 2, "rows_updated": 0, "write_confirmed": True, "email_status": "not_attempted"}
    bad = [dict(base, success=False, error="WRITE_UNCONFIRMED"), dict(base, sheet_status="unconfirmed"), dict(base, sheet_status="failed"), dict(base, write_confirmed=False), dict(base, rows_inserted=1), dict(base, rows_inserted=2, rows_updated=2), dict(base, rows_inserted="2"), dict(base, rows_inserted=True, rows_updated=1),
           {k: v for k, v in base.items() if k != "write_confirmed"}, {k: v for k, v in base.items() if k != "rows_inserted"}, dict(base, success="true"), {"success": True}, {}]
    for response in bad:
        with served(lambda i, p, r=response: ("json", r)) as server:
            out_dir = prepared_dir(); code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server)); assert code == 1, (response, out, err)
            assert os.path.exists(os.path.join(out_dir, "upsert_analysis_result.json")), "the result is recorded even when it is a failure"
    with served(lambda i, p: ("json", base)) as server:
        out_dir = prepared_dir(); assert run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server))[0] == 0
    for response in ({"success": True, "matches": False, "rows_found": 2, "duplicate_keys": 0}, {"success": True, "matches": True, "rows_found": 1, "duplicate_keys": 0}, {"success": True, "matches": True, "rows_found": 2, "duplicate_keys": 1}, {"success": False, "matches": True, "rows_found": 2, "duplicate_keys": 0}, {"success": True, "matches": True}):
        with served(lambda i, p, r=response: ("json", r)) as server:
            code, out, err = run_cli(["post", "--action", "verify_analysis", "--dir", prepared_dir()], env=env_for(server)); assert code == 1, response

@R.register("no success email when the spreadsheet update or its verification did not succeed (no request is even sent)")
def _():
    with served(Model()) as server:
        out_dir = prepared_dir(); code, out, err = run_cli(["post", "--action", "send_success_email", "--dir", out_dir], env=env_for(server)); assert code == 1 and "spreadsheet" in err.lower() and server.calls == [], (err, server.calls)
        assert run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server))[0] == 0; before = len(server.calls)
        code, out, err = run_cli(["post", "--action", "send_success_email", "--dir", out_dir], env=env_for(server)); assert code == 1 and len(server.calls) == before, "verification result missing"
        for name, patch in (("upsert_analysis_result.json", {"sheet_status": "failed"}), ("upsert_analysis_result.json", {"success": False}), ("verify_analysis_result.json", {"matches": False})):
            folder = prepared_dir(); run_cli(["post", "--action", "upsert_analysis", "--dir", folder], env=env_for(server)); run_cli(["post", "--action", "verify_analysis", "--dir", folder], env=env_for(server))
            data = read_json(folder, name); data.update(patch); json.dump(data, open(os.path.join(folder, name), "w", encoding="utf-8")); before = len(server.calls)
            code, out, err = run_cli(["post", "--action", "send_success_email", "--dir", folder], env=env_for(server)); assert code == 1 and len(server.calls) == before, (name, patch)

@R.register("email failure is reported on its own: the spreadsheet stays ok in the summary, the email is failed, the step exits 1")
def _():
    model = Model(); model.fail_mail = True
    with served(model) as server:
        out_dir = prepared_dir(); summary = os.path.join(new_temp_dir(), "s.md"); env = env_for(server, GITHUB_STEP_SUMMARY=summary)
        for action in ("upsert_analysis", "verify_analysis"): assert run_cli(["post", "--action", action, "--dir", out_dir], env=env)[0] == 0
        code, out, err = run_cli(["post", "--action", "send_success_email", "--dir", out_dir], env=env); assert code == 1
        result = read_json(out_dir, "send_success_email_result.json"); assert result["sheet_status"] == "ok" and result["email_status"] == "failed" and result["success"] is False
        text = open(summary, encoding="utf-8").read(); assert re.search(r"Spreadsheet[^\n]*ok", text) and re.search(r"[Ee]mail[^\n]*failed", text), text

@R.register("invalid authentication (wrong token) fails the step with UNAUTHORIZED and never reports the sheet as updated")
def _():
    with served(Model(token="the-real-one")) as server:
        out_dir = prepared_dir(); code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server)); assert code == 1 and "UNAUTHORIZED" in out + err
        result = read_json(out_dir, "upsert_analysis_result.json"); assert result["success"] is False and result["sheet_status"] != "ok" and result["error"] == "UNAUTHORIZED"

@R.register("webhook failure after retries: exit 1, result file says the spreadsheet is unconfirmed, no secrets in output")
def _():
    with served(lambda i, p: ("http", 500, b"down")) as server:
        out_dir = prepared_dir(); code, out, err = run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server)); assert code == 1 and len(server.posts()) == 3 and TOKEN not in out + err
        result = read_json(out_dir, "upsert_analysis_result.json"); assert result["success"] is False and result["sheet_status"] == "unconfirmed" and result["attempts"] == 3

@R.register("result files hold only whitelisted, length-limited fields (a hostile server cannot plant long text or unknown keys)")
def _():
    hostile = {"success": True, "action": "upsert_analysis", "sheet_status": "ok", "rows_inserted": 2, "rows_updated": 0, "write_confirmed": True, "email_status": "not_attempted", "secret_dump": "x" * 5000, "error": "E" * 5000}
    with served(lambda i, p: ("json", hostile)) as server:
        out_dir = prepared_dir(); run_cli(["post", "--action", "upsert_analysis", "--dir", out_dir], env=env_for(server)); result = read_json(out_dir, "upsert_analysis_result.json")
        assert "secret_dump" not in result and len(json.dumps(result)) < 1000, result

# ================================================================ failure notification
@R.register("derive_stage: the first failed step in pipeline order is reported; unknown names are refused; nothing failed gives 'unknown'")
def _():
    outcomes = {"checkout": "success", "setup_python": "success", "install_dependencies": "success", "tests": "failure", "analysis": "skipped", "validate_outputs": "skipped"}
    assert da.derive_stage(outcomes) == "tests"; assert da.derive_stage(dict(outcomes, tests="success", analysis="failure", post_to_sheet="failure")) == "analysis"
    assert da.derive_stage({"checkout": "success"}) == "unknown"; assert da.derive_stage({"verify_sheet": "cancelled"}) == "verify_sheet"
    raises(da.AutomationError, lambda: da.derive_stage({"hack": "failure"}), "stage")

@R.register("notify-failure: a failure before the webhook (tests) sends one notice naming the stage, run id, run URL and spreadsheet status not_attempted")
def _():
    model = Model()
    with served(model) as server:
        out_dir = new_temp_dir(); code, out, err = run_cli(["notify-failure", "--dir", out_dir, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "checkout=success", "--outcome", "tests=failure", "--outcome", "analysis=skipped"], env=env_for(server))
        assert code == 0, (out, err); assert len(model.mail) == 1; sent = model.mail[0]
        assert sent["action"] == "notify_failure" and sent["stage"] == "tests" and sent["sheet_status"] == "not_attempted" and sent["run_id"] == RUN_ID and sent["workflow_url"] == RUN_URL and "tests" in sent["error_summary"]
        assert TOKEN not in out + err

@R.register("notify-failure: the spreadsheet status comes from the recorded upsert result (ok / failed / unconfirmed)")
def _():
    for result, expected in ({"success": True, "sheet_status": "ok"}, "ok"), ({"success": False, "sheet_status": "failed", "error": "HEADER_MISMATCH"}, "failed"), ({"success": False, "sheet_status": "unconfirmed", "error": "WRITE_UNCONFIRMED"}, "unconfirmed"):
        model = Model()
        with served(model) as server:
            out_dir = new_temp_dir(); json.dump(result, open(os.path.join(out_dir, "upsert_analysis_result.json"), "w", encoding="utf-8"))
            code, out, err = run_cli(["notify-failure", "--dir", out_dir, "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "post_to_sheet=failure"], env=env_for(server)); assert code == 0, err
            assert model.mail[0]["sheet_status"] == expected and model.mail[0]["stage"] == "post_to_sheet", model.mail[0]
            if "error" in result: assert result["error"] in model.mail[0]["error_summary"]

@R.register("notify-failure: without secrets it says plainly that no notice could be sent (exit 1); with a wrong token it exits 1; the original failure stays the cause")
def _():
    with served(Model()) as server:
        args = ["notify-failure", "--dir", new_temp_dir(), "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=failure"]
        code, out, err = run_cli(args, env={}); assert code == 1 and "notif" in (out + err).lower() and server.calls == []
        code, out, err = run_cli(args, env=env_for(server, GOOGLE_APPS_SCRIPT_TOKEN="wrong-token")); assert code == 1 and TOKEN not in out + err

@R.register("notify-failure: bad run id, bad run URL and unknown step names are refused before any request")
def _():
    with served(Model()) as server:
        for extra in (["--run-id", "x1"], ["--run-url", "https://evil.example"], ["--outcome", "hack=failure"]):
            args = ["notify-failure", "--dir", new_temp_dir(), "--run-id", RUN_ID, "--run-url", RUN_URL, "--outcome", "tests=failure"] + extra
            code, out, err = run_cli(args, env=env_for(server)); assert code == 1, extra
        assert server.calls == []

@R.register("main: an error message that happens to contain a secret is redacted before it is printed")
def _():
    url, original = "https://script.google.com/macros/s/AKfycbzSECRETDEPLOYMENT0123/exec", da.COMMANDS["validate"]
    def explode(args, env, today): raise da.AutomationError("boom " + TOKEN + " at " + url + " and " + "AKfycbzSECRETDEPLOYMENT0123")
    da.COMMANDS["validate"] = explode
    try: code, out, err = run_cli(["validate", "--reports-dir", "x"], env={"GOOGLE_APPS_SCRIPT_URL": url, "GOOGLE_APPS_SCRIPT_TOKEN": TOKEN})
    finally: da.COMMANDS["validate"] = original
    assert code == 1 and "[redacted]" in err and TOKEN not in out + err and "AKfycbz" not in out + err, err

# ================================================================ self-check command
SELF_LABELS = ["authentication", "script properties", "spreadsheet ID", "worksheet Sheet1", "ten headings", "email configuration"]
def self_lines(text): return {label: next((l for l in text.splitlines() if label in l and l.startswith("self-check:")), "") for label in SELF_LABELS}
def run_self_check(model_or_responder, extra_args=(), **env_extra):
    with served(model_or_responder) as server:
        code, out, err = run_cli(["self-check", *extra_args], env=env_for(server, **env_extra)); return code, out, err, server

@R.register("self-check: everything in order -> exit 0, six PASS lines, one read-only request, nothing written, nothing mailed")
def _():
    model = Model(); code, out, err, server = run_self_check(model)
    assert code == 0, (out, err); lines = self_lines(out + err); assert all("PASS" in line for line in lines.values()), lines; assert "read-only" in out and "self-check: OK" in out
    assert len(server.posts()) == 1 and len(model.self_check_requests) == 1, "exactly one request"; body = json.loads(server.posts()[0]["body"])
    assert body["action"] == "self_check" and set(body) == {"action", "token", "expected_sheet_name"} and body["expected_sheet_name"] == "Sheet1" and body["token"] == TOKEN, body
    assert model.rows == {} and model.mail == [], "the command must not write rows or send e-mail"

@R.register("self-check: the token, the web app address and its deployment id never appear in the output, the job summary or the error text")
def _():
    summary = os.path.join(new_temp_dir(), "s.md")
    with served(Model()) as server:
        ok = run_cli(["self-check"], env=env_for(server, GITHUB_STEP_SUMMARY=summary))
        bad = run_cli(["self-check"], env=env_for(server, GOOGLE_APPS_SCRIPT_TOKEN="wrong-token-0123456789"))
        blob = "".join(ok[1:] + bad[1:]) + open(summary, encoding="utf-8").read()
        for secret in (TOKEN, "wrong-token-0123456789", server.url, "FAKEDEPLOYMENTID", "127.0.0.1"): assert secret not in blob, secret
    text = open(summary, encoding="utf-8").read(); assert "self-check" in text.lower() and text.count("PASS") >= 6, text

FAILURES = {
    "a missing script property": (failing_reply(properties=missing_property("EMAIL_TO"), email_valid=False), {"script properties": "EMAIL_TO", "email configuration": "FAIL"}),
    "several missing properties": (failing_reply(properties={n: n not in ("SHEET_NAME", "DATE_HEADER") for n in PROPERTY_NAMES}), {"script properties": "SHEET_NAME"}),
    "a wrong spreadsheet id": (failing_reply(spreadsheet_found=False, sheet_found=False, headers_ok=False, sheet_name_matches=False), {"spreadsheet ID": "FAIL", "worksheet Sheet1": "NOT CHECKED", "ten headings": "NOT CHECKED"}),
    "a missing worksheet": (failing_reply(sheet_found=False, headers_ok=False), {"spreadsheet ID": "PASS", "worksheet Sheet1": "FAIL", "ten headings": "NOT CHECKED"}),
    "SHEET_NAME that is not Sheet1": (failing_reply(sheet_name_matches=False), {"worksheet Sheet1": "FAIL"}),
    "headings that differ": (failing_reply(headers_ok=False), {"worksheet Sheet1": "PASS", "ten headings": "FAIL"}),
    "an invalid e-mail address": (failing_reply(email_valid=False), {"email configuration": "FAIL"}),
}
@R.register("self-check: each problem fails exactly its own check, exits 1, and says which one (" + str(len(FAILURES)) + " cases)")
def _():
    for name, (reply, expectations) in FAILURES.items():
        model = Model(); model.self_check_reply = reply; code, out, err, server = run_self_check(model); lines = self_lines(out + err)
        assert code == 1, name; assert "self-check: FAILED" in out + err, name
        for label, expected in expectations.items(): assert expected in lines[label], (name, label, lines[label])
        assert "PASS" in lines["authentication"], (name, lines["authentication"]); assert model.rows == {} and model.mail == [] and len(server.posts()) == 1, name

@R.register("self-check: a wrong token (UNAUTHORIZED) fails authentication and leaves the other five checks NOT CHECKED")
def _():
    code, out, err, server = run_self_check(Model(token="the-real-one")); lines = self_lines(out + err)
    assert code == 1 and "FAIL" in lines["authentication"] and "does not match" in lines["authentication"], lines
    for label in SELF_LABELS[1:]: assert "NOT CHECKED" in lines[label], (label, lines[label])
    assert len(server.posts()) == 1

@R.register("self-check: NOT_CONFIGURED without details (the web app has no usable SHARED_SECRET) fails authentication and names SHARED_SECRET")
def _():
    code, out, err, server = run_self_check(lambda i, p: ("json", {"success": False, "error": "NOT_CONFIGURED"})); lines = self_lines(out + err)
    assert code == 1 and "FAIL" in lines["authentication"] and "SHARED_SECRET" in lines["authentication"], lines

@R.register("self-check: replies from the OLD script or an older Code.gs (unknown error text, missing new fields) fail with a hint to deploy the updated Code.gs")
def _():
    old = [{"success": False, "error": "Unauthorized request"}, {"success": False, "error": "Invalid payload"}, {"success": True, "rows_written": 0}, {"success": True}]
    reduced = lambda name: {k: v for k, v in self_reply().items() if k != name}
    missing_fields = [self_reply(spreadsheet_found=None), reduced("sheet_name_matches"), reduced("spreadsheet_found"), reduced("properties")]
    for reply in old + missing_fields:
        model = Model(); model.self_check_reply = reply; code, out, err, server = run_self_check(model); assert code == 1, reply; assert "Code.gs" in out + err and "deployed" in out + err, (reply, out, err)

@R.register("self-check: wrongly typed values (strings instead of booleans, success as text, a failure flag) never count as a pass; hostile extra fields are ignored")
def _():
    for reply in (self_reply(success="true"), self_reply(headers_ok="true"), self_reply(spreadsheet_found=1), self_reply(properties={n: "yes" for n in PROPERTY_NAMES}), self_reply(properties={"EMAIL_TO": True}), self_reply(success=False)):
        model = Model(); model.self_check_reply = reply; code, out, err, server = run_self_check(model); assert code == 1, reply
    hostile = self_reply(); hostile["note"] = "x" * 5000; hostile["message"] = "token " + TOKEN
    model = Model(); model.self_check_reply = hostile; code, out, err, server = run_self_check(model); assert code == 0 and TOKEN not in out + err and "x" * 50 not in out + err

@R.register("self-check: missing secrets or a web app address of the wrong kind stop before any request (exit 1)")
def _():
    with served(Model(), allow_local=False) as server:
        for env in ({}, {"GOOGLE_APPS_SCRIPT_URL": server.url}, {"GOOGLE_APPS_SCRIPT_TOKEN": TOKEN}, env_for(server)):
            code, out, err = run_cli(["self-check"], env=env); assert code == 1 and TOKEN not in out + err, env
        assert server.calls == []

@R.register("self-check: transport failures are retried at most three times, then reported (exit 1, authentication not confirmed)")
def _():
    code, out, err, server = run_self_check(lambda i, p: ("http", 503, b"busy")); assert code == 1 and len(server.posts()) == 3 and server.pauses == [1, 2] and TOKEN not in out + err
    lines = self_lines(out + err); assert "FAIL" in lines["authentication"] or "NOT CHECKED" in lines["authentication"], lines

@R.register("self-check: takes no options; an unknown option is the usual usage error (exit 2) and sends nothing")
def _():
    with served(Model()) as server:
        assert run_cli(["self-check", "--dir", "x"], env=env_for(server))[0] == 2 and run_cli(["self-check", "--action", "upsert_analysis"], env=env_for(server))[0] == 2 and server.calls == []

@R.register("self-check: the expected worksheet name is the agreed one, Sheet1, and the command's code never refers to a state-changing action or opens a file")
def _():
    assert da.EXPECTED_SHEET_NAME == "Sheet1"
    source = open(os.path.join(REPO, "src", "daily_automation.py"), encoding="utf-8").read(); section = source[source.index("def command_self_check"):source.index("def build_parser")]
    for forbidden in ("upsert_analysis", "send_success_email", "notify_failure", "verify_analysis", "write_json", "open("): assert forbidden not in section, forbidden

@R.register("the email steps do not trust a bare success flag: success:true together with email_status failed / not_attempted / missing, or a sheet_status that is not ok, is still a failure")
def _():
    sent = {"success": True, "sheet_status": "ok", "email_status": "sent"}
    assert da.problems_in("send_success_email", sent, 2) == [] and da.problems_in("send_success_email", dict(sent, email_status="skipped_duplicate"), 2) == [] and da.problems_in("notify_failure", dict(sent, sheet_status="not_attempted"), 0) == []
    for status in ("failed", "not_attempted"): assert da.problems_in("send_success_email", dict(sent, email_status=status), 2) and da.problems_in("notify_failure", dict(sent, email_status=status), 0), status
    assert da.problems_in("send_success_email", {"success": True, "sheet_status": "ok"}, 2) and da.problems_in("notify_failure", {"success": True}, 0), "a missing email_status is a failure"
    for status in ("failed", "unconfirmed", "not_attempted"): assert da.problems_in("send_success_email", dict(sent, sheet_status=status), 2), status
    assert da.problems_in("send_success_email", {"success": True, "email_status": "sent"}, 2), "a missing sheet_status is a failure"

# ================================================================ source hygiene of the new module
@R.register("source: no hard-coded secret or URL, no shell, no TLS bypass, no printing of the secret variables, no debug leftovers")
def _():
    source = open(os.path.join(REPO, "src", "daily_automation.py"), encoding="utf-8").read()
    for needle, why in (("verify=False", "TLS bypass"), ("shell=True", "shell"), ("os.system", "shell"), ("eval(", "eval"), ("exec(", "exec"), ("pdb", "debugger"), ("breakpoint(", "debugger"), ("AKfycb", "a deployment id")): assert needle not in source, why
    assert not re.search(r"print\([^)]*(TOKEN|SCRIPT_URL)", source), "printing a secret variable"; assert not re.search(r"https?://(?!script\.google\.com|github\.com|api\.kraken)[A-Za-z0-9.-]+/", re.sub(r"#.*", "", source).replace("http://127.0.0.1", "")), "an unexpected URL literal"
    assert "GOOGLE_APPS_SCRIPT_URL" in source and "GOOGLE_APPS_SCRIPT_TOKEN" in source, "the existing secret names are used"
    assert "SHEETS_WEBHOOK" not in source, "no second set of secret names"

R.finish()
