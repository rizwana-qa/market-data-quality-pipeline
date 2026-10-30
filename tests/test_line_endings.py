"""Tests for finding L1: the repository must behave the same whatever core.autocrlf a developer's git uses.
Several checks are byte-exact (the pinned fetch_data.py hash in test_workflow_integration.py, the report validator, which rejects 'UTC' followed by a carriage return), so every checkout must contain LF only.
The behaviour is tested with REAL git: throw-away repositories and clones are created in the system temp folder (never in this repository). The user's own git configuration is shut out
(GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM), so core.autocrlf is exactly what each case sets. Nothing is committed or pushed anywhere that matters; git must be installed."""
import atexit, hashlib, os, re, shutil, stat, subprocess, sys, tempfile
from datetime import date

from wf_support import Runner, REPO, load, raises

da = load("daily_automation")
GITATTRIBUTES = os.path.join(REPO, ".gitattributes")
RULE = "* text=auto eol=lf"
TODAY = date(2026, 10, 28)                                 # the day after the committed 2026-10-27 report
KEY_FILES = ["src/fetch_data.py", "reports/report_2026-10-27.md", "reports/report_2026-10-27_data.csv", "src/analysis.py", "src/dashboard.py", "src/run_workflow.py", "requirements.txt", "README.md"]
PINNED = re.search(r'FETCHER_SHA256 = "([0-9a-f]{64})"', open(os.path.join(REPO, "tests", "test_workflow_integration.py"), encoding="utf-8").read()).group(1)
def _make_writable_and_retry(function, path, *_):          # git writes its object files read-only, and Windows cannot delete a read-only file
    os.chmod(path, stat.S_IWRITE); function(path)
def _clean_up():
    try: shutil.rmtree(WORK, **({"onexc": _make_writable_and_retry} if sys.version_info >= (3, 12) else {"onerror": _make_writable_and_retry}))
    except OSError: pass
WORK = tempfile.mkdtemp(prefix="eol_"); atexit.register(_clean_up)
R = Runner("line-ending checks")

EMPTY_CONFIG = os.path.join(WORK, "empty.gitconfig"); open(EMPTY_CONFIG, "w", encoding="utf-8").close()
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=EMPTY_CONFIG, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", HOME=WORK, USERPROFILE=WORK, XDG_CONFIG_HOME=WORK)

def git(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace').strip()[:200]}"
    return done.stdout

sha = lambda data: hashlib.sha256(data).hexdigest()
crlf = lambda data: data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
read = lambda folder, rel: open(os.path.join(folder, *rel.split("/")), "rb").read()
LF_FILES = {rel: read(REPO, rel) for rel in KEY_FILES}      # the files exactly as they are in this working tree

def make_repo(name, files, attributes, autocrlf="false"):
    """A throw-away repository holding `files` (and, if asked, this repository's own .gitattributes), with one commit."""
    folder = os.path.join(WORK, name); os.makedirs(folder); git("init", "-q", folder)
    for key, value in (("user.name", "test"), ("user.email", "test@example.invalid"), ("core.autocrlf", autocrlf)): git("config", key, value, cwd=folder)
    if attributes:
        assert os.path.isfile(GITATTRIBUTES), ".gitattributes is missing from the repository"; files = dict(files, **{".gitattributes": open(GITATTRIBUTES, "rb").read()})
    for rel, data in files.items():
        path = os.path.join(folder, *rel.split("/")); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(data)
    git("add", "-A", cwd=folder); git("commit", "-q", "-m", "fixture", cwd=folder); return folder

def checkout(origin, name, autocrlf):
    destination = os.path.join(WORK, name); git("clone", "-q", "-c", f"core.autocrlf={autocrlf}", origin, destination); return destination

def eol_view(folder): return git("ls-files", "--eol", cwd=folder).decode()
def validate(checkout_folder): return da.validate_outputs(os.path.join(checkout_folder, "reports"), today=TODAY)
def in_git_repository(): done = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO, capture_output=True, text=True); return done.returncode == 0 and done.stdout.strip() == "true"

# ================================================================ the rule itself
@R.register("gitattributes: the repository has a .gitattributes whose only rule is '* text=auto eol=lf' (LF for every text file, in the repository and in every working tree)")
def _():
    assert os.path.isfile(GITATTRIBUTES), ".gitattributes is missing"
    raw = open(GITATTRIBUTES, "rb").read(); assert b"\r" not in raw
    rules = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]; assert rules == [RULE], rules

@R.register("gitattributes: git itself applies that rule to every file of this repository (text=auto, eol=lf)")
def _():
    if not in_git_repository(): return                                    # a copy without .git (an archive): the rule file itself is checked above
    names = [n for n in subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO, capture_output=True, text=True).stdout.split("\n") if n]; assert len(names) >= 20, len(names)
    seen = {}
    for i in range(0, len(names), 40):
        for line in subprocess.run(["git", "check-attr", "text", "eol", "--", *names[i:i + 40]], cwd=REPO, capture_output=True, text=True).stdout.splitlines():
            path, attribute, value = re.match(r"^(.*): (text|eol): (.*)$", line).groups(); seen.setdefault(path, {})[attribute] = value
    wrong = {p: a for p, a in seen.items() if a != {"text": "auto", "eol": "lf"}}; assert len(seen) == len(names) and not wrong, list(wrong.items())[:3]

@R.register("this repository: git reports every tracked file as LF in the repository and in the working tree (no crlf, no mixed)")
def _():
    if not in_git_repository(): return
    listing = subprocess.run(["git", "ls-files", "--eol"], cwd=REPO, capture_output=True, text=True).stdout.splitlines(); assert listing
    bad = [line for line in listing if re.search(r"\b[iw]/(crlf|mixed)\b", line)]; assert not bad, bad[:3]

@R.register("this working tree: no text file contains a carriage return (LF everywhere)")
def _():
    named = (".py", ".mjs", ".gs", ".yml", ".yaml", ".md", ".csv", ".json", ".html", ".js", ".txt"); bad = []
    for folder, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for name in names:
            if name.endswith(named) or name in (".gitignore", ".gitattributes", "LICENSE"):
                if b"\r" in open(os.path.join(folder, name), "rb").read(): bad.append(os.path.relpath(os.path.join(folder, name), REPO))
    assert not bad, bad

# ================================================================ real git, both settings
@R.register("control (this is problem L1): WITHOUT .gitattributes a clone made with core.autocrlf=true gets CRLF - the pinned fetch_data.py hash no longer matches and the report is refused; with autocrlf=false the same repository is fine")
def _():
    origin = make_repo("control_origin", LF_FILES, attributes=False); bad = checkout(origin, "control_true", "true"); good = checkout(origin, "control_false", "false")
    assert all(b"\r\n" in read(bad, rel) for rel in ("src/fetch_data.py", "reports/report_2026-10-27.md")), "the control must show CRLF"
    assert sha(read(bad, "src/fetch_data.py")) != PINNED; raises(da.AutomationError, lambda: validate(bad), "Data fetched")
    assert sha(read(good, "src/fetch_data.py")) == PINNED and validate(good)["date"] == "2026-10-27"

@R.register("fix: WITH .gitattributes, clones made with core.autocrlf=true AND false hold only LF, byte-identical to the originals; the pinned fetch_data.py hash matches; the report and CSV validate")
def _():
    origin = make_repo("fixed_origin", LF_FILES, attributes=True)
    for setting in ("true", "false"):
        clone = checkout(origin, f"fixed_{setting}", setting)
        for rel, data in LF_FILES.items(): assert read(clone, rel) == data, (setting, rel)
        assert sha(read(clone, "src/fetch_data.py")) == PINNED, setting; assert validate(clone)["date"] == "2026-10-27", setting
        view = eol_view(clone); assert "w/crlf" not in view and "i/crlf" not in view and view.count("w/lf") == len(LF_FILES) + 1, (setting, view)

@R.register("fix: a developer whose editor wrote CRLF cannot get CRLF into the repository - committing stores LF with core.autocrlf true or false; real binary data is left untouched")
def _():
    binary = b"\x00\x01\r\n\x02\r\n\xff"
    for setting in ("true", "false"):
        files = {rel: crlf(data) for rel, data in LF_FILES.items()}; files["blob.bin"] = binary
        repo = make_repo(f"commit_{setting}", files, attributes=True, autocrlf=setting)
        for rel, data in LF_FILES.items(): assert git("show", f"HEAD:{rel}", cwd=repo) == data, (setting, rel)
        assert git("show", "HEAD:blob.bin", cwd=repo) == binary, setting; view = eol_view(repo); assert "i/crlf" not in view and "i/mixed" not in view, (setting, view)

R.finish()
