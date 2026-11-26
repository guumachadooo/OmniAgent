import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workerPath = fs.existsSync(path.resolve(__dirname, "../worker/cloudflare_live_relay_worker.js"))
  ? path.resolve(__dirname, "../worker/cloudflare_live_relay_worker.js")
  : path.resolve(__dirname, "../docs/cloudflare_live_relay_worker.js");
const workerMod = await import(pathToFileURL(workerPath).href);
const worker = workerMod.default;
const { HubCoordinator } = workerMod;

function makeMockKv() {
  const store = new Map();
  let puts = 0;
  return {
    get putCount() {
      return puts;
    },
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async put(key, val) {
      puts++;
      store.set(key, String(val));
    },
    async delete(key) {
      store.delete(key);
    },
  };
}

function makeMockDoNamespace() {
  const instances = new Map();
  return {
    idFromName(name) {
      return `do-id:${name}`;
    },
    get(id) {
      if (!instances.has(id)) {
        const backing = new Map();
        const state = {
          storage: {
            async get(k) {
              return backing.has(k) ? backing.get(k) : undefined;
            },
            async put(k, v) {
              backing.set(k, v);
            },
            async delete(k) {
              backing.delete(k);
            },
          },
        };
        instances.set(id, new HubCoordinator(state, {}));
      }
      return instances.get(id);
    },
  };
}

function sha256(text) {
  return crypto.createHash("sha256").update(text).digest("hex");
}

const PAIR_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const DEV_HASH = sha256(`wake-channel-v2:${PAIR_SECRET}`);
const KNOCK_ID = sha256(`knock-channel-v5:${DEV_HASH}`);
const AGENT_KEY = sha256(`agent-access-v4:${PAIR_SECRET}`);
const TEL_KEY = sha256(`telemetry-access-v3:${PAIR_SECRET}`);
const SAMPLE_PUB_B64 = Buffer.concat([Buffer.from([0x04]), Buffer.alloc(64, 0x42)]).toString("base64");

async function req(env, method, path, headers = {}, body = null) {
  const init = { method, headers: { ...headers } };
  if (body !== null) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await worker.fetch(new Request(`https://relay.example.workers.dev${path}`, init), env);
  return { status: r.status, json: await r.json() };
}

test("Worker v6 health reports Hybrid Durable Objects & KV fallback modes accurately", async () => {
  const envKvOnly = { RELAY_KV: makeMockKv() };
  const rKv = await req(envKvOnly, "GET", "/health");
  assert.equal(rKv.status, 200);
  assert.equal(rKv.json.version, 6);
  assert.equal(rKv.json.supportsDurableObjects, false);
  assert.equal(rKv.json.storageMode, "kv_fallback");
  assert.equal(rKv.json.recommendedHeartbeatSec, 180);
  assert.equal(rKv.json.supportsSmartKnock, true);
  assert.equal(rKv.json.supportsOmniAgent, true);
  assert.equal(rKv.json.supportsChunkedBlobs, true);
  assert.equal(rKv.json.supportsSwarmBus, true);
  assert.equal(rKv.json.supportsHotExec, true);
  assert.equal(rKv.json.supportsTelemetry, true);

  const envDo = { RELAY_KV: makeMockKv(), HUB_DO: makeMockDoNamespace() };
  const rDo = await req(envDo, "GET", "/health");
  assert.equal(rDo.status, 200);
  assert.equal(rDo.json.version, 6);
  assert.equal(rDo.json.supportsDurableObjects, true);
  assert.equal(rDo.json.storageMode, "durable_object_sqlite");
  assert.equal(rDo.json.recommendedHeartbeatSec, 60);
});

test("Worker v6 Durable Object handles real-time agent/knock/session with ZERO KV writes while keeping telemetry in KV", async () => {
  const kv = makeMockKv();
  const env = { RELAY_KV: kv, HUB_DO: makeMockDoNamespace() };
  const hAndroid = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY, "X-Agent-Channel": "android" };
  const hPublic = { "X-Knock-Id": KNOCK_ID };
  const hOwner = { "X-Device-Hash": DEV_HASH };

  // Heartbeat + cmd + poll + result all hit HubCoordinator DO -> 0 KV writes!
  assert.equal((await req(env, "POST", "/v1/agent/heartbeat", hAndroid, { hostEnv: "android-companion" })).status, 200);
  assert.equal((await req(env, "POST", "/v1/agent/cmd", hAndroid, { requestId: "do1", envelope: { v: 4, ct: "hi" } })).status, 200);
  const poll = await req(env, "GET", "/v1/agent/poll", hAndroid);
  assert.equal(poll.json.hasCommand, true);
  assert.equal(poll.json.command.requestId, "do1");
  assert.equal((await req(env, "POST", "/v1/agent/result", hAndroid, { requestId: "do1", envelope: { v: 4, ct: "bye" } })).status, 200);
  const res = await req(env, "GET", "/v1/agent/result?requestId=do1", hAndroid);
  assert.equal(res.json.ready, true);
  assert.equal(res.json.result.envelope.ct, "bye");

  // Smart Knock also uses DO -> still 0 KV writes!
  assert.equal((await req(env, "POST", "/v1/knock/request", hPublic, { knockReqId: "do_k1", clientPubKey: SAMPLE_PUB_B64 })).status, 200);
  const kp = await req(env, "GET", "/v1/knock/poll", hOwner);
  assert.equal(kp.json.hasKnock, true);
  assert.equal(kv.putCount, 0);

  // Telemetry still writes to RELAY_KV
  const hTel = { "X-Device-Hash": DEV_HASH, "X-Telemetry-Key": TEL_KEY };
  assert.equal((await req(env, "POST", "/v1/telemetry/upload", hTel, { kind: "summary", content: "{\"ok\":true}" })).status, 200);
  assert.ok(kv.putCount > 0);
});

test("Worker v4 /v1/agent/* multi-channel heartbeat, FIFO queue, and result flow", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const hTermux = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY, "X-Agent-Channel": "termux" };
  const hAndroid = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY, "X-Agent-Channel": "android" };

  assert.equal((await req(env, "POST", "/v1/agent/heartbeat", hTermux, { hostEnv: "debian-proot", securityMode: "smart" })).status, 200);
  assert.equal((await req(env, "POST", "/v1/agent/heartbeat", hAndroid, { hostEnv: "android-companion", securityMode: "smart" })).status, 200);

  const stAll = await req(env, "GET", "/v1/agent/status?all=1", hTermux);
  assert.equal(stAll.json.channels.termux.online, true);
  assert.equal(stAll.json.channels.android.online, true);

  // Queue two commands on termux and verify strict FIFO isolation from android
  assert.equal((await req(env, "POST", "/v1/agent/cmd", hTermux, { requestId: "r1", envelope: { v: 4, ct: "a" } })).status, 200);
  assert.equal((await req(env, "POST", "/v1/agent/cmd", hTermux, { requestId: "r2", envelope: { v: 4, ct: "b" } })).status, 200);

  const pAndroid = await req(env, "GET", "/v1/agent/poll", hAndroid);
  assert.equal(pAndroid.json.hasCommand, false);

  const p1 = await req(env, "GET", "/v1/agent/poll", hTermux);
  assert.equal(p1.json.hasCommand, true);
  assert.equal(p1.json.command.requestId, "r1");
  assert.equal(p1.json.remaining, 1);

  const p2 = await req(env, "GET", "/v1/agent/poll", hTermux);
  assert.equal(p2.json.command.requestId, "r2");

  assert.equal((await req(env, "POST", "/v1/agent/result", hTermux, { requestId: "r1", envelope: { v: 4, ct: "ok" } })).status, 200);
  const res1 = await req(env, "GET", "/v1/agent/result?requestId=r1", hTermux);
  assert.equal(res1.json.ready, true);
  assert.equal(res1.json.result.envelope.ct, "ok");
});

test("Worker v4 /v1/blob/* chunked upload, commit, download, and delete", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const h = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY };

  assert.equal((await req(env, "POST", "/v1/blob/chunk", h, { blobId: "b1", index: 0, data: "PART0_" })).status, 200);
  assert.equal((await req(env, "POST", "/v1/blob/chunk", h, { blobId: "b1", index: 1, data: "PART1" })).status, 200);
  assert.equal((await req(env, "POST", "/v1/blob/commit", h, { blobId: "b1", totalChunks: 2, sha256: "abc", bytes: 11 })).status, 200);

  const meta = await req(env, "GET", "/v1/blob/meta?blobId=b1", h);
  assert.equal(meta.json.meta.totalChunks, 2);
  const c0 = await req(env, "GET", "/v1/blob/chunk?blobId=b1&index=0", h);
  const c1 = await req(env, "GET", "/v1/blob/chunk?blobId=b1&index=1", h);
  assert.equal(c0.json.data + c1.json.data, "PART0_PART1");

  const del = await req(env, "DELETE", "/v1/blob/delete?blobId=b1", h);
  assert.equal(del.json.deletedChunks, 2);
  assert.equal((await req(env, "GET", "/v1/blob/meta?blobId=b1", h)).status, 404);
});

test("Worker v4 /v1/swarm/* enqueue, claim, complete, and clear lifecycle", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const h = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY };

  const enq = await req(env, "POST", "/v1/swarm/enqueue", h, {
    taskId: "t_audit",
    role: "debian-worker",
    title: "Run acoustic check",
    instructions: "Run check_android_cs.py",
  });
  assert.equal(enq.status, 200);
  assert.equal(enq.json.task.status, "pending");

  const clm = await req(env, "POST", "/v1/swarm/claim", h, { workerId: "antigravity-debian", role: "debian-worker" });
  assert.equal(clm.json.claimed, true);
  assert.equal(clm.json.task.taskId, "t_audit");

  const cmp = await req(env, "POST", "/v1/swarm/complete", h, {
    taskId: "t_audit",
    workerId: "antigravity-debian",
    ok: true,
    summary: "All 7 layers PASS (0 errors, 0 warnings)",
  });
  assert.equal(cmp.json.status, "done");

  const full = await req(env, "GET", "/v1/swarm/task?taskId=t_audit", h);
  assert.equal(full.json.task.result.summary, "All 7 layers PASS (0 errors, 0 warnings)");

  const clr = await req(env, "DELETE", "/v1/swarm/clear?doneOnly=1", h);
  assert.equal(clr.json.cleared, 1);
});

test("Worker v4 preserves 100% backwards-compatibility with v3 telemetry & session endpoints", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const hTel = { "X-Device-Hash": DEV_HASH, "X-Telemetry-Key": TEL_KEY };
  assert.equal((await req(env, "POST", "/v1/telemetry/upload", hTel, { kind: "summary", content: "{\"ok\":true}" })).status, 200);
  const tel = await req(env, "GET", "/v1/telemetry/latest?kind=summary", hTel);
  assert.equal(tel.json.content, "{\"ok\":true}");

  const hSess = { "X-Session-Token": "CAV-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF" };
  assert.equal((await req(env, "POST", "/v1/session/open", hSess, { ttlSeconds: 600, maxTier: 2 })).status, 200);
  assert.equal((await req(env, "POST", "/v1/session/cmd", hSess, { cmd: "hot_probe_status", args: {} })).status, 200);
  const poll = await req(env, "GET", "/v1/session/poll", hSess);
  assert.equal(poll.json.command.cmd, "hot_probe_status");
});

test("Worker v5 /v1/knock/* zero-secret ECDH lifecycle, owner X-Device-Hash auth, 409 lock & deny cooldown", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const hPublic = { "X-Knock-Id": KNOCK_ID };
  const hOwner = { "X-Device-Hash": DEV_HASH };

  // 1. Public AI knocks with double-hashed knockId & ephemeral P-256 public key
  const k1 = await req(env, "POST", "/v1/knock/request", hPublic, {
    knockReqId: "k_req_1",
    clientPubKey: SAMPLE_PUB_B64,
    requestedScope: "workspace",
    requestedTier: 2,
    reason: "Connect to S25U",
  });
  assert.equal(k1.status, 200);
  assert.equal(k1.json.knockReqId, "k_req_1");

  // 2. Single-slot lock rejects concurrent knock with 409
  const dup = await req(env, "POST", "/v1/knock/request", hPublic, {
    knockReqId: "k_req_dup",
    clientPubKey: SAMPLE_PUB_B64,
  });
  assert.equal(dup.status, 409);

  // 3. Public caller CANNOT poll or approve (missing X-Device-Hash -> 401)
  assert.equal((await req(env, "GET", "/v1/knock/poll", hPublic)).status, 401);
  assert.equal((await req(env, "POST", "/v1/knock/approve", hPublic, { knockReqId: "k_req_1", serverPubKey: SAMPLE_PUB_B64, encryptedGrant: { v: 4 } })).status, 401);

  // 4. Owner phone polls with X-Device-Hash (Worker derives knockId server-side)
  const p = await req(env, "GET", "/v1/knock/poll", hOwner);
  assert.equal(p.status, 200);
  assert.equal(p.json.hasKnock, true);
  assert.equal(p.json.knock.knockReqId, "k_req_1");

  // 5. Owner approves with E2E encrypted grant
  const app = await req(env, "POST", "/v1/knock/approve", hOwner, {
    knockReqId: "k_req_1",
    serverPubKey: SAMPLE_PUB_B64,
    encryptedGrant: { v: 4, ct: "enc_payload" },
    scope: "workspace",
  });
  assert.equal(app.status, 200);

  // 6. AI receives approved grant once (then auto-deleted from KV -> 404)
  const st1 = await req(env, "GET", "/v1/knock/status?knockReqId=k_req_1", hPublic);
  assert.equal(st1.status, 200);
  assert.equal(st1.json.status, "approved");
  assert.equal(st1.json.encryptedGrant.ct, "enc_payload");
  assert.equal((await req(env, "GET", "/v1/knock/status?knockReqId=k_req_1", hPublic)).status, 404);

  // 7. Deny activates automatic lockout cooldown (429 on subsequent knock)
  const k2 = await req(env, "POST", "/v1/knock/request", hPublic, {
    knockReqId: "k_req_2",
    clientPubKey: SAMPLE_PUB_B64,
  });
  assert.equal(k2.status, 200);
  await req(env, "GET", "/v1/knock/poll", hOwner);
  const den = await req(env, "POST", "/v1/knock/deny", hOwner, { knockReqId: "k_req_2", reason: "Not me", cooldownSeconds: 900 });
  assert.equal(den.status, 200);
  const blocked = await req(env, "POST", "/v1/knock/request", hPublic, {
    knockReqId: "k_req_3",
    clientPubKey: SAMPLE_PUB_B64,
  });
  assert.equal(blocked.status, 429);
});

test("Worker v5 /v1/knock/request enforces 3-per-15-min rate limit", async () => {
  const env = { RELAY_KV: makeMockKv() };
  const hPublic = { "X-Knock-Id": KNOCK_ID };
  const hOwner = { "X-Device-Hash": DEV_HASH };

  for (let i = 1; i <= 3; i++) {
    const r = await req(env, "POST", "/v1/knock/request", hPublic, {
      knockReqId: `rate_${i}`,
      clientPubKey: SAMPLE_PUB_B64,
    });
    assert.equal(r.status, 200);
    await req(env, "GET", "/v1/knock/poll", hOwner);
  }
  const r4 = await req(env, "POST", "/v1/knock/request", hPublic, {
    knockReqId: "rate_4",
    clientPubKey: SAMPLE_PUB_B64,
  });
  assert.equal(r4.status, 429);
});

test("Worker v6.1 preserves 7-day presence history with ageSeconds, standbyReachable & FGS diagnostics when stale", async () => {
  const kv = makeMockKv();
  const env = { RELAY_KV: kv };
  const hAndroid = { "X-Device-Hash": DEV_HASH, "X-Agent-Key": AGENT_KEY, "X-Agent-Channel": "android" };

  const health = await req(env, "GET", "/health");
  assert.equal(health.json.supportsPresenceHistory, true);
  assert.equal(health.json.presenceOnlineTtlSec, 420);
  assert.equal(health.json.cmdTtlSec, 900);

  // Send heartbeat with FGS/battery diagnostics
  const hb = await req(env, "POST", "/v1/agent/heartbeat", hAndroid, {
    hostEnv: "android-companion",
    device: "Samsung SM-S938B",
    fgsMode: "remoteMessaging",
    batteryUnrestricted: true,
    androidSdk: 35,
    lastExitReason: "clean",
  });
  assert.equal(hb.status, 200);
  assert.equal(hb.json.historyTtlSeconds, 7 * 24 * 3600);

  // Fresh heartbeat -> online: true, standbyReachable: true
  const fresh = await req(env, "GET", "/v1/agent/status?all=1", hAndroid);
  assert.equal(fresh.json.channels.android.online, true);
  assert.equal(fresh.json.channels.android.standbyReachable, true);
  assert.equal(fresh.json.channels.android.fgsMode, "remoteMessaging");
  assert.equal(fresh.json.channels.android.batteryUnrestricted, true);
  assert.equal(fresh.json.channels.android.androidSdk, 35);

  // Simulate heartbeat aged 12 minutes (> 420s online, <= 1800s standbyReachable)
  const bucket = sha256(`${DEV_HASH}:${AGENT_KEY}`);
  const key = `ag:${bucket}:android:presence`;
  const rec = JSON.parse(await kv.get(key));
  rec.lastSeenUtc = new Date(Date.now() - 720 * 1000).toISOString();
  await kv.put(key, JSON.stringify(rec));

  const stale = await req(env, "GET", "/v1/agent/status?all=1", hAndroid);
  assert.equal(stale.json.channels.android.online, false);
  assert.equal(stale.json.channels.android.standbyReachable, true);
  assert.ok(stale.json.channels.android.ageSeconds >= 715);
  assert.equal(stale.json.channels.android.fgsMode, "remoteMessaging");

  // Simulate heartbeat aged 2 hours (> 1800s) -> still keeps lastSeenUtc & device diagnostics
  rec.lastSeenUtc = new Date(Date.now() - 7200 * 1000).toISOString();
  await kv.put(key, JSON.stringify(rec));
  const old = await req(env, "GET", "/v1/agent/status?channel=android", hAndroid);
  assert.equal(old.json.online, false);
  assert.equal(old.json.standbyReachable, false);
  assert.ok(old.json.ageSeconds >= 7195);
  assert.equal(old.json.presence.device, "Samsung SM-S938B");
});


