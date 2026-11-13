"""Tests for increment 3: the authoritative analysis date, explicit-date arguments, the freshness (age) rule and run_status.json.

The producer (run_workflow) reads its clock ONCE: that reading decides the completed candles, the 'Data fetched' time and the analysis date, and the manifest carries them. Nothing downstream
derives a second expected date from a clock; the clock is used only to measure how old the artifacts are. Everything here is offline: Kraken is mocked, no network, no secret."""
import contextlib, io, json, os, re, shutil
from datetime import datetime, timedelta, timezone

from wf_support import *
import dashboard
import run_evidence as ev
import daily_automation as da

R = Runner("freshness and status checks")
check = R.register
NOW = datetime(2026, 10, 28, 7, 0, 0, tzinfo=timezone.utc)          # four minutes after the committed report's fetch time (2026-10-28 06:56:33 UTC)
REPORTS = committed_reports()                                        # the committed 2026-10-27 outputs plus their manifest (run 123456789, attempt 1)
RUN_ID, RUN_URL = "123456789", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"
state = {}


def run(argv, now=NOW, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err): code = da.main(argv, env=dict(env or {}), now=now)
    return code, out.getvalue(), err.getvalue()


def private_reports():
    d = new_temp_dir()
    for n in os.listdir(REPORTS): shutil.copy(os.path.join(REPORTS, n), os.path.join(d, n))
    return d


def payloads(attempt="1", run_id=RUN_ID, date_text="2026-10-27", reports=None):
    out = new_temp_dir(); code, o, e = run(["build-payload", "--reports-dir", reports or REPORTS, "--out-dir", out, "--run-id", run_id, "--run-attempt", attempt, "--run-url", RUN_URL.replace(RUN_ID, run_id), "--date", date_text])
    return code, e, out


def rewrite_manifest(folder, edit):
    path = os.path.join(folder, "report_2026-10-27_manifest.json"); m = json.load(open(path, encoding="utf-8")); edit(m)
    open(path, "w", encoding="utf-8", newline="\n").write(ev.serialize(m))


# ================================================================ the age rule: the clock only measures how old the artifacts are
@check("age: artifacts fetched four minutes ago pass; exactly six hours old still passes; one second more is stale")
def _():
    fetched = datetime(2026, 10, 28, 6, 56, 33, tzinfo=timezone.utc)
    assert da.validate_outputs(REPORTS, fetched + timedelta(minutes=4))["verification"]["overall"] == "PASS" and da.validate_outputs(REPORTS, fetched + timedelta(hours=6))["verification"]["overall"] == "PASS"
    raises(da.AutomationError, lambda: da.validate_outputs(REPORTS, fetched + timedelta(hours=6, seconds=1)), "stale")
@check("age: a fetch time in the future is refused; the six-hour limit is the maximum artifact age")
def _():
    raises(da.AutomationError, lambda: da.validate_outputs(REPORTS, datetime(2026, 10, 28, 6, 56, 32, tzinfo=timezone.utc)), "future"); assert ev.MAX_ARTIFACT_AGE_SECONDS == 6 * 3600
@check("age: a set from yesterday's run (about 24 hours old) is refused as stale - the same protection the calendar check gave, without any calendar")
def _():
    try: da.validate_outputs(REPORTS, NOW + timedelta(hours=24)); raise AssertionError("accepted")
    except da.AutomationError as e: assert e.verification["checks"][[c["id"] for c in e.verification["checks"]].index("MAN-AGE")]["outcome"] == "FAIL"
@check("age: a time without a date and time part (a bare date) is refused - the age cannot be measured")
def _(): raises(da.AutomationError, lambda: da.validate_outputs(REPORTS, NOW.date()), "date and time")

# ================================================================ one authoritative date, from one clock reading in the producer
def produce_at(moment, run_id="55", attempt="1"):
    d = new_temp_dir(); out, err = io.StringIO(), io.StringIO()
    def fake_get(url, params=None, **kwargs): return fakehttp.FakeResponse(REAL[params["pair"]])
    with mock.patch("requests.get", side_effect=fake_get), mock.patch("time.sleep"), mock.patch.object(run_workflow, "datetime", wraps=datetime) as dt, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        dt.now.return_value = moment; code = run_workflow.main(d, run_id=run_id, run_attempt=attempt)
    assert code == 0, err.getvalue(); return d
@check("midnight: a producer that ran at 23:59:50 and a validator that runs after midnight AGREE on the date (the date comes from the manifest, not from a second clock)")
def _():
    d = produce_at(datetime(2026, 10, 27, 23, 59, 50, tzinfo=timezone.utc)); m = json.load(open(os.path.join(d, "report_2026-10-26_manifest.json")))
    assert m["analysis_date"] == "2026-10-26" and m["fetched_at_utc"] == "2026-10-27 23:59:50 UTC"
    after = datetime(2026, 10, 28, 0, 0, 30, tzinfo=timezone.utc); out = da.validate_outputs(d, after); assert out["date"] == "2026-10-26" and out["verification"]["overall"] == "PASS"
    code, o, e = run(["validate", "--reports-dir", d], now=after); assert code == 0 and "2026-10-26" in o, (o, e)
@check("midnight: the validate step publishes the manifest's analysis date through GITHUB_OUTPUT")
def _():
    f = os.path.join(new_temp_dir(), "out.txt"); open(f, "w").close(); code, o, e = run(["validate", "--reports-dir", REPORTS], env={"GITHUB_OUTPUT": f}); assert code == 0 and open(f).read() == "analysis_date=2026-10-27\n"
@check("source: no code path derives an expected date from a clock any more (no 'today - 1 day' in the validation code); the clock is passed in")
def _():
    text = open(os.path.join(SRC, "daily_automation.py"), encoding="utf-8").read(); section = text[text.index("def validate_outputs"):text.index("# ---------------------------------------------------------------- the payloads")]
    assert "timedelta(days=1)" not in section and "utc_today" not in section and "datetime.now" not in section and "utc_now()" not in section, "a clock is read in validate_outputs"
    assert "utc_today" not in text[text.index("def main("):], "main must not derive a date"
@check("DAT-1: a manifest whose analysis date is not the day before its own fetch time is refused")
def _():
    d = private_reports(); rewrite_manifest(d, lambda m: m.update(fetched_at_utc="2026-10-29 06:56:33 UTC"))
    try: da.validate_outputs(d, NOW); raise AssertionError("accepted")
    except da.AutomationError as e: ids = {c["id"]: c["outcome"] for c in e.verification["checks"]}; assert ids["DAT-1"] == "FAIL" and ids["MAN-FETCHED"] == "FAIL"
@check("date: an explicit --date selects exactly that manifest; a date with no manifest is refused; without --date two manifests are ambiguous")
def _():
    d = private_reports(); assert da.validate_outputs(d, NOW, date="2026-10-27")["date"] == "2026-10-27"; raises(da.AutomationError, lambda: da.validate_outputs(d, NOW, date="2026-10-26"), "manifest")
    for suffix in ("_manifest.json", ".md", "_data.csv"): shutil.copy(os.path.join(d, f"report_2026-10-27{suffix}"), os.path.join(d, f"report_2026-10-26{suffix}"))
    raises(da.AutomationError, lambda: da.validate_outputs(d, NOW), "more than one manifest"); assert da.validate_outputs(d, NOW, date="2026-10-27")["date"] == "2026-10-27"
@check("date: older report files without a manifest are ignored; only a complete set with its manifest counts")
def _():
    d = private_reports(); open(os.path.join(d, "report_2026-10-26.md"), "w").write("old"); open(os.path.join(d, "report_2026-10-26_data.csv"), "w").write("old"); assert da.validate_outputs(d, NOW)["date"] == "2026-10-27"
@check("date: the explicit date given to validate must equal the manifest's, the CSV's and the file names' (MAN-DATE)")
def _():
    d = private_reports(); rewrite_manifest(d, lambda m: m.update(analysis_date="2026-10-26"))
    raises(da.AutomationError, lambda: da.validate_outputs(d, NOW, date="2026-10-27"), "MAN-DATE")

# ================================================================ the date and the run identity travel explicitly to the payloads
@check("build-payload takes the date and the run attempt explicitly (both required) and writes run_context.json with the run, the attempt, the date and the SHA-256 of every payload file")
def _():
    code, e, out = payloads(); assert code == 0, e; c = json.load(open(os.path.join(out, "run_context.json")))
    assert c["run_id"] == RUN_ID and c["run_attempt"] == 1 and c["analysis_date"] == "2026-10-27" and set(c["payloads"]) == {"upsert_analysis.json", "verify_analysis.json", "send_success_email.json", "send_success_report.json"}
    assert all(c["payloads"][n] == da._file_sha256(os.path.join(out, n)) for n in c["payloads"])
    for argv in (["build-payload", "--reports-dir", REPORTS, "--out-dir", new_temp_dir(), "--run-id", RUN_ID, "--run-url", RUN_URL, "--date", "2026-10-27"], ["build-payload", "--reports-dir", REPORTS, "--out-dir", new_temp_dir(), "--run-id", RUN_ID, "--run-url", RUN_URL, "--run-attempt", "1"]):
        assert run(argv)[0] == 2
@check("RUN-001: payloads cannot be built for another run or another attempt than the one that produced the manifest; an explicit date that is not the manifest's is refused")
def _():
    for attempt, run_id, date_text, word in (("2", RUN_ID, "2026-10-27", "RUN-001"), ("1", "999", "2026-10-27", "RUN-001"), ("1", RUN_ID, "2026-10-26", "manifest")):
        code, e, out = payloads(attempt, run_id, date_text); assert code == 1 and word in e and not os.path.exists(os.path.join(out, "upsert_analysis.json")), (word, e)
    assert run(["build-payload", "--reports-dir", REPORTS, "--out-dir", new_temp_dir(), "--run-id", "local-20261028T070000Z-abcdef12", "--run-attempt", "1", "--run-url", RUN_URL, "--date", "2026-10-27"])[0] == 1   # a local id can never post
@check("stale payloads: a payload file that was changed, or that belongs to another run's context, is refused by post BEFORE any secret is read or any request is made")
def _():
    code, e, out = payloads(); assert code == 0
    p = os.path.join(out, "upsert_analysis.json"); open(p, "a", encoding="utf-8", newline="").write(" ")          # any change to the payload file
    code, o, e = run(["post", "--action", "upsert_analysis", "--dir", out]); assert code == 1 and "does not match this run's context" in e and "nothing was sent" in e, e
    code2, e2, out2 = payloads(); c = json.load(open(os.path.join(out2, "run_context.json"))); c["analysis_date"] = "2026-10-20"; json.dump(c, open(os.path.join(out2, "run_context.json"), "w"))
    code, o, e = run(["post", "--action", "upsert_analysis", "--dir", out2]); assert code == 1 and "does not match this run's context" in e
@check("stale payloads: when the success report cannot be built, an old send_success_report.json from an earlier execution is removed, never reused")
def _():
    out = new_temp_dir(); open(os.path.join(out, "send_success_report.json"), "w").write('{"old": true}')
    old = da.build_report_payload
    def broken(*a, **k): raise da.AutomationError("the charts could not be drawn")
    da.build_report_payload = broken
    try: code, o, e = run(["build-payload", "--reports-dir", REPORTS, "--out-dir", out, "--run-id", RUN_ID, "--run-attempt", "1", "--run-url", RUN_URL, "--date", "2026-10-27"])
    finally: da.build_report_payload = old
    assert code == 0 and not os.path.exists(os.path.join(out, "send_success_report.json")) and "send_success_report.json" not in json.load(open(os.path.join(out, "run_context.json")))["payloads"]
@check("results carry the run: a recorded reply is stamped with the run id, the attempt and the analysis date of the payload's context")
def _():
    code, e, out = payloads(); ctx = da.load_run_context(out); da._record(out, "upsert_analysis", {"success": True}, da._stamp_of(ctx))
    assert json.load(open(os.path.join(out, "upsert_analysis_result.json")))["run_context"] == {"run_id": RUN_ID, "run_attempt": 1, "analysis_date": "2026-10-27"}
    da._record(out, "verify_analysis", {"success": True}); assert "run_context" not in json.load(open(os.path.join(out, "verify_analysis_result.json")))

# ================================================================ the dashboard takes the date it is given
@check("dashboard --date: the CSV of exactly that date is used even when a newer CSV is present; no date still means the newest; a missing or malformed date is refused")
def _():
    d = new_temp_dir()
    for n in os.listdir(REPORTS): shutil.copy(os.path.join(REPORTS, n), os.path.join(d, n))
    for suffix in (".md", "_data.csv"): shutil.copy(os.path.join(d, f"report_2026-10-27{suffix}"), os.path.join(d, f"report_2026-10-28{suffix}"))
    out = new_temp_dir(); assert os.path.basename(dashboard.generate(d, out_dir=out, date_text="2026-10-27")) == "dashboard_2026-10-27.html"
    for bad, word in (("2026-10-26", "was not found"), ("2026-1-27", "YYYY-MM-DD"), ("../x", "YYYY-MM-DD"), ("", "YYYY-MM-DD")): raises(dashboard.DashboardInputError, lambda: dashboard.generate(d, out_dir=new_temp_dir(), date_text=bad), word)
    code = dashboard.main(["--reports-dir", d, "--out-dir", new_temp_dir(), "--date", "2026-10-27"]); assert code == 0
@check("dashboard --date: the file name date and the CSV's own last date must still agree")
def _():
    d = new_temp_dir()
    for suffix in (".md", "_data.csv"): shutil.copy(os.path.join(REPORTS, f"report_2026-10-27{suffix}"), os.path.join(d, f"report_2026-10-26{suffix}"))
    raises(dashboard.DashboardInputError, lambda: dashboard.generate(d, out_dir=new_temp_dir(), date_text="2026-10-26"), "last date")

# ================================================================ the consumer's verification record is written separately, and never blocks or masks the outcome
@check("validate --out-dir writes consumer_verification.json (all checks PASS, the manifest's run and date); it is a separate file from the manifest")
def _():
    out = new_temp_dir(); code, o, e = run(["validate", "--reports-dir", REPORTS, "--out-dir", out]); assert code == 0, e
    r = json.load(open(os.path.join(out, ev.VERIFICATION_NAME))); assert r["overall"] == "PASS" and r["run_id"] == RUN_ID and r["analysis_date"] == "2026-10-27" and len(r["checks"]) == 10
@check("validate --out-dir: a failed verification is recorded too (overall FAIL with the failing check); a failure before the check (nothing produced) is recorded as NOT RUN, never PASS")
def _():
    d = private_reports(); open(os.path.join(d, "report_2026-10-27_data.csv"), "a").write(""); p = os.path.join(d, "report_2026-10-27.md"); open(p, "a", encoding="utf-8", newline="").write("tampered\n")
    out = new_temp_dir(); code, o, e = run(["validate", "--reports-dir", d, "--out-dir", out]); assert code == 1 and "MAN-HASH-REPORT" in e
    assert json.load(open(os.path.join(out, ev.VERIFICATION_NAME)))["overall"] == "FAIL"
    out2 = new_temp_dir(); code, o, e = run(["validate", "--reports-dir", new_temp_dir(), "--out-dir", out2]); r = json.load(open(os.path.join(out2, ev.VERIFICATION_NAME))); assert code == 1 and r["overall"] == "NOT RUN" and r["checks"] == [] and r["reason"]
@check("validate --out-dir: an unwritable output folder is only a warning - the validation outcome (exit 0) is unchanged")
def _():
    blocker = os.path.join(new_temp_dir(), "automation_out"); open(blocker, "w").write("a file"); code, o, e = run(["validate", "--reports-dir", REPORTS, "--out-dir", blocker]); assert code == 0 and "WARNING" in e and "not written" in e

# ================================================================ run_status.json: explicit stage outcomes from this run's own evidence
IDENTITY = {"run_id": RUN_ID, "run_attempt": 1, "analysis_date": "2026-10-27"}
STAMP = {"run_id": RUN_ID, "run_attempt": 1, "analysis_date": "2026-10-27"}
ALL_OK = ["analysis=success", "validate_outputs=success", "build_payload=success", "post_to_sheet=success", "verify_sheet=success", "send_success_email=success"]


def evidence(stamp=STAMP, upsert=None, verify=None, send=None, verification=True, report_payload=True):
    out = new_temp_dir(); code, e, folder = payloads(); assert code == 0, e
    for n in os.listdir(folder): shutil.copy(os.path.join(folder, n), os.path.join(out, n))
    if not report_payload: os.remove(os.path.join(out, "send_success_report.json"))
    if verification:
        code, o, e = run(["validate", "--reports-dir", REPORTS, "--out-dir", out]); assert code == 0, e
    pick = lambda given, default: default if given is None else given          # None = the usual reply, False = no result file at all
    results = {"upsert_analysis": pick(upsert, {"success": True, "sheet_status": "ok", "write_confirmed": True, "rows_inserted": 2, "rows_updated": 0}),
               "verify_analysis": pick(verify, {"success": True, "matches": True}),
               "send_success_report": pick(send, {"success": True, "sheet_status": "ok", "email_status": "sent", "email_format": "html"})}
    for action, body in results.items():
        if body is not False: da._record(out, action, body, stamp)
    return out


def status(out, outcomes=ALL_OK, run_id=RUN_ID, attempt="1", env=None):
    argv = ["summarize-run", "--dir", out, "--run-id", run_id, "--run-attempt", attempt] + sum((["--outcome", o] for o in outcomes), [])
    code, o, e = run(argv, env=env); assert code == 0, (o, e)
    return json.load(open(os.path.join(out, da.STATUS_FILE))), o, e
def stage(s, name): return s["stages"][name]["outcome"]


@check("run status: a complete run with evidence stamped for THIS run is PASS at every stage; the dispatch stage says the message was accepted by MailApp and never claims delivery")
def _():
    s, o, e = status(evidence()); assert s["overall"] == "PASS" and all(v["outcome"] == "PASS" for v in s["stages"].values()) and list(s["stages"]) == list(da.STATUS_STAGES), s
    assert s["run_id"] == RUN_ID and s["run_attempt"] == 1 and s["analysis_date"] == "2026-10-27" and s["diagnostics"] == "not_applicable"
    text = json.dumps(s).lower(); assert "accepted by mailapp" in text and "delivered" not in text and "not confirmation that it reached a mailbox" in text
@check("run status: the analysis date comes from the verified manifest (the consumer verification), never from a clock")
def _():
    s, o, e = status(evidence()); assert s["analysis_date"] == "2026-10-27"
@check("run status: the file is written, the job summary table is appended, and the command exits 0 (it is a diagnostic and never changes the pipeline's outcome)")
def _():
    out = evidence(); summary = os.path.join(new_temp_dir(), "summary.md"); open(summary, "w").close(); s, o, e = status(out, env={"GITHUB_STEP_SUMMARY": summary})
    text = open(summary, encoding="utf-8").read(); assert "### Run status" in text and "| sheets_update | PASS |" in text and "email_dispatch_acceptance" in text
@check("stale results: a result file with another run's id, another attempt or another analysis date does not count - the stage is INCONCLUSIVE even though the step reported success")
def _():
    for stamp in ({"run_id": "999", "run_attempt": 1, "analysis_date": "2026-10-27"}, {"run_id": RUN_ID, "run_attempt": 2, "analysis_date": "2026-10-27"}, {"run_id": RUN_ID, "run_attempt": 1, "analysis_date": "2026-10-20"}):
        s, o, e = status(evidence(stamp=stamp)); assert stage(s, "sheets_update") == stage(s, "sheets_verification") == stage(s, "email_dispatch_acceptance") == "INCONCLUSIVE" and s["overall"] == "INCONCLUSIVE", stamp
@check("file presence alone is never PASS: a result without the run stamp, and a reply that does not confirm the stage, are INCONCLUSIVE")
def _():
    s, o, e = status(evidence(stamp=None)); assert stage(s, "sheets_update") == "INCONCLUSIVE" and stage(s, "email_dispatch_acceptance") == "INCONCLUSIVE"
    s, o, e = status(evidence(upsert={"success": True, "sheet_status": "unconfirmed", "write_confirmed": False}, verify={"success": True, "matches": False})); assert stage(s, "sheets_update") == "INCONCLUSIVE" and stage(s, "sheets_verification") == "INCONCLUSIVE"
@check("file presence alone is never PASS: a missing result file, a missing verification record or a missing run context is INCONCLUSIVE or FAIL, never PASS")
def _():
    s, o, e = status(evidence(upsert=False)); assert stage(s, "sheets_update") == "INCONCLUSIVE"
    s, o, e = status(evidence(verification=False)); assert stage(s, "outputs_persisted") == "INCONCLUSIVE" and s["overall"] != "PASS"
    out = evidence(); os.remove(os.path.join(out, da.RUN_CONTEXT_FILE)); s, o, e = status(out); assert stage(s, "email_payload_generation") == "INCONCLUSIVE"
@check("partial failure: Sheets updated and verified, the HTML report payload not generated, the email step failed -> PARTIAL, payload FAIL, dispatch FAIL; Sheets stages stay PASS")
def _():
    out = evidence(report_payload=False, send=False); s, o, e = status(out, ALL_OK[:5] + ["send_success_email=failure"])
    assert stage(s, "sheets_update") == stage(s, "sheets_verification") == "PASS" and stage(s, "email_payload_generation") == "FAIL" and stage(s, "email_dispatch_acceptance") == "FAIL"
    assert s["overall"] == "FAIL" and s["summary"].startswith("PARTIAL: Sheets updated and verified")
@check("email outcomes: sent_dedup_failed and skipped_duplicate are accepted (with a note); send_outcome_unknown is INCONCLUSIVE; a failed or refused send is FAIL; never 'delivered'")
def _():
    for reply, expected in (({"success": True, "email_status": "sent_dedup_failed", "sheet_status": "ok"}, "PASS"), ({"success": True, "email_status": "skipped_duplicate", "sheet_status": "ok"}, "PASS"),
                            ({"success": False, "error": "SEND_OUTCOME_UNKNOWN", "email_status": "send_outcome_unknown", "sheet_status": "ok"}, "INCONCLUSIVE"),
                            ({"success": False, "error": "EMAIL_FAILED", "email_status": "failed", "sheet_status": "ok"}, "FAIL"), ({"success": False, "error": "INVALID_PAYLOAD", "email_status": "not_attempted"}, "FAIL")):
        s, o, e = status(evidence(send=reply), ALL_OK[:5] + ["send_success_email=failure" if expected != "PASS" else "send_success_email=success"])
        got = stage(s, "email_dispatch_acceptance"); assert got == expected or (expected != "PASS" and got in ("FAIL", expected)), (reply, got)
        if reply["email_status"] == "send_outcome_unknown": assert got == "FAIL" or got == "INCONCLUSIVE"
        assert "delivered" not in json.dumps(s).lower()
@check("email outcomes: a send_outcome_unknown reply on a step that reported success is INCONCLUSIVE (the message may or may not have been accepted)")
def _():
    s, o, e = status(evidence(send={"success": False, "error": "SEND_OUTCOME_UNKNOWN", "email_status": "send_outcome_unknown", "sheet_status": "ok"})); assert stage(s, "email_dispatch_acceptance") == "INCONCLUSIVE" and s["overall"] == "INCONCLUSIVE"
@check("step outcomes: failure -> FAIL, cancelled -> INCONCLUSIVE, skipped after an upstream failure -> BLOCKED, skipped or missing with nothing wrong upstream -> NOT RUN")
def _():
    s, o, e = status(evidence(verification=False), ["analysis=failure", "validate_outputs=skipped", "build_payload=skipped", "post_to_sheet=skipped", "verify_sheet=skipped", "send_success_email=skipped"])
    assert stage(s, "analysis") == "FAIL" and stage(s, "outputs_persisted") == "FAIL" and all(stage(s, n) == "BLOCKED" for n in ("sheets_update", "sheets_verification", "email_payload_generation", "email_dispatch_acceptance")), s
    assert s["diagnostics"] == "not_persisted"
    s, o, e = status(new_temp_dir(), []); assert all(v["outcome"] == "NOT RUN" for v in s["stages"].values()) and s["overall"] == "NOT RUN"
    s, o, e = status(evidence(), ALL_OK[:3] + ["post_to_sheet=cancelled"] + ALL_OK[4:]); assert stage(s, "sheets_update") == "INCONCLUSIVE"
@check("diagnostics: a failed analysis whose validation_result.json was persisted is reported as persisted; its absence is reported as not_persisted - never as success")
def _():
    out = new_temp_dir(); outcomes = ["analysis=failure", "validate_outputs=skipped"]
    assert status(out, outcomes)[0]["diagnostics"] == "not_persisted"; open(os.path.join(out, ev.DIAGNOSTIC_NAME), "w").write("{}"); assert status(out, outcomes)[0]["diagnostics"] == "persisted"
@check("a verification that did not pass is FAIL at the persistence stage, even if the analysis step succeeded")
def _():
    out = evidence(); r = json.load(open(os.path.join(out, ev.VERIFICATION_NAME))); r["overall"] = "FAIL"; json.dump(r, open(os.path.join(out, ev.VERIFICATION_NAME), "w"))
    s, o, e = status(out); assert stage(s, "outputs_persisted") == "FAIL" and s["overall"] == "FAIL"
    s, o, e = status(evidence(), ["analysis=success", "validate_outputs=failure"] + ALL_OK[2:]); assert stage(s, "outputs_persisted") == "FAIL"
@check("run status: another run's verification record does not establish this run's persistence (INCONCLUSIVE)")
def _():
    out = evidence(); s, o, e = status(out, run_id="999"); assert stage(s, "outputs_persisted") == "INCONCLUSIVE" and s["analysis_date"] is None
@check("run status: malformed arguments are refused (exit 1, nothing written) and an unwritable folder is only a warning (exit 0)")
def _():
    code, o, e = run(["summarize-run", "--dir", new_temp_dir(), "--run-id", "x", "--run-attempt", "1"]); assert code == 1
    code, o, e = run(["summarize-run", "--dir", new_temp_dir(), "--run-id", RUN_ID, "--run-attempt", "0"]); assert code == 1
    code, o, e = run(["summarize-run", "--dir", new_temp_dir(), "--run-id", RUN_ID, "--run-attempt", "1", "--outcome", "analysis=maybe"]); assert code == 1
    blocker = os.path.join(new_temp_dir(), "automation_out"); open(blocker, "w").write("a file"); code, o, e = run(["summarize-run", "--dir", blocker, "--run-id", RUN_ID, "--run-attempt", "1", "--outcome", "analysis=success"])
    assert code == 0 and "WARNING" in e
@check("the run-status step is the nineteenth workflow step, always runs, and is known to the failure notice and mapped to a stage the web app accepts")
def _():
    assert "summarize_run" in da.STAGE_ORDER and da.SERVER_STAGE["summarize_run"] == "upload_artifacts" and da.STAGE_ORDER.index("summarize_run") == da.STAGE_ORDER.index("send_success_email") + 1

R.finish()
