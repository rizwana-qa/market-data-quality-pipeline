// Tests for apps_script/Code.gs (Google Apps Script web app), run in Node against fake Google services. Written BEFORE Code.gs.
// Usage: node tests/apps_script/test_code_gs.mjs
import assert from "node:assert/strict";
import { makeWorld, readSource, HEADERS, SECRET, WORKFLOW_URL, btcRow, ethRow, summary, upsertPayload, emailPayload, failurePayload, reportPayload, reportBody, makePng, pngBase64, ARTIFACT_URL, COVERAGE_NAMES } from "./harness.mjs";

let passed = 0, failed = 0;
function check(name, fn) { try { fn(); passed++; console.log("PASS", name); } catch (e) { failed++; console.log("FAIL", name, "|", String(e && e.message).split("\n")[0].slice(0, 230)); } }
const world = (o) => makeWorld(o);
const KNOWN_CODES = new Set(["UNAUTHORIZED", "NOT_CONFIGURED", "INVALID_REQUEST", "INVALID_PAYLOAD", "HEADER_MISMATCH", "SHEET_NOT_FOUND", "DUPLICATE_EXISTING_ROWS", "LOCK_TIMEOUT", "WRITE_UNCONFIRMED", "NOT_VERIFIED", "EMAIL_FAILED", "INTERNAL_ERROR", "POST_REQUIRED", "SEND_OUTCOME_UNKNOWN"]);
const clean = (w) => assert.deepEqual(w.dataRows(), [], "the sheet must be untouched");
const noMail = (w) => assert.equal(w.state.mail.sent.length, 0, "no email may be sent");
const refused = (w, payload, code, what) => { const r = w.call(payload); assert.equal(r.success, false, what + ": must fail"); assert.equal(r.error, code, what + ": error code (got " + r.error + ")"); return r; };

// ------------------------------------------------------------------ source hygiene
check("source: no hard-coded KEY constant and no hard-coded secret-like literal", () => {
  const s = readSource(); assert.ok(!/\bconst\s+KEY\b|\bvar\s+KEY\b|\blet\s+KEY\b/.test(s), "KEY constant");
  assert.ok(!/['"][A-Za-z0-9_\-+/=]{24,}['"]/.test(s.replace(/https:\/\/[^'"]*/g, "")), "a long literal that could be a secret");
});
check("source: the sheet is chosen by SHEET_NAME, never by position; no eval or dynamic code", () => {
  const s = readSource(); assert.ok(s.includes("getSheetByName") && !/getSheets\(\)\s*\[/.test(s), "sheet selection"); assert.ok(!/\beval\s*\(|new\s+Function\s*\(/.test(s), "dynamic code");
});
check("source: the script file defines doPost and doGet and the five actions", () => {
  const s = readSource(); for (const a of ["doPost", "doGet", "upsert_analysis", "verify_analysis", "send_success_email", "notify_failure", "self_check"]) assert.ok(s.includes(a), a);
});

// ------------------------------------------------------------------ authentication and configuration
check("auth: wrong, missing, empty, whitespace-padded, case-changed and non-string tokens are refused with UNAUTHORIZED; nothing is written or sent", () => {
  const w = world();
  for (const token of ["wrong", "", " " + SECRET, SECRET + " ", SECRET.toUpperCase(), SECRET.slice(0, -1), SECRET + "x", 12345, null, [SECRET], { token: SECRET }, true]) refused(w, upsertPayload({ token }), "UNAUTHORIZED", "token " + JSON.stringify(token));
  const noToken = upsertPayload(); delete noToken.token; refused(w, noToken, "UNAUTHORIZED", "missing token");
  clean(w); noMail(w); assert.equal(w.state.setValuesCalls, 0);
});
check("auth: every action requires the token (verify, success email, failure notice, self check)", () => {
  const w = world();
  for (const p of [upsertPayload({ action: "verify_analysis", token: "x" }), emailPayload({ token: "x" }), failurePayload({ token: "x" }), { action: "self_check", token: "x" }]) refused(w, p, "UNAUTHORIZED", p.action);
  noMail(w);
});
check("auth: an unauthorized reply is identical for a wrong token and for an unknown action (no information about the configuration)", () => {
  const w = world(); const a = w.call(upsertPayload({ token: "wrong" })), b = w.call({ action: "nonsense", token: "wrong" }); delete a.request_id; delete b.request_id; delete a.action; delete b.action; assert.deepEqual(a, b);
});
check("config: a missing SHARED_SECRET refuses everything with NOT_CONFIGURED (a request can never be authorized against an empty secret)", () => {
  const w = world({ properties: { SHARED_SECRET: "" } }); refused(w, upsertPayload({ token: "" }), "NOT_CONFIGURED", "empty secret"); const w2 = world(); delete w2.state.properties.SHARED_SECRET; refused(w2, upsertPayload({ token: "" }), "NOT_CONFIGURED", "absent secret"); clean(w2);
});
check("config: a SHARED_SECRET shorter than 16 characters counts as not configured, even when the caller sends exactly that value", () => {
  const w = world({ properties: { SHARED_SECRET: "short-secret" } }); refused(w, upsertPayload({ token: "short-secret" }), "NOT_CONFIGURED", "short secret"); clean(w);
});
check("config: missing SPREADSHEET_ID, SHEET_NAME, DATE_HEADER or ASSET_HEADER makes the sheet actions fail with NOT_CONFIGURED and nothing is written", () => {
  for (const name of ["SPREADSHEET_ID", "SHEET_NAME", "DATE_HEADER", "ASSET_HEADER"]) { const w = world(); delete w.state.properties[name]; const r = refused(w, upsertPayload(), "NOT_CONFIGURED", name); assert.equal(r.sheet_status, "not_attempted"); clean(w); }
});
check("config: DATE_HEADER or ASSET_HEADER naming a heading that does not exist is refused (HEADER_MISMATCH) and nothing is written", () => {
  for (const [k, v] of [["DATE_HEADER", "Date"], ["ASSET_HEADER", "Coin"]]) { const w = world({ properties: { [k]: v } }); refused(w, upsertPayload(), "HEADER_MISMATCH", k); clean(w); }
});

// ------------------------------------------------------------------ request parsing and limits
check("request: invalid JSON, an empty body, a missing body, a JSON array or scalar, and an oversized body are refused with INVALID_REQUEST", () => {
  const w = world();
  for (const body of ["{not json", "", "[]", "42", "null", '"text"', "x".repeat(25000), JSON.stringify(upsertPayload({ padding: "x".repeat(25000) }))]) refused(w, body, "INVALID_REQUEST", "body " + body.slice(0, 12));
  const r = w.context.doPost({}); assert.equal(JSON.parse(r.getContent()).error, "INVALID_REQUEST"); assert.equal(JSON.parse(w.context.doPost(undefined).getContent()).error, "INVALID_REQUEST"); clean(w);
});
check("request: a missing, unknown or non-string action is refused with INVALID_REQUEST; unexpected top-level fields are refused with INVALID_PAYLOAD", () => {
  const w = world(); for (const action of [undefined, "", "delete_everything", "UPSERT_ANALYSIS", 5, ["upsert_analysis"]]) refused(w, upsertPayload({ action }), "INVALID_REQUEST", "action " + JSON.stringify(action));
  refused(w, upsertPayload({ sneaky: 1 }), "INVALID_PAYLOAD", "extra field"); clean(w);
});
check("doGet: answers POST_REQUIRED without touching anything and without revealing configuration", () => {
  const w = world(); const out = JSON.parse(w.context.doGet({}).getContent()); assert.equal(out.success, false); assert.equal(out.error, "POST_REQUIRED"); const text = JSON.stringify(out);
  for (const secret of [SECRET, "test-spreadsheet-id", "owner@example.test", "SHARED_SECRET", "SPREADSHEET_ID"]) assert.ok(!text.includes(secret), secret); clean(w);
});

// ------------------------------------------------------------------ payload validation (everything is validated before anything is written)
const badRows = {
  "missing heading key": (r) => { delete r["Correlation"]; }, "unexpected extra key": (r) => { r["Notes"] = "x"; }, "heading with a trailing space": (r) => { r["Asset "] = r["Asset"]; delete r["Asset"]; },
  "closing price as text": (r) => { r["Closing price"] = "80001.9"; }, "closing price null": (r) => { r["Closing price"] = null; }, "closing price zero": (r) => { r["Closing price"] = 0; }, "closing price negative": (r) => { r["Closing price"] = -5; }, "closing price absurd": (r) => { r["Closing price"] = 1e12; },
  "daily return below -1": (r) => { r["Daily return"] = -1.5; }, "daily return absurd": (r) => { r["Daily return"] = 50; }, "volatility negative": (r) => { r["30-day volatility"] = -0.01; }, "volatility absurd": (r) => { r["30-day volatility"] = 9; },
  "drawdown positive": (r) => { r["30-day drawdown"] = 0.2; }, "drawdown below -1": (r) => { r["30-day drawdown"] = -1.2; }, "correlation above 1": (r) => { r["Correlation"] = 1.5; }, "correlation below -1": (r) => { r["Correlation"] = -1.01; },
  "number as boolean": (r) => { r["Correlation"] = true; }, "number as array": (r) => { r["Correlation"] = [0.9]; },
  "asset lower case": (r) => { r["Asset"] = "btc"; }, "asset unknown": (r) => { r["Asset"] = "DOGE"; }, "asset with spaces": (r) => { r["Asset"] = "BTC "; }, "asset as number": (r) => { r["Asset"] = 1; },
  "validation status unknown": (r) => { r["Validation status"] = "OK-ish"; }, "report status unknown": (r) => { r["Report status"] = "done"; }, "run id with letters": (r) => { r["Workflow run ID"] = "12ab"; }, "run id too long": (r) => { r["Workflow run ID"] = "1".repeat(25); }, "run id as number": (r) => { r["Workflow run ID"] = 123; }, "run id empty": (r) => { r["Workflow run ID"] = ""; },
  "date in the wrong format": (r) => { r["Run date"] = "27/10/2026"; }, "date impossible month": (r) => { r["Run date"] = "2026-13-01"; }, "date impossible day": (r) => { r["Run date"] = "2026-02-30"; }, "date with time": (r) => { r["Run date"] = "2026-10-27T00:00:00Z"; }, "date different from analysis_date": (r) => { r["Run date"] = "2026-10-26"; },
  "date in the future": (r) => { r["Run date"] = "2026-11-02"; }, "date before 2020": (r) => { r["Run date"] = "2019-12-31"; },
  "formula in asset": (r) => { r["Asset"] = "=1+1"; }, "formula in validation status": (r) => { r["Validation status"] = "=HYPERLINK(\"http://x\")"; }, "formula in report status": (r) => { r["Report status"] = "+cmd|' /C calc'!A0"; }, "formula in run id": (r) => { r["Workflow run ID"] = "@SUM(1)"; }, "minus formula in run id": (r) => { r["Workflow run ID"] = "-2+3"; },
  "extremely long text": (r) => { r["Validation status"] = "P".repeat(5000); },
};
check("validation: each invalid value is refused with INVALID_PAYLOAD (" + Object.keys(badRows).length + " cases) and nothing is written, even when the other row is valid", () => {
  for (const [name, mutate] of Object.entries(badRows)) {
    for (const position of [0, 1]) {
      const w = world(); const rows = [btcRow(), ethRow()]; mutate(rows[position]); const r = w.call(upsertPayload({ rows })); assert.equal(r.success, false, name + " at row " + position + " was accepted"); assert.equal(r.error, "INVALID_PAYLOAD", name + ": " + r.error);
      assert.equal(r.sheet_status, "not_attempted", name); clean(w); assert.equal(w.state.setValuesCalls, 0, name); noMail(w);
    }
  }
});
check("validation: row list problems - empty, too many, not an array, a row that is not an object, a duplicate (date, asset) key in the same payload - are refused before any write", () => {
  const w = world();
  for (const rows of [[], Array.from({ length: 11 }, () => btcRow()), "rows", null, [btcRow(), "ETH"], [btcRow(), null], [btcRow(), [1, 2]], [btcRow(), btcRow()], [btcRow(), btcRow({ "Closing price": 1 })]]) refused(w, upsertPayload({ rows }), "INVALID_PAYLOAD", "rows " + JSON.stringify(rows).slice(0, 25));
  clean(w); assert.equal(w.state.setValuesCalls, 0);
});
check("validation: a missing or malformed analysis_date is refused", () => {
  const w = world(); for (const analysis_date of [undefined, "", "2026-10-27 ", 20261027, "2026/10/27"]) refused(w, upsertPayload({ analysis_date }), "INVALID_PAYLOAD", "analysis_date " + analysis_date); clean(w);
});

// ------------------------------------------------------------------ sheet structure
check("headers: a renamed, reordered, extra, missing, blank or whitespace-different heading row is refused with HEADER_MISMATCH and nothing is written", () => {
  const variants = { renamed: HEADERS.map((h, i) => (i === 3 ? "Return" : h)), reordered: [HEADERS[1], HEADERS[0], ...HEADERS.slice(2)], extra: [...HEADERS, "Notes"], missing: HEADERS.slice(0, 9), blank: HEADERS.map((h, i) => (i === 6 ? "" : h)), trailing: HEADERS.map((h, i) => (i === 0 ? h + " " : h)), lower: HEADERS.map((h, i) => (i === 1 ? "asset" : h)) };
  for (const [name, headers] of Object.entries(variants)) { const w = world({ headers }); const r = refused(w, upsertPayload(), "HEADER_MISMATCH", name); assert.equal(r.sheet_status, "failed", name); assert.equal(w.dataRows().length, 0, name); assert.equal(w.state.setValuesCalls, 0, name); }
});
check("headers: the heading row itself is never modified or created by any action", () => {
  const w = world(); w.call(upsertPayload()); w.call(upsertPayload()); assert.deepEqual(w.sheet.data[0], HEADERS);
  const empty = world({ headers: [""] }); refused(empty, upsertPayload(), "HEADER_MISMATCH", "empty sheet"); assert.deepEqual(empty.sheet.data[0], [""]);
});
check("sheet: a SHEET_NAME that does not exist gives SHEET_NOT_FOUND; other tabs are never used or touched", () => {
  const w = world({ properties: { SHEET_NAME: "Missing" }, extraSheet: "Other" }); refused(w, upsertPayload(), "SHEET_NOT_FOUND", "missing tab"); assert.deepEqual(w.state.sheets.Other.data, [["unrelated"]]); clean(w);
  const w2 = world({ properties: { SHEET_NAME: "Other" }, extraSheet: "Other" }); refused(w2, upsertPayload(), "HEADER_MISMATCH", "wrong tab has other headings"); assert.deepEqual(w2.state.sheets.Other.data, [["unrelated"]]);
});
check("sheet: a wrong SPREADSHEET_ID fails safely (sheet_status failed, no stack trace in the reply)", () => {
  const w = world({ properties: { SPREADSHEET_ID: "someone-elses-sheet" } }); const r = w.call(upsertPayload()); assert.equal(r.success, false); assert.ok(KNOWN_CODES.has(r.error)); assert.ok(!JSON.stringify(r).includes("someone-elses-sheet")); assert.ok(!/at |stack|Error:/.test(JSON.stringify(r)));
});

// ------------------------------------------------------------------ upsert, duplicate protection, verification
check("upsert: the first run inserts both rows with exact values, the date as TEXT (not a date cell), numbers as numbers, and confirms the write", () => {
  const w = world(); const r = w.call(upsertPayload());
  assert.equal(r.success, true); assert.equal(r.sheet_status, "ok"); assert.equal(r.rows_inserted, 2); assert.equal(r.rows_updated, 0); assert.equal(r.write_confirmed, true); assert.equal(r.email_status, "not_attempted");
  const rows = w.dataRows(); assert.equal(rows.length, 2); assert.deepEqual(rows[0], HEADERS.map((h) => btcRow()[h])); assert.deepEqual(rows[1], HEADERS.map((h) => ethRow()[h]));
  assert.equal(typeof rows[0][0], "string", "the date must stay text"); assert.equal(typeof rows[0][2], "number"); assert.deepEqual(w.sheet.data[0], HEADERS); assert.equal(w.state.lock.releases, 1);
});
check("duplicate protection: running the same analysis again updates the same rows - the sheet still has exactly two rows for the date", () => {
  const w = world(); w.call(upsertPayload()); const r = w.call(upsertPayload()); assert.equal(r.success, true); assert.equal(r.rows_inserted, 0); assert.equal(r.rows_updated, 2); assert.equal(w.dataRows().length, 2);
  for (let i = 0; i < 5; i++) w.call(upsertPayload()); assert.equal(w.dataRows().length, 2);
});
check("duplicate protection: a rerun with different values overwrites the same two rows, it does not append", () => {
  const w = world(); w.call(upsertPayload()); const r = w.call(upsertPayload({ rows: [btcRow({ "Closing price": 81000 }), ethRow({ "Closing price": 2400 })] })); assert.equal(r.rows_updated, 2); const rows = w.dataRows(); assert.equal(rows.length, 2); assert.equal(rows[0][2], 81000); assert.equal(rows[1][2], 2400);
});
check("upsert: a new date appends two new rows and leaves earlier history byte-for-byte unchanged", () => {
  const w = world({ now: new Date("2026-10-29T02:15:30Z") }); w.call(upsertPayload()); const before = JSON.stringify(w.dataRows()); const r = w.call(upsertPayload({ analysis_date: "2026-10-28", rows: [btcRow({ "Run date": "2026-10-28" }), ethRow({ "Run date": "2026-10-28" })] }));
  assert.equal(r.success, true, JSON.stringify(r)); assert.equal(r.rows_inserted, 2); assert.equal(w.dataRows().length, 4); assert.equal(JSON.stringify(w.dataRows().slice(0, 2)), before);
});
check("upsert: the run date must be a COMPLETED UTC day - today (still in progress) and the future are refused, yesterday is accepted", () => {
  const w = world(); refused(w, upsertPayload({ analysis_date: "2026-10-28", rows: [btcRow({ "Run date": "2026-10-28" })] }), "INVALID_PAYLOAD", "today"); clean(w);
  assert.equal(w.call(upsertPayload({ rows: [btcRow()] })).success, true);
  const early = world({ now: new Date("2026-10-28T00:00:01Z") }); assert.equal(early.call(upsertPayload({ rows: [btcRow()] })).success, true, "just after midnight UTC the previous day is complete");
  const late = world({ now: new Date("2026-10-27T23:59:59Z") }); refused(late, upsertPayload({ rows: [btcRow()] }), "INVALID_PAYLOAD", "one second before the day ends"); clean(late);
});
check("upsert: one row exists and the other is new - one update plus one insert", () => {
  const w = world(); w.call(upsertPayload({ rows: [btcRow()] })); const r = w.call(upsertPayload()); assert.equal(r.rows_inserted, 1); assert.equal(r.rows_updated, 1); assert.equal(w.dataRows().length, 2);
});
check("upsert: existing historical rows and unrelated data are preserved; a hand-typed date cell (a real date value) is recognised and updated, not duplicated", () => {
  const history = [[new Date(Date.UTC(2026, 9, 26)), "BTC", 1, 0.1, 0.2, -0.1, 0.5, "PASSED", "GENERATED", "99"], [new Date(Date.UTC(2026, 9, 27)), "BTC", 5, 0.1, 0.2, -0.1, 0.5, "PASSED", "GENERATED", "98"]];
  const w = world({ sheetRows: history }); const r = w.call(upsertPayload({ rows: [btcRow()] })); assert.equal(r.rows_updated, 1); assert.equal(r.rows_inserted, 0); assert.equal(w.dataRows().length, 2); assert.equal(w.dataRows()[0][2], 1, "the 2026-10-26 row is untouched"); assert.equal(w.dataRows()[1][2], 80001.9);
});
check("upsert: if the sheet already holds duplicate keys for a row about to be written, the request is refused with DUPLICATE_EXISTING_ROWS and nothing changes", () => {
  const dup = ["2026-10-27", "BTC", 1, 0, 0, 0, 0, "PASSED", "GENERATED", "1"]; const w = world({ sheetRows: [dup, dup] }); const before = JSON.stringify(w.dataRows()); const r = refused(w, upsertPayload(), "DUPLICATE_EXISTING_ROWS", "dup"); assert.equal(r.sheet_status, "failed"); assert.equal(JSON.stringify(w.dataRows()), before); assert.equal(w.state.setValuesCalls, 0);
});
check("upsert: when the grid has no free rows left the sheet is extended and the write still succeeds", () => {
  const w = world({ maxRows: 1 }); const r = w.call(upsertPayload()); assert.equal(r.success, true, JSON.stringify(r)); assert.equal(w.dataRows().length, 2);
});
check("write verification: if the stored values differ from what was written, the reply says so (WRITE_UNCONFIRMED, write_confirmed false) and is not a success", () => {
  const w = world(); w.state.afterSetValues = (s, row, col) => { if (row === 3 && s.data[2]) s.data[2][2] = 1; }; const r = w.call(upsertPayload()); assert.equal(r.success, false); assert.equal(r.error, "WRITE_UNCONFIRMED"); assert.equal(r.write_confirmed, false); assert.equal(r.sheet_status, "unconfirmed"); assert.equal(w.state.lock.releases, 1);
});
check("write failure: an exception while writing gives sheet_status failed, INTERNAL_ERROR without details, and the lock is still released", () => {
  const w = world(); w.state.afterSetValues = () => { throw new Error("quota exceeded while writing /secret/path"); }; const r = w.call(upsertPayload()); assert.equal(r.success, false); assert.equal(r.sheet_status, "failed"); assert.equal(r.error, "INTERNAL_ERROR"); assert.ok(!JSON.stringify(r).includes("quota") && !JSON.stringify(r).includes("/secret/path")); assert.equal(w.state.lock.releases, 1);
});
check("locking: a busy lock gives LOCK_TIMEOUT with nothing written; a re-entrant request during a write is refused while the first one completes; the lock is always released", () => {
  const busy = world(); busy.state.lock.busy = true; const r = refused(busy, upsertPayload(), "LOCK_TIMEOUT", "busy"); assert.equal(r.sheet_status, "not_attempted"); clean(busy);
  const w = world(); let nested = null; w.state.afterSetValues = () => { if (!nested) nested = w.call(upsertPayload()); }; const first = w.call(upsertPayload()); assert.equal(first.success, true); assert.equal(nested.error, "LOCK_TIMEOUT"); assert.equal(w.dataRows().length, 2); assert.equal(w.state.lock.held, false);
});
check("a refused or failed request never leaves the lock held", () => {
  const w = world(); for (const p of [upsertPayload({ token: "x" }), upsertPayload({ rows: [] }), upsertPayload({ action: "verify_analysis" })]) w.call(p); assert.equal(w.state.lock.held, false);
});

// ------------------------------------------------------------------ verify_analysis (read-only final-state check)
check("verify_analysis: after an upsert it reports 2 rows, matching, no duplicates; before an upsert it reports 0 rows and no match; it never writes", () => {
  const w = world(); const vp = { action: "verify_analysis", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow(), ethRow()] };
  let r = w.call(vp); assert.equal(r.success, false); assert.equal(r.rows_found, 0); assert.equal(r.matches, false);
  w.call(upsertPayload()); const writesBefore = w.state.setValuesCalls; r = w.call(vp); assert.equal(r.success, true); assert.equal(r.rows_found, 2); assert.equal(r.matches, true); assert.equal(r.duplicate_keys, 0); assert.equal(w.state.setValuesCalls, writesBefore, "verify must not write");
});
check("verify_analysis: a tampered cell, a missing row and a duplicate row are all detected", () => {
  const vp = { action: "verify_analysis", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow(), ethRow()] };
  const w = world(); w.call(upsertPayload()); w.sheet.data[2][2] = 999; let r = w.call(vp); assert.equal(r.success, false); assert.equal(r.matches, false);
  const w2 = world(); w2.call(upsertPayload({ rows: [btcRow()] })); r = w2.call(vp); assert.equal(r.success, false); assert.equal(r.rows_found, 1);
  const w3 = world(); w3.call(upsertPayload()); w3.sheet.data.push(w3.sheet.data[1].slice()); r = w3.call(vp); assert.equal(r.success, false); assert.ok(r.duplicate_keys >= 1);
});
check("verify_analysis: validates its input like upsert (bad rows are refused) and does not need or use the mail service", () => {
  const w = world(); refused(w, { action: "verify_analysis", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow({ Asset: "DOGE" })] }, "INVALID_PAYLOAD", "bad verify rows"); noMail(w);
});

// ------------------------------------------------------------------ success email
check("success email: sent only after the sheet is verified; one message to EMAIL_TO; body has the date, both assets, correlation, rules, validation and sheet status, workflow URL, execution time in UTC and Dubai", () => {
  const w = world(); assert.equal(w.call(upsertPayload()).success, true); const r = w.call(emailPayload());
  assert.equal(r.success, true); assert.equal(r.sheet_status, "ok"); assert.equal(r.email_status, "sent"); assert.equal(w.state.mail.sent.length, 1); const m = w.state.mail.sent[0];
  assert.equal(m.to, "owner@example.test"); assert.ok(m.subject.includes("2026-10-27")); const b = m.body;
  for (const needle of ["2026-10-27", "BTC", "ETH", "80001.9", "2320.45", "0.928089", "strong positive correlation", "ETH noticeably more volatile", "down days largely coincide", "PASSED", WORKFLOW_URL, "2026-10-28 02:15:30 UTC", "2026-10-28 06:15:30", "Dubai", "Both assets fell on 16 of 30 days."]) assert.ok(b.includes(needle), "body lacks: " + needle);
  assert.ok(/Spreadsheet:.*(verified|confirmed)/i.test(b), "sheet status line");
});
check("success email: NO email when the sheet holds no rows for the date (the update failed or was never made)", () => {
  const w = world(); const r = w.call(emailPayload()); assert.equal(r.success, false); assert.equal(r.email_status, "not_attempted"); assert.ok(["failed", "unconfirmed"].includes(r.sheet_status)); assert.equal(r.error, "NOT_VERIFIED"); noMail(w);
});
check("success email: NO email when the stored values differ from the expected rows, or one row is missing, or a duplicate exists", () => {
  const w = world(); w.call(upsertPayload()); w.sheet.data[1][3] = 0.5; let r = w.call(emailPayload()); assert.equal(r.email_status, "not_attempted"); noMail(w);
  const w2 = world(); w2.call(upsertPayload({ rows: [btcRow()] })); r = w2.call(emailPayload()); assert.equal(r.email_status, "not_attempted"); noMail(w2);
  const w3 = world(); w3.call(upsertPayload()); w3.sheet.data.push(w3.sheet.data[2].slice()); r = w3.call(emailPayload()); assert.equal(r.email_status, "not_attempted"); noMail(w3);
});
check("success email: a repeated request for the same run is not sent twice (skipped_duplicate), while a different run id is sent", () => {
  const w = world(); w.call(upsertPayload()); w.call(emailPayload()); const again = w.call(emailPayload()); assert.equal(again.success, true); assert.equal(again.email_status, "skipped_duplicate"); assert.equal(w.state.mail.sent.length, 1);
  w.call(emailPayload({ run_id: "987654321", workflow_url: WORKFLOW_URL.replace("123456789", "987654321") })); assert.equal(w.state.mail.sent.length, 2);
});
check("email failure is reported on its own: the sheet stays ok, email_status failed, success false, EMAIL_FAILED, no internal details; a later retry can still send", () => {
  const w = world(); w.call(upsertPayload()); w.state.mail.failWith = "Service invoked too many times for one day: email."; const r = w.call(emailPayload()); assert.equal(r.success, false); assert.equal(r.sheet_status, "ok"); assert.equal(r.email_status, "failed"); assert.equal(r.error, "EMAIL_FAILED"); assert.ok(!JSON.stringify(r).includes("too many times"));
  w.state.mail.failWith = null; assert.equal(w.call(emailPayload()).email_status, "sent");
});
check("email configuration: a missing or malformed EMAIL_TO sends nothing and reports email_status failed with NOT_CONFIGURED", () => {
  for (const value of [undefined, "", "not-an-address", "a@b", "two@example.test,three@example.test", "x@example.test\nbcc: y@example.test"]) { const w = world(); w.call(upsertPayload()); if (value === undefined) delete w.state.properties.EMAIL_TO; else w.state.properties.EMAIL_TO = value; const r = w.call(emailPayload()); assert.equal(r.email_status, "failed", String(value)); assert.equal(r.error, "NOT_CONFIGURED"); assert.equal(r.sheet_status, "ok"); noMail(w); }
});
check("email input: a workflow URL that is not this repository's run page, bad summary numbers, too many or too long observations, and a wrong run id are refused with INVALID_PAYLOAD and nothing is sent", () => {
  const bad = [{ workflow_url: "https://evil.example/phish" }, { workflow_url: WORKFLOW_URL + "/../x" }, { workflow_url: "http://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/1" }, { workflow_url: WORKFLOW_URL.replace("rizwana-qa", "someone-else") },
    { summary: summary({ correlation: null }) }, { summary: summary({ observations: Array.from({ length: 12 }, () => "x") }) }, { summary: summary({ observations: ["y".repeat(2000)] }) }, { summary: summary({ btc: { close: "high" } }) }, { run_id: "abc" }, { validation_status: "MAYBE" }, { sheet_update: { rows_inserted: -1, rows_updated: 0 } }];
  for (const over of bad) { const w = world(); w.call(upsertPayload()); const r = w.call(emailPayload(over)); assert.equal(r.error, "INVALID_PAYLOAD", JSON.stringify(over).slice(0, 60)); noMail(w); }
});
check("email and failure notice: the run id inside the workflow URL must equal run_id (a URL of another run is refused, nothing is sent)", () => {
  const other = WORKFLOW_URL.replace("123456789", "555555555");
  const w = world(); w.call(upsertPayload()); refused(w, emailPayload({ workflow_url: other }), "INVALID_PAYLOAD", "email url of another run"); refused(w, failurePayload({ workflow_url: other }), "INVALID_PAYLOAD", "failure url of another run"); noMail(w);
});
check("email body is plain text built from validated fields: control characters in summary text are removed and the body never contains the token, spreadsheet id or sheet name property values", () => {
  const w = world(); w.call(upsertPayload()); w.call(emailPayload({ summary: summary({ observations: ["line one\u0000\u0007 with controls <b>bold</b>"] }) })); const b = w.state.mail.sent[0].body;
  assert.ok(!/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(b)); for (const secret of [SECRET, "test-spreadsheet-id"]) assert.ok(!b.includes(secret), secret); assert.equal(w.state.mail.sent[0].htmlBody, undefined);
});

// ------------------------------------------------------------------ failure notification (separate action; independent of the sheet)
check("notify_failure: sends one email with the failed stage, error summary, spreadsheet status, run id and workflow URL", () => {
  const w = world(); const r = w.call(failurePayload()); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.equal(r.sheet_status, "not_attempted"); const m = w.state.mail.sent[0]; assert.ok(/fail/i.test(m.subject));
  for (const needle of ["tests", "3 of 172 checks failed", "not_attempted", "123456789", WORKFLOW_URL]) assert.ok(m.body.includes(needle), needle);
});
check("notify_failure: works even when the spreadsheet configuration is missing or broken (it is the path for failures before the webhook was reached)", () => {
  const w = world(); delete w.state.properties.SPREADSHEET_ID; delete w.state.properties.SHEET_NAME; assert.equal(w.call(failurePayload()).email_status, "sent");
  const w2 = world({ headers: ["x"] }); assert.equal(w2.call(failurePayload({ stage: "post_to_sheet", sheet_status: "failed" })).email_status, "sent");
});
check("notify_failure: every pipeline stage name is accepted and an unknown stage, a bad sheet_status, a bad URL or a bad run id is refused with nothing sent", () => {
  for (const stage of ["checkout", "setup_python", "install_dependencies", "tests", "analysis", "validate_outputs", "build_payload", "post_to_sheet", "verify_sheet", "send_success_email", "upload_artifacts", "unknown"]) { const w = world(); assert.equal(w.call(failurePayload({ stage, run_id: "5", workflow_url: WORKFLOW_URL.replace("123456789", "5") })).email_status, "sent", stage); }
  for (const over of [{ stage: "hack" }, { stage: "" }, { stage: 3 }, { sheet_status: "great" }, { workflow_url: "https://evil.example" }, { run_id: "x" }, { error_summary: 5 }]) { const w = world(); const r = w.call(failurePayload(over)); assert.equal(r.error, "INVALID_PAYLOAD", JSON.stringify(over)); noMail(w); }
});
check("notify_failure: a long error summary is cut to 500 characters, control characters are removed, and the shared secret is replaced if it ever appears", () => {
  const w = world(); w.call(failurePayload({ error_summary: "E".repeat(2000) })); assert.ok(w.state.mail.sent[0].body.includes("E".repeat(500)) && !w.state.mail.sent[0].body.includes("E".repeat(501)));
  const w2 = world(); w2.call(failurePayload({ error_summary: "token was " + SECRET + " in the log\u0000" })); const b = w2.state.mail.sent[0].body; assert.ok(!b.includes(SECRET)); assert.ok(b.includes("[redacted]")); assert.ok(!b.includes("\u0000"));
});
check("notify_failure: the same run and stage is mailed once; mail errors are reported as email_status failed / EMAIL_FAILED; wrong token sends nothing", () => {
  const w = world(); w.call(failurePayload()); const again = w.call(failurePayload()); assert.equal(again.email_status, "skipped_duplicate"); assert.equal(w.state.mail.sent.length, 1);
  const w2 = world(); w2.state.mail.failWith = "boom"; const r = w2.call(failurePayload()); assert.equal(r.success, false); assert.equal(r.email_status, "failed"); assert.equal(r.error, "EMAIL_FAILED");
  const w3 = world(); refused(w3, failurePayload({ token: "nope" }), "UNAUTHORIZED", "failure auth"); noMail(w3);
});

// ------------------------------------------------------------------ self check and information leakage
check("self_check: authorized only; reports presence of each property and the heading check as booleans and never returns any value", () => {
  const w = world(); const r = w.call({ action: "self_check", token: SECRET }); assert.equal(r.success, true); const text = JSON.stringify(r);
  for (const k of ["SPREADSHEET_ID", "SHEET_NAME", "SHARED_SECRET", "EMAIL_TO", "DATE_HEADER", "ASSET_HEADER"]) assert.equal(r.properties[k], true, k);
  assert.equal(r.sheet_found, true); assert.equal(r.headers_ok, true); for (const v of [SECRET, "test-spreadsheet-id", "owner@example.test"]) assert.ok(!text.includes(v), v);
  const bad = world({ headers: ["x"] }); delete bad.state.properties.EMAIL_TO; const b = bad.call({ action: "self_check", token: SECRET }); assert.equal(b.headers_ok, false); assert.equal(b.properties.EMAIL_TO, false);
});
const selfCheck = (w, extra = {}) => w.call(Object.assign({ action: "self_check", token: SECRET }, extra));
check("self_check: the spreadsheet and the worksheet are reported separately - a wrong spreadsheet id, a missing worksheet and a wrong heading row each switch off only their own values", () => {
  const good = selfCheck(world()); assert.equal(good.success, true); assert.equal(good.spreadsheet_found, true); assert.equal(good.sheet_found, true); assert.equal(good.headers_ok, true); assert.equal(good.email_valid, true); assert.equal(good.error, undefined);
  const wrongId = selfCheck(world({ properties: { SPREADSHEET_ID: "someone-elses-sheet" } })); assert.deepEqual([wrongId.success, wrongId.spreadsheet_found, wrongId.sheet_found, wrongId.headers_ok], [false, false, false, false]); assert.equal(wrongId.error, "NOT_CONFIGURED");
  const noTab = selfCheck(world({ properties: { SHEET_NAME: "Missing" } })); assert.deepEqual([noTab.success, noTab.spreadsheet_found, noTab.sheet_found, noTab.headers_ok], [false, true, false, false]);
  const badHeadings = selfCheck(world({ headers: [...HEADERS.slice(0, 9), "Run ID"] })); assert.deepEqual([badHeadings.success, badHeadings.spreadsheet_found, badHeadings.sheet_found, badHeadings.headers_ok], [false, true, true, false]);
  const noEmail = world(); noEmail.state.properties.EMAIL_TO = "not-an-address"; const mail = selfCheck(noEmail); assert.deepEqual([mail.success, mail.spreadsheet_found, mail.sheet_found, mail.headers_ok, mail.email_valid, mail.properties.EMAIL_TO], [false, true, true, true, false, true]);
});
check("self_check: expected_sheet_name makes the reply say, as a boolean only, whether SHEET_NAME equals it; a different tab name fails the check; without it the field is absent", () => {
  const same = selfCheck(world(), { expected_sheet_name: "Sheet1" }); assert.equal(same.success, true); assert.equal(same.sheet_name_matches, true);
  const other = selfCheck(world({ properties: { SHEET_NAME: "Other" }, extraSheet: "Other" }), { expected_sheet_name: "Sheet1" }); assert.equal(other.sheet_name_matches, false); assert.equal(other.success, false); assert.equal(other.error, "NOT_CONFIGURED");
  const absent = selfCheck(world()); assert.equal("sheet_name_matches" in absent, false);
  const unset = world(); delete unset.state.properties.SHEET_NAME; const none = selfCheck(unset, { expected_sheet_name: "Sheet1" }); assert.equal(none.sheet_name_matches, false); assert.equal(none.success, false);
  assert.ok(!JSON.stringify(same).includes("Sheet1"), "the reply must not echo the sheet name, only a boolean");
});
check("self_check: expected_sheet_name must be a non-empty string of at most 100 characters; any other extra field is refused", () => {
  const w = world(); for (const value of [5, "", null, ["Sheet1"], { a: 1 }, true, "x".repeat(101)]) refused(w, { action: "self_check", token: SECRET, expected_sheet_name: value }, "INVALID_PAYLOAD", "expected_sheet_name " + JSON.stringify(value).slice(0, 12));
  refused(w, { action: "self_check", token: SECRET, other: 1 }, "INVALID_PAYLOAD", "other field"); refused(w, { action: "self_check", token: SECRET, expected_sheet_name: "Sheet1", other: 1 }, "INVALID_PAYLOAD", "two extra fields");
  assert.equal(selfCheck(w, { expected_sheet_name: "x".repeat(100) }).error, "NOT_CONFIGURED", "100 characters are accepted (the check fails because no such tab exists, not because of the length)");
});
check("self_check is strictly read-only whatever the configuration: no cell written, no format changed, no row added, no e-mail, no lock, no cache entry", () => {
  const variants = [() => world(), () => world({ properties: { SPREADSHEET_ID: "someone-elses-sheet" } }), () => world({ properties: { SHEET_NAME: "Missing" } }), () => world({ headers: ["x"] }), () => world({ headers: [...HEADERS, "Notes"] }), () => { const w = world(); delete w.state.properties.EMAIL_TO; return w; }, () => { const w = world(); w.call(upsertPayload()); w.state.mail.sent.length = 0; return w; }];
  for (const make of variants) {
    const w = make(); const before = JSON.stringify([w.sheet.data, w.sheet.formats, w.sheet.maxRows, w.state.sheets.Sheet1.data]); const writes = w.state.setValuesCalls, locks = w.state.lock.waits, cached = w.state.cache.size;
    selfCheck(w, { expected_sheet_name: "Sheet1" }); selfCheck(w);
    assert.equal(JSON.stringify([w.sheet.data, w.sheet.formats, w.sheet.maxRows, w.state.sheets.Sheet1.data]), before, "the sheet changed"); assert.equal(w.state.setValuesCalls, writes); assert.equal(w.state.mail.sent.length, 0, "an e-mail was sent"); assert.equal(w.state.lock.waits, locks, "the lock was used"); assert.equal(w.state.cache.size, cached);
  }
});
check("self_check: still requires the token (wrong token gives UNAUTHORIZED and reveals nothing), including when expected_sheet_name is sent", () => {
  const w = world(); const r = refused(w, { action: "self_check", token: "wrong", expected_sheet_name: "Sheet1" }, "UNAUTHORIZED", "self check auth"); assert.equal(r.properties, undefined); assert.equal(r.spreadsheet_found, undefined);
});
check("no leakage: across success, failure and malicious requests no reply and no log line contains the shared secret, and every error code is a known one without stack traces", () => {
  const w = world(); const replies = [w.call(upsertPayload()), w.call(upsertPayload()), w.call(emailPayload()), w.call(failurePayload({ error_summary: SECRET })), w.call(upsertPayload({ token: SECRET + "x" })), w.call("{broken"), w.call(upsertPayload({ rows: [btcRow({ Asset: SECRET })] })), w.call({ action: "self_check", token: "x" })];
  for (const r of replies) { assert.ok(!JSON.stringify(r).includes(SECRET)); if (r.error) assert.ok(KNOWN_CODES.has(r.error), r.error); assert.ok(!/\bat\s.*\(|stack/i.test(JSON.stringify(r))); }
  assert.ok(!w.state.logs.join("\n").includes(SECRET), "a log line contains the secret"); assert.ok(!w.state.mail.sent.some((m) => (m.subject + m.body).includes(SECRET)));
});
check("no sheet value ever starts with a formula character after any accepted write", () => {
  const w = world({ now: new Date("2026-10-29T02:15:30Z") }); w.call(upsertPayload()); const second = w.call(upsertPayload({ analysis_date: "2026-10-28", rows: [btcRow({ "Run date": "2026-10-28" }), ethRow({ "Run date": "2026-10-28" })] }));
  assert.equal(second.success, true, JSON.stringify(second)); assert.equal(w.dataRows().length, 4);
  for (const row of w.dataRows()) for (const cell of row) if (typeof cell === "string") assert.ok(!/^[=+\-@\t\r]/.test(cell), cell);
});

// ------------------------------------------------------------------ send_success_report: the HTML success e-mail with inline PNG charts (the existing send_success_email stays as it is)
// The web app builds the HTML ITSELF from validated fields; it never accepts HTML from the caller. These tests judge the message handed to MailApp: htmlBody, inlineImages (cid) and the plain-text body.
// They run offline against imitated Google services: they prove structure and safety, NOT how Gmail or Outlook draw the message.
const sentReport = (w, over) => { w.call(upsertPayload()); return w.call(reportPayload(over)); };
const lastMail = (w) => w.state.mail.sent[w.state.mail.sent.length - 1];
const textOf = (html) => html.replace(/<!--[\s\S]*?-->/g, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const VOID_TAGS = new Set(["meta", "br", "img", "hr", "link", "input"]);
function structureOf(html) {
  const problems = [], stack = [], counts = {}, images = [], hrefs = [];
  const body = html.replace(/<!--[\s\S]*?-->/g, "").replace(/<style[\s\S]*?<\/style>/gi, "<style></style>");
  for (const match of body.matchAll(/<(\/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>/g)) {
    const [, closing, name, rest] = match; const tag = name.toLowerCase(); counts[tag] = (counts[tag] || 0) + 1;
    if (!closing && tag === "img") images.push(rest);
    if (!closing) for (const link of rest.matchAll(/href="([^"]*)"/g)) hrefs.push(link[1]);
    if (VOID_TAGS.has(tag)) continue;
    if (!closing) stack.push(tag); else { const top = stack.pop(); if (top !== tag) problems.push(`</${tag}> closes <${top}>`); }
  }
  if (stack.length) problems.push("unclosed: " + stack.join(","));
  return { problems, counts, images, hrefs };
}

check("report: authentication comes first - a wrong, missing, padded or non-string token is UNAUTHORIZED and nothing is sent", () => {
  const w = world(); w.call(upsertPayload());
  for (const token of ["wrong", "", " " + SECRET, SECRET + " ", 12345, null, { token: SECRET }]) refused(w, reportPayload({ token }), "UNAUTHORIZED", "token " + JSON.stringify(token));
  const noToken = reportPayload(); delete noToken.token; refused(w, noToken, "UNAUTHORIZED", "missing token"); noMail(w);
});
check("report: a valid request sends ONE e-mail - to EMAIL_TO, the usual subject, a plain-text body, an HTML body and the three charts as inline PNG images - and reports email_format html", () => {
  const w = world(); const r = sentReport(w); assert.equal(r.success, true, JSON.stringify(r)); assert.equal(r.action, "send_success_report"); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); assert.equal(r.sheet_status, "ok");
  assert.equal(w.state.mail.sent.length, 1); const m = lastMail(w);
  assert.equal(m.to, "owner@example.test"); assert.equal(m.subject, "Daily Market Analysis OK - 2026-10-27"); assert.ok(typeof m.body === "string" && m.body.length > 200 && typeof m.htmlBody === "string" && m.htmlBody.length > 2000);
  assert.deepEqual(Object.keys(m.inlineImages).sort(), ["chart_drawdown", "chart_indexed", "chart_returns"]);
  for (const image of Object.values(m.inlineImages)) { assert.equal(image.contentType, "image/png"); assert.equal(image.head, "89504e470d0a1a0a"); assert.ok(image.size > 100 && image.size < 6000, String(image.size)); }
});
check("report: the plain-text body is the EXISTING success body, unchanged, followed only by the link to the deliverables", () => {
  const a = world(); a.call(upsertPayload()); a.call(emailPayload()); const old = lastMail(a).body;
  const b = world(); sentReport(b); const body = lastMail(b).body;
  assert.ok(body.startsWith(old), "the existing text must come first and unchanged"); const extra = body.slice(old.length); assert.ok(extra.includes(ARTIFACT_URL) && extra.length < 400, extra);
});
check("report: NO e-mail unless the sheet really holds the rows (NOT_VERIFIED), exactly like the existing success e-mail", () => {
  const w = world(); const r = refused(w, reportPayload(), "NOT_VERIFIED", "no rows"); assert.equal(r.email_status, "not_attempted"); noMail(w);
  const t = world(); t.call(upsertPayload()); t.sheet.data[1][2] = 1; refused(t, reportPayload(), "NOT_VERIFIED", "tampered"); noMail(t);
});
check("report: duplicate protection is shared with the old action - the same run and date is sent once, whichever action comes first; another run is still sent", () => {
  const w = world(); sentReport(w); assert.equal(w.call(reportPayload()).email_status, "skipped_duplicate"); assert.equal(w.call(emailPayload()).email_status, "skipped_duplicate"); assert.equal(w.state.mail.sent.length, 1);
  const v = world(); v.call(upsertPayload()); v.call(emailPayload()); assert.equal(v.call(reportPayload()).email_status, "skipped_duplicate"); assert.equal(v.state.mail.sent.length, 1);
  const other = reportPayload({ run_id: "987654321", workflow_url: WORKFLOW_URL.replace("123456789", "987654321"), artifact_url: ARTIFACT_URL.replace("runs/123456789", "runs/987654321") }); assert.equal(w.call(other).email_status, "sent"); assert.equal(w.state.mail.sent.length, 2);
});
check("report: strict validation - an unknown or missing key, a wrong artifact link, a malformed report and every kind of bad chart are refused with INVALID_PAYLOAD; nothing is sent and nothing is remembered", () => {
  const png = (o) => pngBase64(Object.assign({ pad: 1800 }, o)); const chart = (p, i, change) => { Object.assign(p.report.charts[i], change); };
  const mutations = {
    "unknown top-level key": (p) => { p.extra = 1; }, "missing artifact_url": (p) => { delete p.artifact_url; }, "missing report": (p) => { delete p.report; },
    "artifact over http": (p) => { p.artifact_url = ARTIFACT_URL.replace("https", "http"); }, "artifact of another repository": (p) => { p.artifact_url = ARTIFACT_URL.replace("rizwana-qa", "someone-else"); },
    "artifact of another run": (p) => { p.artifact_url = ARTIFACT_URL.replace("runs/123456789", "runs/5"); }, "artifact with a query": (p) => { p.artifact_url = ARTIFACT_URL + "?x=1"; },
    "artifact with a trailing slash": (p) => { p.artifact_url = ARTIFACT_URL + "/"; }, "artifact id not digits": (p) => { p.artifact_url = ARTIFACT_URL.replace("987654321", "abc"); },
    "artifact with a line break": (p) => { p.artifact_url = ARTIFACT_URL + "\nHost: evil"; }, "artifact is a number": (p) => { p.artifact_url = 5; }, "artifact is a javascript link": (p) => { p.artifact_url = "javascript:alert(1)"; },
    "report without meta": (p) => { delete p.report.meta; }, "report without charts": (p) => { delete p.report.charts; }, "extra key in report": (p) => { p.report.html = "<b>x</b>"; }, "report is an array": (p) => { p.report = []; },
    "meta.rows as text": (p) => { p.report.meta.rows = "31"; }, "meta.rows too small": (p) => { p.report.meta.rows = 3; }, "meta.first_date invalid": (p) => { p.report.meta.first_date = "2026-13-40"; },
    "meta.data_fetched in another format": (p) => { p.report.meta.data_fetched = "yesterday"; }, "meta.generated_at missing": (p) => { delete p.report.meta.generated_at; },
    "asset missing": (p) => { delete p.report.assets.eth; }, "asset field missing": (p) => { delete p.report.assets.btc.return_7d; }, "return out of range": (p) => { p.report.assets.btc.return_30d = -3; },
    "return as text": (p) => { p.report.assets.btc.return_14d = "0.1"; }, "annualised volatility negative": (p) => { p.report.assets.eth.volatility_annualised = -1; }, "worst day date invalid": (p) => { p.report.assets.btc.worst_day.date = "x"; },
    "negative days negative": (p) => { p.report.assets.btc.negative_days = -1; }, "drawdown date invalid": (p) => { p.report.assets.btc.drawdown_peak = "2026-02-31"; },
    "pair ratio negative": (p) => { p.report.pair.volatility_ratio = -1; }, "overlap share above 1": (p) => { p.report.pair.overlap_share = 1.5; }, "pair missing field": (p) => { delete p.report.pair.joint_negative_days; },
    "coverage with 16 entries": (p) => { p.report.coverage.pop(); }, "coverage in another order": (p) => { p.report.coverage.reverse(); }, "coverage with an unknown name": (p) => { p.report.coverage[3].name = "btc_return2"; },
    "coverage valid_rows above the row count": (p) => { p.report.coverage[1].valid_rows = 99; }, "coverage valid_rows negative": (p) => { p.report.coverage[1].valid_rows = -1; },
    "coverage latest with a line break": (p) => { p.report.coverage[1].latest = "1\n2"; }, "coverage latest too long": (p) => { p.report.coverage[1].latest = "9".repeat(40); }, "coverage latest not text": (p) => { p.report.coverage[1].latest = 5; },
    "no charts": (p) => { p.report.charts = []; }, "four charts": (p) => { p.report.charts.push(Object.assign({}, p.report.charts[0], { id: "extra" })); }, "duplicate chart id": (p) => { p.report.charts[1].id = "indexed"; },
    "unknown chart id": (p) => { chart(p, 0, { id: "../etc" }); }, "chart width 601": (p) => { chart(p, 0, { width: 601 }); }, "chart height 100": (p) => { chart(p, 0, { height: 100 }); },
    "chart is not base64": (p) => { chart(p, 0, { png_base64: "***not base64***" }); }, "chart is empty": (p) => { chart(p, 0, { png_base64: "" }); }, "chart far too large": (p) => { chart(p, 0, { png_base64: png({ pad: 7000 }) }); },
    "chart without the PNG signature": (p) => { chart(p, 0, { png_base64: png({ signature: [1, 2, 3, 4, 5, 6, 7, 8] }) }); }, "chart is a truncated file": (p) => { chart(p, 0, { png_base64: Buffer.from(makePng({ pad: 1800 }).subarray(0, 20)).toString("base64") }); },
    "chart IHDR says another width": (p) => { chart(p, 0, { png_base64: png({ width: 400 }) }); }, "chart IHDR says another height": (p) => { chart(p, 0, { png_base64: png({ height: 200 }) }); },
    "chart with one damaged byte (its checksum fails)": (p) => { chart(p, 0, { png_base64: png({ corrupt: true }) }); },
    "chart base64 with line breaks inside": (p) => { const s = png({}); chart(p, 0, { png_base64: s.slice(0, 120) + "\r\n\r\n" + s.slice(120) }); },
    "chart is true colour not palette": (p) => { chart(p, 0, { png_base64: png({ colorType: 2 }) }); }, "chart without IEND": (p) => { chart(p, 0, { png_base64: png({ iend: false }) }); },
    "chart scale low above high": (p) => { chart(p, 0, { low: 200, high: 100 }); }, "chart scale as text": (p) => { chart(p, 1, { low: "-1" }); },
    "summary invalid": (p) => { p.summary.btc.close = "x"; }, "validation status unknown": (p) => { p.validation_status = "MAYBE"; },
  };
  const w = world(); w.call(upsertPayload());
  for (const [name, change] of Object.entries(mutations)) { const p = reportPayload(); change(p); const r = w.call(p); assert.equal(r.success, false, name + ": must fail"); assert.equal(r.error, "INVALID_PAYLOAD", `${name}: error code (got ${r.error})`); assert.equal(r.email_status, "not_attempted", name); }
  noMail(w); assert.equal(w.state.cache.size, 0, "a refused request must leave no deduplication entry");
});
check("report: text from the request reaches the HTML only ESCAPED - script tags, quotes, ampersands and angle brackets in observations, rule labels and variable values", () => {
  const evil = `<script>alert(1)</script> & "q" 'a' <img src=x onerror=alert(2)>`; const body = reportBody(); body.coverage[1].latest = `<b>&"x'`;
  const w = world(); const r = sentReport(w, { summary: summary({ observations: [evil], rules: { R1: evil, R2: "<i>r2</i>", R3: "r3" } }), report: body }); assert.equal(r.success, true, JSON.stringify(r));
  const m = lastMail(w), html = m.htmlBody;
  assert.ok(!/<script/i.test(html) && !/<img src=x/i.test(html) && !html.includes("<b>&\"x'") && !html.includes("<i>r2</i>"), "raw markup from the request reached the HTML");
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot; &#39;a&#39; &lt;img src=x onerror=alert(2)&gt;") && html.includes("&lt;b&gt;&amp;&quot;x&#39;"), "the text must be present, escaped");
  assert.ok(m.body.includes(evil) || m.body.includes("alert(1)"), "the plain-text part may carry the text as it is");
});
check("report: the HTML is email-safe and well formed - balanced tables, presentation roles, inline styles, no script/link/svg/form/iframe, no remote images, links only to the two GitHub addresses, every cid backed by an inline image with alt text and explicit size, under the size budget", () => {
  const w = world(); sentReport(w); const m = lastMail(w), html = m.htmlBody, s = structureOf(html);
  assert.deepEqual(s.problems, [], s.problems.join("; ")); assert.ok(html.startsWith("<!DOCTYPE html>") && /<html[^>]* lang="en"/.test(html) && /<meta name="viewport"/.test(html) && /<title>[^<]+<\/title>/.test(html));
  for (const forbidden of ["script", "link", "svg", "iframe", "form", "object", "embed", "video", "audio", "base", "input", "button"]) assert.ok(!s.counts[forbidden], "forbidden tag: " + forbidden);
  assert.ok(!/@import|url\(|javascript:|expression\(|http:\/\//i.test(html), "forbidden construct in the HTML"); assert.ok(s.counts.table >= 5 && !s.counts.div, "layout must be tables");
  for (const tag of html.match(/<table[^>]*>/g)) assert.ok(/role="presentation"/.test(tag) && /style="/.test(tag), "table without role/inline style: " + tag);
  const cids = s.images.map((attributes) => { assert.ok(/ src="cid:chart_[a-z]+"/.test(attributes) && / alt="[^"]{20,}"/.test(attributes) && / width="600"/.test(attributes) && / height="240"/.test(attributes) && /style="/.test(attributes), attributes); return /src="cid:([a-z_]+)"/.exec(attributes)[1]; });
  assert.deepEqual([...cids].sort(), Object.keys(m.inlineImages).sort(), "every cid needs exactly one inline image and the other way round");
  assert.deepEqual([...new Set(s.hrefs)].sort(), [ARTIFACT_URL, WORKFLOW_URL].sort(), "links"); assert.ok(html.length < 60000, "HTML size " + html.length);
});
check("report: the content - date and status, both closes, daily and 7/14/30-day returns, volatility, drawdown with its dates, correlation, the rule labels, the observations, all 17 variables, the verification status, the time stamps and the disclaimer", () => {
  const w = world(); sentReport(w); const text = textOf(lastMail(w).htmlBody);
  for (const needle of ["Daily Market Analysis", "2026-10-27", "PASSED", "80,001.90", "2,320.45", "-0.06%", "+0.31%", "-2.10%", "+3.40%", "-5.20%", "+5.00%", "-10.00%", "1.61%", "2.12%", "31.00%", "40.00%", "-7.52%", "-14.89%", "2026-10-02", "2026-10-15",
                         "0.928089", "strong positive correlation", "ETH noticeably more volatile", "down days largely coincide", "Both assets fell on 16 of 30 days.", "1.317", "12", "0.86", "-6.11%", "2026-10-12",
                         "Google Sheets", "verified", "2 inserted", "0 updated", "2026-10-28 02:15:12 UTC", "2026-10-28 02:15:20 UTC", "Educational use only. Not financial advice.", "17 of 17"]) assert.ok(text.includes(needle), "the e-mail lacks: " + needle);
  for (const name of COVERAGE_NAMES) assert.ok(text.includes(name), "variable missing: " + name);
});
check("report: data-quality status comes from the data - a variable with fewer valid rows than expected is shown as MISSING and the headline count drops; the two return variables are expected to have one row fewer", () => {
  const ok = world(); sentReport(ok); const good = textOf(lastMail(ok).htmlBody); assert.ok(!good.includes("MISSING") && good.includes("17 of 17"));
  const body = reportBody(); body.coverage[8].valid_rows = 29; const bad = world(); sentReport(bad, { report: body }); const text = textOf(lastMail(bad).htmlBody); assert.ok(text.includes("MISSING") && text.includes("16 of 17") && !text.includes("17 of 17"), text.slice(0, 200));
  const returns = reportBody(); returns.coverage[3].valid_rows = 31; const odd = world(); sentReport(odd, { report: returns }); assert.ok(textOf(lastMail(odd).htmlBody).includes("MISSING"), "31 valid return rows cannot be right (one fewer than the closes)");
});
// ---- no duplicate success e-mail (S4-1): the plain-text fallback is allowed ONLY while the HTML message has not been handed to the mail service. Once MailApp.sendEmail has been called for the HTML
// message, an error or an unclear outcome is reported as send_outcome_unknown and nothing more is sent. "attempts" records every call to the mail service (true = an HTML message), delivered or not.
const withHtml = (transform) => readSource().replace("function reportHtml_(d, secret) {", "function reportHtmlOriginal_(d, secret) {") + "\nfunction reportHtml_(d, secret) { return (" + transform.toString() + ")(reportHtmlOriginal_(d, secret)); }\n";
const attempts = (w) => w.state.mail.attempts.slice();
const NO_SEND_CLAIM = (r) => r.success === false && r.error === "SEND_OUTCOME_UNKNOWN" && r.email_status === "send_outcome_unknown" && r.sheet_status === "ok" && !("email_format" in r) && typeof r.message === "string" && r.message.length > 10;

check("report: HTML PREPARATION failing before any send (the images cannot be built, or the HTML builder fails) allows the plain-text fallback - one plain e-mail, no HTML attempt, reply text_fallback", () => {
  const a = world(); a.state.failBlob = true; const r = sentReport(a);
  assert.equal(r.success, true, JSON.stringify(r)); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "text_fallback"); assert.deepEqual(attempts(a), [false], "only the plain message was ever handed to the mail service");
  assert.equal(a.state.mail.sent.length, 1); assert.ok(!lastMail(a).htmlBody && !lastMail(a).inlineImages && lastMail(a).body.length > 200);
  const b = world({ source: withHtml(() => { throw new Error("the HTML builder is broken"); }) }); const r2 = sentReport(b);
  assert.equal(r2.email_format, "text_fallback"); assert.deepEqual(attempts(b), [false]); assert.equal(b.state.mail.sent.length, 1); assert.ok(!lastMail(b).htmlBody);
});
check("report: HTML VALIDATION failing before any send (a script tag, an image that is not one of the inline charts, or an over-long page) allows the plain-text fallback - no HTML attempt", () => {
  const variants = {
    "script tag": withHtml((html) => html.replace("</body>", "<script>alert(1)</script></body>")),
    "remote image instead of an inline chart": withHtml((html) => html.replace('src="cid:chart_returns"', 'src="https://example.test/chart.png"')),
    "inline chart not referenced": withHtml((html) => html.replace('src="cid:chart_drawdown"', 'src=""')),
    "an image that points to a cid with no attachment": withHtml((html) => html.replace('src="cid:chart_returns"', 'src="cid:chart_unknown"')),
    "an attached chart that the page does not show": withHtml((html) => html.replace('src="cid:chart_drawdown"', 'data-removed="x"')),
    "an extra remote image besides the three charts": withHtml((html) => html.replace("</body>", '<img src="https://example.test/x.png"></body>')),
    "over-long page": withHtml((html) => html + "x".repeat(100001)),
  };
  for (const [name, source] of Object.entries(variants)) {
    const w = world({ source }); const r = sentReport(w); assert.equal(r.success, true, name + ": " + JSON.stringify(r)); assert.equal(r.email_format, "text_fallback", name);
    assert.deepEqual(attempts(w), [false], name + ": the invalid HTML must never reach the mail service"); assert.equal(w.state.mail.sent.length, 1, name); assert.ok(!lastMail(w).htmlBody, name);
  }
  const good = world(); sentReport(good); assert.deepEqual(attempts(good), [true], "the real HTML passes the check");
});
check("report: when the HTML send succeeds exactly ONE e-mail goes out - the HTML message, reply email_format html", () => {
  const w = world(); const r = sentReport(w); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html");
  assert.deepEqual(attempts(w), [true]); assert.equal(w.state.mail.sent.length, 1); assert.ok(lastMail(w).htmlBody);
});
check("report: if the HTML send THROWS, nothing more is sent - no plain-text fallback - and the reply says send_outcome_unknown (not sent, not failed); the e-mail may or may not have been delivered", () => {
  const w = world(); w.state.mail.failHtmlWith = "The HTML message was refused"; const r = sentReport(w);
  assert.ok(NO_SEND_CLAIM(r), JSON.stringify(r)); assert.deepEqual(attempts(w), [true], "exactly one call to the mail service, for the HTML message"); assert.equal(w.state.mail.sent.length, 0);
  assert.ok(!JSON.stringify(r).includes("refused"), "the mail service's own text must not be echoed");
  const q = world(); q.state.mail.failWith = "Service invoked too many times for one day: email."; const r2 = sentReport(q);
  assert.ok(NO_SEND_CLAIM(r2), "any error from the HTML send is ambiguous, whatever its text: " + JSON.stringify(r2)); assert.deepEqual(attempts(q), [true]); assert.equal(q.state.mail.sent.length, 0);
});
check("report: an AMBIGUOUS HTML send - the message was delivered but the call then reported an error - sends no second e-mail and reports send_outcome_unknown", () => {
  const w = world(); w.state.mail.failHtmlAfterSend = "Exception: the answer was lost"; const r = sentReport(w);
  assert.ok(NO_SEND_CLAIM(r), JSON.stringify(r)); assert.deepEqual(attempts(w), [true]); assert.equal(w.state.mail.sent.length, 1, "the delivered HTML message is the only e-mail"); assert.ok(lastMail(w).htmlBody);
  assert.ok(!JSON.stringify(r).includes("answer was lost"));
});
check("report: the outcomes are told apart exactly - a pre-send double failure is EMAIL_FAILED (failed, nothing sent), a confirmed fallback is sent/text_fallback, an unknown outcome is neither", () => {
  const both = world(); both.state.failBlob = true; both.state.mail.failWith = "Service invoked too many times for one day: email."; const r = sentReport(both);
  assert.equal(r.success, false); assert.equal(r.error, "EMAIL_FAILED"); assert.equal(r.email_status, "failed"); assert.equal(r.sheet_status, "ok"); assert.ok(!("email_format" in r)); assert.deepEqual(attempts(both), [false]); assert.equal(both.state.mail.sent.length, 0);
  assert.equal(both.state.cache.size, 0, "nothing is remembered after a failure");
  both.state.mail.failWith = null; assert.equal(both.call(reportPayload()).email_format, "text_fallback", "a later request can still be sent");
  const statuses = new Set(); for (const set of [(w) => {}, (w) => { w.state.failBlob = true; }, (w) => { w.state.mail.failHtmlWith = "x"; }]) { const w = world(); set(w); statuses.add(sentReport(w).email_status); }
  assert.deepEqual([...statuses].sort(), ["send_outcome_unknown", "sent"], "only three situations exist and two of them are 'sent'");
});
check("report: duplicate protection is unchanged - after an HTML send or a confirmed fallback the same run and date is skipped (no further call to the mail service); an unknown outcome remembers nothing, so a deliberate repeat is handled as new", () => {
  const html = world(); sentReport(html); const n = attempts(html).length; assert.equal(html.call(reportPayload()).email_status, "skipped_duplicate"); assert.equal(html.call(emailPayload()).email_status, "skipped_duplicate"); assert.equal(attempts(html).length, n, "no call to the mail service for a duplicate");
  const fb = world(); fb.state.failBlob = true; sentReport(fb); const m = attempts(fb).length; assert.equal(fb.call(reportPayload()).email_status, "skipped_duplicate"); assert.equal(attempts(fb).length, m);
  const unk = world(); unk.state.mail.failHtmlWith = "x"; assert.ok(NO_SEND_CLAIM(sentReport(unk))); assert.equal(unk.state.cache.size, 0, "an unknown outcome is not recorded as sent");
  unk.state.mail.failHtmlWith = null; const again = unk.call(reportPayload()); assert.equal(again.email_status, "sent"); assert.equal(again.email_format, "html"); assert.equal(unk.call(reportPayload()).email_status, "skipped_duplicate");
});
check("report: an unknown outcome is never retried by the web app and the old action stays exactly as it was (plain text only, EMAIL_FAILED on failure, no HTML attempt)", () => {
  const w = world(); w.state.mail.failHtmlWith = "x"; w.state.mail.failHtmlAfterSend = "y"; const old = w.call(upsertPayload()); assert.equal(old.success, true);
  const sent = w.call(emailPayload()); assert.equal(sent.email_status, "sent"); assert.equal(sent.email_format, undefined); assert.deepEqual(attempts(w), [false], "the legacy action never sends HTML, so the HTML-only faults cannot touch it");
  const bad = world(); bad.call(upsertPayload()); bad.state.mail.failWith = "down"; const failed = bad.call(emailPayload()); assert.equal(failed.error, "EMAIL_FAILED"); assert.equal(failed.email_status, "failed"); assert.deepEqual(attempts(bad), [false]);
});
check("report: nothing secret leaks when the HTML send fails with an error text that contains the secret - not in the reply, not in the logs", () => {
  const w = world(); w.state.mail.failHtmlWith = "provider said: " + SECRET; const r = sentReport(w);
  assert.ok(NO_SEND_CLAIM(r)); assert.ok(!JSON.stringify(r).includes(SECRET)); assert.ok(!w.state.logs.join("\n").includes(SECRET), "the log must hold the redacted text only"); assert.ok(w.state.logs.some((line) => line.includes("[redacted]")));
});
check("report: nothing secret in the replies, the logs or the e-mails (including the fallback path), and a reply holds only the known fields", () => {
  const w = world(); w.state.failBlob = true; const replies = [sentReport(w)]; const x = world(); replies.push(sentReport(x)); const y = world(); replies.push(y.call(reportPayload({ token: "bad" })));
  for (const world_ of [w, x, y]) { assert.ok(!world_.state.logs.join("\n").includes(SECRET)); assert.ok(!world_.state.mail.sent.some((m) => JSON.stringify(m).includes(SECRET))); }
  for (const r of replies) { assert.ok(!JSON.stringify(r).includes(SECRET)); assert.ok(Object.keys(r).every((k) => ["success", "action", "sheet_status", "email_status", "email_format", "error", "message", "request_id"].includes(k)), Object.keys(r).join()); }
});
check("report: the request-size limit is untouched (20,000 characters): a realistic report fits, an oversized one is refused before anything is parsed", () => {
  const p = reportPayload(); const size = JSON.stringify(p).length; assert.ok(size < 20000, "the made-up report request is " + size + " characters"); assert.equal(world().call(JSON.stringify(Object.assign(p, { padding: "x".repeat(20000) }))).error, "INVALID_REQUEST");
  assert.ok(/const MAX_BODY_CHARS = 20000;/.test(readSource()), "MAX_BODY_CHARS must stay 20000");
});
check("report: the new action is registered next to the old ones, and an unexpected failure while it runs is reported as a failed sheet action", () => {
  const s = readSource(); assert.ok(/send_success_report: handleSuccessReport_,/.test(s) && /send_success_email: handleSuccessEmail_,/.test(s));
  assert.ok(/const SHEET_ACTIONS = \[[^\]]*'send_success_report'/.test(s));
});

// ---- no duplicate success e-mail after a cache failure (S4-2): the e-mail HAS been sent when the duplicate-protection entry is written. If that write fails, the reply must say so (sent_dedup_failed,
// success true, no error code) - an error such as INTERNAL_ERROR would make the client repeat the request and send the e-mail again. A failure BEFORE the send is still an error, and nothing was sent.
const DEDUP_FAILED = (r) => r.success === true && r.email_status === "sent_dedup_failed" && r.sheet_status === "ok" && !("error" in r) && typeof r.message === "string" && r.message.length > 10;
const SUCCESS_RUNS = { "legacy send_success_email": (w) => { w.call(upsertPayload()); return w.call(emailPayload()); }, "HTML send_success_report": (w) => sentReport(w) };

check("cache: normal recording is unchanged for both success actions - sent, one entry under ok:<run>:<date>, no extra fields, and a repeat of either action is skipped_duplicate without another call to the mail service", () => {
  for (const [name, run] of Object.entries(SUCCESS_RUNS)) {
    const w = world(); const r = run(w); assert.equal(r.success, true, name); assert.equal(r.email_status, "sent", name); assert.ok(!("message" in r), name + ": a normal reply carries no message");
    assert.deepEqual([...w.state.cache.keys()], ["ok:123456789:2026-10-27"], name); assert.equal(w.state.cachePuts, 1, name); assert.deepEqual(w.state.cacheTtl, [21600], name + ": the entry lives for the full six hours"); const calls = w.state.mail.attempts.length;
    assert.equal(w.call(emailPayload()).email_status, "skipped_duplicate", name); assert.equal(w.call(reportPayload()).email_status, "skipped_duplicate", name); assert.equal(w.state.mail.attempts.length, calls, name + ": no further call to the mail service"); assert.equal(w.state.mail.sent.length, 1, name);
  }
});
check("cache: a confirmed send followed by a FAILING cache write is reported as sent_dedup_failed - success, no error code (so nothing is retried), ONE e-mail, ONE call to the mail service, ONE write attempt - for the legacy and the HTML action", () => {
  for (const [name, run] of Object.entries(SUCCESS_RUNS)) {
    const w = world(); w.state.cacheFail.put = "Exception: cache service unavailable"; const r = run(w);
    assert.ok(DEDUP_FAILED(r), name + ": " + JSON.stringify(r)); assert.notEqual(r.error, "INTERNAL_ERROR", name); assert.equal(w.state.mail.sent.length, 1, name); assert.equal(w.state.mail.attempts.length, 1, name); assert.equal(w.state.cachePuts, 1, name + ": the write is tried once and never repeated");
    assert.ok(/sent/i.test(r.message) && !/fail|not sent|could not be sent/i.test(r.message.replace("could not be recorded", "")), name + ": the message must say the e-mail was sent: " + r.message);
    if (name.startsWith("HTML")) { assert.equal(r.email_format, "html"); assert.ok(lastMail(w).htmlBody); } else assert.ok(!("email_format" in r), name);
  }
  const fb = world(); fb.state.failBlob = true; fb.state.cacheFail.put = "x"; const r = sentReport(fb); assert.ok(DEDUP_FAILED(r)); assert.equal(r.email_format, "text_fallback", "the pre-send fallback keeps its own label"); assert.deepEqual(fb.state.mail.attempts, [false]);
});
check("cache: a genuine failure stays distinguishable - a failed send is EMAIL_FAILED (the cache is never touched), a cache READ failure before the send is INTERNAL_ERROR with nothing sent, and S4-1's send_outcome_unknown is unchanged", () => {
  const legacy = world(); legacy.call(upsertPayload()); legacy.state.mail.failWith = "down"; legacy.state.cacheFail.put = "x"; const a = legacy.call(emailPayload());
  assert.equal(a.error, "EMAIL_FAILED"); assert.equal(a.email_status, "failed"); assert.equal(legacy.state.cachePuts, 0, "no entry is written for a send that failed");
  const html = world(); html.state.failBlob = true; html.state.mail.failWith = "down"; html.state.cacheFail.put = "x"; const b = sentReport(html);
  assert.equal(b.error, "EMAIL_FAILED"); assert.equal(b.email_status, "failed"); assert.equal(html.state.cachePuts, 0); assert.equal(html.state.mail.sent.length, 0);
  for (const [name, run] of Object.entries(SUCCESS_RUNS)) { const w = world(); w.state.cacheFail.get = "Exception: cache read failed"; const r = run(w); assert.equal(r.success, false, name); assert.equal(r.error, "INTERNAL_ERROR", name); assert.equal(w.state.mail.attempts.length, 0, name + ": nothing was sent, so a repeat is safe"); }
  for (const fault of ["failHtmlWith", "failHtmlAfterSend"]) { const w = world(); w.state.mail[fault] = "boom"; w.state.cacheFail.put = "x"; const r = sentReport(w); assert.ok(NO_SEND_CLAIM(r), fault + ": " + JSON.stringify(r)); assert.equal(w.state.cachePuts, 0, fault + ": an unknown outcome writes no entry"); assert.deepEqual(attempts(w), [true], fault); }
});
check("cache: neither a secret nor the provider's error text reaches the reply or the log when the duplicate-protection write fails (only a fixed sentence and the error type are logged)", () => {
  for (const [name, run] of Object.entries(SUCCESS_RUNS)) {
    const w = world(); w.state.cacheFail.put = "provider said: " + SECRET + " quota detail xyz"; const r = run(w); assert.ok(DEDUP_FAILED(r), name);
    const logs = w.state.logs.join("\n"); assert.ok(!JSON.stringify(r).includes(SECRET) && !JSON.stringify(r).includes("xyz") && !JSON.stringify(r).includes("provider said"), name + ": reply");
    assert.ok(!logs.includes(SECRET) && !logs.includes("xyz") && !logs.includes("provider said"), name + ": log: " + logs); assert.ok(logs.includes("duplicate-protection"), name + ": the failure is logged without the provider's text");
    assert.ok(Object.keys(r).every((k) => ["success", "action", "sheet_status", "email_status", "email_format", "message", "request_id"].includes(k)), Object.keys(r).join());
  }
});

// ---- the failure notice after a cache failure (S4-3): the same protection as the success e-mails. The notice HAS been sent when its duplicate-protection entry is written; if that write fails the reply
// is a success with email_status sent_dedup_failed - an INTERNAL_ERROR would be repeated by the client and the notice would go out again. A failure BEFORE the send is still an error.
const FAIL_DEDUP = (r, sheet) => r.success === true && r.email_status === "sent_dedup_failed" && r.sheet_status === sheet && !("error" in r) && typeof r.message === "string" && r.message.length > 10;

check("failure notice: normal recording is unchanged - sent, one entry under fail:<run>:<stage> kept for six hours, no extra fields; a repeat for the same run and stage is skipped_duplicate without another call to the mail service; another stage is sent", () => {
  const w = world(); const r = w.call(failurePayload()); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.ok(!("message" in r) && !("email_format" in r));
  assert.deepEqual([...w.state.cache.keys()], ["fail:123456789:tests"]); assert.deepEqual(w.state.cacheTtl, [21600], "the entry lives for the full six hours"); const calls = w.state.mail.attempts.length;
  assert.equal(w.call(failurePayload()).email_status, "skipped_duplicate"); assert.equal(w.state.mail.attempts.length, calls, "no further call to the mail service"); assert.equal(w.call(failurePayload({ stage: "analysis" })).email_status, "sent"); assert.equal(w.state.mail.sent.length, 2);
});
check("failure notice: a confirmed send followed by a FAILING cache write is sent_dedup_failed - success, no error code, the request's own sheet status, ONE e-mail, ONE call to the mail service, ONE write attempt, never INTERNAL_ERROR", () => {
  for (const sheet of ["not_attempted", "ok", "failed", "unconfirmed"]) {
    const w = world(); w.state.cacheFail.put = "Exception: cache service unavailable"; const r = w.call(failurePayload({ sheet_status: sheet }));
    assert.ok(FAIL_DEDUP(r, sheet), sheet + ": " + JSON.stringify(r)); assert.equal(w.state.mail.sent.length, 1, sheet); assert.equal(w.state.mail.attempts.length, 1, sheet); assert.equal(w.state.cachePuts, 1, sheet + ": the write is tried once and never repeated");
    assert.ok(/sent/i.test(r.message) && !/not sent|could not be sent|fail/i.test(r.message), "the message must say the e-mail was sent: " + r.message); assert.ok(/FAILED at stage: tests/.test(lastMail(w).subject), "the notice itself is unchanged");
  }
});
check("failure notice: a genuine failure stays distinguishable - a failed send is EMAIL_FAILED with no cache write tried, a cache READ failure before the send is INTERNAL_ERROR with nothing sent, a missing recipient is NOT_CONFIGURED", () => {
  const a = world(); a.state.mail.failWith = "down"; a.state.cacheFail.put = "x"; const r = a.call(failurePayload()); assert.equal(r.error, "EMAIL_FAILED"); assert.equal(r.email_status, "failed"); assert.equal(a.state.cachePuts, 0, "no entry for a notice that was not sent"); assert.equal(a.state.mail.sent.length, 0);
  const b = world(); b.state.cacheFail.get = "Exception: cache read failed"; const r2 = b.call(failurePayload()); assert.equal(r2.success, false); assert.equal(r2.error, "INTERNAL_ERROR"); assert.equal(b.state.mail.attempts.length, 0, "nothing was sent, so a repeat is safe");
  const c = world({ properties: { EMAIL_TO: "" } }); c.state.cacheFail.put = "x"; const r3 = c.call(failurePayload()); assert.equal(r3.error, "NOT_CONFIGURED"); assert.equal(c.state.cachePuts, 0); assert.equal(c.state.mail.attempts.length, 0);
});
check("failure notice: neither a secret nor the provider's error text reaches the reply or the log when the write fails (a fixed sentence and the error type only), and a failure summary that holds the secret is still redacted in the e-mail", () => {
  const w = world(); w.state.cacheFail.put = "provider said: " + SECRET + " quota detail xyz"; const r = w.call(failurePayload({ error_summary: "boom " + SECRET })); assert.ok(FAIL_DEDUP(r, "not_attempted"), JSON.stringify(r));
  const logs = w.state.logs.join("\n"); assert.ok(!JSON.stringify(r).includes(SECRET) && !JSON.stringify(r).includes("xyz") && !JSON.stringify(r).includes("provider said"), "reply");
  assert.ok(!logs.includes(SECRET) && !logs.includes("xyz") && !logs.includes("provider said"), "log: " + logs); assert.ok(logs.includes("duplicate-protection"), "the failure is logged without the provider's text");
  assert.ok(!JSON.stringify(lastMail(w)).includes(SECRET) && lastMail(w).body.includes("[redacted]"), "the e-mail redacts the secret");
  assert.ok(Object.keys(r).every((k) => ["success", "action", "sheet_status", "email_status", "message", "request_id"].includes(k)), Object.keys(r).join());
});
check("failure notice: authentication and validation still come first even while the cache is failing (wrong token, unknown key, unknown stage: nothing sent, nothing written), and the S4-1 and S4-2 outcomes of the success actions are unchanged", () => {
  const w = world(); w.state.cacheFail.put = "x"; assert.equal(w.call(failurePayload({ token: "wrong" })).error, "UNAUTHORIZED"); assert.equal(w.call(failurePayload({ extra: 1 })).error, "INVALID_PAYLOAD"); assert.equal(w.call(failurePayload({ stage: "nope" })).error, "INVALID_PAYLOAD");
  assert.equal(w.state.mail.attempts.length, 0); assert.equal(w.state.cachePuts, 0);
  for (const [name, run] of Object.entries(SUCCESS_RUNS)) { const v = world(); v.state.cacheFail.put = "x"; assert.ok(DEDUP_FAILED(run(v)), name + ": S4-2"); }
  const u = world(); u.state.mail.failHtmlWith = "x"; u.state.cacheFail.put = "x"; assert.ok(NO_SEND_CLAIM(sentReport(u)), "S4-1"); assert.equal(u.state.cachePuts, 0);
});

console.log(`\n${passed}/${passed + failed} Apps Script (Code.gs) checks passed`);
process.exit(failed ? 1 : 0);
