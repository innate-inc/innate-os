// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import test from "node:test";

import {
  environmentAssetUrl,
  isValidManifestRoom,
  resolveEnvironmentSource,
} from "../src/environmentSource.ts";

function response(status: number, payload: unknown = null): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
  } as Response;
}

test("a transient descriptor failure resolves the active pack instead of legacy apartment", async () => {
  const replies: Array<Response | Error> = [
    new Error("proxy restarting"),
    response(200, { fingerprint: "pack-fingerprint", viewer: { type: "split-glb" } }),
  ];
  let delays = 0;
  const source = await resolveEnvironmentSource(
    (async () => {
      const next = replies.shift();
      if (next instanceof Error) throw next;
      assert.ok(next);
      return next;
    }) as typeof fetch,
    async () => {
      delays += 1;
    },
  );

  assert.equal(delays, 1);
  assert.equal(source.mode, "split-glb");
  assert.equal(source.fingerprint, "pack-fingerprint");
  assert.equal(
    environmentAssetUrl(`${source.roomBaseUrl}kitchen.glb`, source.fingerprint),
    "/sim-environment/rooms/kitchen.glb?fingerprint=pack-fingerprint",
  );
});

test("only a persistent 404 selects legacy routes; a later probe can recover", async () => {
  const noDescriptor = (async () => response(404)) as typeof fetch;
  const legacy = await resolveEnvironmentSource(noDescriptor, async () => {});
  assert.equal(legacy.fingerprint, undefined);
  assert.equal(legacy.manifestUrl, "/models/apartment/manifest.json");

  const active = await resolveEnvironmentSource(
    (async () => response(200, { fingerprint: "gallery", viewer: { type: "glb" } })) as typeof fetch,
    async () => {},
  );
  assert.equal(active.fingerprint, "gallery");
  assert.equal(environmentAssetUrl(active.sceneUrl!, active.fingerprint), "/sim-environment/scene.glb?fingerprint=gallery");
});

test("progressive room validation requires the name used by priority sorting", () => {
  const base = { file: "room.glb", bbox: { min: [0, 0, 0], max: [1, 1, 1] } };
  assert.equal(isValidManifestRoom(base), false);
  assert.equal(isValidManifestRoom({ ...base, name: 17 }), false);
  assert.equal(isValidManifestRoom({ ...base, name: "Kitchen" }), true);
});
