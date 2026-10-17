"""A turn-based embodied agent driven by any chat model, acting through the
tools MARS's own brain is given.

WHAT THIS IS FOR. The benchmark's second hard requirement is that it can be
pointed at a different agent architecture without being rewritten. This is the
proof: an agent that shares nothing with innate-os's brain process except the
seam every agent in this harness uses -- reset / act / hear / bind_events --
and is driven by an LLM.

WHAT IT CAN DO IS WHAT THE ROBOT CAN DO. Its tools are the ones the brain on
innate-os main declares in the sim: the default agent's skills
(intro_agent.get_skills), go_to_point_in_view and wait, and plain text is
speech. innate_roster.py reads all of that from main's sources and says what
runs in place of Nav2, Gemini and the arm. It used to have eight harness
actions instead -- turn, forward, grab what is in front -- which the real robot
does not have, so whatever it certified was certified for a different robot.

WHAT THE AGENT IS ALLOWED TO SEE, and why the list is short. What the brain
sees each update (brain/utils.observation_text): the head camera frame, the
wrist camera frame, a status line with its pose and any running skill, and the
events since its last turn -- what it heard, what its last call returned, how
its skills ended. Plus the brief and its turn budget. It does NOT get the goal
list, prop names, prop coordinates, or the occupancy grid.

WHY IT IS TURN-BASED OVER A CONTINUOUS SIM. The sim advances 0.05 s per control
tick and a model call takes seconds of wall time. A skill runs over many ticks;
while the model thinks with no skill running the robot holds still, as the real
one does between turns. A skill keeps running while the model is consulted
mid-skill, as on the robot.

TURNS ARE THE BUDGET. One model call is one turn. The cap exists because an
agent stuck in a loop otherwise burns the entire sim-time limit at a few
seconds of API latency per tick.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import innate_roster as R
from runner import describe

# A skill that has not ended in this many sim-seconds is abandoned: the nav
# follower's own progress check normally ends it long before.
SKILL_TIMEOUT_S = 240.0


@dataclass
class Turn:
    """One model call and what came of it, for the episode's own record."""

    t: float
    action: str
    args: dict
    result: str
    latency_s: float = 0.0
    say: str = ""


@dataclass
class Observation:
    """Everything the agent is allowed to know at one turn."""

    brief: str
    elapsed_s: float
    heard: list[str] = field(default_factory=list)
    # The harness's own gripper state. Not printed -- the brain has no such
    # line; it has the wrist camera -- but a backend may keep its own books.
    carrying: str | None = None
    # What the agent's last call returned (the brain's function response).
    last_result: str = ""
    turns_left: int = 0
    # Paths to this turn's frames on disk, or None if a camera failed. Paths
    # rather than bytes: one backend hands a filename to a CLI, another needs
    # the bytes, and afterwards a person needs to see what the agent saw.
    image_path: str | None = None
    wrist_image_path: str | None = None
    # (x, y, yaw_rad) in the map frame: the brain's status line carries it.
    robot_pose: tuple[float, float, float] | None = None
    # "Skill <name> completed|failed|cancelled: <detail>" lines since the last turn.
    events: list[str] = field(default_factory=list)
    running_skill: str | None = None

    def as_text(self) -> str:
        status = f"[t+{self.elapsed_s:.0f}s]"
        if self.robot_pose is not None:
            x, y, yaw = self.robot_pose
            # Wrapped, as the brain's pose is: the sim's yaw joint is not, and a
            # probe read "heading=622°".
            status += f" pose: x={x:.2f}m y={y:.2f}m heading={math.degrees(math.remainder(yaw, 2 * math.pi)):.0f}°"
        if self.running_skill:
            status += f" | running skill: {self.running_skill}"
        parts = [f"TASK: {self.brief}", status]
        if self.last_result:
            parts.append(f"- Your last call returned: {self.last_result}")
        parts += [f"- {e}" for e in self.events]
        # Ambient and addressed speech arrive in the SAME list, unlabelled, as
        # the microphone delivers them. Tagging which is which here would hand
        # the agent the answer to every challenge that asks whether it can
        # tell them apart.
        parts += [f'- The user says: "{h}"' for h in self.heard]
        parts.append(f"Turns left: {self.turns_left}.")
        if self.wrist_image_path:
            parts.append("(second image is the arm wrist camera)")
        return "\n".join(parts)


# The skills _skill_gen runs. A skill main adds to the default agent's roster
# and this does not run fails test_innate_roster, rather than being offered and
# answering "no in-process implementation" mid-episode.
IMPLEMENTED = {R.NAVIGATE, "pick_any_object", "open_gripper", "close_gripper", "head_emotion", "wave"}

TOOLS = R.available(R.roster())
ACTIONS = R.menu(TOOLS)
# The same menu with the camera taken out of it.
ACTIONS_BLIND = R.menu(TOOLS, blind=True)
_BY_NAME = {t.name: t for t in TOOLS}


class BrainAgent:
    """Drives the robot from a model's one-tool-call-per-turn decisions."""

    name = "brain"

    def __init__(self, backend, max_turns: int = 40):
        self.backend = backend
        self.max_turns = max_turns
        self.turns = 0
        self.log: list[Turn] = []
        self.failed_reason = ""
        self.blocked_reason = ""
        # Declared so the runner builds this world's occupancy grid and hands
        # it to reset(): navigate_to_position plans on it.
        self.nav = None
        self._post = None
        self._brief = ""
        self._heard: list[str] = []
        self._events: list[str] = []
        self._carrying: str | None = None
        self._last_result = ""
        self._done = False
        # A running skill: (tool name, generator, started_t).
        self._skill = None
        self._next_call_t = 0.0
        # Model call state
        self._thread: threading.Thread | None = None
        self._pending: dict | None = None
        self._call_started = 0.0
        self._call_running: str | None = None
        self._capture: dict = {}
        # Where this episode's frames land, and whether the camera ever failed.
        self.frame_dir = Path(__file__).resolve().parent / "results" / "frames" / "unset"
        self.camera_errors = 0
        self.camera_error_note = ""

    @property
    def thinking(self) -> bool:
        """A model call is in flight.

        run_episode reads this to hold sim time to the wall clock. Headless,
        the sim runs about ten times real time, so without it one second of
        model latency costs the robot ten seconds of its world -- and every
        time limit in the suite is denominated in sim seconds.
        """
        return self._thread is not None and self._thread.is_alive()

    # -- harness seam -------------------------------------------------------

    def reset(self, mars, challenge, nav=None) -> None:
        self.turns = 0
        self.log = []
        self.failed_reason = ""
        self.blocked_reason = ""
        self.nav = nav
        self._brief = challenge.brief
        self._heard = []
        self._events = []
        self._carrying = None
        self._last_result = ""
        self._done = False
        self._skill = None
        self._next_call_t = 0.0
        self._thread = None
        self._pending = None
        # Backends with per-episode state (a task-stack, a scan memory) reset
        # it here rather than carrying stale plan/memory into a fresh world.
        reset_backend = getattr(self.backend, "reset", None)
        if reset_backend is not None:
            reset_backend()

    def bind_events(self, post) -> None:
        self._post = post

    def hear(self, line: dict) -> None:
        self._heard.append(line["text"])

    @property
    def done(self) -> bool:
        # A running skill keeps the episode open: the decision that started it
        # was charged to the turn budget, and act() has to drive it to its end.
        return self._done or (self.turns >= self.max_turns and self._skill is None and self._thread is None)

    # -- control tick -------------------------------------------------------

    def act(self, mars, t: float) -> None:
        if self._carrying is not None and hasattr(mars, "model"):
            # An object bigger than the jaws sits further out: centred on them,
            # the document box enclosed the wrist camera, which then showed an
            # empty claw.
            extra = max(0.0, max(mars.props.props[self._carrying].size) - R.GRIP_JAW_HALF_M)
            x, y, z = R.grip_point(mars, extra)
            mars.drop_prop_at(self._carrying, float(x), float(y), mars.pose()[2], z=float(z))
            # Drawn in the claw, but not "out" in the world: the judge has
            # always seen a held item as gone from where it was, and an item in
            # the claw over a delivery circle has not been delivered.
            mars.props.out.discard(self._carrying)
        # A running skill owns the base, and keeps it while the model thinks.
        if self._skill is not None:
            self._step_skill(mars, t)

        if self._thread is not None:
            if self._thread.is_alive():
                if self._skill is None:
                    mars.set_cmd_vel(0.0, 0.0)
                return
            self._thread = None
            self._apply(mars, t, self._pending or {})
            self._pending = None
            return

        if self.done:
            if self._skill is None:
                mars.set_cmd_vel(0.0, 0.0)
            return

        if self._skill is not None:
            # The brain is woken mid-skill by speech; then its only tool is stop.
            if self._heard:
                self._start_call(mars, t)
            return

        mars.set_cmd_vel(0.0, 0.0)
        if self._heard or self._events or t >= self._next_call_t:
            self._start_call(mars, t)

    # -- model call ---------------------------------------------------------

    def _frame(self, mars, camera: str, suffix: str) -> str | None:
        try:
            # MuJoCo raises for an unknown camera name, and the first version
            # swallowed that -- a vision agent ran blind and nobody could tell
            # from the results.
            jpg = mars.render_jpeg(camera)
        except Exception as exc:  # noqa: BLE001
            # Recorded, not swallowed: an agent that cannot see is a finding
            # about the harness, visible in the episode, not inferred from a score.
            self.camera_errors += 1
            if not self.camera_error_note:
                self.camera_error_note = describe(exc)
            return None
        path = self.frame_dir / f"turn_{self.turns:03d}{suffix}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpg)
        return str(path)

    def _observe(self, mars, t: float) -> Observation:
        heard, self._heard = self._heard, []
        events, self._events = self._events, []
        last, self._last_result = self._last_result, ""
        image = wrist = None
        if getattr(self.backend, "wants_image", False):
            image = self._frame(mars, "main", "")
            wrist = self._frame(mars, "wrist", "_wrist")
        # Frame, head pitch and pose are captured together, as the brain does:
        # go_to_point_in_view must ground a point in the frame the model saw.
        self._capture = {"image": image, "pitch": _head_pitch(mars), "pose": mars.pose()}
        return Observation(
            brief=self._brief,
            elapsed_s=t,
            heard=heard,
            carrying=_kind(self._carrying) if self._carrying else None,
            last_result=last,
            turns_left=self.max_turns - self.turns,
            image_path=image,
            wrist_image_path=wrist,
            robot_pose=mars.pose(),
            events=events,
            running_skill=self._skill[0] if self._skill else None,
        )

    def _start_call(self, mars, t: float) -> None:
        user_spoke = bool(self._heard)
        obs = self._observe(mars, t)
        self._call_running = obs.running_skill
        menu = R.menu(
            TOOLS,
            running=obs.running_skill,
            user_spoke=user_spoke,
            blind=not getattr(self.backend, "wants_image", False),
        )
        self._call_started = time.time()
        box: dict = {}

        def run() -> None:
            try:
                box.update(self.backend.decide(obs, menu) or {})
            except Exception as exc:  # noqa: BLE001 -- a backend failure is the
                # HARNESS's fault, not the robot's, and the report must be able
                # to say so. Recorded as its own action so it never looks like
                # the agent chose to stop.
                box["action"] = "_error"
                box["args"] = {"detail": describe(exc)}

        self._pending = box
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _apply(self, mars, t: float, decision: dict) -> None:
        self.turns += 1
        action = str(decision.get("action", "")).strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        latency = round(time.time() - self._call_started, 2)

        # Plain text is speech. An older envelope spoke through a "say" action.
        speech = str(decision.get("say") or (args.get("text", "") if action == "say" else ""))[:400].strip()
        if action == "say":
            action = R.WAIT
        if speech and self._post is not None:
            self._post({"type": "say", "text": speech})

        if action == "_error":
            self.failed_reason = f"backend error: {args.get('detail', '')}"
            # Not a robot failure: the harness never got a decision to execute.
            self.blocked_reason = f"harness: {self.failed_reason}"
            self._done = True
            result = self.failed_reason
        else:
            result = self._dispatch(mars, t, action or R.WAIT, args)

        # The brain looks again after its idle interval, or at once on an event.
        if self._skill is None:
            self._next_call_t = t + R.IDLE_TURN_S
        if action not in ("", R.WAIT):
            self._last_result = f"{action}({_args_text(args)}) -> {result}"
        self.log.append(Turn(round(t, 1), action, args, result, latency, speech))

    def _dispatch(self, mars, t: float, action: str, args: dict) -> str:
        """One tool call -> the outcome string the brain hands back (brain/agent._dispatch)."""
        if action == R.WAIT:
            return "ok"
        if action == R.STOP_SKILL:
            if self._skill is None:
                return "no skill is running"
            name, gen, _t0 = self._skill
            gen.close()
            mars.set_cmd_vel(0.0, 0.0)
            self._skill = None
            self._events.append(f"Skill {name} cancelled")
            return "stopping — you will get an event when it has stopped"
        if self._skill is not None or self._call_running is not None:
            return "rejected — another skill is already running; stop it first"
        if action == R.GO_TO_POINT_IN_VIEW and R.GO_TO_POINT_IN_VIEW in _BY_NAME:
            return self._go_to_point_in_view(mars, t, args)
        spec = _BY_NAME.get(action)
        if spec is None or action in (R.GO_TO_POINT_IN_VIEW,):
            return f"unknown skill '{action}'"
        try:
            inputs = R.coerce_args(spec, args)
        except (TypeError, ValueError) as exc:
            return f"failed — {describe(exc)}"
        self._start_skill(action, self._skill_gen(mars, action, inputs), t)
        return "started — you will get an event when it finishes"

    def _go_to_point_in_view(self, mars, t: float, args: dict) -> str:
        image = self._capture.get("image")
        if image is None:
            return "rejected — no camera frame to ground the point in"
        try:
            v_norm, u_norm = float(args["y"]), float(args["x"])
        except (KeyError, TypeError, ValueError):
            return "rejected — give integer y and x in 0-1000 image coordinates"
        if not (0.0 <= v_norm <= 1000.0 and 0.0 <= u_norm <= 1000.0):
            return "rejected — y and x must be within 0-1000 image coordinates"
        floor = _GROUNDING.pixel_to_floor(
            u_norm,
            v_norm,
            frame_jpeg=Path(image).read_bytes(),
            vertical_fov_deg=R.SIM_VERTICAL_FOV_DEG,
            pitch_deg=self._capture["pitch"],
            cam_height=R.SIM_CAM_HEIGHT_M,
            cam_forward=R.SIM_CAM_FORWARD_M,
        )
        if floor is None:
            return "rejected — that point is at or above the horizon; point at the floor"
        goal = _GROUNDING.approach_goal(*floor)
        inputs = {"x": goal["x"], "y": goal["y"], "theta_degrees": goal["theta_degrees"], "local_frame": True}
        self._start_skill(R.NAVIGATE, self._skill_gen(mars, R.NAVIGATE, inputs), t)
        return (
            f"driving to the floor point {math.hypot(*floor):.1f}m away (stopping ~{_GROUNDING.STANDOFF_M}m short) "
            "— you will get an event when it finishes"
        )

    # -- skills -------------------------------------------------------------

    def _start_skill(self, name: str, gen, t: float) -> None:
        next(gen)  # run to its first yield, so a skill that fails at once still reports through _step_skill
        self._skill = (name, gen, t)

    def _step_skill(self, mars, t: float) -> None:
        name, gen, t0 = self._skill
        try:
            if t - t0 > SKILL_TIMEOUT_S:
                gen.close()
                raise StopIteration((False, f"timed out after {SKILL_TIMEOUT_S:.0f}s"))
            gen.send(t)
            return
        except StopIteration as stop:
            ok, detail = stop.value if stop.value else (True, "")
        mars.set_cmd_vel(0.0, 0.0)
        self._skill = None
        line = f"Skill {name} {'completed' if ok else 'failed'}"
        self._events.append(line + (f": {detail}" if detail else ""))
        if ok and self._post is not None:
            self._post({"status": "completed", "skill_id": name})

    def _skill_gen(self, mars, name: str, inputs: dict):
        """A generator for one skill: yields once per control tick (receiving
        the sim time), returns (ok, detail) -- the detail the brain's event
        line carries."""
        yield  # primed by _start_skill
        if name == R.NAVIGATE:
            return (yield from self._navigate_skill(mars, **inputs))
        if name == "pick_any_object":
            return (yield from self._pick(mars, inputs["prompt"]))
        if name == "open_gripper":
            dropped = self._release(mars)
            yield from _hold(mars, R.DROP_SETTLE_S if dropped else R.CLOSE_GRIPPER_S)
            return True, "Gripper opened"
        if name == "close_gripper":
            yield from _hold(mars, R.CLOSE_GRIPPER_S)
            return True, "Gripper closed"
        if name == "head_emotion":
            emotion = inputs["emotion"].strip().lower()
            table = R.emotions()
            if emotion not in table:
                return False, f"Unknown emotion '{emotion}'. Available: {', '.join(sorted(table))}"
            repeat = max(1, min(int(inputs.get("repeat", 1)), 5))
            seconds = sum(d for _a, d in table[emotion]["sequence"]) * repeat + 0.2 * (repeat - 1)
            yield from _hold(mars, seconds)
            return True, f"Expressed '{emotion}' ({table[emotion]['description']})"
        if name == "wave":
            yield from _hold(mars, R.WAVE_S)
            return True, ""
        return False, f"{name} has no in-process implementation"

    def _navigate_skill(self, mars, x: float, y: float, theta_degrees: float = 0.0, local_frame: bool = False):
        goal_desc = f"({x}, {y}, {theta_degrees} deg, {'local' if local_frame else 'map'} frame)"
        if local_frame:
            bx, by, byaw = mars.pose()
            gx = bx + x * math.cos(byaw) - y * math.sin(byaw)
            gy = by + x * math.sin(byaw) + y * math.cos(byaw)
            gyaw = byaw + math.radians(theta_degrees)
        else:
            gx, gy, gyaw = x, y, math.radians(theta_degrees)
        ok, detail = yield from _drive_to(mars, self.nav, gx, gy, gyaw, R.NAV_V_MAX)
        if not ok:
            return False, f"Navigation to {goal_desc} failed: {detail}"
        return True, f"Reached position ({x}, {y}, {theta_degrees} deg)"

    def _pick(self, mars, prompt: str):
        """pick_any_object, step for step (see innate_roster's docstring for
        what stands in for Gemini)."""
        mars.set_joint_target("joint_head", math.radians(R.PICK_TILT_DEG))
        # Fold clear of the head camera before searching, as the skill does.
        R.move_arm(mars, _ARM["nav"])
        try:
            result = yield from self._pick_steps(mars, prompt)
        finally:
            mars.set_joint_target("joint_head", 0.0)
            # The skill's teardown, which a cancel runs too: carry if holding,
            # else fold to rest -- not left in the search pose.
            R.move_arm(mars, _ARM["carry"] if self._carrying else _ARM["rest"])
        # The skill ends with the head level again: without this the next
        # frame caught it mid-swing, tilted down with the arm in view.
        yield from _hold(mars, R.HEAD_LEVEL_S)
        return result

    def _pick_steps(self, mars, prompt: str):
        yield from _hold(mars, R.PICK_SETTLE_S)
        cam_z = float(R.camera_pose(mars)[0][2])
        found = None
        for turn in R.PICK_SCAN_RAD:
            if turn:
                yield from _rotate_by(mars, turn)
                yield from _hold(mars, R.PICK_SETTLE_S)
            cand = R.detect(mars, prompt, _kind, held=self._carrying)
            # A point above the camera is above the horizon: the floor
            # projection the skill localises with has no answer for it.
            if cand is not None and float(cand["pose"][2]) < cam_z:
                found = cand
                break
        if found is None:
            return False, f"Could not find '{prompt}' on the floor, even after scanning"
        name, pose = found["name"], found["pose"]
        floor_item = R.on_floor(mars, name, pose)
        if floor_item:
            tx, ty = float(pose[0]), float(pose[1])
        else:
            # Raised, but below the lens: the ray through it meets the
            # floor beyond, and the approach drives for that point.
            cam = R.camera_pose(mars)[0]
            s = cam[2] / (cam[2] - float(pose[2]))
            tx, ty = cam[0] + s * (pose[0] - cam[0]), cam[1] + s * (pose[1] - cam[1])
        x, y, _yaw = mars.pose()
        dist = math.hypot(tx - x, ty - y)
        heading = math.atan2(ty - y, tx - x)
        if dist > R.PICK_SWEET_X_M + 0.02:
            px = tx - R.PICK_SWEET_X_M * math.cos(heading)
            py = ty - R.PICK_SWEET_X_M * math.sin(heading)
            ok, _detail = yield from _drive_to(mars, self.nav, px, py, heading, R.PICK_DRIVE_V, R.PICK_PARK_TOL_M)
            x, y, yaw = mars.pose()
            # The skill servos on the image until the object sits in its grasp
            # box; it does not need the exact park. Short of it but within the
            # arm's reach and nearly centred is a grasp, as it is on the robot.
            near = math.hypot(tx - x, ty - y) <= R.GRASP_REACH_M
            centred = abs(R.wrap(math.atan2(ty - y, tx - x) - yaw)) <= R.PICK_CENTRED_RAD
            if not ok and not (near and centred):
                return False, "Base positioning failed (odometry lost or motion timed out)"
        if not floor_item:
            return False, f"Could not centre '{prompt}' in the approach box"
        # The claw opens to grasp: anything already in it drops here.
        self._release(mars)
        yield from _hold(mars, R.PICK_GRASP_S)
        now = mars.object_poses().get(name)
        x, y, _yaw = mars.pose()
        within = now is not None and math.hypot(now[0] - x, now[1] - y) <= R.GRASP_REACH_M
        caught = within and R.graspable(mars, name) and R.on_floor(mars, name, now)
        if caught:
            # INTO THE GRIPPER: a held object that stayed physically on the
            # floor was a ghost the robot could still see, and one taken out of
            # the world left the wrist camera showing an empty claw.
            R.set_held(mars, name, True)
            mars.props.out.discard(name)
            self._carrying = name
        # Held: fold to the carry pose, where the head camera and the wrist
        # camera both show it in the claw. Missed: back to rest.
        R.move_arm(mars, _ARM["carry"] if caught else _ARM["rest"])
        yield from _drive_for(mars, -R.PICK_DRIVE_V, R.PICK_VERIFY_BACKUP_M / R.PICK_DRIVE_V)
        yield from _hold(mars, max(0.0, _ARM["fold_s"] - R.PICK_VERIFY_BACKUP_M / R.PICK_DRIVE_V))
        if not caught:
            return False, f"Grasp missed — '{prompt}' is still on the floor (verified after backing up)"
        return True, f"Picked up '{prompt}' (verified: floor clear after backing up)"

    def _release(self, mars) -> bool:
        if self._carrying is None:
            return False
        x, y, yaw = mars.pose()
        name, self._carrying = self._carrying, None
        R.set_held(mars, name, False)
        if hasattr(mars, "model"):
            # "The object simply drops from the claw" (open_gripper): under
            # wherever the arm holds it, which the wrist camera shows -- set
            # down just past the fingertips. The simulated claw never opens,
            # and an object released inside it wedged there: blaze_l2's phone
            # rode the closed fingers through four open_gripper calls.
            gx, gy, _gz = R.grip_point(mars)
            prop = mars.props.props[name]
            clear = R.CLAW_CLEAR_M + max(prop.size[0], prop.size[1] if len(prop.size) > 2 else prop.size[0])
            # From the claw's height, a couple of centimetres -- not from the
            # prop's own drop_z, which is a shelf-drop height: the pantry jar's
            # is 0.19 m, and released from there it landed on its side and
            # rolled under the shelf, out of reach (pantry_stocktake, twice).
            mars.drop_prop_at(
                name,
                float(gx) + clear * math.cos(yaw),
                float(gy) + clear * math.sin(yaw),
                yaw,
                z=prop.rest_z + R.RELEASE_DROP_M,
            )
        else:
            mars.drop_prop_at(name, x + R.DROP_AHEAD_M * math.cos(yaw), y + R.DROP_AHEAD_M * math.sin(yaw))
        return True

    # -- reporting ----------------------------------------------------------

    def transcript(self) -> list[dict]:
        return [
            {"t": e.t, "action": e.action, "args": e.args, "say": e.say, "result": e.result, "latency_s": e.latency_s}
            for e in self.log
        ]


_GROUNDING = R.load_grounding()
_ARM = R.arm_poses()


def _head_pitch(mars) -> float:
    reader = getattr(mars, "head_pitch_deg", None)
    return float(reader()) if reader is not None else 0.0


def _args_text(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _hold(mars, seconds: float):
    t0 = None
    while True:
        t = yield
        t0 = t if t0 is None else t0
        mars.set_cmd_vel(0.0, 0.0)
        if t - t0 >= seconds:
            return


def _drive_for(mars, v: float, seconds: float):
    t0 = None
    while True:
        t = yield
        t0 = t if t0 is None else t0
        if t - t0 >= seconds:
            mars.set_cmd_vel(0.0, 0.0)
            return
        mars.set_cmd_vel(v, 0.0)


def _rotate_by(mars, angle: float, w_max: float = R.PICK_ROT_W, timeout_s: float = 15.0):
    yaw0 = mars.pose()[2]
    t0 = None
    while True:
        t = yield
        t0 = t if t0 is None else t0
        rem = R.wrap(angle - R.wrap(mars.pose()[2] - yaw0))
        if abs(rem) < math.radians(2.5) or t - t0 > timeout_s:
            mars.set_cmd_vel(0.0, 0.0)
            return
        mars.set_cmd_vel(0.0, math.copysign(max(0.15, min(w_max, 1.2 * abs(rem))), rem))


def _plan(nav, x: float, y: float, gx: float, gy: float):
    """A route on the static map from (x, y) to (gx, gy), preferring Nav2-like
    clearance: (waypoints, None), or (None, why not)."""
    if nav is None:
        return [(gx, gy)], None
    path = R.widen(nav, R.NAV_CLEARANCE_M).plan((x, y), (gx, gy))
    # The wide map snaps a goal by a wall to its nearest clear cell, which
    # can be far off; then only the gate's map routes to the goal itself.
    if path is None or math.hypot(path[-1][0] - gx, path[-1][1] - gy) > 0.25:
        path = nav.plan((x, y), (gx, gy))
    if path is None:
        return None, "the planner found no path because the route may be blocked or the target may be unreachable"
    return [p for p in path[1:-1]] + [(gx, gy)], None


def _drive_to(mars, nav, gx: float, gy: float, gyaw: float, v_max: float, xy_tol: float = R.NAV_XY_TOL_M):
    """Nav2 as the probe can have it: plan on the static map, follow, settle
    to the goal heading, and on a stall run Nav2's recovery (back up 0.15 m,
    replan) as nav_to_pose.xml does. Returns (ok, detail) with Nav2's own
    wording."""
    x, y, _yaw = mars.pose()
    if nav is not None:
        r, c = nav.to_cell(gx, gy)
        if not nav.free(r, c):
            snapped = nav.nearest_free(gx, gy, R.NAV_PLANNER_TOL_M)
            if snapped is None:
                return False, (
                    "the planner found no path because the target is an occupied map cell. "
                    f"The robot did not move and remains at ({x:.2f}, {y:.2f}), "
                    f"{math.hypot(gx - x, gy - y):.2f}m from the target"
                )
            gx, gy = nav.to_world(*snapped)
    path, why = _plan(nav, x, y, gx, gy)
    if path is None:
        return False, (
            f"{why}. The robot did not move and remains at ({x:.2f}, {y:.2f}), "
            f"{math.hypot(gx - x, gy - y):.2f}m from the target"
        )
    recoveries = 0

    i = 0
    mark = None  # (x, y, t) of the progress checker
    settle = None  # (best heading error, when) while turning to the goal heading
    while True:
        t = yield
        x, y, yaw = mars.pose()
        if mark is None or math.hypot(x - mark[0], y - mark[1]) >= R.NAV_PROGRESS_M:
            mark = (x, y, t)
        wx, wy = path[i]
        d = math.hypot(wx - x, wy - y)
        last = i == len(path) - 1
        if not last and d < 0.15:
            i += 1
            continue
        if last and d < xy_tol:
            err = R.wrap(gyaw - yaw)
            if abs(err) < R.NAV_YAW_TOL_RAD:
                mars.set_cmd_vel(0.0, 0.0)
                return True, ""
            # Settling to the goal heading is progress while the heading
            # closes. It used to count as progress unconditionally, and a robot
            # whose turn was blocked by a door jamb "settled" for the full four
            # minutes with no event (blaze_l4, kitchen doorway).
            if settle is None or abs(err) < settle[0] - 0.05:
                settle = (abs(err), t)
            elif t - settle[1] > R.NAV_SETTLE_STALL_S:
                mars.set_cmd_vel(0.0, 0.0)
                return False, (
                    f"Nav2 reported FAILED at the goal, {math.degrees(abs(err)):.0f} deg short of its heading; "
                    f"the robot stopped at ({x:.2f}, {y:.2f}) in the map frame"
                )
            mark = (x, y, t)
            mars.set_cmd_vel(0.0, math.copysign(min(R.NAV_W_MAX, max(0.15, 1.5 * abs(err))), err))
            continue
        if t - mark[2] > R.NAV_PROGRESS_S and recoveries < R.NAV_RECOVERIES:
            # Pressed against a door jamb the follower can neither turn nor
            # drive; three players in a row sat frozen in blaze's kitchen
            # doorway. Nav2 backs off and replans before it gives up.
            recoveries += 1
            yield from _drive_for(mars, -R.NAV_BACKUP_V, R.NAV_BACKUP_M / R.NAV_BACKUP_V)
            x, y, _yaw = mars.pose()
            path = _plan(nav, x, y, gx, gy)[0] or path[i:]
            i, mark, settle = 0, None, None
            continue
        if t - mark[2] > R.NAV_PROGRESS_S:
            mars.set_cmd_vel(0.0, 0.0)
            remaining = d + sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path[i:-1], path[i + 1 :], strict=True)
            )
            return False, (
                f"Nav2 reported FAILED with {remaining:.2f}m still to go after {recoveries} recoveries; "
                f"the robot stopped at ({x:.2f}, {y:.2f}) in the map frame"
            )
        err = R.wrap(math.atan2(wy - y, wx - x) - yaw)
        w = max(-R.NAV_W_MAX, min(R.NAV_W_MAX, 1.5 * err))
        v = 0.0 if abs(err) > 0.6 else min(v_max, 0.8 * d + 0.05) * math.cos(err)
        mars.set_cmd_vel(v, w)


# What a gripper or a camera can tell about a prop: the kind of thing, from the
# prop id's own words, never the id. First match wins, so "gauge" (an oil can)
# is read before the bare "can".
_KINDS = (
    ("medicine", "a medicine bottle"),
    ("documents", "a document box"),
    ("phone", "a phone"),
    ("photo", "a photo frame"),
    ("towels", "a stack of towels"),
    ("teapot", "a teapot"),
    ("cup", "a cup"),
    ("mug", "a mug"),
    ("jar", "a jar"),
    ("carton", "a carton"),
    ("tin", "a tin"),
    ("gauge", "an oil can"),
    ("target", "a paint can"),
    ("can", "a can"),
    ("book", "a book"),
    ("ball", "a ball"),
    ("cube", "a cube"),
    ("bar", "a bar"),
)


def _kind(name: str) -> str:
    words = name.split("_")
    return next((kind for key, kind in _KINDS if key in words), "an object")
