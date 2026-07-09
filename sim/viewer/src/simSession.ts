// SimSession — a drop-in for the webapp's WebRtcSession when the robot is
// simulated: same state shape and methods, no video pipeline. State comes
// from the world server's ground-truth observer stream (~75Hz pose+joints),
// played back with a short clamped interpolation; rosbridge remains only
// for the Nav2 plan overlay and the /scan debug overlay.
// Architecture: sim/README.md.

import type { SimScene } from "./scene";
import { RosbridgePhysicsController, type RawCostmap } from "./physics/rosbridgeController";
import { WorldStateController } from "./physics/worldStateController";
import {
  OscillationDetector,
  costAt,
  costLabel,
  criticStates,
  headingErrorToCarrot,
  maxCostAhead,
  pathOccupancyRatio,
  STALL_VX,
  STALL_WZ,
  type CriticState,
} from "./navDebug";

/** Nav debugging (planner route, follower overlay, HUD) is opt-in per URL so
 * the normal sim view stays uncluttered and pays no rosbridge cost. */
export function navDebugEnabled(): boolean {
  return new URLSearchParams(location.search).has("navdebug");
}

/** One frame of derived MPPI decision state for the ?navdebug HUD. */
export interface NavDebugSnapshot {
  cmd: { vx: number; wz: number } | null;
  finalCmd: { vx: number; wz: number } | null;
  stalled: boolean;
  wzFlips: number;
  /** The navigation target: the skill's commanded goal when available
   * (frame + commanded=true), else the plan endpoint as a fallback. */
  goal: { x: number; y: number; yaw: number; frame?: string; commanded: boolean } | null;
  distanceRemaining: number | null;
  recoveries: number | null;
  navTimeS: number | null;
  costUnderRobot: string;
  maxCostAhead: string;
  pathOccupancy: number | null;
  headingErrDeg: number | null;
  critics: CriticState[];
}

/** PiP tile render size; square to match the webapp's .cam-tile. */
export const THUMB_W = 240;
export const THUMB_H = 240;

/** Playback delay bounds (see #delayS): one stream interval up to a
 * still-watchable worst case. */
const DELAY_MIN_S = 0.025;
const DELAY_MAX_S = 0.25;

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
  #activeCams = ["main"];
  #primaryIndex = 0;
  #primaryName = "main";

  #controller: WorldStateController | null = null;
  #scanFeed: RosbridgePhysicsController | null = null;
  #thumbCanvases: HTMLCanvasElement[] = [];
  #thumbContexts: (CanvasRenderingContext2D | null)[] = [];
  #started = false;
  #gotPose = false;
  #stageReady = false;

  // Ground-truth snapshots on the sim clock.
  #samples: { t: number; x: number; y: number; yaw: number; joints: Record<string, number> }[] = [];
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

  // --- ?navdebug only (see navDebugEnabled) ---
  #navDebug = navDebugEnabled();

  // Nav2 planned path, shown while navigating. The planner republishes ~1Hz
  // the whole time it's driving, so a lull means navigation ended -- same
  // staleness rule as the webapp's 2D map.
  #plan: Float32Array | null = null;
  #planDirty = false;
  #planStaleAt = 0;
  static readonly #PLAN_STALE_S = 4;
  /** Fallback navigation target: the plan's final pose. Cleared with the plan. */
  #goal: { x: number; y: number; yaw: number } | null = null;
  #goalDirty = false;
  /** The exact goal navigate_to_position commanded (preferred over #goal --
   * the plan endpoint wiggles with every replan, this is the true target).
   * Cleared with the plan: the planner going quiet means navigation ended. */
  #commandedGoal: { x: number; y: number; yaw: number; frame: string } | null = null;

  // Follower overlay ("follower" chip): the MPPI command arrow + the path it's
  // tracking. Both go stale quickly once the controller stops.
  #followerOn = false;
  #followerDirty = false;
  #cmd: { vx: number; wz: number } | null = null;
  #cmdDirty = false;
  #followerPath: Float32Array | null = null;
  #followerPathDirty = false;
  #followerStaleAt = 0;
  static readonly #FOLLOWER_STALE_S = 1;

  // HUD inputs: everything else MPPI's decision is gated on.
  #finalCmd: { vx: number; wz: number } | null = null;
  #navFeedback: { distanceRemaining: number; recoveries: number; navTimeS: number } | null = null;
  #costmap: RawCostmap | null = null;
  #oscillation = new OscillationDetector();
  #lastPose: { x: number; y: number; yaw: number } | null = null;

  // Freeze chip: pauses the simulated world in place. The robot stack (Nav2,
  // MPPI) keeps running against a world that stops advancing, so the planner
  // and controller keep publishing and the overlays stay live -- just static.
  // Unfreezing resumes exactly where it left off.
  #frozen = false;

  #stateUrls: string[];
  #rosUrl: string;

  constructor(opts: { stateUrl?: string; rosUrl?: string } = {}) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const proxied = `${scheme}://${location.host}/worldstate`;
    // Local browsers connect straight to the world server (loopback is
    // mixed-content-exempt in Chrome/Firefox, and skips the container
    // relay's latency tail); Safari and remote browsers fall back to the
    // proxied route.
    this.#stateUrls = opts.stateUrl
      ? [opts.stateUrl]
      : ["localhost", "127.0.0.1"].includes(location.hostname)
        ? ["ws://127.0.0.1:8800", proxied]
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
    if (this.#navDebug) this.#ensureRosFeed().enableNavDebug();
  }

  /** The rosbridge connection is shared by the lidar overlay and the nav-debug
   * feeds, and opened by whichever needs it first -- a plain sim view never
   * opens it at all. */
  #ensureRosFeed(): RosbridgePhysicsController {
    if (this.#scanFeed) return this.#scanFeed;
    const feed = new RosbridgePhysicsController(this.#rosUrl);
    this.#scanFeed = feed;
    feed.onScan = (points) => {
      this.#scan = points;
      this.#scanDirty = true;
    };
    feed.onPlan = (points, goal) => {
      this.#plan = points;
      this.#planDirty = true;
      this.#planStaleAt = performance.now() / 1000 + SimSession.#PLAN_STALE_S;
      if (goal) {
        this.#goal = goal;
        this.#goalDirty = true;
      }
    };
    feed.onCommand = (vx, wz) => {
      this.#cmd = { vx, wz };
      this.#cmdDirty = true;
      this.#oscillation.push(wz);
      this.#followerStaleAt = performance.now() / 1000 + SimSession.#FOLLOWER_STALE_S;
    };
    feed.onFinalCommand = (vx, wz) => {
      this.#finalCmd = { vx, wz };
    };
    feed.onFollowerPath = (points) => {
      this.#followerPath = points;
      this.#followerPathDirty = true;
      this.#followerStaleAt = performance.now() / 1000 + SimSession.#FOLLOWER_STALE_S;
    };
    feed.onNavFeedback = (fb) => {
      this.#navFeedback = fb;
    };
    feed.onCommandedGoal = (goal) => {
      this.#commandedGoal = goal;
      this.#goalDirty = true;
    };
    feed.onCostmap = (cm) => {
      this.#costmap = cm;
    };
    feed.init().catch((err) => console.warn("[sim-session] rosbridge overlays unavailable:", err));
    return feed;
  }

  /** True when the ?navdebug flag is set: the stage shows the follower chip
   * and the decision HUD. */
  get navDebug(): boolean {
    return this.#navDebug;
  }

  /** Connect the state feed, falling through the URL candidates (direct
   * loopback first when local, then the proxied route). */
  #connectState(i: number): void {
    const url = this.#stateUrls[i];
    this.#controller = new WorldStateController(url);
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
        this.#samples.push({ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints });
        if (this.#samples.length > 60) this.#samples.shift();
      } else if (s.t < last.t - 0.5) {
        // Sim clock jumped backwards (world-server restart): restart playback.
        this.#samples = [{ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints }];
        this.#playT = null;
      }
      this.#live = true;
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
    // Drop buffered overlay data so a restart doesn't redraw a stale route/scan.
    this.#plan = null;
    this.#planDirty = false;
    this.#goal = null;
    this.#goalDirty = false;
    this.#commandedGoal = null;
    this.#scan = null;
    this.#scanDirty = false;
    this.#cmd = null;
    this.#cmdDirty = false;
    this.#followerPath = null;
    this.#followerPathDirty = false;
    this.#finalCmd = null;
    this.#navFeedback = null;
    this.#costmap = null;
    this.#started = false;
    this.#gotPose = false;
    this.#patch({ status: "idle", videoStream: null });
  }

  destroy(): void {
    this.stop();
    this.#listeners.clear();
  }

  /** Toggle the /scan hit-point overlay (stage "lidar" chip); opens the
   * rosbridge connection on first use. */
  setLidarVisible(on: boolean): void {
    this.#lidarOn = on;
    this.#overlaysDirty = true;
    if (on) this.#ensureRosFeed().enableScan();
  }

  /** Toggle the collision-hull wireframe overlay (stage "collisions" chip). */
  setCollisionHullsVisible(on: boolean): void {
    this.#hullsOn = on;
    this.#overlaysDirty = true;
  }

  /** Toggle the follower overlay (stage "follower" chip): MPPI's command arrow
   * + the path it's tracking. The feeds are already subscribed under ?navdebug;
   * this only governs rendering. */
  setFollowerVisible(on: boolean): void {
    this.#followerOn = on;
    this.#followerDirty = true;
  }

  /** Freeze/unfreeze the simulated world (stage "freeze" chip). Pauses physics
   * in place: the robot halts, navigation stays exactly where it was, and the
   * overlays hold. Unfreezing continues from that point. */
  setFrozen(on: boolean): void {
    this.#frozen = on;
    this.#ensureRosFeed().setWorldPaused(on);
  }

  get frozen(): boolean {
    return this.#frozen;
  }

  /** Send a map-frame navigation goal (set-goal chip). */
  publishGoalPose(x: number, y: number, yaw: number): void {
    this.#ensureRosFeed().publishGoalPose(x, y, yaw);
  }

  /** Latest interpolated robot pose the scene is showing (world frame). */
  get robotPose(): { x: number; y: number; yaw: number } | null {
    return this.#lastPose;
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
    const yaw = a.yaw + dyaw * u;
    scene.setPose(x, y, yaw);
    this.#lastPose = { x, y, yaw };

    const joints: Record<string, number> = {};
    for (const [name, va] of Object.entries(a.joints)) {
      const vb = b.joints[name] ?? va;
      joints[name] = va + (vb - va) * u;
    }
    scene.setJointAngles(joints);

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

    if (!this.#navDebug) return; // plan + follower overlays are ?navdebug only

    // While the world is frozen, ROS time is stopped, so the planner and
    // controller legitimately go quiet -- suspend staleness expiry (and let
    // the timers re-arm on the first message after unfreeze).
    if (this.#frozen) {
      this.#planStaleAt = performance.now() / 1000 + SimSession.#PLAN_STALE_S;
      this.#followerStaleAt = performance.now() / 1000 + SimSession.#FOLLOWER_STALE_S;
    }

    if (this.#planDirty && this.#plan) {
      this.#planDirty = false;
      scene.setPlanPoints(this.#plan);
    } else if (this.#plan && performance.now() / 1000 > this.#planStaleAt) {
      this.#plan = null; // planner went quiet: navigation ended
      this.#goal = null;
      this.#commandedGoal = null;
      scene.setPlanVisible(false);
      scene.setGoalVisible(false);
    }
    const goal = this.#commandedGoal ?? this.#goal;
    if (this.#goalDirty && goal) {
      this.#goalDirty = false;
      scene.setGoalMarker(goal.x, goal.y, goal.yaw);
    }

    if (this.#followerDirty) {
      this.#followerDirty = false;
      scene.setFollowerVisible(this.#followerOn);
    }
    if (this.#followerOn) {
      const stale = performance.now() / 1000 > this.#followerStaleAt;
      if (stale) {
        // Controller stopped commanding: clear the arrow and tracked path.
        if (this.#cmd || this.#followerPath) {
          this.#cmd = null;
          this.#followerPath = null;
          scene.setCommandVelocity(0, 0);
          scene.setFollowerPathPoints(new Float32Array(0));
        }
      } else {
        if (this.#cmdDirty && this.#cmd) {
          this.#cmdDirty = false;
          scene.setCommandVelocity(this.#cmd.vx, this.#cmd.wz);
        }
        if (this.#followerPathDirty && this.#followerPath) {
          this.#followerPathDirty = false;
          scene.setFollowerPathPoints(this.#followerPath);
        }
      }
    }
  }

  /** Derived MPPI decision state for the ?navdebug HUD. MPPI never publishes
   * its per-critic costs, so instead we report what those critics are gated
   * on -- distance to goal, heading error, and costmap occupancy -- which is
   * what actually decides whether it drives, turns, or sits still.
   *
   * Stays live while the world is frozen: the robot stack keeps publishing
   * against the paused world, so the numbers simply stop changing. */
  get navDebugSnapshot(): NavDebugSnapshot | null {
    if (!this.#navDebug) return null;
    const stale = performance.now() / 1000 > this.#followerStaleAt;
    const cmd = stale ? null : this.#cmd;
    const path = stale ? null : this.#followerPath;
    const cm = this.#costmap;
    const pose = this.#lastPose;

    const occupancy = cm && path ? pathOccupancyRatio(cm, path) : null;
    const headingErr = pose && path ? headingErrorToCarrot(pose, path) : null;
    const distance = stale ? null : (this.#navFeedback?.distanceRemaining ?? null);

    return {
      cmd,
      finalCmd: stale ? null : this.#finalCmd,
      stalled: cmd !== null && Math.abs(cmd.vx) < STALL_VX && Math.abs(cmd.wz) < STALL_WZ,
      wzFlips: this.#oscillation.flipsInWindow,
      goal: this.#commandedGoal
        ? { ...this.#commandedGoal, commanded: true }
        : this.#goal
          ? { ...this.#goal, commanded: false }
          : null,
      distanceRemaining: distance,
      recoveries: stale ? null : (this.#navFeedback?.recoveries ?? null),
      navTimeS: stale ? null : (this.#navFeedback?.navTimeS ?? null),
      costUnderRobot: cm && pose ? costLabel(costAt(cm, pose.x, pose.y)) : "—",
      maxCostAhead: cm && path ? costLabel(maxCostAhead(cm, path, 0.5)) : "—",
      pathOccupancy: occupancy,
      headingErrDeg: headingErr === null ? null : (headingErr * 180) / Math.PI,
      critics: criticStates(distance, headingErr, occupancy),
    };
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

export function createSimSession(opts: { stateUrl?: string; rosUrl?: string } = {}): SimSession {
  return new SimSession(opts);
}

export { createSimStage } from "./simStage";
