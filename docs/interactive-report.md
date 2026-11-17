# Interactive market intelligence report

The daily run publishes one interactive HTML report per analysis date to GitHub Pages and links it from the success e-mail.

    https://rizwana-qa.github.io/market-data-quality-pipeline/reports/<YYYY-MM-DD>/market_intelligence_report.html

Older dates stay online (`keep_files: true`). The same file is the fourth deliverable of the workflow artifact, so it is also the fallback.

## Design is frozen

`src/interactive_report_template.html` is the approved v4 report (`tests/fixtures/market_intelligence_report_2026-11-12_v4_release.html`, SHA-256 `03e68097...f1e30ae`) with every data-dependent token replaced by a named `{{slot}}`.
Everything else (layout, CSS, JavaScript, charts, tooltips, fixed wording) is byte-for-byte v4. `src/interactive_report.py` fills the slots from one validated CSV; the same numbers feed the visible text and the embedded `report-data` JSON.
The golden test renders `tests/fixtures/report_2026-11-12_data.csv` and requires the exact bytes of the approved file.

`report_2026-11-12_data.csv` was rebuilt on 2026-11-14 from Kraken's public daily candles with the project's own fetch and CSV code. It is not the original upload; the 31 closes of both assets equal the closes embedded in the v4 report and the 12 November volumes and trade counts equal the v4 text.

## Metric definitions (all reproduce the v4 figures)

| Metric | Definition |
|---|---|
| Return over n | `close[-1] / close[-1-n] - 1`, n = 7, 15, 30 |
| RSI (14) | `100 - 100 / (1 + average gain / average loss)`, plain averages of the last 14 close-to-close changes |
| ATR (14) | plain average of the last 14 true ranges (`max(high-low, abs(high-prev close), abs(low-prev close))`); ATR / price uses the latest close |
| Realised volatility | sample standard deviation of the last n daily returns x sqrt(365); n = 14 and 30 |
| Turnover | volume x VWAP; ratios compare the latest day with the plain average of the 7 days before it |
| Average trade notional | turnover / trade count |
| Close vs VWAP | `close / VWAP - 1` |
| Close location | `(close - low) / (high - low)` as a percentile of the day's range; range / close = `(high - low) / close` |
| Maximum drawdown | lowest `close / running closing-price peak - 1` over the 31 closes; distance from the sample high and low use the latest close |
| Correlation | Pearson correlation of the 30 daily returns |

## Presentation rules that are not metrics

A ratio is highlighted from 1.25 (the project's existing "clearly higher" mark); a 30-day move is "material" from 5 %; "near" the daily low or high is the lowest or highest quarter of the range; "approximately at VWAP" is within 0.05 %;
RSI is "near the lower/upper end" at 35 or below / 65 or above; correlation strength uses the existing 0.30 and 0.70 limits. The data-dependent sentences are chosen from fixed neutral patterns by comparing the computed numbers.
Where the approved v4 wording applies it is reproduced exactly; the other branches are symmetrical descriptions of the comparison and never predict.

## Request contract of `send_success_report`

Before: exactly the eleven keys `action, token, analysis_date, rows, run_id, workflow_url, validation_status, sheet_update, summary, artifact_url, report`.
After: the same, plus at most one of `report_url` or `report_link_status`. Both are optional; any other extra key is still refused.

* `report_url` must be exactly the Pages address of the request's `analysis_date`; `report_link_status` can only be `unavailable`.
* An invalid, mismatched or conflicting link field never blocks the e-mail: it is logged by reason code only (never the value) and the e-mail carries the validated artifact link instead.
* `self_check` additionally returns `report_link_supported: true`. The client sends the new fields only when it reads exactly `true`; it still requires `report_evidence_supported`.

## Workflow

`build_interactive_report` (from the validated CSV) -> `stage_deliverables` (four files, re-rendered and compared byte for byte) -> `prepare_pages` / `publish_pages` -> sheet steps -> artifact upload ->
`verify_pages` (HTTP 200 and the same SHA-256 on the exact address, bounded retries, never fails the run) -> `send_success_email` (`--report-link`).
A failed Pages publish still stops the run, as before. If verification does not succeed the e-mail carries the artifact link and says so.

## Rollback

Apps Script: deploy the previous version of the existing web app (the URL does not change; an older script simply never receives the new fields because the client checks `report_link_supported`).
Repository: revert the change. Pages: remove the dated `market_intelligence_report.html` with a commit to `gh-pages`; other dates are untouched.
