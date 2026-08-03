// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Keep an already-open simulator tab aligned with the launch-selected world.
// The config gate keeps this off real robots; in simulation, a briefly missing
// descriptor is retried and forces a reload once activation completes.

const CONFIG_URL = "/config.json";
const MANIFEST_URL = "/sim-environment/manifest.json";
const DEFAULT_POLL_MS = 5000;

/**
 * @param {{
 *   fetchFn?: typeof fetch,
 *   reloadFn?: () => void,
 *   setIntervalFn?: typeof setInterval,
 *   clearIntervalFn?: typeof clearInterval,
 *   loadedFingerprintFn?: () => string | null,
 *   pollMs?: number,
 * }} options
 */
export function createSimEnvironmentWatcher(options = {}) {
  const fetchFn = options.fetchFn ?? globalThis.fetch.bind(globalThis);
  const reloadFn = options.reloadFn ?? (() => globalThis.location.reload());
  const setIntervalFn = options.setIntervalFn ?? globalThis.setInterval.bind(globalThis);
  const clearIntervalFn = options.clearIntervalFn ?? globalThis.clearInterval.bind(globalThis);
  const loadedFingerprintFn =
    options.loadedFingerprintFn ??
    (() => {
      const value = globalThis.__innateSimLoadedEnvironmentFingerprint;
      return typeof value === "string" && value ? value : null;
    });
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;

  let fingerprint = null;
  let timer = null;
  let stopped = false;
  let checking = false;
  let sawMissingDescriptor = false;

  function stop() {
    stopped = true;
    if (timer !== null) clearIntervalFn(timer);
    timer = null;
  }

  async function poll() {
    if (stopped || checking) return;
    checking = true;
    try {
      const response = await fetchFn(MANIFEST_URL, { cache: "no-store" });
      if (response.status === 404) {
        sawMissingDescriptor = true;
        return;
      }
      if (!response.ok) return; // Proxy restart/transient failure: try later.
      const payload = await response.json();
      const next = typeof payload?.fingerprint === "string" ? payload.fingerprint : "";
      if (!next) return;
      const loaded = loadedFingerprintFn();
      if (loaded !== null && loaded !== next) {
        stop();
        reloadFn();
        return;
      }
      if (fingerprint === null) {
        fingerprint = next;
        if (sawMissingDescriptor) {
          stop();
          reloadFn();
        }
      } else if (next !== fingerprint) {
        stop();
        reloadFn();
      }
    } catch {
      // A launch-time proxy restart briefly drops requests. Keep polling; the
      // first healthy response will carry the selected environment fingerprint.
    } finally {
      checking = false;
    }
  }

  async function start() {
    await poll();
    if (!stopped && timer === null) timer = setIntervalFn(() => void poll(), pollMs);
  }

  return { poll, start, stop };
}

/** Start only for the simulator overlay; physical robots never poll a sim URL. */
export async function startSimEnvironmentWatcher(options = {}) {
  const fetchFn = options.fetchFn ?? globalThis.fetch.bind(globalThis);
  try {
    const response = await fetchFn(CONFIG_URL, { cache: "no-store" });
    if (!response.ok || (await response.json())?.simControls !== true) return null;
  } catch {
    return null;
  }
  const watcher = createSimEnvironmentWatcher({ ...options, fetchFn });
  await watcher.start();
  return watcher;
}

if (typeof window !== "undefined") {
  void startSimEnvironmentWatcher();
}
