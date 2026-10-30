"""Run every test suite of the project and stop with a non-zero exit code if any suite fails.

    python tests/run_all.py

The Python suites use only the standard library plus the project's own requirements; the Apps Script suite needs Node.js 18 or newer (no packages).
Nothing here uses the network or any real credential.
"""
import os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PYTHON_SUITES = ["test_analysis_units.py", "test_report_content.py", "test_workflow_integration.py", "test_replace_failures.py", "test_csv_17col.py", "test_daily_automation.py", "test_workflow_yaml.py", "test_python_isolation.py", "test_contract_end_to_end.py", "test_workflow_expressions.py", "test_line_endings.py"]
NODE_SUITES = [os.path.join("apps_script", "test_code_gs.mjs")]
SUITE_TIMEOUT_SECONDS = 600


def run(command, label):
    print(f"\n===== {label} =====", flush=True)
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        completed = subprocess.run(command, cwd=REPO, env=environment, timeout=SUITE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: {label} did not finish within {SUITE_TIMEOUT_SECONDS} seconds", flush=True)
        return False
    return completed.returncode == 0


def main():
    results = {}
    for name in PYTHON_SUITES:
        results[name] = run([sys.executable, os.path.join("tests", name)], name)
    node = shutil.which("node")
    for name in NODE_SUITES:
        if node is None:
            print(f"\nERROR: Node.js was not found, so {name} cannot run", file=sys.stderr)
            results[name] = False
        else:
            results[name] = run([node, os.path.join("tests", name)], name)
    print("\n===== summary =====")
    for name, ok in results.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    failed = [name for name, ok in results.items() if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
