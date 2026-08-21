// @ts-check

/** @typedef {{ width: number, height: number, resolution: number, originX: number, originY: number, frameId: string, data: number[] }} KeepoutGrid */

/** @param {any} msg @returns {KeepoutGrid | null} */
export function keepoutGridFromMessage(msg) {
  const width = msg?.info?.width | 0;
  const height = msg?.info?.height | 0;
  const resolution = Number(msg?.info?.resolution);
  if (width <= 0 || height <= 0 || !(resolution > 0) || !Array.isArray(msg?.data) || msg.data.length < width * height) return null;
  return {
    width,
    height,
    resolution,
    originX: Number(msg.info.origin?.position?.x ?? 0),
    originY: Number(msg.info.origin?.position?.y ?? 0),
    frameId: msg.header?.frame_id || "map",
    data: msg.data.slice(0, width * height).map((/** @type {number} */ value) => (value >= 50 ? 100 : 0)),
  };
}

/** @param {any} mapMsg @returns {KeepoutGrid | null} */
export function blankKeepoutGrid(mapMsg) {
  const parsed = keepoutGridFromMessage(mapMsg);
  if (!parsed) return null;
  parsed.data.fill(0);
  return parsed;
}

/** @param {KeepoutGrid} grid @param {number} x @param {number} y */
export function isKeepout(grid, x, y) {
  const col = Math.floor((x - grid.originX) / grid.resolution);
  const row = Math.floor((y - grid.originY) / grid.resolution);
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
    const cx = Math.floor((x0 + (x1 - x0) * t - grid.originX) / grid.resolution);
    const cy = Math.floor((y0 + (y1 - y0) * t - grid.originY) / grid.resolution);
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
  return {
    header: { stamp: { sec: 0, nanosec: 0 }, frame_id: grid.frameId },
    info: {
      map_load_time: { sec: 0, nanosec: 0 },
      resolution: grid.resolution,
      width: grid.width,
      height: grid.height,
      origin: {
        position: { x: grid.originX, y: grid.originY, z: 0 },
        orientation: { x: 0, y: 0, z: 0, w: 1 },
      },
    },
    data: grid.data,
  };
}
