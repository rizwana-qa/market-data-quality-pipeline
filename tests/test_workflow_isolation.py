"""Isolation of the test workflow from production.

.github/workflows/daily-market-analysis.yml is production (schedule, production secrets, GitHub Pages).
.github/workflows/daily-market-analysis-test.yml is the test copy: it runs only on a push to deploy/evidence-pipeline, in the mdq-test environment,
with test-only secret names, its own concurrency group and no GitHub Pages steps.
These are static checks; they cannot prove how GitHub resolves secrets or environments, which depends on settings outside the repository.
"""
import hashlib
import io
import contextlib
import os
import re
import tempfile

from wf_support import Runner, REPO
import daily_automation as da

WORKFLOWS = os.path.join(REPO, ".github", "workflows")
PROD_PATH = os.path.join(WORKFLOWS, "daily-market-analysis.yml")
TEST_PATH = os.path.join(WORKFLOWS, "daily-market-analysis-test.yml")
PROD = open(PROD_PATH, encoding="utf-8", newline="").read()
TEST = open(TEST_PATH, encoding="utf-8", newline="").read()
R = Runner("workflow isolation checks")

PROD_GUARD = "    if: github.ref == 'refs/heads/main'\n"
TEST_GUARD = "    if: github.ref == 'refs/heads/deploy/evidence-pipeline'\n"
PRODUCTION_BEFORE_GUARD_SHA256 = "29c78766dc04e4ec21c48d769c2ccf91db3a12df8ac84e01c440e74ec4d82cd6"     # the production file as approved, before the main guard line was added
TEST_SECRETS = {"MDQ_TEST_APPS_SCRIPT_URL", "MDQ_TEST_APPS_SCRIPT_TOKEN"}
PRODUCTION_SECRETS = {"GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_TOKEN", "GITHUB_TOKEN"}
PAGES_STEP_IDS = ("prepare_pages", "publish_pages")


def steps(text):
    blocks = re.split(r"\n(?=      - name: )", text.split("    steps:\n", 1)[1])
    return {re.search(r"^        id: (\S+)", block, re.M).group(1): block for block in blocks}


def section(text, name):
    """The top-level block `name:` up to the next top-level key."""
    found = re.search(rf"^{name}:\n((?:(?:  .*)?\n)+)", text, re.M)
    assert found, f"no top-level {name}:"
    return found.group(1).rstrip("\n") + "\n"


def notify_outcome_arguments(text):
    block = steps(text)["notify_failure"]
    return re.findall(r'--outcome "([a-z_]+)=\$OUTCOME_[A-Z_]+"', block)


PROD_STEPS, TEST_STEPS = steps(PROD), steps(TEST)


# ------------------------------------------------------------------ the test workflow
@R.register("test workflow: the only trigger is a push to exactly one branch, deploy/evidence-pipeline (no schedule, manual, pull request or other trigger)")
def _():
    on_block = section(TEST, "on")
    assert on_block == "  push:\n    branches:\n      - deploy/evidence-pipeline\n", on_block
    for forbidden in ("schedule", "workflow_dispatch", "pull_request", "workflow_run", "workflow_call", "tags", "paths", "cron"):
        assert forbidden not in on_block, forbidden


@R.register("test workflow: the job runs only for the exact ref refs/heads/deploy/evidence-pipeline")
def _():
    job_head = TEST.split("    steps:\n", 1)[0].split("jobs:\n", 1)[1]
    assert TEST_GUARD in job_head, "exact job guard missing"
    assert len(re.findall(r"^    if:", job_head, re.M)) == 1
    assert "refs/heads/main" not in TEST


@R.register("test workflow: the job uses the mdq-test GitHub Environment (and no other)")
def _():
    job_head = TEST.split("    steps:\n", 1)[0]
    assert re.findall(r"^    environment: (.+)$", job_head, re.M) == ["mdq-test"]
    assert len(re.findall(r"\benvironment:", TEST)) == 1


@R.register("test workflow: its own concurrency group, separate from production, and a started run is never cancelled")
def _():
    assert section(TEST, "concurrency") == "  group: daily-market-analysis-test\n  cancel-in-progress: false\n"
    assert section(PROD, "concurrency") == "  group: daily-market-analysis\n  cancel-in-progress: false\n"


@R.register("test workflow: read-only repository permission (contents: read) and nothing broader")
def _():
    assert section(TEST, "permissions") == "  contents: read\n"
    assert len(re.findall(r"^\s*permissions:", TEST, re.M)) == 1
    assert not re.search(r"^\s+[A-Za-z_-]+:\s*(?:write|admin)\s*$", TEST, re.M)


@R.register("test workflow: no GitHub Pages preparation or publishing step, action, branch or site folder")
def _():
    for step_id in PAGES_STEP_IDS:
        assert step_id not in TEST_STEPS and step_id not in TEST, step_id
    for text in ("gh-pages", "peaceiris", "pages-site", "publish_branch", "publish_dir"):
        assert text not in TEST, text


@R.register("test workflow: the only secrets are the two test-only names (GitHub token and production secret names are absent)")
def _():
    names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEST))
    assert names == TEST_SECRETS, names
    assert "secrets.GOOGLE_APPS_SCRIPT" not in TEST and "GITHUB_TOKEN" not in TEST and "secrets.GITHUB" not in TEST
    assert not (names & PRODUCTION_SECRETS)
    assert "secrets[" not in TEST and "toJSON(secrets" not in TEST and "inherit" not in TEST


@R.register("test workflow: the test secrets are mapped to the existing Python variable names, only in the four steps that use the web app")
def _():
    users = {step_id for step_id, block in TEST_STEPS.items() if "secrets." in block}
    assert users == {"post_to_sheet", "verify_sheet", "send_success_email", "notify_failure"}, users
    for step_id in users:
        block = TEST_STEPS[step_id]
        assert "          GOOGLE_APPS_SCRIPT_URL: ${{ secrets.MDQ_TEST_APPS_SCRIPT_URL }}\n" in block, step_id
        assert "          GOOGLE_APPS_SCRIPT_TOKEN: ${{ secrets.MDQ_TEST_APPS_SCRIPT_TOKEN }}\n" in block, step_id


@R.register("test workflow: it is production's step list minus exactly the two Pages steps, and every shared step runs the same commands")
def _():
    assert [i for i in PROD_STEPS if i not in PAGES_STEP_IDS] == list(TEST_STEPS), (list(PROD_STEPS), list(TEST_STEPS))
    run_of = lambda block: (re.search(r"^        run: \|\n((?:          .*\n?)+)", block, re.M) or [None, None])[1]
    for step_id, block in TEST_STEPS.items():
        if step_id == "notify_failure":
            continue
        assert run_of(block) == run_of(PROD_STEPS[step_id]), step_id
        uses = lambda text: re.findall(r"^        uses: (.+)$", text, re.M)
        assert uses(block) == uses(PROD_STEPS[step_id]), step_id
    expected = re.sub(r'            --outcome "(?:prepare|publish)_pages=\$OUTCOME_[A-Z_]+" \\\n', "", run_of(PROD_STEPS["notify_failure"]))
    assert run_of(TEST_STEPS["notify_failure"]) == expected


# ------------------------------------------------------------------ failure notification without the Pages steps
@R.register("failure notification (test workflow): every --outcome names a step that exists in that file, with a matching OUTCOME_ variable, and a known pipeline stage")
def _():
    names = notify_outcome_arguments(TEST)
    assert names and len(names) == len(set(names))
    assert set(names) <= set(TEST_STEPS) and not (set(names) & set(PAGES_STEP_IDS)), names
    assert set(names) <= set(da.STAGE_ORDER), set(names) - set(da.STAGE_ORDER)
    block = TEST_STEPS["notify_failure"]
    env = dict(re.findall(r"^          (OUTCOME_[A-Z_]+): \$\{\{ steps\.([a-z_]+)\.outcome \}\}$", block, re.M))
    assert set(env.values()) == set(names) and len(env) == len(names), (sorted(env.values()), sorted(names))
    assert "PAGES" not in block
    for step_id in re.findall(r"steps\.([a-z_]+)\.(?:outcome|outputs)", TEST):
        assert step_id in TEST_STEPS, f"steps.{step_id} does not exist in the test workflow"


def _run_notify(outcome_pairs):
    argv = ["notify-failure", "--dir", tempfile.mkdtemp(prefix="isolation_"), "--run-id", "123456789",
            "--run-url", "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789"]
    for pair in outcome_pairs:
        argv += ["--outcome", pair]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = da.main(argv, env={}, now=None)
    return code, err.getvalue()


@R.register("failure notification: with the test workflow's outcomes (no Pages steps) the failed step is found and the outcomes are accepted; the run then stops only at the missing web app configuration")
def _():
    names = notify_outcome_arguments(TEST)
    for failing in ("analysis", "validate_outputs", "post_to_sheet", "send_success_email"):
        pairs = [f"{name}={'failure' if name == failing else 'success'}" for name in names]
        assert da.derive_stage(da.parse_outcomes(pairs)) == failing
        code, err = _run_notify(pairs)
        assert "--outcome must look like" not in err and "unknown pipeline stage" not in err, err
        assert code == 1 and "missing configuration" in err, (code, err[:200])


@R.register("failure notification: control - an EMPTY outcome, which is what a removed Pages step would give, is rejected; this is why the test workflow does not pass those two outcomes")
def _():
    names = notify_outcome_arguments(TEST)
    pairs = [f"{name}={'failure' if name == 'analysis' else 'success'}" for name in names] + ["prepare_pages=", "publish_pages="]
    code, err = _run_notify(pairs)
    assert code == 1 and "--outcome must look like" in err, err


@R.register("run status (test workflow): summarize-run is given only outcomes of steps that exist, and is not given any Pages outcome")
def _():
    block = TEST_STEPS["summarize_run"]
    names = re.findall(r'--outcome "([a-z_]+)=', block)
    assert names == ["analysis", "validate_outputs", "build_payload", "post_to_sheet", "verify_sheet", "send_success_email"], names
    assert set(names) <= set(TEST_STEPS)
    assert "pages" not in block.lower()


# ------------------------------------------------------------------ production protection
@R.register("production: the only change is the job-level main guard; with that one line removed the file is byte-identical to the approved production workflow")
def _():
    assert PROD.count(PROD_GUARD) == 1
    original = PROD.replace(PROD_GUARD, "")
    assert hashlib.sha256(original.encode("utf-8")).hexdigest() == PRODUCTION_BEFORE_GUARD_SHA256
    job_head = PROD.split("    steps:\n", 1)[0].split("jobs:\n", 1)[1]
    assert PROD_GUARD in job_head and len(re.findall(r"^    if:", job_head, re.M)) == 1


@R.register("production: schedule 0 4 * * *, manual trigger, its own concurrency group, contents: write for the Pages publish and the original Pages steps are unchanged")
def _():
    assert re.findall(r'- cron: "([^"]+)"', PROD) == ["0 4 * * *"]
    assert section(PROD, "on") == '  schedule:\n    - cron: "0 4 * * *"\n  workflow_dispatch:\n'
    assert section(PROD, "concurrency") == "  group: daily-market-analysis\n  cancel-in-progress: false\n"
    assert section(PROD, "permissions") == "  contents: write\n"
    assert list(PROD_STEPS)[9:11] == list(PAGES_STEP_IDS), list(PROD_STEPS)
    assert "peaceiris/actions-gh-pages@" in PROD_STEPS["publish_pages"]


@R.register("production: reads only the production secrets, has no environment key and never mentions the test secrets, environment or branch")
def _():
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", PROD)) == PRODUCTION_SECRETS
    assert "environment:" not in PROD
    for text in ("MDQ_TEST", "mdq-test", "deploy/evidence-pipeline", "daily-market-analysis-test"):
        assert text not in PROD, text


@R.register("workflow directory: exactly the production and the test workflow exist (no other workflow can reach the production secrets)")
def _():
    assert sorted(os.listdir(WORKFLOWS)) == ["daily-market-analysis-test.yml", "daily-market-analysis.yml"], os.listdir(WORKFLOWS)


@R.register("both files: no tabs, no trailing whitespace, LF line endings")
def _():
    for text in (PROD, TEST):
        assert "\t" not in text and "\r" not in text and not re.search(r"[ ]+$", text, re.M)


R.finish()
