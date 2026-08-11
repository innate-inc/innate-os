// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Optimistic page mount, shared by every page entry.
//
// The robot always serves the webapp, so the connection target is known
// (location.hostname) and the address-entry card is only a failure fallback —
// there's no reason to gate the whole page on the socket. So: build the view
// immediately and keep it on screen, auto-connect to the serving host in the
// background, and let each panel show its own loading state until the socket is
// up (rosClient re-subscribes on connect). A page switch paints instantly
// instead of waiting on the (mDNS-resolved) reconnect; only a genuine
// first-connect failure brings the connect card back.

import { ros } from "./rosClient.js";
import { createConnectPanel } from "./teleop/connectPanel.js";

/**
 * @param {HTMLElement} stage The page's #stage element.
 * @param {string} viewClass CSS class for the view layer (e.g. "cockpit").
 * @param {(root: HTMLElement) => { destroy: () => void }} buildView
 *   Builds the page's content; called once, immediately, before the socket is up.
 * @returns {{ destroy: () => void }} the built view
 */
export function mountPage(stage, viewClass, buildView) {
  const connectLayer = document.createElement("div");
  connectLayer.className = "connect-layer";
  const viewLayer = document.createElement("div");
  viewLayer.className = viewClass;
  stage.append(connectLayer, viewLayer);

  const connectPanel = createConnectPanel(connectLayer, ros);

  // Build now — don't wait for the socket. Panels subscribe right away and fill
  // in once connected.
  const view = buildView(viewLayer);

  // Auto-connect to the serving host: in production that IS the robot, so it
  // always wins. On localhost (laptop dev) the serving host is the local stack,
  // but a remembered address (e.g. a remote robot reached earlier) is the more
  // likely target there, so prefer lastIp and fall back to localhost itself.
  // Idempotent: the router already connects once at boot, so on later mounts the
  // socket is up and this is a no-op.
  const servedHost = location.hostname;
  const robotServed = servedHost && servedHost !== "localhost" && servedHost !== "127.0.0.1";
  const target = robotServed ? servedHost : (ros.lastIp ?? servedHost);
  if (target) {
    ros.connect(target);
  }

  // Keep the view up through connecting / connected / reconnecting — transient
  // drops self-heal via rosClient while each panel shows its own loading state.
  // Only a fail-fast "disconnected" (a first connect that never opened, or the
  // idle laptop-dev state) shows the card.
  const unsubState = ros.onStateChange((state) => {
    const failed = state === "disconnected";
    connectLayer.hidden = !failed;
    viewLayer.hidden = failed;
  });

  // Fully tear down on navigation (client-side routing): stop the page's panels,
  // drop the state listener, and clear the stage so the next mount starts clean.
  return {
    destroy() {
      unsubState();
      view.destroy();
      connectPanel.destroy();
      connectLayer.remove();
      viewLayer.remove();
    },
  };
}
