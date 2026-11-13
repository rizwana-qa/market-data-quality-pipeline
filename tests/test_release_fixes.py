"""Regression tests for the release-readiness corrections: (1) the Google Sheets 'Validation status' is derived from the run's evidence, (2) the 621-instance ledger is enumerated by scope,
(3) an outdated Apps Script is detected by a read-only preflight BEFORE the sheet is updated. Offline: no network, no secret, nothing is sent or deployed."""
import contextlib, copy, io, json, os, re, shutil
from datetime import datetime, timezone

from wf_support import *
import candle_rules as cr
import run_evidence as ev
import daily_automation as da

R = Runner("release fix checks")
check = R.register
NOW = datetime(2026, 10, 28, 7, 0, 0, tzinfo=timezone.utc)
REPORTS = committed_reports()
RUN_ID, RUN_URL = "123456789", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
URL = "https://script.google.com/macros/s/AKfycbzSECRETDEPLOYMENT0123/exec"
TOKEN = "unit-test-token-0123456789abcdef"


def outputs(): return da.validate_outputs(REPORTS, NOW)


# ================================================================ 1. the sheet status comes from the evidence
@check("sheet status: a complete PASS (producer envelope, consumer verification, recomputed ledger, 17 of 17 fields) is written as PASSED and GENERATED; the ten headings are unchanged")
def _():
    rows = da.build_rows(outputs(), RUN_ID); assert [(r["Validation status"], r["Report status"]) for r in rows] == [("PASSED", "GENERATED")] * 2
    assert da.SHEET_HEADERS == ["Run date", "Asset", "Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation", "Validation status", "Report status", "Workflow run ID"] and all(list(r) == da.SHEET_HEADERS for r in rows)
@check("sheet status: there is no hard-coded PASSED or GENERATED in the row builder or the payload builder any more")
def _():
    text = open(os.path.join(SRC, "daily_automation.py"), encoding="utf-8").read(); assert '"Validation status": "PASSED"' not in text and '"Report status": "GENERATED"' not in text and '"validation_status": "PASSED"' not in text
@check("sheet status: the success e-mail request carries the same value as the rows, derived from the evidence")
def _():
    p = da.build_payloads(outputs(), RUN_ID, RUN_URL); assert p["send_success_email"]["validation_status"] == p["upsert_analysis"]["rows"][0]["Validation status"] == "PASSED"
@check("sheet status: a FAIL is recorded as FAILED and FAILED (both are in the sheet's fixed vocabulary), never as PASSED")
def _():
    o = copy.deepcopy(outputs()); o["verification"]["checks"][1]["outcome"] = "FAIL"; o["verification"]["overall"] = "FAIL"
    assert [(r["Validation status"], r["Report status"]) for r in da.build_rows(o, RUN_ID)] == [("FAILED", "FAILED")] * 2
    o = copy.deepcopy(outputs()); o["dataset"]["candle_ledger"]["non_pass"] = [{"rule_id": "CND-VWAP", "severity": "ERROR", "outcome": "FAIL", "scope": "candle", "asset": "BTC", "date": "2026-10-22", "field": "vwap", "expected": "x", "actual": "y", "explanation": "z"}]
    for row in o["dataset"]["candle_ledger"]["rules"]:
        if row["rule_id"] == "CND-VWAP": row["pass"] -= 1; row["fail"] += 1
    assert da.build_rows(o, RUN_ID)[0]["Validation status"] == "FAILED"
@check("sheet status: missing, incomplete, blocked, not-run or inconclusive evidence is NEVER PASSED - no row is built at all (the step fails before any write)")
def _():
    def broken(edit):
        o = copy.deepcopy(outputs()); edit(o); return o
    cases = {"no manifest": lambda o: o.pop("manifest"), "no verification": lambda o: o.pop("verification"), "missing CSV check": lambda o: o["verification"].update(checks=[c for c in o["verification"]["checks"] if c["id"] != "CON-CSV"]),
             "missing instance": lambda o: o["dataset"]["candle_ledger"].update(missing=1, evaluated=o["dataset"]["candle_ledger"]["evaluated"] - 1), "unknown assumption": lambda o: o["dataset"]["candle_ledger"].update(assumptions_applied=["A9"]),
             "verification inconclusive": lambda o: o["verification"].update(overall="INCONCLUSIVE"), "verification blocked": lambda o: o["verification"].update(overall="BLOCKED"), "verification not run": lambda o: o["verification"].update(overall="NOT RUN"),
             "producer family missing": lambda o: o["manifest"]["envelope"]["families"].pop("source")}
    for name, edit in cases.items():
        o = broken(edit)
        try: rows = da.build_rows(o, RUN_ID)
        except da.AutomationError as e: assert "no sheet row is written" in str(e), (name, str(e))
        else: raise AssertionError(f"{name}: rows were built: {[r['Validation status'] for r in rows]}")
        try: da.build_payloads(o, RUN_ID, RUN_URL)
        except da.AutomationError: pass
        else: raise AssertionError(f"{name}: payloads were built")
@check("sheet status: build-payload writes no payload file when the evidence is not a PASS or FAIL (nothing reaches the sheet step)")
def _():
    out = new_temp_dir(); o = copy.deepcopy(outputs()); o["verification"]["overall"] = "INCONCLUSIVE"; real = da.validate_outputs
    da.validate_outputs = lambda *a, **k: o
    try:
        with contextlib.redirect_stderr(io.StringIO()): code = da.main(["build-payload", "--reports-dir", REPORTS, "--out-dir", out, "--run-id", RUN_ID, "--run-attempt", "1", "--run-url", RUN_URL, "--date", "2026-10-27"], env={}, now=NOW)
    finally: da.validate_outputs = real
    assert code == 1 and not os.path.exists(os.path.join(out, "upsert_analysis.json"))
@check("sheet status: the values the sheet receives are accepted by the web app's own vocabulary (Code.gs VALIDATION_STATUSES and REPORT_STATUSES)")
def _():
    gs = open(os.path.join(REPO, "apps_script", "Code.gs"), encoding="utf-8").read()
    assert "const VALIDATION_STATUSES = ['PASSED', 'FAILED'];" in gs and "const REPORT_STATUSES = ['GENERATED', 'FAILED'];" in gs

# ================================================================ 2. the ledger count, by scope
def planned():
    ledger = cr.evaluate_window({a: [(d, {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "vwap": 1.2, "volume": 3.0, "trade_count": 4}) for d in [f"2026-10-{i:02d}" for i in range(1, 32)]] for a in ("BTC", "ETH")})[0]
    return ledger.plan
@check("ledger count: 620 candle-scope instances (10 per-candle rules x 31 dates x 2 assets) plus exactly ONE window-scope instance (the cross-asset rule XPR-1) = 621; nothing else")
def _():
    plan = planned(); by_scope = {}
    for key in plan: by_scope[key[1]] = by_scope.get(key[1], 0) + 1
    assert by_scope == {"candle": 620, "window": 1} and len(plan) == len(set(plan)) == 621 and 10 * 31 * 2 == 620
    assert [k for k in plan if k[1] == "window"] == [("XPR-1", "window", "BTC+ETH", "", "window")]
@check("ledger count: each of the ten per-candle rules has 62 instances (31 dates x 2 assets), XPR-1 has 1, and no per-candle rule is counted twice")
def _():
    counts = {}
    for key in planned(): counts[key[0]] = counts.get(key[0], 0) + 1
    assert counts == {**{rule_id: 62 for rule_id, _f, _s in cr.CANDLE_RULES}, "XPR-1": 1} and len(cr.CANDLE_RULES) == 10, counts
@check("ledger count: the real run's manifest and the consumer's recomputation both report 621 with the same per-rule rows, and the documentation states the arithmetic")
def _():
    o = outputs(); producer, consumer = o["manifest"]["envelope"]["families"]["candle"], o["dataset"]["candle_ledger"]
    assert producer["expected_instances"] == consumer["expected_instances"] == 621 and {r["rule_id"]: r["expected_instances"] for r in consumer["rules"]} == {**{i: 62 for i, _f, _s in cr.CANDLE_RULES}, "XPR-1": 1}
    doc = open(os.path.join(REPO, "docs", "contracts.md"), encoding="utf-8").read(); assert "620 instances (10 rules x 2 assets x 31 days) plus the one cross-asset instance (621)" in doc and "XPR-1" in doc

# ================================================================ 3. the compatibility preflight runs before the sheet is touched
def payload_dir(with_report=True):
    out = new_temp_dir(); err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err): code = da.main(["build-payload", "--reports-dir", REPORTS, "--out-dir", out, "--run-id", RUN_ID, "--run-attempt", "1", "--run-url", RUN_URL, "--date", "2026-10-27"], env={}, now=NOW)
    assert code == 0, err.getvalue()
    if not with_report:
        os.remove(os.path.join(out, "send_success_report.json")); ctx = da.load_run_context(out); ctx["payloads"].pop("send_success_report.json"); da.write_json(os.path.join(out, da.RUN_CONTEXT_FILE), ctx)
    return out


NEW_SCRIPT = {"success": True, "action": "self_check", "properties": {n: True for n in da.SELF_CHECK_PROPERTIES}, "spreadsheet_found": True, "sheet_found": True, "headers_ok": True, "email_valid": True, "sheet_name_matches": True, "report_evidence_supported": True}
OLD_SCRIPT = {k: v for k, v in NEW_SCRIPT.items() if k != "report_evidence_supported"}


def post(out, action, replies, raises=None):
    """Run `post` with the web app imitated at the post_action level. Returns (exit code, stderr, list of (action, payload keys) that were requested)."""
    calls, err = [], io.StringIO()
    def fake(url, payload, token, attempts=da.MAX_ATTEMPTS):
        calls.append((payload["action"], sorted(payload)))
        if payload["action"] == "self_check" and raises is not None: raise raises
        reply = replies.get(payload["action"], {"success": True, "action": payload["action"], "sheet_status": "ok", "email_status": "not_attempted", "write_confirmed": True, "rows_inserted": 2, "rows_updated": 0})
        return dict(reply), 1
    with mock.patch.object(da, "post_action", fake), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = da.main(["post", "--action", action, "--dir", out], env={"GOOGLE_APPS_SCRIPT_URL": URL, "GOOGLE_APPS_SCRIPT_TOKEN": TOKEN}, now=NOW)
    return code, err.getvalue(), calls


@check("preflight: an OUTDATED Apps Script (a normal self_check answer without the capability) is detected before the sheet update - the upsert is never requested and the result says nothing was written")
def _():
    out = payload_dir(); code, err, calls = post(out, "upsert_analysis", {"self_check": OLD_SCRIPT})
    assert code == 1 and [c[0] for c in calls] == ["self_check"] and "does not support the evidence-backed report" in err and "sheet was not touched" in err and TOKEN not in err, (calls, err)
    r = json.load(open(os.path.join(out, "upsert_analysis_result.json"))); assert r["success"] is False and r["sheet_status"] == "not_attempted" and r["email_status"] == "not_attempted" and r["error"] == "NOT_SENT" and r["run_context"]["run_id"] == RUN_ID
@check("preflight: the preflight is the existing READ-ONLY self_check action (no rows, no e-mail), sent with only the action, the expected sheet name and the token; an UPDATED script lets the upsert proceed")
def _():
    out = payload_dir(); code, err, calls = post(out, "upsert_analysis", {"self_check": NEW_SCRIPT}); assert code == 0 and [c[0] for c in calls] == ["self_check", "upsert_analysis"], (calls, err)
    assert calls[0][1] == ["action", "expected_sheet_name"] and "upsert" not in calls[0][0]
@check("preflight: if the web app cannot be asked (transport failure), the sheet is not touched either: no upsert request, sheet_status not_attempted, exit 1")
def _():
    out = payload_dir(); code, err, calls = post(out, "upsert_analysis", {}, raises=da.AutomationError("the web app could not be used after 3 attempts (down)", attempts=3, delivery="unknown"))
    assert code == 1 and [c[0] for c in calls] == ["self_check"] and "so the sheet was not touched" in err
    r = json.load(open(os.path.join(out, "upsert_analysis_result.json"))); assert r["sheet_status"] == "not_attempted" and r["attempts"] == 3
@check("preflight: an error answer (a wrong token) is not mistaken for 'outdated' - the first request reports its own real error")
def _():
    out = payload_dir(); code, err, calls = post(out, "upsert_analysis", {"self_check": {"success": False, "error": "UNAUTHORIZED"}, "upsert_analysis": {"success": False, "error": "UNAUTHORIZED", "sheet_status": "not_attempted", "email_status": "not_attempted"}})
    assert [c[0] for c in calls] == ["self_check", "upsert_analysis"] and code == 1 and json.load(open(os.path.join(out, "upsert_analysis_result.json")))["error"] == "UNAUTHORIZED"
@check("preflight: it runs only before the sheet update and only when this run sends the evidence-backed report - not for verify, not for the e-mail, and not when no report payload was built")
def _():
    out = payload_dir(); assert [c[0] for c in post(out, "verify_analysis", {"self_check": OLD_SCRIPT})[2]] == ["verify_analysis"]
    out = payload_dir(with_report=False); assert [c[0] for c in post(out, "upsert_analysis", {"self_check": OLD_SCRIPT})[2]] == ["upsert_analysis"]
@check("preflight: an outdated script can no longer cause the partial update - with the outdated script the upsert, the read-back and the e-mail steps never run to a Sheets write (the first step stops the workflow)")
def _():
    out = payload_dir(); code, err, calls = post(out, "upsert_analysis", {"self_check": OLD_SCRIPT}); assert code == 1 and "upsert_analysis" not in [c[0] for c in calls]
@check("preflight: the updated Code.gs says it supports the report (report_evidence_supported) in its read-only self_check reply, and the client keeps only that boolean")
def _():
    gs = open(os.path.join(REPO, "apps_script", "Code.gs"), encoding="utf-8").read(); assert "report_evidence_supported: true" in gs
    assert da.clean_self_check(dict(NEW_SCRIPT, extra="x" * 500), 1)["report_evidence_supported"] is True and "extra" not in da.clean_self_check(dict(NEW_SCRIPT, extra="x"), 1) and "report_evidence_supported" not in da.clean_self_check(dict(NEW_SCRIPT, report_evidence_supported="yes"), 1)

R.finish()
