"""Regression tests for the file-replacement stage of run_workflow. Written BEFORE the fix.

Issue under test: deleting the old copies (.bak) AFTER both new files are already in place must not turn a finished run into
exit code 1. Also pins what happens when replacing a file fails, and when the restore itself fails (what is and is not guaranteed)."""
import pathlib
from wf_support import *

R = Runner("file replacement checks")
check = R.register
cleanup = []
REPORT, CSV = "report_2026-03-19.md", "report_2026-03-19_data.csv"
OLD = {REPORT: "OLD REPORT ✓\n", CSV: "old,csv\n"}
real_replace, real_unlink = os.replace, pathlib.Path.unlink
LAST = {"replace_calls": 0}   # number of os.replace calls made by the most recent run()

def folder(existing=(REPORT, CSV)):
    d = new_temp_dir(); cleanup.append(d)
    for name in existing: open(os.path.join(d, name), "w", encoding="utf-8", newline="").write(OLD[name])
    return d

def run(d, fail_replace_calls=(), fail_unlink_of=()):
    """Run main() on synthetic data. Replace calls are numbered from 1; unlink fails (PermissionError) for the named files."""
    bodies, *_ = synthetic_bodies(); calls = {"n": 0}
    def flaky_replace(source, destination):
        calls["n"] += 1; LAST["replace_calls"] = calls["n"]
        if calls["n"] in fail_replace_calls: raise OSError(f"simulated failure of replace call {calls['n']}")
        return real_replace(source, destination)
    def flaky_unlink(self, missing_ok=False):
        if self.name in fail_unlink_of and self.exists(): raise PermissionError(f"simulated: {self.name} is locked")
        return real_unlink(self, missing_ok=missing_ok)
    with mock.patch("os.replace", side_effect=flaky_replace), mock.patch.object(pathlib.Path, "unlink", flaky_unlink):
        return run_main(bodies, NOW, d)

def is_new(d, name):
    path = os.path.join(d, name)
    return os.path.exists(path) and read(d, name).startswith({REPORT: "# Bitcoin vs Ethereum", CSV: "date,btc_close,eth_close"}[name])
def restored(d, existing):
    """Exactly the old files exist again, byte-identical; no .tmp and no .bak is left."""
    return files_in(d) == sorted(existing) and all(read(d, name) == OLD[name] for name in existing)
def all_old_bytes_survive(d, existing):
    """Whatever else went wrong, every old file's content must still exist somewhere in the folder (as itself or as its .bak)."""
    contents = [read(d, f) for f in files_in(d) if not f.endswith(".tmp")]
    return all(OLD[name] in contents for name in existing)

# ---------------------------------------------------------------- the issue: backup cleanup fails after the new files are in place
@check("cleanup fails (report .bak locked): exit 0, BOTH new files are in place, success message printed")
def _():
    d = folder(); code, out, err = run(d, fail_unlink_of={REPORT + ".bak"})
    assert code == 0 and is_new(d, REPORT) and is_new(d, CSV) and "Report written" in out and "Data written" in out, (code, err[:200], files_in(d))
@check("cleanup fails: a warning (not an error) on stderr names the leftover backup file")
def _():
    d = folder(); code, out, err = run(d, fail_unlink_of={REPORT + ".bak"})
    assert "WARNING" in err and REPORT + ".bak" in err and "ERROR" not in err, err[:300]
@check("cleanup fails: the leftover .bak still holds the OLD report byte-for-byte (nothing is lost)")
def _():
    d = folder(); run(d, fail_unlink_of={REPORT + ".bak"}); assert read(d, REPORT + ".bak") == OLD[REPORT]
@check("cleanup fails for the FIRST backup only: the second backup is still deleted (cleanup continues), no .tmp left")
def _():
    d = folder(); run(d, fail_unlink_of={REPORT + ".bak"}); assert files_in(d) == sorted([REPORT, REPORT + ".bak", CSV]), files_in(d)
@check("cleanup fails for the SECOND backup only: exit 0, both new files in place, the warning names the CSV backup")
def _():
    d = folder(); code, out, err = run(d, fail_unlink_of={CSV + ".bak"})
    assert code == 0 and is_new(d, REPORT) and is_new(d, CSV) and CSV + ".bak" in err and REPORT + ".bak" not in err and files_in(d) == sorted([REPORT, CSV, CSV + ".bak"]), (code, err[:200], files_in(d))
@check("cleanup fails for BOTH backups: exit 0, both new files in place, one warning per leftover, old bytes kept")
def _():
    d = folder(); code, out, err = run(d, fail_unlink_of={REPORT + ".bak", CSV + ".bak"})
    assert code == 0 and is_new(d, REPORT) and is_new(d, CSV) and err.count("WARNING") == 2 and read(d, REPORT + ".bak") == OLD[REPORT] and read(d, CSV + ".bak") == OLD[CSV], (code, err[:300], files_in(d))
@check("cleanup fails: the new files were NOT rolled back (no replace call after the second new file was placed)")
def _():
    d = folder(); bodies, *_ = synthetic_bodies(); calls = []
    def spy(source, destination): calls.append((os.path.basename(source), os.path.basename(destination))); return real_replace(source, destination)
    def flaky_unlink(self, missing_ok=False):
        if self.name.endswith(".bak") and self.exists(): raise PermissionError("locked")
        return real_unlink(self, missing_ok=missing_ok)
    with mock.patch("os.replace", side_effect=spy), mock.patch.object(pathlib.Path, "unlink", flaky_unlink): code = run_main(bodies, NOW, d)[0]
    assert code == 0 and len(calls) == 4 and calls[-1] == (CSV + ".tmp", CSV), calls
@check("cleanup fails once, the next normal run still works: exit 0, stale .bak overwritten and removed, only the two files remain")
def _():
    d = folder(); run(d, fail_unlink_of={REPORT + ".bak"}); code, out, err = run(d)
    assert code == 0 and files_in(d) == sorted([REPORT, CSV]) and is_new(d, REPORT) and is_new(d, CSV), (code, err[:200], files_in(d))
@check("normal success with old files present: exit 0, no warning, only the two files remain")
def _():
    d = folder(); code, out, err = run(d); assert code == 0 and "WARNING" not in err and files_in(d) == sorted([REPORT, CSV]) and is_new(d, REPORT) and is_new(d, CSV)
@check("normal success on an empty folder: exit 0, no warning, nothing to clean up")
def _():
    d = folder(existing=()); code, out, err = run(d); assert code == 0 and "WARNING" not in err and files_in(d) == sorted([REPORT, CSV])

# ---------------------------------------------------------------- failure while replacing either output -> everything is put back
CASES = (  # (old files that exist, replace call that fails, what that call does)
    ((REPORT, CSV), 1, "old report -> .bak"), ((REPORT, CSV), 2, "new report in place"), ((REPORT, CSV), 3, "old CSV -> .bak"), ((REPORT, CSV), 4, "new CSV in place"),
    ((REPORT,), 1, "old report -> .bak"), ((REPORT,), 2, "new report in place"), ((REPORT,), 3, "new CSV in place (no old CSV)"),
    ((CSV,), 1, "new report in place (no old report)"), ((CSV,), 2, "old CSV -> .bak"), ((CSV,), 3, "new CSV in place"),
    ((), 1, "new report in place (no old files)"), ((), 2, "new CSV in place (no old files)"),
)
for existing, failing_call, what in CASES:
    @check(f"replace fails: old files {[n[-8:] for n in existing] or 'none'}, call {failing_call} ({what}) -> exit 1, ERROR, old state exactly restored, no .tmp/.bak")
    def _(existing=existing, failing_call=failing_call):
        d = folder(existing); code, out, err = run(d, fail_replace_calls={failing_call})
        assert code == 1 and "ERROR" in err and "Report written" not in out and restored(d, existing), (code, err[:160], files_in(d))

# ---------------------------------------------------------------- the restore itself fails: what is still guaranteed
for fail_set, what in (({4, 5}, "new CSV cannot be placed, then the CSV restore fails"), ({4, 6}, "new CSV cannot be placed, then the report restore fails"),
                       ({4, 5, 6}, "new CSV cannot be placed, then BOTH restores fail"), ({2, 3}, "new report cannot be placed, then its restore fails")):
    @check(f"restore fails ({what}): exit 1 with an ERROR (no crash), no .tmp left, and every old file's bytes still exist on disk")
    def _(fail_set=fail_set):
        d = folder(); code, out, err = run(d, fail_replace_calls=fail_set)
        assert code == 1 and "ERROR" in err and "Traceback" not in err and not any(f.endswith(".tmp") for f in files_in(d)) and all_old_bytes_survive(d, (REPORT, CSV)), (code, err[:160], files_in(d))

# ---------------------------------------------------------------- D1 + D2: a failed restore must not stop the other restores, and every stuck path is reported
@check("D1: new CSV cannot be placed (call 4) and the CSV restore fails (call 5): the report restore is STILL attempted (6 calls) and the old report is back")
def _():
    d = folder(); code, out, err = run(d, fail_replace_calls={4, 5})
    assert code == 1 and LAST["replace_calls"] == 6 and read(d, REPORT) == OLD[REPORT] and not os.path.exists(os.path.join(d, CSV)) and read(d, CSV + ".bak") == OLD[CSV], (code, LAST, files_in(d))
@check("D2: the report restore fails (call 6) but the CSV restore worked: exit 1, CSV back, and the message names the report's .bak but not the CSV's")
def _():
    d = folder(); code, out, err = run(d, fail_replace_calls={4, 6})
    assert code == 1 and read(d, CSV) == OLD[CSV] and read(d, REPORT + ".bak") == OLD[REPORT] and REPORT + ".bak" in err and CSV + ".bak" not in err, (code, err[:300], files_in(d))
@check("D2: both restores fail: the message names BOTH .bak files and still contains the original replacement error")
def _():
    d = folder(); code, out, err = run(d, fail_replace_calls={4, 5, 6})
    assert code == 1 and REPORT + ".bak" in err and CSV + ".bak" in err and "simulated failure of replace call 4" in err, err[:400]
@check("D1: old CSV only; new CSV cannot be placed (call 3) and the CSV restore fails (call 4): the new report (no backup) is still removed, old CSV kept as .bak, path reported")
def _():
    d = folder(existing=(CSV,)); code, out, err = run(d, fail_replace_calls={3, 4})
    assert code == 1 and files_in(d) == [CSV + ".bak"] and read(d, CSV + ".bak") == OLD[CSV] and CSV + ".bak" in err, (code, err[:300], files_in(d))
@check("when every restore works the message stays the plain original error (no 'also failed' wording) and all old files are back")
def _():
    d = folder(); code, out, err = run(d, fail_replace_calls={4})
    assert code == 1 and "simulated failure of replace call 4" in err and "also failed" not in err and restored(d, (REPORT, CSV)), (err[:300], files_in(d))

# ---------------------------------------------------------------- the documented guarantee must not claim more than the code can do
SRC_DIR = os.environ.get("WORKFLOW_SRC", SRC)
readme = lambda: open(os.path.join(SRC_DIR, "..", "README.md"), encoding="utf-8").read()
code_text = lambda: open(os.path.join(SRC_DIR, "run_workflow.py"), encoding="utf-8").read()
@check("README: exit codes are described without the unconditional promise 'no report was changed', and it mentions the .bak copies")
def _(): text = readme(); assert "no report was changed" not in text and ".bak" in text
@check("run_workflow docstring: exit codes are described without the unconditional promise 'no report was changed', and it mentions *.bak")
def _(): head = code_text().split('"""')[1]; assert "no report was changed" not in head and ".bak" in head, head

for d in cleanup: shutil.rmtree(d, ignore_errors=True)
R.finish()
