
"""Static checks of .github/workflows/daily-market-analysis.yml.

The workflow uses a fixed two-space YAML layout, so this file uses a
small purpose-built parser rather than a YAML dependency.

These checks cannot prove that the workflow runs successfully on GitHub;
that requires a controlled Actions run after the complete test suite passes.
"""

import hashlib
import os
import re

from wf_support import Runner, REPO, load


PATH = os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml")
R = Runner("workflow file checks")
TEXT = open(PATH, encoding="utf-8", newline="").read()
LINES = TEXT.splitlines()

# Exact order from the current daily-market-analysis.yml.
# notify_failure is the final notification step, not a pipeline stage.
ORDER = [
    "checkout",
    "setup_python",
    "install_dependencies",
    "tests",
    "clear_stale_outputs",
    "analysis",
    "validate_outputs",
    "build_dashboard",
    "stage_deliverables",
    "prepare_pages",
    "publish_pages",
    "build_payload",
    "post_to_sheet",
    "verify_sheet",
    "upload_deliverables",
    "send_success_email",
    "summarize_run",
    "upload_artifacts",
    "notify_failure",
]

# These four steps are the original Stage 1 dashboard/deliverables additions.
NEW_STEPS = [
    "clear_stale_outputs",
    "build_dashboard",
    "stage_deliverables",
    "upload_deliverables",
]

SECRET_STEPS = [
    "post_to_sheet",
    "verify_sheet",
    "send_success_email",
    "notify_failure",
]

ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/upload-artifact",
    "peaceiris/actions-gh-pages",
}

PAGES_ACTION_PIN = (
    "peaceiris/actions-gh-pages@"
    "84c30a85c19949d7eee79c4ff27748b70285e453 # v4.1.0"
)

UPLOAD_ACTION_PIN = (
    "actions/upload-artifact@"
    "cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2"
)


def parse_steps(lines):
    """Parse steps from the repository's fixed two-space YAML layout."""
    start = next(
        (i for i, line in enumerate(lines) if line == "    steps:"),
        None,
    )
    assert start is not None, "no '    steps:' line at four spaces"

    steps = []
    current = None

    for line in lines[start + 1:]:
        if line.startswith("      - "):
            current = {"lines": [], "env": {}, "with": {}}
            steps.append(current)
            line = "        " + line[8:]
        elif current is None:
            continue
        elif line.startswith("      #"):
            continue
        elif line and not line.startswith("        "):
            break

        current["lines"].append(line)

    for step in steps:
        body = step["lines"]
        section = None
        run = []
        i = 0

        while i < len(body):
            line = body[i]
            match = re.match(r"^        ([A-Za-z_-]+):\s*(.*)$", line)

            if match:
                key, value = match.groups()
                section = key if key in ("env", "with") else None

                if key == "run":
                    if value in ("|", ">"):
                        i += 1
                        while (
                            i < len(body)
                            and (
                                body[i].startswith("          ")
                                or not body[i].strip()
                            )
                        ):
                            run.append(body[i][10:])
                            i += 1
                        continue
                    run.append(value)

                elif key not in ("env", "with"):
                    step[key] = value.strip().strip('"')

            elif section and (
                match2 := re.match(
                    r"^          ([A-Za-z0-9_-]+):\s*(.*)$",
                    line,
                )
            ):
                step[section][match2.group(1)] = match2.group(2).strip()

            i += 1

        step["run_text"] = "\n".join(run)
        step["text"] = "\n".join(body)

    return steps


STEPS = parse_steps(LINES)
BY_ID = {step.get("id"): step for step in STEPS}


@R.register(
    "the workflow contains all 19 steps in the agreed order, "
    "including dashboard staging and GitHub Pages publishing"
)
def _():
    actual_ids = [step.get("id") for step in STEPS]

    absent = [step_id for step_id in ORDER if step_id not in actual_ids]
    assert not absent, f"MISSING FEATURE: workflow steps not found: {absent}"

    assert actual_ids == ORDER, (
        f"Workflow step order differs from the approved order:\n"
        f"Expected: {ORDER}\n"
        f"Actual:   {actual_ids}"
    )


@R.register(
    "triggers: exactly one daily schedule at 04:00 UTC "
    "(08:00 Dubai) and manual workflow_dispatch; no other triggers or inputs"
)
def _():
    on_block = TEXT.split("\npermissions:")[0]

    assert (
        len(re.findall(r'^\s+- cron: "0 4 \* \* \*"', on_block, re.M))
        == 1
    )
    assert on_block.count("cron:") == 1
    assert re.search(r"^  workflow_dispatch:\s*$", on_block, re.M)

    for forbidden in (
        "push:",
        "pull_request",
        "workflow_run",
        "repository_dispatch",
        "inputs:",
    ):
        assert forbidden not in on_block, forbidden

    assert sorted(
        re.findall(r"^  ([a-z_]+):", on_block, re.M)
    ) == ["schedule", "workflow_dispatch"]


@R.register(
    "permissions: only contents: write at workflow level, "
    "required for publishing the gh-pages branch; no broader permissions"
)
def _():
    assert re.search(
        r"^permissions:\n  contents: write\n(?!  )",
        TEXT,
        re.M,
    ), "workflow-level contents: write permission is required"

    assert len(re.findall(r"^\s*permissions:", TEXT, re.M)) == 1
    assert "write-all" not in TEXT
    assert "id-token" not in TEXT
    assert not re.search(r"^\s+(?!contents:)[A-Za-z_-]+:\s*(?:write|admin)\s*$", TEXT, re.M)


@R.register(
    "concurrency: one run at a time for this workflow; "
    "a started run is never cancelled"
)
def _():
    assert re.search(
        r"^concurrency:\n"
        r"  group: [A-Za-z0-9_${{}. -]+\n"
        r"  cancel-in-progress: false\n",
        TEXT,
        re.M,
    ), "concurrency block"


@R.register(
    "runner and timeouts: Ubuntu 24.04, job timeout at most 30 minutes, "
    "and a timeout on every step"
)
def _():
    assert re.search(r"^    runs-on: ubuntu-24\.04\s*$", TEXT, re.M)
    assert "ubuntu-latest" not in TEXT
    assert "self-hosted" not in TEXT

    job = re.search(r"^    timeout-minutes: (\d+)\s*$", TEXT, re.M)
    assert job and 5 <= int(job.group(1)) <= 30

    for step in STEPS:
        timeout = step.get("timeout-minutes", "")
        assert timeout.isdigit() and 1 <= int(timeout) <= 15, (
            step.get("id"),
            timeout,
        )


@R.register(
    "actions: only approved checkout, setup-python, upload-artifact "
    "and GitHub Pages actions; all pinned to full commit SHAs"
)
def _():
    uses = [
        line.strip()
        for line in LINES
        if re.match(r"^\s+(- )?uses:", line)
    ]

    assert len(uses) == 5, (
        f"Expected five action references; found {len(uses)}: {uses}"
    )

    uploads = [
        line for line in uses
        if "actions/upload-artifact@" in line
    ]
    assert len(uploads) == 2
    assert len(set(uploads)) == 1, (
        "Both upload-artifact steps must use the same pin"
    )

    for line in uses:
        match = re.match(
            r"^(?:- )?uses: "
            r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
            r"@([0-9a-f]{40}) # v\d+\.\d+\.\d+$",
            line,
        )
        assert match, "Action is not pinned to a full SHA: " + line
        assert match.group(1) in ALLOWED_ACTIONS, match.group(1)

    action_names = {
        re.search(r"uses: ([^@]+)@", line).group(1)
        for line in uses
    }
    assert action_names == ALLOWED_ACTIONS

    assert BY_ID["upload_artifacts"]["uses"] == UPLOAD_ACTION_PIN
    assert BY_ID["upload_deliverables"]["uses"] == UPLOAD_ACTION_PIN
    assert BY_ID["publish_pages"]["uses"] == PAGES_ACTION_PIN


@R.register(
    "checkout does not leave credentials behind; Python 3.14 is requested "
    "exactly; no dependency cache is used"
)
def _():
    assert (
        BY_ID["checkout"]["with"].get("persist-credentials")
        == "false"
    )
    assert (
        BY_ID["setup_python"]["with"].get("python-version")
        == '"3.14"'
    )
    assert "cache" not in BY_ID["setup_python"]["with"]
    assert "actions/cache" not in TEXT


@R.register(
    "order and commands: tests run before analysis; the existing analysis "
    "script is used; validation, payload, Sheets update, read-back and "
    "success email use the expected scripts and actions"
)
def _():
    assert re.search(
        r"python3 tests/run_all\.py\s*$",
        BY_ID["tests"]["run_text"],
        re.M,
    )
    assert re.search(
        r'^python3 src/run_workflow\.py --run-id "\$GITHUB_RUN_ID" --run-attempt "\$GITHUB_RUN_ATTEMPT" --diagnostics-dir automation_out\s*$',
        BY_ID["analysis"]["run_text"],
        re.M,
    )

    assert (
        "python3 -m pip install --disable-pip-version-check "
        "-r requirements.txt"
        in BY_ID["install_dependencies"]["run_text"]
    )

    assert (
        "daily_automation.py validate --reports-dir reports --out-dir automation_out"
        in BY_ID["validate_outputs"]["run_text"]
    )

    build = BY_ID["build_payload"]["run_text"]
    assert "daily_automation.py build-payload" in build
    assert "--reports-dir reports" in build
    assert "--out-dir automation_out" in build
    assert '"$GITHUB_RUN_ID"' in build
    assert '"$RUN_URL"' in build

    for step_id, action in (
        ("post_to_sheet", "upsert_analysis"),
        ("verify_sheet", "verify_analysis"),
    ):
        assert re.search(
            rf"daily_automation\.py post --action {action} "
            rf"--dir automation_out\s*$",
            BY_ID[step_id]["run_text"],
            re.M,
        ), step_id

    assert re.search(
        r'daily_automation\.py post --action send_success_report '
        r'--dir automation_out --artifact-url "\$ARTIFACT_URL"\s*$',
        BY_ID["send_success_email"]["run_text"],
        re.M,
    )

    for step in STEPS:
        if step["id"] in SECRET_STEPS + [
            "validate_outputs",
            "build_payload",
        ]:
            assert "python3 " in step["run_text"], step["id"]


@R.register(
    "secrets: only the two Apps Script secrets and the automatically "
    "provided GitHub token; Apps Script secrets exist only in the four "
    "web-app steps and the GitHub token only in the Pages publishing action"
)
def _():
    names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEXT))

    assert names == {
        "GOOGLE_APPS_SCRIPT_URL",
        "GOOGLE_APPS_SCRIPT_TOKEN",
        "GITHUB_TOKEN",
    }, names

    assert "SHEETS_WEBHOOK" not in TEXT

    for step in STEPS:
        has_gas_secrets = any(
            key in step["env"]
            for key in (
                "GOOGLE_APPS_SCRIPT_URL",
                "GOOGLE_APPS_SCRIPT_TOKEN",
            )
        )

        assert has_gas_secrets == (step["id"] in SECRET_STEPS), (
            step["id"],
            step["env"],
        )

        if has_gas_secrets:
            assert (
                step["env"].get("GOOGLE_APPS_SCRIPT_URL")
                == "${{ secrets.GOOGLE_APPS_SCRIPT_URL }}"
            ), step["id"]

            assert (
                step["env"].get("GOOGLE_APPS_SCRIPT_TOKEN")
                == "${{ secrets.GOOGLE_APPS_SCRIPT_TOKEN }}"
            ), step["id"]

        assert "secrets." not in step["run_text"], step["id"]
        assert "${{" not in step["run_text"], (
            step["id"] + ": expressions must reach scripts through env"
        )

    pages = BY_ID["publish_pages"]
    assert (
        pages["with"].get("github_token")
        == "${{ secrets.GITHUB_TOKEN }}"
    )

    assert "GITHUB_TOKEN" not in pages["env"]
    assert "secrets.GITHUB_TOKEN" not in pages["run_text"]

    head = TEXT.split("    steps:")[0]
    assert "secrets." not in head, (
        "A secret must not be declared at workflow or job level"
    )


@R.register(
    "scripts: no expression interpolation, shell tracing, environment "
    "dumping, piped downloads, debug output or continue-on-error"
)
def _():
    for step in STEPS:
        run = step["run_text"]

        for needle in (
            "set -x",
            "printenv",
            "env |",
            "| sh",
            "| bash",
            "curl ",
            "wget ",
            'echo "$GOOGLE',
            "echo $GOOGLE",
            "--debug",
            "-vvv",
        ):
            assert needle not in run, (step["id"], needle)

        assert re.search(r"(^|\s)env\s*$", run) is None, step["id"]

    assert "continue-on-error" not in TEXT
    assert "pull_request_target" not in TEXT
    assert "::set-output" not in TEXT


@R.register(
    "the failure notice: runs only on failure, is the last step, "
    "and receives the outcome of every earlier workflow step"
)
def _():
    notify = BY_ID["notify_failure"]

    assert STEPS[-1] is notify
    assert notify.get("if") == "failure()"

    for step_id in ORDER[:-1]:
        variable = "OUTCOME_" + step_id.upper()

        assert (
            notify["env"].get(variable)
            == "${{ steps." + step_id + ".outcome }}"
        ), (
            f"{'MISSING FEATURE: ' if step_id in NEW_STEPS else ''}"
            f"failure notice has no correct {variable}"
        )

        assert (
            f'--outcome "{step_id}=${variable}"'
            in notify["run_text"]
            or f"--outcome {step_id}=${variable}"
            in notify["run_text"]
        ), step_id

    assert "daily_automation.py notify-failure" in notify["run_text"]
    assert "--dir automation_out" in notify["run_text"]
    assert '--run-id "$GITHUB_RUN_ID"' in notify["run_text"]
    assert '--run-url "$RUN_URL"' in notify["run_text"]


@R.register(
    "RUN_URL is built from GitHub context in env for every step that needs it"
)
def _():
    expected = (
        "${{ github.server_url }}/"
        "${{ github.repository }}/actions/runs/${{ github.run_id }}"
    )

    for step_id in ("build_payload", "notify_failure"):
        assert (
            BY_ID[step_id]["env"].get("RUN_URL") == expected
        ), step_id


@R.register(
    "diagnostic artifacts: reports, CSV and payload/result JSON are "
    "uploaded even after failure; named by run and attempt; kept 14 days"
)
def _():
    upload = BY_ID["upload_artifacts"]

    assert upload.get("if") == "always()"
    assert upload["uses"].startswith("actions/upload-artifact@")

    path = upload["text"]
    assert "reports/report_*.md" in path
    assert "reports/report_*_data.csv" in path
    assert "reports/report_*_manifest.json" in path
    assert "automation_out/" in path

    assert (
        upload["with"].get("name")
        == "daily-market-analysis-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert upload["with"].get("retention-days") == "14"
    assert upload["with"].get("if-no-files-found") == "warn"
    assert not upload["env"]


@R.register(
    "pipeline steps use the default success condition; failed validation "
    "must not reach the Apps Script or success-email steps"
)
def _():
    required = [
        "tests",
        "clear_stale_outputs",
        "analysis",
        "validate_outputs",
        "build_dashboard",
        "stage_deliverables",
        "prepare_pages",
        "publish_pages",
        "build_payload",
        "post_to_sheet",
        "verify_sheet",
        "upload_deliverables",
        "send_success_email",
    ]

    for step_id in required:
        assert step_id in BY_ID, (
            f"MISSING FEATURE: workflow has no step {step_id}"
        )
        assert "if" not in BY_ID[step_id], (
            step_id + " must use the default success() condition"
        )


@R.register(
    "sequence: cleanup after tests; analysis and validation before "
    "dashboard; staging before Pages and payload; Sheets read-back and "
    "deliverables upload before success email; diagnostics and failure notice last"
)
def _():
    index = {
        step.get("id"): i
        for i, step in enumerate(STEPS)
    }

    chain = [
        "tests",
        "clear_stale_outputs",
        "analysis",
        "validate_outputs",
        "build_dashboard",
        "stage_deliverables",
        "prepare_pages",
        "publish_pages",
        "build_payload",
        "post_to_sheet",
        "verify_sheet",
        "upload_deliverables",
        "send_success_email",
        "upload_artifacts",
        "notify_failure",
    ]

    missing = [step_id for step_id in chain if step_id not in index]
    assert not missing, (
        f"MISSING FEATURE: workflow steps not found: {missing}"
    )

    positions = [index[step_id] for step_id in chain]

    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


@R.register(
    "new steps: dashboard uses the existing generator in dashboard_out; "
    "staging receives the validated date through env, not interpolation"
)
def _():
    missing = [
        step_id for step_id in NEW_STEPS
        if step_id not in BY_ID
    ]
    assert not missing, (
        f"MISSING FEATURE: workflow steps not found: {missing}"
    )

    build = BY_ID["build_dashboard"]
    stage = BY_ID["stage_deliverables"]

    assert re.search(
        r"^mkdir -p dashboard_out\s*$",
        build["run_text"],
        re.M,
    )

    assert re.search(
        r"^python3 src/dashboard\.py "
        r'--reports-dir reports --out-dir dashboard_out --date "\$ANALYSIS_DATE"\s*$',
        build["run_text"],
        re.M,
    )

    assert build["env"] == {"ANALYSIS_DATE": "${{ steps.validate_outputs.outputs.analysis_date }}"}      # the validated date is passed down, never recalculated

    assert (
        stage["env"].get("ANALYSIS_DATE")
        == "${{ steps.validate_outputs.outputs.analysis_date }}"
    )

    flat = re.sub(
        r"[ \t]*\\\n\s*",
        " ",
        stage["run_text"],
    )

    assert re.search(
        r'^python3 src/daily_automation\.py stage-deliverables '
        r'--reports-dir reports --dashboard-dir dashboard_out '
        r'--out-dir deliverables --date "\$ANALYSIS_DATE"\s*$',
        flat,
        re.M,
    ), flat


@R.register(
    "deliverables artifact: same pinned upload-artifact action, named "
    "with run id and attempt, kept 14 days, fails if missing, uploads "
    "only deliverables/, and has no environment or custom condition"
)
def _():
    assert "upload_deliverables" in BY_ID

    upload = BY_ID["upload_deliverables"]

    assert upload["uses"] == BY_ID["upload_artifacts"]["uses"]

    assert (
        upload["with"].get("name")
        == "daily-market-analysis-deliverables-${{ github.run_id }}-${{ github.run_attempt }}"
    )

    assert upload["with"].get("retention-days") == "14"
    assert upload["with"].get("if-no-files-found") == "error"
    assert upload["with"].get("path") == "deliverables/"

    assert "*" not in upload["text"]
    assert "reports/" not in upload["text"]
    assert "if" not in upload
    assert not upload["env"]


@R.register(
    "new dashboard/deliverables steps have no Apps Script secrets; "
    "GitHub Pages token is confined to the publishing action; "
    "only contents: write permission is granted"
)
def _():
    missing = [
        step_id for step_id in NEW_STEPS
        if step_id not in BY_ID
    ]
    assert not missing

    for step_id in NEW_STEPS:
        step = BY_ID[step_id]

        assert not any(
            "secrets." in value
            for value in step["env"].values()
        ), step_id

        assert "secrets." not in step["text"], step_id

    assert re.search(
        r"^permissions:\n  contents: write\n(?!  )",
        TEXT,
        re.M,
    )

    assert len(re.findall(r"^\s*permissions:", TEXT, re.M)) == 1
    assert "id-token" not in TEXT
    assert "write-all" not in TEXT


@R.register(
    "cleanup is exactly the approved command and nothing else: "
    "remove stale report, CSV and dashboard outputs"
)
def _():
    assert "clear_stale_outputs" in BY_ID

    step = BY_ID["clear_stale_outputs"]

    commands = [
        line.strip()
        for line in step["run_text"].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert commands == [
        "rm -f reports/report_*.md "
        "reports/report_*_data.csv "
        "reports/report_*_manifest.json "
        "reports/dashboard_*.html"
    ]

    assert not step["env"]
    assert "if" not in step


@R.register(
    "both upload steps use the exact approved upload-artifact pin"
)
def _():
    for step_id in ("upload_artifacts", "upload_deliverables"):
        assert BY_ID[step_id]["uses"] == UPLOAD_ACTION_PIN, (
            step_id,
            BY_ID[step_id]["uses"],
        )


@R.register(
    "artifact URL is taken from this run's deliverables upload output; "
    "it is never constructed manually and only the success email reads it"
)
def _():
    assert "upload_deliverables" in BY_ID

    code = "\n".join(
        line
        for line in TEXT.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert "/artifacts/" not in code

    artifact_steps = [
        step["id"]
        for step in STEPS
        if "ARTIFACT_URL" in step["text"]
    ]
    assert artifact_steps == ["send_success_email"]

    assert (
        code.count("steps.upload_deliverables.outputs.artifact-url")
        == 1
    )

    assert (
        "steps.upload_deliverables.outputs.artifact-url"
        in BY_ID["send_success_email"]["text"]
    )


@R.register(
    "failure handling covers every pipeline step and every stage accepted "
    "by Code.gs; client/server stage mapping is consistent"
)
def _():
    da = load("daily_automation")

    source_path = os.path.join(
        REPO,
        "apps_script",
        "Code.gs",
    )
    source = open(source_path, encoding="utf-8").read()

    match = re.search(
        r"^const STAGES = \[(.*?)\];",
        source,
        re.M | re.S,
    )
    assert match, "Could not locate the STAGES declaration in Code.gs"

    stages = re.findall(r"'([a-z_]+)'", match.group(1))
    notify = BY_ID["notify_failure"]

    flattened_run = re.sub(
        r"[ \t]*\\\n\s*",
        " ",
        notify["run_text"],
    )

    named = re.findall(
        r'--outcome "([a-z_]+)=\$OUTCOME_([A-Z_]+)"',
        flattened_run,
    )

    expected_pipeline_steps = ORDER[:-1]

    assert (
        [name for name, _variable in named]
        == da.STAGE_ORDER
        == expected_pipeline_steps
    ), (
        "Failure notification, Python STAGE_ORDER and workflow order "
        "must match exactly.\n"
        f"Notification: {[name for name, _ in named]}\n"
        f"Python:       {da.STAGE_ORDER}\n"
        f"Workflow:     {expected_pipeline_steps}"
    )

    for name, variable in named:
        assert variable == name.upper(), name

        assert (
            notify["env"].get("OUTCOME_" + variable)
            == "${{ steps." + name + ".outcome }}"
        ), name

    for name in da.STAGE_ORDER:
        server_stage = da.SERVER_STAGE.get(name, name)

        assert server_stage in stages, (
            f"{name} maps to {server_stage!r}, "
            f"which Code.gs does not accept. "
            f"Accepted stages: {stages}"
        )

    assert notify.get("if") == "failure()"
    assert BY_ID["upload_artifacts"].get("if") == "always()"


@R.register(
    "no accidental dashboard change: template, generator, vendored Plotly "
    "file and committed dashboard retain their approved SHA-256 hashes"
)
def _():
    pins = {
        "src/dashboard_template.html":
            "5728e45531d29029357113a3047e1fbeecfa2c3cee8224c006e8fef81a08d817",
        "src/dashboard.py":
            "1805895a4b361edb520844ef3ad983be9d7e44d4d921014d1dcc670057f89cdb",       # re-approved: shared candle rules, --date, byte-exact reading for the manifest hashes
        "src/vendor/plotly.min.js":
            "3b6e15d45dbb7fca5bd2094291e961ddc5472cd887009e6009a56dab668d721f",
        "reports/dashboard_2026-10-27.html":
            "23c4a64d6030f865feaa6cfefd92e96bca295a5b3bbff3bc429cd9ce7533fd2a",
    }

    for path, pin in pins.items():
        full_path = os.path.join(REPO, *path.split("/"))

        actual = hashlib.sha256(
            open(full_path, "rb").read()
        ).hexdigest()

        assert actual == pin, f"{path} changed: {actual}"


@R.register(
    "the daily schedule is the only repeating trigger; "
    "the workflow contains no tabs or trailing whitespace"
)
def _():
    assert TEXT.count("schedule:") == 1
    assert "\t" not in TEXT
    assert not re.search(r"[ ]+$", TEXT, re.M)


# ---------------------------------------------------------------------
# Stage 4: success email sends the HTML report with the artifact link
# produced by this run's deliverables upload.


@R.register(
    "success report step: same step id and Apps Script secrets; "
    "artifact link comes from this run's deliverables upload through env; "
    "uses python3 -I -S and no longer starts the old email action"
)
def _():
    step = BY_ID["send_success_email"]

    assert (
        step["env"].get("ARTIFACT_URL")
        == "${{ steps.upload_deliverables.outputs.artifact-url }}"
    )

    assert (
        step["env"].get("GOOGLE_APPS_SCRIPT_URL")
        == "${{ secrets.GOOGLE_APPS_SCRIPT_URL }}"
    )

    assert (
        step["env"].get("GOOGLE_APPS_SCRIPT_TOKEN")
        == "${{ secrets.GOOGLE_APPS_SCRIPT_TOKEN }}"
    )

    assert set(step["env"]) == {
        "GOOGLE_APPS_SCRIPT_URL",
        "GOOGLE_APPS_SCRIPT_TOKEN",
        "ARTIFACT_URL",
    }

    run = step["run_text"].strip()

    assert run == (
        'python3 -I -S src/daily_automation.py post '
        '--action send_success_report --dir automation_out '
        '--artifact-url "$ARTIFACT_URL"'
    ), run

    assert "${{" not in step["run_text"]
    assert "send_success_email --dir" not in TEXT
    assert "--action send_success_email" not in TEXT

    others = [
        step["id"]
        for step in STEPS
        if "ARTIFACT_URL" in step["env"]
    ]
    assert others == ["send_success_email"]


@R.register(
    "success report step: runs after analysis, validation, dashboard, "
    "staging, Pages publishing, Sheets update/read-back and deliverables upload"
)
def _():
    index = {
        step.get("id"): i
        for i, step in enumerate(STEPS)
    }

    earlier_steps = (
        "analysis",
        "validate_outputs",
        "build_dashboard",
        "stage_deliverables",
        "prepare_pages",
        "publish_pages",
        "build_payload",
        "post_to_sheet",
        "verify_sheet",
        "upload_deliverables",
    )

    for earlier in earlier_steps:
        assert index[earlier] < index["send_success_email"], earlier

    assert "if" not in BY_ID["send_success_email"]
    assert BY_ID["upload_deliverables"].get("id") == "upload_deliverables"

    upload = BY_ID["upload_deliverables"]

    assert (
        upload["with"].get("path") == "deliverables/"
        or "deliverables/" in upload["text"]
    )


@R.register(
    "failure notice still sees the success email under its original step "
    "name and maps it to the original Apps Script stage"
)
def _():
    da = load("daily_automation")
    notify = BY_ID["notify_failure"]

    assert (
        notify["env"].get("OUTCOME_SEND_SUCCESS_EMAIL")
        == "${{ steps.send_success_email.outcome }}"
    )

    assert (
        "send_success_email=$OUTCOME_SEND_SUCCESS_EMAIL"
        in notify["run_text"]
    )

    assert "send_success_email" in da.STAGE_ORDER

    assert (
        da.SERVER_STAGE.get(
            "send_success_email",
            "send_success_email",
        )
        == "send_success_email"
    )


R.finish()
