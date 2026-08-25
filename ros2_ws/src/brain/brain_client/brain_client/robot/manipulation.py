#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Manipulation — the arm + gripper command interface for skills.

Injected as ``self.manipulation``. Motions block until the arm settles and
raise :class:`ArmFailed` / :class:`ArmUnhealthy` on failure; pass
``block=False`` to return once the command is accepted and join later with
:meth:`Manipulation.wait`. Reads return the ROS-free
:class:`~brain_client.state.arm.Arm` dataclass.

Motions are not interrupted by a skill cancel — cancellation lands between
calls — so every method is safe in ``finally`` teardown. ``duration`` is
advisory: the hardware jerk limiter may extend it.

For continuous control (a UI slider, a visual-servoing loop), the goto
motions are the wrong shape — each is a rest-to-rest spline and they
serialize in the driver, so chaining them is inherently stop-start. Use
:meth:`Manipulation.stream_joints` instead: it feeds the driver's teleop
pass-through, which retargets the control loop smoothly.

The gripper (j6) runs current-based position control: the standing position
error IS the grip force, so re-commanding j6 from its measured position
drops a held object. Motions therefore carry the last *commanded* j6 (the
standing grip target) by default — grip once, then move freely.
"""

import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import rclpy
import rclpy.executors
from geometry_msgs.msg import PoseStamped, Twist
from mars_msgs.msg import ArmStatus
from mars_msgs.srv import GotoJS, GotoJSTrajectory
from rclpy.node import Node
from rclpy.subscription import Subscription
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger

from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.state.arm import Arm


@dataclass(frozen=True)
class Waypoint:
    """One stop on a cartesian path for :meth:`Manipulation.follow`.

    Metres in ``base_link``, radians; ``duration`` is the travel time from
    the previous waypoint — for the first waypoint, from wherever the arm
    is when the trajectory starts.
    """

    x: float
    y: float
    z: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    duration: float = 1.0


class Safety:
    """Skill-tunable caps, set via ``self.manipulation.safety``.

    ``max_ee_speed`` (m/s, None = uncapped) stretches cartesian move
    durations to cap straight-line end-effector speed. A tuning aid, not a
    certified limit: joint-space moves are not covered, and ``stop()``
    resets it.
    """

    def __init__(self, logger):
        self.logger = logger
        self._max_ee_speed: float | None = None

    @property
    def max_ee_speed(self) -> float | None:
        return self._max_ee_speed

    @max_ee_speed.setter
    def max_ee_speed(self, value: float | None) -> None:
        if value is not None and not value > 0.0:
            raise ValueError(f"max_ee_speed must be > 0 m/s or None, got {value!r}")
        self._max_ee_speed = None if value is None else float(value)

    def reset(self) -> None:
        self._max_ee_speed = None

    def capped_duration(self, distance: float, duration: float) -> float:
        """``duration``, stretched so ``distance`` over it respects the cap."""
        cap = self._max_ee_speed
        if cap is None or distance <= cap * duration:
            return duration
        stretched = distance / cap
        self.logger.debug(f"[arm] speed cap {cap:.2f} m/s stretches {duration:.2f}s to {stretched:.2f}s")
        return stretched


@dataclass
class _PendingMotion:
    """A non-blocking motion in flight."""

    future: Any  # rclpy.task.Future (untyped upstream)
    name: str
    deadline: float


class Manipulation:
    """Arm + gripper command interface. See the module docstring for the
    contract: blocking, raising, grip-preserving, teardown-safe."""

    # Gripper j6 radians: 0 = closed, 0.85 = fully open. Preload beyond
    # GRIPPER_MAX_STRENGTH overcurrent-trips the servo on a real object;
    # below _GRIPPER_SHUT_J6 the claw is still (tripped) shut after an open.
    GRIPPER_CLOSED = 0.0
    GRIPPER_OPEN = 0.85
    GRIPPER_MAX_STRENGTH = 0.6
    _GRIPPER_SHUT_J6 = 0.10

    # /mars/arm/state order: base yaw, shoulder, elbow, wrist pitch, wrist roll, claw.
    JOINT_NAMES = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")

    # Folded with j4 lifted to clear the floor; j1/j2 sit at their limits.
    # j4 negative pitches the gripper UP in this fold; -0.3 chosen live on
    # hardware (2026-08) via /mars/arm/goto_js_v2 previews.
    REST = [1.5708, -1.2195, 1.5723, -0.3, 0.0, 0.0031]
    ZERO = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # Grasp reach box (base_link metres).
    REACH_X = (0.22, 0.40)
    REACH_Y = (-0.10, 0.10)

    # stream_joints: slew-loop rate, per-joint speed clamp, and how long the
    # stream survives without a fresh target before idling out.
    STREAM_RATE_HZ = 30.0
    STREAM_MAX_SPEED = 1.8  # rad/s
    STREAM_IDLE_S = 0.4

    # A motion completes on ARRIVAL, and motions serialize in the driver, so a
    # queued one waits out whatever is already moving before its own duration
    # even starts. Wait past the driver's own completion bound (total + 5s)
    # rather than racing it: a failed wait must mean failed or hung, not merely
    # queued behind another arm client.
    _MOTION_SLACK_S = 6.0

    # /mars/arm/status samples torque state, then runs a multi-second servo
    # sweep before publishing — a heartbeat can arrive after a local torque
    # command yet predate it. Local writes stay authoritative for this long.
    _TORQUE_STATUS_LAG_S = 2.0

    def __init__(self, node: Node, logger, lazy: bool = False):
        """``lazy`` defers the state feeds until :meth:`start`."""
        self.node = rclpy.create_node(f"{node.get_name()}_manipulation_interface")
        self.logger = logger
        self.safety = Safety(logger)
        # Spins only while a skill is active; parked between skills to keep
        # ~600 msg/s of callback dispatch off the CPU.
        self._executor: rclpy.executors.SingleThreadedExecutor | None = None
        self._executor_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._ik_lock = threading.Lock()

        self._ik_target_pub = self.node.create_publisher(Twist, "/ik_delta", 10)

        # stream_joints state: the latest requested target, what the slew loop
        # last published, and the thread that walks one toward the other.
        self._stream_pub = self.node.create_publisher(Float64MultiArray, "/mars/arm/commands", 10)
        self._stream_lock = threading.Lock()
        self._stream_target: list[float] | None = None
        self._stream_cmd: list[float] | None = None
        self._stream_speed = self.STREAM_MAX_SPEED
        self._stream_stamp = 0.0
        self._stream_thread: threading.Thread | None = None

        self._ik_solution: JointState | None = None
        self._fk_pose: PoseStamped | None = None
        self._arm_state: JointState | None = None
        self._torque_enabled: bool | None = None
        self._torque_stamp = 0.0
        self._status_torque: bool | None = None
        self._status_stamp = 0.0
        # Standing grip target: the last COMMANDED j6 (see module docstring).
        self._grip_target: float | None = None
        self._pending: _PendingMotion | None = None

        # Subscriptions are created once and never destroyed: destroying one
        # while the executor spins races _take_subscription (InvalidHandle
        # crash). stop() gates the callbacks via _active instead.
        self._active = False
        self._ik_solution_sub: Subscription | None = None
        self._fk_pose_sub: Subscription | None = None
        self._arm_state_sub: Subscription | None = None
        self._arm_status_sub: Subscription | None = None

        if not lazy:
            self.start()

        # No blocking wait_for_service anywhere: keeps the executor responsive.
        self._goto_js_client = self.node.create_client(GotoJS, "/mars/arm/goto_js_v2")
        self._goto_js_traj_client = self.node.create_client(GotoJSTrajectory, "/mars/arm/goto_js_trajectory")

        self._torque_on_client = self.node.create_client(Trigger, "/mars/arm/torque_on")
        self._torque_off_client = self.node.create_client(Trigger, "/mars/arm/torque_off")
        self._reboot_servos_client = self.node.create_client(Trigger, "/mars/arm/reboot")

        self.logger.info("Manipulation initialized")

    # --- lifecycle (framework surface, not skill API) ---

    def _spin_executor(self, executor):
        try:
            executor.spin()
        except Exception as e:
            self.logger.error(f"[Manipulation] Executor stopped unexpectedly: {e}")

    def start(self):
        """Enable the arm-state feeds; idempotent."""
        with self._lifecycle_lock:
            self._active = True
            if self._executor is None:
                self._executor = rclpy.executors.SingleThreadedExecutor()
                self._executor.add_node(self.node)
                self._executor_thread = threading.Thread(
                    target=self._spin_executor, args=(self._executor,), daemon=True
                )
                self._executor_thread.start()
            if self._arm_state_sub is not None:
                return
            self._ik_solution_sub = self.node.create_subscription(
                JointState, "/ik_solution", self._ik_solution_callback, 10
            )
            self._fk_pose_sub = self.node.create_subscription(PoseStamped, "/fk_pose", self._fk_pose_callback, 10)
            self._arm_state_sub = self.node.create_subscription(
                JointState, "/mars/arm/state", self._arm_state_callback, 10
            )
            self._arm_status_sub = self.node.create_subscription(
                ArmStatus, "/mars/arm/status", self._arm_status_callback, 10
            )

    def stop(self):
        """Park the executor and clear cached state; the subscriptions stay
        alive (see __init__)."""
        self.stream_stop()
        with self._lifecycle_lock:
            self._active = False
            if self._executor is not None:
                self._executor.shutdown()
                if self._executor_thread is not None:
                    self._executor_thread.join(timeout=2.0)
                self._executor = None
                self._executor_thread = None
        self._ik_solution = None
        self._fk_pose = None
        self._arm_state = None
        # Torque state goes stale while parked (a webapp toggle lands on gated
        # callbacks); report None until either side reports fresh.
        self._torque_enabled = None
        self._torque_stamp = 0.0
        self._status_torque = None
        self._status_stamp = 0.0
        pending = self._pending
        if pending is not None and not pending.future.done():
            # The goto services cannot preempt: the arm will finish the motion.
            self.logger.warning(f"[Manipulation] {pending.name} still in flight at stop(); it will run to completion")
        self._pending = None
        # _grip_target survives: a skill can end while still holding an object.
        self.safety.reset()

    def shutdown(self):
        self.stop()
        self.node.destroy_node()

    def halt(self) -> None:
        """Framework brake hook (Skill._halt_interfaces).

        A joint stream is braked by stopping it — the arm holds where it is.
        Goto motions run out: the services cannot preempt an in-flight motion,
        and halting must not freeze at measured joints (the j6 re-seed would
        drop a held object) nor torque off (the arm would fall).
        """
        self.stream_stop()
        self.logger.debug("[Manipulation] halt(): stream stopped; goto motions are committed")

    # --- callbacks (private executor thread) ---

    def _ik_solution_callback(self, msg: JointState):
        if self._active:
            self._ik_solution = msg

    def _fk_pose_callback(self, msg: PoseStamped):
        if self._active:
            self._fk_pose = msg

    def _arm_state_callback(self, msg: JointState):
        if self._active:
            self._arm_state = msg

    def _arm_status_callback(self, msg):
        if self._active:
            self._status_torque = bool(msg.is_torque_enabled)
            self._status_stamp = time.monotonic()

    # --- reads ---

    @property
    def last_fk_pose(self) -> PoseStamped | None:
        """Latest cached /fk_pose message, no wait — for 50 Hz ambient reads."""
        return self._fk_pose

    @property
    def pose(self) -> Arm:
        """Live end-effector pose. Waits up to 1 s for the first FK fix;
        raises ArmFailed if none arrives."""
        msg = self._fk_pose
        if msg is None:
            deadline = time.monotonic() + 1.0
            while msg is None and time.monotonic() < deadline:
                time.sleep(0.02)  # committed: bounded read, teardown-safe
                msg = self._fk_pose
            if msg is None:
                raise ArmFailed("no end-effector pose on /fk_pose — is the IK node running?")
        return self._arm_from_fk(msg)

    @property
    def joint_names(self) -> tuple[str, ...]:
        """Joint names in command order (j6 is the gripper claw)."""
        return self.JOINT_NAMES

    @property
    def torque_enabled(self) -> bool | None:
        """Freshest known torque state (local commands vs the /mars/arm/status
        heartbeat, which catches webapp toggles); None until either reports."""
        if self._status_stamp > self._torque_stamp + self._TORQUE_STATUS_LAG_S:
            return self._status_torque
        return self._torque_enabled

    @classmethod
    def clamp_reach(cls, x, y):
        """Clamp (x, y) into the graspable reach box."""
        return (max(cls.REACH_X[0], min(cls.REACH_X[1], x)), max(cls.REACH_Y[0], min(cls.REACH_Y[1], y)))

    # --- motion ---

    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        *,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: float = 1.5,
        grip: float | None = None,
        tolerance_xy: float | None = 0.05,
        tolerance_z: float | None = 0.10,
        block: bool = True,
    ) -> Arm | None:
        """Move the end-effector to a cartesian pose (``base_link`` metres,
        radians) and return the settled pose.

        FK-verified: if the settled pose is off by more than ``tolerance_xy``
        / ``tolerance_z``, recover (reboot + torque on) and retry once, then
        raise ArmUnhealthy. ``tolerance_z`` is looser because descents
        legitimately stop early on contact; either tolerance may be None to
        skip that axis, and with both None the move is unverified — the
        service result is trusted, failure raises ArmFailed, no recovery.
        Raises ArmFailed when the pose is unreachable. ``grip`` defaults to
        the standing grip target so a held object stays held. ``block=False``
        returns None once the command is accepted (IK still runs inline);
        join with :meth:`wait`.
        """
        joints = self._solve_ik(x, y, z, roll, pitch, yaw)
        if joints is None:
            raise ArmFailed(f"IK found no solution for ({x:.2f}, {y:.2f}, {z:.2f})")
        if self.safety.max_ee_speed is not None:
            duration = self.safety.capped_duration(math.dist(self.pose.position, (x, y, z)), duration)
        target = joints + [self._grip_or(grip)]
        where = f"({x:.2f}, {y:.2f}, {z:.2f})"

        if not block or (tolerance_xy is None and tolerance_z is None):
            if not self._goto(target, duration, wait=block):
                raise ArmFailed(f"arm move to {where} rejected or did not complete")
            return self._settled_pose("move") if block else None

        for attempt in (1, 2):
            ok = self._goto(target, duration, wait=True)
            settled = self._read_pose_after_motion()
            if ok and settled is not None:
                err_xy = math.hypot(settled.x - x, settled.y - y)
                err_z = abs(settled.z - z)
                if (tolerance_xy is None or err_xy <= tolerance_xy) and (tolerance_z is None or err_z <= tolerance_z):
                    return settled
                why = f"err_xy={err_xy:.3f} err_z={err_z:.3f}"
            else:
                why = f"ok={ok} settled={settled}"
            self.logger.warning(f"[arm] not tracking ({why}) — {'recovering' if attempt == 1 else 'giving up'}")
            if attempt == 1:
                self.recover()
        raise ArmUnhealthy(f"arm failed to reach {where}")

    def move_by(
        self,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        *,
        droll: float = 0.0,
        dpitch: float = 0.0,
        dyaw: float = 0.0,
        duration: float = 0.5,
        grip: float | None = None,
        tolerance_xy: float | None = 0.05,
        tolerance_z: float | None = 0.10,
        block: bool = True,
    ) -> Arm | None:
        """Nudge the end-effector by offsets from its *measured* pose (so a
        servoing loop corrects from where the arm actually is); delegates to
        :meth:`move_to`. RPY deltas add per axis — fine for nudges, wrong for
        large rotations.
        """
        cur = self.pose
        roll, pitch, yaw = cur.rpy
        return self.move_to(
            cur.x + dx,
            cur.y + dy,
            cur.z + dz,
            roll=roll + droll,
            pitch=pitch + dpitch,
            yaw=yaw + dyaw,
            duration=duration,
            grip=grip,
            tolerance_xy=tolerance_xy,
            tolerance_z=tolerance_z,
            block=block,
        )

    def follow(self, waypoints: Sequence[Waypoint], *, grip: float | None = None, block: bool = True) -> Arm | None:
        """Sweep through waypoints as one smooth trajectory, starting from
        wherever the arm is (the driver prepends the current pose).

        Returns the settled pose, unverified — contact along the path is
        legitimate. Raises ArmFailed on IK failure or rejection. ``grip``
        defaults to the standing grip target; ``block=False`` returns None
        once accepted (join with :meth:`wait`).
        """
        wps = list(waypoints)
        if not wps:
            raise ArmFailed("follow() needs at least one waypoint")
        j6 = self._grip_or(grip)

        waypoint_joints = []
        for i, wp in enumerate(wps):
            joints = self._solve_ik(wp.x, wp.y, wp.z, wp.roll, wp.pitch, wp.yaw)
            if joints is None:
                raise ArmFailed(f"IK found no solution for waypoint {i} ({wp.x:.2f}, {wp.y:.2f}, {wp.z:.2f})")
            waypoint_joints.append(joints + [j6])

        if len(waypoint_joints) == 1:
            duration = wps[0].duration
            if self.safety.max_ee_speed is not None:
                duration = self.safety.capped_duration(
                    math.dist(self.pose.position, (wps[0].x, wps[0].y, wps[0].z)), duration
                )
            # One waypoint goes through the plain goto service for its
            # cubic-spline, jerk-limited profile; the trajectory service
            # interpolates linearly.
            if not self._goto(waypoint_joints[0], duration, wait=block):
                raise ArmFailed("arm move rejected or did not complete")
        else:
            # One duration per waypoint: seg_durations[0] paces the driver's
            # prepended approach from the current pose.
            seg_durations = [wp.duration for wp in wps]
            if self.safety.max_ee_speed is not None:
                pts = [self.pose.position] + [(wp.x, wp.y, wp.z) for wp in wps]
                seg_durations = [
                    self.safety.capped_duration(math.dist(pts[k], pts[k + 1]), d) for k, d in enumerate(seg_durations)
                ]
            if not self._send_trajectory(waypoint_joints, seg_durations, wait=block):
                raise ArmFailed("trajectory rejected, failed, or preempted")
        return self._settled_pose("trajectory") if block else None

    def move_joints(self, joints: Sequence[float], *, duration: float = 3.0, block: bool = True) -> None:
        """Move to joint positions (radians): 5 values keep the standing grip,
        6 values set it. Raises ArmFailed on rejection or non-completion."""
        target = [float(j) for j in joints]
        if len(target) == 5:
            target.append(self._grip_or(None))
        if len(target) != 6:
            raise ArmFailed(f"expected 5 or 6 joint positions, got {len(target)}")
        if not self._goto(target, duration, wait=block):
            raise ArmFailed("arm move rejected or did not complete")

    def rest(self, *, duration: float = 3.0) -> None:
        """Fold to the rest pose, keeping the grip; re-commands once after a
        pause so the servos settle under a load shifted by the fold."""
        target = list(self.REST)
        target[5] = self._grip_or(None)
        if not self._goto(target, duration, wait=True):
            raise ArmFailed("rest move rejected or did not complete")
        time.sleep(0.3)  # committed: teardown-safe by design
        if not self._goto(target, 1.0, wait=True):
            raise ArmFailed("rest settle move rejected or did not complete")

    # --- streaming ---

    def stream_joints(self, joints: Sequence[float], *, max_speed: float | None = None) -> None:
        """Continuously retarget the arm (teleop-style streaming); returns at
        once.

        Feeds the driver's ``/mars/arm/commands`` pass-through — the smooth
        path leader-arm teleop uses, soft gains, no per-step splines — via a
        STREAM_RATE_HZ slew loop that clamps each joint to ``max_speed``
        (default STREAM_MAX_SPEED rad/s), so a far target ramps instead of
        snapping. Call repeatedly from a UI or servoing loop; the stream idles
        out STREAM_IDLE_S after the last call, and the last streamed j6
        becomes the standing grip target. 5 values keep the standing grip,
        6 set it. Any discrete motion stops the stream first, and a skill
        halt stops it too. Unverified by design — no FK check, no recovery;
        use :meth:`move_to` for verified positioning.
        """
        target = [float(j) for j in joints]
        if len(target) == 5:
            target.append(self._grip_or(None))
        if len(target) != 6:
            raise ArmFailed(f"expected 5 or 6 joint positions, got {len(target)}")
        if self.torque_enabled is False:
            raise ArmFailed("arm torque is disabled")
        with self._stream_lock:
            # The exiting slew thread clears _stream_thread under this lock, so
            # this check can't see a dying thread as alive and strand a target.
            starting = self._stream_thread is None or not self._stream_thread.is_alive()
            if starting:
                # Seed the slew from measured state so the first tick ramps
                # from where the arm actually is. Without a measurement the
                # clamp would be seeded at the target — one unclamped snap —
                # so refuse instead.
                state = self._arm_state
                if state is None or len(state.position) < 6:
                    raise ArmFailed("no measured joint state to seed the stream — is /mars/arm/state up?")
                seed = [float(p) for p in state.position[:6]]
                # ...except j6: seeding the claw from its MEASURED position is
                # the drop-the-object operation (module docstring) — a gripped
                # claw stalls short of its goal and that error is the grip
                # force. Seed from the standing grip target instead.
                seed[5] = self._grip_or(None)
                self._stream_cmd = seed
            self._stream_target = target
            self._stream_speed = float(max_speed) if max_speed is not None else self.STREAM_MAX_SPEED
            self._stream_stamp = time.monotonic()
            if starting:
                self._stream_thread = threading.Thread(target=self._stream_run, daemon=True)
                self._stream_thread.start()

    def stream_stop(self) -> None:
        """Stop streaming; the arm holds its current position. Idempotent.

        The grip handoff happens here, synchronously — if the slew thread did
        it on exit, a motion issued right after stream_stop (``_goto`` sets
        its own grip target) could lose the race and have its j6 stomped by
        the dying thread up to a tick later.
        """
        with self._stream_lock:
            if self._stream_target is not None and self._stream_cmd is not None:
                self._grip_target = float(self._stream_cmd[5])
            self._stream_target = None

    def _stream_run(self) -> None:
        dt = 1.0 / self.STREAM_RATE_HZ
        while True:
            time.sleep(dt)  # own daemon thread, not skill context
            with self._stream_lock:
                stopped = self._stream_target is None
                if stopped or time.monotonic() - self._stream_stamp > self.STREAM_IDLE_S:
                    # Idle-timeout: hand the streamed j6 to the standing grip
                    # target or the next motion yanks the claw back to a stale
                    # one. An explicit stream_stop already did this — writing
                    # here again could stomp a grip a motion set since.
                    if not stopped and self._stream_cmd is not None:
                        self._grip_target = float(self._stream_cmd[5])
                    self._stream_target = None
                    self._stream_thread = None  # under the lock: see stream_joints
                    return
                step = self._stream_speed * dt
                assert self._stream_cmd is not None  # seeded before the thread starts
                for i in range(6):
                    delta = self._stream_target[i] - self._stream_cmd[i]
                    self._stream_cmd[i] += max(-step, min(step, delta))
                msg = Float64MultiArray()
                msg.data = [float(v) for v in self._stream_cmd]
                # Publish INSIDE the lock: once stream_stop returns, no further
                # frame can land — a goto issued right after can't have its
                # gains flipped or its target overwritten by a straggler, and
                # shutdown can't destroy the node under an in-flight publish.
                self._stream_pub.publish(msg)

    # --- non-blocking motion ---

    @property
    def moving(self) -> bool:
        """True while a non-blocking motion is still in flight. A motion past
        its deadline counts as no longer moving, so a poll loop terminates
        even if the service hangs; :meth:`wait` surfaces the failure."""
        pending = self._pending
        return pending is not None and not pending.future.done() and time.monotonic() < pending.deadline

    def wait(self, timeout: float | None = None) -> Arm:
        """Join the in-flight non-blocking motion and return the settled pose
        (with nothing in flight, just read the pose). Raises ArmFailed on
        failure or timeout; ``timeout`` overrides the default patience of the
        motion's duration plus a margin.
        """
        self._join_pending(timeout)
        return self._settled_pose("motion")

    def _join_pending(self, timeout: float | None = None) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None:
            budget = timeout if timeout is not None else max(0.0, pending.deadline - time.monotonic())
            if not self._await_motion_result(pending.future, pending.name, budget):
                raise ArmFailed(f"{pending.name} motion failed or did not complete in time")

    # --- gripper ---

    def gripper_open(self, percent: float = 100.0, *, duration: float = 0.5, block: bool = True) -> None:
        """Open the claw to ``percent`` (100 = fully open).

        The servo can overcurrent-trip and stay shut, so a blocking open
        verifies the claw moved, rebooting and retrying once before raising
        ArmUnhealthy. Raises ArmFailed on rejection. ``block=False`` skips
        verification; join with :meth:`wait`.
        """
        percent = max(0.0, min(100.0, percent))
        target = self.GRIPPER_CLOSED + (self.GRIPPER_OPEN - self.GRIPPER_CLOSED) * (percent / 100.0)
        if not block:
            if not self._command_gripper(target, duration, False):
                raise ArmFailed("gripper command rejected")
            return
        for attempt in (1, 2):
            if not self._command_gripper(target, duration, True):
                raise ArmFailed("gripper command rejected or did not complete")
            # Only targets above the shut threshold are verifiable.
            if target < self._GRIPPER_SHUT_J6:
                return
            self._settle()
            j6 = self._measured_gripper()
            if j6 is None or j6 >= self._GRIPPER_SHUT_J6:
                return
            if attempt == 1:
                self.logger.warning(f"Gripper did not open (j6={j6:.3f}); rebooting servos, then retrying")
                self.recover()
        raise ArmUnhealthy("gripper did not open (servo tripped shut)")

    def gripper_close(self, strength: float = 0.0, *, duration: float = 0.5, block: bool = True) -> None:
        """Close the claw. ``strength`` is radians of grip preload past the
        closed stop, clamped to GRIPPER_MAX_STRENGTH; it becomes the standing
        grip target. Raises ArmFailed on rejection."""
        strength = min(abs(strength), self.GRIPPER_MAX_STRENGTH)
        if not self._command_gripper(self.GRIPPER_CLOSED - strength, duration, block):
            raise ArmFailed("gripper command rejected or did not complete")

    # --- servo power / recovery ---

    def torque_on(self) -> bool:
        success = self._call_trigger(self._torque_on_client, "Torque on", "Torque enabled on arm")
        if success:
            self._torque_enabled = True
            self._torque_stamp = time.monotonic()
        return success

    def torque_off(self) -> bool:
        self.stream_stop()  # never stream into a limp arm
        success = self._call_trigger(self._torque_off_client, "Torque off", "Torque disabled on arm")
        if success:
            self._torque_enabled = False
            self._torque_stamp = time.monotonic()
            self._grip_target = None  # a limp claw holds nothing
        return success

    def reboot_servos(self) -> bool:
        """Reboot the arm servos, clearing hardware errors; leaves torque off."""
        self.stream_stop()  # never stream across a servo power-cycle
        success = self._call_trigger(
            self._reboot_servos_client,
            "Reboot servos",
            "Servos rebooted; arm torque is disabled",
            timeout_sec=10.0,
        )
        if success:
            self._torque_enabled = False
            self._torque_stamp = time.monotonic()
            self._grip_target = None
        return success

    def recover(self) -> None:
        """Reboot servos, re-enable torque, settle (~2.5 s); preserves the
        standing grip target so a mid-pick retry keeps the grip preload."""
        grip = self._grip_target
        self.logger.warning("[arm] recovering (reboot + torque on)")
        self.reboot_servos()
        time.sleep(2.0)  # committed: teardown-safe by design
        self.torque_on()
        time.sleep(0.5)  # committed: servo re-init settle
        self._grip_target = grip

    # --- internals ---

    def _settle(self, seconds: float = 0.05):
        """Yield while the private executor thread delivers pending callbacks."""
        time.sleep(seconds)

    def _measured_gripper(self) -> float | None:
        state = self._arm_state
        try:
            return state.position[5] if state is not None else None
        except (AttributeError, IndexError, TypeError):
            return None

    def _grip_or(self, explicit: float | None) -> float:
        """The j6 a motion should carry: explicit > standing target > measured > 0."""
        if explicit is not None:
            return float(explicit)
        if self._grip_target is not None:
            return self._grip_target
        measured = self._measured_gripper()
        return float(measured) if measured is not None else 0.0

    def _solve_ik(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        timeout: float = 2.0,
    ) -> list[float] | None:
        """IK for a cartesian pose: the 5 arm joints, or None on failure.

        The IK node speaks topics (/ik_delta in, /ik_solution out) with no
        correlation id, so requests serialize on _ik_lock and failure
        surfaces as a timeout.
        """
        with self._ik_lock:
            self._ik_solution = None

            target = Twist()  # /ik_delta is an ABSOLUTE pose despite the name
            target.linear.x = x
            target.linear.y = y
            target.linear.z = z
            target.angular.x = roll
            target.angular.y = pitch
            target.angular.z = yaw
            self._ik_target_pub.publish(target)

            start_time = time.time()
            while time.time() - start_time < timeout:
                self._settle(0.01)
                if self._ik_solution is not None:
                    joint_positions = list(self._ik_solution.position)
                    if len(joint_positions) == 0:
                        self.logger.error("[Manipulation] IK solver returned empty solution (IK failed)")
                        return None
                    # Callers append j6 unconditionally, so anything but the
                    # 5 arm joints would build a malformed 6-joint command.
                    if len(joint_positions) != 5:
                        self.logger.error(f"[Manipulation] IK returned {len(joint_positions)} joints, expected 5")
                        return None
                    return joint_positions

        self.logger.error(f"[Manipulation] IK solution timeout after {timeout}s")
        return None

    def _goto(self, joint_positions: list[float], duration: float, wait: bool) -> bool:
        """Send a 6-joint GotoJS command; the commanded j6 becomes the
        standing grip target on success."""
        self.stream_stop()  # a discrete motion supersedes any live stream
        if len(joint_positions) != 6:
            self.logger.error(f"Expected 6 joint positions, got {len(joint_positions)}")
            return False
        if self.torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False
        if self._goto_js_client is None or not self._goto_js_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJS v2 service is not ready")
            return False

        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = [float(j) for j in joint_positions]
        request.time = float(duration)

        self._pending = None  # a new command supersedes any unjoined motion
        try:
            future = self._goto_js_client.call_async(request)
            if wait:
                if not self._await_motion_result(future, "GotoJS v2", duration + self._MOTION_SLACK_S):
                    return False
            else:
                self._pending = _PendingMotion(future, "GotoJS v2", time.monotonic() + duration + self._MOTION_SLACK_S)
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJS v2: {e}")
            return False

        self._grip_target = float(joint_positions[5])
        return True

    def _send_trajectory(
        self, waypoint_joints: list[list[float]], seg_durations: list[float], wait: bool = True
    ) -> bool:
        """Send a multi-waypoint GotoJSTrajectory with one duration per
        waypoint; the driver prepends the current pose as waypoint 0, and
        seg_durations[0] paces that approach."""
        self.stream_stop()  # a discrete motion supersedes any live stream
        if self.torque_enabled is False:
            self.logger.error("[Manipulation] Arm torque is disabled")
            return False
        if self._goto_js_traj_client is None or not self._goto_js_traj_client.service_is_ready():
            self.logger.error("[Manipulation] GotoJSTrajectory service not ready")
            return False

        num_joints = len(waypoint_joints[0])
        flat_waypoints: list[float] = []
        for wj in waypoint_joints:
            flat_waypoints.extend(float(j) for j in wj)
        seg_durations = [float(d) for d in seg_durations]

        request = GotoJSTrajectory.Request()
        request.waypoints = Float64MultiArray()
        request.waypoints.data = flat_waypoints
        request.num_joints = num_joints
        request.segment_durations = seg_durations

        total_time = sum(seg_durations)
        self._pending = None  # a new command supersedes any unjoined motion
        try:
            future = self._goto_js_traj_client.call_async(request)
            if wait:
                if not self._await_motion_result(future, "GotoJSTrajectory", total_time + self._MOTION_SLACK_S):
                    return False
            else:
                self._pending = _PendingMotion(
                    future, "GotoJSTrajectory", time.monotonic() + total_time + self._MOTION_SLACK_S
                )
        except Exception as e:
            self.logger.error(f"[Manipulation] Exception calling GotoJSTrajectory: {e}")
            return False

        self._grip_target = float(waypoint_joints[-1][5])
        return True

    def _command_gripper(self, j6: float, duration: float, blocking: bool) -> bool:
        """Send a joint command that moves only the gripper to ``j6``.

        Joins any unjoined non-blocking motion first (raising on its failure):
        j1-j5 are re-sent from measured state, which would otherwise retarget
        an arm still in flight at a transient pose."""
        self._join_pending()
        if self._arm_state is None:
            self.logger.error("No arm state available")
            return False

        self._settle()

        positions = list(self._arm_state.position)
        if len(positions) < 6:
            positions.extend([0.0] * (6 - len(positions)))
        positions[5] = j6
        return self._goto(positions, duration, wait=blocking)

    def _arm_from_fk(self, msg: PoseStamped) -> Arm:
        return Arm.from_fk(msg, gripper=self._measured_gripper())

    def _read_pose_after_motion(self) -> Arm | None:
        self._settle()
        msg = self._fk_pose
        return None if msg is None else self._arm_from_fk(msg)

    def _settled_pose(self, what: str) -> Arm:
        settled = self._read_pose_after_motion()
        if settled is None:
            raise ArmFailed(f"no end-effector pose after {what} — is the IK node running?")
        return settled

    def _await_result(self, future, name: str, timeout_sec: float):
        """Block on a service future; its result, or None on timeout. Waits on
        an Event, never spins — the private executor thread delivers."""
        if not future.done():
            done = threading.Event()
            future.add_done_callback(lambda _future: done.set())
            if not done.wait(timeout=timeout_sec):
                self.logger.error(f"[Manipulation] {name} call timed out")
                return None
        result = future.result()
        if result is None:
            self.logger.error(f"[Manipulation] {name} call timed out")
        return result

    def _await_motion_result(self, future, name: str, timeout_sec: float) -> bool:
        result = self._await_result(future, name, timeout_sec)
        if result is None:
            return False
        if not result.success:
            self.logger.error(f"[Manipulation] {name} returned failure")
            return False
        return True

    def _call_trigger(self, client, action_name: str, success_msg: str, timeout_sec: float = 2.0) -> bool:
        if not client.service_is_ready():
            self.logger.error(f"[Manipulation] {action_name} service not ready")
            return False

        try:
            result = self._await_result(client.call_async(Trigger.Request()), action_name, timeout_sec)
            if result is None:
                return False
            if not result.success:
                self.logger.error(f"{action_name} failed: {result.message}")
                return False
            self.logger.info(success_msg)
            return True
        except Exception as e:
            self.logger.error(f"Exception calling {action_name}: {e}")
            return False
