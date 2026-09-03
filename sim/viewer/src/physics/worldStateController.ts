// Ground-truth state feed for the sim viewer: the world server broadcasts
// {t, wall, world_epoch, pose, joints, objects} per physics slice on its observer WebSocket
// (proxied at /worldstate) -- the 3D view's only state source. See
// world_server.py "two interfaces".

import type { PropInfo } from "../props";

/** What a challenge IS: sent once per connection, like the prop roster,
 * because none of it changes while the server runs (challenges.py roster). */
export interface ChallengeInfo {
  id: string;
  title: string;
  brief: string;
}

/** A challenge's persisted record (workspace/challenges.json). */
export interface ChallengeProgress {
  passed: boolean;
  best_time_s: number | null;
  attempts: number;
}

/** The currently running (or just finished) challenge. */
export interface ChallengeActive {
  id: string;
  state: "running" | "passed" | "failed";
  reason: string;
  elapsed_s: number;
  time_limit_s: number | null;
  goals: { label: string; done: boolean }[];
}

/** The world server's challenge judge state (challenges.py) embedded in every
 * state broadcast -- only the parts that can change, keyed by challenge id
 * for the ones that were attempted. */
export interface ChallengeBlock {
  progress: Record<string, ChallengeProgress>;
  active: ChallengeActive | null;
}

export interface WorldState {
  /** Sim clock (s) -- the playback timeline. */
  t: number;
  /** Server wall clock (s): Date.now()/1000 - wall = true delivery lag. */
  wall: number;
  /** Increments on every simulator reset, including challenge and NaN resets. */
  worldEpoch: number;
  x: number;
  y: number;
  yaw: number;
  joints: Record<string, number>;
  /** Ground truth of every manipulation prop (world.py GRASP_OBJECTS), keyed
   * by name: [x, y, z, qw, qx, qy, qz]. Empty on servers that predate them. */
  objects: Record<string, number[]>;
  /** Challenge judge state; null on servers that predate it. */
  challenge: ChallengeBlock | null;
}

export interface WorldEnvironment {
  id: string;
  fingerprint: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteTuple3(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

export class WorldStateController {
  onState?: (state: WorldState) => void;
  /** The prop roster, sent once per connection (props.py sidecars). */
  onProps?: (props: PropInfo[]) => void;
  /** The challenge roster, sent in the same opening frame (challenges.py). */
  onChallenges?: (challenges: ChallengeInfo[]) => void;
  /** Pack identity from the opening roster frame. */
  onEnvironment?: (environment: WorldEnvironment) => void;
  /** Socket availability across the world-server restart. */
  onConnectionChange?: (connected: boolean) => void;

  #url: string;
  #ws!: WebSocket;
  #open: Promise<void>;
  #resolveOpen!: () => void;
  #rejectOpen!: (err: Error) => void;
  #everOpened = false;
  #disposed = false;
  #retryMs = 500;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(url: string) {
    this.#url = url;
    this.#open = new Promise((resolve, reject) => {
      this.#resolveOpen = resolve;
      this.#rejectOpen = reject;
    });
    this.#connect();
  }

  /** (Re)open the socket (no handshake; the server just pushes). On drop,
   * retry with backoff until dispose(). */
  #connect(): void {
    if (this.#disposed) return;
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      // A fresh transport has not proved its world identity yet. This also
      // prevents a reconnect from momentarily reusing the previous roster.
      this.onConnectionChange?.(false);
      this.#everOpened = true;
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt; reconnection continues.
      if (!this.#everOpened) this.#rejectOpen(new Error(`world state connection failed: ${this.#url}`));
    };
    ws.onclose = () => {
      this.onConnectionChange?.(false);
      if (this.#disposed) return;
      this.#retryTimer = setTimeout(() => {
        this.#retryTimer = null;
        this.#connect();
      }, this.#retryMs);
      this.#retryMs = Math.min(this.#retryMs * 2, 5000);
    };
    ws.onmessage = (ev) => this.#onMessage(ev.data as string);
  }

  async init(): Promise<void> {
    await this.#open;
  }

  /** Send a stage command (e.g. place_group) back up the observer socket.
   * Dropped silently while the socket is (re)connecting -- it is a button
   * press, not something worth queueing. */
  send(cmd: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(cmd));
  }

  dispose(): void {
    this.#disposed = true;
    if (this.#retryTimer !== null) clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
    this.#ws.close();
  }

  #onMessage(raw: string): void {
    let decoded: unknown;
    try {
      decoded = JSON.parse(raw) as unknown;
    } catch {
      console.warn("[sim-viewer] ignoring malformed world-state JSON");
      return;
    }
    if (!isRecord(decoded)) {
      console.warn("[sim-viewer] ignoring non-object world-state JSON");
      return;
    }
    const parsed = decoded;
    if ("props" in parsed || "challenges" in parsed || "environment" in parsed) {
      // Roster frame, not a state frame: it has no clock and arrives once,
      // ahead of the stream (see world_server.serve_state).
      const rawEnvironment = parsed.environment;
      const environment =
        isRecord(rawEnvironment) &&
        typeof rawEnvironment.id === "string" &&
        rawEnvironment.id &&
        typeof rawEnvironment.fingerprint === "string" &&
        rawEnvironment.fingerprint
          ? { id: rawEnvironment.id, fingerprint: rawEnvironment.fingerprint }
          : null;
      let validRoster = environment !== null;
      let props: PropInfo[] | undefined;
      let challenges: ChallengeInfo[] | undefined;
      if ("props" in parsed) {
        if (Array.isArray(parsed.props)) props = parsed.props as PropInfo[];
        else validRoster = false;
      }
      if ("challenges" in parsed) {
        if (Array.isArray(parsed.challenges)) challenges = parsed.challenges as ChallengeInfo[];
        else validRoster = false;
      }
      if (!validRoster || environment === null) {
        // Nothing in an opening frame is observable until the whole frame has
        // validated. Otherwise a bad roster could partially replace the
        // environment, props, or challenges before this one-shot socket closes.
        this.#ws.close(1002, "invalid world-state roster");
        return;
      }

      this.onEnvironment?.(environment);
      if (props !== undefined) this.onProps?.(props);
      if (challenges !== undefined) this.onChallenges?.(challenges);
      // A TCP/WebSocket open is not enough after a restart. Publish connected
      // only after identity and every roster have been installed.
      this.#retryMs = 500;
      this.onConnectionChange?.(true);
      return;
    }
    if (
      typeof parsed.t !== "number" ||
      !Number.isFinite(parsed.t) ||
      typeof parsed.wall !== "number" ||
      !Number.isFinite(parsed.wall) ||
      !finiteTuple3(parsed.pose) ||
      !isRecord(parsed.joints) ||
      Object.values(parsed.joints).some((value) => typeof value !== "number" || !Number.isFinite(value)) ||
      (parsed.objects !== undefined && parsed.objects !== null && !isRecord(parsed.objects))
    ) {
      console.warn("[sim-viewer] ignoring malformed world-state frame");
      return;
    }
    const joints = { ...(parsed.joints as Record<string, number>) };
    // joint6M: the gripper's mirrored finger (URDF mimic of joint6, x-1).
    joints["joint6M"] = -(joints["joint6"] ?? 0);
    this.onState?.({
      t: parsed.t,
      wall: parsed.wall,
      worldEpoch: Number.isInteger(parsed.world_epoch) && (parsed.world_epoch as number) >= 0 ? (parsed.world_epoch as number) : 0,
      x: parsed.pose[0],
      y: parsed.pose[1],
      yaw: parsed.pose[2],
      joints,
      objects: (parsed.objects as Record<string, number[]> | null | undefined) ?? {},
      challenge: (parsed.challenge as ChallengeBlock | null | undefined) ?? null,
    });
  }
}
