// Test harness for apps_script/Code.gs: loads the real source into a Node vm context and replaces every Google service with a small fake.
// Nothing here talks to Google. The fakes imitate the behaviours that matter for correctness: text-versus-date auto-conversion of cells, sheet-size limits, locks, mail failures.
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const SOURCE_PATH = path.join(HERE, "..", "..", "apps_script", "Code.gs");
export const HEADERS = ["Run date", "Asset", "Closing price", "Daily return", "30-day volatility", "30-day drawdown", "Correlation", "Validation status", "Report status", "Workflow run ID"];
export const SECRET = "unit-test-shared-secret-0123456789";
export const WORKFLOW_URL = "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789";

export function readSource() { return fs.readFileSync(SOURCE_PATH, "utf8"); }

export function btcRow(over = {}) {
  return Object.assign({ "Run date": "2026-10-27", "Asset": "BTC", "Closing price": 80001.9, "Daily return": -0.000582, "30-day volatility": 0.0161, "30-day drawdown": -0.0752, "Correlation": 0.928089, "Validation status": "PASSED", "Report status": "GENERATED", "Workflow run ID": "123456789" }, over);
}
export function ethRow(over = {}) {
  return Object.assign({ "Run date": "2026-10-27", "Asset": "ETH", "Closing price": 2320.45, "Daily return": 0.0031, "30-day volatility": 0.0212, "30-day drawdown": -0.1489, "Correlation": 0.928089, "Validation status": "PASSED", "Report status": "GENERATED", "Workflow run ID": "123456789" }, over);
}
export function summary(over = {}) {
  return Object.assign({
    btc: { close: 80001.9, daily_return: -0.000582, volatility: 0.0161, drawdown: -0.0752 }, eth: { close: 2320.45, daily_return: 0.0031, volatility: 0.0212, drawdown: -0.1489 },
    correlation: 0.928089, rules: { R1: "strong positive correlation", R2: "ETH noticeably more volatile", R3: "down days largely coincide" },
    observations: ["Both assets fell on 16 of 30 days.", "Ethereum's maximum drawdown was deeper than Bitcoin's."],
  }, over);
}
export function upsertPayload(over = {}) { return Object.assign({ action: "upsert_analysis", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow(), ethRow()] }, over); }
export function emailPayload(over = {}) {
  return Object.assign({ action: "send_success_email", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow(), ethRow()], run_id: "123456789", workflow_url: WORKFLOW_URL, validation_status: "PASSED", sheet_update: { rows_inserted: 2, rows_updated: 0 }, summary: summary() }, over);
}
export function failurePayload(over = {}) {
  return Object.assign({ action: "notify_failure", token: SECRET, stage: "tests", error_summary: "3 of 172 checks failed", sheet_status: "not_attempted", run_id: "123456789", workflow_url: WORKFLOW_URL }, over);
}

// ---- the success REPORT request (send_success_report): an artifact link, a structured report and up to three small PNG charts. Everything is made up; the PNGs are real, valid files without a picture of anything.
export const ARTIFACT_URL = "https://github.com/rizwana-qa/market-data-quality-pipeline/actions/runs/123456789/artifacts/987654321";
export const COVERAGE_NAMES = ["date", "btc_close", "eth_close", "btc_return", "eth_return", "btc_open", "btc_high", "btc_low", "btc_volume", "btc_vwap", "btc_trade_count", "eth_open", "eth_high", "eth_low", "eth_volume", "eth_vwap", "eth_trade_count"];
const crcTable = (() => { const table = []; for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table.push(c >>> 0); } return table; })();
const crc32 = (buffer) => { let c = 0xffffffff; for (const b of buffer) c = crcTable[(c ^ b) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
const pngChunk = (type, data) => { const length = Buffer.alloc(4); length.writeUInt32BE(data.length); const body = Buffer.concat([Buffer.from(type, "ascii"), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body)); return Buffer.concat([length, body, crc]); };
export function makePng({ width = 600, height = 240, pad = 0, colorType = 3, depth = 8, signature = [137, 80, 78, 71, 13, 10, 26, 10], iend = true, corrupt = false } = {}) {
  const header = Buffer.alloc(13); header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = depth; header[9] = colorType;
  const rows = Buffer.alloc((1 + Math.ceil((width * depth) / 8)) * height);
  const filler = pad ? Buffer.concat(Array.from({ length: Math.ceil(pad / 32) }, (_, i) => crypto.createHash("sha256").update(String(i)).digest())).subarray(0, pad) : null;     // incompressible but reproducible
  const parts = [Buffer.from(signature), pngChunk("IHDR", header), pngChunk("PLTE", Buffer.from([255, 255, 255, 247, 147, 26, 98, 126, 234])), pngChunk("IDAT", zlib.deflateSync(rows))];
  if (corrupt) parts[3][12] ^= 0xff;                                               // one damaged byte inside the image data: the chunk no longer matches its checksum
  if (filler) parts.push(pngChunk("tEXt", filler));
  if (iend) parts.push(pngChunk("IEND", Buffer.alloc(0)));
  return Buffer.concat(parts);
}
export const pngBase64 = (options) => makePng(options).toString("base64");
export function reportBody(over = {}) {
  const asset = (r7, r14, r30, vol) => ({ return_7d: r7, return_14d: r14, return_30d: r30, volatility_annualised: vol, worst_day: { value: -0.0611, date: "2026-10-12" }, negative_days: 14, drawdown_peak: "2026-10-02", drawdown_trough: "2026-10-15" });
  return Object.assign({
    meta: { rows: 31, first_date: "2026-09-27", data_fetched: "2026-10-28 02:15:12 UTC", generated_at: "2026-10-28 02:15:20 UTC" },
    assets: { btc: asset(-0.021, 0.034, -0.052, 0.31), eth: asset(-0.03, 0.05, -0.1, 0.4) },
    pair: { volatility_ratio: 1.317, joint_negative_days: 12, overlap_share: 0.86 },
    coverage: COVERAGE_NAMES.map((name, i) => ({ name, valid_rows: name.endsWith("_return") ? 30 : 31, latest: name === "date" ? "2026-10-27" : String(1000 + i * 1.5) })),
    charts: [["indexed", 80.4, 104.2, 94.8, 87.3], ["returns", -6.1, 6.9, -0.06, 0.31], ["drawdown", -16, 0.5, -7.52, -14.89]].map(([id, low, high, btc_last, eth_last]) => ({ id, png_base64: pngBase64({ pad: 1800 }), width: 600, height: 240, low, high, btc_last, eth_last })),
  }, over);
}
export function reportPayload(over = {}) {
  return Object.assign({ action: "send_success_report", token: SECRET, analysis_date: "2026-10-27", rows: [btcRow(), ethRow()], run_id: "123456789", workflow_url: WORKFLOW_URL, validation_status: "PASSED",
    sheet_update: { rows_inserted: 2, rows_updated: 0 }, summary: summary(), artifact_url: ARTIFACT_URL, report: reportBody() }, over);
}

const toSigned = (buffer) => Array.from(buffer, (b) => (b > 127 ? b - 256 : b));

export function makeWorld(options = {}) {
  const state = {
    properties: Object.assign({ SPREADSHEET_ID: "test-spreadsheet-id", SHEET_NAME: "Sheet1", SHARED_SECRET: SECRET, EMAIL_TO: "owner@example.test", DATE_HEADER: "Run date", ASSET_HEADER: "Asset" }, options.properties || {}),
    sheets: {}, knownSpreadsheetId: "test-spreadsheet-id", timeZone: "Asia/Dubai",
    mail: { sent: [], attempts: [], failWith: null, failHtmlWith: null, failHtmlAfterSend: null }, failBlob: false, lock: { busy: false, held: false, waits: 0, releases: 0 }, cache: new Map(), cacheFail: { get: null, put: null }, cachePuts: 0, cacheTtl: [],
    logs: [], setValuesCalls: 0, afterSetValues: null, now: options.now || new Date("2026-10-28T02:15:30Z"),
  };
  const headerRow = options.headers || HEADERS;
  const sheet = { name: "Sheet1", data: [headerRow.slice()], formats: [headerRow.map(() => "General")], maxRows: options.maxRows || 1000 };
  state.sheets.Sheet1 = sheet;
  if (options.extraSheet) state.sheets[options.extraSheet] = { name: options.extraSheet, data: [["unrelated"]], formats: [["General"]], maxRows: 100 };
  if (options.sheetRows) { for (const r of options.sheetRows) { sheet.data.push(r.slice()); sheet.formats.push(r.map(() => "General")); } }

  const autoParse = (value, format) => (format !== "@" && typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(Date.UTC(+value.slice(0, 4), +value.slice(5, 7) - 1, +value.slice(8, 10))) : value);
  const cols = (s) => s.data.reduce((m, r) => Math.max(m, r.length), 0);
  const makeSheet = (s) => ({
    getName: () => s.name,
    getLastRow: () => { let last = 0; s.data.forEach((r, i) => { if (r.some((c) => c !== "" && c !== null && c !== undefined)) last = i + 1; }); return last; },
    getLastColumn: () => cols(s), getMaxRows: () => s.maxRows,
    insertRowsAfter: (pos, n) => { s.maxRows += n; },
    getRange: (row, col, nr = 1, nc = 1) => {
      if (row < 1 || col < 1 || row + nr - 1 > s.maxRows) throw new Error("The coordinates of the range are outside the dimensions of the sheet.");
      return {
        getValues: () => { const out = []; for (let r = 0; r < nr; r++) { const line = []; for (let c = 0; c < nc; c++) { const v = (s.data[row - 1 + r] || [])[col - 1 + c]; line.push(v === undefined ? "" : v); } out.push(line); } return out; },
        setValues: (values) => {
          if (values.length !== nr || values.some((v) => v.length !== nc)) throw new Error("The number of rows or columns in the data does not match the range.");
          state.setValuesCalls++;
          for (let r = 0; r < nr; r++) for (let c = 0; c < nc; c++) {
            while (s.data.length < row + r) { s.data.push([]); s.formats.push([]); }
            const fmt = (s.formats[row - 1 + r] || [])[col - 1 + c] || "General"; s.data[row - 1 + r][col - 1 + c] = autoParse(values[r][c], fmt);
          }
          if (state.afterSetValues) state.afterSetValues(s, row, col, nr, nc);
        },
        setNumberFormat: (fmt) => { for (let r = 0; r < nr; r++) for (let c = 0; c < nc; c++) { while (s.formats.length < row + r) { s.formats.push([]); s.data.push(s.data[row - 1 + r] || []); } s.formats[row - 1 + r][col - 1 + c] = fmt; } },
      };
    },
  });
  const spreadsheet = () => ({ getSheetByName: (n) => (state.sheets[n] ? makeSheet(state.sheets[n]) : null), getSpreadsheetTimeZone: () => state.timeZone });

  const sandbox = {
    SpreadsheetApp: { openById: (id) => { if (id !== state.knownSpreadsheetId) throw new Error("Unexpected error while getting the method or property openById on object SpreadsheetApp."); return spreadsheet(); }, flush: () => {} },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => (Object.prototype.hasOwnProperty.call(state.properties, k) ? state.properties[k] : null) }) },
    LockService: { getScriptLock: () => ({
      waitLock: () => { if (state.lock.busy || state.lock.held) throw new Error("Lock timeout: another process was holding the lock for too long."); state.lock.waits++; state.lock.held = true; },
      hasLock: () => state.lock.held, releaseLock: () => { state.lock.held = false; state.lock.releases++; } }) },
    CacheService: { getScriptCache: () => ({
      get: (k) => { if (state.cacheFail.get) throw new Error(state.cacheFail.get); return state.cache.has(k) ? state.cache.get(k) : null; },
      put: (k, v, ttl) => { state.cachePuts++; state.cacheTtl.push(ttl); if (state.cacheFail.put) throw new Error(state.cacheFail.put); state.cache.set(k, v); } }) },       // cacheFail: the cache service raises an error on a read or a write
    MailApp: { sendEmail: (message) => {
      state.mail.attempts.push(Boolean(message.htmlBody));                                                  // every call, delivered or not: true = an HTML message
      if (state.mail.failWith) throw new Error(state.mail.failWith);
      if (state.mail.failHtmlWith && message.htmlBody) throw new Error(state.mail.failHtmlWith);          // a mail service that refuses the HTML message but would take the plain one
      const copy = JSON.parse(JSON.stringify(Object.assign({}, message, { inlineImages: undefined })));
      if (message.inlineImages) copy.inlineImages = Object.fromEntries(Object.entries(message.inlineImages).map(([key, blob]) => {       // only real Blobs are accepted, as in Apps Script
        const bytes = Buffer.from(blob.getBytes().map((b) => b & 255)); return [key, { contentType: blob.getContentType(), name: blob.getName(), size: bytes.length, head: bytes.subarray(0, 8).toString("hex"), sha256: crypto.createHash("sha256").update(bytes).digest("hex") }];
      }));
      state.mail.sent.push(copy);
      if (state.mail.failHtmlAfterSend && message.htmlBody) throw new Error(state.mail.failHtmlAfterSend);   // the HTML message WAS delivered, but the call reports an error (a lost answer): the outcome is ambiguous
    } },
    ContentService: { MimeType: { JSON: "application/json" }, createTextOutput: (text) => ({ text, mime: null, setMimeType(m) { this.mime = m; return this; }, getContent() { return this.text; } }) },
    Utilities: {
      DigestAlgorithm: { SHA_256: "SHA_256" },
      computeDigest: (alg, text) => toSigned(crypto.createHash("sha256").update(String(text), "utf8").digest()),
      getUuid: () => crypto.randomUUID(),
      base64Decode: (text) => toSigned(Buffer.from(String(text), "base64")),
      newBlob: (bytes, contentType, name) => { if (state.failBlob) throw new Error("Exception: Invalid argument: blob"); return { getBytes: () => bytes, getContentType: () => contentType, getName: () => name }; },
      formatDate: (date, tz, pattern) => { const offset = tz === "Asia/Dubai" ? 4 : 0; const d = new Date(date.getTime() + offset * 3600 * 1000); const p = (n) => String(n).padStart(2, "0");
        const map = { yyyy: d.getUTCFullYear(), MM: p(d.getUTCMonth() + 1), dd: p(d.getUTCDate()), HH: p(d.getUTCHours()), mm: p(d.getUTCMinutes()), ss: p(d.getUTCSeconds()) };
        return pattern.replace(/yyyy|MM|dd|HH|mm|ss/g, (k) => map[k]); },
    },
    Logger: { log: (...a) => state.logs.push(a.join(" ")) },
    console: { log: (...a) => state.logs.push(a.join(" ")), error: (...a) => state.logs.push(a.join(" ")), warn: (...a) => state.logs.push(a.join(" ")) },
    Date: class extends Date { constructor(...args) { if (args.length === 0) super(state.now.getTime()); else super(...args); } static now() { return state.now.getTime(); } },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(options.source !== undefined ? options.source : readSource(), context, { filename: "Code.gs" });

  const call = (payload, event) => {
    const contents = typeof payload === "string" ? payload : JSON.stringify(payload);
    const output = context.doPost(event || { postData: { contents } });
    return JSON.parse(output.getContent());
  };
  const rawCall = (payload) => context.doPost({ postData: { contents: typeof payload === "string" ? payload : JSON.stringify(payload) } });
  const dataRows = () => sheet.data.slice(1).map((r) => r.slice());
  return { state, context, sheet, call, rawCall, dataRows };
}
