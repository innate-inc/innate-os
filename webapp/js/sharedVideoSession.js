// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The app-level WebRTC session: one WebRtcSession (one RTCPeerConnection, one
// client_id) shared by every video page. Navigating between pages reuses the
// live link, so a tab switch is the robot's no-reneg camera switch (instant)
// instead of a cold handshake (offer/ICE/DTLS/keyframe — seconds, and tens of
// seconds if a fire-and-forget signaling message is dropped).
//
// Pages acquire on mount and release on unmount. Releasing always mutes the
// robot mic (leaving a page must never keep it audible); once no page holds
// the session, a short linger keeps the link warm through quick detours
// (Settings, Datasets) before the peer is genuinely released.

import { WebRtcSession } from "./webrtcSession.js";
import { ros } from "./rosClient.js";

const LINGER_MS = 10_000;

/** @type {WebRtcSession | null} */ let session = null;
let holders = 0;
/** @type {number | null} */ let lingerTimer = null;

/** @returns {WebRtcSession} */
export function acquireVideoSession() {
  if (lingerTimer !== null) {
    clearTimeout(lingerTimer);
    lingerTimer = null;
  }
  session ??= new WebRtcSession(ros);
  holders += 1;
  return session;
}

export function releaseVideoSession() {
  if (!session || holders === 0) return;
  holders -= 1;
  if (holders > 0) return;
  session.setAudio(false);
  lingerTimer = setTimeout(() => {
    lingerTimer = null;
    session?.stop();
  }, LINGER_MS);
}
