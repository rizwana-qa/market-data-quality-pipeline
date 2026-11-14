// Tests for the optional report link of send_success_report in apps_script/Code.gs (report_url, report_link_status, report_link_supported), run in Node against fake Google services.
// Usage: node tests/apps_script/test_report_link.mjs     (set CODE_GS_UNDER_TEST to test another copy of the script)
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { makeWorld, readSource, SECRET, upsertPayload, reportPayload, ARTIFACT_URL } from "./harness.mjs";

let pass = 0, fail = 0;
const check = (name, fn) => { try { fn(); pass++; console.log("PASS", name); } catch (e) { fail++; console.log("FAIL", name, "|", String(e.message).split("\n")[0].slice(0, 220)); } };
const BASE = "https://rizwana-qa.github.io/market-data-quality-pipeline/reports/";
const url = (d) => BASE + d + "/market_intelligence_report.html";
const D = "2026-10-27";
const BUTTON = "Open the interactive market report";
const SUPPORT = "Opens the full interactive dashboard in your browser.";
const UNAVAILABLE = "could not be confirmed online";
const ARTIFACT_BUTTON = "Download the report, dashboard and CSV";
const LEGACY_BUTTON = "Download the dashboard, report and CSV";

const fresh = () => { const w = makeWorld(); w.call(upsertPayload()); return w; };
const html = (m) => String(m.htmlBody || "");
const text = (m) => String(m.body || "");
const logsText = (w) => w.state.logs.join("\n");
const send = (over) => { const w = fresh(); const before = JSON.stringify(w.dataRows()); const calls = w.state.setValuesCalls; const r = w.call(reportPayload(over)); return { w, r, unchanged: JSON.stringify(w.dataRows()) === before && w.state.setValuesCalls === calls }; };

check("source: Code.gs stays plain ASCII and holds the exact Pages pattern for this repository", () => {
  const s = readSource(); assert.ok(!/[^\x00-\x7f]/.test(s)); assert.ok(s.includes("rizwana-qa\\.github\\.io\\/market-data-quality-pipeline\\/reports\\/(\\d{4}-\\d{2}-\\d{2})\\/market_intelligence_report\\.html$/"));
});

// ---- backward compatibility
check("legacy: neither field -> an HTML e-mail with the unchanged deliverables wording and button, no report text, no log line", () => {
  const { w, r, unchanged } = send({}); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); assert.ok(unchanged);
  const m = w.state.mail.sent[0]; assert.ok(html(m).includes(LEGACY_BUTTON) && html(m).includes('href="' + ARTIFACT_URL + '"'));
  assert.ok(html(m).includes("The full interactive HTML dashboard, the Markdown report and the CSV data are in one downloadable file."));
  assert.ok(!html(m).includes("Interactive market report") && !html(m).includes(BUTTON) && !html(m).includes(UNAVAILABLE) && !html(m).includes("github.io")); assert.ok(!text(m).includes("Interactive market report") && !text(m).includes(UNAVAILABLE)); assert.ok(!logsText(w).includes("report link"));
});

check("legacy: the e-mail of a request without link fields is byte for byte the e-mail the script sent before this feature (SHA-256 of the HTML and of the plain text, subject, 3 inline charts)", () => {
  const { w } = send({}); const m = w.state.mail.sent[0]; const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
  assert.equal(m.subject, "Daily Market Analysis OK - 2026-10-27"); assert.equal(Object.keys(m.inlineImages).length, 3);
  assert.equal(sha(html(m)), "2b65cdd4c17b177a9b2559469e3717f85bcfffaef270b10e3cbbc75378c39b01"); assert.equal(sha(text(m)), "a262b2eef3e559a8c042f9f289cb1184cdd3be58a8b2e3aeb041cc09982892c0");
});

// ---- verified link
check("verified: the primary button points at the exact same-date Pages report, with the supporting sentence; the artifact stays as a secondary download; no script", () => {
  const { w, r, unchanged } = send({ report_url: url(D) }); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); assert.ok(unchanged);
  const m = w.state.mail.sent[0], h = html(m);
  assert.ok(h.includes('href="' + url(D) + '"') && h.includes(BUTTON) && h.includes(SUPPORT) && !h.includes(UNAVAILABLE));
  assert.equal(h.split(url(D)).length - 1, 1, "the report address appears exactly once in the HTML");
  assert.ok(h.includes('href="' + ARTIFACT_URL + '"') && h.includes(ARTIFACT_BUTTON) && h.includes("The interactive market report, the interactive HTML dashboard"));
  assert.ok(h.indexOf(BUTTON) < h.indexOf("Market brief"), "the report button comes first");
  assert.ok(!/<script/i.test(h)); assert.ok(text(m).includes("Interactive market report: " + url(D)) && text(m).includes(ARTIFACT_URL)); assert.ok(!logsText(w).includes("report link"));
});

// ---- client-reported unavailable
check("unavailable: no Pages button anywhere; the notice names the artifact, which keeps its own button and sign-in note", () => {
  const { w, r, unchanged } = send({ report_link_status: "unavailable" }); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); assert.ok(unchanged);
  const m = w.state.mail.sent[0], h = html(m);
  assert.ok(h.includes(UNAVAILABLE) && h.includes("downloadable workflow artifact") && h.includes("GitHub sign-in is required") && h.includes('href="' + ARTIFACT_URL + '"') && h.includes(ARTIFACT_BUTTON));
  assert.ok(!h.includes(BUTTON) && !h.includes("github.io") && !h.includes(SUPPORT)); assert.ok(text(m).includes(UNAVAILABLE) && text(m).includes(ARTIFACT_URL) && !text(m).includes("github.io"));
  assert.ok(!logsText(w).includes("report link"), "a client-reported 'unavailable' is normal and logs nothing");
});

// ---- invalid or mismatched report_url: still delivered, with the artifact fallback; the unsafe value appears nowhere
const badUrls = {
  "previous day": [url("2026-10-26"), "date_mismatch"], "next day": [url("2026-10-28"), "date_mismatch"], "other year": [url("2025-10-27"), "date_mismatch"],
  "wrong host": ["https://evil.example/market-data-quality-pipeline/reports/" + D + "/market_intelligence_report.html", "pattern"],
  "wrong repo": [url(D).replace("market-data-quality-pipeline", "other"), "pattern"], "wrong owner": [url(D).replace("rizwana-qa", "someone"), "pattern"],
  "http": [url(D).replace("https", "http"), "pattern"], "query": [url(D) + "?x=1", "pattern"], "fragment": [url(D) + "#a", "pattern"], "extra path": [url(D) + "/x", "pattern"],
  "other file": [url(D).replace("market_intelligence_report", "dashboard"), "pattern"], "trailing space": [url(D) + " ", "pattern"], "user-info": [url(D).replace("https://", "https://a@"), "pattern"],
  "impossible date": [url("2026-02-30"), "pattern"], "empty": ["", "pattern"], "uppercase host": [url(D).replace("rizwana-qa.github.io", "RIZWANA-QA.GITHUB.IO"), "pattern"],
  "double slash": [url(D).replace("/reports/", "//reports/"), "pattern"], "script text": ["javascript:alert(1)", "pattern"], "markup": ['"><script>x</script>', "pattern"],
  "number": [5, "not_text"], "null": [null, "not_text"], "array": [[url(D)], "not_text"], "object": [{ u: url(D) }, "not_text"],
};
for (const [name, [value, reason]] of Object.entries(badUrls)) {
  check("invalid report_url (" + name + "): sent as HTML with the artifact fallback, reason '" + reason + "' logged, value never echoed, sheet untouched", () => {
    const { w, r, unchanged } = send({ report_url: value }); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); assert.ok(unchanged); assert.equal(w.state.mail.sent.length, 1);
    const m = w.state.mail.sent[0], h = html(m), t = text(m), log = logsText(w);
    assert.ok(h.includes(UNAVAILABLE) && h.includes('href="' + ARTIFACT_URL + '"') && !h.includes(BUTTON) && t.includes(UNAVAILABLE) && t.includes(ARTIFACT_URL));
    assert.ok(log.includes("report link rejected (" + reason + ")"), log); assert.ok(!h.includes("github.io") && !t.includes("github.io"));
    const raw = typeof value === "string" ? value : JSON.stringify(value); if (raw.length > 8) assert.ok(!h.includes(raw) && !t.includes(raw) && !log.includes(raw), "the rejected value must not be echoed");
    assert.ok(!/<script/i.test(h));
  });
}
check("invalid: both fields together -> fallback (both_fields); even the valid URL is not used", () => {
  const { w, r } = send({ report_url: url(D), report_link_status: "unavailable" }); assert.equal(r.email_status, "sent"); const h = html(w.state.mail.sent[0]);
  assert.ok(h.includes(UNAVAILABLE) && !h.includes(BUTTON) && !h.includes("github.io")); assert.ok(logsText(w).includes("report link rejected (both_fields)"));
});
for (const [name, value] of [["verified", "verified"], ["upper case", "UNAVAILABLE"], ["number", 1], ["null", null], ["empty", ""]]) {
  check("invalid: report_link_status " + name + " -> fallback (unknown_status)", () => {
    const { w, r } = send({ report_link_status: value }); assert.equal(r.email_status, "sent"); assert.ok(html(w.state.mail.sent[0]).includes(UNAVAILABLE)); assert.ok(logsText(w).includes("report link rejected (unknown_status)"));
  });
}

// ---- the fallback destination is validated on its own: an invalid artifact URL refuses the request whatever the link fields say
const badArtifacts = {
  "wrong run id": ARTIFACT_URL.replace("/runs/123456789/", "/runs/999/"), "wrong repo": ARTIFACT_URL.replace("market-data-quality-pipeline", "other"), "wrong owner": ARTIFACT_URL.replace("rizwana-qa", "someone"),
  "http": ARTIFACT_URL.replace("https", "http"), "trailing text": ARTIFACT_URL + "/x", "query": ARTIFACT_URL + "?a=1", "empty": "", "not text": 7, "null": null, "relative": "/actions/runs/123456789/artifacts/987654321",
};
const linkVariants = { "no link field": {}, "valid report_url": { report_url: url(D) }, "unavailable": { report_link_status: "unavailable" }, "invalid report_url": { report_url: url("2026-10-26") } };
check("fallback validation: ten invalid artifact URLs x four link variants -> refused, nothing sent, nothing written", () => {
  for (const [an, av] of Object.entries(badArtifacts)) for (const [ln, lv] of Object.entries(linkVariants)) {
    const { w, r, unchanged } = send(Object.assign({ artifact_url: av }, lv)); assert.equal(r.success, false, an + " + " + ln); assert.match(String(r.error), /INVALID/); assert.equal(w.state.mail.sent.length, 0); assert.ok(unchanged);
  }
});
check("fallback validation: a missing artifact_url is refused (exact keys)", () => {
  const w = fresh(); const p = reportPayload({ report_link_status: "unavailable" }); delete p.artifact_url; const r = w.call(p); assert.equal(r.success, false); assert.equal(w.state.mail.sent.length, 0);
});

// ---- the other contract rules are unchanged
check("an unknown extra key is still refused (with and without a link field)", () => { for (const over of [{ extra: 1 }, { extra: 1, report_url: url(D) }]) { const { w, r } = send(over); assert.equal(r.success, false); assert.equal(w.state.mail.sent.length, 0); } });
check("authorization: a wrong or missing token gives UNAUTHORIZED for every link variant; nothing sent, nothing logged about the link", () => {
  for (const lv of Object.values(linkVariants)) for (const token of ["x", "", null, SECRET + " "]) { const { w, r } = send(Object.assign({ token }, lv)); assert.equal(r.error, "UNAUTHORIZED"); assert.equal(w.state.mail.sent.length, 0); assert.ok(!logsText(w).includes("report link")); }
  const w = fresh(); const p = reportPayload({ report_url: url(D) }); delete p.token; assert.equal(w.call(p).error, "UNAUTHORIZED"); assert.equal(w.state.mail.sent.length, 0);
});
check("no verified rows -> NOT_VERIFIED and nothing sent, whatever the link says", () => {
  const w = makeWorld(); const r = w.call(reportPayload({ report_url: url("2026-10-26") })); assert.equal(r.success, false); assert.equal(r.error, "NOT_VERIFIED"); assert.equal(w.state.mail.sent.length, 0);
});
check("duplicate protection: one e-mail per run and date whatever the link fields; a different run is a new e-mail", () => {
  const w = fresh(); assert.equal(w.call(reportPayload({ report_url: url("2026-10-26") })).email_status, "sent"); assert.equal(w.call(reportPayload({ report_url: url(D) })).email_status, "skipped_duplicate");
  assert.equal(w.call(reportPayload({})).email_status, "skipped_duplicate"); assert.equal(w.state.mail.sent.length, 1);
  const other = w.call(reportPayload({ run_id: "987654321", workflow_url: reportPayload().workflow_url.replace("123456789", "987654321"), artifact_url: ARTIFACT_URL.replace("/runs/123456789/", "/runs/987654321/"), report_url: url(D) }));
  assert.equal(other.email_status, "sent"); assert.equal(w.state.mail.sent.length, 2);
});
check("text fallback still works: if the HTML cannot be prepared one plain-text e-mail goes out and carries the verified link (or the artifact notice) - never a second e-mail", () => {
  for (const [over, expected] of [[{ report_url: url(D) }, "Interactive market report: " + url(D)], [{ report_link_status: "unavailable" }, UNAVAILABLE], [{}, "Deliverables (HTML dashboard, report and CSV; GitHub sign-in required): " + ARTIFACT_URL]]) {
    const w = fresh(); w.state.failBlob = true; const r = w.call(reportPayload(over)); assert.equal(r.success, true); assert.equal(r.email_format, "text_fallback");
    assert.equal(w.state.mail.sent.length, 1); assert.ok(!w.state.mail.sent[0].htmlBody); assert.ok(text(w.state.mail.sent[0]).includes(expected), expected);
  }
});

// ---- capability flags
check("self_check: report_link_supported is true next to report_evidence_supported (unchanged) and every existing key", () => {
  const w = makeWorld(); const r = w.call({ action: "self_check", token: SECRET }); assert.equal(r.report_link_supported, true); assert.equal(r.report_evidence_supported, true);
  for (const key of ["success", "properties", "spreadsheet_found", "sheet_found", "headers_ok", "email_valid"]) assert.ok(key in r, key);
  const named = w.call({ action: "self_check", token: SECRET, expected_sheet_name: "Sheet1" }); assert.equal(named.report_link_supported, true); assert.equal(typeof named.sheet_name_matches, "boolean");
  assert.equal(w.state.mail.sent.length, 0); assert.deepEqual(w.dataRows(), []);
});

console.log(`\n${pass}/${pass + fail} report link checks passed`);
process.exit(fail ? 1 : 0);
