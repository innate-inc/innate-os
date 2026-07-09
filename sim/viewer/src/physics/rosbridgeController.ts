// Robot-telemetry overlay source for the webapp's SimSession: subscribe over
// rosbridge to the robot's lidar, Nav2's planned path, and the MPPI
// controller's command + tracked path, and emit world-frame points. Pose/
// joints for the 3D view come from the world server's observer stream
// (worldStateController) -- these stay here deliberately: they are robot
// software outputs, so the robot's own pipeline is the honest source for the
// overlays. Read-only except for the ?navdebug freeze chip (setWorldPaused
// below, sim-only) -- teleop and everything else go through the webapp's own
// rosbridge client.
//
// Speaks the rosbridge JSON protocol directly (subscribe/publish) -- no
// roslib dependency for a few message types.

// base_laser mount relative to base_link (mars.urdf base_laser_joint).
const LASER_OFFSET = { x: -0.0764, z: 0.17165 };

// Both Nav2 planner servers are namespaced (mars_nav launch); only the
// active one publishes, so both feed the same handler. Kept in sync by hand
// with PLAN_TOPICS in webapp/js/constants.js -- the viewer is a separately
// built bundle, so the constant can't be imported across that boundary.
const PLAN_TOPICS = ["/navigation/plan", "/mapfree/plan"];

// The router forwards goals to an internal action server; whichever of the two
// relays feedback, we take it. Same only-one-publishes trick as PLAN_TOPICS.
const NAV_FEEDBACK_TOPICS = ["/navigate_to_pose/_action/feedback", "/internal_navigate_to_pose/_action/feedback"];

/** nav2_msgs/Costmap payload, flattened for the debug HUD. */
export interface RawCostmap {
  resolution: number;
  sizeX: number;
  sizeY: number;
  originX: number;
  originY: number;
  data: Uint8Array;
}

/** rosbridge sends uint8[] as either a base64 string or a plain number array
 * depending on the bridge implementation; accept both. */
function decodeCostmapData(data: string | number[]): Uint8Array {
  if (typeof data !== "string") return Uint8Array.from(data);
  const binary = atob(data);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

export class RosbridgePhysicsController {
  /** World-frame lidar hit points [x0,y0,z0, x1,...], null-range rays skipped. */
  onScan?: (points: Float32Array) => void;
  /** Planned-path ground points [x0,y0, x1,y1, ...]; empty = plan cleared.
   * `goal` is the plan's final pose -- the navigation target. (The action goal
   * itself is sent as a service call, so it can't be subscribed to; the
   * planner always plans all the way to it, so the path's end is the goal.) */
  onPlan?: (points: Float32Array, goal: { x: number; y: number; yaw: number } | null) => void;
  /** MPPI's commanded body velocity (m/s, rad/s) from /cmd_vel_raw. */
  onCommand?: (vx: number, wz: number) => void;
  /** The final velocity reaching the wheels, after smoothing + the mux. */
  onFinalCommand?: (vx: number, wz: number) => void;
  /** Ground points of the path MPPI is tracking (transformed_global_plan). */
  onFollowerPath?: (points: Float32Array) => void;
  /** Nav2's own progress feedback for the active NavigateToPose goal. */
  onNavFeedback?: (fb: { distanceRemaining: number; recoveries: number; navTimeS: number }) => void;
  /** The exact goal navigate_to_position commanded, in its fixed frame
   * ("map", or "odom" for local goals) -- the true target, unlike the
   * plan endpoint which wiggles with every replan. */
  onCommandedGoal?: (goal: { x: number; y: number; yaw: number; frame: string }) => void;
  /** The controller's local costmap (raw 0-255 costs). */
  onCostmap?: (cm: RawCostmap) => void;
  #scanEnabled = false;
  #navDebugEnabled = false;
  // AMCL's map->odom: in sim, odom == the ground-truth world frame, so this is
  // the world<->map bridge. Identity until the first TF arrives. Without it,
  // map-frame plans/goals drawn as world coordinates shift by however far
  // AMCL has drifted -- goals appear to "jump".
  #mapToOdom: { x: number; y: number; yaw: number } | null = null;

  #url: string;
  #ws!: WebSocket;
  #open: Promise<void>;
  #resolveOpen!: () => void;
  #rejectOpen!: (err: Error) => void;
  #everOpened = false;
  #disposed = false;
  #retryMs = 500;
  // Latest driver pose -- only anchors scan points in the world frame.
  #pose = { x: 0, y: 0, yaw: 0 };

  constructor(url: string) {
    this.#url = url;
    this.#open = new Promise((resolve, reject) => {
      this.#resolveOpen = resolve;
      this.#rejectOpen = reject;
    });
    this.#connect();
  }

  /** (Re)open the socket. A rosbridge session is stateless per connection, so
   * every open re-issues the subscriptions; on drop we retry with backoff
   * until dispose(), so a network blip doesn't kill the view. */
  #connect(): void {
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      this.#everOpened = true;
      this.#retryMs = 500;
      if (this.#scanEnabled) this.#subscribeScan();
      if (this.#navDebugEnabled) this.#subscribeNavDebug();
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt (later errors are
      // no-ops on the settled promise); reconnection continues regardless.
      if (!this.#everOpened) this.#rejectOpen(new Error(`rosbridge connection failed: ${this.#url}`));
    };
    ws.onclose = () => {
      if (this.#disposed) return;
      setTimeout(() => this.#connect(), this.#retryMs);
      this.#retryMs = Math.min(this.#retryMs * 2, 5000);
    };
    ws.onmessage = (ev) => this.#onMessage(JSON.parse(ev.data as string));
  }

  async init(): Promise<void> {
    await this.#open;
  }

  /** Start the /scan + /odom feeds (the lidar overlay is opt-in; the plan
   * overlay is always on). Idempotent; survives reconnects via #connect. */
  enableScan(): void {
    if (this.#scanEnabled) return;
    this.#scanEnabled = true;
    this.#subscribeScan();
  }

  #subscribeScan(): void {
    // /odom only anchors the scan overlay, so a mild throttle is fine here
    // (the 3D view's pose comes from the world state stream, not rosbridge).
    // queue_length 1: latest-wins -- a hop buffering more than the newest
    // sample converts load hiccups into permanent lag.
    this.#send({ op: "subscribe", topic: "/odom", type: "nav_msgs/msg/Odometry", throttle_rate: 100, queue_length: 1 });
    this.#send({ op: "subscribe", topic: "/scan", type: "sensor_msgs/msg/LaserScan", throttle_rate: 150, queue_length: 1 });
  }

  /** Start every navigation-debug feed (?navdebug only): the planner's route,
   * the MPPI command and tracked path, Nav2's progress feedback, and the local
   * costmap. transformed_global_plan/trajectories only publish when the
   * controller's `visualize` param is on (mars_nav controller.yaml).
   * Idempotent; survives reconnects via #connect. */
  enableNavDebug(): void {
    if (this.#navDebugEnabled) return;
    this.#navDebugEnabled = true;
    this.#subscribeNavDebug();
  }

  /** map-frame point -> world (= odom) frame. P_odom = R(-yaw)·(P_map - t). */
  #mapToWorld(x: number, y: number): [number, number] {
    const t = this.#mapToOdom;
    if (!t) return [x, y];
    const dx = x - t.x;
    const dy = y - t.y;
    const c = Math.cos(-t.yaw);
    const s = Math.sin(-t.yaw);
    return [dx * c - dy * s, dx * s + dy * c];
  }

  #yawMapToWorld(yaw: number): number {
    return this.#mapToOdom ? yaw - this.#mapToOdom.yaw : yaw;
  }

  #subscribeNavDebug(): void {
    // The planner republishes ~1Hz while driving; queue_length 1 keeps every
    // feed latest-wins.
    for (const topic of PLAN_TOPICS) {
      this.#send({ op: "subscribe", topic, type: "nav_msgs/msg/Path", throttle_rate: 250, queue_length: 1 });
    }
    // /cmd_vel_raw is the controller's own output, pre-smoothing/mux -- the
    // honest "what the follower commands"; /cmd_vel is what survives the mux.
    this.#send({ op: "subscribe", topic: "/cmd_vel_raw", type: "geometry_msgs/msg/Twist", throttle_rate: 100, queue_length: 1 });
    this.#send({ op: "subscribe", topic: "/cmd_vel", type: "geometry_msgs/msg/Twist", throttle_rate: 100, queue_length: 1 });
    this.#send({ op: "subscribe", topic: "/transformed_global_plan", type: "nav_msgs/msg/Path", throttle_rate: 100, queue_length: 1 });
    for (const topic of NAV_FEEDBACK_TOPICS) {
      this.#send({ op: "subscribe", topic, throttle_rate: 200, queue_length: 1 });
    }
    this.#send({ op: "subscribe", topic: "/nav/commanded_goal", type: "geometry_msgs/msg/PoseStamped", queue_length: 1 });
    // map->odom from AMCL rides /tf among the 30Hz odom transforms; no
    // throttle (tiny messages) so we never starve on the rarer map->odom.
    this.#send({ op: "subscribe", topic: "/tf", type: "tf2_msgs/msg/TFMessage", queue_length: 1 });
    // The costmap is the biggest payload here (a 6x6m @5cm grid); 2Hz is
    // plenty to explain why forward rollouts are expensive.
    this.#send({ op: "subscribe", topic: "/local_costmap/costmap_raw", type: "nav2_msgs/msg/Costmap", throttle_rate: 500, queue_length: 1 });
  }

  dispose(): void {
    this.#disposed = true;
    this.#ws.close();
  }

  #onMessage(msg: { op: string; topic?: string; msg?: unknown }): void {
    if (msg.op !== "publish") return;
    if (msg.topic === "/odom") {
      const odom = msg.msg as {
        pose: { pose: { position: { x: number; y: number }; orientation: { z: number; w: number } } };
      };
      const { position, orientation } = odom.pose.pose;
      this.#pose = { x: position.x, y: position.y, yaw: 2 * Math.atan2(orientation.z, orientation.w) };
    } else if (msg.topic === "/tf") {
      const tf = msg.msg as {
        transforms?: Array<{
          header: { frame_id: string };
          child_frame_id: string;
          transform: { translation: { x: number; y: number }; rotation: { z: number; w: number } };
        }>;
      };
      for (const tr of tf.transforms ?? []) {
        if (tr.header.frame_id === "map" && tr.child_frame_id === "odom") {
          const { translation, rotation } = tr.transform;
          this.#mapToOdom = { x: translation.x, y: translation.y, yaw: 2 * Math.atan2(rotation.z, rotation.w) };
        }
      }
    } else if (msg.topic !== undefined && PLAN_TOPICS.includes(msg.topic) && this.onPlan) {
      // Everything is drawn in the world frame (= odom in sim, since the
      // driver's odometry is ground truth). Map-frame plans are transformed
      // through AMCL's map->odom, so localization drift can't shift them.
      const path = msg.msg as {
        header?: { frame_id?: string };
        poses?: Array<{ pose: { position: { x: number; y: number }; orientation: { z: number; w: number } } }>;
      };
      if (!Array.isArray(path.poses)) return;
      const inMap = path.header?.frame_id === "map";
      const points = new Float32Array(path.poses.length * 2);
      path.poses.forEach((p, i) => {
        const [x, y] = inMap ? this.#mapToWorld(p.pose.position.x, p.pose.position.y) : [p.pose.position.x, p.pose.position.y];
        points[i * 2] = x;
        points[i * 2 + 1] = y;
      });
      const end = path.poses[path.poses.length - 1];
      const endYaw = end ? 2 * Math.atan2(end.pose.orientation.z, end.pose.orientation.w) : 0;
      const goal = end
        ? {
            x: points[points.length - 2],
            y: points[points.length - 1],
            yaw: inMap ? this.#yawMapToWorld(endYaw) : endYaw,
          }
        : null;
      this.onPlan(points, goal);
    } else if (msg.topic === "/cmd_vel_raw" && this.onCommand) {
      const twist = msg.msg as { linear: { x: number }; angular: { z: number } };
      this.onCommand(twist.linear.x, twist.angular.z);
    } else if (msg.topic === "/cmd_vel" && this.onFinalCommand) {
      const twist = msg.msg as { linear: { x: number }; angular: { z: number } };
      this.onFinalCommand(twist.linear.x, twist.angular.z);
    } else if (msg.topic === "/transformed_global_plan" && this.onFollowerPath) {
      const path = msg.msg as { poses?: Array<{ pose: { position: { x: number; y: number } } }> };
      if (!Array.isArray(path.poses)) return;
      const points = new Float32Array(path.poses.length * 2);
      path.poses.forEach((p, i) => {
        points[i * 2] = p.pose.position.x;
        points[i * 2 + 1] = p.pose.position.y;
      });
      this.onFollowerPath(points);
    } else if (msg.topic === "/nav/commanded_goal" && this.onCommandedGoal) {
      const ps = msg.msg as {
        header: { frame_id: string };
        pose: { position: { x: number; y: number }; orientation: { z: number; w: number } };
      };
      const rawYaw = 2 * Math.atan2(ps.pose.orientation.z, ps.pose.orientation.w);
      const inMap = ps.header.frame_id === "map";
      const [x, y] = inMap ? this.#mapToWorld(ps.pose.position.x, ps.pose.position.y) : [ps.pose.position.x, ps.pose.position.y];
      this.onCommandedGoal({
        x,
        y,
        yaw: inMap ? this.#yawMapToWorld(rawYaw) : rawYaw,
        frame: ps.header.frame_id,
      });
    } else if (msg.topic !== undefined && NAV_FEEDBACK_TOPICS.includes(msg.topic) && this.onNavFeedback) {
      const wrapper = msg.msg as {
        feedback?: {
          distance_remaining?: number;
          number_of_recoveries?: number;
          navigation_time?: { sec: number; nanosec: number };
        };
      };
      const fb = wrapper.feedback;
      if (!fb) return;
      this.onNavFeedback({
        distanceRemaining: fb.distance_remaining ?? 0,
        recoveries: fb.number_of_recoveries ?? 0,
        navTimeS: (fb.navigation_time?.sec ?? 0) + (fb.navigation_time?.nanosec ?? 0) / 1e9,
      });
    } else if (msg.topic === "/local_costmap/costmap_raw" && this.onCostmap) {
      const cm = msg.msg as {
        metadata?: {
          resolution: number;
          size_x: number;
          size_y: number;
          origin: { position: { x: number; y: number } };
        };
        data?: string | number[];
      };
      if (!cm.metadata || cm.data === undefined) return;
      this.onCostmap({
        resolution: cm.metadata.resolution,
        sizeX: cm.metadata.size_x,
        sizeY: cm.metadata.size_y,
        originX: cm.metadata.origin.position.x,
        originY: cm.metadata.origin.position.y,
        data: decodeCostmapData(cm.data),
      });
    } else if (msg.topic === "/scan" && this.onScan) {
      const scan = msg.msg as { angle_min: number; angle_increment: number; range_max: number; ranges: number[] };
      const { x, y, yaw } = this.#pose;
      const cos = Math.cos(yaw);
      const sin = Math.sin(yaw);
      const originX = x + LASER_OFFSET.x * cos;
      const originY = y + LASER_OFFSET.x * sin;
      const points: number[] = [];
      scan.ranges.forEach((r, i) => {
        if (!Number.isFinite(r) || r <= 0 || r >= scan.range_max) return;
        const a = yaw + scan.angle_min + i * scan.angle_increment;
        points.push(originX + r * Math.cos(a), originY + r * Math.sin(a), LASER_OFFSET.z);
      });
      this.onScan(new Float32Array(points));
    }
  }

  /** Freeze/unfreeze the simulated world (?navdebug freeze chip). Sim-only:
   * mars_sim_driver holds physics stepping while the robot stack keeps running. */
  setWorldPaused(on: boolean): void {
    this.#send({ op: "publish", topic: "/virtual_mars/pause", msg: { data: on } });
  }

  /** Send a navigation goal picked in WORLD coordinates (?navdebug set-goal
   * chip) -- converted into AMCL's map frame before publishing on /goal_pose,
   * the same route the webapp map's Set Goal uses. Zero stamp = "latest" to
   * TF; never wall time, the sim runs on ROS sim time. */
  publishGoalPose(worldX: number, worldY: number, worldYaw: number): void {
    // P_map = R(yaw)·P_world + t (world == odom in sim).
    const t = this.#mapToOdom;
    const c = t ? Math.cos(t.yaw) : 1;
    const s = t ? Math.sin(t.yaw) : 0;
    const x = t ? worldX * c - worldY * s + t.x : worldX;
    const y = t ? worldX * s + worldY * c + t.y : worldY;
    const yaw = t ? worldYaw + t.yaw : worldYaw;
    this.#send({
      op: "publish",
      topic: "/goal_pose",
      msg: {
        header: { stamp: { sec: 0, nanosec: 0 }, frame_id: "map" },
        pose: {
          position: { x, y, z: 0 },
          orientation: { x: 0, y: 0, z: Math.sin(yaw / 2), w: Math.cos(yaw / 2) },
        },
      },
    });
  }

  #send(payload: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(payload));
  }
}
