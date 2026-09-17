// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

/** The camera panel's module graph (hand tracker, kinematics, MediaPipe loader)
 * is fetched only when a page actually mounts it.
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {import("./panel.js").HandControlPanelOptions} [opts]
 * @returns {Promise<{ el: HTMLElement, destroy: () => void }>} */
export const loadHandControlPanel = (parent, ros, opts) =>
  import("./panel.js").then((m) => m.createHandControlPanel(parent, ros, opts));
