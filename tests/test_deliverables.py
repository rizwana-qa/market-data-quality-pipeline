"""Stage 1 tests for (A) the stale-output cleanup step and (B) the stage-deliverables command of the daily workflow. Written BEFORE either exists, so every check here that depends on them fails with
'MISSING FEATURE' until Stage 2; nothing here changes production code.

(A) The cleanup is specified as a step of the workflow (id clear_stale_outputs) whose script removes ONLY reports/report_*.md, reports/report_*_data.csv and reports/dashboard_*.html from the runner's workspace.
    The tests extract that script from the workflow file and run it with bash (-eo pipefail, as GitHub does) in a temporary workspace - but only after a static check proved that it is narrow (plain 'rm -f' of
    exactly those three patterns), so a broad script is never executed.
(B) The command:  daily_automation.py stage-deliverables --reports-dir reports --dashboard-dir dashboard_out --out-dir deliverables --date <analysis date>
    Contract specified here: it validates the report, the CSV and the dashboard of that date, and only if all three are consistent copies EXACTLY those three files (byte for byte) into a new --out-dir. Any problem:
    exit code 1, 'ERROR: <reason>' on stderr, no output folder, no temporary file, the sources untouched. It never reads a secret.
Offline and deterministic: the saved Kraken replies (current run, 2026-10-26) and synthetic replies (an older run, 2026-03-19) are processed by the real project code into temporary folders."""
import atexit, contextlib, hashlib, io, json, os, re, shutil, stat, subprocess, sys, tempfile
from datetime import date, datetime, timezone

from wf_support import Runner, REPO, REAL, REAL_NOW, NOW, synthetic_bodies, run_main, files_in, load

dashboard, da = load("dashboard"), load("daily_automation")
R = Runner("deliverables checks")
WORK = tempfile.mkdtemp(prefix="dv_")
def _remove(function, path, error):
    os.chmod(path, 0o700); function(path)
atexit.register(shutil.rmtree, WORK, False, _remove)
TEXT = open(os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml"), encoding="utf-8", newline="").read()
GENERATED_AT = datetime(2026, 10, 27, 9, 45, tzinfo=timezone.utc)
CURRENT, HISTORICAL = "2026-10-26", "2026-03-19"
NAMES = lambda day: [f"report_{day}.md", f"report_{day}_data.csv", f"dashboard_{day}.html"]

# ================================================================ (A) the cleanup step
PATTERNS = ["reports/report_*.md", "reports/report_*_data.csv", "reports/dashboard_*.html"]
GENERATED = ["reports/report_2026-10-25.md", "reports/report_2026-10-25_data.csv", "reports/dashboard_2026-10-25.html", "reports/report_2026-10-26.md", "reports/report_2026-10-26_data.csv", "reports/dashboard_2026-10-26.html",
             "reports/report_2026-03-19.md", "reports/report_2026-03-19_data.csv"]
UNRELATED_IN_REPORTS = ["reports/notes.md", "reports/report_notes.txt", "reports/report_2026-10-26.md.bak", "reports/report_2026-10-26.md.tmp", "reports/report_2026-10-26_data.csv.bak", "reports/dashboard_2026-10-26.html.tmp",
                        "reports/dashboard.html", "reports/dashboard_2026-10-26.txt", "reports/archive/report_2026-10-20.md", "reports/archive/report_2026-10-20_data.csv", "reports/archive/dashboard_2026-10-20.html"]
OUTSIDE_REPORTS = ["report_2026-10-26.md", "report_2026-10-26_data.csv", "dashboard_2026-10-26.html", "dashboard_out/dashboard_2026-10-26.html", "deliverables/report_2026-10-26.md", "automation_out/upsert_analysis.json",
                   "src/dashboard.py", "src/report_enhanced.py", "tests/test_example.py", "README.md", "AI_LOG.md", ".github/workflows/daily-market-analysis.yml", ".git/HEAD", ".git/refs/heads/main", "docs/report_2026-10-26.md"]

def step_script(step_id):
    for block in re.split(r"\n(?=      - name: )", TEXT.split("    steps:\n", 1)[1]):
        if re.search(rf"^        id: {step_id}\s*$", block, re.M):
            found = re.search(r"^        run: \|\n((?:          .*\n?)+)", block, re.M); assert found, f"step {step_id} has no literal run block"
            return "\n".join(line[10:] for line in found.group(1).rstrip("\n").split("\n"))
    raise AssertionError(f"MISSING FEATURE: the workflow has no step with id {step_id!r}")

def narrow_cleanup_script():
    """The script of the cleanup step, returned only if it is plain 'rm -f' of exactly the three patterns."""
    script = step_script("clear_stale_outputs"); commands = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert commands, "the cleanup script is empty"
    for line in commands:
        tokens = line.split(); assert tokens[:2] == ["rm", "-f"], f"only 'rm -f' is allowed, found: {line!r}"
        arguments = [t for t in tokens[2:] if t != "--"]; assert arguments and all(a in PATTERNS for a in arguments), f"arguments other than the three patterns: {line!r}"
    assert {a for line in commands for a in line.split() if a in PATTERNS} == set(PATTERNS), "all three patterns must be cleaned"
    code_only = "\n".join(commands)                                          # comments may say what they like; only the commands are judged
    assert not re.search(r"(^|\s)-[A-Za-z]*[rR]", code_only) and "--recursive" not in code_only, "recursive deletion"
    for forbidden in ("find ", "git ", "xargs", "sudo", "$", "`", "..", "~", "|", ";", "&", ">", "<", "\\", "/*", " * "): assert forbidden not in code_only, f"forbidden in the cleanup script: {forbidden!r}"
    return script

def find_bash():
    found = shutil.which("bash")
    if found and "system32" not in found.lower(): return found
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.exists(candidate): return candidate
    raise AssertionError("bash was not found, so the cleanup script cannot be executed here")
def run_script(script, workspace):
    done = subprocess.run([find_bash(), "--noprofile", "--norc", "-eo", "pipefail", "-s"], input=script, text=True, cwd=workspace, capture_output=True, timeout=60)
    return done.returncode, done.stdout + done.stderr
def make_tree(root, names):
    for name in names:
        path = os.path.join(root, *name.split("/")); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w", encoding="utf-8", newline="\n").write(name + "\n")
def listing(root): return sorted(os.path.relpath(os.path.join(folder, f), root).replace(os.sep, "/") for folder, _dirs, files in os.walk(root) for f in files)
def content_ok(root, names): return all(open(os.path.join(root, *n.split("/")), encoding="utf-8").read() == n + "\n" for n in names)

@R.register("cleanup script is narrow: only 'rm -f' of exactly reports/report_*.md, reports/report_*_data.csv and reports/dashboard_*.html - no recursion, find, git, variables, parent paths or other commands")
def _():
    narrow_cleanup_script()

@R.register("cleanup removes the generated report, CSV and dashboard files of every date from reports/ - and nothing else is removed")
def _():
    ws = tempfile.mkdtemp(dir=WORK); keep = UNRELATED_IN_REPORTS + OUTSIDE_REPORTS; make_tree(ws, GENERATED + keep); script = narrow_cleanup_script()
    code, text = run_script(script, ws); assert code == 0, text
    assert listing(ws) == sorted(keep), (sorted(set(listing(ws)) ^ set(keep)))

@R.register("cleanup leaves unrelated files in reports/ untouched: notes, .bak/.tmp copies, other extensions, near-miss names and nested folders (the patterns are not recursive)")
def _():
    ws = tempfile.mkdtemp(dir=WORK); make_tree(ws, GENERATED + UNRELATED_IN_REPORTS); script = narrow_cleanup_script()
    code, text = run_script(script, ws); assert code == 0, text
    assert content_ok(ws, UNRELATED_IN_REPORTS) and listing(ws) == sorted(UNRELATED_IN_REPORTS)

@R.register("cleanup leaves everything outside reports/ untouched: look-alikes in the workspace root and in docs/, dashboard_out, deliverables, automation_out, src, tests, README, workflow file")
def _():
    ws = tempfile.mkdtemp(dir=WORK); make_tree(ws, GENERATED + OUTSIDE_REPORTS); script = narrow_cleanup_script()
    code, text = run_script(script, ws); assert code == 0, text
    assert content_ok(ws, OUTSIDE_REPORTS) and listing(ws) == sorted(OUTSIDE_REPORTS)

@R.register("cleanup handles a missing or empty reports/ folder: exit 0, nothing created, nothing else touched; running it twice is harmless")
def _():
    missing = tempfile.mkdtemp(dir=WORK); make_tree(missing, OUTSIDE_REPORTS); empty = tempfile.mkdtemp(dir=WORK); os.makedirs(os.path.join(empty, "reports")); make_tree(empty, OUTSIDE_REPORTS); script = narrow_cleanup_script()
    code, text = run_script(script, missing); assert code == 0 and not os.path.exists(os.path.join(missing, "reports")) and listing(missing) == sorted(OUTSIDE_REPORTS), text
    for _attempt in range(2):
        code, text = run_script(script, empty); assert code == 0 and os.path.isdir(os.path.join(empty, "reports")) and listing(empty) == sorted(OUTSIDE_REPORTS), text

GIT_CONFIG = os.path.join(WORK, "empty.gitconfig"); open(GIT_CONFIG, "w").close()
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=GIT_CONFIG, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", HOME=WORK, USERPROFILE=WORK, XDG_CONFIG_HOME=WORK)
GIT_ENV = {k: v for k, v in GIT_ENV.items() if not k.startswith("GITHUB_")}
def git(args, cwd):
    done = subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr.strip()[:200]}"; return done.stdout

@R.register("cleanup never rewrites Git: in a real throw-away repository HEAD, the index and the committed content are unchanged - only the working copies of the three generated files disappear")
def _():
    repo = tempfile.mkdtemp(dir=WORK); committed = ["reports/report_2026-10-27.md", "reports/report_2026-10-27_data.csv", "reports/dashboard_2026-10-27.html", "src/dashboard.py", "README.md"]
    make_tree(repo, committed); git(["init", "-q"], repo); git(["add", "-A"], repo); git(["commit", "-q", "-m", "committed historical files"], repo)
    head, index = git(["rev-parse", "HEAD"], repo), git(["ls-files"], repo); script = narrow_cleanup_script()
    code, text = run_script(script, repo); assert code == 0, text
    assert git(["rev-parse", "HEAD"], repo) == head and git(["ls-files"], repo) == index, "history or index changed"
    assert sorted(git(["status", "--porcelain"], repo).splitlines()) == sorted(f" D {n}" for n in committed[:3]), git(["status", "--porcelain"], repo)
    for name in committed: assert git(["show", f"HEAD:{name}"], repo) == name + "\n", f"committed content of {name} changed"
    assert [n for n in listing(repo) if not n.startswith(".git/")] == sorted(committed[3:]), "other working files changed"

# ================================================================ (B) the stage-deliverables command
_gold = {}
def gold():
    if not _gold:
        base = tempfile.mkdtemp(dir=WORK)
        for label, (bodies, now) in {"current": (REAL, REAL_NOW), "historical": (synthetic_bodies()[0], NOW)}.items():
            reports, out = os.path.join(base, label, "reports"), os.path.join(base, label, "dashboard_out")
            code, _stdout, error = run_main(bodies, now, reports); assert code == 0, f"the fixture run for {label} failed: {error[-200:]}"
            os.makedirs(out); dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=out)
        _gold["base"] = base
    return _gold["base"]
def workspace(with_historical=False):
    ws = tempfile.mkdtemp(dir=WORK)
    for part in ("reports", "dashboard_out"):
        shutil.copytree(os.path.join(gold(), "current", part), os.path.join(ws, part))
        if with_historical: shutil.copytree(os.path.join(gold(), "historical", part), os.path.join(ws, part), dirs_exist_ok=True)
    return ws
def paths(ws): return os.path.join(ws, "reports"), os.path.join(ws, "dashboard_out"), os.path.join(ws, "deliverables")
def digest(path): return hashlib.sha256(open(path, "rb").read()).hexdigest()
def snapshot(*folders): return {os.path.join(folder, name): digest(os.path.join(folder, name)) for top in folders if os.path.isdir(top) for folder, _d, names in os.walk(top) for name in names}
def edit(path, change):
    text = open(path, encoding="utf-8", newline="").read(); new = change(text); assert new != text, "the edit changed nothing (the test would prove nothing)"
    open(path, "w", encoding="utf-8", newline="").write(new)

def stage(ws, day=CURRENT, extra_env=None, out=None):
    """Run the command the way the workflow will. Returns (exit code, stdout + stderr). A 'no such command' answer is reported as the missing feature it is."""
    reports, dash, default_out = paths(ws); out = out or default_out; buffer = io.StringIO()
    argv = ["stage-deliverables", "--reports-dir", reports, "--dashboard-dir", dash, "--out-dir", out, "--date", day]
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = da.main(argv, env=dict(extra_env or {}), now=date(2026, 10, 27))
    text = buffer.getvalue()
    if code == 2 and "invalid choice" in text: raise AssertionError("MISSING FEATURE: daily_automation.py has no stage-deliverables command yet (argparse: invalid choice)")
    return code, text
def rejected(ws, mention, day=CURRENT):
    """The command must refuse (exit 1) and name the reason; no output folder, no stray files, the sources byte-identical."""
    reports, dash, out = paths(ws); before = snapshot(reports, dash); code, text = stage(ws, day)
    assert code == 1, f"expected exit 1 (rejected), got {code}: {text.strip()[-160:]}"
    for needle in mention: assert needle.lower() in text.lower(), f"the message does not mention {needle!r}: {text.strip()[-200:]}"
    assert snapshot(reports, dash) == before, "a rejected staging changed its sources"
    assert not os.path.exists(out), f"a partial deliverables folder was left behind: {files_in(out)}"
    assert set(os.listdir(ws)) <= {"reports", "dashboard_out"}, f"stray entries after a rejection: {sorted(os.listdir(ws))}"

def each_rejected(cases):
    """cases: (label, workspace, words the message must contain, analysis date). Every workspace is ARRANGED first by the caller, so a broken arrangement fails loudly even while the command does not exist yet."""
    for label, ws, mention, day in cases:
        try: rejected(ws, mention, day)
        except AssertionError as error:
            if str(error).startswith("MISSING FEATURE"): raise
            raise AssertionError(f"{label}: {error}")

@R.register("the command exists and documents its four options (--reports-dir, --dashboard-dir, --out-dir, --date)")
def _():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer): code = da.main(["stage-deliverables", "--help"], env={})
    text = buffer.getvalue()
    if code == 2 and "invalid choice" in text: raise AssertionError("MISSING FEATURE: daily_automation.py has no stage-deliverables command yet (argparse: invalid choice)")
    assert code == 0 and all(option in text for option in ("--reports-dir", "--dashboard-dir", "--out-dir", "--date")), text[-300:]

@R.register("success: exactly the three files of the analysis date are staged - report_<date>.md, report_<date>_data.csv, dashboard_<date>.html - byte-identical to their sources; the command creates --out-dir itself")
def _():
    ws = workspace(); reports, dash, out = paths(ws); assert not os.path.exists(out)
    code, text = stage(ws); assert code == 0, text.strip()[-300:]
    assert files_in(out) == sorted(NAMES(CURRENT)) and CURRENT in text, (files_in(out), text.strip()[-200:])
    for name in NAMES(CURRENT): assert digest(os.path.join(out, name)) == digest(os.path.join(dash if name.startswith("dashboard") else reports, name)), f"{name} differs from its source"

@R.register("success does not alter its sources: every file in the reports folder and the dashboard folder is byte-identical afterwards")
def _():
    ws = workspace(with_historical=True); reports, dash, out = paths(ws); before = snapshot(reports, dash)
    code, text = stage(ws); assert code == 0, text.strip()[-300:]
    assert snapshot(reports, dash) == before

@R.register("the staging folder holds no unrelated file even when the sources are full of them: historical pairs, backups, notes, a committed historical dashboard in the reports folder")
def _():
    ws = workspace(with_historical=True); reports, dash, out = paths(ws)
    for name, body in (("notes.md", "n"), (f"report_{CURRENT}.md.bak", "b"), (f"report_{CURRENT}_data.csv.tmp", "t"), ("dashboard_2026-10-20.html", "old")): open(os.path.join(reports, name), "w").write(body)
    open(os.path.join(dash, "readme.txt"), "w").write("x")
    code, text = stage(ws); assert code == 0, text.strip()[-300:]
    assert files_in(out) == sorted(NAMES(CURRENT)), files_in(out)

@R.register("the dashboard is taken ONLY from --dashboard-dir: a committed dashboard of the same date in the reports folder does not stand in for a missing generated one")
def _():
    ws = workspace(); reports, dash, out = paths(ws); shutil.copy(os.path.join(dash, f"dashboard_{CURRENT}.html"), os.path.join(reports, f"dashboard_{CURRENT}.html")); os.remove(os.path.join(dash, f"dashboard_{CURRENT}.html"))
    rejected(ws, [f"dashboard_{CURRENT}.html"])

@R.register("an existing non-empty --out-dir is refused: a previous or foreign set is never mixed with a new one and is left exactly as it was")
def _():
    ws = workspace(); reports, dash, out = paths(ws); os.makedirs(out); open(os.path.join(out, "old_report.md"), "w").write("previous run\n"); before = snapshot(out)
    code, text = stage(ws); assert code == 1 and "out" in text.lower(), f"expected exit 1, got {code}: {text.strip()[-200:]}"
    assert snapshot(out) == before and files_in(out) == ["old_report.md"]

@R.register("a missing report is rejected (names report_<date>.md): nothing staged")
def _():
    ws = workspace(); os.remove(os.path.join(paths(ws)[0], f"report_{CURRENT}.md")); rejected(ws, [f"report_{CURRENT}.md"])

@R.register("a missing CSV is rejected (names report_<date>_data.csv): nothing staged")
def _():
    ws = workspace(); os.remove(os.path.join(paths(ws)[0], f"report_{CURRENT}_data.csv")); rejected(ws, [f"report_{CURRENT}_data.csv"])

@R.register("a missing dashboard is rejected (names dashboard_<date>.html): nothing staged, although the report and CSV are valid")
def _():
    ws = workspace(); os.remove(os.path.join(paths(ws)[1], f"dashboard_{CURRENT}.html")); rejected(ws, [f"dashboard_{CURRENT}.html"])

@R.register("mismatched dates are rejected: any one of the three files named for another day than the other two - and the analysis date given on the command line must match all three")
def _():
    cases = []
    for victim, folder_index in ((f"report_{CURRENT}.md", 0), (f"report_{CURRENT}_data.csv", 0), (f"dashboard_{CURRENT}.html", 1)):
        ws = workspace(); folder = paths(ws)[folder_index]; os.rename(os.path.join(folder, victim), os.path.join(folder, victim.replace(CURRENT, "2026-10-25"))); cases.append((f"{victim} renamed to another day", ws, [victim], CURRENT))
    cases += [(f"--date {day}", workspace(), [day], day) for day in ("2026-10-25", "2026-10-27")]
    each_rejected(cases)

@R.register("an invalid analysis date argument is rejected before anything is read: empty, impossible, wrong format, padded, not a date")
def _():
    each_rejected([(f"--date {day!r}", workspace(), ["date"], day) for day in ("", "2026-13-01", "2026-02-30", "26-10-26", "2026-10-26 ", "yesterday", "2026-10-26/../x")])

@R.register("an invalid CSV is rejected even though the dashboard built from the valid one is fine: wrong header, text instead of a number, a gap in the dates, too few rows, last row not on the analysis date")
def _():
    lines_of = lambda text: text.split("\n")
    breaks = {"wrong header": lambda t: t.replace("btc_close", "btc_closing", 1), "text instead of a number": lambda t: t.rstrip("\n").rsplit("\n", 1)[0] + "\n" + ",".join(["2026-10-26", "abc"] + t.rstrip("\n").split("\n")[-1].split(",")[2:]) + "\n",
              "gap in the dates": lambda t: "\n".join(lines_of(t)[:10] + lines_of(t)[11:]), "too few rows": lambda t: "\n".join(lines_of(t)[:6]) + "\n", "last row on another day": lambda t: t.replace("\n2026-10-26,", "\n2026-10-27,")}
    cases = []
    for label, change in breaks.items():
        ws = workspace(); edit(os.path.join(paths(ws)[0], f"report_{CURRENT}_data.csv"), change); cases.append((label, ws, ["csv"], CURRENT))
    each_rejected(cases)

@R.register("stale or historical input is rejected: only the historical report, CSV and dashboard present while the validated date is the current run's day (nothing of the historical set is staged)")
def _():
    ws = tempfile.mkdtemp(dir=WORK)
    for part in ("reports", "dashboard_out"): shutil.copytree(os.path.join(gold(), "historical", part), os.path.join(ws, part))
    rejected(ws, [CURRENT])

@R.register("a historical set renamed to the current date is rejected: the old CSV's rows, the old report's figures and the old dashboard's data all disagree with the analysis date")
def _():
    ws = workspace(); reports, dash, out = paths(ws)
    for suffix in (".md", "_data.csv"): shutil.copy(os.path.join(gold(), "historical", "reports", f"report_{HISTORICAL}{suffix}"), os.path.join(reports, f"report_{CURRENT}{suffix}"))
    shutil.copy(os.path.join(gold(), "historical", "dashboard_out", f"dashboard_{HISTORICAL}.html"), os.path.join(dash, f"dashboard_{CURRENT}.html"))
    rejected(ws, [CURRENT])

@R.register("incorrect dashboard references are rejected: wrong CSV name, wrong report name, wrong embedded analysis date, another run's dashboard under the current name, an empty file, a file that is not a dashboard")
def _():
    fixes = {"wrong CSV name": lambda t: t.replace(f'"csv_file":"report_{CURRENT}_data.csv"', '"csv_file":"report_2026-10-25_data.csv"'), "wrong report name": lambda t: t.replace(f'"report_file":"report_{CURRENT}.md"', '"report_file":"report_2026-10-25.md"'),
             "wrong embedded date": lambda t: re.sub(r'("close_period":\{"start":"[^"]+","end":")' + CURRENT, r"\g<1>2026-10-25", t), "not a dashboard": lambda t: "<html><body>not a dashboard</body></html>\n", "empty file": lambda t: ""}
    cases = []
    for label, change in fixes.items():
        ws = workspace(); edit(os.path.join(paths(ws)[1], f"dashboard_{CURRENT}.html"), change); cases.append((label, ws, ["dashboard"], CURRENT))
    ws = workspace(); shutil.copy(os.path.join(gold(), "historical", "dashboard_out", f"dashboard_{HISTORICAL}.html"), os.path.join(paths(ws)[1], f"dashboard_{CURRENT}.html")); cases.append(("another run's dashboard", ws, ["dashboard"], CURRENT))
    each_rejected(cases)

@R.register("incorrect report references are rejected: another run's report under the current name, a changed figure, and a missing, invalid or out-of-date 'Data fetched' timestamp")
def _():
    ws = workspace(); shutil.copy(os.path.join(gold(), "historical", "reports", f"report_{HISTORICAL}.md"), os.path.join(paths(ws)[0], f"report_{CURRENT}.md")); cases = [("another run's report", ws, ["report"], CURRENT)]
    changes = {"changed figure": lambda t: re.sub(r"(Pearson correlation of the 30 aligned daily returns: )[-0-9.]+", r"\g<1>0.123456", t), "no timestamp": lambda t: re.sub(r"^- Data fetched: .*\n", "", t, flags=re.M),
               "invalid timestamp": lambda t: re.sub(r"^- Data fetched: .*$", "- Data fetched: yesterday UTC", t, flags=re.M), "out-of-date timestamp": lambda t: re.sub(r"^- Data fetched: .*$", "- Data fetched: 2026-10-20 09:41:00 UTC", t, flags=re.M)}
    for label, change in changes.items():
        ws = workspace(); edit(os.path.join(paths(ws)[0], f"report_{CURRENT}.md"), change); cases.append((label, ws, ["report"], CURRENT))
    each_rejected(cases)

@R.register("a failed staging attempt leaves no partial deliverables set: with a valid report and CSV but an invalid dashboard, none of the valid files appears in any output folder and no temporary entry is left")
def _():
    ws = workspace(); edit(os.path.join(paths(ws)[1], f"dashboard_{CURRENT}.html"), lambda t: "<html>broken</html>\n")
    rejected(ws, ["dashboard"]); assert not any(n.endswith((".tmp", ".partial")) or "deliverables" in n for n in os.listdir(ws)), os.listdir(ws)

# ---- added in Stage 2: requirements of the Stage 2 brief that the Stage 1 checks did not yet state (non-empty files, regular files, the real UTC time relationship, reported paths, output-folder rules)
def with_fetched(ws, stamp):
    """Give the report another 'Data fetched' time and rebuild the dashboard from it, so that the TIME is the only thing that is right or wrong."""
    reports, dash, out = paths(ws); edit(os.path.join(reports, f"report_{CURRENT}.md"), lambda t: re.sub(r"^- Data fetched: .*$", f"- Data fetched: {stamp}", t, flags=re.M))
    os.remove(os.path.join(dash, f"dashboard_{CURRENT}.html")); dashboard.generate(reports, generated_at=GENERATED_AT, out_dir=dash)

@R.register("every deliverable must be non-empty: a zero-byte report or CSV is rejected and named (an empty dashboard is covered above)")
def _():
    cases = []
    for name in (f"report_{CURRENT}.md", f"report_{CURRENT}_data.csv"):
        ws = workspace(); open(os.path.join(paths(ws)[0], name), "wb").close(); cases.append((f"empty {name}", ws, [name, "empty"], CURRENT))
    each_rejected(cases)

@R.register("every deliverable must be a regular file: a folder standing in for the report, the CSV or the dashboard is rejected")
def _():
    cases = []
    for folder_index, name in ((0, f"report_{CURRENT}.md"), (0, f"report_{CURRENT}_data.csv"), (1, f"dashboard_{CURRENT}.html")):
        ws = workspace(); path = os.path.join(paths(ws)[folder_index], name); os.remove(path); os.makedirs(path); cases.append((f"folder named {name}", ws, [name], CURRENT))
    each_rejected(cases)

@R.register("the 'Data fetched' time is judged as a real UTC date and time against the analysis date: the following day is accepted (00:00:00 and 23:59:59 included); the same day, two days later and an impossible time are rejected")
def _():
    good = []; bad = []
    for stamp in ("2026-10-27 00:00:00 UTC", "2026-10-27 23:59:59 UTC"): ws = workspace(); with_fetched(ws, stamp); good.append((stamp, ws))
    for stamp in ("2026-10-26 23:59:59 UTC", "2026-10-28 00:00:00 UTC", "2026-10-27 25:61:61 UTC"): ws = workspace(); with_fetched(ws, stamp); bad.append((stamp, ws, ["report"], CURRENT))
    for stamp, ws in good:
        code, text = stage(ws); assert code == 0, f"{stamp} must be accepted: {text.strip()[-200:]}"; assert files_in(paths(ws)[2]) == sorted(NAMES(CURRENT))
    each_rejected(bad)

@R.register("on success the output names the date and the path of every staged file")
def _():
    ws = workspace(); reports, dash, out = paths(ws); code, text = stage(ws); assert code == 0, text.strip()[-200:]
    assert CURRENT in text and all(os.path.join(out, name) in text for name in NAMES(CURRENT)), text

@R.register("an existing EMPTY output folder is filled (there is nothing to overwrite); a missing parent folder is an error and is never created")
def _():
    ws = workspace(); reports, dash, out = paths(ws); os.makedirs(out); code, text = stage(ws); assert code == 0 and files_in(out) == sorted(NAMES(CURRENT)), text.strip()[-200:]
    ws = workspace(); missing_parent = os.path.join(ws, "no_such_folder"); code, text = stage(ws, out=os.path.join(missing_parent, "deliverables"))
    assert code == 1 and not os.path.exists(missing_parent), (code, text.strip()[-200:])

def edit_dashboard_data(ws, change):
    """Change only the data object embedded in the dashboard page; the rest of the page stays byte-identical."""
    def splice(page):
        found = re.search(r'(<script id="dashboard-data" type="application/json">)(.*?)(</script>)', page, re.S); data = json.loads(found.group(2)); change(data)
        return page[:found.start(2)] + dashboard.json_for_script(data) + page[found.end(2):]
    edit(os.path.join(paths(ws)[1], f"dashboard_{CURRENT}.html"), splice)

@R.register("size limits are enforced before anything is parsed: a CSV over its limit, a report over its limit and a dashboard over 16 MiB are rejected and named")
def _():
    cases = []
    for name, folder_index, size in ((f"report_{CURRENT}_data.csv", 0, dashboard.MAX_CSV_BYTES + 1), (f"report_{CURRENT}.md", 0, dashboard.MAX_REPORT_BYTES + 1), (f"dashboard_{CURRENT}.html", 1, da.MAX_DASHBOARD_BYTES + 1)):
        ws = workspace(); open(os.path.join(paths(ws)[folder_index], name), "wb").write(b"x" * size); cases.append((f"{name} of {size} bytes", ws, [name, "larger"], CURRENT))
    each_rejected(cases)

@R.register("a dashboard built from another fetch is rejected: its embedded 'data fetched' time differs from the report's although names, dates and rows all match")
def _():
    ws = workspace(); edit_dashboard_data(ws, lambda data: data["meta"].__setitem__("data_fetched", "2026-10-27 09:42:00 UTC")); each_rejected([("embedded fetched time", ws, ["dashboard"], CURRENT)])

@R.register("a dashboard whose embedded data differs from the CSV in a single value is rejected (same names, same dates, same columns)")
def _():
    ws = workspace(); edit_dashboard_data(ws, lambda data: data["rows"][5].__setitem__(1, "1.0")); each_rejected([("one changed value", ws, ["dashboard"], CURRENT)])

@R.register("a failure while publishing leaves nothing behind: no output folder and no temporary '.staging-' folder (the final rename is made to fail)")
def _():
    ws = workspace(); reports, dash, out = paths(ws); real_rename = da.os.rename
    def broken(source, destination): raise OSError("simulated failure of the final rename")
    da.os.rename = broken
    try: code, text = stage(ws)
    finally: da.os.rename = real_rename
    assert code == 1 and "ERROR" in text, (code, text.strip()[-200:])
    assert not os.path.exists(out) and set(os.listdir(ws)) <= {"reports", "dashboard_out"}, sorted(os.listdir(ws))

@R.register("a CSV that ends on another day than the analysis date is rejected for exactly that reason (named: the CSV and the day it really ends on)")
def _():
    ws = workspace(); shutil.copy(os.path.join(gold(), "historical", "reports", f"report_{HISTORICAL}_data.csv"), os.path.join(paths(ws)[0], f"report_{CURRENT}_data.csv"))
    each_rejected([("historical CSV under the current name", ws, [f"report_{CURRENT}_data.csv", f"ends on {HISTORICAL}", f"not on the analysis date {CURRENT}"], CURRENT)])

@R.register("anything the operating system reports as not a regular file (a link, a pipe, a device) is rejected, even if it is not empty - simulated through lstat so that it works on every platform")
def _():
    ws = workspace(); name = f"dashboard_{CURRENT}.html"; real_lstat = da.os.lstat
    def lstat_reports_a_pipe(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return os.stat_result((stat.S_IFIFO | 0o644,) + tuple(info)[1:]) if os.path.basename(str(path)) == name else info
    da.os.lstat = lstat_reports_a_pipe
    try: rejected(ws, [name, "regular file"])
    finally: da.os.lstat = real_lstat

@R.register("the analysis date must be written exactly YYYY-MM-DD: forms that Python's own date parser would also accept (20261026, 2026-W44-1) or that are not padded are rejected")
def _():
    each_rejected([(f"--date {day}", workspace(), ["date"], day) for day in ("20261026", "2026-W44-1", "2026-10-6", "+2026-10-26")])

@R.register("the command needs no secret and prints none: with token-like variables in the environment they are neither required nor echoed, on success or on failure")
def _():
    dummy = {"GOOGLE_APPS_SCRIPT_TOKEN": "dummy-token-value-for-the-test-0123456789", "GOOGLE_APPS_SCRIPT_URL": "https://script.google.com/macros/s/DUMMYDEPLOYMENTID0123456789/exec"}
    ws = workspace(); code, text = stage(ws, extra_env=dummy); assert code == 0 and not any(v in text for v in dummy.values()), text.strip()[-200:]
    ws = workspace(); os.remove(os.path.join(paths(ws)[0], f"report_{CURRENT}.md")); code, text = stage(ws, extra_env=dummy); assert code == 1 and not any(v in text for v in dummy.values()), text.strip()[-200:]

# ================================================================ security regression S2-1: text taken from a FILE must never put a second line, or a GitHub workflow command, into the output
# GitHub Actions acts on a line that STARTS with '::' (or '##['). The command's error message used to repeat parts of the CSV, so a header cell with a line break could start such a line.
# These checks run the real command (in-process for the variants, as a child process for the end-to-end case) with hostile file content and judge only what a person or the runner would see.
def hostile_header_workspace(fragment):
    """A workspace whose CSV header cell 'btc_close' is followed by `fragment`, inside a correctly quoted CSV cell."""
    ws = workspace(); edit(os.path.join(paths(ws)[0], f"report_{CURRENT}_data.csv"), lambda t: t.replace("btc_close", '"btc_close' + fragment + '"', 1)); return ws
def hostile_last_row(change):
    """A workspace whose CSV last row is rewritten by change(list of fields) -> line text."""
    ws = workspace(); edit(os.path.join(paths(ws)[0], f"report_{CURRENT}_data.csv"), lambda t: t.rstrip("\n").rsplit("\n", 1)[0] + "\n" + change(t.rstrip("\n").rsplit("\n", 1)[1].split(",")) + "\n"); return ws
def single_safe_line(text):
    """The output of a refused run must be ONE printable line that starts with 'ERROR: ' and holds no workflow-command syntax anywhere in it."""
    lines = text.splitlines(); assert len(lines) == 1, f"{len(lines)} output lines: {text[:160]!r}"
    line = lines[0]; assert line.startswith("ERROR: "), line[:60]
    assert all(ch.isprintable() for ch in line), f"unsafe character(s) in the output: {[ch for ch in line if not ch.isprintable()][:5]!r}"
    assert "::" not in line and "##[" not in line, f"workflow-command syntax in the output: {line[:160]!r}"
    return line
def refused_as_one_safe_line(ws, mention=("csv",)):
    """Exit 1, one safe line that still says what is wrong, nothing staged, the sources untouched. Returns the line."""
    reports, dash, out = paths(ws); before = snapshot(reports, dash); code, text = stage(ws)
    assert code == 1, f"expected exit 1, got {code}: {text.strip()[-160:]!r}"
    line = single_safe_line(text)
    for needle in mention: assert needle.lower() in line.lower(), f"the diagnostic lost its meaning ({needle!r} is missing): {line[:200]!r}"
    assert snapshot(reports, dash) == before and not os.path.exists(out), "the sources changed or something was staged"
    return line

@R.register("S2-1 reproduction: a CSV header cell holding CR/LF followed by '::error::' is refused with ONE safe line, and the line still names the CSV and the header problem")
def _():
    cases = [hostile_header_workspace(fragment) for fragment in ("\r\n::error::injected", "\n::error::injected", "\r::error::injected", "\n\n::error file=x,line=1,title=t::injected")]
    for ws in cases: refused_as_one_safe_line(ws, ["csv", "17-column header", "found 17 columns"])

@R.register("S2-1: every GitHub workflow-command spelling in file text - ::warning::, ::notice::, ::add-mask::, ::stop-commands::, ::set-output, ::debug::, ::group::, ::save-state, legacy ##[error], runs of colons - is neutralised, at a line start and inline")
def _():
    commands = ["::warning::w", "::notice::n", "::add-mask::secret", "::stop-commands::token", "::set-output name=x::y", "::debug::d", "::group::g", "::save-state name=a::b", "##[error]legacy", ":::error:::triple", "::error::inline"]
    cases = [(command, prefix, hostile_header_workspace(prefix + command)) for command in commands for prefix in ("\n", " ")]
    for command, prefix, ws in cases:
        try: refused_as_one_safe_line(ws, ["csv"])
        except AssertionError as error: raise AssertionError(f"{prefix!r} + {command}: {error}")

@R.register("S2-1: other control and invisible characters in file text (bell, tab, vertical tab, form feed, ESC colour codes, DEL, NEL, line/paragraph separators, zero-width and bidirectional marks, BOM) never reach the output")
def _():
    controls = ["\x07", "\x08", "\t", "\x0b", "\x0c", "\x1b[31mred\x1b[0m", "\x1c", "\x1d", "\x1e", "\x7f", "\x85", " ", " ", "​", "‮", "﻿", "\x00"]
    cases = [(repr(control), hostile_header_workspace("A" + control + "B")) for control in controls]
    for label, ws in cases:
        try: refused_as_one_safe_line(ws, ["csv"])
        except AssertionError as error: raise AssertionError(f"{label}: {error}")

@R.register("S2-1: very long file text is cut: a 10,000-character CSV cell (header or data) gives a message of a few hundred characters at most, shortened with '...', that still says where the problem is")
def _():
    header = hostile_header_workspace("X" * 10000); data = hostile_last_row(lambda fields: ",".join([fields[0], "9" * 10000 + "x"] + fields[2:]))
    line = refused_as_one_safe_line(header, ["csv", "17-column header"]); assert len(line) <= 400, len(line)
    line = refused_as_one_safe_line(data, ["csv", "row 31", "btc_close"]); assert len(line) <= 400 and line.endswith("..."), (len(line), line[-60:])

@R.register("S2-1: ordinary validation errors stay useful - a mistyped header, a non-numeric price, a gap in the dates and a changed return are each named precisely (and exit 1)")
def _():
    typo = workspace(); edit(os.path.join(paths(typo)[0], f"report_{CURRENT}_data.csv"), lambda t: t.replace("btc_close", "btc_closing", 1))
    number = hostile_last_row(lambda fields: ",".join([fields[0], "abc"] + fields[2:]))
    gap = workspace(); edit(os.path.join(paths(gap)[0], f"report_{CURRENT}_data.csv"), lambda t: "\n".join(t.split("\n")[:10] + t.split("\n")[11:]))
    changed = hostile_last_row(lambda fields: ",".join(fields[:3] + ["0.5"] + fields[4:]))
    for label, ws, mention in (("typo", typo, ["17-column header", "found 17 columns", "btc_closing"]), ("price", number, ["row 31", "btc_close", "'abc'"]), ("gap", gap, ["does not follow"]), ("changed return", changed, ["row 31", "btc_return", "does not equal"])):
        try: refused_as_one_safe_line(ws, ["csv"] + mention)
        except AssertionError as error: raise AssertionError(f"{label}: {error}")

@R.register("S2-1: hostile text in the OTHER inputs is just as harmless - a CSV date cell and return cell with line breaks, a dashboard whose embedded names hold line breaks and '::error::', a report whose 'Data fetched' line holds a command")
def _():
    date_cell = hostile_last_row(lambda fields: ",".join(['"' + fields[0] + '\n::error::x"'] + fields[1:]))
    return_cell = hostile_last_row(lambda fields: ",".join(fields[:3] + ['"0.5\n"'] + fields[4:]))
    page = workspace(); edit_dashboard_data(page, lambda data: data["meta"].__setitem__("csv_file", "x\n::error::y\r\n##[error]z"))
    report = workspace(); edit(os.path.join(paths(report)[0], f"report_{CURRENT}.md"), lambda t: re.sub(r"^- Data fetched: .*$", "- Data fetched: ::error::from-the-report", t, flags=re.M))
    for label, ws, mention in (("date cell", date_cell, ["csv", "invalid date"]), ("return cell", return_cell, ["csv", "does not equal"]), ("dashboard names", page, ["dashboard"]), ("report line", report, ["report"])):
        try: refused_as_one_safe_line(ws, mention)
        except AssertionError as error: raise AssertionError(f"{label}: {error}")

@R.register("S2-1 defence in depth: if the project's own report check (or CSV parser) ever put hostile text into its error message, the command would still print one safe line (the check is made to fail with such a message)")
def _():
    hostile = "the report disagrees\n::error::from-the-checker\r\n##[warning]x\x1b[31m " + "Z" * 500
    for name, expected_word in (("check_against_report", "report"), ("parse_dataset", "csv")):
        ws = workspace(); real = getattr(dashboard, name)
        def failing(*arguments, **keywords): raise dashboard.DashboardInputError(hostile)
        setattr(dashboard, name, failing)
        try: line = refused_as_one_safe_line(ws, [expected_word])
        finally: setattr(dashboard, name, real)
        assert len(line) <= 400 and "the report disagrees" in line, (name, len(line), line[:200])

@R.register("S2-1 end to end: the real command run as a child process (as the workflow runs it) with the original reproduction prints exactly one stderr line, nothing on stdout, no line starting with '::', exit code 1 and no output folder")
def _():
    ws = hostile_header_workspace("\n::error::injected-by-the-csv"); reports, dash, out = paths(ws)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}; environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    done = subprocess.run([sys.executable, os.path.join(REPO, "src", "daily_automation.py"), "stage-deliverables", "--reports-dir", reports, "--dashboard-dir", dash, "--out-dir", out, "--date", CURRENT],
                          capture_output=True, text=True, env=environment, timeout=120)
    assert done.returncode == 1 and done.stdout == "", (done.returncode, done.stdout[:100])
    lines = done.stderr.splitlines(); assert len(lines) == 1 and not any(line.startswith(("::", "##[")) for line in lines) and lines[0].startswith("ERROR: "), lines
    assert not os.path.exists(out)

R.finish()
