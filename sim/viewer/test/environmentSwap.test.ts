// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import test from "node:test";
import {
  claimEnvironmentSwapKey,
  deferEnvironmentSwapRetry,
  EnvironmentSwapCoordinator,
  prepareEnvironmentSwapScene,
} from "../src/environmentSwap.ts";
import { LoadQueue } from "../src/loadQueue.ts";

test("a stale scene build is discarded and cannot replace the latest scene", async () => {
  const swap = new EnvironmentSwapCoordinator<string>();
  let finishFirst!: (value: string) => void;
  const firstBuilt = new Promise<string>((resolve) => {
    finishFirst = resolve;
  });
  const activated: string[] = [];
  const discarded: string[] = [];
  const activate = (candidate: string) => activated.push(candidate);
  const discard = (candidate: string) => discarded.push(candidate);

  const first = swap.replace(() => firstBuilt, activate, discard);
  const second = swap.replace(async () => "second", activate, discard);
  assert.equal(await second, true);
  finishFirst("first");
  assert.equal(await first, false);
  assert.deepEqual(activated, ["second"]);
  assert.deepEqual(discarded, ["first"]);
});

test("a synchronous ready-state echo cannot request the same scene recursively", () => {
  let desired = "";
  let announcements = 0;
  const request = (key: string) => {
    const claimed = claimEnvironmentSwapKey(desired, key);
    if (claimed === null) return;
    desired = claimed;
    announcements += 1;
    request(key); // Models viewer-ready -> watcher-state -> stage synchronously.
  };

  request("7:intersection-fingerprint");
  assert.equal(announcements, 1);
});

test("a synchronous failure echo cannot bypass the scene retry backoff", () => {
  const requestedKey = "7:town-fingerprint";
  const claim = { key: requestedKey };
  let scheduled: (() => void) | null = null;
  let requests = 0;

  const requestFromSwitchState = () => {
    const claimed = claimEnvironmentSwapKey(claim.key, requestedKey);
    if (claimed === null) return;
    claim.key = claimed;
    requests += 1;
  };
  const timer = deferEnvironmentSwapRetry(
    claim,
    requestedKey,
    requestFromSwitchState,
    (callback, delayMs) => {
      assert.equal(delayMs, 1000);
      scheduled = callback;
      return 42;
    },
    () => true,
    requestFromSwitchState,
  );

  assert.equal(timer, 42);
  assert.equal(requests, 0);
  assert.ok(scheduled);
  scheduled();
  assert.equal(requests, 1);

  const superseded = { key: requestedKey };
  let staleTimer: (() => void) | null = null;
  let staleRetries = 0;
  deferEnvironmentSwapRetry(
    superseded,
    requestedKey,
    () => undefined,
    (callback) => {
      staleTimer = callback;
      return 43;
    },
    () => true,
    () => {
      staleRetries += 1;
    },
  );
  superseded.key = "8:apartment-fingerprint";
  assert.ok(staleTimer);
  staleTimer();
  assert.equal(superseded.key, "8:apartment-fingerprint");
  assert.equal(staleRetries, 0);
});

test("replacement restores responsive presentation after its first world tick", () => {
  type Mode = "free" | "top";
  const calls: string[] = [];
  const state = { inset: 0, mode: "free" as Mode };
  const scene = {
    setRenderSize(width: number, height: number, pixelRatio: number) {
      calls.push(`size:${width}x${height}@${pixelRatio}`);
    },
    setSafeInsets({ right = 0 }: { right?: number }) {
      state.inset = right;
      calls.push(`inset:${right}`);
    },
    setCameraMode(mode: Mode) {
      state.mode = mode;
      calls.push(`mode:${mode}`);
    },
    setView(view: "orbit") {
      calls.push(`view:${view}`);
    },
    render() {
      calls.push("render");
    },
  };

  prepareEnvironmentSwapScene(
    scene,
    () => {
      calls.push("tick");
      state.mode = "free"; // spawnAt() resets the camera during the first tick
    },
    {
      width: 1280,
      height: 720,
      pixelRatio: 2,
      safeInsetRight: 320,
      cameraMode: "top",
      view: "orbit",
    },
  );

  assert.equal(state.mode, "top");
  assert.equal(state.inset, 320);
  assert.deepEqual(calls, ["size:1280x720@2", "tick", "inset:320", "mode:top", "view:orbit", "render"]);
});

test("a cancelled scene queue rejects active, pending, and future room loads", async () => {
  const successful = new LoadQueue(1);
  assert.equal(await successful.add(async () => "loaded"), "loaded");

  const progress: number[] = [];
  const queue = new LoadQueue(1, ({ loaded }) => progress.push(loaded));
  let finishActive!: (value: string) => void;
  let reportActive!: (loaded: number, total: number) => void;
  let appliedAfterLoad = false;
  const active = queue
    .add(
      (report) =>
        new Promise<string>((resolve) => {
          reportActive = report;
          finishActive = resolve;
        }),
    )
    .then(() => {
      appliedAfterLoad = true;
    });
  const pending = queue.add(async () => "pending");

  // Let the active job enter its browser-request equivalent before teardown.
  await Promise.resolve();
  reportActive(10, 100);
  queue.cancel();
  assert.equal(queue.cancelled, true);
  await assert.rejects(active, /load queue cancelled/);
  await assert.rejects(pending, /load queue cancelled/);
  await assert.rejects(queue.add(async () => "future"), /load queue cancelled/);

  // A loader callback can still arrive because GLTFLoader does not expose an
  // AbortController. It must not resolve the consumer or update stage progress.
  reportActive(100, 100);
  finishActive("active");
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(appliedAfterLoad, false);
  assert.deepEqual(progress, [10]);
});
