# Contracts: rules, evidence, metrics and interfaces

This document states what the pipeline guarantees, where each guarantee is enforced, and what it does not guarantee. It describes the code as it is. It is a technical control description for a small educational BTC/ETH pipeline; it is not a statement of compliance with any financial regulation, security standard or governance framework (none has been defined or assessed for this project).

Where a statement rests on an assumption or on something that was not verified, it says so.

## 1. Source-of-truth model

| Layer | Authoritative artifact | Notes |
|---|---|---|
| Validated source data | In memory in `run_workflow.main` (`completed_by_pair`) | Raw Kraken replies are not persisted. |
| Metrics | In memory, `analysis.analyse` (`results`) | Computed from closing prices only. |
| Persisted snapshot | `reports/report_<date>_data.csv` | 17 columns, 31 rows. Not the only source of truth. |
| Persisted narrative and metrics | `reports/report_<date>.md` | Holds the metrics, the rule labels and the "Data fetched" time. |
| Persisted run record | `reports/report_<date>_manifest.json` | Written last. Names the run, the date and the hashes of the CSV and the Markdown. |
| Consumers | Dashboard, Google Sheets rows, e-mail | They recompute from the CSV and cross-check the Markdown; they never trust a single file. |

The CSV and the Markdown are siblings generated from the same in-memory results. Neither is generated from the other, and a disagreement between them fails the run.

## 2. Outcomes and when the overall result is PASS

Every validation result, stage result and run result uses one vocabulary:

| Outcome | Meaning |
|---|---|
| PASS | Evaluated and satisfied |
| FAIL | Evaluated and violated |
| BLOCKED | Not evaluated because a prerequisite failed |
| NOT RUN | The stage or rule never executed |
| INCONCLUSIVE | Evaluated, but the evidence is insufficient (for example positive volume with zero trades, or a send whose outcome is unknown) |

**Missing evidence is never PASS.** A missing record, file or result is NOT RUN, BLOCKED or INCONCLUSIVE.

### The expected-instance ledger

A rule instance is one rule applied to one place: `(rule id, scope, asset, date, field)`. The ledger lists every instance that must be judged **before** judging starts, built from the window (the pairs, the 31 dates and the rule catalogue) and never from results. Each result is then counted against that plan. A rule that appears once proves nothing: every planned instance must have exactly one result.

An overall PASS requires **all** of the following:

1. The catalogue version is known and the plan is not empty.
2. Every planned instance has exactly one result (no missing, duplicate or unexpected result).
3. Every ERROR-severity instance is PASS.
4. No ERROR-severity instance is BLOCKED, NOT RUN or INCONCLUSIVE.
5. Every required family is present (`source`, `candle`).
6. The plan digest recorded by the producer equals the plan rebuilt from the CSV.
7. For the candle family, the consumer recomputed the same counts and the same instance digest from the CSV.

Precedence when not PASS: FAIL, then INCONCLUSIVE, then BLOCKED, then NOT RUN. For the example window, the candle family has 620 instances (10 rules x 2 assets x 31 days) plus the one cross-asset instance (621).

Attestation: the candle family is **consumer-recomputed** (it is a pure function of the CSV). The source family (fetch, history, alignment, unfinished candle) is **producer-attested**: the raw replies are not kept, so a consumer cannot recompute it.

## 3. Rule catalogue

| Rule id | Family | Applies to | Condition | Severity |
|---|---|---|---|---|
| FET-RESP | source | each asset | HTTP, size cap, JSON, structure, `error` field, expected Kraken pair key (`RuntimeError` from the fetcher) | ERROR |
| FET-PARSE | source | each asset | strict numeric and shape parsing of every candle (`ValueError` from the fetcher) | ERROR |
| FET-VALID | source | each asset | candle timestamps (midnight UTC, no duplicate, order, no missing day), no candle after today, at least 31 completed candles | ERROR |
| FET-ALIGN | source | both assets | the same completed dates for both | ERROR |
| RUN-UNFIN | source | each asset | at most one unfinished candle | ERROR |
| CND-PRICE-OPEN, -HIGH, -LOW, -CLOSE | candle | each candle | finite and above zero | ERROR |
| CND-VOL | candle | each candle | finite and not negative | ERROR |
| CND-COUNT | candle | each candle | whole number, not negative, not a boolean | ERROR |
| CND-ORDER | candle | each candle | high is not below low | ERROR |
| CND-RANGE-OPEN, -CLOSE | candle | each candle | open and close lie within [low, high], inclusive | ERROR |
| CND-VWAP | candle | each candle | the VWAP cases in section 4 | ERROR |
| XPR-1 | candle | the window | BTC and ETH do not carry identical data on every day (a heuristic against cross-wired replies) | ERROR |
| MAN-* , DAT-1, CON-* | consumer | each run | the verification checks in section 6 | ERROR |

The fetcher (`src/fetch_data.py`) is protected and unchanged by the evidence work; its rules are reported as the four groups above, with the original message kept as evidence.

## 4. VWAP rules and their status

| Class | Statement |
|---|---|
| Verified source behaviour | Kraken documents the candle layout `[time, open, high, low, close, vwap, volume, count]`, that the last entry is the not-yet-committed period, and that at most 720 entries are returned. |
| Observed fixture behaviour | In the saved replies (721 candles per pair) no candle has zero volume, zero VWAP or zero trades, and every candle has its VWAP within [low, high]. |
| Project assumption A1 | A day without trades is volume 0, trade count 0 and VWAP 0. This is **not** documented by Kraken and was never observed here. |
| Project assumption A2 | With positive volume, the VWAP lies within [low, high] (inclusive). It follows from what a volume-weighted average is, and is consistent with the saved replies. |
| Unverified external behaviour | What Kraken actually returns for a day without trades; whether positive volume with zero trades can occur; the decimal precision of the VWAP relative to low and high. A live capture would be needed. |

| Case | Condition | Outcome | Blocks the output |
|---|---|---|---|
| 1 | VWAP negative or not finite | FAIL | yes |
| 2 | Volume above zero, VWAP inside [low, high] | PASS | no |
| 3 | Volume above zero, VWAP 0 | FAIL | yes |
| 4 | Volume above zero, VWAP outside [low, high] | FAIL | yes |
| 5 | Volume above zero, trade count 0 | INCONCLUSIVE | yes (fail closed) |
| 6 | Volume 0, trade count 0, VWAP 0 | PASS under A1 (recorded as an applied assumption) | no |
| 7 | Volume 0, trade count above 0 | FAIL | yes |
| 8 | Volume 0, trade count 0, VWAP above 0 | FAIL | yes |

Comparison: the values are compared as `Decimal` built from the exact text the CSV stores (`repr` of the float), with no arithmetic and no tolerance. Rounding to the nearest double is monotonic, so the producer and every consumer compare identical values and floating-point representation cannot cause a false failure. A source that reports the VWAP at a different precision than low or high could still produce a failure; that would be a source-data event for manual review. No value is repaired, replaced or imputed.

## 5. Run identity and the manifest

Run identity: in CI `run_id` is `GITHUB_RUN_ID` and `run_attempt` is `GITHUB_RUN_ATTEMPT`, passed explicitly (`--run-id`, `--run-attempt`). A local run gets `local-<UTC yyyymmddThhmmssZ>-<8 hex>` and attempt 1. A retry keeps the run id with a higher attempt; a separate execution has a new run id. A local id can never be posted (the Sheets, e-mail and run-URL contracts accept digits only).

`reports/report_<date>_manifest.json` (written last) holds: schema and calculation versions, run id, attempt and context, the fetch time (the producer's single clock reading), the analysis date, the close and return windows, the provenance of each pair (expected pair key, received, completed, unfinished and window candle counts), the producer's validation envelope, and the name, size and SHA-256 of the CSV and the Markdown. It holds no Sheets or e-mail result and no result that depends on validating itself.

Transaction: the report, the CSV and the manifest are staged as `.tmp` files, read back and compared with what was meant to be written, then replaced in this order: report, CSV, manifest. An exception during replacement restores the old files (or removes the new ones). **A kill or a power loss bypasses that rollback.** What protects against it is the manifest being last: a consumer refuses a set whose manifest is missing, invalid or does not match the two hashes. A leftover `.bak` or `.tmp` file does not change the verdict.

## 6. Consumer verification (a separate record)

`daily_automation.py validate` verifies the set and writes `consumer_verification.json` (never into the manifest). Checks: `MAN-STRUCT` (structure), `MAN-HASH-CSV`, `MAN-HASH-REPORT`, `MAN-DATE` (manifest, CSV and file names agree), `MAN-FETCHED` (manifest and Markdown agree), `DAT-1` (the analysis date is the day before the manifest's own fetch time), `MAN-AGE`, `MAN-ENVELOPE` (the producer envelope is a complete PASS for this run), `CON-LEDGER` (the candle ledger recomputed from the CSV equals the producer's), `CON-CSV` (the CSV contract held on every row).

## 7. Date and freshness

`run_workflow` reads the clock once. That reading decides which candles are completed, is the "Data fetched" time, and the analysis date is the last completed UTC day. They are stored in the manifest. The validate step takes the date from the verified manifest and publishes it as the `analysis_date` step output; dashboard (`--date`), staging, GitHub Pages and payload generation (`--date`) receive it as an argument. Nothing downstream derives a second expected date from a clock.

The clock is used downstream only to measure age: the artifacts must not be older than six hours (`MAX_ARTIFACT_AGE_SECONDS`) and not from the future. Because no date is compared with a fresh clock reading, a midnight between the producer and a later step cannot cause a false failure.

Residual risks (accepted, not mitigated): the producer trusts its own UTC clock to decide which day is complete; there is no overall download deadline; redirects are followed and `Retry-After` is ignored. Apps Script (`validateAnalysisDate_`) accepts any date from 2020-01-01 up to its own last completed day: it does not itself require freshness.

## 8. Diagnostics

The validation envelope is built in memory on every run. It is persisted only when a diagnostics folder is given: CI passes `--diagnostics-dir automation_out`; a local run writes nothing unless `--diagnostics-dir` is passed. Persistence is best effort: if the folder is unavailable or the write fails, a warning is printed and the original failure and exit code are unchanged. **A blocked run does not always have a persisted diagnostic** (a crash, a kill or an unwritable folder leave none); the absence of a diagnostic is never a PASS. A successful run carries its envelope inside the manifest.

## 9. Run status

`run_status.json` (written by `daily_automation.py summarize-run`, an always-run workflow step) records one outcome per stage:

| Stage | Source of the outcome |
|---|---|
| analysis | the `analysis` step outcome |
| outputs_persisted | the `analysis` and `validate_outputs` step outcomes and `consumer_verification.json` |
| sheets_update | the `post_to_sheet` step outcome and `upsert_analysis_result.json` |
| sheets_verification | the `verify_sheet` step outcome and `verify_analysis_result.json` |
| email_payload_generation | the `build_payload` step outcome and the presence of `send_success_report.json` that matches `run_context.json` |
| email_dispatch_acceptance | the `send_success_email` step outcome and `send_success_report_result.json` |

Every payload and result must carry this run's id, attempt and analysis date (`run_context.json` records them with the SHA-256 of each payload; replies are stamped with them). A file of an earlier execution, or a file without the stamp, never gives PASS. A step skipped after an earlier failure is BLOCKED. **MailApp accepting a message is not confirmation that it reached a mailbox**; the status says "accepted by MailApp". There is no plain-text e-mail fallback in the client (the web app's own fallback, used only when the HTML message cannot be prepared before it is handed to MailApp, is unchanged).

## 10. Metric contracts

All metrics use the closing prices of the last 31 completed UTC days (30 daily returns). Definitions are those of `src/analysis.py` and are unchanged.

| Id | Metric | Function | Unit and display | Missing-data behaviour |
|---|---|---|---|---|
| M01 | Daily simple return `C_t / C_(t-1) - 1` | `analyse` | fraction; percent with 2 decimals | first row empty |
| M02 | Daily volatility (sample standard deviation, n - 1) | `daily_volatility` | fraction; 4 decimals in percent | n/a |
| M03 | Annualised volatility (daily x sqrt 365) | `annualised_volatility` | 2 decimals in percent | n/a |
| M04 | Pearson correlation of the 30 returns | `pearson_correlation` | 6 decimals | None if a series is constant |
| M05 | Volatility ratio ETH / BTC | `volatility_ratio` | 3 decimals | None if BTC volatility is zero |
| M06 | Negative days, joint negative days | `count_negative_days`, `count_joint_negative_days` | integer of 30 | n/a |
| M07 | Down-day overlap share | `classify_down_day_overlap` | 2 decimals | None if an asset had no down days |
| M08 | Maximum drawdown with dates | `max_drawdown` | 2 decimals in percent | "no fall from a peak" |
| M09 | Cumulative return | `cumulative_return` | 2 decimals in percent | n/a |
| M10 | Worst day with date | `worst_day` | 2 decimals in percent | n/a |
| M11 | Relative-performance spread | `email_report.comparison_of` | percentage points, signed, rounded to 6 decimals | n/a |

M11: `spread_pp = (ETH 30-day return - BTC 30-day return) x 100`; `stronger` is the asset with the larger 30-day return, or `equal` when the spread is below 0.005 points. The 7- and 14-day correlations are the same Pearson figure over 7 and 14 returns; they rest on few observations and are optional analyst context, never a headline. The rules R1 to R3 are classifications with thresholds fixed in `analysis.py` (0.30 and 0.70; 1.25 and 0.80; 0.75 and 0.50), not alarms and not recommendations. In the Google Sheet, "30-day volatility" is the **daily** sample standard deviation, not annualised; the sheet headings are a fixed contract and are not renamed.

## 11. The e-mail report

Payload: the `send_success_report` request carries the existing parts and a `report` with `meta`, `assets`, `pair`, `coverage`, `charts` and, new, two optional parts: `comparison` (stronger, spread, 7- and 14-day correlation) and `validation` (run id, overall, fields validated of 17, candle instances expected, evaluated and passed, verification checks, applied assumptions, up to five exceptions with rule, severity, outcome, field and reason). The web app accepts the old shape (without the two parts) and the new one. It performs contract and invariant checks only (shapes, ranges, a validation PASS is accepted only if the counts prove complete evidence, the stronger performer agrees with the sign of the spread); it calculates nothing financial.

Without a `validation` part the e-mail shows "Data completeness: N of 17 fields populated", which only says that every expected cell is a well-formed number or date; it never shows PASS. The completeness count is not a validation. The 17-row field table is no longer shown; field-level evidence is in the manifest and the diagnostics.

Deployment order (separately approved, not done by this repository): deploy the new `apps_script/Code.gs` first; then use the new client. **A new client with the old deployed script is refused at the e-mail step, after the sheet has been updated.** The repository tests prove compatibility of the repository files only, not of the deployed script.

## 12. Interfaces and boundaries

| Interface | Behaviour |
|---|---|
| Kraken `GET /0/public/OHLC` | Public, no credentials. Retried on 429 and 5xx and on connection errors, three attempts, 1 s then 2 s. 1 MiB response cap, 15 s timeout. Kraken's published rate limits were not examined. |
| Apps Script web app | POST, shared secret in the request body, exact key sets, 20,000-character request limit, same-scheme redirects only. |
| `upsert_analysis` | Idempotent per date and asset; retried only for `LOCK_TIMEOUT`, `WRITE_UNCONFIRMED`, `INTERNAL_ERROR`. |
| `verify_analysis` | Read-only. |
| `send_success_email` and `send_success_report` | One e-mail per run id and date (6 hours, shared key). `send_outcome_unknown` is never retried. A separate execution (a different run id) on the same date can send again. |
| Secrets | Read only by the `post` and `notify-failure` commands, run as `python3 -I -S`. The analysis, validation and payload steps hold none. |

## 13. What this document does not establish

Live Kraken behaviour on a day without trades; the rendering of the e-mail in Gmail or Outlook; the real `MailApp` and `CacheService` behaviour; which version of the Apps Script is deployed; symlink behaviour; behaviour on Linux and macOS outside the CI runner. These need separate, approved verification.
