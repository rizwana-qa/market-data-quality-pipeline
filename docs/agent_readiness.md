# Agent readiness: provenance, permissions and escalation (documentation only)

This document prepares the pipeline for a possible later phase in which an AI agent operates it. **No agent exists, and nothing in this repository builds one.** It contains no agent prompt, no agent tool, no MCP integration, no new API, no autonomous workflow and no model setting. It describes the contracts and boundaries that such an agent would have to respect. The rules and outcomes it refers to are defined in `docs/contracts.md`.

It describes technical controls. It does not claim compliance with any regulation, standard or framework.

## 1. What an agent could read, and what each record proves

| Record | Where | What it establishes |
|---|---|---|
| Manifest | `reports/report_<date>_manifest.json` | Which run produced the set, on which date and fetch time, from which pair keys and candle counts, the producer's validation envelope, and the SHA-256 of the CSV and the Markdown |
| Consumer verification | `automation_out/consumer_verification.json` | That a separate check confirmed the manifest, the hashes, the dates, the age and the candle ledger recomputed from the CSV |
| Validation diagnostic | `automation_out/validation_result.json` | Why a blocked run was blocked (only if it could be written) |
| Run context | `automation_out/run_context.json` | Which run, attempt and analysis date the payload files belong to, and their hashes |
| Result files | `automation_out/<action>_result.json` | What the web app replied, stamped with the run |
| Run status | `automation_out/run_status.json` | One explicit outcome per stage |

An agent must not infer definitions from an e-mail or from the Markdown. The authoritative definitions of rules, outcomes, metrics and interfaces are in `docs/contracts.md` and in `src/analysis.py`, `src/candle_rules.py` and `src/run_evidence.py`.

## 2. Interpreting evidence

- The outcomes are PASS, FAIL, BLOCKED, NOT RUN and INCONCLUSIVE. **Missing evidence is never PASS**: a missing record, file, stamp or result is not success.
- A file counts only if it carries this run's id, attempt and analysis date. A file from an earlier execution is stale.
- "Accepted by MailApp" is not delivery to a mailbox.
- An INCONCLUSIVE result (for example positive volume with zero trades, `send_outcome_unknown`, an unconfirmed Sheets write) must be escalated, not retried automatically and not treated as a pass.

## 3. Permission boundaries

| Tier | Actions | Approval |
|---|---|---|
| Read-only | Read the reports, the manifest, the status, the diagnostics and the documentation; run the validators on local files | none |
| Local write | Run the analysis into a temporary or explicitly named local folder | none, never into the committed `reports/` folder |
| Approval required (human) | Posting to Google Sheets; sending any e-mail; starting or re-running a GitHub Actions workflow; deploying or changing the Apps Script; changing a validator, a formula, a threshold, the rule catalogue or a protected file; live Kraken requests outside the pipeline's own fetch; deleting or overwriting outputs | explicit, per action |
| Prohibited | Reading, printing, storing or transmitting any secret; disabling or weakening a validation or a test; presenting missing or incomplete evidence as success; sending data anywhere not named in the contracts; modifying the backup copies | not available |

Protected files include `src/fetch_data.py` (its SHA-256 is pinned by tests), `src/analysis.py`, the dashboard template, the vendored Plotly.js and the committed reports.

## 4. Failure and escalation

Escalate to a human, with the evidence files, when: an overall result is FAIL or INCONCLUSIVE; a manifest, a hash or a ledger does not verify; the age check says the artifacts are stale; a run ends with a partial status (for example Sheets updated, e-mail not generated); a send outcome is unknown; a diagnostic could not be persisted for a failed run; or any approval-required action would be needed. Do not retry an external write on your own: upsert is idempotent, but e-mail duplicate protection is per run id and date.

Handling stale or invalid data: do not publish, post or send. Re-running the pipeline is a human decision.

## 5. Audit and retention

The records in section 1, the GitHub job summaries and the uploaded artifacts (kept 14 days) are the audit trail of a run. An agent must write its own actions to a log that names the run id and the evidence files it read. Raw Kraken replies are not retained by the pipeline; reproducibility rests on the CSV and Markdown hashes and the candle counts. Retaining raw replies would need a separate storage and security decision.

## 6. Open items for a future phase (not designed here)

The agent's instructions, tools, tests, guardrail enforcement and logging format; how approvals are requested and recorded; and live verification of the items listed in `docs/contracts.md`, section 13.
