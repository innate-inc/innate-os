// SimSession — a drop-in for the webapp's WebRtcSession when the robot is
// simulated: same state shape and methods, no video pipeline. State comes
// from the world server's ground-truth observer stream (~75Hz pose+joints),
// played back with a short clamped interpolation; rosbridge remains only
// for the /scan debug overlay. Architecture: sim/README.md.

import type { SimScene } from "./scene";
import type { DeformableFrame } from "./physics/deformableFrame";
import { RosbridgePhysicsController } from "./physics/rosbridgeController";
import { WorldStateController } from "./physics/worldStateController";
import type { ChallengeActive, ChallengeBlock, ChallengeInfo, ChallengeProgress } from "./physics/worldStateController";
import type { PropInfo } from "./props";

/** One roster row as a renderer wants it: what the challenge is, plus how it
 * has gone so far. Merged here from the two halves the server sends. */
export interface ChallengeEntry extends ChallengeInfo, ChallengeProgress {}

/** The challenge panel's whole world: the roster and the run in progress. */
export interface ChallengeView {
  list: ChallengeEntry[];
  active: ChallengeActive | null;
}

const NO_PROGRESS: ChallengeProgress = { passed: false, best_time_s: null, attempts: 0 };

/** PiP tile render size; square to match the webapp's .cam-tile. */
export const THUMB_W = 240;
export const THUMB_H = 240;

/** Playback delay bounds (see #delayS): one stream interval up to a
 * still-watchable worst case. */
const DELAY_MIN_S = 0.025;
const DELAY_MAX_S = 0.25;
const DEFAULT_CAMERA = "orbit";

/** Advance `samples` past renderT and return the bracketing pair plus the
 * clamped interpolation factor (holds the last sample during a gap instead
 * of extrapolating past it). Mutates the array (drops consumed history). */
function bracket<T extends { t: number }>(samples: T[], renderT: number): [T, T, number] {
  while (samples.length > 2 && samples[1].t <= renderT) samples.shift();
  const a = samples[0];
  const b = samples.length > 1 ? samples[1] : a;
  const span = b.t - a.t;
  const u = span > 1e-4 ? Math.min(1, Math.max(0, (renderT - a.t) / span)) : 1;
  return [a, b, u];
}

export interface SimSessionState {
  status: "idle" | "connecting" | "streaming" | "error";
  videoStream: MediaStream | null;
  videoStreams: (MediaStream | null)[];
  videoLive: boolean[];
  audioStream: MediaStream | null;
  audioRequested: boolean;
  iceState: string;
  stunFallback: boolean;
}

export class SimSession {
  #state: SimSessionState = {
    status: "idle",
    videoStream: null,
    videoStreams: [],
    videoLive: [],
    audioStream: null,
    audioRequested: false,
    iceState: "connected",
    stunFallback: false,
  };
  #listeners = new Set<(state: SimSessionState) => void>();

  #roster = ["main", "arm", "orbit"];
  #activeCams = [DEFAULT_CAMERA];
  #primaryIndex = this.#roster.indexOf(DEFAULT_CAMERA);
  #primaryName = DEFAULT_CAMERA;

  #controller: WorldStateController | null = null;
  #scanFeed: RosbridgePhysicsController | null = null;
  #thumbCanvases: HTMLCanvasElement[] = [];
  #thumbContexts: (CanvasRenderingContext2D | null)[] = [];
  #started = false;
  #gotPose = false;
  #stageReady = false;

  // Ground-truth snapshots on the sim clock.
  #samples: {
    t: number;
    x: number;
    y: number;
    yaw: number;
    joints: Record<string, number>;
    objects: Record<string, number[]>;
  }[] = [];
  #gaps: number[] = []; // recent inter-arrival gaps: sizes the playback delay
  #lastArrival = 0;
  // Playback position on the sim clock (see tick).
  #playT: number | null = null;
  #live = false;
  #spawned = false;

  // True server->browser delivery lag (shared wall clock); ?simperf HUD.
  #lagRecent: number[] = [];
  #lagMinS = Infinity;

  // Debug overlays (stage toggle chips); applied to the scene in tick().
  #scan: Float32Array | null = null;
  #scanDirty = false;
  #lidarOn = false;
  #hullsOn = false;
  #overlaysDirty = false;

  // The prop roster, relayed from the world server once per connection
  // (props.py sidecars). The stage builds its buttons from it and the scene
  // builds its models; both key off this rather than a second local table.
  #props: PropInfo[] = [];
  #propListeners = new Set<(props: PropInfo[]) => void>();
  #propsDirty = false;
  // IDF1 is not interpolated with the rigid timeline: retain one latest frame
  // per deformable and upload it at most once per browser render tick.
  #deformables = new Map<number, DeformableFrame>();
  #deformablesDirty = new Set<number>();

  #stateUrls: string[];
  #rosUrl: string;

  // Challenge judge state relayed from the world server (see challenges.py).
  // The server splits it in two -- what each challenge IS arrives once per
  // connection, what changes rides the state stream -- and this is where the
  // halves are put back together, so a renderer sees one roster. Deduped by
  // content, so listeners see transitions (~10Hz worst case from the
  // elapsed-time field), not the raw broadcast rate.
  #challengeInfo: ChallengeInfo[] = [];
  #challenge: ChallengeView | null = null;
  #challengeJson = "";
  #challengeListeners = new Set<(view: ChallengeView) => void>();

  constructor(opts: { stateUrl?: string; statePort?: number; rosUrl?: string } = {}) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const proxied = `${scheme}://${location.host}/worldstate`;
    // Local browsers connect straight to the world server (loopback is
    // mixed-content-exempt in Chrome/Firefox, and skips the container
    // relay's latency tail); Safari and remote browsers fall back to the
    // proxied route. statePort arrives from /config.json: only the server
    // knows which port the world server was published on.
    this.#stateUrls = opts.stateUrl
      ? [opts.stateUrl]
      : ["localhost", "127.0.0.1"].includes(location.hostname)
        ? [`ws://127.0.0.1:${opts.statePort ?? 8800}`, proxied]
        : [proxied];
    this.#rosUrl = opts.rosUrl ?? `${scheme}://${location.host}/ws`;
  }

  get state(): SimSessionState {
    return { ...this.#state, videoStreams: [...this.#state.videoStreams], videoLive: [...this.#state.videoLive] };
  }

  get primaryCamera(): string {
    return this.#primaryName;
  }

  onChange(cb: (state: SimSessionState) => void): () => void {
    this.#listeners.add(cb);
    cb(this.state);
    return () => this.#listeners.delete(cb);
  }

  start(): void {
    if (this.#started) return;
    this.#started = true;
    this.#patch({ status: "connecting" });

    this.#thumbCanvases = this.#roster.map(() => {
      const c = document.createElement("canvas");
      c.width = THUMB_W;
      c.height = THUMB_H;
      return c;
    });
    this.#thumbContexts = this.#thumbCanvases.map((c) => c.getContext("2d"));
    // No captureStream: an active canvas-capture pipeline pins the page's
    // composition to its 15fps tick; the webapp mounts these canvases
    // directly (thumbnailCanvas below).

    this.#connectState(0);
  }

  /** Connect the state feed, falling through the URL candidates (direct
   * loopback first when local, then the proxied route). */
  #connectState(i: number): void {
    const url = this.#stateUrls[i];
    this.#controller = new WorldStateController(url);
    this.#controller.onProps = (props) => {
      this.#props = props;
      this.#propsDirty = true; // handed to the scene on the next tick
      for (const cb of this.#propListeners) cb(props);
    };
    this.#controller.onChallenges = (challenges) => {
      // Arrives ahead of the stream, so the merge below has titles and briefs
      // before the first block; on a reconnect it just replaces them.
      this.#challengeInfo = challenges;
      this.#challengeJson = "";
    };
    this.#controller.onDeformableFrame = (frame) => {
      this.#deformables.set(frame.id, frame);
      this.#deformablesDirty.add(frame.id);
    };
    this.#controller.onState = (s) => {
      const lag = Date.now() / 1000 - s.wall;
      if (lag < this.#lagMinS) this.#lagMinS = lag;
      this.#lagRecent.push(lag);
      if (this.#lagRecent.length > 60) this.#lagRecent.shift();

      const now = performance.now() / 1000;
      if (this.#lastArrival > 0) {
        this.#gaps.push(Math.min(now - this.#lastArrival, 0.5));
        if (this.#gaps.length > 60) this.#gaps.shift();
      }
      this.#lastArrival = now;

      const last = this.#samples[this.#samples.length - 1];
      if (last === undefined || s.t > last.t) {
        this.#samples.push({ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints, objects: s.objects });
        if (this.#samples.length > 60) this.#samples.shift();
      } else if (s.t < last.t - 0.5) {
        // Sim clock jumped backwards (world-server restart): restart playback.
        this.#samples = [{ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints, objects: s.objects }];
        this.#playT = null;
      }
      this.#live = true;
      if (s.challenge) this.#publishChallenge(s.challenge);
      if (!this.#gotPose) {
        this.#gotPose = true;
        this.#maybeStreaming();
      }
    };
    this.#controller.init().catch((err) => {
      this.#controller?.dispose();
      if (i + 1 < this.#stateUrls.length) {
        console.warn(`[sim-session] ${url} unavailable, falling back:`, err);
        this.#connectState(i + 1);
        return;
      }
      console.error("[sim-session] world state feed failed:", err);
      this.#patch({ status: "error" });
    });
  }

  stop(): void {
    this.#controller?.dispose();
    this.#controller = null;
    this.#scanFeed?.dispose();
    this.#scanFeed = null;
    this.#deformables.clear();
    this.#deformablesDirty.clear();
    this.#started = false;
    this.#gotPose = false;
    this.#patch({ status: "idle", videoStream: null });
  }

  destroy(): void {
    this.stop();
    this.#listeners.clear();
  }

  /** Toggle the /scan hit-point overlay (stage "lidar" chip). The rosbridge
   * connection is opened on first use -- the 3D view itself never consumes
   * robot telemetry. */
  setLidarVisible(on: boolean): void {
    this.#lidarOn = on;
    this.#overlaysDirty = true;
    if (on && this.#scanFeed === null) {
      this.#scanFeed = new RosbridgePhysicsController(this.#rosUrl);
      this.#scanFeed.onScan = (points) => {
        this.#scan = points;
        this.#scanDirty = true;
      };
      this.#scanFeed.init().catch((err) => console.warn("[sim-session] scan overlay unavailable:", err));
    }
  }

  /** Toggle the collision-hull wireframe overlay (stage "collisions" chip). */
  setCollisionHullsVisible(on: boolean): void {
    this.#hullsOn = on;
    this.#overlaysDirty = true;
  }

  /** Send every prop back off-map (stage "clear" chip). */
  removeAllProps(): void {
    this.#controller?.send({ op: "remove_all_props" });
  }

  /** Set a whole set of props down in front of the robot at once, each at its
   * own reach offset (props.py `group`), parking everything outside the set. */
  placePropGroup(group: string): void {
    this.#controller?.send({ op: "place_group", group });
  }

  /** Set one prop down in front of the robot at the prop's own reach offset --
   * for the manipulation props that is an arc the arm can reach top-down, so
   * it lands at rest rather than falling. */
  placePropAtRobot(name: string): void {
    this.#controller?.send({ op: "place_prop_at_robot", name });
  }

  /** Release one prop above a spot the user picked, yawed about +z; the world
   * server's physics settles it onto whatever is below. */
  dropPropAt(name: string, x: number, y: number, yaw: number): void {
    this.#controller?.send({ op: "drop_prop_at", name, x, y, yaw });
  }

  /** Send one prop back off-map. */
  removeProp(name: string): void {
    this.#controller?.send({ op: "remove_prop", name });
  }

  /** Subscribe to the prop roster (props.py sidecars); fires immediately if it
   * has already arrived. The stage builds its buttons from this. */
  onProps(cb: (props: PropInfo[]) => void): () => void {
    this.#propListeners.add(cb);
    if (this.#props.length) cb(this.#props);
    return () => this.#propListeners.delete(cb);
  }

  /** Whether any manipulation prop is currently in the world. Read from
   * ground truth rather than from what this client last asked for, so the
   * stage's button still reads right after a sim reset or another viewer's
   * drop. False until the first state arrives. */
  get objectsPresent(): boolean {
    const last = this.#samples[this.#samples.length - 1];
    return last !== undefined && Object.keys(last.objects).length > 0;
  }

  /** Subscribe to the challenge judge's state (roster + active run); fires
   * immediately with the latest view once one has arrived. The webapp's
   * challenge panel keys off this method's existence to stay sim-only. */
  onChallenge(cb: (view: ChallengeView) => void): () => void {
    this.#challengeListeners.add(cb);
    if (this.#challenge) cb(this.#challenge);
    return () => this.#challengeListeners.delete(cb);
  }

  /** Merge the roster with the block that just arrived and notify listeners
   * if anything actually changed. A challenge with no record yet reads as
   * unattempted -- the server only sends progress for the ones it has. */
  #publishChallenge(block: ChallengeBlock): void {
    const json = JSON.stringify(block);
    if (json === this.#challengeJson) return;
    this.#challengeJson = json;
    this.#challenge = {
      list: this.#challengeInfo.map((info) => ({ ...info, ...(block.progress[info.id] ?? NO_PROGRESS) })),
      active: block.active,
    };
    for (const cb of this.#challengeListeners) cb(this.#challenge);
  }

  /** Start a challenge by id (resets the world and drops its props). */
  startChallenge(id: string): void {
    this.#controller?.send({ op: "start_challenge", id });
  }

  /** Abort the active challenge (or dismiss a finished one). */
  abortChallenge(): void {
    this.#controller?.send({ op: "abort_challenge" });
  }

  // WebRTC-specific surface: harmless no-ops in sim.
  setAudio(_on: boolean): void {}
  async getStats(): Promise<null> {
    return null;
  }

  setActiveCameras(names: string[]): void {
    this.#activeCams = names.filter((n) => this.#roster.includes(n));
    if (!this.#activeCams.includes(this.#primaryName) && this.#activeCams.length) {
      this.#primaryName = this.#activeCams[0];
      this.#primaryIndex = this.#roster.indexOf(this.#primaryName);
    }
    this.#patch(this.#videoArrays());
  }

  setPrimaryCamera(index: number, name: string): void {
    if (index < 0 || index >= this.#roster.length) return;
    this.#primaryIndex = index;
    this.#primaryName = name;
    if (!this.#activeCams.includes(name)) this.#activeCams.push(name);
    this.#patch(this.#videoArrays());
  }

  // --- stage integration (createSimStage) ---

  /** Stage scene finished loading its assets. */
  stageReady(): void {
    this.#stageReady = true;
    this.#maybeStreaming();
  }

  stageError(err: unknown): void {
    console.error("[sim-session] stage failed:", err);
    this.#patch({ status: "error" });
  }

  /** Playback delay behind the newest sample: 2x the p90 inter-arrival gap
   * (clamped) -- ~30ms locally, growing only as the transport demands. */
  #delayS(): number {
    if (this.#gaps.length < 5) return 0.05; // conservative until measured
    const sorted = [...this.#gaps].sort((a, b) => a - b);
    const p90 = sorted[Math.floor(sorted.length * 0.9)];
    return Math.min(DELAY_MAX_S, Math.max(DELAY_MIN_S, p90 * 2.0));
  }

  /** Per-frame: clamped interpolation on the sim clock, #delayS behind the
   * newest sample; a delivery gap holds the last pose, never extrapolates. */
  tick(scene: SimScene, dt: number): void {
    if (!this.#live || this.#samples.length === 0) return;
    const first = this.#samples[0];
    if (!this.#spawned) {
      this.#spawned = true;
      scene.spawnAt(first.x, first.y, first.yaw);
    }

    // Playback advances with the frame clock, softly steered toward the
    // stream (a hard lock would replay delivery jitter 1:1); a large error
    // (hidden tab, server restart) snaps instead of chasing for seconds.
    const target = this.#samples[this.#samples.length - 1].t - this.#delayS();
    if (this.#playT === null || Math.abs(target - this.#playT) > 0.3) this.#playT = target;
    else this.#playT += dt + (target - this.#playT) * Math.min(1, dt * 4);

    const [a, b, u] = bracket(this.#samples, this.#playT);
    const x = a.x + (b.x - a.x) * u;
    const y = a.y + (b.y - a.y) * u;
    const dyaw = Math.atan2(Math.sin(b.yaw - a.yaw), Math.cos(b.yaw - a.yaw));
    scene.setPose(x, y, a.yaw + dyaw * u);

    const joints: Record<string, number> = {};
    for (const [name, va] of Object.entries(a.joints)) {
      const vb = b.joints[name] ?? va;
      joints[name] = va + (vb - va) * u;
    }
    scene.setJointAngles(joints);
    // Interpolate the props on the SAME timeline as the robot. Drawing them at
    // sample b while the robot is drawn at u between a and b puts them up to
    // one sample ahead: ~13ms at 75Hz, which at a 1.2m/s wrist is over 15mm of
    // mismatch, and it shows as the gripper passing through whatever it is
    // carrying -- only ever while moving, which is exactly when you look.
    const objects: Record<string, number[]> = {};
    for (const [name, pb] of Object.entries(b.objects)) {
      const pa = a.objects[name] ?? pb;
      // Shortest-arc quaternion blend; the props barely rotate, so a
      // normalised lerp is indistinguishable from a slerp here.
      const dot = pa[3] * pb[3] + pa[4] * pb[4] + pa[5] * pb[5] + pa[6] * pb[6];
      const s = dot < 0 ? -1 : 1;
      const q = [3, 4, 5, 6].map((i) => pa[i] + (s * pb[i] - pa[i]) * u);
      const norm = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
      objects[name] = [
        pa[0] + (pb[0] - pa[0]) * u,
        pa[1] + (pb[1] - pa[1]) * u,
        pa[2] + (pb[2] - pa[2]) * u,
        ...q.map((v) => v / norm),
      ];
    }
    if (this.#propsDirty) {
      this.#propsDirty = false;
      scene.setPropManifest(this.#props);
    }
    scene.setObjectPoses(objects);
    for (const id of this.#deformablesDirty) {
      const frame = this.#deformables.get(id);
      if (frame) scene.setDeformableFrame(frame);
    }
    this.#deformablesDirty.clear();

    if (this.#overlaysDirty) {
      this.#overlaysDirty = false;
      scene.setLidarVisible(this.#lidarOn);
      scene.setCollisionHullsVisible(this.#hullsOn);
    }
    if (this.#lidarOn && this.#scanDirty && this.#scan) {
      this.#scanDirty = false;
      scene.setLidarPoints(this.#scan);
      scene.setLidarVisible(true); // first points may arrive after the toggle
    }
  }

  /** Active, non-primary views whose PiP tiles need frames. */
  liveThumbnails(): { index: number; name: string }[] {
    return this.#roster
      .map((name, index) => ({ index, name }))
      .filter(({ index, name }) => index !== this.#primaryIndex && this.#activeCams.includes(name));
  }

  /** Copy a region rendered at the source canvas' bottom-left into a thumb
   * stream (GL viewport origin is bottom-left; 2D canvas is top-left). */
  blitThumbnail(index: number, source: HTMLCanvasElement, pixelW: number, pixelH: number): void {
    this.#thumbContexts[index]?.drawImage(source, 0, source.height - pixelH, pixelW, pixelH, 0, 0, THUMB_W, THUMB_H);
  }

  #maybeStreaming(): void {
    if (this.#gotPose && this.#stageReady) {
      this.#patch({ status: "streaming", ...this.#videoArrays() });
    }
  }

  #videoArrays() {
    return {
      // No MediaStreams in sim: tiles mount thumbnailCanvas() nodes instead.
      videoStreams: this.#roster.map(() => null),
      videoLive: this.#roster.map((name) => this.#gotPose && this.#stageReady && this.#activeCams.includes(name)),
    };
  }

  /** The live 2D canvas behind a PiP tile; the webapp mounts it directly. */
  thumbnailCanvas(index: number): HTMLCanvasElement | null {
    return this.#thumbCanvases[index] ?? null;
  }

  /** Server->browser state delivery lag: cur is the median of the last ~2s,
   * min is the session floor (the pipeline's fixed cost). cur >> min means
   * a queue is filling upstream. Null until state has arrived. */
  get pipelineLag(): { curMs: number; minMs: number } | null {
    if (this.#lagRecent.length === 0) return null;
    const sorted = [...this.#lagRecent].sort((a, b) => a - b);
    return { curMs: sorted[sorted.length >> 1] * 1000, minMs: this.#lagMinS * 1000 };
  }

  #patch(partial: Partial<SimSessionState>): void {
    Object.assign(this.#state, partial);
    const snapshot = this.state;
    for (const cb of this.#listeners) cb(snapshot);
  }
}

export function createSimSession(
  opts: { stateUrl?: string; statePort?: number; rosUrl?: string } = {},
): SimSession {
  return new SimSession(opts);
}

export { createSimStage } from "./simStage";
