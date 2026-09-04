// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// RosClient — the single rosbridge (rws) WebSocket shared by the whole page.
// Ported from the mobile app's useWebSocketManager: pending service-call map
// with timeouts, per-topic handler registry, subscribe retry with backoff on
// rws "Topic <t> not found" errors, and a publish-zero-on-hide drive safety.
// Web-specific additions: automatic reconnect with backoff after an
// unexpected close (the socket replays every active subscription on reopen,
// so consumers never notice), and localStorage for the last-connected IP.

import { JOYSTICK_TOPIC, LAST_IP_KEY, ROSBRIDGE_PORT } from "./constants.js";

const SERVICE_TIMEOUT_MS = 10_000;
const CONNECT_TIMEOUT_MS = 8_000;
const RECONNECT_BASE_MS = 1_000;
// Orphaned-goal backlog cap: enough for every plausibly-live goal across a
// blip; anything older has long since terminated server-side.
const MAX_ORPHANED_GOALS = 8;
const RECONNECT_CAP_MS = 10_000;
const SUB_RETRY_CAP_MS = 30_000;
// Ping/pong is invisible to page scripts, so a half-open socket (phone asleep,
// WiFi roamed) reads as a healthy idle one until the OS TCP timeout. The /ws
// proxy fills the silence every 20s; three missed means the socket is gone.
const STALE_SOCKET_MS = 70_000;
const STALE_CHECK_MS = 15_000;

/**
 * True when a value carries no actual data: null/undefined, or an object/array
 * whose leaves are all empty ({success: false} still counts as data).
 * @param {any} v
 * @returns {boolean}
 */
function isEmptyDeep(v) {
  if (v == null) return true;
  if (typeof v !== "object") return false;
  return Object.values(v).every(isEmptyDeep);
}

/**
 * rws serializes non-finite floats as empty array slots — a LaserScan with
 * inf ranges arrives as `[2.48,,,,0.39]`, which strict JSON rejects. Fill
 * each hole with null, walking the string so a `,,` inside a string value is
 * never touched. `[]` keeps its meaning (no hole to fill).
 * Exported for tests (tests/patchJsonHoles.test.js).
 * @param {string} raw
 * @returns {string}
 */
export function patchJsonHoles(raw) {
  let out = "";
  let inString = false;
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (inString) {
      out += ch;
      if (ch === "\\") out += raw[++i] ?? "";
      else if (ch === '"') inString = false;
    } else if (ch === '"') {
      inString = true;
      out += ch;
    } else if (ch === "[" && raw[i + 1] === ",") {
      out += "[null";
    } else if (ch === "," && (raw[i + 1] === "," || raw[i + 1] === "]")) {
      out += ",null";
    } else {
      out += ch;
    }
  }
  return out;
}

/**
 * @typedef {Object} Subscription
 * @property {Set<(msg: any) => void>} handlers
 * @property {number | undefined} throttleRate
 * @property {string | undefined} type
 * @property {number} retryCount
 * @property {number | null} retryTimer
 */

export class RosClient {
  /** @type {WebSocket | null} */ #ws = null;
  /** @type {ConnState} */ #state = "disconnected";
  /** @type {string | null} */ #ip = null;
  /** @type {Set<(state: ConnState, ip: string | null) => void>} */ #stateListeners = new Set();
  /** @type {Map<string, Subscription>} */ #subs = new Map();
  /** Advertised publish topics, kept for replay on reconnect. @type {Map<string, string>} */ #advertised = new Map();
  /** @type {Map<string, { resolve: (v: any) => void, reject: (e: Error) => void, timer: number }>} */ #pendingCalls = new Map();
  /** @type {Map<string, { resolve: (v: any) => void, reject: (e: Error) => void, onFeedback?: (v: any) => void, action: string, goalId: string, cancelRequested: boolean }>} */ #pendingActions = new Map();
  /** Goals in flight when the socket died, cancelled on the next reconnect. @type {Array<{ id: string, action: string, goalId: string }>} */ #orphanedGoals = [];
  #callCounter = 0;
  #reconnectDelay = RECONNECT_BASE_MS;
  /** @type {number | null} */ #reconnectTimer = null;
  /** @type {number | null} */ #connectTimer = null;
  /** @type {number | null} */ #staleTimer = null;
  /** Timestamp of the newest frame off the socket; drives the stale check. */ #lastFrameAt = 0;
  /** Whether this socket is one the keepalive reaches — see #dropIfStale. */ #keepaliveExpected = false;
  /** True once the current target IP has had a successful open; gates the
   * reconnect-forever loop so a typo'd address fails fast instead. */
  #everConnected = false;

  constructor() {
    // Safety net: a hidden or closing page must never leave the robot
    // driving on a latched vector. DriveController halts its own state on the
    // same signals; this raw zero goes out even if that module is broken.
    const publishZero = () => {
      if (this.#state === "connected") {
        this.publish(JOYSTICK_TOPIC, { x: 0, y: 0 });
      }
    };
    window.addEventListener("pagehide", publishZero);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        publishZero();
        return;
      }
      // Background tabs freeze timers, so the periodic check may never have run.
      this.#dropIfStale();
    });
  }

  /** Force a reconnect once the keepalive has gone missing — closing is enough,
   *  onclose runs the normal replay-and-refetch path. */
  #dropIfStale() {
    if (!this.#keepaliveExpected || this.#state !== "connected") return;
    if (Date.now() - this.#lastFrameAt < STALE_SOCKET_MS) return;
    console.warn("[ros] socket silent past the keepalive budget — reconnecting");
    this.#teardownSocket();
    this.#handleClose();
  }

  /** @returns {ConnState} */
  get state() {
    return this.#state;
  }

  /** @returns {string | null} */
  get ip() {
    return this.#ip;
  }

  /** Last successfully connected address, or null. */
  get lastIp() {
    return localStorage.getItem(LAST_IP_KEY);
  }

  /**
   * Subscribe to connection-state changes. The callback fires immediately
   * with the current state, then on every transition.
   * @param {(state: ConnState, ip: string | null) => void} cb
   * @returns {() => void} unsubscribe
   */
  onStateChange(cb) {
    this.#stateListeners.add(cb);
    cb(this.#state, this.#ip);
    return () => this.#stateListeners.delete(cb);
  }

  /** @param {string} ip Hostname or IP of the robot (port is fixed). */
  connect(ip) {
    if (!ip) return;
    if (this.#ws && this.#ip === ip && (this.#state === "connected" || this.#state === "connecting")) {
      return;
    }
    this.#clearReconnect();
    this.#teardownSocket();
    this.#ip = ip;
    this.#everConnected = false;
    this.#open("connecting");
  }

  /** Intentional close: no reconnect, pending calls rejected. */
  disconnect() {
    this.#clearReconnect();
    this.#teardownSocket();
    this.#rejectAllPending(new Error("Disconnected"));
    this.#everConnected = false;
    this.#setState("disconnected");
  }

  /**
   * Fire-and-forget publish. Returns false when the socket is down instead of
   * pretending a frame was accepted; callers that own transactional UI state
   * can keep that state pending and offer a retry.
   * @param {string} topic
   * @param {object} msg
   * @returns {boolean}
   */
  publish(topic, msg) {
    return this.#send({ op: "publish", topic, msg });
  }

  /**
   * Declare a topic's type before publishing to it. A bare publish resolves the
   * type against the live graph, which fails when the only subscriber appears
   * later. Re-sent on reconnect until unadvertised.
   * @param {string} topic
   * @param {string} type e.g. "std_msgs/msg/String"
   * @returns {() => void} unadvertise
   */
  advertise(topic, type) {
    this.#advertised.set(topic, type);
    this.#send({ op: "advertise", topic, type });
    return () => {
      if (!this.#advertised.delete(topic)) return;
      this.#send({ op: "unadvertise", topic });
    };
  }

  /**
   * @param {string} service
   * @param {object} [args]
   * @param {number} [timeoutMs]
   * @returns {Promise<any>} resolves with the response `values`
   */
  callService(service, args = {}, timeoutMs = SERVICE_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      if (this.#state !== "connected" || !this.#ws) {
        reject(new Error(`Not connected; cannot call ${service}`));
        return;
      }
      const id = `call_${this.#callCounter++}_${service.replace(/[^a-zA-Z0-9]/g, "_")}`;
      const timer = setTimeout(() => {
        if (this.#pendingCalls.delete(id)) {
          reject(new Error(`Service call ${service} timed out after ${timeoutMs}ms`));
        }
      }, timeoutMs);
      this.#pendingCalls.set(id, { resolve, reject, timer });
      this.#send({ op: "call_service", id, service, args });
    });
  }

  /**
   * Send a goal to a ROS action (rws action protocol over the shared socket).
   * Long-running and cancelable, with streamed feedback — the right primitive
   * for skill execution. The result `values` (e.g. {success, message, ...}) is
   * resolved when the action terminates; onFeedback fires for each interim
   * `action_feedback`.
   * @param {string} action e.g. "/execute_skill"
   * @param {string} actionType e.g. "brain_messages/action/ExecuteSkill"
   * @param {object} [args] Goal fields.
   * @param {{ onFeedback?: (values: any) => void }} [opts]
   * @returns {{ promise: Promise<any>, cancel: () => void }}
   */
  sendActionGoal(action, actionType, args = {}, opts = {}) {
    const id = `act_${this.#callCounter++}_${action.replace(/[^a-zA-Z0-9]/g, "_")}`;
    const goalId = `${id}_${Date.now()}`;
    let settled = false;
    const promise = new Promise((resolve, reject) => {
      if (this.#state !== "connected" || !this.#ws) {
        reject(new Error(`Not connected; cannot run ${action}`));
        return;
      }
      this.#pendingActions.set(id, {
        resolve: (v) => {
          settled = true;
          resolve(v);
        },
        reject: (e) => {
          settled = true;
          reject(e);
        },
        onFeedback: opts.onFeedback,
        action,
        // rws assigns the authoritative goal_id and echoes it on feedback/result;
        // cancel must reference THAT, not our client-side one. Seed with ours and
        // upgrade when the server's arrives.
        goalId,
        cancelRequested: false,
      });
      this.#send({
        op: "send_action_goal",
        id,
        action,
        action_type: actionType,
        args,
        feedback: true,
        goal_id: goalId,
      });
    });
    const cancel = () => {
      const pending = this.#pendingActions.get(id);
      if (settled || !pending) return;
      pending.cancelRequested = true;
      this.#send({ op: "cancel_action_goal", id, action, goal_id: pending.goalId });
    };
    return { promise, cancel };
  }

  /**
   * Ref-counted subscribe. The first handler for a topic sends the subscribe
   * op; the last one leaving sends unsubscribe. Active topics are replayed
   * automatically after every reconnect, and retried with backoff when rws
   * reports the publisher doesn't exist yet.
   * @param {string} topic
   * @param {(msg: any) => void} handler Receives the message payload
   *   (`frame.msg`, falling back to the whole frame for rws variants).
   * @param {number} [throttleRate] Server-side throttle in ms.
   * @param {string} [type] Message type (e.g. "nav_msgs/msg/Path") -- required
   *   for topics that may not be published yet, or rws errors on every retry.
   * @returns {() => void} unsubscribe
   */
  subscribe(topic, handler, throttleRate, type) {
    let sub = this.#subs.get(topic);
    if (!sub) {
      sub = { handlers: new Set(), throttleRate, type, retryCount: 0, retryTimer: null };
      this.#subs.set(topic, sub);
      if (this.#state === "connected") this.#sendSubscribe(topic, sub);
    }
    sub.handlers.add(handler);
    return () => {
      const current = this.#subs.get(topic);
      if (!current || !current.handlers.delete(handler)) return;
      if (current.handlers.size === 0) {
        if (current.retryTimer !== null) clearTimeout(current.retryTimer);
        this.#subs.delete(topic);
        if (this.#state === "connected") this.#send({ op: "unsubscribe", topic });
      }
    };
  }

  // ---- internals ----------------------------------------------------------

  /**
   * True when `ip` points at the same host that served this page, so the
   * connection can ride the same-origin `/ws` proxy. Beyond an exact match,
   * the loopback aliases (localhost / 127.0.0.1 / ::1) name the same machine
   * even though they differ as strings — the case that broke local dev.
   * @param {string} ip
   */
  #isServingHost(ip) {
    if (ip === location.hostname) return true;
    const loopback = new Set(["localhost", "127.0.0.1", "::1"]);
    return loopback.has(ip) && loopback.has(location.hostname);
  }

  /**
   * Through the front door, rosbridge rides the same-origin `/ws` proxy, with
   * the transport matched to the page: wss under https (plain ws:// would be
   * blocked as mixed content), ws under http. A different host means talking
   * to rws directly on its own port.
   * @param {string} ip
   */
  #urlFor(ip) {
    if (this.#isServingHost(ip)) {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      return `${scheme}://${location.host}/ws`;
    }
    return `ws://${ip}:${ROSBRIDGE_PORT}`;
  }

  /** @param {"connecting" | "reconnecting"} phase */
  #open(phase) {
    const ip = this.#ip;
    if (!ip) return;
    this.#setState(phase);
    // A direct rws socket gets no keepalive and is legitimately silent when idle.
    this.#keepaliveExpected = this.#isServingHost(ip);
    let ws;
    try {
      ws = new WebSocket(this.#urlFor(ip));
    } catch (err) {
      console.error("[ros] WebSocket constructor failed:", err);
      this.#handleClose();
      return;
    }
    this.#ws = ws;

    // Browsers can sit in CONNECTING for the full OS TCP timeout; cut an
    // unreachable address short so the UI can report failure quickly.
    this.#connectTimer = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) ws.close();
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      if (this.#ws !== ws) return;
      this.#clearConnectTimer();
      this.#reconnectDelay = RECONNECT_BASE_MS;
      this.#everConnected = true;
      try {
        localStorage.setItem(LAST_IP_KEY, ip);
      } catch {
        // Storage can be unavailable (private mode); connection still works.
      }
      // Re-subscribe BEFORE announcing "connected": #setState runs listeners
      // synchronously, and some (e.g. WebRtcSession) publish a request whose
      // reply rides a topic we must already be subscribed to. Flushing first
      // closes the window where that first reply is dropped.
      for (const [topic, sub] of this.#subs) {
        sub.retryCount = 0;
        this.#sendSubscribe(topic, sub);
      }
      for (const [topic, type] of this.#advertised) {
        this.#send({ op: "advertise", topic, type });
      }
      // Cancel goals orphaned by the previous socket's death (see
      // #rejectAllPending) so a skill can't keep running with no owner.
      for (const goal of this.#orphanedGoals.splice(0)) {
        this.#send({ op: "cancel_action_goal", id: goal.id, action: goal.action, goal_id: goal.goalId });
      }
      this.#lastFrameAt = Date.now();
      if (this.#keepaliveExpected) {
        this.#staleTimer = setInterval(() => this.#dropIfStale(), STALE_CHECK_MS);
      }
      this.#setState("connected");
    };

    ws.onmessage = (event) => {
      if (this.#ws !== ws) return;
      this.#handleFrame(String(event.data));
    };

    ws.onclose = () => {
      if (this.#ws !== ws) return;
      this.#handleClose();
    };

    ws.onerror = () => {
      // onclose always follows; nothing useful in the error event itself.
    };
  }

  #handleClose() {
    this.#clearConnectTimer();
    this.#clearStaleTimer();
    this.#ws = null;
    this.#rejectAllPending(new Error("Connection closed"));
    for (const sub of this.#subs.values()) {
      if (sub.retryTimer !== null) {
        clearTimeout(sub.retryTimer);
        sub.retryTimer = null;
      }
      sub.retryCount = 0;
    }
    if (!this.#everConnected) {
      // First attempt to this address never opened: fail fast for the
      // connect panel instead of silently retrying a typo forever.
      this.#setState("disconnected");
      return;
    }
    this.#setState("reconnecting");
    const delay = this.#reconnectDelay;
    this.#reconnectDelay = Math.min(this.#reconnectDelay * 2, RECONNECT_CAP_MS);
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      if (this.#state === "reconnecting") this.#open("reconnecting");
    }, delay);
  }

  /** @param {string} raw */
  #handleFrame(raw) {
    this.#lastFrameAt = Date.now();
    /** @type {any} */
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      // rws array holes (non-finite floats) — patch to null and retry
      // (consumers already skip non-finite values). Only attempted once a
      // strict parse has failed.
      try {
        data = JSON.parse(patchJsonHoles(raw));
      } catch {
        return;
      }
    }

    if (data.op === "service_response") {
      const pending = data.id ? this.#pendingCalls.get(data.id) : undefined;
      if (!pending) return;
      this.#pendingCalls.delete(data.id);
      clearTimeout(pending.timer);
      if (data.result === false) {
        pending.reject(new Error(typeof data.values === "string" ? data.values : "Service call failed"));
      } else {
        pending.resolve(data.values);
      }
      return;
    }

    if (data.op === "action_feedback") {
      const pending = data.id ? this.#pendingActions.get(data.id) : undefined;
      if (pending && typeof data.goal_id === "string" && data.goal_id && data.goal_id !== pending.goalId) {
        pending.goalId = data.goal_id;
        // A cancel pressed before the server's goal_id arrived couldn't bind;
        // re-issue it now against the authoritative id.
        if (pending.cancelRequested) {
          this.#send({ op: "cancel_action_goal", id: data.id, action: pending.action, goal_id: pending.goalId });
        }
      }
      pending?.onFeedback?.(data.values);
      return;
    }

    if (data.op === "action_result") {
      const pending = data.id ? this.#pendingActions.get(data.id) : undefined;
      if (!pending) return;
      this.#pendingActions.delete(data.id);
      // rws sets `result: false` for any non-SUCCEEDED goal but the payload may
      // still carry the skill-level outcome — resolve with it. Reject only when
      // nothing came back, checked deeply: NavigateToPose's aborted result is
      // just {result: {}}.
      const values = data.values;
      if (data.result === false && isEmptyDeep(values)) {
        pending.reject(new Error(`Action ${pending.action} was rejected`));
      } else {
        pending.resolve(values);
      }
      return;
    }

    if (typeof data.topic === "string") {
      const sub = this.#subs.get(data.topic);
      if (!sub) return;
      sub.retryCount = 0;
      // rws payload quirk: the message normally rides in `frame.msg`, but
      // some paths deliver fields on the frame itself — pass the frame
      // through so `payload.data` keeps working either way.
      const payload = data.msg ?? data;
      for (const handler of sub.handlers) {
        try {
          handler(payload);
        } catch (err) {
          console.error(`[ros] handler for ${data.topic} threw:`, err);
        }
      }
      return;
    }

    if (data.op === "status" && data.level === "error") {
      const match = typeof data.msg === "string" && data.msg.match(/^Topic (.+?) not found/);
      if (match && this.#subs.has(match[1])) {
        this.#scheduleSubRetry(match[1]);
      } else {
        console.error("[ros] rosbridge error:", data.msg);
      }
    }
  }

  /**
   * Publisher didn't exist yet (subscribe raced node startup): retry with
   * exponential backoff, capped, until the topic appears or we unsubscribe.
   * @param {string} topic
   */
  #scheduleSubRetry(topic) {
    const sub = this.#subs.get(topic);
    if (!sub) return;
    if (sub.retryTimer !== null) clearTimeout(sub.retryTimer);
    const delay = Math.min(1000 * 2 ** sub.retryCount, SUB_RETRY_CAP_MS);
    sub.retryCount += 1;
    sub.retryTimer = setTimeout(() => {
      sub.retryTimer = null;
      if (this.#subs.has(topic) && this.#state === "connected") {
        this.#sendSubscribe(topic, sub);
      }
    }, delay);
  }

  /**
   * @param {string} topic
   * @param {Subscription} sub
   */
  #sendSubscribe(topic, sub) {
    this.#send({
      op: "subscribe",
      topic,
      ...(sub.throttleRate ? { throttle_rate: sub.throttleRate } : {}),
      ...(sub.type ? { type: sub.type } : {}),
    });
  }

  /** @param {object} payload @returns {boolean} */
  #send(payload) {
    if (this.#ws?.readyState === WebSocket.OPEN) {
      this.#ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  /** @param {Error} err */
  #rejectAllPending(err) {
    for (const pending of this.#pendingCalls.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.#pendingCalls.clear();
    for (const [id, pending] of this.#pendingActions) {
      // The socket died but the server-side goal may keep running — with the
      // promise rejected, the caller's cancel handle is dead, so nothing could
      // ever stop it (and the one-skill-at-a-time server would reject every
      // new goal with "another skill is already running"). Remember the goal
      // and best-effort cancel it after the next reconnect: a no-op if the
      // bridge already reaped it, a rescue if it didn't.
      this.#orphanedGoals.push({ id, action: pending.action, goalId: pending.goalId });
      pending.reject(err);
    }
    while (this.#orphanedGoals.length > MAX_ORPHANED_GOALS) this.#orphanedGoals.shift();
    this.#pendingActions.clear();
  }

  #teardownSocket() {
    this.#clearConnectTimer();
    this.#clearStaleTimer();
    const ws = this.#ws;
    this.#ws = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        // Already closed.
      }
    }
  }

  #clearReconnect() {
    if (this.#reconnectTimer !== null) {
      clearTimeout(this.#reconnectTimer);
      this.#reconnectTimer = null;
    }
    this.#reconnectDelay = RECONNECT_BASE_MS;
  }

  #clearStaleTimer() {
    if (this.#staleTimer !== null) {
      clearInterval(this.#staleTimer);
      this.#staleTimer = null;
    }
  }

  #clearConnectTimer() {
    if (this.#connectTimer !== null) {
      clearTimeout(this.#connectTimer);
      this.#connectTimer = null;
    }
  }

  /** @param {ConnState} state */
  #setState(state) {
    if (state === this.#state) return;
    this.#state = state;
    // Snapshot: a listener registered during this emission already saw the
    // current state via onStateChange's immediate fire — visiting it here
    // too would double-deliver (Sets iterate items added mid-loop).
    for (const cb of [...this.#stateListeners]) {
      try {
        cb(state, this.#ip);
      } catch (err) {
        console.error("[ros] state listener threw:", err);
      }
    }
  }
}

/** The one client every module shares. */
export const ros = new RosClient();
