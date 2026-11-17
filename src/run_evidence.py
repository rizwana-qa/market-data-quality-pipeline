"""Run identity, the producer's validation envelope, the manifest, the consumer's verification and the failure diagnostic.

Two separate records exist, and neither depends on validating itself:
  * the PRODUCER writes the manifest (run_workflow.py): run identity, the single date and time of the run, provenance, the producer's validation envelope and the SHA-256
    of the CSV and the Markdown. The manifest is written LAST, so it exists only after both data files are in place.
  * a CONSUMER verifies (daily_automation.py): the manifest's structure, the two hashes, the dates, and a recomputation of the candle ledger from the CSV. The result is
    a separate record (consumer_verification.json); it is never written into the manifest.
A missing or invalid manifest means the run is not complete. A partial or mismatched set is never accepted. Nothing here reads the clock: callers pass the moment in.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import candle_rules as cr

SCHEMA_VERSION = 1
CALC_VERSION = "1"                      # change when a formula or a metric definition changes
MANIFEST_NAME = "report_{date}_manifest.json"
MANIFEST_PATTERN = re.compile(r"^report_(\d{4}-\d{2}-\d{2})_manifest\.json$")
MAX_MANIFEST_BYTES = 256 * 1024
DIAGNOSTIC_NAME = "validation_result.json"
VERIFICATION_NAME = "consumer_verification.json"
STAGE_PERSISTED = "outputs_persisted"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
CI_RUN_ID = re.compile(r"[0-9]{1,20}")
CI_ATTEMPT = re.compile(r"[0-9]{1,6}")
LOCAL_RUN_ID = re.compile(r"local-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
SHA256_TEXT = re.compile(r"[0-9a-f]{64}")
DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
REQUIRED_FAMILIES = ("source", "candle")
ATTESTATION = {"source": "producer-attested", "candle": "consumer-recomputed"}
SOURCE_RULES = (("FET-RESP", "response"), ("FET-PARSE", "candles"), ("FET-VALID", "history"), ("RUN-UNFIN", "unfinished"))
ALIGN_RULE = ("FET-ALIGN", "alignment")
MAX_NON_PASS_IN_DIAGNOSTIC = 200
PRECEDENCE = (cr.FAIL, cr.INCONCLUSIVE, cr.BLOCKED, cr.NOT_RUN)


class ManifestError(Exception):
    """The manifest is missing, unreadable or does not have the expected structure. The message is one line and safe to print."""


# ---------------------------------------------------------------- run identity
def resolve_identity(run_id, run_attempt, now):
    """The canonical run identity. CI passes GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT explicitly; a local run gets a unique UTC timestamp plus a random suffix.

    A retry keeps the run id and has a higher attempt; a separate execution has a new run id. Raises ValueError for a malformed id or attempt.
    """
    if run_id is None:
        if run_attempt is not None:
            raise ValueError("a run attempt needs a run id")
        return {"run_id": f"local-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}", "run_attempt": 1, "run_context": "local"}
    run_id = str(run_id)
    attempt_text = "1" if run_attempt is None else str(run_attempt)
    if CI_RUN_ID.fullmatch(run_id):
        context = "ci"
    elif LOCAL_RUN_ID.fullmatch(run_id):
        context = "local"
    else:
        raise ValueError("the run id must be 1 to 20 digits (CI) or local-<UTC timestamp>-<8 hex characters>")
    if CI_ATTEMPT.fullmatch(attempt_text) is None or int(attempt_text) < 1:
        raise ValueError("the run attempt must be a whole number of at least 1")
    return {"run_id": run_id, "run_attempt": int(attempt_text), "run_context": context}


def valid_identity(run_id, run_attempt):
    return (isinstance(run_id, str) and (CI_RUN_ID.fullmatch(run_id) is not None or LOCAL_RUN_ID.fullmatch(run_id) is not None)
            and isinstance(run_attempt, int) and not isinstance(run_attempt, bool) and run_attempt >= 1)


# ---------------------------------------------------------------- the producer's source ledger
class SourceLedger:
    """Plans the source-stage instances (each fetcher group per asset, the alignment of the two assets) before anything is fetched; results are recorded as the stages run.

    The fetcher itself is not changed: its rules are reported as GROUPS (FET-RESP response and key identity, FET-PARSE candle parsing, FET-VALID history checks, FET-ALIGN
    alignment) with the original message as evidence, and RUN-UNFIN is run_workflow's own check of the unfinished candle.
    """

    def __init__(self, assets):
        self.assets = list(assets)
        self.label = "+".join(self.assets)
        plan = [cr.instance_key(rule_id, "pair", asset, "", field) for asset in self.assets for rule_id, field in SOURCE_RULES]
        plan.append(cr.instance_key(ALIGN_RULE[0], "window", self.label, "", ALIGN_RULE[1]))
        self.ledger = cr.Ledger(plan, "source")
        self._field = dict(SOURCE_RULES)

    def passed(self, asset, rule_id, evidence):
        self.ledger.record(cr.result(rule_id, "pair", asset, "", self._field[rule_id], cr.PASS, "no problem found", evidence, evidence))

    def failed(self, asset, rule_id, message, outcome=cr.FAIL):
        self.ledger.record(cr.result(rule_id, "pair", asset, "", self._field[rule_id], outcome, "no problem found", message, message, message))

    def alignment(self, problems):
        field = ALIGN_RULE[1]
        if problems:
            text = "; ".join(problems)
            self.ledger.record(cr.result(ALIGN_RULE[0], "window", self.label, "", field, cr.FAIL, "both assets have the same completed dates", text, text, text))
        else:
            self.ledger.record(cr.result(ALIGN_RULE[0], "window", self.label, "", field, cr.PASS, "both assets have the same completed dates", "aligned", "aligned"))

    def summary(self, unjudged=cr.BLOCKED):
        return self.ledger.finalize(unjudged).summary()


def empty_candle_summary():
    """The candle family of a run that stopped before the window was known: nothing was planned, so the family is NOT RUN (never PASS)."""
    return cr.Ledger([], "candle").finalize(cr.NOT_RUN).summary()


# ---------------------------------------------------------------- the envelope
def combine(outcomes):
    for outcome in PRECEDENCE:
        if outcome in outcomes:
            return outcome
    return cr.PASS


def envelope_overall(envelope):
    """The overall outcome, recomputed from the families alone. PASS needs every required family present, nothing but the required families, and each family PASS."""
    families = envelope.get("families")
    if not isinstance(families, dict):
        return cr.NOT_RUN
    if envelope.get("catalogue_version") != cr.CATALOGUE_VERSION:       # a rule catalogue this code does not know cannot be interpreted
        return cr.INCONCLUSIVE
    if set(families) - set(REQUIRED_FAMILIES):
        return cr.FAIL
    outcomes = [cr.ledger_overall(families[name]) if name in families else cr.NOT_RUN for name in REQUIRED_FAMILIES]
    return combine(outcomes)


def build_envelope(identity, timestamp, source_summary, candle_summary):
    families = {"source": dict(source_summary, attestation=ATTESTATION["source"]), "candle": dict(candle_summary, attestation=ATTESTATION["candle"])}
    for name, summary in families.items():
        summary["overall"] = cr.ledger_overall(summary)
    envelope = {"schema_version": SCHEMA_VERSION, "catalogue_version": cr.CATALOGUE_VERSION, "run_id": identity["run_id"], "run_attempt": identity["run_attempt"],
                "timestamp": timestamp.strftime(TIME_FORMAT), "required_families": list(REQUIRED_FAMILIES), "families": families,
                "assumptions_applied": sorted({a for summary in families.values() for a in summary["assumptions_applied"]})}
    envelope["overall"] = envelope_overall(envelope)
    return envelope


# ---------------------------------------------------------------- the manifest (producer)
def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def build_manifest(identity, fetched_at, analysis_date, windows, pairs, envelope, artifacts, endpoint):
    """The manifest as plain data. `artifacts` is a list of (role, file name, bytes) for the CSV and the Markdown; the hashes are computed from exactly those bytes."""
    return {
        "schema_version": SCHEMA_VERSION, "calc_version": CALC_VERSION, "run_id": identity["run_id"], "run_attempt": identity["run_attempt"], "run_context": identity["run_context"],
        "fetched_at_utc": fetched_at.strftime(TIME_FORMAT), "analysis_date": analysis_date, "windows": windows,
        "source": {"name": "Kraken public OHLC", "endpoint": endpoint, "interval_minutes": 1440, "pairs": pairs},
        "envelope": envelope,
        "artifacts": [{"role": role, "name": name, "bytes": len(data), "sha256": sha256_hex(data)} for role, name, data in artifacts],
        "stage": STAGE_PERSISTED,
    }


def serialize(value):
    return json.dumps(value, indent=2, allow_nan=False) + "\n"


MANIFEST_KEYS = {"schema_version", "calc_version", "run_id", "run_attempt", "run_context", "fetched_at_utc", "analysis_date", "windows", "source", "envelope", "artifacts", "stage"}
ARTIFACT_KEYS = {"role", "name", "bytes", "sha256"}
PAIR_KEYS = {"pair", "asset", "expected_key", "received_candles", "completed_candles", "unfinished_candles", "window_candles"}


def _is_int(value, low=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


def parse_manifest(data):
    """The manifest from its bytes, with its structure checked. Raises ManifestError (one line) for anything that is not a complete, well-formed manifest."""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ManifestError("the manifest is empty")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"the manifest is larger than the limit of {MAX_MANIFEST_BYTES} bytes")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ManifestError("the manifest is not valid JSON text")
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ManifestError("the manifest does not have exactly the expected fields")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["stage"] != STAGE_PERSISTED:
        raise ManifestError("the manifest has an unknown schema version or is not marked as persisted outputs")
    if not valid_identity(manifest["run_id"], manifest["run_attempt"]) or manifest["run_context"] not in ("ci", "local"):
        raise ManifestError("the manifest's run identity is not valid")
    for key in ("analysis_date",):
        if not isinstance(manifest[key], str) or DATE_TEXT.fullmatch(manifest[key]) is None:
            raise ManifestError(f"the manifest's {key} is not a date")
    if not isinstance(manifest["fetched_at_utc"], str) or not manifest["fetched_at_utc"].endswith(" UTC"):
        raise ManifestError("the manifest's fetch time is not a UTC time")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or sorted(a.get("role") if isinstance(a, dict) else None for a in artifacts) != ["csv", "report"]:
        raise ManifestError("the manifest must list exactly the csv and the report artifacts")
    for entry in artifacts:
        if set(entry) != ARTIFACT_KEYS or not isinstance(entry["name"], str) or not _is_int(entry["bytes"], 1) or not isinstance(entry["sha256"], str) or SHA256_TEXT.fullmatch(entry["sha256"]) is None:
            raise ManifestError("a manifest artifact entry is not well formed")
    envelope = manifest["envelope"]
    if not isinstance(envelope, dict) or not isinstance(envelope.get("families"), dict) or set(envelope.get("families", {})) != set(REQUIRED_FAMILIES):
        raise ManifestError("the manifest's validation envelope does not hold exactly the required families")
    for name, summary in envelope["families"].items():
        for key in ("expected_instances", "evaluated", "missing", "duplicate", "unexpected", "plan_digest", "instance_digest", "rules", "non_pass", "assumptions_applied"):
            if key not in summary:
                raise ManifestError(f"the {name} family of the envelope lacks {key}")
    source = manifest["source"]
    if not isinstance(source, dict) or not isinstance(source.get("pairs"), list) or any(not isinstance(p, dict) or set(p) != PAIR_KEYS for p in source["pairs"]):
        raise ManifestError("the manifest's source section is not well formed")
    return manifest


# ---------------------------------------------------------------- the consumer's verification (a separate record)
MAX_ARTIFACT_AGE_SECONDS = 6 * 3600       # a set older than this (measured from the manifest's own fetch time) is stale and is refused


def verify_artifacts(manifest_data, csv_name, csv_bytes, report_name, report_bytes, dataset, report_fetched, expected_date, verified_at, max_age_seconds=MAX_ARTIFACT_AGE_SECONDS):
    """Check a manifest against the files actually on disk and against the CSV, and recompute the candle ledger from the CSV.

    Returns the verification record. Its overall is PASS only if every check is PASS; a check that could not run because the manifest is unusable is BLOCKED.

    The analysis date is the one the PRODUCER generated from its single clock reading and wrote into the manifest. Nothing here derives a second expected date from a clock:
    DAT-1 requires the manifest's date to be the day before its own fetch time, and MAN-AGE uses the clock only to measure how old that fetch time is (so a midnight between
    the producer and this check cannot cause a failure). `expected_date` is only an explicit date given by the caller (--date); it is compared, never calculated.
    """
    checks = []

    def add(check_id, outcome, explanation):
        checks.append({"id": check_id, "outcome": outcome, "explanation": explanation})

    ids = ("MAN-STRUCT", "MAN-HASH-CSV", "MAN-HASH-REPORT", "MAN-DATE", "MAN-FETCHED", "DAT-1", "MAN-AGE", "MAN-ENVELOPE", "CON-LEDGER", "CON-CSV")
    manifest, analysis_date, run_id, run_attempt = None, expected_date, None, None
    try:
        manifest = parse_manifest(manifest_data)
        add("MAN-STRUCT", cr.PASS, "the manifest has the expected structure")
        analysis_date, run_id, run_attempt = manifest["analysis_date"], manifest["run_id"], manifest["run_attempt"]
    except ManifestError as error:
        add("MAN-STRUCT", cr.FAIL, str(error))
    if manifest is None:
        for check_id in ids[1:]:
            add(check_id, cr.BLOCKED, "the manifest is not usable")
    else:
        by_role = {entry["role"]: entry for entry in manifest["artifacts"]}
        for check_id, role, name, data in (("MAN-HASH-CSV", "csv", csv_name, csv_bytes), ("MAN-HASH-REPORT", "report", report_name, report_bytes)):
            entry = by_role[role]
            if entry["name"] != name:
                add(check_id, cr.FAIL, f"the manifest names {entry['name']!r} for the {role}, not {name!r}")
            elif entry["bytes"] != len(data) or entry["sha256"] != sha256_hex(data):
                add(check_id, cr.FAIL, f"{name} does not match the manifest (size or SHA-256 differs): the set is partial, mixed or was changed after it was written")
            else:
                add(check_id, cr.PASS, f"{name} matches the manifest")
        last = dataset["dates"][-1]
        names_date = csv_name[len("report_"):len("report_") + 10]
        if not (manifest["analysis_date"] == last == names_date) or (expected_date is not None and expected_date != manifest["analysis_date"]):
            add("MAN-DATE", cr.FAIL, f"the analysis date differs between the manifest ({manifest['analysis_date']}), the CSV ({last}), the file names ({names_date})"
                + ("" if expected_date is None else f" and the expected date ({expected_date})"))
        else:
            add("MAN-DATE", cr.PASS, "the manifest, the CSV and the file names carry the same analysis date")
        if manifest["fetched_at_utc"] != report_fetched:
            add("MAN-FETCHED", cr.FAIL, f"the fetch time differs between the manifest ({manifest['fetched_at_utc']}) and the report ({report_fetched})")
        else:
            add("MAN-FETCHED", cr.PASS, "the manifest and the report carry the same fetch time")
        try:
            fetched = datetime.strptime(manifest["fetched_at_utc"], TIME_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            fetched = None
        if fetched is None:
            add("DAT-1", cr.FAIL, "the manifest's fetch time is not a real UTC date and time")
            add("MAN-AGE", cr.BLOCKED, "the fetch time is not usable")
        else:
            if date.fromisoformat(manifest["analysis_date"]) != fetched.date() - timedelta(days=1):
                add("DAT-1", cr.FAIL, f"the analysis date {manifest['analysis_date']} is not the day before the fetch time {manifest['fetched_at_utc']}")
            else:
                add("DAT-1", cr.PASS, "the analysis date is the last completed UTC day before the producer's fetch time")
            age = (verified_at - fetched).total_seconds()
            if age < 0:
                add("MAN-AGE", cr.FAIL, f"the fetch time {manifest['fetched_at_utc']} is in the future")
            elif age > max_age_seconds:
                add("MAN-AGE", cr.FAIL, f"the artifacts are stale: fetched {manifest['fetched_at_utc']}, more than {max_age_seconds // 3600} hours before this check")
            else:
                add("MAN-AGE", cr.PASS, f"fetched {int(age // 60)} minutes before this check (limit {max_age_seconds // 3600} hours)")
        envelope = manifest["envelope"]
        recomputed = envelope_overall(envelope)
        if envelope.get("overall") != recomputed or recomputed != cr.PASS or envelope.get("run_id") != manifest["run_id"] or envelope.get("run_attempt") != manifest["run_attempt"]:
            add("MAN-ENVELOPE", cr.FAIL, f"the producer envelope is not a complete PASS for this run (recorded {envelope.get('overall')!r}, recomputed {recomputed!r})")
        else:
            add("MAN-ENVELOPE", cr.PASS, "every required family of the producer envelope is present and PASS for this run")
        producer, consumer = envelope["families"]["candle"], dataset["candle_ledger"]
        same = all(producer[key] == consumer[key] for key in ("expected_instances", "evaluated", "missing", "duplicate", "unexpected", "plan_digest", "instance_digest"))
        if not same:
            add("CON-LEDGER", cr.FAIL, "the candle ledger recomputed from the CSV differs from the producer's (plan, counts or instance outcomes)")
        elif cr.ledger_overall(consumer) != cr.PASS:
            add("CON-LEDGER", cr.FAIL, f"the candle ledger recomputed from the CSV is {cr.ledger_overall(consumer)}, not PASS")
        else:
            add("CON-LEDGER", cr.PASS, f"all {consumer['expected_instances']} candle instances were recomputed from the CSV and match the producer")
    rows = len(dataset["dates"])                         # the dataset exists only because dashboard.parse_dataset accepted every row: real consecutive dates, valid numbers, stored returns equal to close / previous close - 1
    add("CON-CSV", cr.PASS, f"the CSV contract holds on all {rows} rows: the 17-column header, real consecutive dates, valid numbers, and {2 * (rows - 1)} stored returns equal to close / previous close - 1")
    overall = combine([c["outcome"] for c in checks])
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "run_attempt": run_attempt, "analysis_date": analysis_date, "verified_at": verified_at.strftime(TIME_FORMAT),
            "checks": checks, "overall": overall}


# ---------------------------------------------------------------- persisting a record without ever masking the pipeline's own failure
def write_json_atomic(directory, name, value):
    """Write one JSON file next to its final name and replace it in one step. Returns None on success, or one line saying why it could not be written. Never raises."""
    try:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialize(value))
            os.replace(temporary, folder / name)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return None
    except (OSError, ValueError, TypeError) as error:
        return f"{type(error).__name__}: {error}"


def write_diagnostic(directory, identity, timestamp, reason, envelope):
    """Persist the validation envelope of a run that was blocked. Best effort: returns None if written (or no directory was given), else one line saying why not.

    A diagnostic that could not be written never changes the run's own outcome; the caller prints the line as a warning. The absence of a diagnostic is never PASS.
    """
    if directory is None:
        return None
    trimmed = json.loads(json.dumps(envelope))
    for summary in trimmed["families"].values():
        if len(summary["non_pass"]) > MAX_NON_PASS_IN_DIAGNOSTIC:
            summary["non_pass_omitted"] = len(summary["non_pass"]) - MAX_NON_PASS_IN_DIAGNOSTIC
            summary["non_pass"] = summary["non_pass"][:MAX_NON_PASS_IN_DIAGNOSTIC]
    record = {"schema_version": SCHEMA_VERSION, "run_id": identity["run_id"], "run_attempt": identity["run_attempt"], "timestamp": timestamp.strftime(TIME_FORMAT),
              "run_outcome": "FAIL", "overall": trimmed["overall"], "reason": reason, "envelope": trimmed}
    return write_json_atomic(directory, DIAGNOSTIC_NAME, record)
