// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Dynamixel Protocol 2.0 over WebSerial — reads the leader arm's six servos.
//
// The leader arm is a chain of Dynamixel X-series servos (ids 1–6) behind a
// USB-serial adapter at 1 Mbaud. Each round is one SyncRead of the 4-byte
// Present Position register (132); every servo answers with a status packet.
// Positions are raw signed ticks (4096/rev, 2048 = center) — consumers pass
// them through untouched, exactly like the mobile app does.
//
// Protocol framing (header FF FF FD 00, little-endian length, CRC-16 with
// poly 0x8005) follows the Robotis reference implementation. Byte stuffing
// is deliberately not implemented: our request bytes can never contain the
// stuffing pattern, and a (practically impossible) stuffed status packet
// fails CRC and costs one dropped round, nothing more.

const HEADER = [0xff, 0xff, 0xfd, 0x00];
const BROADCAST_ID = 0xfe;
const INSTR_WRITE = 0x03;
const INSTR_SYNC_READ = 0x82;
const INSTR_SYNC_WRITE = 0x83;
const INSTR_STATUS = 0x55;

// Control table (X-series), mirroring mars_control/dynamixel.py — the robot-side
// driver for the same servo family.
export const ADDR_OPERATING_MODE = 11; // EEPROM: rejects writes while torqued
export const ADDR_TORQUE_ENABLE = 64;
export const ADDR_GOAL_CURRENT = 102;
export const ADDR_GOAL_POSITION = 116;
export const OPERATING_MODE_CURRENT_POSITION = 5;

// Present Current(126,2), Velocity(128,4) and Position(132,4) are contiguous, so
// one 10-byte read carries all three — the guard needs draw and angle together,
// and splitting them would double the traffic on a half-duplex bus.
const PRESENT_BLOCK_ADDR = 126;
const PRESENT_BLOCK_LEN = 10;

export const LEADER_BAUD = 1_000_000;
export const LEADER_SERVO_IDS = [1, 2, 3, 4, 5, 6];

const ROUND_TIMEOUT_MS = 25;
const PARSE_BUFFER_CAP = 4096;

// CRC-16 (poly 0x8005, MSB-first, init 0), as published in the Robotis
// Protocol 2.0 reference. Table generated instead of pasted.
const CRC_TABLE = (() => {
  const table = new Uint16Array(256);
  for (let i = 0; i < 256; i++) {
    let value = i << 8;
    for (let j = 0; j < 8; j++) {
      value = value & 0x8000 ? ((value << 1) ^ 0x8005) & 0xffff : (value << 1) & 0xffff;
    }
    table[i] = value;
  }
  return table;
})();

/**
 * @param {Uint8Array | number[]} bytes
 * @returns {number} CRC-16 over the bytes.
 */
export function crc16(bytes) {
  let crc = 0;
  for (const b of bytes) {
    crc = ((crc << 8) ^ CRC_TABLE[((crc >> 8) ^ b) & 0xff]) & 0xffff;
  }
  return crc;
}

/**
 * Build a Protocol 2.0 instruction packet.
 * @param {number} id
 * @param {number} instruction
 * @param {number[]} params
 * @returns {Uint8Array}
 */
export function buildPacket(id, instruction, params) {
  const length = params.length + 3; // instruction + crc16
  const packet = new Uint8Array(7 + 1 + params.length + 2); // header..len + instr + params + crc
  packet.set(HEADER, 0);
  packet[4] = id;
  packet[5] = length & 0xff;
  packet[6] = (length >> 8) & 0xff;
  packet[7] = instruction;
  packet.set(params, 8);
  const crc = crc16(packet.subarray(0, packet.length - 2));
  packet[packet.length - 2] = crc & 0xff;
  packet[packet.length - 1] = (crc >> 8) & 0xff;
  return packet;
}

/**
 * SyncRead of one register block across several servos.
 * @param {number[]} ids
 * @param {number} addr
 * @param {number} len
 * @returns {Uint8Array}
 */
export function buildSyncRead(ids, addr, len) {
  return buildPacket(BROADCAST_ID, INSTR_SYNC_READ, [
    addr & 0xff,
    (addr >> 8) & 0xff,
    len & 0xff,
    (len >> 8) & 0xff,
    ...ids,
  ]);
}

/**
 * Write one register block on a single servo.
 * @param {number} id
 * @param {number} addr
 * @param {number[]} bytes Little-endian value bytes.
 * @returns {Uint8Array}
 */
export function buildWrite(id, addr, bytes) {
  return buildPacket(id, INSTR_WRITE, [addr & 0xff, (addr >> 8) & 0xff, ...bytes]);
}

/**
 * Write the same register block on several servos in one packet, each with its
 * own value.
 * @param {number} addr
 * @param {number} len Bytes per servo.
 * @param {Map<number, number[]>} valuesById Little-endian value bytes per servo id.
 * @returns {Uint8Array}
 */
export function buildSyncWrite(addr, len, valuesById) {
  /** @type {number[]} */
  const params = [addr & 0xff, (addr >> 8) & 0xff, len & 0xff, (len >> 8) & 0xff];
  for (const [id, bytes] of valuesById) params.push(id, ...bytes);
  return buildPacket(BROADCAST_ID, INSTR_SYNC_WRITE, params);
}

/** @param {number} value @param {number} len @returns {number[]} Little-endian bytes. */
export function toBytes(value, len) {
  const bytes = [];
  for (let i = 0; i < len; i++) bytes.push((value >> (8 * i)) & 0xff);
  return bytes;
}

/**
 * @typedef {Object} StatusPacket
 * @property {number} id
 * @property {number} error Hardware/processing error byte (0 = ok).
 * @property {Uint8Array} params
 */

/**
 * Incremental Protocol 2.0 status-packet parser. Feed arbitrary serial
 * chunks; emits each CRC-valid status packet. Garbage and torn packets
 * resynchronize on the next header.
 * @param {(packet: StatusPacket) => void} onPacket
 * @returns {(chunk: Uint8Array) => void}
 */
export function createStatusParser(onPacket) {
  /** @type {Uint8Array} */
  let buf = new Uint8Array(0);

  return (chunk) => {
    const merged = new Uint8Array(buf.length + chunk.length);
    merged.set(buf, 0);
    merged.set(chunk, buf.length);
    buf = merged;

    let offset = 0;
    while (true) {
      // Hunt for the header.
      let start = -1;
      for (let i = offset; i + 3 < buf.length; i++) {
        if (buf[i] === 0xff && buf[i + 1] === 0xff && buf[i + 2] === 0xfd && buf[i + 3] === 0x00) {
          start = i;
          break;
        }
      }
      if (start === -1) {
        // Keep a potential partial header tail.
        offset = Math.max(offset, buf.length - 3);
        break;
      }
      if (start + 7 > buf.length) {
        offset = start;
        break; // need id + length
      }
      const length = buf[start + 5] | (buf[start + 6] << 8);
      const total = 7 + length;
      if (length < 3 || total > 512) {
        offset = start + 4; // implausible — resync past this header
        continue;
      }
      if (start + total > buf.length) {
        offset = start;
        break; // wait for the rest
      }

      const packet = buf.subarray(start, start + total);
      const crc = packet[total - 2] | (packet[total - 1] << 8);
      if (crc === crc16(packet.subarray(0, total - 2)) && packet[7] === INSTR_STATUS) {
        onPacket({
          id: packet[4],
          error: packet[8],
          params: packet.slice(9, total - 2),
        });
      }
      offset = start + total;
    }

    buf = buf.slice(offset);
    if (buf.length > PARSE_BUFFER_CAP) buf = new Uint8Array(0);
  };
}

/** @param {number} ms @returns {Promise<void>} */
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Continuous reader for the leader arm. Owns the serial port lifecycle and
 * emits one LeaderArmState per completed position round (~50–60 Hz when the
 * page is visible; background-tab timer throttling slows polling, never
 * correctness).
 */
export class DynamixelLeader {
  /** @type {SerialPort | null} */ #port = null;
  /** @type {WritableStreamDefaultWriter<Uint8Array> | null} */ #writer = null;
  /** @type {ReadableStreamDefaultReader<Uint8Array> | null} */ #reader = null;
  /** @type {Set<(state: LeaderArmState) => void>} */ #listeners = new Set();
  /** @type {LeaderArmState} */ #state = {
    connected: false,
    positions: null,
    currents: null,
    rate: 0,
    error: null,
  };
  /** @type {number[]} */ #ids;
  #running = false;
  #opening = false;
  /** @type {Map<number, { position: number, current: number }>} */ #round = new Map();
  /** @type {(() => void) | null} */ #roundComplete = null;
  /** @type {number[]} */ #emitTimes = [];
  // The bus is half-duplex: a packet sent while servos are answering the round's
  // SyncRead collides with their status packets. Writes therefore queue here and
  // the poll loop flushes them between rounds, never mid-round.
  /** @type {Uint8Array[]} */ #pending = [];
  /** @type {Set<number>} */ #torqued = new Set();

  /** @param {number[]} [ids] */
  constructor(ids = LEADER_SERVO_IDS) {
    this.#ids = ids;
  }

  /** @returns {LeaderArmState} */
  get state() {
    return this.#state;
  }

  /**
   * @param {(state: LeaderArmState) => void} cb Fires immediately, then on change.
   * @returns {() => void} unsubscribe
   */
  onChange(cb) {
    this.#listeners.add(cb);
    cb(this.#state);
    return () => this.#listeners.delete(cb);
  }

  /**
   * Open the port and start polling. Resolves once the loop is running;
   * rejects if the port can't be opened (e.g. held by another program).
   * @param {SerialPort} port
   */
  async open(port) {
    // Coalesce concurrent opens: `connected` only flips true after this resolves,
    // so the !connected guards at the call sites (button, serial 'connect' event,
    // getPorts() on mount) are TOCTOU — on a cold load with the arm already
    // plugged in, two can fire and the second's close() would tear down the
    // first's streams mid-open. First caller wins; the rest no-op.
    if (this.#opening) return;
    this.#opening = true;
    try {
      await this.close();
      await port.open({ baudRate: LEADER_BAUD, bufferSize: 4096 });
      if (!port.readable || !port.writable) {
        await port.close().catch(() => {});
        throw new Error("Serial port has no streams");
      }
      this.#port = port;
      this.#writer = port.writable.getWriter();
      this.#reader = port.readable.getReader();
      this.#running = true;
      this.#patch({ connected: true, error: null });
      void this.#pumpReads();
      void this.#pollLoop();
    } finally {
      this.#opening = false;
    }
  }

  /** Stop polling and release the port (it stays granted for next time). */
  async close() {
    this.#running = false;
    this.#roundComplete?.();
    // committed: the arm must never be left energized behind a closed port, so
    // this torque-off is not cancellable and not conditional on a clean loop.
    // The poll loop is already stopped; one round timeout is long enough for its
    // last SyncRead to have been answered, so this cannot collide with it.
    if (this.#writer && this.#torqued.size) {
      await sleep(ROUND_TIMEOUT_MS + 5);
      const off = buildSyncWrite(
        ADDR_TORQUE_ENABLE,
        1,
        new Map([...this.#torqued].map((id) => [id, [0]])),
      );
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          await this.#writer.write(off);
        } catch {
          break; // port already gone — the servos lost power with it
        }
      }
      this.#torqued.clear();
    }
    this.#pending.length = 0;
    const reader = this.#reader;
    const writer = this.#writer;
    const port = this.#port;
    this.#reader = null;
    this.#writer = null;
    this.#port = null;
    try {
      await reader?.cancel();
      reader?.releaseLock();
    } catch {
      // Port may already be gone (unplugged).
    }
    try {
      writer?.releaseLock();
    } catch {
      // Same.
    }
    await port?.close().catch(() => {});
    if (this.#state.connected) {
      this.#patch({ connected: false, positions: null, currents: null, rate: 0 });
    }
  }

  /** @returns {ReadonlySet<number>} Servo ids currently torqued by this driver. */
  get torqued() {
    return this.#torqued;
  }

  /**
   * Torque on/off. Queued, so it lands between rounds.
   * @param {number[]} ids
   * @param {boolean} on
   */
  writeTorque(ids, on) {
    if (!ids.length) return;
    this.#pending.push(buildSyncWrite(ADDR_TORQUE_ENABLE, 1, new Map(ids.map((id) => [id, [on ? 1 : 0]]))));
    for (const id of ids) {
      if (on) this.#torqued.add(id);
      else this.#torqued.delete(id);
    }
  }

  /**
   * Operating mode. EEPROM — the servo rejects this while torqued, so callers
   * must torque off first, and must not call it per round (write endurance).
   * @param {number[]} ids
   * @param {number} mode
   */
  writeOperatingMode(ids, mode) {
    if (!ids.length) return;
    this.#pending.push(buildSyncWrite(ADDR_OPERATING_MODE, 1, new Map(ids.map((id) => [id, [mode & 0xff]]))));
  }

  /** @param {Map<number, number>} byId Goal current in mA per servo id. */
  writeGoalCurrent(byId) {
    if (!byId.size) return;
    this.#pending.push(buildSyncWrite(ADDR_GOAL_CURRENT, 2, new Map([...byId].map(([id, mA]) => [id, toBytes(mA, 2)]))));
  }

  /** @param {Map<number, number>} byId Goal position in ticks per servo id. */
  writeGoalPosition(byId) {
    if (!byId.size) return;
    this.#pending.push(
      buildSyncWrite(ADDR_GOAL_POSITION, 4, new Map([...byId].map(([id, tick]) => [id, toBytes(tick, 4)]))),
    );
  }

  async #drainPending() {
    const writer = this.#writer;
    if (!writer) return;
    while (this.#pending.length) {
      const packet = /** @type {Uint8Array} */ (this.#pending.shift());
      await writer.write(packet);
    }
  }

  /** @param {string} message */
  #fail(message) {
    if (!this.#running) return;
    this.#patch({ error: message });
    void this.close();
  }

  async #pumpReads() {
    const reader = this.#reader;
    if (!reader) return;
    const parse = createStatusParser((packet) => {
      // Write acknowledgements come back as zero-parameter status packets; only
      // a full present-block answer is a round contribution.
      if (packet.params.length !== PRESENT_BLOCK_LEN || !this.#ids.includes(packet.id)) return;
      const view = new DataView(packet.params.buffer, packet.params.byteOffset, PRESENT_BLOCK_LEN);
      this.#round.set(packet.id, { current: view.getInt16(0, true), position: view.getInt32(6, true) });
      if (this.#round.size === this.#ids.length) this.#roundComplete?.();
    });
    try {
      while (this.#running) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) parse(value);
      }
    } catch (err) {
      this.#fail(err instanceof Error ? err.message : "Serial read failed");
    }
  }

  async #pollLoop() {
    const request = buildSyncRead(this.#ids, PRESENT_BLOCK_ADDR, PRESENT_BLOCK_LEN);
    while (this.#running && this.#writer) {
      this.#round.clear();
      try {
        await this.#drainPending();
        await this.#writer.write(request);
      } catch (err) {
        this.#fail(err instanceof Error ? err.message : "Serial write failed");
        return;
      }

      // Wait until every servo answered, or give the round up.
      await Promise.race([
        new Promise((resolve) => {
          this.#roundComplete = () => resolve(undefined);
        }),
        sleep(ROUND_TIMEOUT_MS),
      ]);
      this.#roundComplete = null;

      if (!this.#running) return;
      if (this.#round.size === this.#ids.length) {
        const positions = this.#ids.map((id) => this.#round.get(id)?.position ?? 0);
        const currents = this.#ids.map((id) => this.#round.get(id)?.current ?? 0);
        this.#trackRate();
        this.#patch({ positions, currents, rate: this.#emitTimes.length, error: null });
      }
      // Breather so a wedged bus can't busy-loop; sets the ~60 Hz ceiling.
      await sleep(2);
    }
  }

  #trackRate() {
    const now = performance.now();
    this.#emitTimes.push(now);
    this.#emitTimes = this.#emitTimes.filter((t) => now - t < 1000);
  }

  /** @param {Partial<LeaderArmState>} patch */
  #patch(patch) {
    this.#state = { ...this.#state, ...patch };
    for (const cb of [...this.#listeners]) {
      try {
        cb(this.#state);
      } catch (err) {
        console.error("[dynamixel] listener threw:", err);
      }
    }
  }
}
