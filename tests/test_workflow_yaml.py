"""Static checks of .github/workflows/daily-market-analysis.yml (no YAML library: the file follows a fixed two-space layout that this parser relies on). Written BEFORE the workflow was rewritten.
These checks cannot prove the workflow runs on GitHub; that needs a real Actions run."""
import hashlib, os, re

from wf_support import Runner, REPO, load

PATH = os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml")
R = Runner("workflow file checks")
TEXT = open(PATH, encoding="utf-8", newline="").read()
LINES = TEXT.splitlines()
ORDER = ["checkout", "setup_python", "install_dependencies", "tests", "clear_stale_outputs", "analysis", "validate_outputs", "build_dashboard", "stage_deliverables", "build_payload", "post_to_sheet", "verify_sheet", "upload_deliverables", "send_success_email", "upload_artifacts", "notify_failure"]
NEW_STEPS = ["clear_stale_outputs", "build_dashboard", "stage_deliverables", "upload_deliverables"]       # the Stage 1 specification of the dashboard and deliverables integration
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

@R.register("the workflow file parses: sixteen steps with ids in the agreed order (cleanup after the tests, dashboard and staging after validation, deliverables upload after the Sheets read-back and before the success email)")
def _():
    absent = [step_id for step_id in ORDER if step_id not in {s.get("id") for s in STEPS}]; assert not absent, f"MISSING FEATURE: steps not in the workflow yet: {absent}"
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

@R.register("actions: only checkout, setup-python and upload-artifact (used twice, with the SAME pin), each pinned to a full 40-character commit SHA with a version comment (no floating tags)")
def _():
    uses = [l.strip() for l in LINES if re.match(r"^\s+(- )?uses:", l)]; assert len(uses) == 4, f"MISSING FEATURE: the deliverables upload (a second upload-artifact use) is not in the workflow yet; found {len(uses)} uses"
    uploads = [l for l in uses if "actions/upload-artifact@" in l]; assert len(uploads) == 2 and len(set(uploads)) == 1, f"MISSING FEATURE: the deliverables upload must reuse the existing pinned upload-artifact: {uploads}"
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
    for step_id, action in (("post_to_sheet", "upsert_analysis"), ("verify_sheet", "verify_analysis")):
        assert re.search(rf"daily_automation\.py post --action {action} --dir automation_out\s*$", BY_ID[step_id]["run_text"], re.M), step_id
    assert re.search(r"daily_automation\.py post --action send_success_report --dir automation_out --artifact-url \"\$ARTIFACT_URL\"\s*$", BY_ID["send_success_email"]["run_text"], re.M), "the success e-mail step runs the new action (Stage 4)"
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
        variable = "OUTCOME_" + step_id.upper(); assert notify["env"].get(variable) == "${{ steps." + step_id + ".outcome }}", f"{'MISSING FEATURE: ' if step_id in NEW_STEPS else ''}the failure notice has no correct {variable} ({notify['env'].get(variable)!r})"
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
    for step_id in ["tests", "clear_stale_outputs", "analysis", "validate_outputs", "build_dashboard", "stage_deliverables", "build_payload", "post_to_sheet", "verify_sheet", "upload_deliverables", "send_success_email"]:
        assert step_id in BY_ID, f"MISSING FEATURE: the workflow has no step {step_id}"; assert "if" not in BY_ID[step_id], step_id + " must use the default success() condition"

@R.register("sequence: cleanup after the tests and before the analysis; dashboard after the validation; staging after the dashboard and before the payload; deliverables upload after the Sheets read-back and before the success email; diagnostics and the failure notice last")
def _():
    index = {step.get("id"): i for i, step in enumerate(STEPS)}
    chain = ["tests", "clear_stale_outputs", "analysis", "validate_outputs", "build_dashboard", "stage_deliverables", "build_payload", "post_to_sheet", "verify_sheet", "upload_deliverables", "send_success_email", "upload_artifacts", "notify_failure"]
    missing = [step_id for step_id in chain if step_id not in index]; assert not missing, f"MISSING FEATURE: steps not in the workflow yet: {missing}"
    positions = [index[step_id] for step_id in chain]; assert positions == sorted(positions) and len(set(positions)) == len(positions), dict(zip(chain, positions))

@R.register("new steps: build_dashboard runs the EXISTING generator into a NEW folder (mkdir -p dashboard_out; python3 src/dashboard.py --reports-dir reports --out-dir dashboard_out); stage_deliverables gets the validated date through env, never by interpolation")
def _():
    missing = [step_id for step_id in NEW_STEPS if step_id not in BY_ID]; assert not missing, f"MISSING FEATURE: steps not in the workflow yet: {missing}"
    build, stage = BY_ID["build_dashboard"], BY_ID["stage_deliverables"]
    assert re.search(r"^mkdir -p dashboard_out\s*$", build["run_text"], re.M) and re.search(r"^python3 src/dashboard\.py --reports-dir reports --out-dir dashboard_out\s*$", build["run_text"], re.M), build["run_text"]
    assert not build["env"], "the dashboard step needs no environment"
    assert stage["env"].get("ANALYSIS_DATE") == "${{ steps.validate_outputs.outputs.analysis_date }}", stage["env"]
    flat = re.sub(r"[ \t]*\\\n\s*", " ", stage["run_text"])                       # a YAML/shell line continuation: whitespace before the backslash and indentation after it are not significant
    assert re.search(r'^python3 src/daily_automation\.py stage-deliverables --reports-dir reports --dashboard-dir dashboard_out --out-dir deliverables --date "\$ANALYSIS_DATE"\s*$', flat, re.M), flat

@R.register("deliverables artifact: the same pinned upload-artifact, named with run id AND attempt, kept 14 days, FAILS when no file is found, and uploads only the staged folder deliverables/ (no globs, no reports/ path); default success() condition, no environment")
def _():
    assert "upload_deliverables" in BY_ID, "MISSING FEATURE: the workflow has no step upload_deliverables"
    up = BY_ID["upload_deliverables"]; assert up["uses"] == BY_ID["upload_artifacts"]["uses"], "it must use the same pinned upload-artifact as the diagnostics upload"
    assert up["with"].get("name") == "daily-market-analysis-deliverables-${{ github.run_id }}-${{ github.run_attempt }}", up["with"]
    assert up["with"].get("retention-days") == "14" and up["with"].get("if-no-files-found") == "error", up["with"]
    assert up["with"].get("path") == "deliverables/" and "*" not in up["text"] and "reports/" not in up["text"], f"only the staged folder may be uploaded: {up['with'].get('path')!r}"
    assert "if" not in up and not up["env"], "no condition (success() applies) and no environment"

@R.register("no new secrets or permissions: the four new steps read no secret; the only permission is still contents: read and there is no write permission, token or id-token anywhere")
def _():
    missing = [step_id for step_id in NEW_STEPS if step_id not in BY_ID]; assert not missing, f"MISSING FEATURE: steps not in the workflow yet: {missing}"
    for step_id in NEW_STEPS: assert not any("secrets." in value for value in BY_ID[step_id]["env"].values()) and "secrets." not in BY_ID[step_id]["text"], step_id
    assert re.search(r"^permissions:\n  contents: read\n(?!  )", TEXT, re.M) and len(re.findall(r"^\s*permissions:", TEXT, re.M)) == 1 and not re.search(r":\s*write\b", TEXT) and "id-token" not in TEXT and "GITHUB_TOKEN" not in TEXT

@R.register("cleanup is EXACTLY the approved command and nothing else: rm -f reports/report_*.md reports/report_*_data.csv reports/dashboard_*.html (no environment, default success() condition)")
def _():
    assert "clear_stale_outputs" in BY_ID, "MISSING FEATURE: the workflow has no step clear_stale_outputs"
    step = BY_ID["clear_stale_outputs"]; commands = [line.strip() for line in step["run_text"].splitlines() if line.strip() and not line.strip().startswith("#")]
    assert commands == ["rm -f reports/report_*.md reports/report_*_data.csv reports/dashboard_*.html"], commands
    assert not step["env"] and "if" not in step

@R.register("both upload steps use the one approved pin, exactly: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2 (no floating tag)")
def _():
    assert "upload_deliverables" in BY_ID, "MISSING FEATURE: the workflow has no step upload_deliverables"
    pin = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2"
    for step_id in ("upload_artifacts", "upload_deliverables"): assert BY_ID[step_id]["uses"] == pin, (step_id, BY_ID[step_id]["uses"])

@R.register("the artifact URL is never built by hand: the upload step has the id upload_deliverables (the action's artifact-url output), no address with /artifacts/ is written anywhere, and exactly ONE place reads the output - the success e-mail step (changed in Stage 4: in Stage 3 nothing read it yet)")
def _():
    assert "upload_deliverables" in BY_ID, "MISSING FEATURE: the workflow has no step upload_deliverables"
    code = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert "/artifacts/" not in code, "the artifact URL must never be built by hand"
    assert [step["id"] for step in STEPS if "ARTIFACT_URL" in step["text"]] == ["send_success_email"], "only the success e-mail step may carry the artifact link"
    assert code.count("steps.upload_deliverables.outputs.artifact-url") == 1 and "steps.upload_deliverables.outputs.artifact-url" in BY_ID["send_success_email"]["text"], "only the success e-mail step may read the upload's artifact-url output"

@R.register("failure handling covers every step and every stage the real Code.gs accepts: the --outcome names equal the client's STAGE_ORDER, each value is steps.<same name>.outcome, and every name is sent as a stage that Code.gs lists")
def _():
    da = load("daily_automation"); source = open(os.path.join(REPO, "apps_script", "Code.gs"), encoding="utf-8").read()
    stages = re.findall(r"'([a-z_]+)'", re.search(r"^const STAGES = \[(.*?)\];", source, re.M).group(1)); notify = BY_ID["notify_failure"]
    named = re.findall(r'--outcome "([a-z_]+)=\$OUTCOME_([A-Z_]+)"', re.sub(r"[ \t]*\\\n\s*", " ", notify["run_text"]))
    assert [name for name, _variable in named] == da.STAGE_ORDER == ORDER[:-1], f"MISSING FEATURE: the failure notice must collect exactly the pipeline steps; it collects {[name for name, _v in named]}"
    for name, variable in named: assert variable == name.upper() and notify["env"].get("OUTCOME_" + variable) == "${{ steps." + name + ".outcome }}", name
    for name in da.STAGE_ORDER: assert da.SERVER_STAGE.get(name, name) in stages, f"{name} would be sent as {da.SERVER_STAGE.get(name, name)!r}, which Code.gs does not list"
    assert notify.get("if") == "failure()" and BY_ID["upload_artifacts"].get("if") == "always()", "the failure notice and the diagnostics upload keep their conditions"

@R.register("no accidental dashboard change: the template, the generator, the vendored Plotly file and the committed dashboard still have the approved SHA-256 hashes")
def _():
    pins = {"src/dashboard_template.html": "5728e45531d29029357113a3047e1fbeecfa2c3cee8224c006e8fef81a08d817", "src/dashboard.py": "ba332bf10b6173568459c6c493072453f4908e7a7ddcb0a16669e61fa9c8a332",
            "src/vendor/plotly.min.js": "3b6e15d45dbb7fca5bd2094291e961ddc5472cd887009e6009a56dab668d721f", "reports/dashboard_2026-10-27.html": "23c4a64d6030f865feaa6cfefd92e96bca295a5b3bbff3bc429cd9ce7533fd2a"}
    for path, pin in pins.items(): assert hashlib.sha256(open(os.path.join(REPO, *path.split("/")), "rb").read()).hexdigest() == pin, f"{path} changed"

@R.register("the daily schedule is the only repeating trigger (the heartbeat was deferred) and the file has no tabs or trailing whitespace")
def _():
    assert TEXT.count("schedule:") == 1 and "\t" not in TEXT and not re.search(r"[ ]+$", TEXT, re.M)

# ---------------------------------------------------------------- Stage 4: the success e-mail step sends the HTML report with the artifact link of THIS run's deliverables upload
@R.register("success report step: same step id and secrets as before, runs the new action with the artifact link taken from the deliverables upload of this run through env (never interpolated into the script), under python3 -I -S, and the old action is no longer started by the workflow")
def _():
    step = BY_ID["send_success_email"]; assert step["env"].get("ARTIFACT_URL") == "${{ steps.upload_deliverables.outputs.artifact-url }}", step["env"]
    assert step["env"].get("GOOGLE_APPS_SCRIPT_URL") == "${{ secrets.GOOGLE_APPS_SCRIPT_URL }}" and step["env"].get("GOOGLE_APPS_SCRIPT_TOKEN") == "${{ secrets.GOOGLE_APPS_SCRIPT_TOKEN }}" and set(step["env"]) == {"GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_TOKEN", "ARTIFACT_URL"}
    run = step["run_text"].strip(); assert run == 'python3 -I -S src/daily_automation.py post --action send_success_report --dir automation_out --artifact-url "$ARTIFACT_URL"', run
    assert "${{" not in step["run_text"] and "send_success_email --dir" not in TEXT and "--action send_success_email" not in TEXT, "the workflow must not start the old action any more"
    others = [s["id"] for s in STEPS if "ARTIFACT_URL" in s["env"]]; assert others == ["send_success_email"], others

@R.register("success report step: it comes after the artifact upload it reads (upload_deliverables) and after the Sheets read-back, so the e-mail is sent only when the analysis, the dashboard, the deliverables, the Sheets update, the read-back and the upload all succeeded")
def _():
    index = {step.get("id"): i for i, step in enumerate(STEPS)}
    for earlier in ("analysis", "validate_outputs", "build_dashboard", "stage_deliverables", "build_payload", "post_to_sheet", "verify_sheet", "upload_deliverables"): assert index[earlier] < index["send_success_email"], earlier
    assert "if" not in BY_ID["send_success_email"] and BY_ID["upload_deliverables"].get("id") == "upload_deliverables", "the e-mail step keeps the default success() condition"
    up = BY_ID["upload_deliverables"]; assert up["with"].get("path") == "deliverables/" or "deliverables/" in up["text"], "the artifact the link points to holds the three staged deliverables"

@R.register("success report step: the failure notice still sees the step under its old name (OUTCOME_SEND_SUCCESS_EMAIL) and the old stage name keeps working for the web app")
def _():
    da = load("daily_automation"); notify = BY_ID["notify_failure"]; assert notify["env"].get("OUTCOME_SEND_SUCCESS_EMAIL") == "${{ steps.send_success_email.outcome }}" and "send_success_email=$OUTCOME_SEND_SUCCESS_EMAIL" in notify["run_text"]
    assert "send_success_email" in da.STAGE_ORDER and da.SERVER_STAGE.get("send_success_email", "send_success_email") == "send_success_email"

R.finish()
