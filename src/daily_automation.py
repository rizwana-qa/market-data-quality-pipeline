"""Daily automation glue: validate the generated report and CSV, build the payloads, post them to the Google Apps Script web app, check the replies, send the failure notice.

Run by the GitHub Actions workflow (see .github/workflows/daily-market-analysis.yml):

    python3 src/daily_automation.py validate --reports-dir reports
    python3 src/daily_automation.py build-payload --reports-dir reports --out-dir automation_out --run-id ID --run-url URL
    python3 src/daily_automation.py post --action upsert_analysis|verify_analysis|send_success_email --dir automation_out
    python3 src/daily_automation.py post --action send_success_report --dir automation_out --artifact-url URL     (the HTML e-mail; the workflow uses this one)
    python3 src/daily_automation.py notify-failure --dir automation_out --run-id ID --run-url URL --outcome step=outcome ...
    python3 src/daily_automation.py stage-deliverables --reports-dir reports --dashboard-dir dashboard_out --out-dir deliverables --date YYYY-MM-DD
    python3 -I -S src/daily_automation.py self-check    (read-only: checks the web app's configuration; writes no row and sends no email)

The three commands that read the secrets (post, notify-failure, self-check) use the standard library only and are started with python3 -I -S: no site module, no PYTHON*
variables, no user site and no script folder on the import path, so start-up code left behind by an installed package (a .pth file, sitecustomize.py) cannot run in the process
that holds the token. validate, build-payload and stage-deliverables import dashboard.py (and build-payload email_report.py) from the script folder and never see the secrets, so they run without -I.

The calculations are not repeated here: validation and metrics come from dashboard.py and analysis.py. This module only maps their results onto the ten sheet columns
and talks to the web app. It uses the standard library only, so the failure notice still works when installing the dependencies was the step that failed.

The web app address and the shared secret come from the environment (GitHub Actions secrets GOOGLE_APPS_SCRIPT_URL and GOOGLE_APPS_SCRIPT_TOKEN). They are added to a
request in memory only; they are never written to a file, a result, the job summary or the log (all output passes through redact()).
"""

import argparse
import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

URL_ENV = "GOOGLE_APPS_SCRIPT_URL"
TOKEN_ENV = "GOOGLE_APPS_SCRIPT_TOKEN"
SHEET_HEADERS = ["Run date", "Asset", "Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation", "Validation status", "Report status", "Workflow run ID"]
WEBHOOK_URL_PATTERN = re.compile(r"https://script\.google\.com/(?:macros|a/macros/[A-Za-z0-9.-]+)/s/[A-Za-z0-9_-]{20,}/exec")
RUN_URL_PATTERN = re.compile(r"https://github\.com/rizwana-qa/market-data-quality-pipeline/actions/runs/(\d{1,20})")
RUN_ID_PATTERN = re.compile(r"\d{1,20}")
STAGE_ORDER = ["checkout", "setup_python", "install_dependencies", "tests", "clear_stale_outputs", "analysis", "validate_outputs", "build_dashboard", "stage_deliverables", "build_payload", "post_to_sheet", "verify_sheet",
               "upload_deliverables", "send_success_email", "upload_artifacts"]          # the workflow steps, in pipeline order
SERVER_STAGE = {"clear_stale_outputs": "analysis", "build_dashboard": "analysis", "stage_deliverables": "validate_outputs", "upload_deliverables": "upload_artifacts"}
# ^ apps_script/Code.gs accepts only the stage names in its own STAGES list. A workflow step it does not know is reported as the nearest stage it does know; the real step name stays in the error summary.
#   Every other step name is sent unchanged. Nothing is added to Code.gs, so the deployed web app needs no change.
DELIVERABLE_NAMES = ("report_{day}.md", "report_{day}_data.csv", "dashboard_{day}.html")      # exactly the three files that are staged, for one analysis date
ANALYSIS_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
FETCHED_TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"       # how the report writes its 'Data fetched' time (always UTC)
MAX_DASHBOARD_BYTES = 16 * 1024 * 1024              # the approved dashboard is about 4.9 MB because it embeds Plotly.js; a file far beyond that is not that page
MAX_FILE_TEXT_CHARS = 240                           # the longest piece of text taken from a file that an error message may repeat
OUTCOMES = {"success", "failure", "cancelled", "skipped"}
RESULT_OF_STAGE = {"post_to_sheet": "upsert_analysis", "verify_sheet": "verify_analysis", "send_success_email": "send_success_report"}    # the e-mail step (its id is unchanged) now runs send_success_report
POST_ACTIONS = ["upsert_analysis", "verify_analysis", "send_success_email", "send_success_report"]
REPORT_ACTION = "send_success_report"
SUCCESS_ACTIONS = ("send_success_email", REPORT_ACTION)            # both need the confirmed upsert and the passed read-back, and the web app sends at most one e-mail per run and date for the two together
EXPECTED_SHEET_NAME = "Sheet1"                     # the worksheet the daily rows belong to
SELF_CHECK_PROPERTIES = ["SPREADSHEET_ID", "SHEET_NAME", "SHARED_SECRET", "EMAIL_TO", "DATE_HEADER", "ASSET_HEADER"]
SELF_CHECK_FLAGS = ("spreadsheet_found", "sheet_found", "sheet_name_matches", "headers_ok", "email_valid")
SELF_CHECK_LABELS = ["authentication", "script properties", "spreadsheet ID", "worksheet Sheet1", "ten headings", "email configuration"]
SHEET_STATUSES = {"not_attempted", "ok", "failed", "unconfirmed"}
EMAIL_STATUSES = {"not_attempted", "sent", "skipped_duplicate", "failed", "send_outcome_unknown", "sent_dedup_failed"}   # the last: the HTML e-mail was handed to the mail service and the outcome is not known (never retried, never followed by a plain-text e-mail)
EMAIL_DONE = {"sent", "skipped_duplicate"}
EMAIL_ACCEPTED = EMAIL_DONE | {"sent_dedup_failed"}          # sent_dedup_failed: the e-mail (success e-mail or failure notice) WAS sent, but the web app could not record it for duplicate protection: done, with a warning, and never repeated (the reply is a success, not an error code)
EMAIL_FORMATS = {"html", "text_fallback"}                       # what the web app reports for a success report it sent: the HTML message, or the plain-text one when the HTML could not be sent
ARTIFACT_URL_PATTERN = re.compile(r"https://github\.com/rizwana-qa/market-data-quality-pipeline/actions/runs/(\d{1,20})/artifacts/(\d{1,20})")      # the address the upload-artifact action reports
MAX_REQUEST_CHARS = 19000                                       # the web app refuses a request body of more than 20,000 characters; the client never sends more than this (same value as email_report.MAX_REQUEST_CHARS)
REQUEST_EXTRAS_CHARS = 400                                      # what is added when a report request is posted: the token, the artifact link and the sheet counts
REPORT_REQUEST_KEYS = {"action", "analysis_date", "rows", "run_id", "workflow_url", "validation_status", "summary", "report", "sheet_update", "artifact_url"}
REPORT_PARTS = {"meta", "assets", "pair", "coverage", "charts"}
REPORT_CHART_IDS = ["indexed", "returns", "drawdown"]
MAX_CHART_BYTES = 6000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

WINDOW_DAYS = 30
MAX_OBSERVATIONS = 8
MAX_OBSERVATION_CHARS = 400
REQUEST_TIMEOUT = 60                               # seconds for one request, including the redirect the web app answers with
MAX_ATTEMPTS = 3                                   # one try plus two retries, as in fetch_data.py
RETRY_BASE_DELAY_SECONDS = 1                       # waits 1 s, then 2 s
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_ERROR_CODES = {"LOCK_TIMEOUT", "WRITE_UNCONFIRMED", "INTERNAL_ERROR"}   # the answers of the web app that can succeed on a repeat (the upsert is idempotent)
MAX_REPLY_BYTES = 64 * 1024
MAX_REDIRECTS = 3
INT_LIMIT = 1000


class AutomationError(Exception):
    """A step failed. The message is safe to print. delivery says how far a request got: not_sent, rejected (the web app did not run) or unknown."""

    def __init__(self, message, attempts=0, delivery="not_sent"):
        super().__init__(message)
        self.attempts = attempts
        self.delivery = delivery


class _Retry(Exception):
    """One attempt failed in a way that can be repeated."""


def pause(seconds):
    time.sleep(seconds)                            # a separate function so that tests can replace it


# ---------------------------------------------------------------- secrets and output
def read_secrets(env):
    url = (env.get(URL_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    missing = [name for name, value in ((URL_ENV, url), (TOKEN_ENV, token)) if not value]
    if missing:
        raise AutomationError("missing configuration: " + ", ".join(missing) + " (they are GitHub Actions secrets)")
    return url, token


def secrets_to_hide(env):
    """Everything that must never appear in output: the token, the web app address and the deployment id inside it."""
    hidden = []
    url = (env.get(URL_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    if token:
        hidden.append(token)
    if url:
        hidden.append(url)
        found = re.search(r"/s/([A-Za-z0-9_-]+)/", url)
        if found:
            hidden.append(found.group(1))
    return hidden


def redact(text, secrets):
    text = str(text)
    forms = []
    for secret in secrets:
        if secret:
            forms += [secret, json.dumps(secret)[1:-1], urllib.parse.quote(secret, safe="")]
    for form in sorted(set(forms), key=len, reverse=True):
        text = text.replace(form, "[redacted]")
    return text


def validate_webhook_url(url):
    if not isinstance(url, str) or WEBHOOK_URL_PATTERN.fullmatch(url) is None:
        raise AutomationError("the webhook URL is not a Google Apps Script web app address (expected https://script.google.com/macros/s/<id>/exec)")
    return url


# ---------------------------------------------------------------- validating the generated outputs
def _dashboard():
    import dashboard                               # imported here: the failure notice does not need the analysis code
    return dashboard


def utc_today(now):
    if now is None:
        return datetime.now(timezone.utc).date()
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).date()
    return now


def validate_outputs(reports_dir, today):
    """Check the report and CSV of the last completed UTC day with the project's own validators. Returns what the payload needs."""
    dashboard = _dashboard()
    expected = (today - timedelta(days=1)).isoformat()
    folder = Path(reports_dir)
    if not folder.is_dir():
        raise AutomationError(f"the reports folder {folder.name!r} does not exist, so no report was produced")
    report_path = folder / f"report_{expected}.md"
    csv_path = folder / f"report_{expected}_data.csv"
    for path in (report_path, csv_path):
        if not path.is_file():
            raise AutomationError(f"{path.name} was not produced (expected for the last completed UTC day, {expected})")
    try:
        csv_text = dashboard.read_text_limited(csv_path, dashboard.MAX_CSV_BYTES, "CSV")
        report_text = dashboard.read_text_limited(report_path, dashboard.MAX_REPORT_BYTES, "report")
        dataset = dashboard.parse_dataset(csv_text)
        dashboard.check_against_report(dataset, report_text)
        metrics = dashboard.window_metrics(dataset, WINDOW_DAYS)
    except dashboard.DashboardInputError as error:
        raise AutomationError(f"validation failed: {error}")
    last_date = dataset["dates"][-1]
    if last_date != expected:
        raise AutomationError(f"the CSV ends on {last_date} but the file names and the last completed UTC day say {expected}")
    return {"date": last_date, "dataset": dataset, "metrics": metrics, "report_path": report_path, "csv_path": csv_path, "report_text": report_text}


# ---------------------------------------------------------------- the payloads
def check_run_id(run_id):
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise AutomationError("the run id must be 1 to 20 digits")
    return run_id


def check_run_url(run_url, run_id):
    found = RUN_URL_PATTERN.fullmatch(run_url) if isinstance(run_url, str) else None
    if found is None or found.group(1) != run_id:
        raise AutomationError("the run URL is not the workflow run page of this repository for this run id")
    return run_url


def check_artifact_url(artifact_url, run_id):
    """The address of the artifact that THIS run uploaded: the GitHub page https://github.com/<this repository>/actions/runs/<run id>/artifacts/<artifact id>, exactly as the upload action reports it.
    It is only ever read from the upload step's output, never built or guessed here. A missing, empty, malformed or other run's address is refused (and not repeated in the message)."""
    found = ARTIFACT_URL_PATTERN.fullmatch(artifact_url) if isinstance(artifact_url, str) else None
    if found is None or found.group(1) != run_id:
        raise AutomationError("the artifact link is missing or is not the artifact page of this run in this repository (it must come from the deliverables upload step of the same run), so no success report is sent")
    return artifact_url


def check_report_request(payload):
    """A last, light check of the stored report request (the file may have been altered between the steps): the exact fields, this run's own addresses, three chart files that really are PNG images
    within the size limit. The web app validates everything again; this uses the standard library only because the posting commands run without the project's other modules."""
    if set(payload) != REPORT_REQUEST_KEYS:
        raise AutomationError("the success report request does not have exactly the expected fields")
    check_run_url(payload["workflow_url"], check_run_id(payload["run_id"]))
    report = payload["report"]
    if not isinstance(report, dict) or set(report) != REPORT_PARTS or not isinstance(report["charts"], list) or len(report["charts"]) != len(REPORT_CHART_IDS):
        raise AutomationError("the success report is incomplete: it needs its figures and exactly three charts")
    for chart, expected in zip(report["charts"], REPORT_CHART_IDS):
        encoded = chart.get("png_base64") if isinstance(chart, dict) else None
        try:
            data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (binascii.Error, ValueError):
            data = b""
        if not isinstance(chart, dict) or chart.get("id") != expected or data[:8] != PNG_SIGNATURE or data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82" or len(data) > MAX_CHART_BYTES:
            raise AutomationError(f"the {expected} chart of the success report is not a valid PNG image within the size limit")


def _finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise AutomationError(f"{label} is not a finite number")
    return number + 0.0                            # turns -0.0 into 0.0


def build_rows(outputs, run_id):
    """One sheet row per asset. Daily return is a fraction, volatility is the daily sample standard deviation (not annualised), drawdown is a negative fraction."""
    run_id = check_run_id(run_id)
    dataset, metrics = outputs["dataset"], outputs["metrics"]
    correlation = metrics["correlation"]
    if correlation is None or not math.isfinite(correlation):
        raise AutomationError("the correlation is undefined (a series is constant), so the sheet rows cannot be written")
    correlation = max(-1.0, min(1.0, float(correlation)))
    rows = []
    for asset in _dashboard().ASSETS:
        rows.append({
            "Run date": outputs["date"],
            "Asset": asset,
            "Closing price": _finite(dataset["closes"][asset][-1], f"{asset} closing price"),
            "Daily return": _finite(dataset["returns"][asset][-1], f"{asset} daily return"),
            "30-day volatility": _finite(metrics["volatility"][asset]["daily"], f"{asset} volatility"),
            "30-day drawdown": min(0.0, _finite(metrics["max_drawdown"][asset][0], f"{asset} drawdown")),
            "Correlation": correlation,
            "Validation status": "PASSED",
            "Report status": "GENERATED",
            "Workflow run ID": run_id,
        })
    return rows


def _short(text):
    return text if len(text) <= MAX_OBSERVATION_CHARS else text[:MAX_OBSERVATION_CHARS - 3] + "..."


def build_payloads(outputs, run_id, run_url):
    """The request bodies without the token. The success e-mail request gets its sheet_update when it is sent, from the recorded result of the upsert."""
    run_id = check_run_id(run_id)
    run_url = check_run_url(run_url, run_id)
    rows = build_rows(outputs, run_id)
    metrics, dataset = outputs["metrics"], outputs["dataset"]
    summary = {
        "btc": _asset_summary(rows[0]),
        "eth": _asset_summary(rows[1]),
        "correlation": rows[0]["Correlation"],
        "rules": {name: str(label) for name, label in metrics["rules"].items()},
        "observations": [_short(text) for text in _dashboard().window_insights(dataset, WINDOW_DAYS)[:MAX_OBSERVATIONS]],
    }
    date_text = outputs["date"]
    return {
        "upsert_analysis": {"action": "upsert_analysis", "analysis_date": date_text, "rows": rows},
        "verify_analysis": {"action": "verify_analysis", "analysis_date": date_text, "rows": rows},
        "send_success_email": {"action": "send_success_email", "analysis_date": date_text, "rows": rows, "run_id": run_id, "workflow_url": run_url, "validation_status": "PASSED", "summary": summary},
    }


def _asset_summary(row):
    return {"close": row["Closing price"], "daily_return": row["Daily return"], "volatility": row["30-day volatility"], "drawdown": row["30-day drawdown"]}


def utc_now():
    return datetime.now(timezone.utc)              # a separate function so that tests can replace it


def _email_report():
    import email_report                            # imported here: only build-payload needs it; the commands that hold the secrets never load the analysis or report code
    return email_report


def build_report_payload(outputs, email_payload, generated_at):
    """The request body of the HTML success report: the existing success-email body (same rows, summary and run data) plus the report with its three charts, built from the same validated outputs.
    The token, the artifact link and the sheet counts are added when it is posted. Raises AutomationError if the report cannot be built or would not fit the web app's request limit."""
    module = _email_report()
    try:
        report = module.build_report(outputs, generated_at)
    except module.EmailReportError as error:
        raise AutomationError(f"the success report could not be built: {safe_text(error)}")
    body = dict(email_payload, action=REPORT_ACTION, report=report)
    size = len(json.dumps(body, allow_nan=False))
    if size + REQUEST_EXTRAS_CHARS > MAX_REQUEST_CHARS:
        raise AutomationError(f"the success report request would be {size + REQUEST_EXTRAS_CHARS} characters, more than the limit of {MAX_REQUEST_CHARS}")
    return body


def write_json(path, value):
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------- talking to the web app
class _SameSchemeRedirects(urllib.request.HTTPRedirectHandler):
    """The web app answers a POST with a redirect that must be followed with a body-less GET (urllib does that). Never follow it to another scheme."""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != urllib.parse.urlparse(req.full_url).scheme:
            raise urllib.error.HTTPError(newurl, code, "redirect to a different scheme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _post_once(url, body):
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "User-Agent": "daily-market-analysis/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameSchemeRedirects)   # no proxy: the token goes to Google or nowhere
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as reply:
            data = reply.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in RETRYABLE_HTTP_STATUS:
            raise _Retry(f"HTTP {error.code}")
        raise AutomationError(f"the web app answered HTTP {error.code}", delivery="rejected")
    except (urllib.error.URLError, socket.timeout, ConnectionError, http.client.HTTPException, OSError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        raise _Retry(f"network error ({type(reason).__name__})")
    if len(data) > MAX_REPLY_BYTES:
        raise AutomationError("the web app reply is too large", delivery="unknown")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _Retry("the reply was not JSON (an error page?)")
    if not isinstance(parsed, dict):
        raise AutomationError("the web app reply is not a JSON object", delivery="unknown")
    return parsed


def post_action(url, payload, token, attempts=MAX_ATTEMPTS):
    """POST the payload with the token added in memory. Returns (reply, attempts used). Transport problems are retried at most `attempts` times in total;
    a reply that carries a final error code is returned at once; one that carries a repeatable code is repeated and, after the last attempt, returned."""
    validate_webhook_url(url)
    if not token:
        raise AutomationError("no token is configured")
    try:
        body = json.dumps(dict(payload, token=token), allow_nan=False).encode("utf-8")
    except ValueError:
        raise AutomationError("the payload cannot be written as strict JSON")
    if len(body) > MAX_REQUEST_CHARS:
        raise AutomationError(f"the request is larger than the web app accepts ({len(body)} characters, the limit used here is {MAX_REQUEST_CHARS}), so it is not sent")
    last = "no attempt was made"
    for attempt in range(1, attempts + 1):
        try:
            reply = _post_once(url, body)
        except _Retry as error:
            last = str(error)
        except AutomationError as error:
            error.attempts = attempt
            raise
        else:
            if reply.get("error") not in RETRYABLE_ERROR_CODES or attempt == attempts:
                return reply, attempt
            last = f"the web app answered {reply.get('error')}"
        if attempt < attempts:
            pause(RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1))
    raise AutomationError(f"the web app could not be used after {attempts} attempts ({last})", attempts=attempts, delivery="unknown")


# ---------------------------------------------------------------- checking the replies (HTTP 200 alone proves nothing)
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= INT_LIMIT


def clean_reply(reply, attempts):
    """Only known fields with the expected types survive; text is limited to the web app's own status words and error codes."""
    kept = {}
    for name in ("success", "matches", "write_confirmed"):
        if isinstance(reply.get(name), bool):
            kept[name] = reply[name]
    for name in ("rows_inserted", "rows_updated", "rows_found", "duplicate_keys"):
        if _is_int(reply.get(name)):
            kept[name] = reply[name]
    if reply.get("sheet_status") in SHEET_STATUSES:
        kept["sheet_status"] = reply["sheet_status"]
    if reply.get("email_status") in EMAIL_STATUSES:
        kept["email_status"] = reply["email_status"]
    if isinstance(reply.get("email_format"), str) and reply["email_format"] in EMAIL_FORMATS:
        kept["email_format"] = reply["email_format"]
    if isinstance(reply.get("error"), str) and re.fullmatch(r"[A-Z_]{1,40}", reply["error"]):
        kept["error"] = reply["error"]
    if isinstance(reply.get("action"), str) and re.fullmatch(r"[a-z_]{1,30}", reply["action"]):
        kept["action"] = reply["action"]
    if isinstance(reply.get("request_id"), str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", reply["request_id"]):
        kept["request_id"] = reply["request_id"]
    kept["attempts"] = attempts
    return kept


def problems_in(action, result, expected_rows):
    """Why a reply does not count as success. An empty list means the step succeeded."""
    problems = []
    if result.get("success") is not True:
        problems.append("the web app did not report success" + (f" ({result['error']})" if "error" in result else ""))
    if action == "upsert_analysis":
        if result.get("sheet_status") != "ok":
            problems.append(f"sheet_status is {result.get('sheet_status', 'missing')}, not ok")
        if result.get("write_confirmed") is not True:
            problems.append("the write was not confirmed by reading it back")
        if "rows_inserted" not in result or "rows_updated" not in result:
            problems.append("the row counts are missing")
        elif result["rows_inserted"] + result["rows_updated"] != expected_rows:
            problems.append(f"{result['rows_inserted']} inserted + {result['rows_updated']} updated is not the {expected_rows} expected rows")
    elif action == "verify_analysis":
        if result.get("matches") is not True:
            problems.append("the sheet rows do not match the expected values")
        if result.get("rows_found") != expected_rows:
            problems.append(f"rows_found is {result.get('rows_found', 'missing')}, expected {expected_rows}")
        if result.get("duplicate_keys") != 0:
            problems.append("duplicate keys are present or the count is missing")
    elif action in SUCCESS_ACTIONS:
        if result.get("sheet_status") != "ok":
            problems.append(f"sheet_status is {result.get('sheet_status', 'missing')}, not ok")
        if action == REPORT_ACTION and result.get("email_status") == "send_outcome_unknown":
            problems.append("the web app could not tell whether the HTML success email was delivered (send_outcome_unknown); no plain-text email was sent instead. "
                            "Check the mailbox before running this step again, because a repeat may send a duplicate")
        elif result.get("email_status") not in EMAIL_ACCEPTED:
            problems.append(f"email_status is {result.get('email_status', 'missing')}")
        if action == REPORT_ACTION and result.get("email_status") in ("sent", "sent_dedup_failed") and result.get("email_format") not in EMAIL_FORMATS:
            problems.append(f"email_format is {result.get('email_format', 'missing')}, not html or text_fallback")
    elif action == "notify_failure":
        if result.get("email_status") not in EMAIL_ACCEPTED:
            problems.append(f"email_status is {result.get('email_status', 'missing')}")
    return problems


def _sheet_status_after_transport_failure(error):
    return {"not_sent": "not_attempted", "rejected": "failed"}.get(error.delivery, "unconfirmed")


def _result_path(folder, action):
    return Path(folder) / f"{action}_result.json"


def load_result(folder, action):
    path = _result_path(folder, action)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def append_summary(env, lines):
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        print(f"WARNING: could not write the job summary ({type(error).__name__})", file=sys.stderr)


def describe(action, result):
    parts = [f"{name}={result[name]}" for name in ("sheet_status", "email_status", "email_format", "rows_inserted", "rows_updated", "rows_found", "duplicate_keys", "attempts") if name in result]
    return f"{action}: " + " ".join(parts)


def suppressed_notice_lines(stage, server_stage, sheet_status, summary):
    """The job-summary entry for a failure notice the web app answered with skipped_duplicate: it already holds a notice for this run and stage, so NO email went out for this failure."""
    return [
        "### Daily Market Analysis: failure notice SUPPRESSED (no failure email went out)",
        f"- Failed workflow step: {safe_text(stage, 60)}",
        f"- Server stage used for deduplication: {safe_text(server_stage, 60)}",
        f"- Spreadsheet: {sheet_status}",
        "- Failure email: NOT delivered (the web app answered skipped_duplicate)",
        "- Why: the web app keeps one failure notice per workflow run and stage for up to 6 hours, and one already exists for this run and stage. It may describe a different step: check that earlier email, or start a NEW run instead of re-running.",
        f"- Original failure: {safe_text(summary, 600)}",
    ]


def summary_lines(action, result, problems):
    sheet, email = result.get("sheet_status", "not_attempted"), result.get("email_status", "not_attempted")
    lines = [f"### Daily Market Analysis: {action}"]
    if action == "upsert_analysis":
        lines.append(f"- Spreadsheet update: {sheet} (inserted {result.get('rows_inserted', '?')}, updated {result.get('rows_updated', '?')}, write confirmed: {result.get('write_confirmed', False)})")
    elif action == "verify_analysis":
        lines.append(f"- Spreadsheet read-back verification: {'ok' if not problems else 'FAILED'} ({result.get('rows_found', '?')} rows found, matches: {result.get('matches', False)})")
    elif action in SUCCESS_ACTIONS:
        lines += [f"- Spreadsheet: {sheet}", f"- Success email: {email}" + (f" (format: {result['email_format']})" if "email_format" in result else "")]
        if result.get("email_status") == "sent_dedup_failed":
            lines.append("- WARNING: the success email WAS SENT, but the web app could not record it for duplicate protection (sent_dedup_failed): do not run this step again for this workflow run within 6 hours, a repeat would send it again")
        if result.get("email_format") == "text_fallback":
            lines.append("- WARNING: the HTML success report could not be sent; the plain-text success email went out instead (email_format text_fallback)")
    else:
        lines += [f"- Spreadsheet: {sheet}", f"- Failure email: {email}"]
        if email == "sent_dedup_failed":
            lines.append("- WARNING: the failure email WAS SENT, but the web app could not record it for duplicate protection (sent_dedup_failed): do not run the notification step again for this workflow run within 6 hours, a repeat would send it again")
    lines += [f"- Problem: {text}" for text in problems]
    return lines


# ---------------------------------------------------------------- commands
def command_validate(args, env, today):
    outputs = validate_outputs(args.reports_dir, today)
    print(f"validate: OK analysis date {outputs['date']}, validation PASSED ({outputs['report_path'].name}, {outputs['csv_path'].name})")
    output_file = env.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"analysis_date={outputs['date']}\n")
    return 0


def command_build_payload(args, env, today):
    outputs = validate_outputs(args.reports_dir, today)
    payloads = build_payloads(outputs, args.run_id, args.run_url)
    try:                                                                    # the HTML report is built here (this step holds no secret); if it cannot be, the sheet update must not be blocked
        payloads[REPORT_ACTION] = build_report_payload(outputs, payloads["send_success_email"], utc_now())
    except Exception as error:                                              # deliberately broad: whatever goes wrong in the report code, the other three payloads are still written
        reason = redact(safe_text(error if isinstance(error, AutomationError) else f"{type(error).__name__}: {error}", 600), secrets_to_hide(env))
        print(f"WARNING: the HTML success report could not be built and will not be sent (the plain payloads are still written, so the sheet update is not affected): {reason}", file=sys.stderr)
    folder = Path(args.out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for action, body in payloads.items():
        write_json(folder / f"{action}.json", body)
    print(f"build-payload: wrote {len(payloads)} payload files for {outputs['date']} ({len(payloads['upsert_analysis']['rows'])} rows)" + ("" if REPORT_ACTION in payloads else "; NO success report payload"))
    return 0


def _record(folder, action, result):
    write_json(_result_path(folder, action), result)


def command_post(args, env, today):
    folder, action = Path(args.dir), args.action
    secrets = secrets_to_hide(env)
    if (args.artifact_url is not None) != (action == REPORT_ACTION):
        raise AutomationError(f"--artifact-url is required by {REPORT_ACTION} and used by no other action" if action == REPORT_ACTION else f"--artifact-url is only used by {REPORT_ACTION}, not by {action}")
    path = folder / f"{action}.json"
    if not path.is_file():
        raise AutomationError(f"{path.name} was not found in {folder.name}; the payload step must run first")
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("action") != action or not isinstance(payload.get("rows"), list):
        raise AutomationError(f"{path.name} is not a valid {action} payload")
    if action in SUCCESS_ACTIONS:
        upsert, verify = load_result(folder, "upsert_analysis"), load_result(folder, "verify_analysis")
        if not (upsert and upsert.get("success") is True and upsert.get("sheet_status") == "ok" and upsert.get("write_confirmed") is True and _is_int(upsert.get("rows_inserted")) and _is_int(upsert.get("rows_updated"))):
            raise AutomationError("refusing to send the success email: the spreadsheet update was not confirmed")
        if not (verify and verify.get("success") is True and verify.get("matches") is True):
            raise AutomationError("refusing to send the success email: the spreadsheet verification did not pass")
        payload["sheet_update"] = {"rows_inserted": upsert["rows_inserted"], "rows_updated": upsert["rows_updated"]}
        if action == REPORT_ACTION:
            payload["artifact_url"] = check_artifact_url(args.artifact_url, payload.get("run_id"))
            check_report_request(payload)
    try:
        url, token = read_secrets(env)
        reply, attempts = post_action(url, payload, token)
    except AutomationError as error:
        result = {"success": False, "error": "NOT_SENT" if error.delivery == "not_sent" else "TRANSPORT", "sheet_status": _sheet_status_after_transport_failure(error), "email_status": "not_attempted", "attempts": error.attempts}
        _record(folder, action, result)
        append_summary(env, summary_lines(action, result, [redact(str(error), secrets)]))
        raise
    result = clean_reply(reply, attempts)
    problems = problems_in(action, result, len(payload["rows"]))
    _record(folder, action, result)
    append_summary(env, summary_lines(action, result, problems))
    if problems:
        print(f"{describe(action, result)} FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    if result.get("email_status") == "sent_dedup_failed":
        print("WARNING: the success email WAS SENT, but the web app could not record it for duplicate protection (sent_dedup_failed). Nothing is sent again and this step is complete, "
              "but do NOT run it again for this workflow run within 6 hours: without the record a repeat would send the email again.", file=sys.stderr)
    if result.get("email_format") == "text_fallback":
        print("WARNING: the HTML success report was NOT sent: the web app sent the plain-text success email instead (email_format text_fallback). Check the web app's mail service and the HTML message; "
              "the run itself is complete.", file=sys.stderr)
    print(describe(action, result) + " OK")
    return 0


def derive_stage(outcomes):
    for name in outcomes:
        if name not in STAGE_ORDER:
            raise AutomationError(f"unknown pipeline stage {name!r}")
    for name in STAGE_ORDER:
        if outcomes.get(name) in ("failure", "cancelled"):
            return name
    return "unknown"


def parse_outcomes(items):
    outcomes = {}
    for item in items or []:
        name, separator, value = item.partition("=")
        if not separator or value not in OUTCOMES:
            raise AutomationError(f"--outcome must look like step=success|failure|cancelled|skipped, got {item!r}")
        outcomes[name] = value
    return outcomes


def command_notify_failure(args, env, today):
    folder = Path(args.dir)
    secrets = secrets_to_hide(env)
    run_id = check_run_id(args.run_id)
    run_url = check_run_url(args.run_url, run_id)
    outcomes = parse_outcomes(args.outcome)
    stage = derive_stage(outcomes)
    upsert = load_result(folder, "upsert_analysis")
    sheet_status = "not_attempted"
    if upsert is not None:
        sheet_status = upsert["sheet_status"] if upsert.get("sheet_status") in SHEET_STATUSES else "unconfirmed"
    summary = f"step {stage} ended with outcome {outcomes.get(stage, 'unknown')}"
    stage_result = load_result(folder, RESULT_OF_STAGE[stage]) if stage in RESULT_OF_STAGE else None
    if stage_result and stage_result.get("error"):
        summary += f"; web app error {stage_result['error']}"
    summary += "; outcomes: " + ", ".join(f"{name}={outcomes[name]}" for name in STAGE_ORDER if name in outcomes)
    payload = {"action": "notify_failure", "stage": SERVER_STAGE.get(stage, stage), "error_summary": summary, "sheet_status": sheet_status, "run_id": run_id, "workflow_url": run_url}
    try:
        url, token = read_secrets(env)
        reply, attempts = post_action(url, payload, token)
    except AutomationError as error:
        append_summary(env, [f"### Daily Market Analysis: failure notice", f"- Failure email: NOT SENT ({redact(str(error), secrets)})"])
        raise AutomationError("the failure notification could not be sent: " + str(error))
    result = clean_reply(reply, attempts)
    problems = problems_in("notify_failure", result, 0)
    suppressed = not problems and result.get("email_status") == "skipped_duplicate"       # the web app holds a notice for this run and stage already: it answered, but NO email went out for THIS failure
    if suppressed:
        result.update(suppressed=True, failed_step=stage, server_stage=payload["stage"])
    _record(folder, "notify_failure", result)
    append_summary(env, suppressed_notice_lines(stage, payload["stage"], sheet_status, summary) if suppressed else summary_lines("notify_failure", result, problems))
    if problems:
        print(f"{describe('notify_failure', result)} FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    if suppressed:
        print(f"WARNING: the failure notice for step {safe_text(stage, 60)} (server stage {safe_text(payload['stage'], 60)}) was NOT delivered: the web app already holds a notice for this run and stage "
              f"(it keeps one for up to 6 hours), so it suppressed this one as a duplicate. This failure: {safe_text(summary, 600)}", file=sys.stderr)
        print(describe("notify_failure", result) + " SUPPRESSED (no failure email went out)")
        return 0
    if result.get("email_status") == "sent_dedup_failed":
        print("WARNING: the failure notice WAS SENT, but the web app could not record it for duplicate protection (sent_dedup_failed). Nothing is sent again and this step is complete, "
              "but do NOT run the notification step again for this workflow run within 6 hours: without the record a repeat would send the notice again.", file=sys.stderr)
    print(describe("notify_failure", result) + " OK")
    return 0


def clean_self_check(reply, attempts):
    """The self-check reply keeps only booleans and the six property flags; anything else the web app sends is dropped."""
    kept = {}
    for name in ("success", *SELF_CHECK_FLAGS):
        if isinstance(reply.get(name), bool):
            kept[name] = reply[name]
    properties = reply.get("properties")
    if isinstance(properties, dict) and set(properties) == set(SELF_CHECK_PROPERTIES) and all(isinstance(value, bool) for value in properties.values()):
        kept["properties"] = dict(properties)
    if isinstance(reply.get("error"), str) and re.fullmatch(r"[A-Z_]{1,40}", reply["error"]):
        kept["error"] = reply["error"]
    kept["attempts"] = attempts
    return kept


def self_check_results(result):
    """The six checks as (label, state, detail); state is PASS, FAIL or NOT CHECKED. A missing field is a FAIL: it means the updated Code.gs is not deployed."""
    not_deployed = "the updated Code.gs is not deployed to this web app address"
    properties = result.get("properties")
    if result.get("error") == "UNAUTHORIZED":
        authentication = ("FAIL", "the token does not match SHARED_SECRET in the web app")
    elif properties is None and result.get("error") == "NOT_CONFIGURED":
        authentication = ("FAIL", "the web app has no usable SHARED_SECRET (missing, or shorter than 16 characters)")
    elif properties is None:
        authentication = ("FAIL", "unrecognised reply: " + not_deployed)
    else:
        authentication = ("PASS", "the token was accepted by the web app")
    if authentication[0] != "PASS":
        return [("authentication",) + authentication] + [(label, "NOT CHECKED", "authentication did not succeed") for label in SELF_CHECK_LABELS[1:]]

    def flag(name):
        if name not in result:
            return None, f"the reply has no '{name}' value; {not_deployed}"
        return result[name], ""

    results = [("authentication",) + authentication]
    missing = [name for name in SELF_CHECK_PROPERTIES if not properties[name]]
    results.append(("script properties", "FAIL", "missing or empty: " + ", ".join(missing)) if missing else ("script properties", "PASS", f"all {len(SELF_CHECK_PROPERTIES)} are set"))

    spreadsheet, note = flag("spreadsheet_found")
    if not properties["SPREADSHEET_ID"]:
        results.append(("spreadsheet ID", "FAIL", "SPREADSHEET_ID is not set"))
    elif spreadsheet is None:
        results.append(("spreadsheet ID", "FAIL", note))
    elif spreadsheet is False:
        results.append(("spreadsheet ID", "FAIL", "the spreadsheet could not be opened with the configured SPREADSHEET_ID"))
    else:
        results.append(("spreadsheet ID", "PASS", "the spreadsheet opens"))

    sheet, sheet_note = flag("sheet_found")
    named, named_note = flag("sheet_name_matches")
    if spreadsheet is not True:
        results.append(("worksheet Sheet1", "NOT CHECKED", "the spreadsheet could not be opened"))
    elif not properties["SHEET_NAME"]:
        results.append(("worksheet Sheet1", "FAIL", "SHEET_NAME is not set"))
    elif sheet is None or named is None:
        results.append(("worksheet Sheet1", "FAIL", sheet_note or named_note))
    elif sheet is False:
        results.append(("worksheet Sheet1", "FAIL", "no worksheet with the configured SHEET_NAME exists"))
    elif named is False:
        results.append(("worksheet Sheet1", "FAIL", f"SHEET_NAME is not {EXPECTED_SHEET_NAME}"))
    else:
        results.append(("worksheet Sheet1", "PASS", f"{EXPECTED_SHEET_NAME} exists and is the configured worksheet"))

    headers, headers_note = flag("headers_ok")
    if spreadsheet is not True or sheet is not True:
        results.append(("ten headings", "NOT CHECKED", "there is no worksheet to read"))
    elif headers is None:
        results.append(("ten headings", "FAIL", headers_note))
    elif headers is False:
        results.append(("ten headings", "FAIL", "row 1 (or DATE_HEADER / ASSET_HEADER) does not match the ten expected headings exactly: " + ", ".join(SHEET_HEADERS)))
    else:
        results.append(("ten headings", "PASS", "row 1 holds exactly the ten expected headings"))

    email, email_note = flag("email_valid")
    if not properties["EMAIL_TO"]:
        results.append(("email configuration", "FAIL", "EMAIL_TO is not set"))
    elif email is None:
        results.append(("email configuration", "FAIL", email_note))
    elif email is False:
        results.append(("email configuration", "FAIL", "EMAIL_TO is not a single valid address"))
    else:
        results.append(("email configuration", "PASS", "EMAIL_TO is set and is a single valid address"))
    return results


# ---------------------------------------------------------------- staging the deliverables (report, CSV and dashboard of ONE analysis date)
def safe_text(value, limit=MAX_FILE_TEXT_CHARS):
    """Text taken from a file (or from a message built from one), made safe to print as part of ONE line of a CI log. GitHub Actions acts on a line that starts with '::' (or '##['), so
    every line break, control, invisible or separator character becomes a space, the command syntax itself is broken up, and the length is limited. Words and numbers are kept."""
    text = "".join(" " if character.isspace() or not character.isprintable() else character for character in str(value))
    text = re.sub(r" {2,}", " ", text).strip()
    text = re.sub(r":(?=:)", ": ", text).replace("##[", "# #[")                  # '::' -> ': :' (also for longer runs of colons), '##[' -> '# #['
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def check_analysis_date(value):
    """The analysis date as a date: exactly YYYY-MM-DD and a real calendar day. No padding and no path-like text get through."""
    if not isinstance(value, str) or ANALYSIS_DATE_PATTERN.fullmatch(value) is None:
        raise AutomationError("the analysis date must be written as YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AutomationError(f"the analysis date {safe_text(value, 40)} is not a real calendar day")


def _shown(value):
    return repr(safe_text(value, 60))               # a value taken from a file is made safe and shortened before it is shown


def _read_source(folder, name, limit):
    """The bytes of one deliverable, read once: a regular file (not a folder or a link), not empty, within the size limit."""
    path = Path(folder) / name
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise AutomationError(f"{name} was not found in {Path(folder).name!r}")
    except OSError as error:
        raise AutomationError(f"{name} cannot be read ({type(error).__name__})")
    if not stat.S_ISREG(info.st_mode):
        raise AutomationError(f"{name} is not a regular file")
    if info.st_size == 0:
        raise AutomationError(f"{name} is empty")
    if info.st_size > limit:
        raise AutomationError(f"{name} is larger than the limit of {limit} bytes")
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise AutomationError(f"{name} cannot be read ({type(error).__name__})")
    if len(data) > limit:
        raise AutomationError(f"{name} grew beyond the limit of {limit} bytes while it was read")
    return data


def _text_of(data, name):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AutomationError(f"{name} is not valid UTF-8 text")


def _dashboard_data(page, name):
    """The data object embedded in the dashboard page: the element the approved page itself reads (id dashboard-data)."""
    dashboard = _dashboard()
    found = [text for attributes, text in dashboard.SCRIPT_ELEMENT.findall(page) if attributes.strip() == dashboard.EXPECTED_SCRIPT_ATTRIBUTES[0]]
    if len(found) != 1:
        raise AutomationError(f"the dashboard {name} has no single embedded data element, so it is not a dashboard page of this project")
    try:
        data = json.loads(found[0])
    except ValueError:
        raise AutomationError(f"the dashboard {name} holds embedded data that is not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict) or not isinstance(data.get("rows"), list):
        raise AutomationError(f"the dashboard {name} holds embedded data in an unexpected shape")
    return data


def validate_deliverables(reports_dir, dashboard_dir, day):
    """Check the report, the CSV and the dashboard of one analysis date against the project's own data contract and against each other.
    Everything is read once and checked BEFORE anything is written; the returned bytes are exactly the validated bytes. Returns {file name: bytes}."""
    dashboard = _dashboard()
    iso = day.isoformat()
    report_name, csv_name, page_name = (pattern.format(day=iso) for pattern in DELIVERABLE_NAMES)
    csv_bytes = _read_source(reports_dir, csv_name, dashboard.MAX_CSV_BYTES)
    report_bytes = _read_source(reports_dir, report_name, dashboard.MAX_REPORT_BYTES)
    page_bytes = _read_source(dashboard_dir, page_name, MAX_DASHBOARD_BYTES)

    try:                                                                      # the CSV: the 17-column contract, real consecutive dates, valid numbers; and it must end on the analysis date
        dataset = dashboard.parse_dataset(_text_of(csv_bytes, csv_name))
    except dashboard.DashboardInputError as error:
        raise AutomationError(f"the CSV {csv_name} is not valid: {safe_text(error)}")
    if dataset["dates"][-1] != iso:
        raise AutomationError(f"the CSV {csv_name} ends on {dataset['dates'][-1]}, not on the analysis date {iso}")

    try:                                                                      # the report: the same period and the same 30-day figures as the CSV, and a 'Data fetched' line
        facts = dashboard.check_against_report(dataset, _text_of(report_bytes, report_name))
    except dashboard.DashboardInputError as error:
        raise AutomationError(f"the report {report_name} does not match its CSV: {safe_text(error)}")
    try:                                                                      # the real UTC time, not the file name: the data of a completed day is fetched on the next UTC day
        fetched = datetime.strptime(facts["data_fetched"], FETCHED_TIME_FORMAT)
    except ValueError:
        raise AutomationError(f"the report {report_name} has a 'Data fetched' time that is not a real UTC date and time")
    if fetched.date() != day + timedelta(days=1):
        raise AutomationError(f"the report {report_name} says the data was fetched on {fetched.date().isoformat()}, which is not the day after the analysis date {iso}")

    data = _dashboard_data(_text_of(page_bytes, page_name), page_name)         # the dashboard: built from THIS CSV and THIS report
    meta, problems = data["meta"], []
    for key, expected in (("csv_file", csv_name), ("report_file", report_name), ("data_fetched", facts["data_fetched"])):
        if meta.get(key) != expected:
            problems.append(f"{key} is {_shown(meta.get(key))}, expected {_shown(expected)}")
    for key in ("close_period", "return_period"):
        period = meta.get(key)
        if not isinstance(period, dict) or period.get("end") != iso:
            problems.append(f"{key} does not end on {iso}")
    if data.get("columns") != list(dashboard.EXPECTED_COLUMNS) or data["rows"] != dataset["rows"]:
        problems.append("its embedded data differs from the CSV")
    if problems:
        raise AutomationError(f"the dashboard {page_name} does not match the CSV and the report of {iso}: " + "; ".join(problems))
    return {report_name: report_bytes, csv_name: csv_bytes, page_name: page_bytes}


def _check_output_folder(out):
    """The staging folder must be new (or an existing EMPTY folder). A folder that holds anything is never overwritten or mixed with."""
    if not out.parent.is_dir():
        raise AutomationError(f"the folder that should hold the output folder {out.name!r} does not exist")
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise AutomationError(f"the output folder {out.name!r} already exists and is not empty; it is never overwritten or mixed with a new set")


def stage_files(files, out_dir):
    """Publish exactly these {name: bytes} as the new folder out_dir in one step: written and read back inside a temporary folder next to it, then renamed. On any failure nothing is left behind."""
    out = Path(out_dir)
    _check_output_folder(out)
    temporary = Path(tempfile.mkdtemp(dir=out.parent, prefix=out.name + ".staging-"))
    published = False
    try:
        for name, data in files.items():
            with open(temporary / name, "xb") as handle:
                handle.write(data)
            if hashlib.sha256((temporary / name).read_bytes()).digest() != hashlib.sha256(data).digest():
                raise AutomationError(f"{name} was not written correctly")
        os.chmod(temporary, 0o755)
        _check_output_folder(out)
        if out.exists():
            out.rmdir()                                                       # only an EMPTY existing folder gets here
        os.rename(temporary, out)
        published = True
    except OSError as error:
        raise AutomationError(f"could not stage the deliverables ({type(error).__name__})")
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def command_stage_deliverables(args, env, today):
    """Stage report_<date>.md, report_<date>_data.csv and dashboard_<date>.html as one validated set. Exit 0 only if all three are consistent and were published together."""
    day = check_analysis_date(args.date)
    _check_output_folder(Path(args.out_dir))
    files = validate_deliverables(args.reports_dir, args.dashboard_dir, day)
    stage_files(files, args.out_dir)
    print(f"stage-deliverables: OK, {len(files)} files for {day.isoformat()} staged in {args.out_dir}")
    for name, data in files.items():
        print(f"  {Path(args.out_dir) / name} ({len(data)} bytes)")
    return 0


def command_self_check(args, env, today):
    """Ask the web app to check its own configuration. Read-only: one request, no rows written, no email sent. Exit 0 only if all six checks pass."""
    secrets = secrets_to_hide(env)
    url, token = read_secrets(env)
    validate_webhook_url(url)
    try:
        reply, attempts = post_action(url, {"action": "self_check", "expected_sheet_name": EXPECTED_SHEET_NAME}, token)
        cleaned = clean_self_check(reply, attempts)
        results = self_check_results(cleaned)
        success = cleaned.get("success") is True
    except AutomationError as error:
        results = [("authentication", "FAIL", redact(str(error), secrets))] + [(label, "NOT CHECKED", "authentication did not succeed") for label in SELF_CHECK_LABELS[1:]]
        success = False
    lines = [f"self-check: {state:<11} {label} - {detail}" for label, state, detail in results]
    for line in lines:
        print(line)
    failed = sum(1 for _, state, _ in results if state != "PASS")
    append_summary(env, ["### Daily Market Analysis: self-check"] + [f"- {state} {label}: {detail}" for label, state, detail in results])
    if failed:
        print(f"self-check: FAILED - {failed} of {len(results)} checks did not pass", file=sys.stderr)
        return 1
    if not success:
        print("self-check: FAILED - every check passed but the web app did not report success", file=sys.stderr)
        return 1
    print("self-check: OK - read-only check: no row was written and no email was sent")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="daily_automation.py", description="Validate, publish and report the daily BTC/ETH analysis.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check the report and CSV of the last completed UTC day")
    validate.add_argument("--reports-dir", required=True)
    build = commands.add_parser("build-payload", help="write the request bodies (without the token)")
    build.add_argument("--reports-dir", required=True)
    build.add_argument("--out-dir", required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--run-url", required=True)
    post = commands.add_parser("post", help="send one request to the web app and check the reply")
    post.add_argument("--action", required=True, choices=POST_ACTIONS)
    post.add_argument("--dir", required=True)
    post.add_argument("--artifact-url", help="the artifact page of this run (the upload step's artifact-url output); required by, and only used by, send_success_report")
    notify = commands.add_parser("notify-failure", help="send the failure e-mail request")
    notify.add_argument("--dir", required=True)
    notify.add_argument("--run-id", required=True)
    notify.add_argument("--run-url", required=True)
    notify.add_argument("--outcome", action="append", default=[], help="step=outcome, repeated for every earlier step")
    commands.add_parser("self-check", help="read-only check of the web app's configuration (no rows written, no email sent)")
    staging = commands.add_parser("stage-deliverables", help="check the report, CSV and dashboard of one analysis date and stage exactly those three files in a new folder")
    staging.add_argument("--reports-dir", required=True, help="folder with report_<date>.md and report_<date>_data.csv")
    staging.add_argument("--dashboard-dir", required=True, help="folder with dashboard_<date>.html")
    staging.add_argument("--out-dir", required=True, help="the new folder for the three files (must not exist or must be empty)")
    staging.add_argument("--date", required=True, help="the analysis date, YYYY-MM-DD")
    return parser


COMMANDS = {"validate": command_validate, "build-payload": command_build_payload, "post": command_post, "notify-failure": command_notify_failure, "self-check": command_self_check, "stage-deliverables": command_stage_deliverables}


def main(argv=None, env=None, now=None):
    """Returns the exit code: 0 success, 1 a step failed, 2 wrong usage. `env` and `now` (a UTC date or datetime) exist for tests."""
    env = os.environ if env is None else env
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    try:
        return COMMANDS[args.command](args, env, utc_today(now))
    except AutomationError as error:
        print("ERROR: " + redact(error, secrets_to_hide(env)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
