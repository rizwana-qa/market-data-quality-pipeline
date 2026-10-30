"""Static checks of .github/workflows/daily-market-analysis.yml (no YAML library: the file follows a fixed two-space layout that this parser relies on). Written BEFORE the workflow was rewritten.
These checks cannot prove the workflow runs on GitHub; that needs a real Actions run."""
import os, re

from wf_support import Runner, REPO

PATH = os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml")
R = Runner("workflow file checks")
TEXT = open(PATH, encoding="utf-8", newline="").read()
LINES = TEXT.splitlines()
ORDER = ["checkout", "setup_python", "install_dependencies", "tests", "analysis", "validate_outputs", "build_payload", "post_to_sheet", "verify_sheet", "send_success_email", "upload_artifacts", "notify_failure"]
SECRET_STEPS = ["post_to_sheet", "verify_sheet", "send_success_email", "notify_failure"]
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python", "actions/upload-artifact"}

def parse_steps(lines):
    start = next((i for i, l in enumerate(lines) if l == "    steps:"), None)
    assert start is not None, "no '    steps:' line at four spaces"
    steps, current = [], None
    for line in lines[start + 1:]:
        if line.startswith("      - "):
            current = {"lines": [], "env": {}, "with": {}}; steps.append(current); line = "        " + line[8:]
        elif current is None: continue
        elif line and not line.startswith("        "): break
        current["lines"].append(line)
    for step in steps:
        body, section, run = step["lines"], None, []
        i = 0
        while i < len(body):
            line = body[i]; m = re.match(r"^        ([A-Za-z_-]+):\s*(.*)$", line)
            if m:
                key, value = m.groups(); section = key if key in ("env", "with") else None
                if key == "run":
                    if value in ("|", ">"):
                        i += 1
                        while i < len(body) and (body[i].startswith("          ") or not body[i].strip()): run.append(body[i][10:]); i += 1
                        continue
                    run.append(value)
                elif key not in ("env", "with"): step[key] = value.strip().strip('"')
            elif section and (m2 := re.match(r"^          ([A-Za-z0-9_-]+):\s*(.*)$", line)): step[section][m2.group(1)] = m2.group(2).strip()
            i += 1
        step["run_text"] = "\n".join(run); step["text"] = "\n".join(body)
    return steps

STEPS = parse_steps(LINES)
BY_ID = {s.get("id"): s for s in STEPS}

@R.register("the workflow file parses: twelve steps with ids in the agreed order")
def _():
    assert [s.get("id") for s in STEPS] == ORDER, [s.get("id") for s in STEPS]

@R.register("triggers: exactly the daily schedule 15 2 * * * (02:15 UTC = 06:15 Dubai) and a manual workflow_dispatch; no push/pull_request/other triggers, no manual inputs")
def _():
    on_block = TEXT.split("\npermissions:")[0]
    assert len(re.findall(r'^\s+- cron: "15 2 \* \* \*"', on_block, re.M)) == 1 and on_block.count("cron:") == 1
    assert re.search(r"^  workflow_dispatch:\s*$", on_block, re.M)
    for forbidden in ("push:", "pull_request", "workflow_run", "repository_dispatch", "inputs:"): assert forbidden not in on_block, forbidden
    assert sorted(re.findall(r"^  ([a-z_]+):", on_block, re.M)) == ["schedule", "workflow_dispatch"]

@R.register("permissions: only contents: read, at workflow level; nothing broader anywhere in the file")
def _():
    assert re.search(r"^permissions:\n  contents: read\n(?!  )", TEXT, re.M), "workflow-level permissions"
    assert len(re.findall(r"^\s*permissions:", TEXT, re.M)) == 1 and not re.search(r"(write|admin)\b", TEXT.split("jobs:")[0].split("permissions:")[1].split("\n\n")[0])
    assert not re.search(r":\s*write\b", TEXT) and "write-all" not in TEXT and "id-token" not in TEXT

@R.register("concurrency: one run at a time for this workflow, a started run is never cancelled")
def _():
    assert re.search(r"^concurrency:\n  group: [A-Za-z0-9_${{}. -]+\n  cancel-in-progress: false\n", TEXT, re.M), "concurrency block"

@R.register("runner and timeouts: a pinned Ubuntu image, a job timeout of at most 30 minutes, a timeout on every step")
def _():
    assert re.search(r"^    runs-on: ubuntu-24\.04\s*$", TEXT, re.M) and "ubuntu-latest" not in TEXT and "self-hosted" not in TEXT
    job = re.search(r"^    timeout-minutes: (\d+)\s*$", TEXT, re.M); assert job and 5 <= int(job.group(1)) <= 30
    for step in STEPS: assert step.get("timeout-minutes", "").isdigit() and 1 <= int(step["timeout-minutes"]) <= 15, (step.get("id"), step.get("timeout-minutes"))

@R.register("actions: only checkout, setup-python and upload-artifact, each pinned to a full 40-character commit SHA with a version comment (no floating tags)")
def _():
    uses = [l.strip() for l in LINES if re.match(r"^\s+(- )?uses:", l)]; assert len(uses) == 3, uses
    for line in uses:
        m = re.match(r"^(?:- )?uses: ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40}) # v\d+\.\d+\.\d+$", line); assert m, "not pinned: " + line; assert m.group(1) in ALLOWED_ACTIONS, m.group(1)
    assert {re.search(r"uses: ([^@]+)@", l).group(1) for l in uses} == ALLOWED_ACTIONS

@R.register("checkout does not leave credentials behind; Python 3.14 is requested exactly; no dependency cache is used")
def _():
    assert BY_ID["checkout"]["with"].get("persist-credentials") == "false", BY_ID["checkout"]["with"]
    assert BY_ID["setup_python"]["with"].get("python-version") == '"3.14"', BY_ID["setup_python"]["with"]
    assert "cache" not in BY_ID["setup_python"]["with"] and "actions/cache" not in TEXT

@R.register("order and commands: tests run before the analysis; the analysis is the existing src/run_workflow.py; validation, payload, post, verify and email use src/daily_automation.py in that order")
def _():
    assert re.search(r"python3 tests/run_all\.py\s*$", BY_ID["tests"]["run_text"], re.M)
    assert re.search(r"^python3 src/run_workflow\.py\s*$", BY_ID["analysis"]["run_text"], re.M)
    assert "python3 -m pip install --disable-pip-version-check -r requirements.txt" in BY_ID["install_dependencies"]["run_text"]
    assert "daily_automation.py validate --reports-dir reports" in BY_ID["validate_outputs"]["run_text"]
    build = BY_ID["build_payload"]["run_text"]; assert "daily_automation.py build-payload" in build and "--reports-dir reports" in build and "--out-dir automation_out" in build and '"$GITHUB_RUN_ID"' in build and '"$RUN_URL"' in build
    for step_id, action in (("post_to_sheet", "upsert_analysis"), ("verify_sheet", "verify_analysis"), ("send_success_email", "send_success_email")):
        assert re.search(rf"daily_automation\.py post --action {action} --dir automation_out\s*$", BY_ID[step_id]["run_text"], re.M), step_id
    for step in STEPS:
        if step["id"] in SECRET_STEPS + ["validate_outputs", "build_payload"]: assert "python3 " in step["run_text"], step["id"]

@R.register("secrets: only GOOGLE_APPS_SCRIPT_URL and GOOGLE_APPS_SCRIPT_TOKEN, only in the env of the four steps that talk to the web app; never at workflow or job level, never inside a run script")
def _():
    names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEXT)); assert names == {"GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_TOKEN"}, names
    assert "SHEETS_WEBHOOK" not in TEXT and "GITHUB_TOKEN" not in TEXT
    for step in STEPS:
        has = any("secrets." in v for v in step["env"].values())
        assert has == (step["id"] in SECRET_STEPS), step["id"]
        if has: assert step["env"].get("GOOGLE_APPS_SCRIPT_URL") == "${{ secrets.GOOGLE_APPS_SCRIPT_URL }}" and step["env"].get("GOOGLE_APPS_SCRIPT_TOKEN") == "${{ secrets.GOOGLE_APPS_SCRIPT_TOKEN }}", step["id"]
        assert "secrets." not in step["run_text"] and "${{" not in step["run_text"], step["id"] + ": expressions must reach scripts through env, not by interpolation"
    head = TEXT.split("    steps:")[0]; assert "secrets." not in head, "a secret at workflow or job level"

@R.register("scripts: no expression interpolation, no tracing, no dumping of the environment, no piping downloads into a shell, no continue-on-error")
def _():
    for step in STEPS:
        run = step["run_text"]
        for needle in ("set -x", "printenv", "env |", "| sh", "| bash", "curl ", "wget ", "echo \"$GOOGLE", "echo $GOOGLE", "--debug", "-vvv"): assert needle not in run, (step["id"], needle)
        assert re.search(r"(^|\s)env\s*$", run) is None, step["id"]
    assert "continue-on-error" not in TEXT and "pull_request_target" not in TEXT and "::set-output" not in TEXT

@R.register("the failure notice: runs only on failure, as the last step, and gets the outcome of every earlier step")
def _():
    notify = BY_ID["notify_failure"]; assert STEPS[-1] is notify and notify.get("if") == "failure()"
    for step_id in ORDER[:-1]:
        variable = "OUTCOME_" + step_id.upper(); assert notify["env"].get(variable) == "${{ steps." + step_id + ".outcome }}", (step_id, notify["env"].get(variable))
        assert f'--outcome "{step_id}=${variable}"' in notify["run_text"] or f"--outcome {step_id}=${variable}" in notify["run_text"], step_id
    assert "daily_automation.py notify-failure" in notify["run_text"] and "--dir automation_out" in notify["run_text"] and '--run-id "$GITHUB_RUN_ID"' in notify["run_text"] and '--run-url "$RUN_URL"' in notify["run_text"]

@R.register("RUN_URL is built from the GitHub context in env (server url, repository, run id) for every step that needs it")
def _():
    expected = "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
    for step_id in ("build_payload", "notify_failure"): assert BY_ID[step_id]["env"].get("RUN_URL") == expected, step_id

@R.register("artifacts: reports, CSV and the payload/result JSON are uploaded even when an earlier step failed; named by run; kept 14 days; no secrets are in the uploaded folders")
def _():
    up = BY_ID["upload_artifacts"]; assert up.get("if") == "always()" and up["uses"].startswith("actions/upload-artifact@")
    path = up["text"]; assert "reports/report_*.md" in path and "reports/report_*_data.csv" in path and "automation_out/" in path
    assert up["with"].get("name") == "daily-market-analysis-${{ github.run_id }}-${{ github.run_attempt }}" and up["with"].get("retention-days") == "14" and up["with"].get("if-no-files-found") == "warn"
    assert not up["env"], "the upload step needs no environment"

@R.register("the secret steps run only after the previous steps succeeded (default condition), so a failed validation never reaches the web app")
def _():
    for step_id in ["tests", "analysis", "validate_outputs", "build_payload", "post_to_sheet", "verify_sheet", "send_success_email"]: assert "if" not in BY_ID[step_id], step_id + " must use the default success() condition"

@R.register("the daily schedule is the only repeating trigger (the heartbeat was deferred) and the file has no tabs or trailing whitespace")
def _():
    assert TEXT.count("schedule:") == 1 and "\t" not in TEXT and not re.search(r"[ ]+$", TEXT, re.M)

R.finish()
