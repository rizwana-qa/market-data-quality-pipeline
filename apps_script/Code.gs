/**
 * Daily Market Analysis: Google Apps Script web app.
 *
 * Receives the validated daily BTC/ETH analysis from the GitHub Actions workflow, writes it to one Google Sheet, verifies the write by reading it back,
 * and sends the success or failure e-mail. Deploy as a web app: execute as "Me", access "Anyone". Apps Script cannot read HTTP headers, so the shared
 * secret travels in the JSON body of a POST; every action checks it first.
 *
 * Actions (field "action"):
 *   upsert_analysis     insert or update the rows for (Run date, Asset), then confirm them by reading them back
 *   verify_analysis     read-only: do the expected rows exist exactly once with the expected values?
 *   send_success_email  re-verifies the sheet itself and only then sends the success e-mail (once per workflow run)
 *   send_success_report the same, with an HTML report and three inline PNG charts instead of the plain text (shares the once-per-run protection with send_success_email)
 *   notify_failure      failure e-mail for any failed pipeline stage; needs no sheet access
 *   self_check          reports (true/false only) whether the configuration and the sheet headings are in order
 *
 * Script Properties (Project Settings): SPREADSHEET_ID, SHEET_NAME, SHARED_SECRET, EMAIL_TO, DATE_HEADER, ASSET_HEADER. Nothing secret is stored in this file.
 *
 * Every reply is JSON: success, error (a code), message (fixed text), sheet_status and email_status (reported separately), request_id.
 * The replies never contain property values, stack traces or request content.
 */

const SHEET_HEADERS = ['Run date', 'Asset', 'Closing price', 'Daily return', '30-day volatility', '30-day drawdown', 'Correlation', 'Validation status', 'Report status', 'Workflow run ID'];
const NUMBER_COLUMNS = [2, 3, 4, 5, 6];            // zero-based positions of the numeric columns
const ASSETS = ['BTC', 'ETH'];
const VALIDATION_STATUSES = ['PASSED', 'FAILED'];
const REPORT_STATUSES = ['GENERATED', 'FAILED'];
const SHEET_STATUSES = ['not_attempted', 'ok', 'failed', 'unconfirmed'];
const STAGES = ['checkout', 'setup_python', 'install_dependencies', 'tests', 'analysis', 'validate_outputs', 'build_payload', 'post_to_sheet', 'verify_sheet', 'send_success_email', 'upload_artifacts', 'unknown'];
const CONFIG_PROPERTIES = ['SPREADSHEET_ID', 'SHEET_NAME', 'SHARED_SECRET', 'EMAIL_TO', 'DATE_HEADER', 'ASSET_HEADER'];
const SHEET_ACTIONS = ['upsert_analysis', 'verify_analysis', 'send_success_email', 'send_success_report'];
const WORKFLOW_URL_PATTERN = /^https:\/\/github\.com\/rizwana-qa\/market-data-quality-pipeline\/actions\/runs\/(\d{1,20})$/;
const EMAIL_PATTERN = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const RUN_ID_PATTERN = /^\d{1,20}$/;
const MIN_RUN_DATE = '2020-01-01';
const MAX_BODY_CHARS = 20000;
const MAX_ROWS = 10;
const MAX_OBSERVATIONS = 8;
const MAX_OBSERVATION_CHARS = 500;
const MAX_FAILURE_SUMMARY_CHARS = 500;
const MAX_SHEET_NAME_CHARS = 100;
const MIN_SECRET_LENGTH = 16;
const LOCK_WAIT_MS = 20000;
const DEDUPE_SECONDS = 21600;                       // the longest time CacheService keeps an entry (6 hours)
const READ_BACK_TOLERANCE = 1e-12;

// success report (send_success_report): the HTML e-mail with three small PNG charts that the web app builds itself from validated fields
const ARTIFACT_URL_PATTERN = /^https:\/\/github\.com\/rizwana-qa\/market-data-quality-pipeline\/actions\/runs\/(\d{1,20})\/artifacts\/\d{1,20}$/;
const UTC_TIME_PATTERN = /^(\d{4}-\d{2}-\d{2}) ([01]\d|2[0-3]):[0-5]\d:[0-5]\d UTC$/;
const BASE64_PATTERN = /^[A-Za-z0-9+\/]+={0,2}$/;
const LATEST_PATTERN = /^[\x20-\x7e]{1,30}$/;
const CHART_IDS = ['indexed', 'returns', 'drawdown'];
const CHART_WIDTH = 600;
const CHART_HEIGHT = 240;
const MAX_CHART_BYTES = 6000;                       // one PNG; the real charts are about 1 to 3 KB
const MAX_CHART_BASE64_CHARS = 8000;
const MAX_HTML_CHARS = 100000;                      // Gmail clips a message from about 102 KB; the real page is about 36 KB
const MIN_REPORT_ROWS = 8;
const MAX_REPORT_ROWS = 400;
const WINDOW_DAYS = 30;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];
const COVERAGE_NAMES = ['date', 'btc_close', 'eth_close', 'btc_return', 'eth_return', 'btc_open', 'btc_high', 'btc_low', 'btc_volume', 'btc_vwap', 'btc_trade_count', 'eth_open', 'eth_high', 'eth_low', 'eth_volume', 'eth_vwap', 'eth_trade_count'];

const MESSAGES = {
  UNAUTHORIZED: 'The request is not authorized.',
  NOT_CONFIGURED: 'The service is not configured.',
  INVALID_REQUEST: 'The request body is not valid.',
  INVALID_PAYLOAD: 'The request content failed validation.',
  HEADER_MISMATCH: 'The sheet headings do not match the expected headings.',
  SHEET_NOT_FOUND: 'The spreadsheet or worksheet could not be opened.',
  DUPLICATE_EXISTING_ROWS: 'The sheet already holds duplicate rows for this key.',
  LOCK_TIMEOUT: 'Another run holds the lock, try again later.',
  WRITE_UNCONFIRMED: 'The written values could not be confirmed by reading them back.',
  NOT_VERIFIED: 'The sheet does not hold the expected rows.',
  EMAIL_FAILED: 'The email could not be sent.',
  SEND_OUTCOME_UNKNOWN: 'It is not known whether the email was sent.',
  SENT_DEDUP_FAILED: 'The email was sent, but the duplicate-protection entry could not be recorded.',
  INTERNAL_ERROR: 'An internal error occurred.',
  POST_REQUIRED: 'This endpoint accepts POST requests only.',
};

class ApiError extends Error {
  constructor(code, sheetStatus, emailStatus, extra) {
    super(code);
    this.code = code;
    this.sheetStatus = sheetStatus || 'not_attempted';
    this.emailStatus = emailStatus || 'not_attempted';
    this.extra = extra || null;
  }
}

// ------------------------------------------------------------------ entry points
function doGet() {
  return reply_({ success: false, error: 'POST_REQUIRED', message: MESSAGES.POST_REQUIRED, request_id: Utilities.getUuid() });
}

function doPost(e) {
  const requestId = Utilities.getUuid();
  let action = null;
  let secret = '';
  try {
    const request = parseRequest_(e);
    secret = readSecret_();
    if (!tokensMatch_(request.token, secret)) throw new ApiError('UNAUTHORIZED');
    action = readAction_(request.action);
    const result = HANDLERS[action](request, secret);
    return reply_(Object.assign({ success: true, action: action, sheet_status: 'not_attempted', email_status: 'not_attempted', request_id: requestId }, result));
  } catch (err) {
    let apiError = err;
    if (!(err instanceof ApiError)) {
      console.error('request ' + requestId + ' failed unexpectedly: ' + describeError_(err, secret));
      apiError = new ApiError('INTERNAL_ERROR', SHEET_ACTIONS.indexOf(action) >= 0 ? 'failed' : 'not_attempted');
    } else {
      console.error('request ' + requestId + ' refused: ' + apiError.code);
    }
    const body = { success: false, error: apiError.code, message: MESSAGES[apiError.code], sheet_status: apiError.sheetStatus, email_status: apiError.emailStatus, request_id: requestId };
    if (action) body.action = action;
    return reply_(Object.assign(body, apiError.extra || {}));
  }
}

function reply_(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}

// ------------------------------------------------------------------ request, authentication, configuration
function parseRequest_(e) {
  const contents = e && e.postData ? e.postData.contents : undefined;
  if (typeof contents !== 'string' || contents.length === 0 || contents.length > MAX_BODY_CHARS) throw new ApiError('INVALID_REQUEST');
  let parsed;
  try {
    parsed = JSON.parse(contents);
  } catch (ignored) {
    throw new ApiError('INVALID_REQUEST');
  }
  if (!isObject_(parsed)) throw new ApiError('INVALID_REQUEST');
  return parsed;
}

function readAction_(action) {
  if (typeof action !== 'string' || !Object.prototype.hasOwnProperty.call(HANDLERS, action)) throw new ApiError('INVALID_REQUEST');
  return action;
}

function readProperty_(name) {
  const value = PropertiesService.getScriptProperties().getProperty(name);
  return value === null || value === undefined ? '' : String(value).trim();
}

function readSecret_() {
  const secret = readProperty_('SHARED_SECRET');
  if (secret.length < MIN_SECRET_LENGTH) throw new ApiError('NOT_CONFIGURED');
  return secret;
}

function tokensMatch_(supplied, expected) {
  if (typeof supplied !== 'string') return false;
  const a = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, supplied);
  const b = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, expected);
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];     // compares the digests in constant time
  return difference === 0;
}

function readSheetConfig_() {
  const config = { spreadsheetId: readProperty_('SPREADSHEET_ID'), sheetName: readProperty_('SHEET_NAME'), dateHeader: readProperty_('DATE_HEADER'), assetHeader: readProperty_('ASSET_HEADER') };
  if (!config.spreadsheetId || !config.sheetName || !config.dateHeader || !config.assetHeader) throw new ApiError('NOT_CONFIGURED');
  return config;
}

function describeError_(err, secret) {
  const text = String(err && err.name ? err.name : 'Error') + ': ' + String(err && err.message ? err.message : '').slice(0, 200);
  return redact_(text, secret);
}

function redact_(text, secret) {
  return secret ? String(text).split(secret).join('[redacted]') : String(text);
}

// ------------------------------------------------------------------ validation (everything is checked before anything is written or sent)
function isObject_(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function invalid_() {
  return new ApiError('INVALID_PAYLOAD');
}

function requireExactKeys_(object, keys) {
  if (!isObject_(object)) throw invalid_();
  const actual = Object.keys(object);
  if (actual.length !== keys.length) throw invalid_();
  keys.forEach(function (key) { if (!Object.prototype.hasOwnProperty.call(object, key)) throw invalid_(); });
}

function isNumberIn_(value, low, high, lowOpen) {
  return typeof value === 'number' && isFinite(value) && (lowOpen ? value > low : value >= low) && value <= high;
}

function isoDate_(date) {
  const pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return date.getUTCFullYear() + '-' + pad(date.getUTCMonth() + 1) + '-' + pad(date.getUTCDate());
}

function validateAnalysisDate_(value) {
  if (typeof value !== 'string' || !DATE_PATTERN.test(value)) throw invalid_();
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const check = new Date(Date.UTC(year, month - 1, day));
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) throw invalid_();
  const now = new Date();
  const lastCompletedDay = isoDate_(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 1)));
  if (value < MIN_RUN_DATE || value > lastCompletedDay) throw invalid_();
  return value;
}

function validateRows_(rows, analysisDate) {
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > MAX_ROWS) throw invalid_();
  const seen = {};
  return rows.map(function (row) {
    requireExactKeys_(row, SHEET_HEADERS);
    if (row['Run date'] !== analysisDate) throw invalid_();
    if (ASSETS.indexOf(row['Asset']) < 0) throw invalid_();
    if (!isNumberIn_(row['Closing price'], 0, 1e9, true)) throw invalid_();
    if (!isNumberIn_(row['Daily return'], -1, 5, true)) throw invalid_();
    if (!isNumberIn_(row['30-day volatility'], 0, 5, false)) throw invalid_();
    if (!isNumberIn_(row['30-day drawdown'], -1, 0, false)) throw invalid_();
    if (!isNumberIn_(row['Correlation'], -1, 1, false)) throw invalid_();
    if (VALIDATION_STATUSES.indexOf(row['Validation status']) < 0) throw invalid_();
    if (REPORT_STATUSES.indexOf(row['Report status']) < 0) throw invalid_();
    if (typeof row['Workflow run ID'] !== 'string' || !RUN_ID_PATTERN.test(row['Workflow run ID'])) throw invalid_();
    const key = row['Run date'] + '|' + row['Asset'];
    if (Object.prototype.hasOwnProperty.call(seen, key)) throw invalid_();
    seen[key] = true;
    const values = SHEET_HEADERS.map(function (heading) { return row[heading]; });
    NUMBER_COLUMNS.forEach(function (position) { if (values[position] === 0) values[position] = 0; });   // turns -0 into 0
    return { key: key, values: values };
  });
}

function validateRunId_(value) {
  if (typeof value !== 'string' || !RUN_ID_PATTERN.test(value)) throw invalid_();
  return value;
}

function validateWorkflowUrl_(value, runId) {
  const match = typeof value === 'string' ? WORKFLOW_URL_PATTERN.exec(value) : null;
  if (!match || match[1] !== runId) throw invalid_();
  return value;
}

function validateEnum_(value, allowed) {
  if (typeof value !== 'string' || allowed.indexOf(value) < 0) throw invalid_();
  return value;
}

function validateSheetUpdate_(update) {
  requireExactKeys_(update, ['rows_inserted', 'rows_updated']);
  ['rows_inserted', 'rows_updated'].forEach(function (name) {
    if (typeof update[name] !== 'number' || Math.floor(update[name]) !== update[name] || update[name] < 0 || update[name] > MAX_ROWS) throw invalid_();
  });
  return update;
}

function validateSummary_(summary) {
  requireExactKeys_(summary, ['btc', 'eth', 'correlation', 'rules', 'observations']);
  ['btc', 'eth'].forEach(function (asset) {
    requireExactKeys_(summary[asset], ['close', 'daily_return', 'volatility', 'drawdown']);
    Object.keys(summary[asset]).forEach(function (name) { if (!isNumberIn_(summary[asset][name], -1e9, 1e9, false)) throw invalid_(); });
  });
  if (!isNumberIn_(summary.correlation, -1, 1, false)) throw invalid_();
  requireExactKeys_(summary.rules, ['R1', 'R2', 'R3']);
  Object.keys(summary.rules).forEach(function (name) { if (typeof summary.rules[name] !== 'string' || summary.rules[name].length < 1 || summary.rules[name].length > 100) throw invalid_(); });
  if (!Array.isArray(summary.observations) || summary.observations.length > MAX_OBSERVATIONS) throw invalid_();
  summary.observations.forEach(function (text) { if (typeof text !== 'string' || text.length < 1 || text.length > MAX_OBSERVATION_CHARS) throw invalid_(); });
  return summary;
}

// ------------------------------------------------------------------ validation of the success report (the figures and the three chart images); nothing here is trusted, and no HTML is ever accepted
function isIntegerIn_(value, low, high) {
  return typeof value === 'number' && isFinite(value) && Math.floor(value) === value && value >= low && value <= high;
}

function isCalendarDate_(value) {
  if (typeof value !== 'string' || !DATE_PATTERN.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const check = new Date(Date.UTC(year, month - 1, day));
  return check.getUTCFullYear() === year && check.getUTCMonth() === month - 1 && check.getUTCDate() === day;
}

function validateDate_(value, nullable) {
  if (value === null && nullable) return value;
  if (!isCalendarDate_(value)) throw invalid_();
  return value;
}

function validateUtcTime_(value) {
  const match = typeof value === 'string' ? UTC_TIME_PATTERN.exec(value) : null;
  if (!match || !isCalendarDate_(match[1])) throw invalid_();
  return value;
}

function validateArtifactUrl_(value, runId) {
  const match = typeof value === 'string' ? ARTIFACT_URL_PATTERN.exec(value) : null;
  if (!match || match[1] !== runId) throw invalid_();       // this repository, this run, https, nothing after the artifact id
  return value;
}

function validateAssetFigures_(figures) {
  requireExactKeys_(figures, ['return_7d', 'return_14d', 'return_30d', 'volatility_annualised', 'worst_day', 'negative_days', 'drawdown_peak', 'drawdown_trough']);
  ['return_7d', 'return_14d', 'return_30d'].forEach(function (name) { if (!isNumberIn_(figures[name], -1, 1000, true)) throw invalid_(); });
  if (!isNumberIn_(figures.volatility_annualised, 0, 1000, false)) throw invalid_();
  requireExactKeys_(figures.worst_day, ['value', 'date']);
  if (!isNumberIn_(figures.worst_day.value, -1, 1000, true)) throw invalid_();
  validateDate_(figures.worst_day.date);
  if (!isIntegerIn_(figures.negative_days, 0, 366)) throw invalid_();
  validateDate_(figures.drawdown_peak, true);
  validateDate_(figures.drawdown_trough, true);
}

function validateCoverage_(coverage, rows) {
  if (!Array.isArray(coverage) || coverage.length !== COVERAGE_NAMES.length) throw invalid_();
  coverage.forEach(function (entry, position) {
    requireExactKeys_(entry, ['name', 'valid_rows', 'latest']);
    if (entry.name !== COVERAGE_NAMES[position] || !isIntegerIn_(entry.valid_rows, 0, rows)) throw invalid_();
    if (typeof entry.latest !== 'string' || !LATEST_PATTERN.test(entry.latest)) throw invalid_();
  });
}

function crcTable_() {
  const table = [];
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    table.push(c >>> 0);
  }
  return table;
}

function crc32_(bytes, start, end, table) {
  let c = 0xffffffff;
  for (let i = start; i < end; i++) c = table[(c ^ bytes[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function uint32_(bytes, at) {
  return (bytes[at] * 16777216) + ((bytes[at + 1] << 16) | (bytes[at + 2] << 8) | bytes[at + 3]);
}

// A chart must really be the small palette PNG the client draws: signature, every chunk's length and CRC, a 600 x 240 palette header, a palette and image data, and the end chunk last.
function decodeChartPng_(encoded) {
  if (typeof encoded !== 'string' || encoded.length < 100 || encoded.length > MAX_CHART_BASE64_CHARS || encoded.length % 4 !== 0 || !BASE64_PATTERN.test(encoded)) throw invalid_();
  let bytes;
  try {
    bytes = Utilities.base64Decode(encoded);
  } catch (ignored) {
    throw invalid_();
  }
  const unsigned = bytes.map(function (b) { return b & 255; });
  if (unsigned.length < 57 || unsigned.length > MAX_CHART_BYTES) throw invalid_();
  PNG_SIGNATURE.forEach(function (value, i) { if (unsigned[i] !== value) throw invalid_(); });
  const table = crcTable_();
  const types = [];
  let position = 8;
  while (position < unsigned.length) {
    if (position + 12 > unsigned.length) throw invalid_();
    const length = uint32_(unsigned, position);
    const end = position + 12 + length;
    if (end > unsigned.length) throw invalid_();
    const type = String.fromCharCode(unsigned[position + 4], unsigned[position + 5], unsigned[position + 6], unsigned[position + 7]);
    if (!/^[A-Za-z]{4}$/.test(type) || crc32_(unsigned, position + 4, position + 8 + length, table) !== uint32_(unsigned, end - 4)) throw invalid_();
    if (types.length === 0) {
      const depth = unsigned[position + 16];
      if (type !== 'IHDR' || length !== 13 || uint32_(unsigned, position + 8) !== CHART_WIDTH || uint32_(unsigned, position + 12) !== CHART_HEIGHT || [1, 2, 4, 8].indexOf(depth) < 0 || unsigned[position + 17] !== 3) throw invalid_();
    }
    types.push(type);
    if (type === 'IEND' && (length !== 0 || end !== unsigned.length)) throw invalid_();
    position = end;
  }
  if (types[types.length - 1] !== 'IEND' || types.indexOf('PLTE') < 0 || types.indexOf('IDAT') < 0) throw invalid_();
  return bytes;
}

function validateCharts_(charts) {
  if (!Array.isArray(charts) || charts.length !== CHART_IDS.length) throw invalid_();
  charts.forEach(function (chart, position) {
    requireExactKeys_(chart, ['id', 'png_base64', 'width', 'height', 'low', 'high', 'btc_last', 'eth_last']);
    if (chart.id !== CHART_IDS[position] || chart.width !== CHART_WIDTH || chart.height !== CHART_HEIGHT) throw invalid_();
    ['low', 'high', 'btc_last', 'eth_last'].forEach(function (name) { if (!isNumberIn_(chart[name], -1e6, 1e6, false)) throw invalid_(); });
    if (!(chart.low < chart.high)) throw invalid_();
    decodeChartPng_(chart.png_base64);
  });
}

function validateReport_(report) {
  requireExactKeys_(report, ['meta', 'assets', 'pair', 'coverage', 'charts']);
  requireExactKeys_(report.meta, ['rows', 'first_date', 'data_fetched', 'generated_at']);
  if (!isIntegerIn_(report.meta.rows, MIN_REPORT_ROWS, MAX_REPORT_ROWS)) throw invalid_();
  validateDate_(report.meta.first_date);
  validateUtcTime_(report.meta.data_fetched);
  validateUtcTime_(report.meta.generated_at);
  requireExactKeys_(report.assets, ['btc', 'eth']);
  validateAssetFigures_(report.assets.btc);
  validateAssetFigures_(report.assets.eth);
  requireExactKeys_(report.pair, ['volatility_ratio', 'joint_negative_days', 'overlap_share']);
  if (!(report.pair.volatility_ratio === null || isNumberIn_(report.pair.volatility_ratio, 0, 1000, false))) throw invalid_();
  if (!isIntegerIn_(report.pair.joint_negative_days, 0, 366)) throw invalid_();
  if (!(report.pair.overlap_share === null || isNumberIn_(report.pair.overlap_share, 0, 1, false))) throw invalid_();
  validateCoverage_(report.coverage, report.meta.rows);
  validateCharts_(report.charts);
  return report;
}

// ------------------------------------------------------------------ the sheet
function withLock_(work) {
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(LOCK_WAIT_MS);
  } catch (ignored) {
    throw new ApiError('LOCK_TIMEOUT');
  }
  try {
    return work();
  } finally {
    lock.releaseLock();
  }
}

function openSheet_(config) {
  let spreadsheet;
  try {
    spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
  } catch (ignored) {
    throw new ApiError('SHEET_NOT_FOUND', 'failed');
  }
  const sheet = spreadsheet.getSheetByName(config.sheetName);
  if (!sheet) throw new ApiError('SHEET_NOT_FOUND', 'failed');
  checkHeadings_(sheet, config);
  return { sheet: sheet, timeZone: spreadsheet.getSpreadsheetTimeZone() };
}

function checkHeadings_(sheet, config) {
  const mismatch = new ApiError('HEADER_MISMATCH', 'failed');
  if (config.dateHeader !== SHEET_HEADERS[0] || config.assetHeader !== SHEET_HEADERS[1]) throw mismatch;
  const width = Math.max(sheet.getLastColumn(), SHEET_HEADERS.length);
  let found;
  try {
    found = sheet.getRange(1, 1, 1, width).getValues()[0];
  } catch (ignored) {
    throw mismatch;
  }
  for (let i = 0; i < width; i++) {
    const expected = i < SHEET_HEADERS.length ? SHEET_HEADERS[i] : '';
    if (found[i] !== expected) throw mismatch;
  }
}

function dateText_(cell, timeZone) {
  if (Object.prototype.toString.call(cell) === '[object Date]') return Utilities.formatDate(cell, timeZone, 'yyyy-MM-dd');
  return String(cell).trim();
}

function readTable_(sheet, timeZone) {
  const lastRow = sheet.getLastRow();
  const table = { values: [], index: {} };
  if (lastRow < 2) return table;
  table.values = sheet.getRange(2, 1, lastRow - 1, SHEET_HEADERS.length).getValues();
  table.values.forEach(function (row, position) {
    const key = dateText_(row[0], timeZone) + '|' + String(row[1]).trim();
    if (!Object.prototype.hasOwnProperty.call(table.index, key)) table.index[key] = [];
    table.index[key].push(position + 2);
  });
  return table;
}

function storedMatches_(stored, expected, timeZone) {
  if (dateText_(stored[0], timeZone) !== expected[0]) return false;
  for (let c = 1; c < SHEET_HEADERS.length; c++) {
    if (NUMBER_COLUMNS.indexOf(c) >= 0) {
      if (typeof stored[c] !== 'number' || Math.abs(stored[c] - expected[c]) > READ_BACK_TOLERANCE * Math.max(1, Math.abs(expected[c]))) return false;
    } else if (String(stored[c]) !== expected[c]) {
      return false;
    }
  }
  return true;
}

function checkRows_(sheet, timeZone, rows) {
  const table = readTable_(sheet, timeZone);
  let found = 0;
  let duplicates = 0;
  let matches = true;
  rows.forEach(function (row) {
    const where = Object.prototype.hasOwnProperty.call(table.index, row.key) ? table.index[row.key] : [];
    if (where.length === 0) { matches = false; return; }
    found++;
    if (where.length > 1) duplicates++;
    if (!storedMatches_(table.values[where[0] - 2], row.values, timeZone)) matches = false;
  });
  matches = matches && found === rows.length;
  return { found: found, matches: matches, duplicates: duplicates, ok: matches && duplicates === 0 };
}

function writeRow_(sheet, rowNumber, values) {
  sheet.getRange(rowNumber, 1, 1, 2).setNumberFormat('@');      // the date, the asset, the statuses and the run id stay text: the sheet must not turn them into dates or numbers
  sheet.getRange(rowNumber, 8, 1, 3).setNumberFormat('@');
  sheet.getRange(rowNumber, 1, 1, SHEET_HEADERS.length).setValues([values]);
}

function upsertRows_(config, rows) {
  return withLock_(function () {
    const opened = openSheet_(config);
    const sheet = opened.sheet;
    const table = readTable_(sheet, opened.timeZone);
    const existing = rows.map(function (row) { return Object.prototype.hasOwnProperty.call(table.index, row.key) ? table.index[row.key] : []; });
    if (existing.some(function (where) { return where.length > 1; })) throw new ApiError('DUPLICATE_EXISTING_ROWS', 'failed');
    let nextRow = sheet.getLastRow() + 1;
    const targets = existing.map(function (where) { return where.length === 1 ? where[0] : nextRow++; });
    const lastNeeded = Math.max.apply(null, targets);
    if (lastNeeded > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), lastNeeded - sheet.getMaxRows());
    let inserted = 0;
    let updated = 0;
    rows.forEach(function (row, i) {
      writeRow_(sheet, targets[i], row.values);
      if (existing[i].length === 1) updated++; else inserted++;
    });
    SpreadsheetApp.flush();
    if (!checkRows_(sheet, opened.timeZone, rows).ok) {
      throw new ApiError('WRITE_UNCONFIRMED', 'unconfirmed', 'not_attempted', { write_confirmed: false, rows_inserted: inserted, rows_updated: updated });
    }
    return { sheet_status: 'ok', rows_inserted: inserted, rows_updated: updated, write_confirmed: true };
  });
}

function verifiedRows_(config, rows) {
  const opened = openSheet_(config);
  const result = checkRows_(opened.sheet, opened.timeZone, rows);
  if (!result.ok) throw new ApiError('NOT_VERIFIED', 'failed', 'not_attempted', { rows_found: result.found, matches: result.matches, duplicate_keys: result.duplicates });
  return result;
}

// ------------------------------------------------------------------ actions
function handleUpsert_(request) {
  requireExactKeys_(request, ['action', 'token', 'analysis_date', 'rows']);
  const rows = validateRows_(request.rows, validateAnalysisDate_(request.analysis_date));
  return upsertRows_(readSheetConfig_(), rows);
}

function handleVerify_(request) {
  requireExactKeys_(request, ['action', 'token', 'analysis_date', 'rows']);
  const rows = validateRows_(request.rows, validateAnalysisDate_(request.analysis_date));
  const config = readSheetConfig_();
  const result = withLock_(function () { return verifiedRows_(config, rows); });
  return { sheet_status: 'ok', rows_found: result.found, matches: true, duplicate_keys: 0 };
}

function handleSuccessEmail_(request, secret) {
  requireExactKeys_(request, ['action', 'token', 'analysis_date', 'rows', 'run_id', 'workflow_url', 'validation_status', 'sheet_update', 'summary']);
  const date = validateAnalysisDate_(request.analysis_date);
  const rows = validateRows_(request.rows, date);
  const runId = validateRunId_(request.run_id);
  const url = validateWorkflowUrl_(request.workflow_url, runId);
  const validation = validateEnum_(request.validation_status, VALIDATION_STATUSES);
  const update = validateSheetUpdate_(request.sheet_update);
  const summary = validateSummary_(request.summary);
  const config = readSheetConfig_();
  return withLock_(function () {
    verifiedRows_(config, rows);                                   // no success e-mail unless the sheet really holds the rows
    const recipient = readRecipient_('ok');
    const cache = CacheService.getScriptCache();
    const dedupeKey = 'ok:' + runId + ':' + date;
    if (cache.get(dedupeKey)) return { sheet_status: 'ok', email_status: 'skipped_duplicate' };
    const message = { to: recipient, subject: 'Daily Market Analysis OK - ' + date, body: successBody_(date, validation, update, summary, runId, url, secret), name: 'Daily Market Analysis' };
    sendMail_(message, 'ok', secret);
    return recordSent_(cache, dedupeKey, { sheet_status: 'ok', email_status: 'sent' });
  });
}

// send_success_report: the same guarantees as send_success_email (the sheet is re-verified first; one success e-mail per run and date, shared with send_success_email), but the message is an HTML report
// with three inline PNG charts. The HTML is built here from validated fields only. If the HTML message cannot be PREPARED (or fails its own check) before it is handed to the mail service, the plain-text
// success e-mail goes out instead and the reply says email_format "text_fallback"; if that fails too, the reply is EMAIL_FAILED and nothing is remembered. If the HTML send itself raises an error, the
// outcome is unknown: nothing more is sent and the reply is SEND_OUTCOME_UNKNOWN (see sendReportMail_).
function handleSuccessReport_(request, secret) {
  requireExactKeys_(request, ['action', 'token', 'analysis_date', 'rows', 'run_id', 'workflow_url', 'validation_status', 'sheet_update', 'summary', 'artifact_url', 'report']);
  const date = validateAnalysisDate_(request.analysis_date);
  const rows = validateRows_(request.rows, date);
  const runId = validateRunId_(request.run_id);
  const url = validateWorkflowUrl_(request.workflow_url, runId);
  const validation = validateEnum_(request.validation_status, VALIDATION_STATUSES);
  const update = validateSheetUpdate_(request.sheet_update);
  const summary = validateSummary_(request.summary);
  const artifactUrl = validateArtifactUrl_(request.artifact_url, runId);
  const report = validateReport_(request.report);
  const config = readSheetConfig_();
  return withLock_(function () {
    verifiedRows_(config, rows);                                   // no success e-mail unless the sheet really holds the rows
    const recipient = readRecipient_('ok');
    const cache = CacheService.getScriptCache();
    const dedupeKey = 'ok:' + runId + ':' + date;
    if (cache.get(dedupeKey)) return { sheet_status: 'ok', email_status: 'skipped_duplicate' };
    const subject = 'Daily Market Analysis OK - ' + date;
    const text = successBody_(date, validation, update, summary, runId, url, secret) + '\n\nDeliverables (HTML dashboard, report and CSV; GitHub sign-in required): ' + artifactUrl;
    const format = sendReportMail_(recipient, subject, text, { date: date, validation: validation, reportStatus: rows[0].values[8], update: update, summary: summary, runId: runId, url: url, artifactUrl: artifactUrl, report: report }, secret);
    return recordSent_(cache, dedupeKey, { sheet_status: 'ok', email_status: 'sent', email_format: format });
  });
}

// Records that the success e-mail of this run and date has gone out (the duplicate protection). The e-mail HAS been sent when this runs, so a failure here must never become an error reply:
// the client repeats INTERNAL_ERROR and the e-mail would go out again. It is reported as email_status sent_dedup_failed instead (success true, no error code), and logged without the
// provider's own text. Nothing is retried here either.
function recordSent_(cache, key, result) {
  try {
    cache.put(key, '1', DEDUPE_SECONDS);
    return result;
  } catch (err) {
    console.error('the duplicate-protection entry could not be written after the e-mail was sent (' + errorType_(err) + '); the e-mail is not sent again');
    return Object.assign({}, result, { email_status: 'sent_dedup_failed', message: MESSAGES.SENT_DEDUP_FAILED });
  }
}

function errorType_(err) {
  const name = String(err && err.name ? err.name : 'Error');
  return /^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(name) ? name : 'Error';
}

function handleFailure_(request, secret) {
  requireExactKeys_(request, ['action', 'token', 'stage', 'error_summary', 'sheet_status', 'run_id', 'workflow_url']);
  const stage = validateEnum_(request.stage, STAGES);
  if (typeof request.error_summary !== 'string') throw invalid_();
  const sheetStatus = validateEnum_(request.sheet_status, SHEET_STATUSES);
  const runId = validateRunId_(request.run_id);
  const url = validateWorkflowUrl_(request.workflow_url, runId);
  const summary = oneLine_(redact_(request.error_summary, secret)).slice(0, MAX_FAILURE_SUMMARY_CHARS);
  return withLock_(function () {
    const recipient = readRecipient_(sheetStatus);
    const cache = CacheService.getScriptCache();
    const dedupeKey = 'fail:' + runId + ':' + stage;
    if (cache.get(dedupeKey)) return { sheet_status: sheetStatus, email_status: 'skipped_duplicate' };
    const message = { to: recipient, subject: 'Daily Market Analysis FAILED at stage: ' + stage, body: failureBody_(stage, summary, sheetStatus, runId, url), name: 'Daily Market Analysis' };
    sendMail_(message, sheetStatus, secret);
    return recordSent_(cache, dedupeKey, { sheet_status: sheetStatus, email_status: 'sent' });
  });
}

// Read-only: it opens the spreadsheet and reads the heading row, and nothing else. It takes no lock, writes nothing, sends no e-mail and returns booleans only, never a value.
// The caller may add expected_sheet_name; the reply then says (true/false) whether the SHEET_NAME property equals it.
function handleSelfCheck_(request) {
  const hasExpectedName = Object.prototype.hasOwnProperty.call(request, 'expected_sheet_name');
  requireExactKeys_(request, hasExpectedName ? ['action', 'token', 'expected_sheet_name'] : ['action', 'token']);
  if (hasExpectedName && (typeof request.expected_sheet_name !== 'string' || request.expected_sheet_name.length < 1 || request.expected_sheet_name.length > MAX_SHEET_NAME_CHARS)) throw invalid_();
  const properties = {};
  CONFIG_PROPERTIES.forEach(function (name) { properties[name] = readProperty_(name) !== ''; });
  let spreadsheetFound = false;
  let sheetFound = false;
  let headersOk = false;
  if (properties.SPREADSHEET_ID) {
    let spreadsheet = null;
    try {
      spreadsheet = SpreadsheetApp.openById(readProperty_('SPREADSHEET_ID'));
      spreadsheetFound = true;
    } catch (ignored) {
      spreadsheet = null;
    }
    const sheet = spreadsheet && properties.SHEET_NAME ? spreadsheet.getSheetByName(readProperty_('SHEET_NAME')) : null;
    sheetFound = Boolean(sheet);
    if (sheet && properties.DATE_HEADER && properties.ASSET_HEADER) {
      try {
        checkHeadings_(sheet, readSheetConfig_());
        headersOk = true;
      } catch (ignored) {
        headersOk = false;
      }
    }
  }
  const emailValid = EMAIL_PATTERN.test(readProperty_('EMAIL_TO'));
  const sheetNameMatches = hasExpectedName ? properties.SHEET_NAME && readProperty_('SHEET_NAME') === request.expected_sheet_name : true;
  const ok = CONFIG_PROPERTIES.every(function (name) { return properties[name]; }) && spreadsheetFound && sheetFound && headersOk && emailValid && sheetNameMatches;
  const result = { success: ok, properties: properties, spreadsheet_found: spreadsheetFound, sheet_found: sheetFound, headers_ok: headersOk, email_valid: emailValid };
  if (hasExpectedName) result.sheet_name_matches = Boolean(sheetNameMatches);
  if (!ok) { result.error = 'NOT_CONFIGURED'; result.message = MESSAGES.NOT_CONFIGURED; }
  return result;
}

const HANDLERS = {
  upsert_analysis: handleUpsert_,
  verify_analysis: handleVerify_,
  send_success_email: handleSuccessEmail_,
  send_success_report: handleSuccessReport_,
  notify_failure: handleFailure_,
  self_check: handleSelfCheck_,
};

// ------------------------------------------------------------------ e-mail (plain text built only from validated fields)
function readRecipient_(sheetStatus) {
  const recipient = readProperty_('EMAIL_TO');
  if (!EMAIL_PATTERN.test(recipient)) throw new ApiError('NOT_CONFIGURED', sheetStatus, 'failed');
  return recipient;
}

function sendMail_(message, sheetStatus, secret) {
  try {
    MailApp.sendEmail(message);
  } catch (err) {
    console.error('mail failed: ' + describeError_(err, secret));
    throw new ApiError('EMAIL_FAILED', sheetStatus, 'failed');
  }
}

function oneLine_(text) {
  return String(text).replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
}

function percent_(fraction) {
  return (fraction * 100).toFixed(2) + '%';
}

function assetLine_(name, values) {
  return name + ': close ' + values.close.toFixed(2) + ' USD | daily return ' + percent_(values.daily_return) + ' | 30-day volatility ' + percent_(values.volatility) + ' (daily, not annualised) | 30-day max drawdown ' + percent_(values.drawdown);
}

function timesText_() {
  const now = new Date();
  return Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd HH:mm:ss') + ' UTC / ' + Utilities.formatDate(now, 'Asia/Dubai', 'yyyy-MM-dd HH:mm:ss') + ' Dubai (UTC+4)';
}

function successBody_(date, validation, update, summary, runId, url, secret) {
  const lines = [
    'Daily Market Analysis finished successfully.',
    '',
    'Analysis date (last completed UTC day): ' + date,
    'Data validation: ' + validation,
    'Spreadsheet: updated and verified by reading the rows back (' + update.rows_inserted + ' inserted, ' + update.rows_updated + ' updated)',
    '',
    assetLine_('BTC', summary.btc),
    assetLine_('ETH', summary.eth),
    'BTC/ETH correlation of daily returns (30 days): ' + summary.correlation.toFixed(6),
    '',
    'Observation rules (descriptive labels for this 30-day sample only, not advice):',
    '- R1 co-movement: ' + oneLine_(summary.rules.R1),
    '- R2 relative volatility: ' + oneLine_(summary.rules.R2),
    '- R3 down-day overlap: ' + oneLine_(summary.rules.R3),
  ];
  if (summary.observations.length > 0) {
    lines.push('', 'Observations:');
    summary.observations.forEach(function (text) { lines.push('- ' + oneLine_(redact_(text, secret))); });
  }
  lines.push('', 'Executed: ' + timesText_(), 'Workflow run ' + runId + ': ' + url, '', 'Educational use only. Not financial advice.');
  return lines.join('\n');
}

function failureBody_(stage, summary, sheetStatus, runId, url) {
  return [
    'Daily Market Analysis FAILED.',
    '',
    'Failed stage: ' + stage,
    'Error summary: ' + summary,
    'Spreadsheet status: ' + sheetStatus,
    '',
    'Executed: ' + timesText_(),
    'Workflow run ' + runId + ': ' + url,
  ].join('\n');
}

// ------------------------------------------------------------------ e-mail: the HTML success report (built only from validated fields, every piece of text escaped)
// Layout: nested tables with inline styles at 600 px (works in Gmail and, as far as can be told without Outlook, in the Word-based Outlook), no scripts, no remote images, no data: images, no external CSS.
// The only <style> block holds a mobile media query; clients that drop it still show the 600 px layout.
const FONT = 'Arial, Helvetica, sans-serif';
const INK = '#111827';
const MUTED = '#4b5563';
const LINE = '#e5e7eb';
const BTC_COLOUR = '#f7931a';
const ETH_COLOUR = '#627eea';
const CHART_TEXT = {
  indexed: { title: 'Price, indexed to 100 at the start of the 30-day window', kind: 'Line chart' },
  returns: { title: 'Daily returns, percent', kind: 'Bar chart' },
  drawdown: { title: 'Drawdown from the highest close so far, percent', kind: 'Line chart' },
};

function escapeHtml_(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function withCommas_(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function money_(value) {
  const parts = value.toFixed(2).split('.');
  return withCommas_(parts[0]) + '.' + parts[1];
}

function signedPercent_(fraction) {
  const text = (fraction * 100).toFixed(2);
  if (/^-?0\.00$/.test(text)) return '0.00%';
  return (text.charAt(0) === '-' ? '' : '+') + text + '%';
}

function plainNumber_(value) {
  return String(Number(value.toFixed(2)));
}

function tone_(fraction) {
  const text = (fraction * 100).toFixed(2);
  return /^-?0\.00$/.test(text) ? MUTED : text.charAt(0) === '-' ? '#b91c1c' : '#15803d';
}

function table_(width, style, inner) {
  return '<table role="presentation"' + (width ? ' width="' + width + '"' : '') + ' cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;' + style + '">' + inner + '</table>';
}

function text_(style, content) {
  return '<p style="margin:0;font-family:' + FONT + ';' + style + '">' + content + '</p>';
}

function section_(title, inner) {
  return '<tr><td class="pad" style="padding:20px 24px 4px 24px;border-top:1px solid ' + LINE + ';"><h2 style="margin:0 0 10px 0;font-family:' + FONT + ';font-size:16px;line-height:22px;color:' + INK + ';">' + escapeHtml_(title) + '</h2>' + inner + '</td></tr>';
}

// One label and one value per row; a note (the dates a figure belongs to) goes under the value in small print, so that narrow columns do not wrap the value.
function metricRow_(label, value, colour, note) {
  const small = 'font-family:' + FONT + ';font-size:11px;line-height:15px;font-weight:normal;color:' + MUTED + ';';
  return '<tr><td valign="top" style="padding:3px 0;font-family:' + FONT + ';font-size:13px;line-height:18px;color:' + MUTED + ';">' + escapeHtml_(label) + '</td><td align="right" valign="top" style="padding:3px 0 3px 8px;font-family:' + FONT + ';font-size:13px;line-height:18px;font-weight:bold;color:' + (colour || INK) + ';">' +
    escapeHtml_(value) + (note ? '<br><span style="' + small + '">' + escapeHtml_(note) + '</span>' : '') + '</td></tr>';
}

function drawdownNote_(peak, trough) {
  return peak && trough ? peak + ' to ' + trough : 'no fall from a previous high';
}

// The latest value of a chart series as it is shown next to the picture: an index level, or a percent.
function chartValue_(id, value) {
  return id === 'indexed' ? value.toFixed(2) : id === 'returns' ? signedPercent_(value / 100) : value.toFixed(2) + '%';
}

function scaleEnd_(id, value) {
  return id === 'indexed' ? plainNumber_(value) : plainNumber_(value) + '%';
}

function assetCard_(name, label, colour, values, figures) {
  const rows = [
    metricRow_('Daily return', signedPercent_(values.daily_return), tone_(values.daily_return)),
    metricRow_('7-day return', signedPercent_(figures.return_7d), tone_(figures.return_7d)),
    metricRow_('14-day return', signedPercent_(figures.return_14d), tone_(figures.return_14d)),
    metricRow_('30-day return', signedPercent_(figures.return_30d), tone_(figures.return_30d)),
    metricRow_('Volatility, daily', percent_(values.volatility)),
    metricRow_('Volatility, annualised', percent_(figures.volatility_annualised)),
    metricRow_('Maximum drawdown', percent_(values.drawdown), null, drawdownNote_(figures.drawdown_peak, figures.drawdown_trough)),
    metricRow_('Worst day', signedPercent_(figures.worst_day.value), null, figures.worst_day.date),
    metricRow_('Down days', figures.negative_days + ' of ' + WINDOW_DAYS),
  ].join('');
  return table_('100%', 'border:1px solid ' + LINE + ';border-top:4px solid ' + colour + ';',
    '<tr><td style="padding:12px 14px;">' + text_('font-size:13px;line-height:18px;color:' + MUTED + ';', escapeHtml_(label) + ' (' + name + ')') +
    text_('font-size:24px;line-height:32px;font-weight:bold;color:' + INK + ';', escapeHtml_(money_(values.close)) + ' <span style="font-size:13px;font-weight:normal;color:' + MUTED + ';">USD, latest close</span>') +
    table_('100%', 'margin-top:8px;', rows) + '</td></tr>');
}

function coverageStatus_(entry, rows) {
  const expected = /_return$/.test(entry.name) ? rows - 1 : rows;             // the first day has no return
  return { expected: expected, ok: entry.valid_rows === expected };
}

function coverageSection_(report) {
  const rows = report.meta.rows;
  const statuses = report.coverage.map(function (entry) { return coverageStatus_(entry, rows); });
  const complete = statuses.filter(function (status) { return status.ok; }).length;
  const head = '<tr>' + ['Variable', 'Valid rows', 'Latest value', 'Status'].map(function (title, i) {
    return '<th scope="col" align="' + (i === 0 || i === 3 ? 'left' : 'right') + '" style="padding:4px 6px;font-family:' + FONT + ';font-size:12px;line-height:16px;color:' + MUTED + ';border-bottom:1px solid ' + LINE + ';">' + title + '</th>';
  }).join('') + '</tr>';
  const body = report.coverage.map(function (entry, i) {
    const status = statuses[i];
    const cell = function (align, content, extra) { return '<td align="' + align + '" style="padding:3px 6px;font-family:' + FONT + ';font-size:12px;line-height:16px;color:' + INK + ';border-bottom:1px solid ' + LINE + ';' + (extra || '') + '">' + content + '</td>'; };
    return '<tr>' + cell('left', escapeHtml_(entry.name)) + cell('right', entry.valid_rows + ' of ' + status.expected) + cell('right', escapeHtml_(entry.latest)) +
      cell('left', status.ok ? 'OK' : 'MISSING', status.ok ? '' : 'font-weight:bold;color:#b91c1c;') + '</tr>';
  }).join('');
  return section_('Data quality: ' + complete + ' of ' + COVERAGE_NAMES.length + ' variables complete',
    text_('font-size:13px;line-height:19px;color:' + MUTED + ';padding-bottom:8px;', escapeHtml_(rows + ' daily rows from ' + report.meta.first_date + '. The two return variables have one row fewer, because the first day has no previous close.')) +
    table_('100%', '', head + body));
}

function chartSection_(chart) {
  const info = CHART_TEXT[chart.id];
  const legend = table_('', '', '<tr><td width="12" height="12" bgcolor="' + BTC_COLOUR + '" style="width:12px;height:12px;line-height:12px;font-size:1px;background-color:' + BTC_COLOUR + ';">&nbsp;</td><td style="padding:0 14px 0 6px;font-family:' + FONT + ';font-size:12px;color:' + MUTED + ';">BTC latest ' + escapeHtml_(chartValue_(chart.id, chart.btc_last)) + '</td>' +
    '<td width="12" height="12" bgcolor="' + ETH_COLOUR + '" style="width:12px;height:12px;line-height:12px;font-size:1px;background-color:' + ETH_COLOUR + ';">&nbsp;</td><td style="padding:0 0 0 6px;font-family:' + FONT + ';font-size:12px;color:' + MUTED + ';">ETH latest ' + escapeHtml_(chartValue_(chart.id, chart.eth_last)) + '</td></tr>');
  const alt = info.kind + ': ' + info.title.toLowerCase() + ', BTC and ETH, last 30 days. The latest value is ' + chartValue_(chart.id, chart.btc_last) + ' for BTC and ' + chartValue_(chart.id, chart.eth_last) + ' for ETH.';
  return '<tr><td class="pad" style="padding:16px 24px 0 24px;border-top:1px solid ' + LINE + ';">' + text_('font-size:14px;line-height:20px;font-weight:bold;color:' + INK + ';padding-bottom:6px;', escapeHtml_(info.title)) + legend + '</td></tr>' +
    '<tr><td style="padding:8px 0 0 0;"><img src="cid:chart_' + chart.id + '" alt="' + escapeHtml_(alt) + '" width="600" height="240" style="display:block;width:100%;max-width:600px;height:auto;border:0;outline:none;text-decoration:none;"></td></tr>' +
    '<tr><td class="pad" style="padding:4px 24px 12px 24px;">' + text_('font-size:11px;line-height:16px;color:' + MUTED + ';', 'Vertical scale ' + escapeHtml_(scaleEnd_(chart.id, chart.low)) + ' to ' + escapeHtml_(scaleEnd_(chart.id, chart.high)) + '. Orange is BTC, blue is ETH.') + '</td></tr>';
}

function linkButton_(href, label) {
  return '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;"><tr><td bgcolor="#1d4ed8" style="background-color:#1d4ed8;padding:10px 18px;"><a href="' + escapeHtml_(href) + '" style="font-family:' + FONT + ';font-size:14px;font-weight:bold;color:#ffffff;text-decoration:none;">' + escapeHtml_(label) + '</a></td></tr></table>';
}

function reportHtml_(d, secret) {
  const s = d.summary;
  const report = d.report;
  const meta = report.meta;
  const pair = report.pair;
  const ratio = pair.volatility_ratio === null ? 'not computable (BTC volatility is zero)' : pair.volatility_ratio.toFixed(3);
  const share = pair.overlap_share === null ? 'not assessable (an asset had no down days)' : pair.overlap_share.toFixed(2);
  const passed = d.validation === 'PASSED';
  const observations = s.observations.map(function (text) { return '<li style="margin:0 0 6px 0;">' + escapeHtml_(oneLine_(redact_(text, secret))) + '</li>'; }).join('');
  const parts = [];
  parts.push('<!DOCTYPE html>', '<html lang="en">', '<head>', '<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<title>Daily Market Analysis ' + escapeHtml_(d.date) + '</title>',
    '<style>@media only screen and (max-width:620px){.container{width:100% !important;}.stack{display:block !important;width:100% !important;padding-left:0 !important;padding-right:0 !important;}.pad{padding-left:14px !important;padding-right:14px !important;}}</style>', '</head>',
    '<body style="margin:0;padding:0;background-color:#f3f4f6;">',
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3f4f6" style="border-collapse:collapse;background-color:#f3f4f6;"><tr><td align="center" style="padding:16px 8px;">',
    '<table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="border-collapse:collapse;width:600px;background-color:#ffffff;border:1px solid ' + LINE + ';">');
  parts.push('<tr><td class="pad" bgcolor="#0f172a" style="background-color:#0f172a;padding:22px 24px;"><h1 style="margin:0;font-family:' + FONT + ';font-size:22px;line-height:28px;color:#ffffff;">Daily Market Analysis</h1>' +
    text_('font-size:14px;line-height:20px;color:#cbd5e1;padding-top:6px;', 'Bitcoin vs Ethereum, Kraken daily data, 30-day window. Analysis date (last completed UTC day): <strong style="color:#ffffff;">' + escapeHtml_(d.date) + '</strong>') +
    text_('font-size:13px;line-height:20px;color:#cbd5e1;padding-top:8px;', 'Data validation: <span style="background-color:' + (passed ? '#dcfce7' : '#fee2e2') + ';color:' + (passed ? '#166534' : '#991b1b') + ';font-weight:bold;padding:2px 8px;">' + escapeHtml_(d.validation) + '</span>&nbsp; Report status: <strong style="color:#ffffff;">' + escapeHtml_(d.reportStatus) + '</strong>') + '</td></tr>');
  parts.push(section_('Latest figures', '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;"><tr><td class="stack" width="50%" valign="top" style="padding:0 6px 12px 0;">' + assetCard_('BTC', 'Bitcoin', BTC_COLOUR, s.btc, report.assets.btc) +
    '</td><td class="stack" width="50%" valign="top" style="padding:0 0 12px 6px;">' + assetCard_('ETH', 'Ethereum', ETH_COLOUR, s.eth, report.assets.eth) + '</td></tr></table>' +
    text_('font-size:11px;line-height:16px;color:' + MUTED + ';padding-bottom:12px;', 'Returns are changes of the daily close; volatility is the sample standard deviation of the 30 daily returns; drawdown is the largest fall from a previous high within the window.')));
  parts.push(section_('How the two assets moved together', table_('100%', 'padding-bottom:12px;',
    metricRow_('Correlation of daily returns (30 days)', s.correlation.toFixed(6)) + metricRow_('Co-movement (rule R1)', oneLine_(s.rules.R1)) +
    metricRow_('ETH / BTC volatility ratio', ratio) + metricRow_('Relative volatility (rule R2)', oneLine_(s.rules.R2)) +
    metricRow_('Days both assets fell', pair.joint_negative_days + ' of ' + WINDOW_DAYS) + metricRow_('Down-day overlap share', share) + metricRow_('Down-day overlap (rule R3)', oneLine_(s.rules.R3)))));
  if (observations) parts.push(section_('Observations', '<ul style="margin:0 0 12px 18px;padding:0;font-family:' + FONT + ';font-size:13px;line-height:19px;color:' + INK + ';">' + observations + '</ul>' +
    text_('font-size:11px;line-height:16px;color:' + MUTED + ';padding-bottom:12px;', 'Descriptive labels for this 30-day sample only, not advice.')));
  report.charts.forEach(function (chart) { parts.push(chartSection_(chart)); });
  parts.push(coverageSection_(report));
  parts.push(section_('Google Sheets and deliverables', table_('100%', 'padding-bottom:6px;', metricRow_('Google Sheets', 'updated and verified by reading the rows back (' + d.update.rows_inserted + ' inserted, ' + d.update.rows_updated + ' updated)') +
    metricRow_('Data fetched', meta.data_fetched) + metricRow_('Report generated', meta.generated_at)) +
    text_('font-size:13px;line-height:19px;color:' + INK + ';padding:10px 0 10px 0;', 'The full interactive HTML dashboard, the Markdown report and the CSV data are in one downloadable file. Open the page, sign in to GitHub, download the archive and open the HTML file in a browser. The files are kept for 14 days.') +
    linkButton_(d.artifactUrl, 'Download the dashboard, report and CSV') + '<p style="margin:12px 0 0 0;font-family:' + FONT + ';font-size:13px;line-height:19px;color:' + INK + ';">Workflow run ' + escapeHtml_(d.runId) + ': <a href="' + escapeHtml_(d.url) + '" style="color:#1d4ed8;">' + escapeHtml_(d.url) + '</a></p>'));
  parts.push('<tr><td class="pad" style="padding:16px 24px 20px 24px;border-top:1px solid ' + LINE + ';">' + text_('font-size:12px;line-height:18px;color:' + MUTED + ';', 'Executed: ' + escapeHtml_(timesText_())) +
    text_('font-size:12px;line-height:18px;color:' + MUTED + ';padding-top:4px;', 'Educational use only. Not financial advice.') + '</td></tr>');
  parts.push('</table>', '</td></tr></table>', '</body>', '</html>');
  return parts.join('\n');
}

function chartBlobs_(report) {
  const blobs = {};
  report.charts.forEach(function (chart) {
    blobs['chart_' + chart.id] = Utilities.newBlob(Utilities.base64Decode(chart.png_base64), 'image/png', 'chart_' + chart.id + '.png');
  });
  return blobs;
}

// Why a built HTML page must not be sent, or null. A last check of the page itself, made before the mail service is called: the expected start, a size below the point where Gmail clips a message,
// none of the constructs the report promises never to contain, and exactly the three inline charts (every src is a cid and every chart is referenced once).
function htmlProblem_(html, imageKeys) {
  if (typeof html !== 'string' || html.indexOf('<!DOCTYPE html>') !== 0) return 'the page does not start as expected';
  if (html.length > MAX_HTML_CHARS) return 'the page is too long';
  if (/<(script|iframe|form|object|embed|link|base|input|button|svg)[\s>\/]|javascript:|http:\/\/|@import|url\(|\ssrc="(?!cid:)/i.test(html)) return 'the page holds a construct that is not allowed';
  const referenced = (html.match(/src="cid:[a-z_]+"/g) || []).map(function (found) { return found.replace(/^src="cid:|"$/g, ''); }).sort();
  if (referenced.join(',') !== imageKeys.slice().sort().join(',')) return 'the inline images do not match the page';
  return null;
}

// Everything that can fail BEFORE the HTML message is handed to the mail service: the HTML, the image attachments and the check of the page. Throws if the message is not fit to be sent.
function prepareHtmlMessage_(recipient, subject, text, data, secret) {
  const html = reportHtml_(data, secret);
  const images = chartBlobs_(data.report);
  const problem = htmlProblem_(html, Object.keys(images));
  if (problem) throw new Error('html report check failed: ' + problem);
  return { to: recipient, subject: subject, body: text, htmlBody: html, inlineImages: images, name: 'Daily Market Analysis' };
}

// The plain-text fallback exists ONLY for the time before the HTML message is handed to the mail service: if the HTML cannot be prepared or fails its check, the plain-text success e-mail goes out instead
// (reported as text_fallback). Once MailApp.sendEmail has been called for the HTML message, an error leaves it unknown whether the message was accepted, so NOTHING more is sent: the reply is
// SEND_OUTCOME_UNKNOWN (email_status send_outcome_unknown), no duplicate-protection entry is written, and there is no retry. Returns the format that went out.
function sendReportMail_(recipient, subject, text, data, secret) {
  let message = null;
  try {
    message = prepareHtmlMessage_(recipient, subject, text, data, secret);
  } catch (err) {
    console.error('html report not prepared, the plain-text e-mail is used instead: ' + describeError_(err, secret));
  }
  if (message) {
    try {
      MailApp.sendEmail(message);
    } catch (err) {
      console.error('html report: the send outcome is unknown, nothing more is sent: ' + describeError_(err, secret));
      throw new ApiError('SEND_OUTCOME_UNKNOWN', 'ok', 'send_outcome_unknown');
    }
    return 'html';
  }
  sendMail_({ to: recipient, subject: subject, body: text, name: 'Daily Market Analysis' }, 'ok', secret);
  return 'text_fallback';
}
