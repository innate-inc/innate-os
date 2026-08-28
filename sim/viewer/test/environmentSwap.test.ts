// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import test from "node:test";
import { claimEnvironmentSwapKey, EnvironmentSwapCoordinator } from "../src/environmentSwap.ts";
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

test("a cancelled scene queue rejects pending and future room loads", async () => {
  const queue = new LoadQueue(1);
  let finishActive!: (value: string) => void;
  const active = queue.add(() => new Promise<string>((resolve) => (finishActive = resolve)));
  const pending = queue.add(async () => "pending");

  queue.cancel();
  await assert.rejects(pending, /load queue cancelled/);
  await assert.rejects(queue.add(async () => "future"), /load queue cancelled/);
  finishActive("active");
  assert.equal(await active, "active");
});
