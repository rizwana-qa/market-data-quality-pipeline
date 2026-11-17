// Tests for the market snapshot (open, high, low, close of both assets) and the data-freshness line of the success e-mail in apps_script/Code.gs, run in Node against fake Google services.
// Usage: node tests/apps_script/test_snapshot_freshness.mjs     (set CODE_GS_UNDER_TEST to test another copy of the script)
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { makeWorld, SECRET, upsertPayload, reportPayload, reportBody, summary } from "./harness.mjs";

let pass = 0, fail = 0;
const check = (name, fn) => { try { fn(); pass++; console.log("PASS", name); } catch (e) { fail++; console.log("FAIL", name, "|", String(e.message).split("\n")[0].slice(0, 220)); } };
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
const fresh = () => { const w = makeWorld(); w.call(upsertPayload()); return w; };
const html = (m) => String(m.htmlBody || ""), text = (m) => String(m.body || "");
const OHLC = { date: "2026-10-27", btc: { open: 80500.5, high: 81200.25, low: 79800.1, close: 80001.9 }, eth: { open: 2345.67, high: 2390.12, low: 2301.5, close: 2320.45 } };
const withOhlc = (ohlc, extra = {}) => reportPayload(Object.assign({ report: Object.assign(reportBody(), { ohlc }) }, extra));
const send = (payload) => { const w = fresh(); const r = w.call(payload); return { w, r, mail: w.state.mail.sent[0] }; };
const FRESH = /<p style="[^"]*">Data fetched from Kraken \(UTC\): <strong style="color:#ffffff;">([^<]*)<\/strong><\/p>/;
const clone = (o) => JSON.parse(JSON.stringify(o));

// ---- freshness: the validated fetch time of the pipeline, next to the analysis date
check("freshness: every e-mail states the UTC fetch time from report.meta.data_fetched, once, in the header right after the analysis date and before the reporting period", () => {
  for (const over of [{}, { report_url: "https://rizwana-qa.github.io/market-data-quality-pipeline/reports/2026-10-27/market_intelligence_report.html" }, { report_link_status: "unavailable" }]) {
    const { r, mail } = send(reportPayload(over)); assert.equal(r.email_format, "html"); const h = html(mail);
    assert.equal(h.split("Data fetched from Kraken (UTC)").length - 1, 1); assert.equal(FRESH.exec(h)[1], "2026-10-28 02:15:12 UTC");
    assert.ok(h.indexOf("Analysis date (last completed UTC day)") < h.indexOf("Data fetched from Kraken (UTC)") && h.indexOf("Data fetched from Kraken (UTC)") < h.indexOf("Reporting period"));
    assert.ok(text(mail).includes("Data fetched from Kraken (UTC): 2026-10-28 02:15:12 UTC"));
  }
});
check("freshness: it is the recorded fetch time, never the generation time and never a fixed text - another valid time appears as given, and the generated_at time appears nowhere in the e-mail", () => {
  const body = reportBody(); body.meta = Object.assign({}, body.meta, { data_fetched: "2026-10-28 06:56:33 UTC", generated_at: "2026-10-28 07:01:09 UTC" });
  const { mail } = send(reportPayload({ report: body })); assert.equal(FRESH.exec(html(mail))[1], "2026-10-28 06:56:33 UTC");
  const h = html(mail); assert.ok(!h.includes("02:15:12"), "the old fixed time must be gone");
  const header = FRESH.exec(h)[0]; assert.ok(!header.includes("07:01:09"), "the freshness line never carries the generation time");
  assert.ok(!text(mail).includes("Data fetched from Kraken (UTC): 2026-10-28 07:01:09"));
});
check("freshness: an invalid or missing fetch time is still refused by the existing validation (nothing is sent, nothing is invented)", () => {
  for (const value of ["2026-10-28 02:15:12", "2026-10-28T02:15:12 UTC", "2026-13-40 02:15:12 UTC", "yesterday", null, 5, ""]) {
    const body = reportBody(); body.meta = Object.assign({}, body.meta, { data_fetched: value }); const { w, r } = send(reportPayload({ report: body })); assert.equal(r.success, false); assert.equal(w.state.mail.sent.length, 0);
  }
  const body = reportBody(); delete body.meta.data_fetched; const { w, r } = send(reportPayload({ report: body })); assert.equal(r.success, false); assert.equal(w.state.mail.sent.length, 0);
});
check("legacy: apart from that one line the e-mail is byte for byte the e-mail the script sent before (SHA-256 of the HTML and of the plain text after removing exactly the freshness lines)", () => {
  const { mail } = send(reportPayload({})); const h = html(mail).replace(FRESH, ""); const t = text(mail).replace("\n\nData fetched from Kraken (UTC): 2026-10-28 02:15:12 UTC", "");
  assert.notEqual(h, html(mail)); assert.notEqual(t, text(mail));
  assert.equal(sha(h), "86bd317c1c553697f91a78d23520fae3f6f1741e9e99c5d85f4921264a2e5d98"); assert.equal(sha(t), "a262b2eef3e559a8c042f9f289cb1184cdd3be58a8b2e3aeb041cc09982892c0");
  assert.ok(!html(mail).includes("Market snapshot"), "an older client sends no OHLC part, so there is no snapshot");
});

// ---- the market snapshot
const rowValues = (h, label) => { const m = new RegExp(">" + label + "</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>").exec(h); return m ? [m[1], m[2]] : null; };
check("snapshot: Open, High, Low and Close of BTC and ETH from the report, in a labelled table (rows in that order, BTC column then ETH column), USD with thousands separators and two decimals", () => {
  const { r, mail } = send(withOhlc(OHLC)); assert.equal(r.success, true); assert.equal(r.email_status, "sent"); assert.equal(r.email_format, "html"); const h = html(mail);
  assert.ok(h.includes(">Market snapshot</h2>") && h.includes("Latest completed UTC day, 2026-10-27.") && h.includes(">Bitcoin (BTC)</td>") && h.includes(">Ethereum (ETH)</td>"));
  assert.deepEqual(rowValues(h, "Open"), ["80,500.50", "2,345.67"]); assert.deepEqual(rowValues(h, "High"), ["81,200.25", "2,390.12"]);
  assert.deepEqual(rowValues(h, "Low"), ["79,800.10", "2,301.50"]); assert.deepEqual(rowValues(h, "Close"), ["80,001.90", "2,320.45"]);
  assert.ok(h.indexOf(">Open<") < h.indexOf(">High<") && h.indexOf(">High<") < h.indexOf(">Low<") && h.indexOf(">Low<") < h.indexOf(">Close<"));
  assert.ok(!/<script|<svg|<button|http:\/\//i.test(h)); assert.ok(h.length < 100000);
});
check("snapshot: it sits between the market brief and the latest figures, the existing sections and values are all still there, and the freshness line is present", () => {
  const { mail } = send(withOhlc(OHLC)); const h = html(mail);
  assert.ok(h.indexOf(">Market brief</h2>") < h.indexOf(">Market snapshot</h2>") && h.indexOf(">Market snapshot</h2>") < h.indexOf(">Latest figures</h2>"));
  for (const s of [">Latest figures</h2>", ">Risk and volatility</h2>", ">How the assets moved together</h2>", "80,001.90 <span", "2,320.45 <span", "Data fetched from Kraken (UTC)", "Download the dashboard, report and CSV"]) assert.ok(h.includes(s), s);
  assert.equal((h.match(/cid:chart_/g) || []).length, 3);
});
check("snapshot: it is dynamic - other values give other numbers and nothing is hard-coded; with a verified report link both blocks appear", () => {
  const o = clone(OHLC); o.btc = { open: 1234.5, high: 90000, low: 1000.01, close: 80001.9 };
  const { mail } = send(withOhlc(o, { report_url: "https://rizwana-qa.github.io/market-data-quality-pipeline/reports/2026-10-27/market_intelligence_report.html" })); const h = html(mail);
  assert.deepEqual(rowValues(h, "Open"), ["1,234.50", "2,345.67"]); assert.deepEqual(rowValues(h, "High"), ["90,000.00", "2,390.12"]); assert.deepEqual(rowValues(h, "Low"), ["1,000.01", "2,301.50"]);
  assert.ok(h.includes("Open the interactive market report") && h.includes(">Market snapshot</h2>"));
});
check("snapshot: the plain-text fallback e-mail still goes out when the HTML cannot be prepared (and carries the fetch time)", () => {
  const w = fresh(); w.state.failBlob = true; const r = w.call(withOhlc(OHLC)); assert.equal(r.success, true); assert.equal(r.email_format, "text_fallback"); assert.equal(w.state.mail.sent.length, 1);
  assert.ok(text(w.state.mail.sent[0]).includes("Data fetched from Kraken (UTC): 2026-10-28 02:15:12 UTC"));
});

// ---- validation of the OHLC part: nothing wrong is ever shown
const bad = (name, mutate) => check("snapshot refused: " + name + " -> INVALID, nothing sent, nothing written", () => {
  const o = clone(OHLC); const v = mutate(o); const payload = v === undefined ? withOhlc(o) : withOhlc(v);
  const w = fresh(); const before = JSON.stringify(w.dataRows()); const calls = w.state.setValuesCalls; const r = w.call(payload);
  assert.equal(r.success, false); assert.match(String(r.error), /INVALID/); assert.equal(w.state.mail.sent.length, 0); assert.equal(JSON.stringify(w.dataRows()), before); assert.equal(w.state.setValuesCalls, calls);
});
bad("a missing asset", (o) => { delete o.eth; });
bad("a missing field", (o) => { delete o.btc.low; });
bad("an extra field", (o) => { o.btc.volume = 1; });
bad("an extra top-level key", (o) => { o.source = "x"; });
bad("numbers as text", (o) => { o.btc.open = "80500.5"; });
bad("a zero price", (o) => { o.eth.low = 0; });
bad("a negative price", (o) => { o.eth.open = -1; });
bad("an infinite price", (o) => { o.btc.high = Infinity; });
bad("a NaN price", (o) => { o.btc.high = NaN; });
bad("a null price", (o) => { o.btc.close = null; });
bad("low above high", (o) => { o.btc.low = 82000; });
bad("open above high", (o) => { o.eth.open = 2500; });
bad("close below low", (o) => { o.eth.close = 2200; });
bad("not an object", () => "80000");
bad("null", () => null);
bad("an array", () => [OHLC]);
bad("the previous day", (o) => { o.date = "2026-10-26"; });
bad("the next day", (o) => { o.date = "2026-10-28"; });
bad("an impossible date", (o) => { o.date = "2026-02-30"; });
bad("a BTC close that differs from the summary close", (o) => { o.btc.close = 80002.9; o.btc.high = 81200.25; });
bad("an ETH close that differs from the summary close", (o) => { o.eth.close = 2321.45; });
check("snapshot refused: an unknown extra part of the report is still refused (only comparison, validation and ohlc are optional)", () => {
  const body = reportBody(); body.market = OHLC; const w = fresh(); const r = w.call(reportPayload({ report: body })); assert.equal(r.success, false); assert.equal(w.state.mail.sent.length, 0);
});

// ---- the other contract rules are unchanged
check("authorization and duplicate protection are unchanged for a report with OHLC: a wrong token is refused, the same run and date is e-mailed once", () => {
  const w = fresh(); assert.equal(w.call(withOhlc(OHLC, { token: "x" })).error, "UNAUTHORIZED"); assert.equal(w.state.mail.sent.length, 0);
  assert.equal(w.call(withOhlc(OHLC)).email_status, "sent"); assert.equal(w.call(withOhlc(OHLC)).email_status, "skipped_duplicate"); assert.equal(w.call(reportPayload({})).email_status, "skipped_duplicate"); assert.equal(w.state.mail.sent.length, 1);
});
check("self_check: report_ohlc_supported is true next to the existing capability flags and every existing key", () => {
  const w = makeWorld(); const r = w.call({ action: "self_check", token: SECRET }); assert.equal(r.report_ohlc_supported, true); assert.equal(r.report_link_supported, true); assert.equal(r.report_evidence_supported, true);
  for (const key of ["success", "properties", "spreadsheet_found", "sheet_found", "headers_ok", "email_valid"]) assert.ok(key in r, key);
  assert.equal(w.state.mail.sent.length, 0); assert.deepEqual(w.dataRows(), []);
});

console.log(`\n${pass}/${pass + fail} market snapshot and freshness checks passed`);
process.exit(fail ? 1 : 0);
