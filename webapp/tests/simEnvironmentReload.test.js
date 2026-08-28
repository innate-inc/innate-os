// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { createSimEnvironmentWatcher, startSimEnvironmentWatcher } from "../js/simEnvironmentReload.js";

let passed = 0;
/** @param {string} name @param {() => Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

function response(payload, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

const environment = (id, fingerprint) => ({
  id,
  display_name: id === "a" ? "Apartment" : "Intersection",
  ...(fingerprint ? { fingerprint } : {}),
});
const catalog = (id, fingerprint) => ({
  schema_version: 1,
  active: environment(id, fingerprint),
  environments: [environment("a"), environment("b")],
});

await test("switch survives an outage and unlocks only after job, viewer, and physics identities agree", async () => {
  let reloads = 0;
  const previousLocation = globalThis.location;
  globalThis.location = { reload: () => (reloads += 1) };
  let activeCatalog = catalog("a", "fp-a");
  let jobState = { state: "queued", phase: "queued" };
  let catalogOutage = false;
  const states = [];
  const requests = [];
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url, init = {}) => {
      requests.push({ url, init });
      if (url === "/sim-environments.json") {
        if (catalogOutage) throw new Error("proxy restarting");
        return response(activeCatalog);
      }
      if (url === "/sim-environment/switch" && init.method === "POST") {
        return response({ job_id: "job-1", state: "queued", target: environment("b") }, 202);
      }
      if (url === "/sim-environment/switch/job-1") return response(jobState);
      throw new Error(`unexpected request ${url}`);
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
    onState: (state) => states.push(state),
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  await watcher.requestSwitch("b");
  assert.equal(watcher.snapshot().active, true);
  assert.equal(
    requests.find(({ url }) => url === "/sim-environment/switch").init.headers["X-Requested-By"],
    "innate-webapp",
  );

  catalogOutage = true;
  await watcher.poll();
  assert.equal(watcher.snapshot().active, true, "transient proxy failure must preserve the transition");

  catalogOutage = false;
  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-b", ready: true });
  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  assert.equal(watcher.snapshot().active, true, "matching scenes must wait for the host job to become ready");
  watcher.noteWorld({ id: "b", fingerprint: "wrong-world", connected: true });
  jobState = { state: "ready", phase: "ready", target: environment("b"), fingerprint: "fp-b" };
  catalogOutage = true;
  await watcher.poll();
  assert.equal(watcher.snapshot().active, true, "a ready job must still wait for matching physics identity");
  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  assert.equal(watcher.snapshot().active, false);
  assert.equal(states.at(-1).status, "complete");
  assert.equal(reloads, 0, "the document must remain mounted");
  if (previousLocation === undefined) delete globalThis.location;
  else globalThis.location = previousLocation;
});

await test("adopts a same-target 409 job and unlocks only after a verified rollback", async () => {
  const apartment = catalog("a", "fp-a");
  let catalogRequests = 0;
  const catalogs = [];
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url, init = {}) => {
      if (url === "/sim-environments.json") {
        catalogRequests += 1;
        return response(catalogRequests === 1 ? { ...apartment, active: null } : apartment);
      }
      if (url === "/sim-environment/switch" && init.method === "POST") {
        return response(
          { error: "already switching", job_id: "job-2", state: "running", target: environment("b") },
          409,
        );
      }
      if (url === "/sim-environment/switch/job-2") {
        return response({
          state: "failed",
          phase: "rollback",
          message: "Intersection failed. Apartment was restored.",
          recovered_environment: environment("a", "fp-a"),
        });
      }
      throw new Error(`unexpected request ${url}`);
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
    onCatalog: (value) => catalogs.push(value),
  });

  await watcher.poll();
  assert.equal(catalogs[0].active, null, "a healthy catalog may temporarily have no active environment");
  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  await watcher.requestSwitch("b");
  assert.equal(watcher.snapshot().jobId, "job-2");
  await watcher.poll();
  assert.equal(
    watcher.snapshot().recoverySafe,
    true,
    "verified backend recovery plus the still-connected matching world permits acknowledgment",
  );
  assert.equal(watcher.snapshot().active, true, "rollback remains interlocked until the user acknowledges it");
  watcher.continueRecovery();
  assert.equal(watcher.snapshot().active, false);
});

await test("a repeatedly broken visual scene offers an explicit return to the previous environment", async () => {
  const requested = [];
  let activeCatalog = catalog("a", "fp-a");
  let returnReady = false;
  let job = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url, init = {}) => {
      if (url === "/sim-environments.json") return response(activeCatalog);
      if (url === "/sim-environment/switch" && init.method === "POST") {
        const id = JSON.parse(init.body).id;
        requested.push(id);
        job += 1;
        return response({ job_id: `job-${job}`, state: "queued", target: environment(id) }, 202);
      }
      if (url === "/sim-environment/switch/job-1") {
        return response({ state: "ready", phase: "ready", target: environment("b"), fingerprint: "fp-b" });
      }
      if (url === "/sim-environment/switch/job-2") {
        return response({ state: "ready", phase: "ready", target: environment("b"), fingerprint: "fp-b" });
      }
      if (url === "/sim-environment/switch/job-3") {
        return response(
          returnReady
            ? { state: "ready", phase: "ready", target: environment("a"), fingerprint: "fp-a" }
            : { state: "queued", phase: "queued", target: environment("a") },
        );
      }
      throw new Error(`unexpected request ${url}`);
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  await watcher.requestSwitch("b");
  activeCatalog = catalog("b", "fp-b");
  await watcher.poll();
  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  assert.equal(watcher.snapshot().status, "failed");
  assert.equal(watcher.snapshot().fallback?.id, "a");
  assert.deepEqual(requested, ["b"], "a passive render failure must not mutate the host automatically");

  await watcher.retry();
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  watcher.noteViewer({ fingerprint: "fp-b", ready: false, error: "invalid GLB" });
  assert.equal(watcher.snapshot().fallback?.id, "a", "retry must retain the original safe fallback");

  await watcher.returnToPrevious();
  assert.deepEqual(requested, ["b", "b", "a"]);
  assert.equal(watcher.snapshot().target?.id, "a");
  assert.equal(watcher.snapshot().active, true);

  activeCatalog = catalog("a", "fp-a");
  returnReady = true;
  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  assert.equal(watcher.snapshot().active, false);
});

await test("an explicit return after a request failure can complete as a same-environment no-op", async () => {
  const requested = [];
  const states = [];
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url, init = {}) => {
      if (url === "/sim-environments.json") return response(catalog("a", "fp-a"));
      if (url === "/sim-environment/switch" && init.method === "POST") {
        const id = JSON.parse(init.body).id;
        requested.push(id);
        if (id === "b") return response({ error: "target failed validation" }, 400);
        return response({ job_id: "job-return", state: "queued", target: environment("a") }, 202);
      }
      if (url === "/sim-environment/switch/job-return") {
        return response({ state: "ready", phase: "ready", target: environment("a"), fingerprint: "fp-a" });
      }
      throw new Error(`unexpected request ${url}`);
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
    onState: (state) => states.push(state),
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  await watcher.requestSwitch("b");
  assert.equal(watcher.snapshot().status, "failed");
  assert.equal(watcher.snapshot().fallback?.id, "a");

  await watcher.returnToPrevious();
  assert.deepEqual(requested, ["b", "a"]);
  assert.equal(watcher.snapshot().active, false);
  assert.equal(states.at(-1).status, "complete");
  assert.equal(states.at(-1).target?.id, "a");
});

await test("a ready A-to-B job cannot unlock against the old A world", async () => {
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url, init = {}) => {
      if (url === "/sim-environments.json") return response(catalog("a", "fp-a"));
      if (url === "/sim-environment/switch" && init.method === "POST") {
        return response({ job_id: "job-b", state: "queued", target: environment("b") }, 202);
      }
      if (url === "/sim-environment/switch/job-b") {
        return response({ state: "ready", phase: "ready", target: environment("b"), fingerprint: "fp-b" });
      }
      throw new Error(`unexpected request ${url}`);
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  await watcher.requestSwitch("b");
  watcher.noteViewer({ fingerprint: "fp-b", ready: true });
  assert.equal(watcher.snapshot().active, true, "the old connected A world cannot satisfy target B");

  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  assert.equal(watcher.snapshot().active, false);
});

await test("a late viewer identity mismatch interlocks immediately without another catalog poll", async () => {
  let catalogRequests = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url) => {
      assert.equal(url, "/sim-environments.json");
      catalogRequests += 1;
      return response(catalog("b", "fp-b"));
    },
    loadedFingerprintFn: () => null,
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.poll();
  assert.equal(watcher.snapshot().active, false);
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  assert.equal(catalogRequests, 1, "the event itself must interlock before the next catalog poll");
  assert.equal(watcher.snapshot().active, true);
  assert.equal(watcher.snapshot().target?.id, "b");

  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  assert.equal(watcher.snapshot().active, true, "physics alone cannot unlock a mismatched viewer");
  watcher.noteViewer({ fingerprint: "fp-b", ready: true });
  assert.equal(watcher.snapshot().active, false);
});

await test("only a connected mismatched world interlocks immediately", async () => {
  let catalogRequests = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url) => {
      assert.equal(url, "/sim-environments.json");
      catalogRequests += 1;
      return response(catalog("b", "fp-b"));
    },
    loadedFingerprintFn: () => null,
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-b", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: false });
  assert.equal(watcher.snapshot().active, false, "a stale socket's disconnect must not start a transition");

  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  assert.equal(catalogRequests, 1, "the connected-world event must interlock before the next catalog poll");
  assert.equal(watcher.snapshot().active, true);
  assert.equal(watcher.snapshot().target?.id, "b");

  watcher.noteWorld({ id: "b", fingerprint: "fp-b", connected: true });
  assert.equal(watcher.snapshot().active, false);
});

await test("an external catalog rollback follows the prior fingerprint instead of ignoring it", async () => {
  let activeCatalog = catalog("a", "fp-a");
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async (url) => {
      assert.equal(url, "/sim-environments.json");
      return response(activeCatalog);
    },
    loadedFingerprintFn: () => null,
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.poll();
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  watcher.noteWorld({ id: "a", fingerprint: "fp-a", connected: true });
  activeCatalog = catalog("b", "fp-b");
  await watcher.poll();
  assert.equal(watcher.snapshot().target?.id, "b");

  activeCatalog = catalog("a", "fp-a");
  await watcher.poll();
  assert.equal(watcher.snapshot().target?.id, "a");
  assert.equal(watcher.snapshot().active, true);
  watcher.noteViewer({ fingerprint: "fp-a", ready: true });
  assert.equal(watcher.snapshot().active, false);
});

await test("the config gate does not start environment polling on a real robot", async () => {
  let requests = 0;
  let scheduled = 0;
  const watcher = await startSimEnvironmentWatcher({
    fetchFn: async () => {
      requests += 1;
      return response({ simControls: false });
    },
    setIntervalFn: () => {
      scheduled += 1;
      return 1;
    },
  });
  assert.equal(watcher, null);
  assert.equal(requests, 1);
  assert.equal(scheduled, 0);
});

console.log(`\n${passed} passed`);
