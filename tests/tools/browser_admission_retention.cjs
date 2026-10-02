"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const source = fs.readFileSync(process.argv[2], "utf8");
const canonical = new Map();
const nativeChild = process.argv[3] ? require("node:child_process").spawn(process.argv[3], [process.argv[4], process.argv[5]], { stdio: ["pipe", "pipe", "inherit"] }) : null;
let nativeBuffer = Buffer.alloc(0), pending = [];
if (nativeChild) nativeChild.stdout.on("data", chunk => {
  nativeBuffer = Buffer.concat([nativeBuffer, chunk]);
  while (nativeBuffer.length >= 4 && nativeBuffer.length >= 4 + nativeBuffer.readUInt32LE(0)) {
    const size = nativeBuffer.readUInt32LE(0);
    pending.shift()(JSON.parse(nativeBuffer.subarray(4, size + 4)));
    nativeBuffer = nativeBuffer.subarray(size + 4);
  }
});
function nativeCall(message) {
  return new Promise(resolve => {
    pending.push(resolve);
    const body = Buffer.from(JSON.stringify(message)), header = Buffer.alloc(4);
    header.writeUInt32LE(body.length);
    nativeChild.stdin.write(Buffer.concat([header, body]));
  });
}
let stored = {}, sendCount = 0, loseAck = false;
function startWorker() {
  const event = { addListener() {} };
  const runtime = { id: "mopnolkjddkmgojfjkenjobehhmmklll", onMessage: event, onInstalled: event, onStartup: event,
    getURL: path => `mock://${path}`,
    sendNativeMessage(host, message, callback) {
      if (nativeChild) {
        if (message.action === "admission.submit") sendCount++;
        nativeCall(message).then(response => {
          if (loseAck && message.action === "admission.submit") {
            loseAck = false; runtime.lastError = { message: "Lost Native ACK" }; callback(); runtime.lastError = null;
          } else callback(response);
        });
        return;
      }
      // Native Messaging's framed JSON round trip is represented explicitly;
      // the Python integration suite independently verifies the durable store.
      const frame = Buffer.from(JSON.stringify(message));
      const header = Buffer.alloc(4); header.writeUInt32LE(frame.length);
      const request = JSON.parse(Buffer.concat([header, frame]).subarray(4));
      let receipt = canonical.get(request.submission_key || request.request?.submission_key);
      const requestDigest = request.request_digest || request.request?.request_digest;
      let status = "success", error_code;
      if (receipt && receipt.request_digest !== requestDigest) { status = "failed"; error_code = "submission_conflict"; }
      else if (request.action === "admission.submit") {
        sendCount++;
        receipt ||= { submission_key: request.request.submission_key, request_digest: requestDigest, status: "ACCEPTED", task_id: `task-${sendCount}`, intent_revision_id: `intent-${sendCount}` };
        canonical.set(receipt.submission_key, receipt);
        if (loseAck) { loseAck = false; runtime.lastError = { message: "Lost Native ACK" }; callback(); runtime.lastError = null; return; }
      }
      callback({ schema: "bdb-vnext-native-response-v1", generation_id: "bdb-vnext-g1", protocol_generation: "bdb-vnext-protocol-v1",
        native_host_name: host, browser_extension_id: runtime.id, status, receipt, error_code });
    }
  };
  const context = vm.createContext({ crypto: webcrypto, TextEncoder, Uint8Array, URL,
    chrome: { runtime, storage: { local: { async get(key) { return { [key]: structuredClone(stored[key]) }; }, async set(value) { stored = { ...stored, ...structuredClone(value) }; } } } },
    fetch() { return Promise.reject(new Error("No Browser installation in this source harness")); }
  });
  vm.runInContext(source + "\nglobalThis.api = {submit, lookup, readOutbox, resumeOutbox, prepare, transition};", context);
  return context.api;
}
const request = key => ({ submission_key: key, intent_revision: "r1", intent: { operation: "inspect" }, conversation_binding: { conversation_id: "c1" }, consumer_binding: { consumer_id: "browser", kind: "browser" } });
(async () => {
  let api = startWorker();
  for (let i = 0; i < 400; i++) assert.equal((await api.submit(request(`k${i}`))).ok, true);
  assert.ok(Object.keys((await api.readOutbox()).entries).length <= 128);
  const beforeReplay = sendCount;
  const replay = await api.submit(request("k0"));
  assert.equal(replay.replay, true);
  assert.equal(sendCount, beforeReplay);
  await assert.rejects(() => api.submit({ ...request("k1"), intent: { operation: "other" } }), /rejected|request/i);
  assert.equal(sendCount, beforeReplay);
  loseAck = true;
  await assert.rejects(() => api.submit(request("lost")), /Lost Native ACK/);
  assert.equal((await api.readOutbox()).entries.lost.state, "UNKNOWN");
  for (let i = 400; i < 550; i++) await api.submit(request(`k${i}`));
  assert.equal((await api.readOutbox()).entries.lost.state, "UNKNOWN");
  api = startWorker();
  if (nativeChild) await nativeCall({ action: "test.restart" });
  await api.resumeOutbox();
  assert.equal((await api.readOutbox()).entries.lost.state, "ACKED");
  assert.equal((await api.submit(request("lost"))).replay, true);
  const tasks = nativeChild ? (await nativeCall({ action: "test.stats" })).counts.tasks : canonical.size;
  assert.equal(tasks, 551);
  console.log(JSON.stringify({ status: "PASS", requests: 550, tasks, framedNative: !!nativeChild, entries: Object.keys((await api.readOutbox()).entries).length, sendCount }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { if (nativeChild) nativeChild.stdin.end(); });
