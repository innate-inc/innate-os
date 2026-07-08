// Robot-telemetry overlay source for the webapp's SimSession: subscribe to
// the robot's lidar and Nav2's planned path over rosbridge and emit
// world-frame points. Pose/joints for the 3D view come from the world
// server's observer stream (worldStateController) -- these stay here
// deliberately: they are robot software outputs, so the robot's own
// pipeline is the honest source for the overlays. Read-only --
// teleop/commands go through the webapp's own rosbridge client.
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

export class RosbridgePhysicsController {
  /** World-frame lidar hit points [x0,y0,z0, x1,...], null-range rays skipped. */
  onScan?: (points: Float32Array) => void;
  /** Planned-path ground points [x0,y0, x1,y1, ...]; empty = plan cleared. */
  onPlan?: (points: Float32Array) => void;
  #scanEnabled = false;

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
      // The planner republishes ~1Hz while driving; queue_length 1 keeps it
      // latest-wins like the other feeds.
      for (const topic of PLAN_TOPICS) {
        this.#send({ op: "subscribe", topic, type: "nav_msgs/msg/Path", throttle_rate: 250, queue_length: 1 });
      }
      if (this.#scanEnabled) this.#subscribeScan();
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
    } else if (msg.topic !== undefined && PLAN_TOPICS.includes(msg.topic) && this.onPlan) {
      // Plan poses arrive in the map/odom frame; the sim's world frame is the
      // map frame (ground-truth-seeded localization), so use them directly --
      // the same assumption the webapp's 2D map widget makes.
      const path = msg.msg as { poses?: Array<{ pose: { position: { x: number; y: number } } }> };
      if (!Array.isArray(path.poses)) return;
      const points = new Float32Array(path.poses.length * 2);
      path.poses.forEach((p, i) => {
        points[i * 2] = p.pose.position.x;
        points[i * 2 + 1] = p.pose.position.y;
      });
      this.onPlan(points);
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

  #send(payload: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(payload));
  }
}
