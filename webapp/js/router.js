// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Client-side router — the single entry point for the whole app.
//
// The app used to be multi-page: every rail click was a full browser reload
// that tore down the JS context, dropped the rosbridge socket, and forced a
// reconnect + re-subscribe + WebRTC renegotiation (the lag you felt on a
// switch). It also meant a page's teardown never ran on navigation, so a
// learned skill (or any running action) kept going with no owner.
//
// Now navigation is client-side: the rosbridge socket and the shell persist,
// and each page is a module that exports `mount(stage) -> { destroy }`. On
// every navigation we destroy the outgoing page (which stops its skills/drive
// and frees its panels) and mount the next into the same #stage. One socket,
// one shell, instant switches.

import { ros } from "./rosClient.js";
import { initShell } from "./shell.js";
import { getConfig } from "./config.js";
import { SIM_SECTIONS } from "./railLayout.js";

/**
 * @typedef {{ destroy: () => void }} PageView
 * @typedef {{ path: string, key: string, load: () => Promise<{ mount: (stage: HTMLElement) => PageView | Promise<PageView> }> }} Route
 */

// Teleop is the site root; every other section lives at /<key>. `key` matches
// the shell's section keys so setActive can highlight the right rail link.
// /brain is an alias into the Agent page (which opens its Brain monitor when
// mounted at that path, then settles the URL on /agent) — old bookmarks keep
// working and the rail highlights Agent.
/** @type {Route[]} */
const ROUTES = [
  { path: "/", key: "teleop", load: () => import("./teleop/main.js") },
  { path: "/agent", key: "agent", load: () => import("./agent/main.js") },
  { path: "/brain", key: "agent", load: () => import("./agent/main.js") },
  { path: "/nav", key: "nav", load: () => import("./nav/main.js") },
  { path: "/logging", key: "logging", load: () => import("./logging/main.js") },
  { path: "/datasets", key: "datasets", load: () => import("./datasets/main.js") },
  { path: "/collect", key: "collect", load: () => import("./collect/main.js") },
  { path: "/training", key: "training", load: () => import("./training/main.js") },
  { path: "/profiling", key: "profiling", load: () => import("./profiling/main.js") },
  { path: "/calibration", key: "calibration", load: () => import("./calibration/main.js") },
  { path: "/armsdk", key: "armsdk", load: () => import("./armsdk/main.js") },
  { path: "/settings", key: "settings", load: () => import("./settings/main.js") },
];

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

/** @type {PageView | null} */
let currentView = null;
let currentKey = "";
// Bumped on every navigation so a slow dynamic import that resolves after a
// newer navigation started can detect it's stale and not mount over the winner.
let navSeq = 0;

/** Drop a trailing slash (except root) so "/profiling/" and "/profiling" match. @param {string} pathname */
function normalize(pathname) {
  const p = pathname.replace(/\/+$/, "");
  return p === "" ? "/" : p;
}

/** The route for a pathname, defaulting to teleop for anything unrecognized. @param {string} pathname */
function routeFor(pathname) {
  const p = normalize(pathname);
  return ROUTES.find((r) => r.path === p) || ROUTES[0];
}

const shell = initShell(navigate);

// Sim deployments hide robot-data workflows from the rail (SIM_SECTIONS);
// gate the routes too, so a deep link or refresh can't mount a page whose
// services have no sim backing.
/** @type {Promise<{simControls?: boolean}>} */
const configPromise = getConfig();

/**
 * Tear down the current page and mount `route`. Pages read their own query
 * string (e.g. collect's ?dir=) from location, so history is updated before
 * this runs.
 * @param {Route} route
 */
async function render(route) {
  const seq = ++navSeq;
  if ((await configPromise)?.simControls && !SIM_SECTIONS.has(route.key)) route = ROUTES[0];
  if (seq !== navSeq) return; // superseded while awaiting config
  // Destroy BEFORE building the next page: the outgoing destroy() stops running
  // skills/drive and frees socket-bound panels, and clears the stage.
  if (currentView) {
    currentView.destroy();
    currentView = null;
  }
  currentKey = route.key;
  shell.setActive(route.key);
  try {
    const mod = await route.load();
    if (seq !== navSeq) return; // a newer navigation superseded this one
    currentView = await mod.mount(stage);
    if (seq !== navSeq && currentView) {
      // Superseded while mount() awaited (e.g. config fetch) — undo it.
      currentView.destroy();
      currentView = null;
    }
  } catch (err) {
    console.error(`[router] failed to load ${route.path}:`, err);
  } finally {
    // The first page has now built its DOM (a sim stage brings up its own
    // loading scrim here), so hand off from the boot splash. In `finally` so a
    // failed first mount still clears it rather than stranding the splash.
    dismissBootSplash();
  }
}

// Boot splash lives in index.html so it paints before this module's graph
// loads; drop it once the first page mounts. Fade then remove; a fallback
// timer covers a missed transitionend (reduced-motion, pre-paint start).
function dismissBootSplash() {
  const splash = document.getElementById("boot-splash");
  if (!splash) return;
  splash.classList.add("is-leaving");
  splash.addEventListener("transitionend", () => splash.remove(), { once: true });
  setTimeout(() => splash.remove(), 600);
}

/**
 * Go to an in-app location, updating history. Preserves the query string so
 * cross-page links like /collect?dir=… reach the page with their params.
 * @param {string} href pathname (+ optional search), e.g. "/collect?dir=x".
 */
function navigate(href) {
  const url = new URL(href, location.origin);
  const route = routeFor(url.pathname);
  // Dedupe on the path, not the route key: /brain shares Agent's key, but a
  // /brain link clicked from /agent must still render so the monitor opens.
  if (normalize(url.pathname) === normalize(location.pathname) && url.search === location.search) return;
  history.pushState({}, "", url.pathname + url.search);
  void render(route);
}

// Intercept in-app link clicks so navigation stays client-side. Anything that
// isn't a plain left-click on a same-origin app-route link (external links,
// doc links with target=_blank, downloads, the http→https arm switch) falls
// through to the browser as a normal navigation.
document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const target = e.target;
  const a = target instanceof Element ? target.closest("a") : null;
  if (!(a instanceof HTMLAnchorElement)) return;
  if (a.target === "_blank" || a.hasAttribute("download") || a.origin !== location.origin) return;
  if (!ROUTES.some((r) => r.path === normalize(a.pathname))) return; // not an app route
  e.preventDefault();
  navigate(a.pathname + a.search);
});

// Back/forward: re-render for the target location (history is already updated).
window.addEventListener("popstate", () => {
  const route = routeFor(location.pathname);
  if (route.key === currentKey) return;
  void render(route);
});

// Connect once; the socket now persists across in-app navigation. Mirrors
// pageMount's target logic (robot serves the app in prod; prefer a remembered
// address on laptop dev).
const servedHost = location.hostname;
const robotServed = servedHost && servedHost !== "localhost" && servedHost !== "127.0.0.1";
const connectTarget = robotServed ? servedHost : (ros.lastIp ?? servedHost);
if (connectTarget) {
  ros.connect(connectTarget);
  // rosClient retries on its own after a drop that follows a successful open, but
  // a first connect that never opens fails fast with no retry — which would strand
  // pages with no connect card (settings, profiling) on a refresh or deep link
  // while the robot is momentarily unreachable. So retry the initial-connect-failed
  // case here, debounced so a refused connection can't spin, and only until we've
  // connected once: after that, drops self-heal via rosClient, and a manual
  // connect from teleop's panel is respected.
  let everConnected = false;
  /** @type {number | null} */
  let connectRetry = null;
  ros.onStateChange((state) => {
    if (state === "connected") everConnected = true;
    if (state === "disconnected" && !everConnected) {
      if (connectRetry === null) {
        connectRetry = setTimeout(() => {
          connectRetry = null;
          ros.connect(connectTarget);
        }, 5000);
      }
    } else if (connectRetry !== null) {
      clearTimeout(connectRetry);
      connectRetry = null;
    }
  });
}

// Render the page for the URL we loaded at (deep link or refresh), then prefetch
// the other route modules while idle so later switches are instant.
void render(routeFor(location.pathname));
prefetchRoutes();

function prefetchRoutes() {
  const idle = /** @type {any} */ (window).requestIdleCallback || ((/** @type {() => void} */ fn) => setTimeout(fn, 1500));
  idle(() => {
    for (const route of ROUTES) {
      if (route.key !== currentKey) route.load().catch(() => {});
    }
  });
}
