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
const SHEET_ACTIONS = ['upsert_analysis', 'verify_analysis', 'send_success_email'];
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
    cache.put(dedupeKey, '1', DEDUPE_SECONDS);
    return { sheet_status: 'ok', email_status: 'sent' };
  });
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
    cache.put(dedupeKey, '1', DEDUPE_SECONDS);
    return { sheet_status: sheetStatus, email_status: 'sent' };
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
