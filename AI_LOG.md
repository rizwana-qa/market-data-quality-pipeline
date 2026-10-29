# AI Log

Five important prompts from this project, what the AI assistant did, and what it got wrong. Statements under "AI actions" and "AI errors" are based on the project history and should be checked against the session transcript. Statements about the learner's actions are limited to actions and decisions supported by the available project history. The later sections record the 17-column CSV change, a second live run, the standalone dashboard and its security remediation, and the open items.

## 1. Plan first, one stage at a time

**Prompt (excerpt):** "Do not generate the complete project in one step. Explain the plan first and implement one stage at a time."

**AI actions:** Queried Kraken's public endpoints read-only before proposing anything. Found that the reply keys the candles by `XXBTZUSD` / `XETHZUSD` rather than the requested names, that each pair returns 721 candles (720 completed plus the unfinished current day), and that prices arrive as text. Wrote a plan, waited for approval, and wrote code only after it.

**AI errors:** None identified in this step.

**My actions:** I followed the staged project workflow and participated in the approval process before the proposed changes were applied. The README API exploration notes should describe only the exploration I personally performed.

## 2. "Data fetched successfully"

**Prompt (excerpt):** "Please update the existing Python file by adding a print statement at the end that displays a summary or confirmation message, such as a simple 'Data fetched successfully'."

**AI actions:** Declined the literal wording, because the file then contained only a parsing function and nothing had been fetched, so the message would have been false. Offered a truthful alternative: a real fetch with error handling and a summary computed from the reply. The learner chose that option.

**AI errors:** None identified in this step.

**My actions:** The project proceeded with a workflow that reports actual fetch results and handles errors rather than relying on a success message that could be misleading.

## 3. Mandatory tests, a security review, and approval for every fix

**Prompt (excerpt):** "Every time code is developed or modified, enforce unit testing and integration testing as mandatory. Follow a test-driven development approach."

**AI actions:** For each change it wrote tests first and showed them failing, implemented, re-ran everything, and listed security findings without fixing them until approved. This led to fixes for out-of-range timestamps, unbounded error output, an unlimited response size, missing retries, and unescaped error text.

**AI errors:** A test fixture of 100,000 rows exceeded the new 1 MiB response limit and broke a test that had passed; the AI rescaled the fixture. Its first estimate for how long retries against a refused connection would take (3 to 5 seconds) was wrong: measured, it was about 9 seconds, because each refused connection takes about 2 seconds on this machine.

**My actions:** I required test-driven development, unit and integration testing, security review, and explicit approval before security fixes. I reviewed the reported verification results before authorizing the final proposed changes. The final project verification report recorded 147 regression checks passing and 43 mutants caught. These were reported test results, not tests I personally executed.

## 4. The clock-trust review

**Prompt (excerpt):** "Retain the existing code for now. Do not apply v5." (the decision that ended a long series of review prompts)

**AI actions:** Showed that a wrong computer clock can make an unfinished daily candle count as complete, designed a safeguard (v5), and audited it. The learner decided not to apply it.

**AI errors:** The first design read the clock once at program start, long before the check, which a test showed could let an unfinished candle through. A lenient date parser raised an uncaught error for non-text input, found by fuzzing. The AI also claimed that one alternative "removes dependence on the local clock", which tests disproved.

**My actions:** I instructed the AI to retain the existing code and not apply v5. The safeguard was not included in the final applied changes.

## 5. Refactor and read-only audit

**Prompt (excerpt):** "I approve refactoring A and C only, with the following conditions:"

**AI actions:** Wrote characterization tests that capture the exact output of the existing code, refactored two parts without changing any output, and ran a mutation audit of the tests.

**AI errors:** The first recorded characterization data differed between two runs because a random port number appeared in an error message; the AI normalised it. The mutation audit showed that the AI's own tests did not pin the 90-second margin value or the rule that the checked clock reading is the one used, which were reported as gaps.

**My actions:** I required a controlled refactor with explicit scope and a read-only audit. I reviewed the reported findings and required verification before the changes were accepted. The exact approval conditions should be checked against the original session transcript.

## Final project verification

The final applied candidate was reported to pass 147 regression checks, with 43 mutants caught and none surviving. The original `src/fetch_data.py` was reported unchanged by hash comparison.

The workflow was first run once, with authorization, making two live Kraken API requests (2026-10-27). The reported results were 721 candles for each trading pair, with one unfinished candle excluded, leaving 720 completed candles per pair.

The generated report's correlation was independently checked by me in Excel using:

`=CORREL(D3:D32,E3:E32)`

The result was approximately `0.926100`, consistent with the workflow report.

## Recovery review findings

Three recovery-related findings were reviewed during the final audit:

- **Backup cleanup:** If deleting backups fails after both new output files have been placed, the workflow should report the leftover backups without incorrectly treating the completed output replacement as a failed operation.
- **D1, restore attempts:** A restore failure should not prevent attempts to restore the other output file.
- **D2, recovery diagnostics:** Recovery errors should identify every backup path that remains stuck and preserve the original replacement error.

The final candidate was reported to address these findings. Regression and mutation checks were rerun against the applied project. Recovery failures were simulated in tests; actual operating-system lock failures, power loss, and interruption during replacement were not verified.

## 17-column CSV extension

**Request:** Extend the original report CSV from 5 to 17 columns: keep the existing five columns in place and append open, high, low, volume, VWAP and trade count for Bitcoin and for Ethereum. No live API calls, no change to the report text, the calculations or the date window.

**AI actions:** Checked that the six extra fields were already fetched and held in memory but were dropped when the closing prices were selected. Wrote 25 new CSV checks and updated one existing header check before changing any code. Ran them against the unchanged code: 11 of the new checks and the updated one failed, and 14 guard checks passed. Then changed only `src/run_workflow.py` (25 lines added, 5 removed): a helper that picks each asset's candles by date, the CSV writer, and two lines in `main`. `src/analysis.py` and `src/fetch_data.py` were not changed.

**Results (offline, using saved Kraken replies; no Kraken requests):** the 25 new checks pass. The 147 existing regression checks still pass, so the original workflow suites now total 172. The report text is byte-for-byte identical to the one the previous code wrote. Columns 1 to 5 equal the published 5-column sample line by line. All 12 appended columns match the raw Kraken values for the same date and asset. A 56-mutant run (43 existing and 13 new CSV mutants) caught all 56.

**AI errors:** Two checks in other test suites that pinned the old hash of `src/run_workflow.py` failed after the intended change; the AI updated those two pins to the new hash. In the mutation run, every mutant also showed one failing check caused only by a missing README file in the scratch copy; the AI recounted and confirmed that no mutant was caught only because of it.

**Not done:** The published sample report and CSV were not regenerated and still have 5 columns; that needs a separate live run.

**My actions:** I specified the 17-column layout and the limits of this change, and did not authorise live API calls or publication.

## Second live run (2026-10-28)

After the 17-column change, the original workflow was run once more with authorization (`python src/run_workflow.py`, 2026-10-28 06:56 UTC). Before the run, the PC clock was compared with the HTTP Date headers of four public websites (differences of 1.3 seconds or less; the Windows Time service was stopped), the expected output names were confirmed, and the existing sample hashes were verified. The run exited with code 0 and created `report_2026-10-27.md` and `report_2026-10-27_data.csv` (17 columns, 31 rows, 2026-09-27 to 2026-10-27). The 2026-10-26 sample pair was not changed by this run and was not part of the submission; it was retired and archived outside the project afterwards (see 'Retirement of the 2026-10-26 sample'). An independent recalculation matched the report (correlation 0.928089), and the new CSV matched the saved earlier Kraken replies on all 30 overlapping completed days. The Excel check applies only to the earlier 2026-10-26 sample; the 2026-10-27 report was checked by the independent recalculation, not in Excel.

## Standalone HTML dashboard

**Requests (in order):** an enhanced-report review and a dashboard plan (a Streamlit plus Plotly design, a dependency dry run in which nothing was installed, a saved HTML mockup checked in a browser), then an implementation prompt for a professional Bitcoin vs. Ethereum HTML dashboard using HTML, CSS, JavaScript and Plotly.js, to stop and ask before adding any dependency or using the network. I then approved a standalone HTML dashboard with conditions: Plotly.js only from a verified wheel, a standalone generation command, only the approved files, and integration into `run_workflow.py` deferred.

**AI actions:** Wrote tests first (47 generator checks, 24 page-logic checks run with Node), then `src/dashboard.py` and `src/dashboard_template.html`. Downloaded the Plotly wheel once, as approved, checked the wheel's SHA-256 against the PyPI metadata and the extracted file against the wheel's record, and stored only `plotly.min.js` (plotly.js 4.1.1, MIT) with a license notice; the Python package was not installed. Viewed the page in the Claude desktop app's built-in browser on Windows, from a copy served on localhost: the key figures matched the report, the 7, 14 and 30-day figures matched independent 60-digit calculations, the asset toggles, pagination and USD tooltip worked, and the export of all 30 days was byte-identical to the CSV. The first `reports/dashboard_2026-10-27.html` was generated on 2026-10-28 at 07:33:08 UTC; it was later replaced (see 'Integration of the dashboard refinement and regeneration').

**Scope notes:** The dashboard is a separate command. `src/run_workflow.py` does not call it and was not changed for it. The enhanced-report module (`src/report_enhanced.py`) was also built and tested during development but is not part of the submission.

**My actions:** I chose the standalone HTML approach, set the conditions for the Plotly file, deferred workflow integration, and approved each security fix below separately.

## Dashboard security remediation D1 to D4

These four dashboard findings are different from the earlier recovery findings "D1" and "D2" above. For each, I approved one finding at a time ("Proceed with ... only"). The AI wrote tests first, showed them failing against the earlier code, fixed only that finding, and re-ran everything.

- **D1, unverified Plotly file:** the vendored Plotly.js was embedded without checking it. Fix: a SHA-256 check before anything is written. Tests: 4 of 10 passed before the fix and 10 of 10 after.
- **D2, overwrite race:** an existing dashboard could be replaced if it appeared between the existence check and the write. Fix: write a temporary file, then publish it with a step that refuses an existing name (rename on Windows, hard link on Linux and macOS). Tests: 10 checks; the revised set passes 3 of 10 against the pre-fix code. The AI had to adapt some D2 tests (the simulated-failure test and the race hook were generalised, and one guard was flipped after D3); each adapted test was shown still failing against the original defect.
- **D3, predictable temporary name:** all runs shared one fixed `.tmp` name. Fix: a unique, exclusively created temporary file per attempt. Tests: 5 of 13 passed before and 13 of 13 after (the symlink case could not run on this account and is not counted). A read-only post-fix review gave PASS WITH LIMITATIONS, and I accepted D3 provisionally, with review notes F1 to F6 (see below).
- **D4, no input-size limits:** the CSV and report were read in full and any number of rows was embedded. Fix: at most 256 KiB for the CSV, 400 data rows and 64 KiB for the report; larger input is refused, never truncated. The AI explained each limit before choosing it. Evidence before the fix: a 40 MiB CSV used 240.5 MiB of Python memory just to be rejected. Tests: 5 of 18 passed before and 18 of 18 after. The same code path also now turns invalid UTF-8 and an oversized field into controlled errors. A mutation check of D4 caught 17 of 18 mutants; the survivor was reported as behaviour-equivalent. I accepted D4 provisionally. The limits are fixed constants with no override.

**AI errors:** An early D2 stress assertion was too strict and was corrected. The AI accidentally deleted a test-support file and restored it from an identical copy. A D4 test fixture (3,000 rows) was larger than the byte limit it claimed to stay under and was changed to 1,200 rows. A first D4 mutation rerun skipped every mutant because the scratch copies were already modified, and was repeated in a fresh folder. The first draft of the README's Excel statement was too broad and was limited to the earlier sample.

## Verification status

**Superseded by the 2026-10-29 results in 'Integration of the dashboard refinement and regeneration' below. Run and passed on 2026-10-28 (exit code 0):** dashboard generator 47/47, page logic (Node) 24/24, D1 10/10, D2 10/10, D3 13/13, D4 18/18; at the time of D4 the regression suites also passed (CSV 25, report content 23, workflow integration 29, file replacement 33, analysis units 62, enhanced report 43, remediation 79). Mutation: 56/56 for the original workflow; 17 of 18 for D4. **No mutation-test evidence was found for D1, D2 or D3.**

**Not run or not verified:** Linux and macOS (the hard-link branch ran only through a switch on Windows); the symlink case; browsers other than the built-in one; the Plotly.js advisory check (it needed network access; it was done later, on 2026-10-28 at 23:21 UTC, see 'Dependency advisory check' below); real operating-system lock failures, power loss and interruption (see above). These test suites are kept outside this repository, and the counts are reported results rather than tests I personally executed.

## Unresolved findings

**The project is not security-cleared.** Open and not fixed:

- **D5, Content-Security-Policy (implemented on 2026-10-29; limits remain, not a clearance):** the generated page now carries the policy described under "Dashboard", and the Plotly.js sharing button is turned off. It was checked in one browser engine only (Chromium in the Claude desktop app's built-in browser, served from localhost): loading the file directly (`file://`), Firefox and Safari were not tested. Remaining weaknesses: inline styles are allowed; a meta policy cannot add `frame-ancestors` or `sandbox` and cannot stop script-initiated navigation; the Plotly.js bundle's own code was not audited.
- **D6, advisory check for the vendored Plotly.js 4.1.1 (checked, not cleared):** on 2026-10-28 (23:21 UTC) the npm registry, OSV and the GitHub Advisory Database were queried. The vendored file has the same SHA-256 and size as the `dist/plotly.min.js` that jsDelivr's package metadata records for npm `plotly.js@4.1.1`. No advisory matching version 4.1.1 was found; the two advisories recorded for the package affect only versions below 2.25.2 and below 1.16.0, so 4.1.1 is not affected by them according to those sources. Two components inside the bundle were checked by the versions found in it: MapLibre GL JS 6.9.0 (read from a license comment, not from a version property; its one recorded advisory, GHSA-jrc7-96c5-q579, reported as critical, affects 6.4.0 and earlier and is fixed in 6.4.1, so 6.9.0 is not affected according to the source) and d3 3.8.2 (no advisory found for the package in the sources checked, which is not a confirmation that it is safe). A newer plotly.js, 4.1.2, exists and its changelog lists no security fix; this repository still ships 4.1.1. **Limits:** other bundled components (for example buffer, ieee754, pad-left, repeat-string, regl and the `gl-*` shader code, topojson) could not be mapped to versions or advisories and are unreviewed; no audit of the bundle's code was performed; advisory databases lag disclosure. D5 remains open and the project is not security-cleared.
- **F1 to F6 (D3 review notes):** no tests for chmod or file-descriptor failures; an unexpected non-file error while writing could leave a temporary file; POSIX permissions untested; temporary files left by a hard crash are not cleaned; some tests depend on private names and file-name conventions; the symlink case is untested.
- **M37:** removing the final file-permission change is not detected by any test (confirmed by a mutation check; matters only on Linux and macOS).
- **Python dependency advisories (checked 2026-10-28, 23:21 UTC, against PyPI, OSV and the GitHub Advisory Database):** only `requests` is pinned in `requirements.txt` (`requests==2.34.2`); for that exact version no advisory was found and it is not affected according to those sources. The other versions are merely **observed** in the development environment and are not locked by this repository (there is no lock file and no hashes): urllib3 2.8.0, certifi 2026.10.23 and idna 3.20 are not affected by the advisories recorded for those packages according to the sources (2.8.0 is the first version fixed for the urllib3 advisory GHSA-vxq7-64xx-v4gw, reported as high; 2026.10.18 is the first version fixed for the certifi advisory GHSA-6q48-6c7w-9gw2, reported as critical; the idna advisories recorded affect versions below 3.15), while for charset-normalizer 3.5.2 no advisory was found for the package in the sources checked, which is not a confirmation that it is safe. **Constraint gap:** `requests` 2.34.2 still allows urllib3 1.26 or newer, certifi 2023.5.7 or newer and idna 2.5 or newer, which include versions that those advisories list as affected. A fresh install normally takes the newest versions, but `pip install -r requirements.txt` does not upgrade an older copy that is already present in an environment, and nothing in the repository enforces the safe versions. This has not been changed.
- **S1 to S8 (original security review), by scope.** S1 and S2 concern the optional `src/report_enhanced.py` (not part of the repository) and were remediated there (79 remediation checks); for S1 the non-finite-volume check still runs after the analysis and charting ("ordering"). **S3 (negative trade count), S4 (high below low, or close outside the range) and S6 (one asset's candles served for the other) remain open in the published `src/fetch_data.py`**: they were reproduced offline on 2026-10-29, and the fixes written for the excluded module do not protect the published pipeline. **S5:** concurrent runs sharing one output folder can interfere (a 100-run simulation: 84 failures and a missing report in the shared folder; separate folders 100 of 100; Windows only). **S7:** a symlink at a fixed temporary file name is unverified (no symlink privilege). **S8:** the four accepted fetcher limits (clock trust, no overall deadline, redirects, `Retry-After`).

## Retirement of the 2026-10-26 sample (2026-10-28)

**Decision.** The user decided that the obsolete five-column CSV (`reports/report_2026-10-26_data.csv`) and its report (`reports/report_2026-10-26.md`) must not remain in the active project, and authorised their removal after a read-only dependency review, an archive and passing tests. The approved dataset is `reports/report_2026-10-27_data.csv` (17 columns, 31 data rows, 2026-09-27 to 2026-10-27), with `reports/report_2026-10-27.md` and `reports/dashboard_2026-10-27.html`.

**Dependencies found.** No file in `src/`, the README or the dashboard depended on the old files. Two tests, kept outside the project, read the old CSV from the project folder: the 17-column CSV suite (it compared columns 1 to 5 with the "published sample") and the dashboard suite (it used the file as the legacy input that must be rejected). Other suites only create files with those names in temporary folders from saved Kraken replies.

**Test changes (test code only, made before any deletion).**
- Dashboard suite: the legacy input is now a five-column CSV the test builds in a temporary folder from the first five columns of the real 17-column CSV. It must still be rejected with a header error (no silent fallback).
- CSV suite: the comparison with the project's old CSV was replaced by a legacy-output compatibility check. The first five columns of the current output, written back out as text, must equal, line by line, the CSV text of the pre-change implementation (`run_workflow_BEFORE.py`, fixed clock, saved real replies). This partly overlaps an existing check that compares columns 1 to 5 with the original implementation. No assertion was weakened and the label no longer calls the deleted file a "published sample".
- Both suites passed before the edits (25 of 25 and 47 of 47) and after them.

**Archive (outside the project, not in the submission).** a folder on the learner's computer (the full path is not recorded here), byte-identical copies verified with `cmp` and SHA-256, plus a short README:
- Report: original and archive `3a72452981da7a12c07a91f0be0fac255fd30d497436a730c895bcc5d168c3f6`
- CSV: original and archive `c31d399f5029cf5662e9a4c554e9931cd56e36d93218bfc21e5e7e95d4a564a9`

**Unique content in the old files.** The 2026-09-26 closes (BTC 84426.70, ETH 2695.35), the 2026-09-27 returns (first row of the older window) and the Excel-checked correlation of 0.926100. The other 30 dates equal columns 1 to 5 of the approved CSV. These exist only in the archive and in the text above.

**Validation (actual results, none skipped).** Run both before and after the two files were deleted; every suite exited 0 with 0 failures both times: D1 10, D2 10, D3 13, D4 18, dashboard 47, 17-column CSV 25, report content 23, workflow integration 29, replacement/failure 33, analysis units 62, enhanced report 43, remediation 79. The Node page-logic suite (24 of 24) also passed when run directly against the shipped dashboard and CSV. There is no separate "17-column check" suite beyond the CSV suite and the dashboard's header validation. The active CSV was re-checked: 17 columns, 31 data rows. Mutation testing was not performed, because these are fixture-only test changes.

**Scope.** Only the two test files (outside the project), `README.md` and this log were edited, and only the two obsolete files were deleted. No production code, market data, calculation, dashboard output or dashboard behaviour was changed; the other project files kept their hashes. The project now has 15 files. No Git command, network access, installation, staging, commit, push or publication took place.

**Security.** This was a file-retirement and test-fixture change, not a security review. The project is not security-cleared; D5, D6 and F1 to F6 remain unresolved.

## Integration of the dashboard refinement and regeneration (2026-10-29)

**Request.** Integrate the reviewed dashboard refinement, apply a redesign of the D1 to D4 guard tests, run the full regression suite, and prepare publication. All work was local: no Git command (apart from one read-only `git status`, see the errors below), no network access, no staging, commit or publication.

**What changed in the project.** `src/dashboard.py` gained the confirmed endpoint and trading-pair facts (the pairs appear only when the report's source line names them). `src/dashboard_template.html` got the refined presentation: header with only the title and subtitle; a provenance section holding the data source, both reporting periods, the fetch and generation times, the input file, the endpoint and the pairs; the unchanged method, limitations and non-affiliation text; and the three-line footer. The learner reviewed the full template diff (twice) and approved the template with SHA-256 `561d20e26582db1abbaecd0b84e62c800d6c251e18d3d23dc1853e4b0ddb4aba`. No data loading, validation, calculation or publication safeguard was changed, and the CSV and the Markdown report were not touched.

**Tests first.** The presentation tests were rewritten before the template edit and showed 16 of 21 (Python) and 4 of 10 (Node) passing against the earlier template, then 20 of 20 and 10 of 10 after it. After the source was integrated, the original D1 to D4 tests failed on 9 checks, all of which pinned the old page or template hash; that is what they were built to detect. The redesigned D1 to D4 tests (data, protected metadata and the Plotly bundle compared as data; the template checked structurally; every no-overwrite, temporary-file, size-limit, output-directory and file-listing assertion kept) replaced them. A new missing-folder test (6 checks) was shown failing against 7 of 7 deliberately broken copies before being accepted; this closed the earlier gap in which no test noticed a removed reports-folder check. The approved template fingerprint is checked by a separate test that was shown to accept the approved template and reject a one-character change and an appended comment.

**Regeneration.** The previous dashboard was copied aside and verified by hash, then `python src/dashboard.py --reports-dir reports` was run. The new `reports/dashboard_2026-10-27.html` has SHA-256 `fa38b2cfd5fbf806aa7b884b71bfd4218cb945017a30b4fb9e917a7611c09bf8`; it was generated at 2026-10-28 21:56:13 UTC, from data fetched at 2026-10-28 06:56:33 UTC, using the real clock. Its columns, 31 rows, chart series, metrics, protected metadata and Plotly.js bundle are identical to the previous dashboard (SHA-256 `b4bd3bc5f5d9f9240369ab3a835536df0f3702a3dca8f5082fd48c8ba55db2c4`), and its embedded rows equal the approved CSV line by line.

**A weakness found and fixed in the tests.** After the regeneration, all nine D1 to D4 data comparisons compared a generated page with the project's own dashboard, which the same code had just produced, so they could no longer detect a data or calculation regression. The learner approved a correction: the guard helper now compares with a preserved copy of the previous dashboard, kept outside the project and refused unless its SHA-256 equals `b4bd3bc5f5d9f9240369ab3a835536df0f3702a3dca8f5082fd48c8ba55db2c4`, and also compares the embedded rows with the approved CSV line by line. The D1 to D4 test files themselves were not changed. Nine new helper checks were shown failing first (7 of them failed individually; 2 hold on both the old and new helper).

**Results (2026-10-29, local time; every suite exit code 0, 0 failures).** CSV 25; dashboard generator 47; D1 10, D2 10, D3 13 (symlink case not tested, not counted), D4 18; guard helper 27; missing-folder 6; presentation 20 and 10 (Node); page logic 24 (Node); analysis units 62; report content 23; workflow integration 29; file replacement 33; enhanced report 43; remediation 79; approved-template mechanism 12 and live check 1. Mutation: guard helper 39 of 40 caught; D1 to D4 production mutants 35 of 37 caught (survivors: M37, and the missing-folder check, which the missing-folder suite catches); missing-folder mutants 7 of 7; approved-template mutants 8 of 8. Browser check in the built-in browser on Windows only: header, eight provenance rows, three-line footer, four charts, the 7, 14 and 30-day controls, the ETH toggle, paging, a 33-line CSV export, no external requests, no console errors, and no horizontal scroll at 375 pixels. Not performed: screenshots, Linux and macOS, other browsers, the symlink case.

**AI errors and corrections.** (1) The AI ran one read-only `git status` early in this stage, against the instruction not to run any Git command; it reported that the folder is not a repository and nothing was changed. (2) A test-editing script silently turned `\b` into a backspace character in two regular expressions; this was found by inspection and fixed before the tests were used. (3) Three scratch tests still assumed the old dashboard or the old template and failed after the replacement; their premises were repointed to preserved copies without changing any assertion. (4) Earlier guard-helper mutation runs 1 and 2 were invalid (the mutants were never loaded) and were discarded; run 3 had two equivalent survivors, which led to an extra test. (5) A first check for stale dataset references wrongly flagged the real data date 2026-10-26; it was a false alarm.

**Decisions left open.** The project license is settled (the MIT License, with the copyright notice in the root-level `LICENSE` file). Public or private visibility and whether the course permits sharing; the commit identity; whether the target GitHub repository is empty; integration of the dashboard into `src/run_workflow.py`. Publication allowlist (15 files, including the root-level `LICENSE`, to be staged by name only, never with `git add .`): `.gitignore`, `README.md`, `AI_LOG.md`, `LICENSE`, `requirements.txt`, `src/analysis.py`, `src/fetch_data.py`, `src/run_workflow.py`, `src/dashboard.py`, `src/dashboard_template.html`, `src/vendor/plotly.min.js`, `src/vendor/PLOTLY_LICENSE.txt`, `reports/report_2026-10-27.md`, `reports/report_2026-10-27_data.csv` and `reports/dashboard_2026-10-27.html`. `src/report_enhanced.py` stays in the folder but is excluded. The project is not security-cleared, and at the time this entry was written (2026-10-29, before publication) nothing had been committed, pushed or published.

## Documentation corrections for findings S1 to S8 (2026-10-29)

An earlier version of this log and of the README called S1, S5, S7 and S8 "preliminary, unverified summaries" and labelled S1 only "(ordering)". The original findings table of the security review was then recovered from the session record and read before any wording was changed. It shows: S1 and S2 belong to `src/report_enhanced.py`, which is excluded from the repository; S3, S4 and S6 concern inputs that `src/fetch_data.py` accepts; S5 and S7 concern `src/run_workflow.py`; S8 is the four accepted fetcher limits. The README and this log now say so.

**Evidence used.** The original table (severity, impact, file) and its re-assessment against the then-current code; the 79-check remediation suite for S1, S2, S3, S4 and S6 in the excluded module; the 100-run simulation for S5. For S3, S4 and S6 the published `src/fetch_data.py` was also exercised offline with synthetic candles on 2026-10-29: a negative trade count, a high below the low, a close outside the range, and identical candles for both pairs were all accepted, while a negative price was rejected. S5 and S7 were not re-run; S7 remains untested because the account cannot create symlinks.

**AI error corrected.** The earlier wording implied that all of S1, S5, S7 and S8 were open findings of the published files and did not mention S3, S4 and S6 at all, although the published pipeline still accepts those inputs. No code, test, data, report, dashboard or template was changed; the project is not security-cleared.

## Dependency advisory check (2026-10-28, 23:21 UTC)

**Request.** With network access authorized for this step only, check public advisories for the Plotly.js file shipped in `src/vendor/` and for the Python dependency versions seen in the development environment. Read-only: nothing was installed, upgraded or changed, no lock file was created, and no code was downloaded or run.

**Method.** Package metadata and advisory records were read from PyPI, the npm registry, OSV and the GitHub Advisory Database, plus the plotly.js changelog and jsDelivr's package metadata. The queries were first tried on known-vulnerable older versions (`requests` 2.19.0 and `lodash` 4.17.15) and returned advisories, which shows that they find advisories when they exist. The vendored Plotly.js file was tied to npm `plotly.js@4.1.1` by its SHA-256 and size.

**What is pinned and what is only observed.** `requirements.txt` pins `requests==2.34.2` and nothing else. urllib3 2.8.0, certifi 2026.10.23, idna 3.20 and charset-normalizer 3.5.2 were seen in the development environment but are not locked. The vendored Plotly.js 4.1.1 is a separate, hash-pinned file, not part of the Python dependency set.

**Results.** No advisory matching the exact versions checked was found for `requests` 2.34.2, urllib3 2.8.0, certifi 2026.10.23, idna 3.20, Plotly.js 4.1.1, MapLibre GL JS 6.9.0 or d3 3.8.2; for `requests`, urllib3, certifi, idna, Plotly.js and MapLibre GL JS the advisory ranges recorded in the sources exclude the versions checked, so they are reported as not affected according to those sources. For charset-normalizer 3.5.2 and d3 3.8.2 the sources record no advisories for the packages at all, which is not a confirmation that they are safe. Where an advisory matters (urllib3, certifi, MapLibre GL JS), its ID, affected range and first fixed version are given in the README.

**Limits that remain.** Components bundled in Plotly.js that could not be mapped to versions (for example buffer, ieee754, pad-left, repeat-string, regl and the `gl-*` shader code, topojson) are unreviewed; the MapLibre version comes from a license comment; no audit of the bundle code was done; the results are a snapshot of 2026-10-28. The `requests` constraints still allow older urllib3, certifi and idna versions that the sources list as affected. D5 (no Content-Security-Policy) remains open. The project is not security-cleared.

**Earlier statements.** Entries dated before this check that say the advisories had not been checked were accurate when written and are kept as history; the unresolved-findings list above reflects the current state.

**AI error corrected.** The README and this log still described the advisories as unchecked after the check had been made; the wording was updated in a separate documentation-only step.

## Content-Security-Policy and the sharing button (2026-10-29)

**Request.** Close finding D5 as far as can be shown locally: add a restrictive Content-Security-Policy to the generated dashboard, then disable the Plotly.js sharing button. Work was local; a short read-only `git status` is the only Git-like command in the whole project (see the earlier integration section), and none was run here. No network access was used for this work.

**Experiments first.** Seven copies of the dashboard were built outside the project and six were opened in the built-in browser: no policy, the proposed policy, two diagnostic variants (hash-only styles, no image source), a variant narrowing images to `blob:`, and a deliberate wrong-hash copy as a negative control. The seventh copy, with `unsafe-eval` added, was only for use if the proposed policy failed, and was not needed. The proposed policy worked without `eval`; styles had to stay inline because Plotly.js writes its own style sheets at run time, and `blob:` images were needed for PNG export. The wrong-hash copy was blocked, which showed that the policy is enforced and that the browser's hash matches the computed hash.

**Implementation, tests first.** Tests for the policy (tag position, exact directives, both script hashes against the page and the vendored Plotly.js bytes, no `eval`, no script `unsafe-inline`, no `data:` images, tampered scripts, missing or malformed policy) and for the generator were written and shown failing (generator tests 2 passed and 13 failed; helper tests failed on the missing helpers). Three older assertions that said the page must contain no policy were replaced by checks for the exact policy. The generator now computes the two script hashes from the exact final script text; no hash is typed in. Then a focused test (4 checks, 1 passed and 3 failed beforehand) drove the one-line change `showSendToCloud: false` in the single chart call.

**Results.** Full suite before the sharing change: 19 of 21 suites passed, 0 failures; the two template-fingerprint suites failed until the new template was approved and pinned. After the change and the re-pinning, the dashboard-related suites and the fingerprint checks were rerun and passed (counts in the README). Mutation checks: production code 50 of 54, helper 55 of 57 (survivors and my classifications are in the README; they are classifications, not proofs). Browser checks on the built-in browser only; `file://`, other browsers, screenshots and the published page after its last regeneration were not browser-tested.

**Current files (SHA-256).** Template `5728e45531d29029357113a3047e1fbeecfa2c3cee8224c006e8fef81a08d817` (pinned with the learner's approval); generator `ba332bf10b6173568459c6c493072453f4908e7a7ddcb0a16669e61fa9c8a332`; dashboard, generated 2026-10-29 09:40:30 UTC, `23c4a64d6030f865feaa6cfefd92e96bca295a5b3bbff3bc429cd9ce7533fd2a`. Hashes given in earlier sections of this log are historical.

**AI errors and corrections.** The first run of the helper mutation checks was invalid: the harness ran only the older test file, so every new mutant looked like a survivor; the harness was fixed and rerun. One older mutant no longer applied after the template was reworded and was reported as skipped. A long wait was caused by the computer sleeping, not by a stuck process.

**Unresolved.** The project is not security-cleared. D5 is implemented but with limits (inline styles allowed; a meta policy cannot set `frame-ancestors` or `sandbox` or stop script-initiated navigation; one browser engine tested). Also open: D6 is only a point-in-time advisory check; S3, S4 and S6 (the published fetcher still accepts those inputs); S5; S7; S8; M37; F1 to F6; unpinned Python dependency versions; course permission for sharing; the commit identity and the state and visibility of the target repository.

## Remaining assignment items

- API exploration notes in `README.md` should accurately describe the exploration I personally performed.
- The Excel correlation check was done on the earlier, now retired 2026-10-26 sample (approximately `0.926100`, archived outside the project); the submitted 2026-10-27 report was checked by independent recalculation only.
- The README test counts are up to date (172 regression checks and 56 mutants for the original workflow, plus the dashboard suites).
- Integrating the dashboard into `src/run_workflow.py` is deferred and undecided.
- The project uses the MIT License, with the copyright notice in the root-level `LICENSE` file.
- Public sharing permission has been confirmed by the course, and publication is proceeding through the controlled GitHub workflow. At the time this item was written (before publication), nothing had been committed, pushed or published.
