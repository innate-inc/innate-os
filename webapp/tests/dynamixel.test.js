// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Protocol-layer tests for js/dynamixel.js — zero dependencies, plain node:
//   node tests/dynamixel.test.js
// The CRC is anchored against the Read-instruction example published in the
// Robotis Protocol 2.0 e-manual, so these checks are not merely self-consistent.

import assert from "node:assert/strict";
import {
  crc16,
  buildPacket,
  buildSyncRead,
  createStatusParser,
  DynamixelLeader,
  LEADER_SERVO_IDS,
} from "../js/dynamixel.js";

let passed = 0;
/** @param {string} name @param {() => void | Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/**
 * Build a status packet the way a servo would: the present block the reader
 * asks for, Present Current (int16) / Velocity (int32) / Position (int32)
 * contiguous from address 126.
 * @param {number} id @param {number} position @param {number} [current]
 */
function statusPacket(id, position, current = 0) {
  const params = new Uint8Array(10);
  const view = new DataView(params.buffer);
  view.setInt16(0, current, true);
  view.setInt32(2, 0, true); // present velocity — read past, not used
  view.setInt32(6, position, true);
  const length = params.length + 4; // error byte + instruction + crc16
  const body = [0xff, 0xff, 0xfd, 0x00, id, length & 0xff, (length >> 8) & 0xff, 0x55, 0x00, ...params];
  const crc = crc16(body);
  return new Uint8Array([...body, crc & 0xff, crc >> 8]);
}

await test("CRC matches the e-manual Read example (ID1, addr 132, len 4)", () => {
  // Documented instruction packet: FF FF FD 00 01 07 00 02 84 00 04 00 1D 15
  const withoutCrc = [0xff, 0xff, 0xfd, 0x00, 0x01, 0x07, 0x00, 0x02, 0x84, 0x00, 0x04, 0x00];
  assert.equal(crc16(withoutCrc), 0x151d);
  const built = buildPacket(0x01, 0x02, [0x84, 0x00, 0x04, 0x00]);
  assert.deepEqual([...built], [...withoutCrc, 0x1d, 0x15]);
});

await test("SyncRead packet has broadcast id, right length and ids", () => {
  const pkt = buildSyncRead([1, 2, 3, 4, 5, 6], 132, 4);
  assert.equal(pkt[4], 0xfe);
  assert.equal(pkt[5] | (pkt[6] << 8), 4 + 6 + 3); // params + instr + crc
  assert.equal(pkt[7], 0x82);
  assert.deepEqual([...pkt.subarray(8, 12)], [0x84, 0x00, 0x04, 0x00]);
  assert.deepEqual([...pkt.subarray(12, 18)], [1, 2, 3, 4, 5, 6]);
  const crc = pkt[pkt.length - 2] | (pkt[pkt.length - 1] << 8);
  assert.equal(crc, crc16(pkt.subarray(0, pkt.length - 2)));
});

await test("parser survives torn chunks, garbage, and bad CRC", () => {
  /** @type {{id: number, pos: number}[]} */
  const seen = [];
  const parse = createStatusParser((p) => {
    const view = new DataView(p.params.buffer, p.params.byteOffset, 10);
    seen.push({ id: p.id, pos: view.getInt32(6, true) });
  });

  const a = statusPacket(1, 2048);
  const b = statusPacket(2, -512);
  const corrupt = statusPacket(3, 100);
  corrupt[corrupt.length - 1] ^= 0xff; // break the CRC

  const stream = new Uint8Array([0x00, 0xff, 0xff, ...a, ...corrupt, ...b, 0xff, 0xff]);
  // Feed one byte at a time — the cruellest chunking serial can produce.
  for (const byte of stream) parse(new Uint8Array([byte]));

  assert.deepEqual(seen, [
    { id: 1, pos: 2048 },
    { id: 2, pos: -512 },
  ]);
});

await test("DynamixelLeader completes rounds against a mock port", async () => {
  /** Mock SerialPort: replies to every sync read with six status packets. */
  let pulls = 0;
  /** @type {ReadableStreamDefaultController<Uint8Array>} */
  let readCtl;
  const mockPort = /** @type {any} */ ({
    readable: new ReadableStream({ start: (c) => void (readCtl = c) }),
    writable: new WritableStream({
      write(chunk) {
        assert.equal(chunk[7], 0x82, "loop must send SyncRead");
        pulls += 1;
        for (const id of LEADER_SERVO_IDS) {
          readCtl.enqueue(statusPacket(id, id * 1000 + pulls, id * 10));
        }
      },
    }),
    open: async () => {},
    close: async () => {
      readCtl.close();
    },
    getInfo: () => ({}),
  });

  const leader = new DynamixelLeader();
  /** @type {LeaderArmState[]} */
  const states = [];
  leader.onChange((s) => states.push(s));
  await leader.open(mockPort);
  await new Promise((r) => setTimeout(r, 120));
  await leader.close();

  const withPositions = states.filter((s) => s.positions);
  assert.ok(withPositions.length >= 2, `expected ≥2 rounds, got ${withPositions.length}`);
  const last = withPositions[withPositions.length - 1].positions;
  assert.equal(last.length, 6);
  assert.deepEqual(
    last.map((p) => Math.floor(p / 1000)),
    [1, 2, 3, 4, 5, 6],
    "positions ordered by servo id",
  );
  assert.deepEqual(
    withPositions[withPositions.length - 1].currents,
    LEADER_SERVO_IDS.map((id) => id * 10),
    "present current read alongside position, ordered by servo id",
  );
  assert.ok(withPositions[withPositions.length - 1].rate > 0, "rate tracked");
  const final = states[states.length - 1];
  assert.equal(final.connected, false);
});

console.log(`\n${passed} tests passed`);
