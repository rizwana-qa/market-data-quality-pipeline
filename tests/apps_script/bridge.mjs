// Local HTTP bridge for tests/test_contract_end_to_end.py: lets the REAL Python client talk to the REAL apps_script/Code.gs, which runs in the Node test harness with imitated Google services
// (spreadsheet, mail, lock, cache, properties). Like Google's web app it answers a POST with a redirect to an address that serves the JSON reply. It listens on 127.0.0.1 only.
//   POST /macros/s/<id>/exec  -> run Code.gs doPost with the body, answer 302 to /echo/<n>      GET /echo/<n>  -> the JSON reply
//   POST /_reset {options}    -> new imitated world (options as for makeWorld)                    GET /_state    -> sheet rows, headings, mail sent, write/lock/cache counters
import http from "node:http";
import { makeWorld } from "./harness.mjs";

let world = makeWorld();
const replies = [];
const send = (res, status, body, headers = {}) => { const data = Buffer.from(body); res.writeHead(status, Object.assign({ "Content-Length": data.length }, headers)); res.end(data); };

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    const body = Buffer.concat(chunks).toString("utf8");
    try {
      if (req.method === "POST" && req.url.startsWith("/macros/s/")) {
        replies.push(world.rawCall(body).getContent());
        return send(res, 302, "", { Location: `/echo/${replies.length - 1}` });
      }
      if (req.method === "GET" && req.url.startsWith("/echo/")) return send(res, 200, replies[Number(req.url.split("/")[2])] ?? "{}", { "Content-Type": "application/json; charset=utf-8" });
      if (req.method === "POST" && req.url === "/_reset") {
        const options = JSON.parse(body || "{}");
        if (options.now) options.now = new Date(options.now);
        world = makeWorld(options); replies.length = 0;
        return send(res, 200, "{}", { "Content-Type": "application/json" });
      }
      if (req.method === "POST" && req.url === "/_mail_failure") {      // make the imitated mail service fail ({"message": "..."}) or work again ({})
        const request = JSON.parse(body || "{}");                                                  // {"message": "...", "only_html": true} makes only the HTML message fail
        world.state.mail.failWith = request.only_html || request.after_send ? null : request.message || null; world.state.mail.failHtmlWith = request.only_html ? request.message || null : null;
        world.state.mail.failHtmlAfterSend = request.after_send ? request.message || "error after sending" : null; world.state.failBlob = Boolean(request.fail_blob);       // after_send: delivered, then an error; fail_blob: the HTML message cannot be prepared
        return send(res, 200, "{}", { "Content-Type": "application/json" });
      }
      if (req.method === "POST" && req.url === "/_cache_failure") {      // make the imitated cache service fail on a write and/or a read ({"put": "...", "get": "..."}) or work again ({})
        const request = JSON.parse(body || "{}"); world.state.cacheFail.put = request.put || null; world.state.cacheFail.get = request.get || null;
        return send(res, 200, "{}", { "Content-Type": "application/json" });
      }
      if (req.method === "GET" && req.url === "/_state") {
        return send(res, 200, JSON.stringify({ rows: world.dataRows(), headings: world.sheet.data[0], mail: world.state.mail.sent, mail_attempts: world.state.mail.attempts, writes: world.state.setValuesCalls, lock_waits: world.state.lock.waits, cache_entries: world.state.cache.size, cache_puts: world.state.cachePuts }), { "Content-Type": "application/json" });
      }
      send(res, 404, "not found");
    } catch (error) {
      send(res, 500, String(error && error.message));
    }
  });
});
server.listen(0, "127.0.0.1", () => console.log(`PORT=${server.address().port}`));
