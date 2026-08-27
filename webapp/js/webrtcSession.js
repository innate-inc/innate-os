// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// WebRtcSession — one long-lived RTCPeerConnection signaled over the shared
// RosClient (no second socket).
//
// Handshake (robot is the offerer): build pc → publish /webrtc/start →
// robot sends SDP offer on /webrtc/offer → setRemoteDescription, drain the
// queued robot ICE, createAnswer → publish /webrtc/answer → trickle ICE both
// ways (client → /webrtc/ice_in, robot → /webrtc/ice_out).
//
// Tracks: audio starts disabled (robot mic must never be audible before the
// operator opts in); video dispatched by transceiver mid with arrival-order
// fallback; only the main camera is exposed (the arm camera is ignored in v1).
//
// Self-heal: 30 s initial-handshake watchdog, ICE disconnected/failed
// persisting 10 s → re-handshake, and a re-handshake whenever the rosbridge
// link comes back. Audio config changes debounce 700 ms then rebuild the pc —
// the robot rebuilds its whole pipeline on every START, so there is no
// renegotiation path. The old video stream is kept during rebuilds so the
// stage shows a freeze-frame instead of flashing black.

import {
  WEBRTC_START_TOPIC,
  WEBRTC_OFFER_TOPIC,
  WEBRTC_ANSWER_TOPIC,
  WEBRTC_ICE_IN_TOPIC,
  WEBRTC_ICE_OUT_TOPIC,
} from "./constants.js";
import { createLocalPeerConnection, describeIceCandidate, wireDiagnosticDataChannels } from "./webrtcConfig.js";
import { setMicAudioActive } from "./micAudioState.js";

// No SDP offer back this soon after START → the START or its broadcast offer
// was dropped (rws /webrtc/* are fire-and-forget); cheap to just republish.
// Must comfortably exceed the server's worst-case offer latency: the sim's
// aiortc can't trickle ICE, so its offer waits for full candidate gathering
// (~5s when an interface has no route to STUN, e.g. VPN utuns). A timeout at
// ~that latency re-STARTs right as the offer lands, superseding the peer the
// offer belongs to — each cycle stays one offer behind and takes minutes to
// converge instead of seconds.
const OFFER_TIMEOUT_MS = 12_000;
// Offer applied but no media flowing this long → ICE/pipeline is stuck, and
// rebuilding is worth throwing away the in-flight negotiation.
const MEDIA_TIMEOUT_MS = 7_000;
const ICE_DEGRADE_MS = 10_000;
const AUDIO_REBUILD_DEBOUNCE_MS = 700;
const OFFER_GUARD_RESET_MS = 1_000;
// Escalating rebuilds before we surface an error and wait for a manual retry.
// Bounded retries instead of a single long stare-at-black, which is what made
// refreshing feel faster.
const MAX_HANDSHAKE_ATTEMPTS = 3;
// Adaptive receive jitter buffer: receivers attach at the pc's last-known target
// (0 until the path is measured — right on LAN), re-derived every poll from path
// RTT + recent video loss. On WAN a NACK retransmit arrives ~1 RTT after the gap,
// so the buffer must cover that or loss plays out as artifacts/freezes. Polls run
// fast until the first RTT sample lands (ICE just connected): the first seconds
// are exactly when an unbuffered receiver on a long path shows every loss.
const JITTER_POLL_MS = 3_000;
const JITTER_POLL_FAST_MS = 500;

export class WebRtcSession {
  /** @type {import("./rosClient.js").RosClient} */ #ros;
  /** @type {RTCPeerConnection | null} */ #pc = null;
  /** @type {WebRtcState} */ #state = {
    status: "idle",
    videoStream: null,
    videoStreams: [],
    videoLive: [],
    audioStream: null,
    audioRequested: false,
    iceState: "new",
    stunFallback: false,
  };
  /** @type {Set<(state: WebRtcState) => void>} */ #listeners = new Set();
  /** @type {(() => void)[]} */ #unsubs = [];

  #started = false;
  #builtWithAudio = false;
  #processingOffer = false;
  #remoteDescriptionSet = false;
  /** @type {RTCIceCandidateInit[]} */ #iceQueue = [];
  #videoTrackCount = 0;
  #handshakeAttempts = 0;
  // Sticky for the session once a connect attempt fails: the next pc is rebuilt with a public STUN server
  // added (see webrtcConfig). Off on the happy path so we never hit a third-party server when the robot's
  // own STUN responder is reachable. Reset only on a full stop().
  #useFallbackStun = false;
  // Multi-camera: the robot negotiates every camera's m-line (video0, video1, …); we keep each track's
  // stream by m-line index. The robot encodes/pushes the ACTIVE set (and only those); of those, one is
  // PRIMARY (the big stage), the rest are live PiP thumbnails. Changing the active set or the primary is a
  // no-reneg START (or no START at all, for promotion within the already-live set), so it's instant.
  /** @type {(MediaStream | null)[]} */ #videoStreams = [];
  /** @type {boolean[]} */ #videoLive = [];
  // Camera names the robot should push (the START `video:` payload). Bootstrap guess until the UI learns
  // the real roster from /webrtc/active_streams and calls setActiveCameras.
  /** @type {string[]} */ #activeCams = ["main"];
  #primaryIndex = 0;
  #primaryName = "main";
  // Unique per page-load; the robot routes our offer/answer/ICE on the *_id topics by this id, so we
  // negotiate as an independent peer (and stream concurrently with any other device).
  #clientId = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);

  /** @type {number | null} */ #watchdog = null;
  /** @type {number | null} */ #degradeTimer = null;
  /** @type {number | null} */ #audioDebounce = null;
  /** @type {number | null} */ #jitterPoll = null;
  #lastJitterTargetMs = 0;
  // Previous poll's cumulative inbound-video packet counters, for a per-interval loss fraction.
  #lastVideoPackets = { received: 0, lost: 0 };

  /** @param {import("./rosClient.js").RosClient} rosClient */
  constructor(rosClient) {
    this.#ros = rosClient;
    this.#unsubs = [
      rosClient.subscribe(WEBRTC_OFFER_TOPIC, (p) => void this.#onOffer(p), undefined, "std_msgs/msg/String"),
      rosClient.subscribe(WEBRTC_ICE_OUT_TOPIC, (p) => void this.#onIceOut(p), undefined, "std_msgs/msg/String"),
      // We're an independent peer (client_id), so we do NOT yield when another device opens the
      // camera — the robot fans out to all viewers concurrently. (No /webrtc/start watch / preemption.)
      rosClient.onStateChange((state) => {
        // The robot may have restarted while we were away; renegotiate.
        if (state === "connected" && this.#started) {
          this.#handshakeAttempts = 0;
          this.#handshake();
        }
      }),
    ];
  }

  /** @returns {WebRtcState} */
  get state() {
    return this.#state;
  }

  /**
   * Live RTCStatsReport for the profiling panel, or null when no pc is up.
   * @returns {Promise<RTCStatsReport | null>}
   */
  getStats() {
    return this.#pc ? this.#pc.getStats() : Promise.resolve(null);
  }

  /**
   * @param {(state: WebRtcState) => void} cb Fires immediately, then on change.
   * @returns {() => void} unsubscribe
   */
  onChange(cb) {
    this.#listeners.add(cb);
    cb(this.#state);
    return () => this.#listeners.delete(cb);
  }

  /** Begin (or manually retry) the video link. */
  start() {
    this.#started = true;
    this.#handshakeAttempts = 0;
    this.#handshake();
  }

  /** Tear down entirely (drops the freeze-frame too). */
  stop() {
    this.#started = false;
    this.#useFallbackStun = false;
    this.#closePc();
    this.#clearAudioDebounce();
    // No mic stream once stopped (e.g. leaving the teleop page) — let TTS play.
    setMicAudioActive(false);
    this.#patch({ status: "idle", videoStream: null, audioStream: null, iceState: "new", stunFallback: false });
  }

  destroy() {
    this.stop();
    for (const unsub of this.#unsubs) unsub();
    this.#unsubs = [];
    this.#listeners.clear();
  }

  /**
   * Toggle robot-mic audio. The current track (if any) is flipped instantly;
   * the pipeline rebuild needed to add/remove the audio m-line is debounced
   * so rapid toggling costs one re-handshake, not several.
   * @param {boolean} on
   */
  setAudio(on) {
    if (this.#state.audioRequested === on) return;
    // The robot always negotiates the audio m-line for us, so toggling is a no-reneg START (it starts/
    // stops SENDING audio + opens/closes the mic) — no reconnect. Flip the local <audio> instantly too.
    const track = this.#state.audioStream?.getAudioTracks()[0];
    if (track) track.enabled = on;
    this.#patch({ audioRequested: on });
    // Tell TTS playback to stand down while we're audible via the mic.
    setMicAudioActive(on);
    if (!this.#started || this.#ros.state !== "connected") return;
    if (this.#pc) {
      this.#ros.publish(WEBRTC_START_TOPIC, {
        data: JSON.stringify({ source: "live", audio: on, client_id: this.#clientId, video: this.#activeCams }),
      });
      console.log("[webrtc] audio toggle ->", on, "(no reconnect)");
    } else {
      // No live peer (map-only, all cameras off) — bring one up so the mic can flow.
      this.#handshake();
    }
  }

  /**
   * Set which cameras the robot should encode and push (the live set: primary + PiP thumbnails). The
   * robot negotiated every camera up front, so switching between non-empty sets is a no-reneg START
   * (instant). Going to an EMPTY set releases the peer entirely (a map-only view costs zero streaming);
   * coming back from empty is a fresh handshake, since the released peer can't be renegotiated.
   * @param {string[]} names camera names the robot keys on, in m-line order
   */
  setActiveCameras(names) {
    const next = [...names];
    if (next.length === this.#activeCams.length && next.every((n, i) => n === this.#activeCams[i])) return;
    const wasEmpty = this.#activeCams.length === 0;
    this.#activeCams = next;
    if (!this.#started || this.#ros.state !== "connected") return;
    // Empty set, returning from one, or no live pc → (re)handshake, which also handles the release case.
    // A normal switch between non-empty sets stays reneg-free.
    if (next.length === 0 || wasEmpty || !this.#pc) {
      this.#handshakeAttempts = 0;
      this.#handshake();
      return;
    }
    this.#ros.publish(WEBRTC_START_TOPIC, {
      data: JSON.stringify({ source: "live", video: next, audio: this.#state.audioRequested, client_id: this.#clientId }),
    });
    console.log("[webrtc] active cameras ->", next.join("+"), "(no reconnect)");
  }

  /**
   * Choose which active camera fills the big stage. Both old and new primary are already in the active
   * set (already streaming), so this is purely a display swap — no START, instant.
   * @param {number} index m-line index of the camera (video0 -> 0, …)
   * @param {string} name camera name (for diagnostics)
   */
  setPrimaryCamera(index, name) {
    if (this.#primaryIndex === index) return;
    this.#primaryIndex = index;
    this.#primaryName = name;
    // Show the now-primary track immediately; if it isn't live yet the previous frame/overlay holds until
    // its `unmute` lands (showLive patches it then).
    const stream = this.#videoStreams[index] ?? null;
    if (stream) this.#patch({ videoStream: stream, status: this.#videoLive[index] ? "streaming" : "connecting" });
    console.log("[webrtc] primary camera " + name + " (index " + index + ", no reconnect)");
  }

  /** @returns {{ index: number, name: string }} the currently displayed (big) camera */
  get primaryCamera() {
    return { index: this.#primaryIndex, name: this.#primaryName };
  }

  // ---- handshake ----------------------------------------------------------

  #handshake() {
    // Map-only view: no cameras (and no mic) requested. Tear the peer down and stay idle — the robot
    // releases its side on an empty START, and arming a watchdog for media that will never come would
    // just thrash. The map runs over rosbridge, so it's unaffected. (Mic-only still builds a peer below.)
    if (this.#activeCams.length === 0 && !this.#state.audioRequested) {
      this.#closePc();
      this.#videoLive = [];
      this.#patch({ status: "idle", videoStream: null, ...this.#videoArrays() });
      if (this.#ros.state === "connected") {
        this.#ros.publish(WEBRTC_START_TOPIC, {
          data: JSON.stringify({ source: "live", video: [], audio: false, client_id: this.#clientId }),
        });
      }
      console.log("[webrtc] no streams requested — peer released (map-only)");
      return;
    }

    this.#closePc();
    // Keep the last video frame on screen during the rebuild, but always drop
    // the audio stream: a dead mic track has no freeze-frame value, and
    // keeping it would mask a rebuilt connection that came up silent.
    if (this.#state.audioStream) this.#patch({ audioStream: null });
    if (!this.#state.videoStream) this.#patch({ status: "connecting" });

    if (this.#ros.state !== "connected") return; // resumes on reconnect

    const pc = createLocalPeerConnection(this.#ros.ip, { fallback: this.#useFallbackStun });
    this.#pc = pc;
    this.#builtWithAudio = this.#state.audioRequested;
    wireDiagnosticDataChannels(pc);
    this.#startJitterPoll();

    pc.onicecandidate = (event) => {
      if (this.#pc !== pc || !event.candidate) return;
      const c = event.candidate;
      // Log every candidate we send the robot: type (host/srflx/relay) + address — `.local` means Chrome
      // obfuscated a host IP behind an mDNS name (the robot must peer-reflexive past it).
      console.log(
        "[webrtc] ice ->",
        describeIceCandidate(c),
      );
      this.#ros.publish(WEBRTC_ICE_IN_TOPIC, {
        data: JSON.stringify({
          client_id: this.#clientId, // envelope: the robot routes our ICE by client_id (independent peer)
          candidate: event.candidate.candidate,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
          sdpMid: event.candidate.sdpMid,
        }),
      });
    };

    pc.ontrack = (event) => {
      if (this.#pc !== pc) return;
      this.#onTrack(event, pc);
    };

    pc.oniceconnectionstatechange = () => {
      if (this.#pc !== pc) return;
      const s = pc.iceConnectionState;
      console.log("[webrtc] ice:", s);
      this.#patch({ iceState: s });
      if (s === "connected" || s === "completed") {
        this.#clearDegradeTimer();
      } else if (s === "failed") {
        // No working candidate pair — turn on the public-STUN fallback so the degrade-timer rebuild below
        // can learn an srflx candidate even when the robot's own STUN responder isn't reachable.
        this.#enableStunFallback();
        // ICE exhausted every candidate pair without finding a working one = NO NETWORK PATH between this
        // browser and the robot (e.g. the robot can't reach our host candidates — mDNS-obfuscated on a
        // network where they don't resolve — and srflx/NAT-hairpin didn't work either).
        console.error(
          "[webrtc] NO USABLE NETWORK PATH to the robot — ICE failed (no candidate pair connected). " +
            "If you're on the same LAN, your browser may be hiding its local IP via mDNS; the robot " +
            "couldn't open a return path. Workaround: set media.peerconnection.ice.obfuscate_host_addresses=false.",
        );
        this.#startDegradeTimer();
      } else if (s === "disconnected") {
        this.#startDegradeTimer();
      }
    };
    pc.onconnectionstatechange = () => {
      if (this.#pc === pc) console.log("[webrtc] connection:", pc.connectionState);
    };

    // Phase 1: expect an SDP offer back within OFFER_TIMEOUT_MS. If none
    // arrives the START (or its broadcast offer) was dropped — republish fast.
    // Re-armed to the longer MEDIA_TIMEOUT_MS in #onOffer once we've answered.
    this.#armWatchdog(OFFER_TIMEOUT_MS);

    // Ask the robot to push just the selected camera. It still negotiates every camera's transceiver for
    // a client_id peer (so switching is reneg-free), but won't encode/send the others until we request
    // them — so we don't waste its bandwidth/CPU on cameras we're not viewing.
    this.#ros.publish(WEBRTC_START_TOPIC, {
      data: JSON.stringify({
        source: "live",
        audio: this.#state.audioRequested,
        client_id: this.#clientId,
        renegotiate: true,
        video: this.#activeCams,
      }),
    });
    console.log("[webrtc] handshake: START sent", { client_id: this.#clientId, audio: this.#builtWithAudio });
  }

  /**
   * @param {RTCTrackEvent} event
   * @param {RTCPeerConnection} pc owning connection — ignore events from a
   *   superseded pc whose stopped tracks may still fire mute/unmute.
   */
  #onTrack(event, pc) {
    const track = event.track;
    console.log("[webrtc] track:", track.kind, "mid=" + (event.transceiver?.mid ?? "?"));
    if (track.kind === "audio") {
      // Start in the operator's chosen state; never audible by default.
      // NB: deliberately not tuned below — zeroing the audio receiver's NetEq
      // buffer starves mic audio under jitter, and the latency win is video-only.
      track.enabled = this.#state.audioRequested;
      this.#patch({ audioStream: new MediaStream([track]) });
      return;
    }

    // Start the video receiver's jitter buffer at this pc's last-known target (0
    // until the first RTT sample — lowest latency, right on LAN), not a hard 0: a
    // track arriving after the path was measured must not reset to an unbuffered
    // state the adapt poll already rejected. Units differ: jitterBufferTarget is
    // in milliseconds, playoutDelayHint in seconds (a 40ms target is 40 vs 0.04).
    // Modern Chrome honors jitterBufferTarget and ignores the hint (not a strict
    // fallback — both are set whenever present).
    const receiver = event.receiver;
    if (receiver) {
      try {
        if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = this.#lastJitterTargetMs;
        if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = this.#lastJitterTargetMs / 1000;
      } catch {
        // unsupported; default buffer applies
      }
    }

    const stream = new MediaStream([track]);
    // m-line index: "video0"/"0" -> 0, "video1"/"1" -> 1, … We keep every camera's stream by index. The
    // active set renders live (primary big + PiP thumbnails); the rest stay warm (negotiated) but unpushed.
    const mid = event.transceiver?.mid ?? "";
    const m = /(\d+)$/.exec(mid);
    const index = m ? Number(m[1]) : this.#videoTrackCount;
    this.#videoTrackCount += 1;
    this.#videoStreams[index] = stream;
    this.#videoLive[index] = false;

    // A remote track arrives muted and unmutes when RTP actually flows — only then is there a real frame.
    // We track liveness per camera so the PiP strip can show each thumbnail's state; the big stage only
    // swaps to a stream once it's genuinely live (else the cold-start overlay / previous freeze-frame holds).
    const showLive = () => {
      if (this.#pc !== pc) return; // stale pc
      this.#videoLive[index] = true;
      if (index === this.#primaryIndex) {
        // The displayed camera went live: clear the handshake watchdog and reveal it on the big stage.
        this.#handshakeAttempts = 0;
        this.#clearWatchdog();
        console.log("[webrtc] primary video live (camera index " + index + ")");
        this.#patch({ videoStream: stream, status: "streaming", ...this.#videoArrays() });
      } else {
        this.#patchVideo(); // a PiP thumbnail came up
      }
    };

    if (!track.muted) showLive();
    track.addEventListener("unmute", showLive);
    track.addEventListener("mute", () => {
      if (this.#pc !== pc || !this.#started) return;
      this.#videoLive[index] = false;
      // Media stalled mid-stream — keep the last good frame frozen. For the primary, flag connecting so the
      // stage degrades it (the degrade/handshake timers drive recovery); for a PiP, just mark it not-live.
      if (index === this.#primaryIndex && this.#state.videoStream === stream) {
        this.#patch({ status: "connecting", ...this.#videoArrays() });
      } else {
        this.#patchVideo();
      }
    });
  }

  /** Fresh copies of the per-camera stream/liveness arrays, so every patch emits a new reference. */
  #videoArrays() {
    return { videoStreams: this.#videoStreams.slice(), videoLive: this.#videoLive.slice() };
  }

  /** Re-emit the per-camera stream/liveness arrays so the PiP strip refreshes (no status change). */
  #patchVideo() {
    this.#patch(this.#videoArrays());
  }

  /** @param {any} payload /webrtc/offer_id message: std_msgs/String whose data is {client_id, sdp} */
  async #onOffer(payload) {
    const raw = payload?.data ?? payload?.msg?.data;
    if (typeof raw !== "string" || !raw) return;
    let env;
    try { env = JSON.parse(raw); } catch { return; }
    if (env.client_id !== this.#clientId) return; // an offer for some other device's peer
    const sdp = env.sdp;
    const pc = this.#pc;
    if (typeof sdp !== "string" || !sdp || !pc) return;
    if (this.#processingOffer || pc.signalingState !== "stable") return;

    this.#processingOffer = true;
    try {
      console.log("[webrtc] offer received (" + sdp.length + "B), answering");
      await pc.setRemoteDescription({ type: "offer", sdp });
      if (this.#pc !== pc) return;
      this.#remoteDescriptionSet = true;
      // Offer applied — past the lost-START window; now we're waiting on ICE
      // and media, which deserves a longer leash before we rebuild.
      this.#armWatchdog(MEDIA_TIMEOUT_MS);

      for (const candidate of this.#iceQueue) {
        if (this.#pc !== pc) return;
        try {
          await pc.addIceCandidate(candidate);
        } catch {
          // Malformed/stale candidates are common and harmless.
        }
      }
      this.#iceQueue = [];

      const answer = await pc.createAnswer();
      if (this.#pc !== pc) return;
      await pc.setLocalDescription(answer);
      if (this.#pc !== pc) return;

      this.#ros.publish(WEBRTC_ANSWER_TOPIC, {
        data: JSON.stringify({ client_id: this.#clientId, sdp: answer.sdp ?? "" }),
      });
      console.log("[webrtc] answer sent");
    } catch (err) {
      if (this.#pc === pc) console.error("[webrtc] offer processing failed:", err);
    } finally {
      // Brief guard so a duplicate offer broadcast doesn't double-process.
      setTimeout(() => {
        this.#processingOffer = false;
      }, OFFER_GUARD_RESET_MS);
    }
  }

  /** @param {any} payload /webrtc/ice_out_id message: std_msgs/String whose data is {client_id, candidate, ...} */
  async #onIceOut(payload) {
    const raw = payload?.data ?? payload?.msg?.data;
    const pc = this.#pc;
    if (typeof raw !== "string" || !raw || !pc) return;
    try {
      const parsed = JSON.parse(raw);
      if (parsed.client_id !== this.#clientId) return; // a candidate for some other device's peer
      if (!parsed.candidate) return;
      /** @type {RTCIceCandidateInit} */
      const candidate = {
        candidate: String(parsed.candidate),
        sdpMLineIndex: parsed.sdpMLineIndex ?? 0,
        sdpMid: parsed.sdpMid ?? undefined,
      };
      if (!this.#remoteDescriptionSet) {
        this.#iceQueue.push(candidate);
      } else {
        await pc.addIceCandidate(candidate);
      }
    } catch {
      // ICE parse/add failures are usually transient; the next candidate wins.
    }
  }

  // ---- timers & teardown --------------------------------------------------

  #startDegradeTimer() {
    if (this.#degradeTimer !== null) return;
    this.#degradeTimer = setTimeout(() => {
      this.#degradeTimer = null;
      if (this.#started) {
        // Connected briefly then lost the path (or never got media). Repeated rebuilds here usually mean
        // the robot can't keep a return path to this browser — see the NO USABLE NETWORK PATH note above.
        console.warn("[webrtc] no stable media path for 10s (robot may have no route back to us), rebuilding");
        this.#handshake();
      }
    }, ICE_DEGRADE_MS);
  }

  /** Latch the public-STUN fallback on (sticky until stop()); the next #handshake() rebuilds with it. */
  #enableStunFallback() {
    if (this.#useFallbackStun) return;
    this.#useFallbackStun = true;
    console.warn("[webrtc] enabling public STUN fallback for subsequent rebuilds");
    this.#patch({ stunFallback: true });
  }

  #clearDegradeTimer() {
    if (this.#degradeTimer !== null) {
      clearTimeout(this.#degradeTimer);
      this.#degradeTimer = null;
    }
  }

  /**
   * Arm (or re-arm) the handshake watchdog. On fire, escalate: rebuild the pc
   * up to MAX_HANDSHAKE_ATTEMPTS times, then surface an error for manual retry.
   * A freeze-frame stream survives the rebuilds (status stays "streaming"); a
   * cold start shows "establishing video link" throughout.
   * @param {number} ms
   */
  #armWatchdog(ms) {
    this.#clearWatchdog();
    this.#watchdog = setTimeout(() => {
      this.#watchdog = null;
      this.#handshakeAttempts += 1;
      if (this.#handshakeAttempts < MAX_HANDSHAKE_ATTEMPTS) {
        // The first attempt used the local-only config; escalate the rebuilds onto the public-STUN
        // fallback in case the missing media is an unreachable robot STUN responder (no srflx candidate).
        this.#enableStunFallback();
        console.warn(`[webrtc] no media yet (attempt ${this.#handshakeAttempts}), rebuilding`);
        this.#handshake();
      } else {
        console.error("[webrtc] no media after repeated handshakes");
        this.#closePc();
        this.#patch({ status: "error" });
      }
    }, ms);
  }

  #clearWatchdog() {
    if (this.#watchdog !== null) {
      clearTimeout(this.#watchdog);
      this.#watchdog = null;
    }
  }

  #clearAudioDebounce() {
    if (this.#audioDebounce !== null) {
      clearTimeout(this.#audioDebounce);
      this.#audioDebounce = null;
    }
  }

  #startJitterPoll() {
    this.#clearJitterPoll();
    // #lastJitterTargetMs deliberately carries over from the previous pc: a rebuild talks to the
    // same robot over the same path, so its tracks attach already buffered (#onTrack) instead of
    // replaying the unbuffered first seconds. A stale value costs one fast poll to correct.
    this.#lastVideoPackets = { received: 0, lost: 0 };
    const pc = this.#pc;
    const tick = async () => {
      if (this.#pc !== pc) return; // superseded mid-flight; don't re-arm a dead chain
      const measured = await this.#adaptJitterBuffer();
      if (this.#pc !== pc) return;
      this.#jitterPoll = setTimeout(tick, measured ? JITTER_POLL_MS : JITTER_POLL_FAST_MS);
    };
    this.#jitterPoll = setTimeout(tick, JITTER_POLL_FAST_MS);
  }

  #clearJitterPoll() {
    if (this.#jitterPoll !== null) {
      clearTimeout(this.#jitterPoll);
      this.#jitterPoll = null;
    }
  }

  // Clean fast path (LAN-ish RTT, ~no loss) → 0. Otherwise ~1.2×RTT + 30ms, capped at 500,
  // so a NACK retransmit lands before playout. 20ms hysteresis avoids churning the decoder.
  // Returns whether an RTT sample was available, so #startJitterPoll keeps the fast cadence
  // until the path is measured.
  async #adaptJitterBuffer() {
    const pc = this.#pc;
    if (!pc) return false;
    const stats = await pc.getStats().catch(() => null);
    if (!stats || this.#pc !== pc) return false;

    /** @type {number | null} */ let rttMs = null;
    const packets = { received: 0, lost: 0 };
    stats.forEach((s) => {
      if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded") {
        if (typeof s.currentRoundTripTime === "number") rttMs = s.currentRoundTripTime * 1000;
      } else if (s.type === "inbound-rtp" && s.kind === "video") {
        packets.received += s.packetsReceived ?? 0;
        packets.lost += s.packetsLost ?? 0;
      }
    });
    const dReceived = packets.received - this.#lastVideoPackets.received;
    const dLost = packets.lost - this.#lastVideoPackets.lost;
    this.#lastVideoPackets = packets;
    if (rttMs === null) return false;

    const loss = dLost > 0 ? dLost / (dReceived + dLost) : 0;
    const target = rttMs < 60 && loss < 0.005 ? 0 : Math.min(500, Math.round(rttMs * 1.2 + 30));
    if (Math.abs(target - this.#lastJitterTargetMs) <= 20) return true;
    this.#lastJitterTargetMs = target;
    for (const receiver of pc.getReceivers()) {
      if (receiver.track?.kind !== "video") continue; // track is null on stopped receivers; a throw here would kill the poll chain
      try {
        if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = target;
        if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = target / 1000; // seconds (see #onTrack)
      } catch {
        // unsupported; default buffer applies
      }
    }
    console.log(`[webrtc] jitter target -> ${target}ms (rtt ${Math.round(rttMs)}ms, loss ${(loss * 100).toFixed(1)}%)`);
    return true;
  }

  #closePc() {
    this.#clearWatchdog();
    this.#clearDegradeTimer();
    this.#clearJitterPoll();
    this.#processingOffer = false;
    this.#remoteDescriptionSet = false;
    this.#iceQueue = [];
    this.#videoTrackCount = 0;
    const pc = this.#pc;
    this.#pc = null;
    if (pc) {
      pc.onicecandidate = null;
      pc.ontrack = null;
      pc.oniceconnectionstatechange = null;
      pc.close();
    }
  }

  /** @param {Partial<WebRtcState>} patch */
  #patch(patch) {
    this.#state = { ...this.#state, ...patch };
    for (const cb of [...this.#listeners]) {
      try {
        cb(this.#state);
      } catch (err) {
        console.error("[webrtc] listener threw:", err);
      }
    }
  }
}
