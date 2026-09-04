// Ground-truth state feed for the sim viewer: the world server broadcasts
// {t, wall, pose, joints, objects} per physics slice on its observer WebSocket
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

/** One environment pack as the world server lists it (environments.py). */
export interface EnvironmentSummary {
  id: string;
  display_name: string;
}

/** The loaded pack: its browser assets as the manifest names them (paths
 * under sim/viewer/public, served at /models and /physics) and spawn pose. */
export interface EnvironmentInfo extends EnvironmentSummary {
  viewer: Record<string, string>;
  spawn: [number, number, number];
}

/** The roster frame's environment half: what is loaded, what could be, and a
 * switch in flight or the last one that failed. A server that predates packs
 * sends none of it -- `environment` is null and it is running the apartment. */
export interface EnvironmentRoster {
  environment: EnvironmentInfo | null;
  environments: EnvironmentSummary[];
  switch: (EnvironmentSummary & { state: "loading" | "failed"; message?: string }) | null;
}

export interface WorldState {
  /** Sim clock (s) -- the playback timeline. */
  t: number;
  /** Server wall clock (s): Date.now()/1000 - wall = true delivery lag. */
  wall: number;
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

export class WorldStateController {
  onState?: (state: WorldState) => void;
  /** The prop roster, sent once per connection (props.py sidecars). */
  onProps?: (props: PropInfo[]) => void;
  /** The challenge roster, sent in the same opening frame (challenges.py). */
  onChallenges?: (challenges: ChallengeInfo[]) => void;
  /** The environment roster: in the opening frame, and again on every switch. */
  onEnvironment?: (roster: EnvironmentRoster) => void;

  #url: string;
  #ws!: WebSocket;
  #open: Promise<void>;
  #resolveOpen!: () => void;
  #rejectOpen!: (err: Error) => void;
  #everOpened = false;
  #disposed = false;
  #retryMs = 500;

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
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      this.#everOpened = true;
      this.#retryMs = 500;
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt; reconnection continues.
      if (!this.#everOpened) this.#rejectOpen(new Error(`world state connection failed: ${this.#url}`));
    };
    ws.onclose = () => {
      if (this.#disposed) return;
      setTimeout(() => this.#connect(), this.#retryMs);
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
    this.#ws.close();
  }

  #onMessage(raw: string): void {
    const parsed = JSON.parse(raw) as {
      props?: PropInfo[];
      challenges?: ChallengeInfo[];
      environment?: EnvironmentInfo | null;
      environments?: EnvironmentSummary[];
      switch?: EnvironmentRoster["switch"];
    };
    if (parsed.props || parsed.challenges) {
      // Roster frame, not a state frame: it has no clock, opens the stream and
      // returns whenever the world changes (see world_server.serve_state).
      if (parsed.props) this.onProps?.(parsed.props);
      if (parsed.challenges) this.onChallenges?.(parsed.challenges);
      this.onEnvironment?.({
        environment: parsed.environment ?? null,
        environments: parsed.environments ?? [],
        switch: parsed.switch ?? null,
      });
      return;
    }
    const msg = parsed as unknown as {
      t: number;
      wall: number;
      pose: [number, number, number];
      joints: Record<string, number>;
      objects?: Record<string, number[]> | null;
      challenge?: ChallengeBlock | null;
    };
    const joints = msg.joints;
    // joint6M: the gripper's mirrored finger (URDF mimic of joint6, x-1).
    joints["joint6M"] = -(joints["joint6"] ?? 0);
    this.onState?.({
      t: msg.t,
      wall: msg.wall,
      x: msg.pose[0],
      y: msg.pose[1],
      yaw: msg.pose[2],
      joints,
      objects: msg.objects ?? {},
      challenge: msg.challenge ?? null,
    });
  }
}
