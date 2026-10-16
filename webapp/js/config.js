// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shared /config.json loader. The file is fetched once per page load and the
// promise reused, so the router, shell, robot session, and the active section
// don't each hit the proxy — it was ~5 requests a load before this. Still
// no-store so a fresh load reflects env-driven flags, just deduped in-page.

/** @typedef {{simControls?: boolean, micSource?: string}} AppConfig */

/** @type {Promise<AppConfig> | undefined} */
let _config;

/** @returns {Promise<AppConfig>} */
export function getConfig() {
  return (_config ??= fetch("/config.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({})));
}
