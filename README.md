# Bitcoin vs Ethereum: 30-day daily-return analysis

Densight Labs AI Masterclass, Assignment 1: Build a Crypto Data Workflow.

Educational project. Not financial advice. A correlation does not establish causation. This project is for education only and is not financial advice.

## The question

How has the relationship between Bitcoin and Ethereum daily price movements behaved over the most recent 30 days, and what do their correlation, volatility, and downside-risk patterns reveal?

## Run it

Developed and tested on Python 3.14.7 on Windows. The code uses `statistics.correlation`, which requires Python 3.10 or newer. Python 3.10 and 3.11 were not tested. An internet connection is required.

```bash
pip install -r requirements.txt
python src/run_workflow.py
```

`run_workflow.py` does not create the dashboard. The dashboard is a separate, manual step (see "Dashboard").

The workflow writes two files to `reports/`: `report_<date>.md` (the report) and `report_<date>_data.csv` (the 31 daily candles used, with the returns; see "The data CSV" below), where `<date>` is the last completed UTC day.

Exit code 0 means both files were written. A warning about an old `.bak` copy that could not be deleted does not change that. Exit code 1 means something failed. The workflow attempts to restore the old files; if restoration also fails, backup copies may remain next to the reports as `.bak` files.

To run only the data fetch and validation, without the analysis:

```bash
python src/fetch_data.py
```

## Data source

Kraken's public OHLC endpoint, `https://api.kraken.com/0/public/OHLC`, with `interval=1440` (one candle per UTC day) for `XBTUSD` and `ETHUSD`. No account or API key is used.

During development, the AI assistant checked the endpoint against live responses and the official documentation. The following endpoint details were recorded:

- Kraken uses "XBT" for Bitcoin in its pair names.
- The response stores candles under Kraken's pair names (`XXBTZUSD`, `XETHZUSD`), rather than necessarily using the requested pair names as response keys.
- Each candle contains `[time, open, high, low, close, vwap, volume, count]`. Prices and volume arrive as text.
- The endpoint returns up to 720 recent candles plus the current, not-yet-committed candle in the observed responses. The workflow excludes the unfinished candle.

### Kraken API exploration notes

The development investigation examined Kraken's public OHLC endpoint for the `XBTUSD` and `ETHUSD` trading pairs. It identified the response structure, the returned pair keys, the candle fields, and the need to exclude the unfinished daily candle before analysis.

The workflow uses the retrieved and validated data to calculate daily returns, correlation, volatility, and downside-risk measures. API responses must be validated before being used in calculations.

These notes describe findings recorded during development; they should not be interpreted as a claim that every API investigation step was personally performed by the learner.

## Method

1. Fetch both pairs and validate numeric fields, consecutive days, duplicate records, identical dates, and at least 31 completed candles.
2. Exclude the unfinished current UTC day.
3. Take the last 31 completed daily closes for each asset, producing 30 daily simple returns: `r_t = C_t / C_(t-1) - 1`. A gap, duplicate, misaligned date, or non-positive price stops the run; records are not silently skipped or repaired.
4. Compute the following from the 30 aligned returns:
   - Pearson correlation.
   - Daily volatility using the sample standard deviation (dividing by n - 1). Illustrative annualised volatility is the daily value multiplied by the square root of 365.
   - Downside measures: negative-return days, days when both assets fell, worst daily return, and maximum drawdown (the largest fall in closing price from its running peak across the 31 closes).

## The data CSV

`report_<date>_data.csv` has 17 columns and 31 rows, one per completed UTC day. The first row has no return, because it has no previous day. The header is exactly:

```
date,btc_close,eth_close,btc_return,eth_return,btc_open,btc_high,btc_low,btc_volume,btc_vwap,btc_trade_count,eth_open,eth_high,eth_low,eth_volume,eth_vwap,eth_trade_count
```

| Columns | Names | Origin |
|---|---|---|
| 1 | `date` | Kraken's candle time, shown as a UTC date |
| 2-3 | `btc_close`, `eth_close` | Fetched from Kraken |
| 4-5 | `btc_return`, `eth_return` | Calculated: `close / previous close - 1`. These are fractions: `0.01` means a 1% return |
| 6-11 | `btc_open`, `btc_high`, `btc_low`, `btc_volume`, `btc_vwap`, `btc_trade_count` | Fetched from Kraken (volume is in BTC) |
| 12-17 | `eth_open`, `eth_high`, `eth_low`, `eth_volume`, `eth_vwap`, `eth_trade_count` | Fetched from Kraken (volume is in ETH) |

Columns 1 to 5 are the same as in earlier versions of this file, so the spreadsheet check `=CORREL(D3:D32,E3:E32)` still works. The correlation, volatility and the other metrics in the report use the daily closing-price returns only. Open, high, low, volume, VWAP and trade count are provided for context and are not used in any calculation.

## Dashboard

`src/dashboard.py` builds one standalone HTML page from an existing report CSV. It is a separate command: `run_workflow.py` does not call it, so run the workflow first and then:

```bash
python src/dashboard.py
```

It reads the newest `reports/report_<date>_data.csv` and the matching `report_<date>.md`, and writes `reports/dashboard_<date>.html`. Optional arguments: `--reports-dir DIR` and `--out-dir DIR`. To view it, open the HTML file in a web browser (JavaScript must be enabled). The file is about 4.8 MB because it contains its own copy of Plotly.js, so it works offline and loads nothing from the internet. Exit code 0 means the dashboard was written; exit code 1 means the input or output was not usable and nothing was written. An existing dashboard is never overwritten: move or delete it, or use `--out-dir`, to build it again.

The page shows key figures, an indexed price chart, daily returns, a BTC/ETH return scatter, drawdown, and a paginated table of the CSV with export of the selected rows, for the last 7, 14 or 30 daily returns. Metrics come from the same `analysis.py` functions as the report. Returns in the CSV are fractions (0.01 = 1%); the dashboard shows them in percent. All chart data is embedded in the page.

The page header shows only the title ("Bitcoin vs. Ethereum") and the subtitle ("30-Day Market Analysis Dashboard"). A "Data provenance and methodology" section after the table lists the data source, the closing-price period, the daily-return period, the time the data was fetched (read from the report), the time the dashboard was generated, the input file name, and the API endpoint and trading pairs. A value is shown only when the data or the report supports it: the endpoint and pairs appear only when the report's own source line confirms them, and otherwise the page states that the source is not confirmed. The section also holds the method note, the limitations note, and the statement "An independent educational project, not affiliated with Kraken." The footer has exactly three lines: "Created by Rizwana Zahoor", "Bitcoin vs. Ethereum | 30-Day Market Analysis" and "Educational purposes only. Not financial advice."

The page carries a Content-Security-Policy as a meta tag, the first tag after the character-set declaration. Scripts may run only if they match the SHA-256 hash of the page's own two inline scripts (Plotly.js and the page logic); the generator computes those hashes from the exact final script text, so they are never typed in and change when the page logic changes. Nothing may connect to a network, load fonts or objects, change the base URL or submit a form. Images may only be blob images (needed for the PNG export), and inline styles are allowed because Plotly.js writes its own style sheets at run time. `eval` and script `unsafe-inline` are not allowed. A meta tag cannot set `frame-ancestors` or `sandbox`, and a policy cannot stop script-initiated navigation. The Plotly.js "Share chart..." button, which can upload a chart to Plotly Cloud after a confirmation dialog, is turned off with the chart option `showSendToCloud: false`; the other chart buttons remain.

### Approved outputs

These are the exact files the 2026-10-29 test run was made against. SHA-256 fingerprints are given in full.

| File | SHA-256 |
|---|---|
| `reports/report_2026-10-27_data.csv` (17 columns, 31 data rows, closing prices 2026-09-27 to 2026-10-27) | `ead33696f1a4e3da230823967187a8c95828911aa61e8bacfa6e32862e1d87a2` |
| `reports/report_2026-10-27.md` | `f6f3636053646d61742e117ba1063bdc6fb586327dfd056463caadf1dc584b33` |
| `reports/dashboard_2026-10-27.html` (data fetched 2026-10-28 06:56:33 UTC; dashboard generated 2026-10-29 09:40:30 UTC) | `23c4a64d6030f865feaa6cfefd92e96bca295a5b3bbff3bc429cd9ce7533fd2a` |
| `src/dashboard_template.html` (the approved template: Content-Security-Policy placeholder and `showSendToCloud: false`) | `5728e45531d29029357113a3047e1fbeecfa2c3cee8224c006e8fef81a08d817` |
| `src/dashboard.py` | `ba332bf10b6173568459c6c493072453f4908e7a7ddcb0a16669e61fa9c8a332` |
| `src/vendor/plotly.min.js` | `3b6e15d45dbb7fca5bd2094291e961ddc5472cd887009e6009a56dab668d721f` |

The dashboard embeds its generation time, so building it again produces a different file hash. What is reproducible is its data: the rows, chart series and metrics are fixed by the CSV and the report. The first dashboard, generated at 2026-10-28 07:33:08 UTC (SHA-256 `b4bd3bc5f5d9f9240369ab3a835536df0f3702a3dca8f5082fd48c8ba55db2c4`), is kept outside the repository as the test reference. The current page has identical rows, chart series, metrics and Plotly.js bundle to that page; only the presentation (header, provenance section, footer), the generation time, the added endpoint and pairs fields, the Content-Security-Policy meta tag and the chart option that turns off Plotly Cloud sharing differ.

Before writing anything the generator checks the CSV (exact 17-column header, consecutive dates, numeric fields, returns equal to `close / previous close - 1`), checks that the 30-day figures agree with the Markdown report, and verifies the vendored Plotly.js file against an approved SHA-256.

**Input limits:** the CSV may be at most 256 KiB and 400 data rows, and the Markdown report at most 64 KiB. The real files are 5.5 KB with 31 rows, and 6.5 KB. Larger input is refused with an error and is never truncated.

Plotly.js 4.1.1 (MIT) is stored unmodified in `src/vendor/`; see `src/vendor/PLOTLY_LICENSE.txt` for its origin and checksums. No dependency was installed for it. `requirements.txt` is unchanged by the dashboard.

## Observation rules

Three descriptive rules use thresholds fixed before results were observed. They are conventions for this assignment, not financial standards or trading signals.

| Rule | Thresholds | Possible outcomes |
|---|---|---|
| R1 co-movement | Absolute correlation below 0.30; 0.30 up to 0.70; 0.70 or more | Weak, moderate, or strong correlation, with positive or negative direction |
| R2 relative volatility | ETH / BTC daily volatility: 1.25 or more; 0.80 or less; otherwise | ETH noticeably more volatile, noticeably less volatile, or similar |
| R3 down-day overlap | Share of down days for the asset with fewer down days that were also down days for the other: 0.75 or more; 0.50 or less; otherwise | Largely coincide, partly coincide, or often occur separately |

## How it was checked

- **Automated testing:** The final verification report recorded 172 regression checks passed for the original workflow, across unit, integration, report-content, file-recovery, and CSV-schema suites. These suites are kept outside this repository and are not included in it.
- **Mutation testing:** The final verification report recorded 56 mutants of the original workflow code caught, 0 surviving (43 from earlier checks and 13 added for the CSV change).
- **Scope of the counts:** They cover the original workflow only. Further suites written for an optional enhanced-report extension are not part of this repository and are not included in these counts.
- **Independent numerical verification:** An independent recalculation using 60-digit decimal arithmetic compared the report's metrics with the program's output.
- **Manual correlation verification:** An earlier, retired sample (`report_2026-10-26`, a five-column CSV and its report) was checked in Excel using `=CORREL(D3:D32,E3:E32)`. The result was approximately `0.926100`, consistent with that earlier report. That sample is archived outside the project and is not part of the active submission. The active `report_2026-10-27` (correlation 0.928089) was checked only by the independent recalculation above, not in Excel.
- **Dashboard tests:** The dashboard has separate suites, kept outside this repository. The full set was last run on 2026-10-29 (local time) before the final one-line sharing change: 19 of 21 suites passed with 0 failures (CSV 25; generator 47; D1 to D4 10, 10, 13 and 18, where the symlink case in D3 cannot run on this account and is not counted; guard helper 26; missing reports or output folder 6; Content-Security-Policy helper 21 and generator 15; presentation 20; analysis, report, workflow, replacement, enhanced-report and remediation suites unchanged; page logic 24 and presentation logic 10 with Node). The two approved-template suites failed at that point, as designed, because the template had changed and its new fingerprint was not yet approved. After the sharing change and the re-pinning, only these were rerun, all passing: the sharing-button test (4), the generator (47), presentation (20), Content-Security-Policy generator (15) and helper (21), guard helper (26), D1 to D4 (10, 10, 13, 18), and the template-fingerprint checks (12 of 12 mechanism, 1 of 1 live); the full set and the mutation checks were not repeated after that change. The D1 to D4 data checks compare each generated page with a preserved copy of the earlier dashboard (kept outside the repository and checked against its SHA-256 before use) and with the approved CSV line by line, not with the current dashboard. Mutation checks (before the sharing change; each mutant a deliberate single change that a test must detect): production code 50 of 54 caught (survivors: two defensive duplicates in the new policy code that I classified as behaviourally equivalent, and the removed final file-permission change, finding M37 below; one older mutant could not be applied after the template was reworded); guard helper 55 of 57 caught (one survivor compares the reference file's fingerprint by its first 32 characters, which I classified as practically equivalent; the other leaves comments in the page head unstripped, which I classified as harmless for security but a gap in the tests); missing-folder checks 7 of 7; template-fingerprint checks 8 of 8 (with the earlier fingerprint value). These counts do not include the original workflow counts above. The page was viewed in the Claude desktop app's built-in browser on Windows only, from copies served on localhost: header, provenance rows, footer, four charts, the 7, 14 and 30-day controls, the ETH toggle, paging, CSV, PNG and SVG export, hover, zoom, pan, autoscale and reset, no console errors, no policy violations, no external requests, and a 375-pixel width; the sharing button was absent on a scratch page generated from the final template. The published page was regenerated afterwards and was checked by file-level tests (valid policy hashes, data, rows and sharing option), not reopened in the browser. Loading the file directly (`file://`) could not be tested, and no screenshots could be taken.

The automated test and mutation counts above are reported verification results; they do not imply that the learner personally executed those tests.

## Known limitations

- Thirty observations from one exchange in USD form a short sample and may not be representative of broader market behaviour.
- The data fetcher (`src/fetch_data.py`) has known limitations identified during development and deliberately left unchanged:
  - It excludes the unfinished day using the computer's own UTC clock. An incorrect clock could make an unfinished candle count as complete or stop the run.
  - It has no overall download deadline.
  - It follows redirects and ignores any `Retry-After` instruction when retrying.
- The open, high, low, volume, VWAP and trade-count columns are context only. The fetcher checks that the price and volume fields are finite, non-negative numbers and that the trade count is an integer. It does not check relationships between the fields, and it does not compare the two assets' replies with each other. The published pipeline therefore still accepts a **negative trade count** (finding S3), a **high price below the low price or a close outside the high-low range** (S4), and a reply that serves the same candles for both Bitcoin and Ethereum (S6; in the original test the report showed a correlation of 1.000000). These three were reproduced offline against the published `src/fetch_data.py` with synthetic candles on 2026-10-29. Checks for them were written only into the optional `src/report_enhanced.py`, which is not part of this repository, so they do **not** protect this workflow or the dashboard.
- Annualisation assumes independent daily returns, and the observation thresholds are assignment conventions.
- Recovery failure tests used simulated errors. Actual operating-system file locks, power loss, and interruption during replacement were not verified.
- The workflow's outputs describe the selected historical sample and should not be treated as investment advice or a prediction.
- **This project is not security-cleared.** The dashboard security findings D1 to D4 were fixed test-first; D3 and D4 were accepted provisionally, not as final. Outstanding, not fixed:
  - **D5, Content-Security-Policy (implemented on 2026-10-29; limits remain, not a clearance):** the generated page now carries the policy described under "Dashboard", and the Plotly.js sharing button is turned off. It was checked in one browser engine only (Chromium in the Claude desktop app's built-in browser, served from localhost): loading the file directly (`file://`), Firefox and Safari were not tested. Remaining weaknesses: inline styles are allowed; a meta policy cannot add `frame-ancestors` or `sandbox` and cannot stop script-initiated navigation; the Plotly.js bundle's own code was not audited.
  - **D6, advisory check for the vendored Plotly.js 4.1.1 (checked, not cleared):** on 2026-10-28 (23:21 UTC) the npm registry, OSV and the GitHub Advisory Database were queried. The vendored file has the same SHA-256 and size as the `dist/plotly.min.js` that jsDelivr's package metadata records for npm `plotly.js@4.1.1`. No advisory matching version 4.1.1 was found; the two advisories recorded for the package affect only versions below 2.25.2 and below 1.16.0, so 4.1.1 is not affected by them according to those sources. Two components inside the bundle were checked by the versions found in it: MapLibre GL JS 6.9.0 (read from a license comment, not from a version property; its one recorded advisory, GHSA-jrc7-96c5-q579, reported as critical, affects 6.4.0 and earlier and is fixed in 6.4.1, so 6.9.0 is not affected according to the source) and d3 3.8.2 (no advisory found for the package in the sources checked, which is not a confirmation that it is safe). A newer plotly.js, 4.1.2, exists and its changelog lists no security fix; this repository still ships 4.1.1. **Limits:** other bundled components (for example buffer, ieee754, pad-left, repeat-string, regl and the `gl-*` shader code, topojson) could not be mapped to versions or advisories and are unreviewed; no audit of the bundle's code was performed; advisory databases lag disclosure. D5 remains open and the project is not security-cleared.
  - **Review notes F1 to F6 on the D3 temporary-file fix:** no tests for chmod or file-descriptor failures; an unexpected non-file error while writing could leave a temporary file behind; POSIX file permissions were never tested; temporary files left by a hard crash are not cleaned up; some tests depend on private names and file-name conventions; the symlink case is untested.
  - **M37:** removing the step that sets the final dashboard file's permissions is not detected by any test (finding F3, now confirmed by a mutation check). It matters only on Linux and macOS.
  - **Python dependency advisories (checked 2026-10-28, 23:21 UTC, against PyPI, OSV and the GitHub Advisory Database):** only `requests` is pinned in `requirements.txt` (`requests==2.34.2`); for that exact version no advisory was found and it is not affected according to those sources. The other versions are merely **observed** in the development environment and are not locked by this repository (there is no lock file and no hashes): urllib3 2.8.0, certifi 2026.10.23 and idna 3.20 are not affected by the advisories recorded for those packages according to the sources (2.8.0 is the first version fixed for the urllib3 advisory GHSA-vxq7-64xx-v4gw, reported as high; 2026.10.18 is the first version fixed for the certifi advisory GHSA-6q48-6c7w-9gw2, reported as critical; the idna advisories recorded affect versions below 3.15), while for charset-normalizer 3.5.2 no advisory was found for the package in the sources checked, which is not a confirmation that it is safe. **Constraint gap:** `requests` 2.34.2 still allows urllib3 1.26 or newer, certifi 2023.5.7 or newer and idna 2.5 or newer, which include versions that those advisories list as affected. A fresh install normally takes the newest versions, but `pip install -r requirements.txt` does not upgrade an older copy that is already present in an environment, and nothing in the repository enforces the safe versions. This has not been changed.
  - **Findings S1 to S8 from the project's original security review, with their scope.** The descriptions below were taken from that review's recorded findings table; S3, S4 and S6 were re-checked against the published code on 2026-10-29, while S5 and S7 are quoted from the original evidence and were not re-run.
    - **S1 and S2 do not concern the published files.** S1 (two very large volumes add up to infinity and the report prints "inf") and S2 (extreme prices raise an uncaught overflow error) were found in the optional `src/report_enhanced.py`, which is not part of this repository. They were remediated in that module (79 remediation checks passed); for S1 the check still runs after the analysis and charting, which is the "ordering" note.
    - **S3, S4 and S6 are open in the published pipeline** (see the limitation above): a negative trade count, a high price below the low price or a close outside the range, and one asset's candles served for the other. All three were rated Low in the original review; the data source is a single exchange and the run is manual. Fixing them means changing `src/fetch_data.py`, which has been deliberately left unchanged.
    - **S5, concurrent runs in one output folder can interfere (`src/run_workflow.py`):** in a simulation of 100 simultaneous runs sharing one folder, 16 exited with code 0 and 84 failed, the final folder was missing the Markdown report, and some runs that exited with code 0 had reported success over a damaged folder. Separate folders worked (100 of 100). The original rating was medium impact and low likelihood; it was tested on Windows only. Run the workflow one at a time; this is not enforced.
    - **S7, symlink behaviour is unverified (`src/run_workflow.py`):** fixed temporary file names could in theory be written through a symlink; this was never tested because the account used cannot create symlinks.
    - **S8, accepted fetcher limits (informational):** the four limits listed above (local clock trust, no overall download deadline, redirects followed, `Retry-After` ignored), which the project owner accepted.
- **Cross-platform:** the dashboard code was run and tested on Windows only. The Linux and macOS publishing branch was exercised only through a switch on Windows. Real Linux and macOS behaviour is unverified.
- The dashboard is a separate manual step. It is not part of the workflow's backup-and-replace output handling.

## Files

| Path | Purpose |
|---|---|
| `src/fetch_data.py` | Fetches and validates daily candles |
| `src/analysis.py` | Performs calculations and applies observation rules |
| `src/run_workflow.py` | Runs the workflow and writes the report |
| `src/dashboard.py` | Builds the standalone HTML dashboard (separate command) |
| `src/dashboard_template.html` | Page template used by the dashboard |
| `src/vendor/` | Vendored Plotly.js 4.1.1 and its license notice |
| `reports/` | Generated report, data CSV and dashboard |
| `requirements.txt` | Pinned dependency |
| `AI_LOG.md` | Record of AI-assisted work |

The active submission files are exactly `reports/report_2026-10-27.md`, `reports/report_2026-10-27_data.csv` and `reports/dashboard_2026-10-27.html`. The active dataset is the 17-column, 31-row CSV (closing prices 2026-09-27 to 2026-10-27). No earlier report, five-column CSV or the optional enhanced-report module (`src/report_enhanced.py`) is part of the submission.

## Assignment and publication status

Before making this repository public, confirm that the assignment's rules permit public sharing of the source code, generated reports, and AI-assisted work. Until permission is confirmed, keep the GitHub repository private. Do not publish or deploy assignment materials publicly without checking the applicable requirements. The project uses the MIT License, with the copyright notice in the root-level `LICENSE` file.
