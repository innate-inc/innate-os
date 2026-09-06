// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure map-goal validation shared by the canvas widget and its Node test.

/**
 * Explain why a world point is not known-free on an occupancy grid.
 * @param {{ width: number, height: number, resolution: number, originX: number, originY: number, originYaw?: number } | null} grid
 * @param {ArrayLike<number> | null} cells
 * @param {number} x
 * @param {number} y
 * @param {number} occupiedThreshold
 * @returns {string | null}
 */
export function goalCellError(grid, cells, x, y, occupiedThreshold) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return "Choose a valid destination";
  if (!grid || !cells || grid.width <= 0 || grid.height <= 0 || grid.resolution <= 0) return "No navigation map is available";

  const dx = x - grid.originX;
  const dy = y - grid.originY;
  const yaw = Number(grid.originYaw ?? 0);
  const c = Math.cos(yaw);
  const s = Math.sin(yaw);
  const col = Math.floor((c * dx + s * dy) / grid.resolution);
  const row = Math.floor((-s * dx + c * dy) / grid.resolution);
  if (col < 0 || col >= grid.width || row < 0 || row >= grid.height) return "Choose a destination inside the map";

  const value = Number(cells[row * grid.width + col]);
  if (!Number.isFinite(value) || value < 0) return "Choose a destination on explored floor";
  if (value >= occupiedThreshold) return "Choose a destination on free floor";
  return null;
}
