// @ts-check

const EDIT_FRAME_SEPARATOR = "#keepout-map=";

/** @typedef {{ width: number, height: number, resolution: number, originX: number, originY: number, originYaw: number, frameId: string, mapHash: string, data: number[] }} KeepoutGrid */

/** @param {any} orientation */
function yawOf(orientation) {
  const q = orientation ?? {};
  const x = Number(q.x ?? 0);
  const y = Number(q.y ?? 0);
  const z = Number(q.z ?? 0);
  const w = Number(q.w ?? 1);
  return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
}

/** @param {string} value */
function editFrame(value) {
  const index = value.lastIndexOf(EDIT_FRAME_SEPARATOR);
  if (index <= 0) return null;
  const frameId = value.slice(0, index);
  const mapHash = value.slice(index + EDIT_FRAME_SEPARATOR.length);
  if (!/^[0-9a-f]{64}$/.test(mapHash) || frameId.includes(EDIT_FRAME_SEPARATOR)) return null;
  return { frameId, mapHash };
}

const SHA256_INITIAL = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];
const SHA256_ROUND = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/** SHA-256 for browsers where Web Crypto is unavailable (notably robot HTTP pages).
 * @param {Uint8Array} bytes @returns {string} */
export function sha256Hex(bytes) {
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const bitLength = bytes.length * 8;
  const tail = new DataView(padded.buffer);
  tail.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
  tail.setUint32(paddedLength - 4, bitLength >>> 0, false);

  const hash = SHA256_INITIAL.slice();
  const words = new Uint32Array(64);
  const rotateRight = (/** @type {number} */ value, /** @type {number} */ count) =>
    (value >>> count) | (value << (32 - count));
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index++) words[index] = tail.getUint32(offset + index * 4, false);
    for (let index = 16; index < 64; index++) {
      const s0 = rotateRight(words[index - 15], 7) ^ rotateRight(words[index - 15], 18) ^ (words[index - 15] >>> 3);
      const s1 = rotateRight(words[index - 2], 17) ^ rotateRight(words[index - 2], 19) ^ (words[index - 2] >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index++) {
      const s1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choice + SHA256_ROUND[index] + words[index]) >>> 0;
      const s0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + majority) >>> 0;
      [a, b, c, d, e, f, g, h] = [(temp1 + temp2) >>> 0, a, b, c, (d + temp1) >>> 0, e, f, g];
    }
    [a, b, c, d, e, f, g, h].forEach((value, index) => (hash[index] = (hash[index] + value) >>> 0));
  }
  return hash.map((value) => value.toString(16).padStart(8, "0")).join("");
}

/** @param {KeepoutGrid} grid @param {number} x @param {number} y */
function worldToGrid(grid, x, y) {
  const dx = x - grid.originX;
  const dy = y - grid.originY;
  const c = Math.cos(grid.originYaw);
  const s = Math.sin(grid.originYaw);
  return { x: c * dx + s * dy, y: -s * dx + c * dy };
}

/** @param {any} msg @returns {KeepoutGrid | null} */
export function keepoutGridFromMessage(msg) {
  const width = msg?.info?.width | 0;
  const height = msg?.info?.height | 0;
  const resolution = Number(msg?.info?.resolution);
  const frame = editFrame(msg?.header?.frame_id || "");
  if (width <= 0 || height <= 0 || !(resolution > 0) || !frame || !Array.isArray(msg?.data) || msg.data.length < width * height) return null;
  return {
    width,
    height,
    resolution,
    originX: Number(msg.info.origin?.position?.x ?? 0),
    originY: Number(msg.info.origin?.position?.y ?? 0),
    originYaw: yawOf(msg.info.origin?.orientation),
    frameId: frame.frameId,
    mapHash: frame.mapHash,
    data: msg.data.slice(0, width * height).map((/** @type {number} */ value) => (value >= 50 ? 100 : 0)),
  };
}

/** Compute the server-compatible identity of a localization map.
 * @param {any} msg @returns {Promise<string | null>} */
export async function mapFingerprintFromMessage(msg) {
  const width = msg?.info?.width | 0;
  const height = msg?.info?.height | 0;
  const resolution = Number(msg?.info?.resolution);
  const originX = Number(msg?.info?.origin?.position?.x ?? 0);
  const originY = Number(msg?.info?.origin?.position?.y ?? 0);
  const originYaw = yawOf(msg?.info?.origin?.orientation);
  const frameId = msg?.header?.frame_id || "";
  const cells = width * height;
  if (
    width <= 0 ||
    height <= 0 ||
    !(resolution > 0) ||
    !Number.isFinite(originX) ||
    !Number.isFinite(originY) ||
    !Number.isFinite(originYaw) ||
    !frameId ||
    !Array.isArray(msg?.data) ||
    msg.data.length < cells
  ) {
    return null;
  }

  // Keep this byte-for-byte equal to mars_nav.keepout_mask.map_fingerprint:
  // little-endian <IIdddd>, UTF-8 frame id, then occupancy values offset by 1.
  const frame = new TextEncoder().encode(frameId);
  const encoded = new Uint8Array(40 + frame.length + cells);
  const view = new DataView(encoded.buffer);
  view.setUint32(0, width, true);
  view.setUint32(4, height, true);
  view.setFloat64(8, resolution, true);
  view.setFloat64(16, originX, true);
  view.setFloat64(24, originY, true);
  view.setFloat64(32, originYaw, true);
  encoded.set(frame, 40);
  for (let index = 0; index < cells; index++) encoded[40 + frame.length + index] = (Math.trunc(Number(msg.data[index])) + 1) & 0xff;

  if (!globalThis.crypto?.subtle) return sha256Hex(encoded);
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", encoded));
  return Array.from(digest, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** @param {KeepoutGrid | null} grid @param {string | null} mapHash */
export function keepoutGridForMap(grid, mapHash) {
  return grid && mapHash && grid.mapHash === mapHash ? grid : null;
}

/** Stable identity for one loaded /map instance; equal map contents can have different epochs.
 * @param {any} msg @returns {string | null} */
export function mapEpochFromMessage(msg) {
  const key = (/** @type {any} */ stamp) => {
    const sec = Number(stamp?.sec);
    const nanosec = Number(stamp?.nanosec);
    return Number.isFinite(sec) && Number.isFinite(nanosec) && (sec !== 0 || nanosec !== 0) ? `${sec}:${nanosec}` : null;
  };
  return key(msg?.info?.map_load_time) ?? key(msg?.header?.stamp);
}

/** Decide whether a received /map fingerprint belongs to the selected-map epoch.
 * @param {string} candidate @param {string | null} selected
 * @param {boolean} selectionPending @param {boolean} arrivedAfterSelection
 * @param {string | null} candidateEpoch @param {string | null} selectedEpoch */
export function shouldActivateMapFingerprint(
  candidate,
  selected,
  selectionPending,
  arrivedAfterSelection,
  candidateEpoch = null,
  selectedEpoch = null,
) {
  if (!selected) return true;
  if (!selectionPending) return candidate === selected;
  return candidate !== selected || (!!candidateEpoch && !!selectedEpoch && candidateEpoch !== selectedEpoch) || arrivedAfterSelection;
}

/** A new robot can publish /map before its current-map string catches up.
 * @param {string | null} candidate @param {string | null} selected @param {string | null} active
 * @param {boolean} robotSelectionPending */
export function isRobotSelectionCatchup(candidate, selected, active, robotSelectionPending) {
  return !!robotSelectionPending && !!candidate && candidate === selected && candidate === active;
}

/** @param {KeepoutGrid | null} grid @param {{ mapHash: string, data: number[] } | null} expected */
export function keepoutUpdateMatches(grid, expected) {
  return (
    !!grid &&
    !!expected &&
    grid.mapHash === expected.mapHash &&
    grid.data.length === expected.data.length &&
    grid.data.every((value, index) => value === expected.data[index])
  );
}

/** @param {KeepoutGrid} grid @param {number} x @param {number} y */
export function isKeepout(grid, x, y) {
  const local = worldToGrid(grid, x, y);
  const col = Math.floor(local.x / grid.resolution);
  const row = Math.floor(local.y / grid.resolution);
  if (col < 0 || row < 0 || col >= grid.width || row >= grid.height) return false;
  return grid.data[row * grid.width + col] >= 50;
}

/** Paint a round brush along a world-coordinate segment. Returns whether any cell changed.
 * @param {KeepoutGrid} grid @param {number} x0 @param {number} y0 @param {number} x1 @param {number} y1
 * @param {number} radiusM @param {boolean} blocked */
export function paintKeepout(grid, x0, y0, x1, y1, radiusM, blocked) {
  const length = Math.hypot(x1 - x0, y1 - y0);
  const steps = Math.max(1, Math.ceil(length / Math.max(grid.resolution * 0.5, radiusM * 0.35)));
  const radiusCells = Math.max(0, Math.ceil(radiusM / grid.resolution));
  const value = blocked ? 100 : 0;
  let changed = false;
  for (let step = 0; step <= steps; step++) {
    const t = step / steps;
    const local = worldToGrid(grid, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t);
    const cx = Math.floor(local.x / grid.resolution);
    const cy = Math.floor(local.y / grid.resolution);
    for (let dy = -radiusCells; dy <= radiusCells; dy++) {
      for (let dx = -radiusCells; dx <= radiusCells; dx++) {
        if (dx * dx + dy * dy > radiusCells * radiusCells) continue;
        const col = cx + dx;
        const row = cy + dy;
        if (col < 0 || row < 0 || col >= grid.width || row >= grid.height) continue;
        const index = row * grid.width + col;
        if (grid.data[index] === value) continue;
        grid.data[index] = value;
        changed = true;
      }
    }
  }
  return changed;
}

/** @param {KeepoutGrid} grid */
export function keepoutMessage(grid) {
  if (!/^[0-9a-f]{64}$/.test(grid.mapHash)) throw new Error("keepout grid is not bound to an active map");
  const halfYaw = grid.originYaw / 2;
  return {
    header: { stamp: { sec: 0, nanosec: 0 }, frame_id: `${grid.frameId}${EDIT_FRAME_SEPARATOR}${grid.mapHash}` },
    info: {
      map_load_time: { sec: 0, nanosec: 0 },
      resolution: grid.resolution,
      width: grid.width,
      height: grid.height,
      origin: {
        position: { x: grid.originX, y: grid.originY, z: 0 },
        orientation: { x: 0, y: 0, z: Math.sin(halfYaw), w: Math.cos(halfYaw) },
      },
    },
    data: grid.data,
  };
}
