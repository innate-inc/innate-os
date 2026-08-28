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
    msg.data.length < cells ||
    !globalThis.crypto?.subtle
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

  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", encoded));
  return Array.from(digest, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** @param {KeepoutGrid | null} grid @param {string | null} mapHash */
export function keepoutGridForMap(grid, mapHash) {
  return grid && mapHash && grid.mapHash === mapHash ? grid : null;
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
