"""Static checks of the GitHub Actions expressions and of the YAML layout of .github/workflows/daily-market-analysis.yml.
No YAML parser and no actionlint are installed here (and nothing may be installed without approval), so these are strict checks written for the fixed two-space layout this file uses.
They reduce the risk of a broken workflow; they cannot replace GitHub's own validation, which happens when the file is pushed."""
import os, re

from wf_support import Runner, REPO

PATH = os.path.join(REPO, ".github", "workflows", "daily-market-analysis.yml")
TEXT = open(PATH, encoding="utf-8", newline="").read(); LINES = TEXT.splitlines()
R = Runner("workflow expression checks")
EXPRESSION = re.compile(r"\$\{\{\s*([^{}]*?)\s*\}\}")
ALLOWED = [re.compile(p) for p in (r"secrets\.(GOOGLE_APPS_SCRIPT_URL|GOOGLE_APPS_SCRIPT_TOKEN)", r"github\.(server_url|repository|run_id|run_attempt)", r"steps\.([a-z_]+)\.outcome", r"steps\.validate_outputs\.outputs\.analysis_date", r"steps\.upload_deliverables\.outputs\.artifact-url")]

def step_blocks():
    return re.split(r"\n(?=      - name: )", TEXT.split("    steps:\n", 1)[1])
STEP_IDS = [re.search(r"^        id: (\S+)", b, re.M).group(1) for b in step_blocks()]

def expression_sites():
    """(line number, step index, section, key, expression) for every expression; section is the 8-space key the line belongs to (env, with, run ...)."""
    sites, step_index, section = [], -1, None
    in_steps = False
    for number, line in enumerate(LINES, 1):
        if line == "    steps:": in_steps = True; continue
        if not in_steps:
            if "${{" in line: sites.append((number, -1, "outside-steps", None, line))
            continue
        if line.startswith("      - name: "): step_index += 1; section = "name"
        elif (m := re.match(r"^        ([A-Za-z_-]+):", line)): section = m.group(1)
        key = (re.match(r"^\s+- ?([A-Za-z_-]+):|^\s+([A-Za-z_-]+):", line) or [None, None, None])
        key = key[1] or key[2]
        for expression in EXPRESSION.findall(line): sites.append((number, step_index, section, key, expression))
        if "${{" in line and not EXPRESSION.search(line): sites.append((number, step_index, section, key, "UNCLOSED"))
    return sites

@R.register("expressions: every ${{ }} is opened and closed once on its line, and is one of the allowed forms (secrets.<the two retained names>, github.<server_url|repository|run_id|run_attempt>, steps.<id>.outcome, steps.validate_outputs.outputs.analysis_date, steps.upload_deliverables.outputs.artifact-url)")
def _():
    for number, line in enumerate(LINES, 1):
        stripped = EXPRESSION.sub("", line); assert "${{" not in stripped and "}}" not in stripped, f"line {number}: unbalanced expression: {line.strip()}"
    found = [e for *_rest, e in expression_sites()]; assert found, "no expressions found"
    for expression in found: assert any(p.fullmatch(expression) for p in ALLOWED), f"expression not allowed: {expression!r}"
    assert {e for e in found if e.startswith("secrets.")} == {"secrets.GOOGLE_APPS_SCRIPT_URL", "secrets.GOOGLE_APPS_SCRIPT_TOKEN"}

@R.register("expressions sit only where GitHub evaluates them: in a step's env values and in the artifact name - never in run scripts, uses, id, name or if")
def _():
    for number, index, section, key, expression in expression_sites():
        assert section in ("env", "with"), f"line {number}: expression {expression!r} in '{section}'"
        if section == "with": assert key == "name", f"line {number}: expression in with.{key}"

@R.register("step references: every steps.<id>.outcome and steps.<id>.outputs.<name> names a step that exists AND comes earlier in the job (GitHub cannot read a later step); the only outputs read are validate_outputs.analysis_date and upload_deliverables.artifact-url; sixteen steps")
def _():
    outputs = set()
    for number, index, section, key, expression in expression_sites():
        m = re.fullmatch(r"steps\.([a-z_]+)\.(?:outcome|outputs\.([a-z_-]+))", expression)
        if m:
            assert m.group(1) in STEP_IDS, f"line {number}: unknown step {m.group(1)}"; assert STEP_IDS.index(m.group(1)) < index, f"line {number}: step {m.group(1)} is not before step {STEP_IDS[index]}"
            if m.group(2): outputs.add((m.group(1), m.group(2)))
    assert outputs == {("validate_outputs", "analysis_date"), ("upload_deliverables", "artifact-url")}, f"MISSING FEATURE: the staging step must read analysis_date and the e-mail step the artifact-url (found {sorted(outputs)})"
    assert len(STEP_IDS) == len(set(STEP_IDS)) == 16, STEP_IDS

@R.register("conditions: the only if: conditions are always() on the artifact upload and failure() on the failure notice (nothing else is conditional)")
def _():
    conditions = {}
    for block in step_blocks():
        for value in re.findall(r"^        if: (.*)$", block, re.M): conditions[re.search(r"^        id: (\S+)", block, re.M).group(1)] = value.strip()
    assert conditions == {"upload_artifacts": "always()", "notify_failure": "failure()"}, conditions

@R.register("layout: no tabs, two-space indentation everywhere, exactly the expected top-level keys, and no duplicate key in any mapping (top level, job, step, env, with)")
def _():
    assert "\t" not in TEXT
    for number, line in enumerate(LINES, 1):
        if line.strip() and not line.lstrip().startswith("#"): assert (len(line) - len(line.lstrip(" "))) % 2 == 0, f"line {number}: odd indentation"
    top = re.findall(r"^([A-Za-z_-]+):", TEXT, re.M); assert top == ["name", "on", "permissions", "concurrency", "defaults", "jobs"], top
    job = TEXT.split("    steps:\n")[0].split("jobs:\n", 1)[1]; keys = re.findall(r"^    ([A-Za-z_-]+):", job, re.M); assert keys == ["name", "runs-on", "timeout-minutes", "env"] and len(set(keys)) == len(keys), keys
    for block in step_blocks():
        keys = re.findall(r"^(?:      - |        )([A-Za-z_-]+):", block, re.M); sid = re.search(r"^        id: (\S+)", block, re.M).group(1)
        assert len(keys) == len(set(keys)), f"{sid}: duplicate key in {keys}"; assert "name" in keys and "id" in keys and ("run" in keys) != ("uses" in keys), (sid, keys)
        for mapping in ("env", "with"):
            m = re.search(rf"^        {mapping}:\n((?:          .*\n?)+)", block, re.M)
            if m: inner = re.findall(r"^          ([A-Za-z_-]+):", m.group(1), re.M); assert len(inner) == len(set(inner)), f"{sid}: duplicate key in {mapping}: {inner}"

@R.register("scripts: every run: block is a literal block (run: |) that is not empty, and its backslash continuations are followed by an indented line (no dangling continuation at the end)")
def _():
    for block in step_blocks():
        sid = re.search(r"^        id: (\S+)", block, re.M).group(1)
        if re.search(r"^        run:", block, re.M):
            m = re.search(r"^        run: \|\n((?:          .*\n?)+)", block, re.M); assert m, f"{sid}: run is not a literal block"
            script = m.group(1).rstrip("\n"); assert script.strip() and not script.rstrip().endswith("\\"), f"{sid}: empty script or dangling continuation"

@R.register("trigger: one cron entry with five valid fields (15 2 * * *) and a manual trigger without inputs; the contexts used (github.server_url, repository, run_id, run_attempt) are real context names")
def _():
    crons = re.findall(r'- cron: "([^"]+)"', TEXT); assert crons == ["15 2 * * *"], crons
    minute, hour, dom, month, dow = crons[0].split(); assert 0 <= int(minute) <= 59 and 0 <= int(hour) <= 23 and (dom, month, dow) == ("*", "*", "*")
    assert re.search(r"^  workflow_dispatch:\s*$", TEXT, re.M) and "inputs:" not in TEXT

R.finish()
