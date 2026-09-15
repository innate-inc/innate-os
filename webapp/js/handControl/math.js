// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

export const clamp = (/** @type {number} */ v, lo = -1, hi = 1) => Math.max(lo, Math.min(hi, v));
export const median = (/** @type {number[]} */ v) => [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)];
