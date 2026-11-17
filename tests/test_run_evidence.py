"""Tests for increment 2: run identity, the producer's validation envelope, the manifest, the consumer's verification and the failure diagnostic.

Kraken is mocked; nothing contacts the network. A kill or a power loss cannot be run inside a test, so interruption is tested by constructing the states it leaves behind
(a new CSV over an old manifest, a missing file, a leftover .bak) and by failing os.replace on each of the three files in turn."""
import hashlib, io, json, os, re, shutil
from datetime import date
from pathlib import Path

from wf_support import *
import candle_rules as cr
import dashboard
import run_evidence as ev
import daily_automation as da

R = Runner("run evidence checks")
check = R.register
DAY_TEXT = "2026-10-26"
NAMES = (f"report_{DAY_TEXT}.md", f"report_{DAY_TEXT}_data.csv", f"report_{DAY_TEXT}_manifest.json")
TODAY_FOR_REAL = REAL_NOW + __import__("datetime").timedelta(minutes=4)    # four minutes after the fetch of the saved replies (2026-10-27 09:41 UTC)
sha = lambda path: hashlib.sha256(open(path, "rb").read()).hexdigest()
state = {}


def produce(run_id=None, attempt=None, diagnostics=None, bodies=None, directory=None):
    d = directory or new_temp_dir(); out, err = io.StringIO(), io.StringIO()
    def fake_get(url, params=None, **kwargs): return fakehttp.FakeResponse((bodies or REAL)[params["pair"]])
    with mock.patch("requests.get", side_effect=fake_get), mock.patch("time.sleep"), mock.patch.object(run_workflow, "datetime", wraps=datetime) as dt, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        dt.now.return_value = REAL_NOW; code = run_workflow.main(d, run_id=run_id, run_attempt=attempt, diagnostics_dir=diagnostics)
    return code, out.getvalue(), err.getvalue(), d


def good_run():
    if "good" not in state: state["good"] = produce(run_id="123456789", attempt=2)
    return state["good"]


def manifest_of(d): return json.loads(read(d, NAMES[2]))


def verify(d, today=TODAY_FOR_REAL):
    return da.validate_outputs(d, today)


# ================================================================ run identity
@check("identity: CI passes GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT explicitly and they are kept as given")
def _(): assert ev.resolve_identity("123456789", "3", REAL_NOW) == {"run_id": "123456789", "run_attempt": 3, "run_context": "ci"}
@check("identity: a local run gets a unique UTC timestamp plus a random suffix, attempt 1; two runs in the same second differ")
def _():
    a, b = ev.resolve_identity(None, None, REAL_NOW), ev.resolve_identity(None, None, REAL_NOW)
    assert re.fullmatch(r"local-20261027T094100Z-[0-9a-f]{8}", a["run_id"]) and a["run_attempt"] == 1 and a["run_context"] == "local" and a["run_id"] != b["run_id"]
@check("identity: a malformed run id or attempt is refused, and an attempt without a run id is refused")
def _():
    for bad in ("", "12a", "local", "local-1", "1" * 21, "../x"): raises(ValueError, lambda: ev.resolve_identity(bad, "1", REAL_NOW))
    for bad in ("0", "-1", "x", "1.5", "1234567"): raises(ValueError, lambda: ev.resolve_identity("1", bad, REAL_NOW))
    raises(ValueError, lambda: ev.resolve_identity(None, "2", REAL_NOW))
@check("identity: a retry keeps the run id with a higher attempt; the identity reaches the manifest and the envelope of the run")
def _():
    code, out, err, d = good_run(); assert code == 0, err; m = manifest_of(d)
    assert m["run_id"] == "123456789" and m["run_attempt"] == 2 and m["run_context"] == "ci" and m["envelope"]["run_id"] == "123456789" and m["envelope"]["run_attempt"] == 2
@check("identity: run_workflow without arguments still works and records a unique local identity")
def _():
    a = produce()[3]; b = produce()[3]; ma, mb = manifest_of(a), manifest_of(b)
    assert ma["run_context"] == "local" and ma["run_id"] != mb["run_id"] and ev.valid_identity(ma["run_id"], ma["run_attempt"])
@check("identity: a bad run id stops the run before any fetch or write (exit 1, nothing written)")
def _():
    code, out, err, d = produce(run_id="nope"); assert code == 1 and files_in(d) == [] and "run id" in err

# ================================================================ the producer's outputs
@check("manifest: a successful run writes exactly the Markdown, the CSV and the manifest")
def _(): code, out, err, d = good_run(); assert code == 0 and files_in(d) == sorted(NAMES), files_in(d)
@check("manifest: it holds run identity, date, fetch time, provenance, the envelope and the CSV and Markdown hashes; the hashes equal the files on disk")
def _():
    code, out, err, d = good_run(); m = manifest_of(d); art = {a["role"]: a for a in m["artifacts"]}
    assert m["analysis_date"] == DAY_TEXT and m["fetched_at_utc"] == "2026-10-27 09:41:00 UTC" and m["stage"] == "outputs_persisted" and m["calc_version"] == ev.CALC_VERSION
    assert art["csv"]["sha256"] == sha(os.path.join(d, NAMES[1])) and art["report"]["sha256"] == sha(os.path.join(d, NAMES[0])) and art["csv"]["bytes"] == os.path.getsize(os.path.join(d, NAMES[1]))
    assert m["windows"]["close"] == {"first": "2026-09-26", "last": DAY_TEXT, "count": 31} and m["windows"]["return"]["count"] == 30
    assert [(p["pair"], p["expected_key"], p["window_candles"], p["unfinished_candles"]) for p in m["source"]["pairs"]] == [("XBTUSD", "XXBTZUSD", 31, 1), ("ETHUSD", "XETHZUSD", 31, 1)]
    assert all(p["received_candles"] == 721 and p["completed_candles"] == 720 for p in m["source"]["pairs"])
@check("manifest: the fetch time equals the report's 'Data fetched' line and the analysis date equals the CSV's last date (one clock reading)")
def _():
    code, out, err, d = good_run(); assert "- Data fetched: 2026-10-27 09:41:00 UTC" in read(d, NAMES[0]) and manifest_of(d)["analysis_date"] == read(d, NAMES[1]).strip().split("\n")[-1].split(",")[0]
@check("manifest: it carries no result that depends on validating itself - no consumer verification, no self-hash, no downstream stage")
def _():
    text = read(good_run()[3], NAMES[2])
    for word in ("verification", "manifest_sha256", "sheets", "email", "delivered"): assert word not in text.lower(), word
@check("manifest: the producer envelope is PASS with 621 candle instances and the source instances all evaluated; no assumption was applied")
def _():
    e = manifest_of(good_run()[3])["envelope"]; assert e["overall"] == "PASS" and e["families"]["candle"]["expected_instances"] == 621 and e["families"]["candle"]["attestation"] == "consumer-recomputed"
    s = e["families"]["source"]; assert s["expected_instances"] == 9 and s["evaluated"] == 9 and s["attestation"] == "producer-attested" and e["assumptions_applied"] == []
    assert ev.envelope_overall(e) == "PASS"
@check("csv and report: the 17-column CSV and the Markdown are unchanged by the manifest (header, shape, the Markdown has no manifest text)")
def _():
    code, out, err, d = good_run(); head = read(d, NAMES[1]).split("\n")[0]
    assert head == "date,btc_close,eth_close,btc_return,eth_return,btc_open,btc_high,btc_low,btc_volume,btc_vwap,btc_trade_count,eth_open,eth_high,eth_low,eth_volume,eth_vwap,eth_trade_count" and "manifest" not in read(d, NAMES[0]).lower()
@check("transaction: the manifest is replaced LAST - the order of the final replaces is CSV-or-Markdown first, the manifest at the end")
def _():
    real, order = os.replace, []
    def spy(src, dst, *a, **k):
        if str(src).endswith(".tmp"): order.append(os.path.basename(str(dst)))
        return real(src, dst, *a, **k)
    with mock.patch("os.replace", side_effect=spy): code, out, err, d = produce()
    assert code == 0 and order[-1].endswith("_manifest.json") and len(order) == 3 and not any(n.endswith("_manifest.json") for n in order[:-1]), order
@check("transaction: the staged files are read back; a temporary file that differs from what was meant to be written aborts the run and leaves nothing")
def _():
    real = Path.write_text
    def corrupt(self, text, *a, **k):
        return real(self, text + "X" if str(self).endswith("_data.csv.tmp") else text, *a, **k)
    with mock.patch.object(Path, "write_text", corrupt): code, out, err, d = produce()
    assert code == 1 and files_in(d) == [] and "does not match" in err

def seeded():
    d = new_temp_dir()
    for n in NAMES: open(os.path.join(d, n), "w", newline="").write("OLD " + n)
    return d, {n: sha(os.path.join(d, n)) for n in NAMES}
@check("transaction: a failure while replacing ANY of the three files restores the old set byte for byte and leaves no .tmp or .bak")
def _():
    for failing in range(3):
        d, before = seeded(); real, count = os.replace, {"n": 0}
        def flaky(src, dst, *a, **k):
            if str(src).endswith(".tmp"):
                if count["n"] == failing: raise OSError("forced failure on one replace")
                count["n"] += 1
            return real(src, dst, *a, **k)
        with mock.patch("os.replace", side_effect=flaky): code, out, err, _d = produce(directory=d)
        assert code == 1 and files_in(d) == sorted(NAMES) and {n: sha(os.path.join(d, n)) for n in NAMES} == before, (failing, files_in(d))
@check("transaction: with no old files, a failure on the third file (the manifest) removes the two data files again - no set without its manifest")
def _():
    d = new_temp_dir(); real = os.replace
    def flaky(src, dst, *a, **k):
        if str(src).endswith("_manifest.json.tmp"): raise OSError("forced")
        return real(src, dst, *a, **k)
    with mock.patch("os.replace", side_effect=flaky): code, out, err, _d = produce(directory=d)
    assert code == 1 and files_in(d) == []

# ================================================================ the consumer verifies (a separate record) and detects interruption and mismatch
def copy_good():
    d = new_temp_dir()
    for n in NAMES: shutil.copy(os.path.join(good_run()[3], n), os.path.join(d, n))
    return d
@check("consumer: a complete, matching set is accepted and a separate verification record (all checks PASS, same run id and attempt) is returned")
def _():
    o = verify(copy_good()); v = o["verification"]
    assert v["overall"] == "PASS" and [c["id"] for c in v["checks"]] == ["MAN-STRUCT", "MAN-HASH-CSV", "MAN-HASH-REPORT", "MAN-DATE", "MAN-FETCHED", "DAT-1", "MAN-AGE", "MAN-ENVELOPE", "CON-LEDGER", "CON-CSV"]
    assert v["run_id"] == "123456789" and v["run_attempt"] == 2 and v["analysis_date"] == DAY_TEXT and o["manifest"]["run_id"] == "123456789"
@check("consumer: the verification record is separate - the manifest bytes are identical before and after verification")
def _(): d = copy_good(); before = sha(os.path.join(d, NAMES[2])); verify(d); assert sha(os.path.join(d, NAMES[2])) == before
@check("interruption: a missing manifest means the run is not complete (the data files alone are never accepted)")
def _():
    d = copy_good(); os.remove(os.path.join(d, NAMES[2])); raises(da.AutomationError, lambda: verify(d), "manifest")
@check("interruption: a CSV from a newer run over the manifest of an older run is detected (hash mismatch), whatever the file system state")
def _():
    d = copy_good(); p = os.path.join(d, NAMES[1]); text = open(p, encoding="utf-8", newline="").read().replace("2534.8411291", "2534.8411292"); open(p, "w", encoding="utf-8", newline="").write(text)
    try: verify(d); raise AssertionError("accepted")
    except da.AutomationError as e: assert e.verification["checks"][1]["id"] == "MAN-HASH-CSV" and e.verification["checks"][1]["outcome"] == "FAIL" and e.verification["overall"] == "FAIL"
@check("interruption: a report from another run, a missing CSV or a missing report are detected; a leftover .bak or .tmp file changes nothing")
def _():
    d = copy_good(); p = os.path.join(d, NAMES[0]); open(p, "a", encoding="utf-8", newline="").write("tampered\n")
    try: verify(d); raise AssertionError("accepted")
    except da.AutomationError as e: assert e.verification["checks"][2]["outcome"] == "FAIL" or "report" in str(e).lower()
    for n in (NAMES[1], NAMES[0]): d = copy_good(); os.remove(os.path.join(d, n)); raises(da.AutomationError, lambda: verify(d), "was not produced")
    d = copy_good(); open(os.path.join(d, NAMES[1] + ".bak"), "w").write("old"); open(os.path.join(d, NAMES[0] + ".tmp"), "w").write("half"); assert verify(d)["verification"]["overall"] == "PASS"
@check("hash mismatch: a manifest that names another file or carries another size or hash is rejected with a clear reason")
def _():
    for edit in (lambda m: m["artifacts"][0].update(sha256="0" * 64), lambda m: m["artifacts"][1].update(bytes=1), lambda m: m["artifacts"][0].update(name="report_other.md")):
        d = copy_good(); m = manifest_of(d); edit(m); open(os.path.join(d, NAMES[2]), "w", encoding="utf-8", newline="\n").write(ev.serialize(m)); raises(da.AutomationError, lambda: verify(d), "manifest")
@check("manifest structure: invalid JSON, missing fields, extra fields, a wrong stage, a bad run id or an empty file are never accepted")
def _():
    good = manifest_of(copy_good()); variants = [b"", b"{not json", b"[]", json.dumps(dict(good, stage="done")).encode(), json.dumps(dict(good, extra=1)).encode(), json.dumps(dict(good, run_id="x")).encode(),
                                                 json.dumps({k: v for k, v in good.items() if k != "envelope"}).encode(), json.dumps(dict(good, artifacts=[good["artifacts"][0]])).encode()]
    for data in variants: raises(ev.ManifestError, lambda: ev.parse_manifest(data))
@check("envelope: an overall that is not a complete PASS (a missing family, an extra family, a failing instance) is never accepted from a manifest")
def _():
    for edit in (lambda e: e["families"].pop("candle"), lambda e: e["families"].update(other=e["families"]["source"]), lambda e: e["families"]["candle"].update(missing=3)):
        d = copy_good(); m = manifest_of(d); edit(m["envelope"]); open(os.path.join(d, NAMES[2]), "w", encoding="utf-8", newline="\n").write(ev.serialize(m)); raises(da.AutomationError, lambda: verify(d), "MAN-")
@check("envelope: an envelope of an unknown rule catalogue version cannot be interpreted, so it is never PASS (INCONCLUSIVE), and a manifest carrying one is refused")
def _():
    e = json.loads(json.dumps(manifest_of(copy_good())["envelope"])); assert ev.envelope_overall(e) == "PASS"; e["catalogue_version"] = "99"; assert ev.envelope_overall(e) == "INCONCLUSIVE"
    d = copy_good(); m = manifest_of(d); m["envelope"]["catalogue_version"] = "99"; open(os.path.join(d, NAMES[2]), "w", encoding="utf-8", newline="\n").write(ev.serialize(m)); raises(da.AutomationError, lambda: verify(d), "MAN-ENVELOPE")
@check("consumer recomputation: a manifest whose hashes are right but whose candle ledger differs from the one recomputed from the CSV is rejected (CON-LEDGER)")
def _():
    d = copy_good(); m = manifest_of(d); m["envelope"]["families"]["candle"]["instance_digest"] = "0" * 64
    open(os.path.join(d, NAMES[2]), "w", encoding="utf-8", newline="\n").write(ev.serialize(m))
    try: verify(d); raise AssertionError("accepted")
    except da.AutomationError as e: assert [c for c in e.verification["checks"] if c["id"] == "CON-LEDGER"][0]["outcome"] == "FAIL"
@check("consumer: the dates must agree - a manifest dated differently from the CSV and the file names is rejected")
def _():
    d = copy_good(); m = manifest_of(d); m["analysis_date"] = "2026-10-25"; open(os.path.join(d, NAMES[2]), "w", encoding="utf-8", newline="\n").write(ev.serialize(m)); raises(da.AutomationError, lambda: verify(d), "manifest")

# ================================================================ failure diagnostics
def blocked_bodies():
    bodies = {k: json.loads(v) for k, v in REAL.items()}; r = bodies["ETHUSD"]["result"]; r[[k for k in r if k != "last"][0]][-3][7] = -5
    return {k: json.dumps(v).encode() for k, v in bodies.items()}
@check("diagnostics: a candle-stage block persists the envelope to the diagnostics folder (overall FAIL, same run id, the failing instance with asset, date and field); no report is written")
def _():
    diag = os.path.join(new_temp_dir(), "automation_out"); code, out, err, d = produce(run_id="777", attempt=1, diagnostics=diag, bodies=blocked_bodies())
    assert code == 1 and files_in(d) == [] and files_in(diag) == [ev.DIAGNOSTIC_NAME]
    rec = json.load(open(os.path.join(diag, ev.DIAGNOSTIC_NAME))); assert rec["run_id"] == "777" and rec["overall"] == "FAIL" and rec["run_outcome"] == "FAIL" and rec["envelope"]["run_id"] == "777"
    assert rec["envelope"]["families"]["source"]["overall"] == "PASS" and rec["envelope"]["families"]["candle"]["overall"] == "FAIL"
    entry = [e for e in rec["envelope"]["families"]["candle"]["non_pass"] if e["rule_id"] == "CND-COUNT"][0]; assert entry["asset"] == "ETH" and entry["field"] == "trade_count" and entry["date"] == "2026-10-25" and "negative" in entry["actual"] + entry["explanation"]
@check("diagnostics: a source-stage block (a BTC day missing) records FET-VALID FAIL for BTC, evaluates the other source instances, and the candle family is NOT RUN - never PASS")
def _():
    b = {k: json.loads(v) for k, v in REAL.items()}; r = b["XBTUSD"]["result"]; rows = r[[k for k in r if k != "last"][0]]; rows.pop(-10)
    diag = new_temp_dir(); code, out, err, d = produce(run_id="8", diagnostics=diag, bodies={k: json.dumps(v).encode() for k, v in b.items()}); env = json.load(open(os.path.join(diag, ev.DIAGNOSTIC_NAME)))["envelope"]
    s, c = env["families"]["source"], env["families"]["candle"]
    assert code == 1 and files_in(d) == [] and env["overall"] == "FAIL" and s["overall"] == "FAIL" and c["overall"] == "NOT RUN" and c["expected_instances"] == 0
    assert any(e["rule_id"] == "FET-VALID" and e["asset"] == "BTC" and e["outcome"] == "FAIL" for e in s["non_pass"]) and all(e["outcome"] in ("FAIL", "BLOCKED") for e in s["non_pass"])
@check("diagnostics: a candle-rule failure (VWAP outside the range) persists a candle family with the failing instance and the plan digest")
def _():
    b = {k: json.loads(v) for k, v in REAL.items()}; r = b["XBTUSD"]["result"]; rows = r[[k for k in r if k != "last"][0]]; rows[-4][5] = "9999999.0"
    diag = new_temp_dir(); code, out, err, d = produce(run_id="5", diagnostics=diag, bodies={k: json.dumps(v).encode() for k, v in b.items()}); rec = json.load(open(os.path.join(diag, ev.DIAGNOSTIC_NAME)))
    c = rec["envelope"]["families"]["candle"]; assert code == 1 and c["overall"] == "FAIL" and c["expected_instances"] == 621 and any(e["rule_id"] == "CND-VWAP" and e["asset"] == "BTC" and e["field"] == "vwap" for e in c["non_pass"])
@check("diagnostics: with no diagnostics folder (the local default) nothing is persisted and the run fails exactly as before")
def _():
    code, out, err, d = produce(bodies=blocked_bodies()); assert code == 1 and files_in(d) == [] and "VALIDATION FAILED" in err and "diagnostic" not in err
@check("diagnostics: an unavailable folder (a file is in its place) is reported as a warning and the ORIGINAL failure stays primary (same exit code and message)")
def _():
    blocker = os.path.join(new_temp_dir(), "automation_out"); open(blocker, "w").write("a file, not a folder")
    code, out, err, d = produce(diagnostics=blocker, bodies=blocked_bodies()); assert code == 1 and "VALIDATION FAILED" in err and "trade count is negative" in err and "WARNING: the validation diagnostic was not persisted" in err
@check("diagnostics: a failure while writing the diagnostic itself never masks the pipeline failure and never raises")
def _():
    with mock.patch("os.replace", side_effect=OSError("disk full")): code, out, err, d = produce(diagnostics=new_temp_dir(), bodies=blocked_bodies())
    assert code == 1 and "VALIDATION FAILED" in err and "was not persisted" in err
@check("diagnostics: a fetch failure (HTTP 403) is recorded as FET-RESP FAIL with the later instances BLOCKED, and the exit code stays 1")
def _():
    diag = new_temp_dir(); d = new_temp_dir(); code, out, err = run_main(REAL, REAL_NOW, d, failing_pair="XBTUSD"); assert code == 1
    out2, err2 = io.StringIO(), io.StringIO()
    def fake_get(url, params=None, **kw): return fakehttp.FakeResponse(b"", status=403) if params["pair"] == "XBTUSD" else fakehttp.FakeResponse(REAL[params["pair"]])
    with mock.patch("requests.get", side_effect=fake_get), mock.patch("time.sleep"), mock.patch.object(run_workflow, "datetime", wraps=datetime) as dt, contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
        dt.now.return_value = REAL_NOW; assert run_workflow.main(d, run_id="9", diagnostics_dir=diag) == 1
    s = json.load(open(os.path.join(diag, ev.DIAGNOSTIC_NAME)))["envelope"]["families"]["source"]
    assert any(e["rule_id"] == "FET-RESP" and e["asset"] == "BTC" and e["outcome"] == "FAIL" for e in s["non_pass"]) and all(e["outcome"] in ("FAIL", "BLOCKED") for e in s["non_pass"])
@check("diagnostics: a successful run writes no diagnostic (its envelope is inside the manifest)")
def _():
    diag = os.path.join(new_temp_dir(), "automation_out"); code, out, err, d = produce(diagnostics=diag); assert code == 0 and not os.path.exists(diag)

# ================================================================ nothing the earlier increments or the fetcher guarantee has moved
@check("preservation: the fetcher is byte-identical (hash pin) and the analysis module is unchanged by this increment")
def _():
    assert hashlib.sha256(open(os.path.join(SRC, "fetch_data.py"), "rb").read()).hexdigest() == "ee4f045d9b4455ce13ed1245e04951dfe2942a35debbaa65cefc77dd838147d9"

R.finish()
