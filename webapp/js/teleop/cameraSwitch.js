import { WEBRTC_ACTIVE_STREAMS_TOPIC } from "../constants.js";

// Multi-view PiP strip — a Zoom-style switcher pinned bottom-right. The big stage shows the PRIMARY view;
// the strip shows every OTHER view as a tile that climbs a three-rung ladder:
//
//   off (dark placeholder tile)  --click-->  live thumbnail (PiP)  --click-->  primary (fills the stage; leaves the strip)
//
// Promoting a thumbnail demotes whatever was big back into the strip (the swap). Hovering a live thumbnail
// reveals a × that drops it back to off. "Size is the state": the big view is self-evidently primary, so
// tiles carry no extra active highlight.
//
// Views are the robot's cameras (from /webrtc/active_streams, 2-4, not hardcoded) PLUS the nav map, which
// behaves identically — off / live thumbnail / big. When the map is primary it covers the stage and the
// video stage hides; a camera stays the session's primary underneath so the WebRTC link keeps a live feed.
// The enabled set + which view is primary persist in localStorage; by default only the default primary
// camera and the map are on. A closed tile genuinely stops streaming (each live tile is a full-bitrate
// WebRTC stream), so collapsing views is how an operator sheds bandwidth.

const STORE_KEY = "innate.cameras";

// Tile tag labels; roster ids stay the wire/camera names.
/** @type {Record<string, string>} */
const DISPLAY_LABELS = { main: "Main", arm: "Arm", orbit: "Top View" };

/** @param {string} name */
function displayLabel(name) {
  return DISPLAY_LABELS[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

// Same stroke style as the rest of the cockpit's inline icons (24 viewBox, 1.5 stroke).
const CAMERA_ICON =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>';
const MAP_ICON =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/></svg>';
const MAP_ZOOM_KEY = "innate.map.zoom"; // { small, big } metres-across, persisted per map size
const MAP_ID = "__map__"; // sentinel "view" id for the nav map (never a real camera name)
const MAP_ZOOM_DEFAULT = { small: 6, big: 16 }; // tighter as a thumbnail, wider on the full stage

/**
 * @param {HTMLElement} parent cockpit root — owns the strip and (when big) the map layer.
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ storeKey?: string, stripParent?: HTMLElement, primaryOnMount?: string }} [opts]
 *   storeKey: isolate this strip's prefs (primary view, map state) per page.
 *   primaryOnMount: open on this view every time, ignoring any persisted
 *   primary. Switching views still works and still persists — the next mount
 *   just starts here again. Falls back to the usual default if the view is not
 *   in the roster.
 * @returns {{ destroy: () => void }}
 */
export function createCameraSwitch(parent, session, ros, opts = {}) {
  const storeKey = opts.storeKey || STORE_KEY;

  const strip = document.createElement("div");
  strip.className = "cam-strip";
  strip.setAttribute("role", "group");
  strip.setAttribute("aria-label", "Camera views");
  strip.hidden = true; // shown once we learn the camera roster
  (opts.stripParent ?? parent).append(strip);

  /** @type {string[]} */ let roster = []; // camera names in m-line order
  /** @type {Set<string>} */ let enabledCams = new Set();
  let mapOn = false;
  /** @type {string} */ let primary = MAP_ID; // a camera name or MAP_ID; reconcile fixes the real default
  /** @type {Map<string, { tile: HTMLElement, video: HTMLVideoElement | null, index: number }>} */
  let tiles = new Map(); // video is null for sim tiles (canvas-backed, no MediaStream)

  // The map widget is heavy (subscriptions + canvas + the app's biggest module), so both the module and the
  // widget arrive only when the map is turned on. The host div is what marks the map as live: it is created
  // synchronously so the tiles have something to reparent, and the widget drops into it once the import
  // lands. It is persistent and reparents between a strip tile (small) and the stage (big) — never rebuilt.
  /** @type {HTMLElement | null} */ let mapHost = null;
  /** @type {{ destroy: () => void, setZoom: (m: number) => void, refresh: () => void } | null} */ let mapWidget = null;
  /** @type {"small" | "big"} which saved zoom is live: thumbnail vs full stage */ let mapMode = "small";
  let mapZoom = { ...MAP_ZOOM_DEFAULT };

  loadPrefs();

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(storeKey) || "{}");
      // "" = no saved choice; reconcile picks. A primaryOnMount caller overrides
      // the saved value outright — reconcile validates it against the roster.
      primary = opts.primaryOnMount ?? (typeof p.primary === "string" ? p.primary : "");
      if (p.v === 2) {
        enabledCams = new Set(Array.isArray(p.enabled) ? p.enabled : []);
        mapOn = p.mapOn === true;
      } else {
        // v1 profiles: reconcile forced every camera on (never a user choice), so only the
        // primary survives the migration to the primary+map default (reconcile re-enables it).
        mapOn = true;
      }
    } catch {
      primary = opts.primaryOnMount ?? "";
      /* other defaults: nothing enabled, reconcile picks the first camera */
    }
    try {
      const z = JSON.parse(localStorage.getItem(MAP_ZOOM_KEY) || "{}");
      if (z.small > 0) mapZoom.small = z.small;
      if (z.big > 0) mapZoom.big = z.big;
    } catch {
      /* defaults above */
    }
  }

  function saveMapZoom() {
    localStorage.setItem(MAP_ZOOM_KEY, JSON.stringify(mapZoom));
  }

  function savePrefs() {
    localStorage.setItem(storeKey, JSON.stringify({ v: 2, enabled: [...enabledCams], mapOn, primary }));
  }

  // Drop names the roster no longer has, then guarantee a valid enabled primary (the stage always needs a
  // view). Zero cameras is fine when the map is primary (map-only releases WebRTC). Fresh profile: only
  // the default primary camera streams, plus the map.
  function reconcile() {
    enabledCams = new Set([...enabledCams].filter((n) => roster.includes(n)));
    if (primary === "") mapOn = true; // nothing persisted yet — the map starts on
    // Roster membership is the validity test: the tail below re-enables the primary, so a
    // primaryOnMount view (or a migrated v1 primary) outside the saved enabled set still wins.
    const validPrimary = primary === MAP_ID ? mapOn : roster.includes(primary);
    if (!validPrimary) {
      // No saved choice (or a stale one): default to the view that shows the
      // robot best -- the sim's orbit "top view" (only simulated robots have
      // it), or the head camera on real robots. A saved choice always wins.
      primary =
        (roster.includes("orbit") ? "orbit" : null) ??
        (roster.includes("main") ? "main" : null) ??
        [...enabledCams][0] ??
        MAP_ID;
    }
    if (primary === MAP_ID) mapOn = true;
    else enabledCams.add(primary);
  }

  // Tell the session which cameras to stream and which one backs the big stage. When the map is primary
  // there's no big camera, but a camera still streams as the session primary (its liveness gates the link).
  function pushSession() {
    session.setActiveCameras(roster.filter((n) => enabledCams.has(n)));
    const primaryCam =
      primary !== MAP_ID && roster.includes(primary) ? primary : roster.find((n) => enabledCams.has(n));
    if (primaryCam) session.setPrimaryCamera(roster.indexOf(primaryCam), primaryCam);
  }

  function commit() {
    savePrefs();
    pushSession();
    renderStructure();
  }

  /** One-shot: the view whose fresh live tile plays the pill→tile entrance animation. */
  let justOpened = "";

  /** Bring a view one rung up the ladder: off → live thumbnail (does NOT steal the big stage). @param {string} id */
  function enable(id) {
    if (id === MAP_ID) mapOn = true;
    else enabledCams.add(id);
    justOpened = id;
    commit();
  }

  /** Promote a view to the big stage; whatever was big drops back into the strip. @param {string} id */
  function promote(id) {
    if (id === primary) return;
    primary = id;
    if (id === MAP_ID) mapOn = true;
    else enabledCams.add(id);
    commit();
  }

  // Drop a live view back to off. The strip never shows the primary, so the closed view normally isn't
  // it; if that invariant ever breaks, fall back so the stage keeps a view.
  /** @param {string} id */
  function disable(id) {
    if (id === MAP_ID) mapOn = false;
    else enabledCams.delete(id);
    if (id === primary) {
      primary = roster.find((n) => enabledCams.has(n)) ?? MAP_ID;
      if (primary === MAP_ID) mapOn = true;
    }
    commit();
  }

  // Match the live map widget to mapOn (create when turned on, tear down when off).
  function ensureMap() {
    if (mapOn && !mapHost) {
      const host = document.createElement("div");
      host.className = "cam-map-host";
      mapHost = host;
      import("../map/mapWidget.js")
        .then((m) => {
          if (mapHost !== host) return; // the map was turned off (or the strip torn down) mid-fetch
          mapWidget = m.createMap(host, {
            zoom: mapZoom[mapMode], // read on arrival: the size may have changed while it loaded
            // Scroll-zoom persists against whichever size is showing now.
            onZoomChange: (z) => {
              mapZoom[mapMode] = z;
              saveMapZoom();
            },
            // Memories pulse in live while you drive — the tour builds the
            // robot's spatial memory, and this is where you watch it happen.
            layers: { memories: true },
          });
        })
        .catch(() => {
          // A dropped fetch must not blank the map for the session: with the
          // host cleared, the next ensureMap() recreates it and retries.
          if (mapHost !== host) return;
          host.remove();
          mapHost = null;
        });
    } else if (!mapOn && mapHost) {
      mapWidget?.destroy();
      mapHost.remove();
      mapWidget = null;
      mapHost = null;
    }
  }

  // Park the map host where the current primary dictates: full stage (big) or inside its strip tile (small),
  // and swap in that size's saved zoom. (As a thumbnail, a plain click still bubbles to the tile's promote
  // handler — goal-mode is off and its control hidden — so the map stays clickable while also wheel-zoomable.)
  function placeMap() {
    const big = primary === MAP_ID;
    mapMode = big ? "big" : "small";
    parent.classList.toggle("cam-map-primary", big);
    if (mapHost) {
      mapHost.classList.toggle("big", big);
      if (big && parent.firstChild !== mapHost) parent.insertBefore(mapHost, parent.firstChild);
    }
    mapWidget?.setZoom(mapZoom[mapMode]);
    // The host has just moved between the stage and its tile; redraw against
    // the box it landed in rather than waiting on a ResizeObserver tick.
    mapWidget?.refresh();
  }

  // Rebuild the strip's tiles — every view EXCEPT the primary (which is the big stage).
  function renderStructure() {
    strip.hidden = roster.length === 0;
    tiles = new Map();
    ensureMap();
    if (strip.hidden) {
      strip.replaceChildren();
      placeMap();
      return;
    }
    const children = roster.filter((name) => name !== primary).map(buildCameraTile);
    if (primary !== MAP_ID) children.push(buildMapTile());
    strip.replaceChildren(...children);
    placeMap();
    syncStreams(session.state);
  }

  /** @param {string} name */
  function buildCameraTile(name) {
    if (!enabledCams.has(name)) return offTile(name, displayLabel(name), `Turn on the ${name} camera`);
    const index = roster.indexOf(name);
    const label = displayLabel(name);
    const tile = liveTile(name, label, `Switch to ${label} view`);
    const status = document.createElement("span");
    status.className = "cam-tile-status";
    status.textContent = "connecting…";
    tile.append(status);
    // Sim sessions expose live canvases (no MediaStream pipeline -- canvas
    // capture pinned page composition to its capture rate); mount those
    // directly. Real robots keep the <video> + WebRTC stream path. SimSession
    // reaches here through robotSession.js's runtime import, so tsc only sees
    // WebRtcSession -- duck-type the sim-only method instead.
    const maybeSim = /** @type {{ thumbnailCanvas?: (i: number) => HTMLCanvasElement | null }} */ (
      /** @type {unknown} */ (session)
    );
    const thumbCanvas = maybeSim.thumbnailCanvas?.(index) ?? null;
    if (thumbCanvas) {
      thumbCanvas.style.cssText = "width:100%;height:100%;object-fit:cover;display:block;";
      tile.prepend(thumbCanvas);
      tiles.set(name, { tile, video: null, index });
      return tile;
    }
    const video = document.createElement("video");
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    tile.prepend(video);
    tiles.set(name, { tile, video, index });
    return tile;
  }

  function buildMapTile() {
    if (!mapOn) return offTile(MAP_ID, "Map", "Show the navigation map");
    const tile = liveTile(MAP_ID, "Map", "Switch to Map view · scroll to zoom");
    tile.classList.add("cam-tile-map");
    if (mapHost) tile.prepend(mapHost); // reparent the persistent host into the thumbnail
    return tile;
  }

  /** Dark placeholder tile for an off view — same footprint as a live tile, so the strip never
   *  reflows; clicking climbs to the live-thumbnail rung. @param {string} id @param {string} label @param {string} title */
  function offTile(id, label, title) {
    const tile = document.createElement("div");
    tile.className = "cam-tile off";
    tile.tabIndex = 0;
    tile.setAttribute("role", "button");
    tile.setAttribute("aria-label", title);
    tile.innerHTML =
      (id === MAP_ID ? MAP_ICON : CAMERA_ICON) +
      '<span class="cam-tile-name"></span><span class="cam-tile-hint">tap to view</span>';
    const name = tile.querySelector(".cam-tile-name");
    if (name) name.textContent = label;
    tile.addEventListener("click", () => enable(id));
    tile.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      enable(id);
    });
    return tile;
  }

  /** Live thumbnail shell (caller prepends the video/map). @param {string} id @param {string} label @param {string} title */
  function liveTile(id, label, title) {
    const tile = document.createElement("div");
    tile.className = "cam-tile live";
    tile.tabIndex = 0;
    tile.setAttribute("role", "button");
    tile.setAttribute("aria-label", title);
    if (id === justOpened) {
      justOpened = "";
      tile.classList.add("opening");
      tile.addEventListener("animationend", () => tile.classList.remove("opening"), { once: true });
    }
    tile.addEventListener("click", () => promote(id));
    tile.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      promote(id);
    });
    const tag = document.createElement("span");
    tag.className = "cam-tile-label";
    tag.textContent = label;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "cam-tile-close";
    close.title = id === MAP_ID ? "Close" : "Close (stops streaming)";
    close.textContent = "×";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      disable(id);
    });
    tile.append(tag, close);
    return tile;
  }

  // Keep each camera thumbnail bound to its stream and reflect liveness (a not-yet-flowing feed shows the
  // "connecting" shimmer). Cheap and idempotent — runs on every session change.
  /** @param {WebRtcState} state */
  function syncStreams(state) {
    for (const { tile, video, index } of tiles.values()) {
      if (video) {
        const stream = state.videoStreams[index] ?? null;
        if (video.srcObject !== stream) {
          video.srcObject = stream;
          video.play().catch(() => {});
        }
      }
      tile.classList.toggle("connecting", !state.videoLive[index]);
    }
  }

  const unsubSession = session.onChange(syncStreams);

  const unsub = ros.subscribe(WEBRTC_ACTIVE_STREAMS_TOPIC, (m) => {
    const raw = m?.data ?? m?.msg?.data;
    if (typeof raw !== "string") return;
    let next;
    try {
      next = JSON.parse(raw).cameras;
    } catch {
      return;
    }
    if (!Array.isArray(next)) return;
    // Status republishes on a 2s timer; only react when the roster actually changes.
    if (next.length === roster.length && next.every((c, i) => c === roster[i])) return;
    roster = next;
    reconcile();
    commit();
  }, undefined, "std_msgs/msg/String");

  return {
    destroy() {
      unsub?.();
      unsubSession();
      mapWidget?.destroy();
      mapHost?.remove();
      mapHost = null; // an import still in flight must not build into the removed host
      parent.classList.remove("cam-map-primary");
      strip.remove();
    },
  };
}
