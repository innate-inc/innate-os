// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Keep an already-open simulator tab aligned with the host-selected world.
//
// Environment switching restarts the host world and the in-container ROS
// session, so this controller deliberately lives in the page shell. It keeps
// running while the proxy is unavailable, owns the blocking transition UI,
// and coordinates the host job with the independently reloaded Three.js scene
// and world-state connection. Nothing here reloads the document.

const CONFIG_URL = "/config.json";
const CATALOG_URL = "/sim-environments.json";
const SWITCH_URL = "/sim-environment/switch";
const DEFAULT_POLL_MS = 1000;

export const SWITCH_REQUEST_EVENT = "innate:sim-environment-switch-request";
export const SWITCH_STATE_EVENT = "innate:sim-environment-switch-state";
export const CATALOG_EVENT = "innate:sim-environment-catalog";
export const VIEWER_STATE_EVENT = "innate:sim-environment-viewer-state";
export const WORLD_STATE_EVENT = "innate:sim-environment-world-state";

/** @typedef {{id:string, display_name:string, fingerprint?:string}} EnvironmentSummary */
/** @typedef {{schema_version:number, active:EnvironmentSummary | null, environments:EnvironmentSummary[]}} EnvironmentCatalog */
/**
 * @typedef {Object} SimEnvironmentWatcherOptions
 * @property {typeof fetch} [fetchFn]
 * @property {typeof setInterval} [setIntervalFn]
 * @property {typeof clearInterval} [clearIntervalFn]
 * @property {() => string | null} [loadedFingerprintFn]
 * @property {(state: TransitionSnapshot) => void} [onState]
 * @property {(catalog: EnvironmentCatalog) => void} [onCatalog]
 * @property {number} [pollMs]
 */

/** @param {unknown} value @returns {EnvironmentSummary | null} */
function environmentSummary(value) {
  if (!value || typeof value !== "object") return null;
  const raw = /** @type {{id?:unknown, display_name?:unknown, fingerprint?:unknown}} */ (value);
  if (typeof raw.id !== "string" || !raw.id) return null;
  return {
    id: raw.id,
    display_name: typeof raw.display_name === "string" && raw.display_name ? raw.display_name : raw.id,
    ...(typeof raw.fingerprint === "string" && raw.fingerprint ? { fingerprint: raw.fingerprint } : {}),
  };
}

/** @param {unknown} value @returns {EnvironmentCatalog | null} */
function environmentCatalog(value) {
  if (!value || typeof value !== "object") return null;
  const raw = /** @type {{schema_version?:unknown, active?:unknown, environments?:unknown}} */ (value);
  const active = raw.active === null ? null : environmentSummary(raw.active);
  if (raw.schema_version !== 1 || (raw.active !== null && !active) || !Array.isArray(raw.environments)) return null;
  const environments = raw.environments.map(environmentSummary).filter((item) => item !== null);
  return { schema_version: 1, active, environments };
}

/** @param {unknown} value @returns {EnvironmentSummary | null} */
function recoveredEnvironment(value) {
  if (typeof value === "string" && value) return { id: value, display_name: value };
  return environmentSummary(value);
}

/**
 * @typedef {Object} TransitionSnapshot
 * @property {boolean} active
 * @property {"idle"|"submitting"|"queued"|"running"|"loading"|"failed"|"recovering"|"complete"} status
 * @property {number} generation
 * @property {EnvironmentSummary | null} target
 * @property {string | null} fingerprint
 * @property {string | null} jobId
 * @property {string} phase
 * @property {string} message
 * @property {EnvironmentSummary | null} recovery
 * @property {EnvironmentSummary | null} fallback
 * @property {boolean} recoverySafe
 */

/**
 * Dependency-injected controller used by the browser and the Node tests.
 * The latest generation always wins: late job/catalog/viewer/world results
 * carry their identity and cannot finish a newer transition.
 *
 * @param {SimEnvironmentWatcherOptions} [options]
 */
export function createSimEnvironmentWatcher(options = {}) {
  const fetchFn = options.fetchFn ?? globalThis.fetch.bind(globalThis);
  const setIntervalFn = options.setIntervalFn ?? globalThis.setInterval.bind(globalThis);
  const clearIntervalFn = options.clearIntervalFn ?? globalThis.clearInterval.bind(globalThis);
  const loadedFingerprintFn =
    options.loadedFingerprintFn ??
    (() => {
      const shared = /** @type {typeof globalThis & {__innateSimLoadedEnvironmentFingerprint?: unknown}} */ (
        globalThis
      );
      const value = shared.__innateSimLoadedEnvironmentFingerprint;
      return typeof value === "string" && value ? value : null;
    });
  const onState = options.onState ?? (() => {});
  const onCatalog = options.onCatalog ?? (() => {});
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;

  /** @type {EnvironmentCatalog | null} */
  let catalog = null;
  /** @type {ReturnType<typeof setInterval> | null} */
  let timer = null;
  let stopped = false;
  let checking = false;
  /** @type {string | null} */
  let seenFingerprint = null;
  let generation = 0;
  /** @type {{id:string, fingerprint:string, connected:boolean} | null} */
  let world = null;
  /** @type {{fingerprint:string, ready:boolean} | null} */
  let viewer = null;
  /** @type {{
   *   generation:number,
   *   target:EnvironmentSummary,
   *   external:boolean,
   *   fingerprint:string|null,
   *   jobId:string|null,
   *   jobReady:boolean,
   *   failed:boolean,
   *   phase:string,
   *   message:string,
   *   recovery:EnvironmentSummary|null,
   *   recoveryFingerprint:string|null,
   *   missingPolls:number,
   *   previous:EnvironmentSummary|null,
   *   viewerFailures:number,
   * } | null} */
  let transition = null;

  /** @returns {TransitionSnapshot} */
  function snapshot() {
    if (!transition) {
      return {
        active: false,
        status: "idle",
        generation,
        target: null,
        fingerprint: null,
        jobId: null,
        phase: "",
        message: "",
        recovery: null,
        fallback: null,
        recoverySafe: false,
      };
    }
    const recoverySafe = Boolean(
      transition.failed &&
        transition.recoveryFingerprint &&
        viewer?.ready &&
        viewer.fingerprint === transition.recoveryFingerprint &&
        world?.connected &&
        world.id === transition.recovery?.id &&
        world.fingerprint === transition.recoveryFingerprint,
    );
    /** @type {TransitionSnapshot["status"]} */
    let status = transition.failed ? (transition.recovery ? "recovering" : "failed") : "loading";
    if (!transition.failed) {
      if (!transition.jobId && !transition.external) status = "submitting";
      else if (!transition.jobReady && transition.phase === "queued") status = "queued";
      else if (!transition.jobReady) status = "running";
    }
    return {
      active: true,
      status,
      generation: transition.generation,
      target: transition.target,
      fingerprint: transition.failed ? transition.recoveryFingerprint : transition.fingerprint,
      jobId: transition.jobId,
      phase: transition.phase,
      message: transition.message,
      recovery: transition.recovery,
      fallback: transition.previous,
      recoverySafe,
    };
  }

  function emit() {
    onState(snapshot());
  }

  /** @param {EnvironmentSummary} target @param {boolean} external @param {EnvironmentSummary | null} [previous] */
  function begin(target, external, previous = catalog?.active ?? null) {
    generation += 1;
    // Keep the socket's actual observed state. An old world cannot satisfy a
    // different target because completion checks both id and fingerprint, and
    // WorldStateController reports a real disconnect immediately. Preserving
    // a still-connected matching world is important when validation fails
    // before mutation or a retry/return is a same-environment no-op.
    transition = {
      generation,
      target,
      external,
      fingerprint: target.fingerprint ?? null,
      jobId: null,
      jobReady: external,
      failed: false,
      phase: external ? "loading" : "submitting",
      message: external ? `Loading ${target.display_name}…` : `Requesting ${target.display_name}…`,
      recovery: null,
      recoveryFingerprint: null,
      missingPolls: 0,
      previous,
      viewerFailures: 0,
    };
    emit();
    return generation;
  }

  function maybeComplete() {
    if (!transition || transition.failed || !transition.jobReady || !transition.fingerprint) return;
    const fingerprint = transition.fingerprint;
    if (!viewer?.ready || viewer.fingerprint !== fingerprint) return;
    if (!world?.connected || world.id !== transition.target.id || world.fingerprint !== fingerprint) return;
    const completed = transition;
    seenFingerprint = fingerprint;
    transition = null;
    onState({
      active: false,
      status: "complete",
      generation: completed.generation,
      target: completed.target,
      fingerprint,
      jobId: completed.jobId,
      phase: "ready",
      message: `${completed.target.display_name} is ready.`,
      recovery: null,
      fallback: null,
      recoverySafe: false,
    });
  }

  /**
   * A scene/world identity can arrive after the catalog's first healthy poll.
   * Interlock immediately when that late identity disproves the catalog instead
   * of leaving controls enabled until another poll happens to notice it.
   *
   * @returns {boolean} whether a new external transition was started
   */
  function interlockCatalogMismatch() {
    if (transition) return false;
    const active = catalog?.active;
    const fingerprint = active?.fingerprint;
    if (!active || !fingerprint) return false;
    const viewerMismatch = Boolean(viewer && viewer.fingerprint !== fingerprint);
    const connectedWorldMismatch = Boolean(
      world?.connected && (world.id !== active.id || world.fingerprint !== fingerprint),
    );
    if (!viewerMismatch && !connectedWorldMismatch) return false;
    begin(active, true);
    return true;
  }

  /** @param {EnvironmentCatalog} next */
  function acceptCatalog(next) {
    const previousActive = catalog?.active ?? null;
    catalog = next;
    onCatalog(next);
    const active = next.active;
    if (!active) return;
    const fingerprint = active.fingerprint ?? null;
    if (!fingerprint) return;

    if (!transition) {
      const loaded = loadedFingerprintFn();
      if ((loaded && loaded !== fingerprint) || (seenFingerprint && seenFingerprint !== fingerprint)) {
        begin(active, true, previousActive);
      } else if (!interlockCatalogMismatch()) {
        seenFingerprint = fingerprint;
      }
    }

    if (!transition) return;
    if (transition.failed && transition.recovery && active.id === transition.recovery.id) {
      transition.recovery = { ...transition.recovery, ...active };
      transition.recoveryFingerprint = fingerprint;
      transition.phase = "rollback";
      transition.message ||= `Restoring ${active.display_name}…`;
      emit();
      return;
    }
    if (active.id === transition.target.id) {
      transition.target = { ...transition.target, ...active };
      transition.fingerprint = fingerprint;
      emit();
      maybeComplete();
      return;
    }

    // A different environment became active while a previous request was in
    // flight (CLI override or a newer host request). Follow the source of truth;
    // late results from the superseded generation are ignored. The one expected
    // exception is a submitted job's exact pre-request identity: the catalog
    // continues publishing it until the controller commits the requested pack.
    // External transitions never get this exception; B -> A there is a real
    // rollback even if A was the last fully rendered fingerprint.
    const submittedPreviousStillActive = Boolean(
      !transition.external &&
        !transition.jobReady &&
        transition.previous?.id === active.id &&
        transition.previous.fingerprint === fingerprint,
    );
    if (!submittedPreviousStillActive) begin(active, true, previousActive);
  }

  async function fetchCatalog() {
    const response = await fetchFn(CATALOG_URL, { cache: "no-store" });
    if (!response.ok) return;
    const next = environmentCatalog(await response.json());
    if (next) acceptCatalog(next);
  }

  async function pollJob() {
    const current = transition;
    if (!current?.jobId || current.jobReady || current.failed) return;
    const currentGeneration = current.generation;
    const response = await fetchFn(`${SWITCH_URL}/${encodeURIComponent(current.jobId)}`, { cache: "no-store" });
    if (!transition || transition.generation !== currentGeneration) return;
    if (!response.ok) {
      // The proxy can observe the mailbox before the controller writes its
      // first job snapshot. Give that bounded race time, and keep all 5xx
      // responses retryable while the service restarts.
      if (response.status === 404 && current.missingPolls < 10) {
        current.missingPolls += 1;
        current.message = "Waiting for the simulator controller…";
        emit();
        return;
      }
      if (response.status >= 500) return;
      transition.failed = true;
      transition.phase = "status";
      transition.message = `Could not read environment switch status (HTTP ${response.status}).`;
      emit();
      return;
    }
    current.missingPolls = 0;
    const payload = await response.json();
    if (!transition || transition.generation !== currentGeneration) return;
    const state = typeof payload?.state === "string" ? payload.state : "";
    const returnedTarget = environmentSummary(payload?.target);
    if (returnedTarget && returnedTarget.id !== transition.target.id) {
      transition.failed = true;
      transition.phase = "status";
      transition.message = "The simulator controller returned status for a different environment.";
      emit();
      return;
    }
    if (returnedTarget) transition.target = { ...transition.target, ...returnedTarget };
    transition.phase = typeof payload?.phase === "string" && payload.phase ? payload.phase : state;
    transition.message =
      typeof payload?.message === "string" && payload.message
        ? payload.message
        : state === "ready"
          ? `Waiting for ${transition.target.display_name} to reconnect…`
          : `Switching to ${transition.target.display_name}…`;
    if (state === "ready") {
      const fingerprint = typeof payload?.fingerprint === "string" && payload.fingerprint ? payload.fingerprint : null;
      if (fingerprint && transition.fingerprint && fingerprint !== transition.fingerprint) {
        transition.failed = true;
        transition.phase = "status";
        transition.message = "The simulator controller and environment catalog disagree.";
      } else {
        transition.fingerprint = fingerprint ?? transition.fingerprint;
        transition.jobReady = true;
      }
    }
    if (state === "failed") {
      transition.failed = true;
      transition.recovery = recoveredEnvironment(payload?.recovered_environment);
      transition.recoveryFingerprint = transition.recovery?.fingerprint ?? null;
    }
    emit();
    maybeComplete();
  }

  async function poll() {
    if (stopped || checking) return;
    checking = true;
    try {
      // These are deliberately independent: the proxy can disappear between
      // them while ROS restarts. A failed request leaves the in-page state
      // intact and the next poll resumes the same job.
      await fetchCatalog().catch(() => {});
      await pollJob().catch(() => {});
    } finally {
      checking = false;
    }
  }

  /** @param {string} id @param {EnvironmentSummary | null | undefined} [previous] */
  async function requestSwitch(id, previous = catalog?.active ?? null) {
    const option = catalog?.environments.find((item) => item.id === id) ?? { id, display_name: id };
    if (catalog?.active?.id === id && !transition) return;
    const currentGeneration = begin(option, false, previous);
    try {
      const response = await fetchFn(SWITCH_URL, {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Requested-By": "innate-webapp" },
        body: JSON.stringify({ id }),
      });
      const payload = await response.json();
      if (!transition || transition.generation !== currentGeneration) return;
      const returnedTarget = environmentSummary(payload?.target);
      const adoptsExisting =
        response.status === 409 && returnedTarget?.id === id && typeof payload?.job_id === "string";
      if (!response.ok && !adoptsExisting) {
        const reason = typeof payload?.error === "string" && payload.error ? payload.error : `HTTP ${response.status}`;
        throw new Error(`Could not request the environment switch: ${reason}`);
      }
      if (typeof payload?.job_id !== "string" || !payload.job_id) throw new Error("switch request returned no job id");
      transition.jobId = payload.job_id;
      transition.phase = typeof payload?.state === "string" ? payload.state : "queued";
      transition.message = `Switching to ${transition.target.display_name}…`;
      emit();
      await poll();
    } catch (error) {
      if (!transition || transition.generation !== currentGeneration) return;
      transition.failed = true;
      transition.phase = "request";
      transition.message = error instanceof Error ? error.message : "Could not request the environment switch.";
      emit();
    }
  }

  /** @param {{fingerprint?:unknown, ready?:unknown, error?:unknown}} detail */
  function noteViewer(detail) {
    if (typeof detail?.fingerprint !== "string" || !detail.fingerprint) return;
    viewer = { fingerprint: detail.fingerprint, ready: detail.ready === true };
    interlockCatalogMismatch();
    if (
      transition &&
      !transition.failed &&
      transition.fingerprint === detail.fingerprint &&
      detail.ready !== true &&
      typeof detail.error === "string" &&
      detail.error
    ) {
      transition.viewerFailures += 1;
      if (transition.viewerFailures >= 3) {
        const previous = transition.previous;
        transition.failed = true;
        transition.phase = "viewer";
        transition.message = previous && previous.id !== transition.target.id
          ? `Could not render ${transition.target.display_name}. You can try again or return to ${previous.display_name}.`
          : `Could not render ${transition.target.display_name}: ${detail.error}`;
      }
    } else if (detail.ready === true && transition?.fingerprint === detail.fingerprint) {
      transition.viewerFailures = 0;
    }
    emit();
    maybeComplete();
  }

  /** @param {{id?:unknown, fingerprint?:unknown, connected?:unknown}} detail */
  function noteWorld(detail) {
    if (typeof detail?.id !== "string" || typeof detail?.fingerprint !== "string") return;
    world = { id: detail.id, fingerprint: detail.fingerprint, connected: detail.connected === true };
    interlockCatalogMismatch();
    emit();
    maybeComplete();
  }

  async function retry() {
    const current = transition;
    if (current) await requestSwitch(current.target.id, current.previous);
  }

  async function returnToPrevious() {
    const previous = transition?.previous;
    if (previous && previous.id !== transition?.target.id) await requestSwitch(previous.id);
  }

  function continueRecovery() {
    const state = snapshot();
    if (!transition || !state.recoverySafe || !transition.recoveryFingerprint) return;
    const recovered = transition.recovery;
    const fingerprint = transition.recoveryFingerprint;
    const completedGeneration = transition.generation;
    transition = null;
    seenFingerprint = fingerprint;
    onState({
      active: false,
      status: "complete",
      generation: completedGeneration,
      target: recovered,
      fingerprint,
      jobId: null,
      phase: "rollback",
      message: `${recovered?.display_name ?? "The previous environment"} was restored.`,
      recovery: recovered,
      fallback: null,
      recoverySafe: true,
    });
  }

  async function start() {
    await poll();
    if (!stopped && timer === null) timer = setIntervalFn(() => void poll(), pollMs);
  }

  function stop() {
    stopped = true;
    if (timer !== null) clearIntervalFn(timer);
    timer = null;
    transition = null;
    emit();
  }

  return { poll, start, stop, requestSwitch, retry, returnToPrevious, continueRecovery, noteViewer, noteWorld, snapshot };
}

  /** Build the DOM that remains available while the proxy and ROS are down. */
function createTransitionOverlay() {
  const overlay = document.createElement("div");
  overlay.className = "sim-environment-transition";
  overlay.hidden = true;
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "sim-environment-transition-title");

  const card = document.createElement("div");
  card.className = "sim-environment-transition__card";
  const spinner = document.createElement("span");
  spinner.className = "sim-environment-transition__spinner";
  spinner.setAttribute("aria-hidden", "true");
  const title = document.createElement("h2");
  title.id = "sim-environment-transition-title";
  const message = document.createElement("p");
  message.className = "sim-environment-transition__message";
  message.setAttribute("aria-live", "polite");
  const hint = document.createElement("p");
  hint.className = "sim-environment-transition__hint";
  const actions = document.createElement("div");
  actions.className = "sim-environment-transition__actions";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "Try again";
  const continueButton = document.createElement("button");
  continueButton.type = "button";
  continueButton.textContent = "Continue with restored environment";
  actions.append(retry, continueButton);
  card.append(spinner, title, message, hint, actions);
  overlay.append(card);
  document.body.append(overlay);

  /** @type {Map<HTMLElement, boolean>} */
  const previousInert = new Map();
  /** @param {boolean} active */
  function setBackgroundInert(active) {
    if (active) {
      for (const child of document.body.children) {
        if (!(child instanceof HTMLElement) || child === overlay) continue;
        previousInert.set(child, child.inert);
        child.inert = true;
      }
    } else {
      for (const [child, inert] of previousInert) child.inert = inert;
      previousInert.clear();
    }
  }

  const inertObserver = new MutationObserver((records) => {
    if (!visible) return;
    for (const record of records) {
      for (const added of record.addedNodes) {
        if (!(added instanceof HTMLElement) || added === overlay || previousInert.has(added)) continue;
        previousInert.set(added, added.inert);
        added.inert = true;
      }
    }
  });
  inertObserver.observe(document.body, { childList: true });

  let visible = false;
  /** @type {HTMLElement | null} */
  let previousFocus = null;
  return {
    retry,
    continueButton,
    /** @param {TransitionSnapshot} state */
    render(state) {
      const active = state.active;
      if (active !== visible) {
        visible = active;
        if (active) previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        overlay.hidden = !active;
        document.documentElement.classList.toggle("sim-environment-switching", active);
        setBackgroundInert(active);
        if (active) {
          card.tabIndex = -1;
          card.focus({ preventScroll: true });
        } else if (previousFocus?.isConnected) {
          previousFocus.focus({ preventScroll: true });
          previousFocus = null;
        }
      }
      if (!active) return;
      const name = state.target?.display_name ?? "environment";
      const failed = state.status === "failed" || state.status === "recovering";
      overlay.classList.toggle("is-failed", failed);
      spinner.hidden = failed;
      title.textContent = failed ? `Couldn’t switch to ${name}` : `Switching to ${name}`;
      message.textContent = state.message || (failed ? "The simulator could not finish the switch." : "The simulator is restarting.");
      const repairAction = state.fallback && state.fallback.id !== state.target?.id
        ? `then choose Return to ${state.fallback.display_name}`
        : "then use Try again";
      hint.textContent = failed
        ? state.recovery
          ? state.recoverySafe
            ? `${state.recovery.display_name} is running again. You can continue safely or retry the switch.`
            : `Restoring ${state.recovery.display_name} before controls are enabled…`
          : `Controls remain paused so the visual scene cannot disagree with physics. Run ./innate-sim up if the runtime was left stopped, ${repairAction}; this page can stay open.`
        : "Keep this tab open. The page and your workspace will stay in place.";
      retry.hidden = !failed;
      const fallback = state.fallback;
      const canReturn = Boolean(failed && fallback && fallback.id !== state.target?.id && !state.recovery);
      continueButton.hidden = !(failed && (state.recoverySafe || canReturn));
      if (state.recoverySafe) {
        continueButton.textContent = `Continue with ${state.recovery?.display_name ?? "restored environment"}`;
      } else if (canReturn) {
        continueButton.textContent = `Return to ${fallback?.display_name ?? "previous environment"}`;
      }
    },
    destroy() {
      setBackgroundInert(false);
      document.documentElement.classList.remove("sim-environment-switching");
      inertObserver.disconnect();
      overlay.remove();
    },
  };
}

/** Start only for simulator deployments; physical robots never poll sim APIs. */
/** @param {SimEnvironmentWatcherOptions} [options] */
export async function startSimEnvironmentWatcher(options = {}) {
  const fetchFn = options.fetchFn ?? globalThis.fetch.bind(globalThis);
  try {
    const response = await fetchFn(CONFIG_URL, { cache: "no-store" });
    if (!response.ok || (await response.json())?.simControls !== true) return null;
  } catch {
    return null;
  }

  const overlay = typeof document === "undefined" ? null : createTransitionOverlay();
  const externalState = options.onState;
  const externalCatalog = options.onCatalog;
  const watcher = createSimEnvironmentWatcher({
    ...options,
    fetchFn,
    onState: (state) => {
      overlay?.render(state);
      if (typeof document !== "undefined") document.dispatchEvent(new CustomEvent(SWITCH_STATE_EVENT, { detail: state }));
      externalState?.(state);
    },
    onCatalog: (catalog) => {
      if (typeof document !== "undefined") document.dispatchEvent(new CustomEvent(CATALOG_EVENT, { detail: catalog }));
      externalCatalog?.(catalog);
    },
  });
  overlay?.retry.addEventListener("click", () => void watcher.retry());
  overlay?.continueButton.addEventListener("click", () => {
    if (watcher.snapshot().recoverySafe) watcher.continueRecovery();
    else void watcher.returnToPrevious();
  });

  if (typeof document !== "undefined") {
    document.addEventListener(SWITCH_REQUEST_EVENT, (event) => {
      const id = /** @type {CustomEvent<{id?:unknown}>} */ (event).detail?.id;
      if (typeof id === "string" && id) void watcher.requestSwitch(id);
    });
    document.addEventListener(VIEWER_STATE_EVENT, (event) => {
      watcher.noteViewer(/** @type {CustomEvent} */ (event).detail);
    });
    document.addEventListener(WORLD_STATE_EVENT, (event) => {
      watcher.noteWorld(/** @type {CustomEvent} */ (event).detail);
    });
  }
  await watcher.start();
  return watcher;
}

if (typeof window !== "undefined") void startSimEnvironmentWatcher();
