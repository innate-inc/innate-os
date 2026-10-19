"""MARS's own tools, as the sim on innate-os main offers them, run in-process.

WHY THIS EXISTS. The probe used to hand its model eight harness actions --
turn N degrees, drive N metres, grab whatever is in front, put it down 30 cm
ahead. The robot in innate's sim has none of those. Its brain sees one camera
frame, a status line and the events since its last turn, and acts by calling
the skills of the running agent (intro_agent is the default the brain
initialiser picks) plus two built-in tools: go_to_point_in_view and wait.
Whatever it writes as plain text is spoken. A probe that certifies "solvable"
with a different action set certifies nothing about this robot, so the menu
and the semantics come from main.

WHAT IS READ FROM MAIN, NOT COPIED. The roster (intro_agent.get_skills), each
skill's description (its guidelines(), defaulting to the class docstring, the
same rule the brain's catalog applies), each skill's parameters (execute()'s
signature), the brain's system prompt and its built-in tool descriptions are
all parsed out of the workspace and brain sources at import. Change a skill on
main and the probe's menu changes with it; test_innate_roster.py fails if a
roster skill has no in-process implementation here.

WHAT RUNS IN PLACE OF THE REAL STACK, and the cost of each stand-in:

  navigate_to_position  A* on the world's static occupancy grid and a path
                        follower with Nav2's limits from mars_nav's config:
                        0.45 m/s, 0.6 rad/s, 0.10 m / 0.10 rad goal tolerance,
                        0.1 m planner tolerance, and its progress checker (0.5 m
                        in 10 s, or the goal is abandoned). Nav2's rolling
                        costmap would also see props; this grid does not, so a
                        prop in the way is found by driving into it.
  go_to_point_in_view   The brain's own grounding module, imported from the
                        brain package, with the brain's own sim camera config
                        (80 deg vertical FOV, camera 0.2 m up, 0.0 m forward).
                        The sim camera is 84 deg; the brain's projection is
                        used anyway, because that error is the real one.
  pick_any_object       Gemini finding the object from the prompt is replaced
                        by a matcher over the props the head camera can see
                        (projected into the frame, line of sight by ray cast),
                        scored on kind, colour, contents and relative size --
                        words a vision model can read off the object, never
                        the prop id. Everything else follows the skill: head
                        to -20 deg, scan straight / right 30 / left 60, floor
                        objects only, approach to its 0.285 m park at its
                        0.10 m/s, grasp, back up 0.15 m to verify.
  open_gripper          What is held drops 0.30 m ahead of the base.
  close_gripper,        Timing only: nothing in these worlds hands the robot
  head_emotion, wave    anything, and the judge cannot see a nod or a wave.

  search_memory is in intro_agent's roster and is WITHHELD here, as the live
  benchmark's roster gate withholds it (prime_brain.sh): this process has no
  memory tier, and a stand-in that answered "nothing found" for a place the
  robot did see would be a false fact, not a weaker skill.

ONE TIMING SIMPLIFICATION. While a skill runs the real brain is called every
5 s with only stop_current_skill and wait. Here it is called while a skill
runs only when someone speaks -- which is when the real brain is woken at once
-- so a probe cannot abort a drive on a mid-route look. Every skill here ends
on its own within a bounded time.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO / "workspace"
BRAIN = REPO / "ros2_ws" / "src" / "brain" / "brain_client" / "brain_client" / "brain"
DEFAULT_AGENT = WORKSPACE / "innate_agents" / "intro_agent.py"

# The brain's own constants (brain/agent.py, core/config.py, the sim launch file).
IDLE_TURN_S = 3.0  # idle_turn_interval: the next look when nothing is running
SIM_VERTICAL_FOV_DEG = 80.0  # config default; brain_client.sim.launch.py does not override it
SIM_CAM_HEIGHT_M = 0.2  # brain_client.sim.launch.py
SIM_CAM_FORWARD_M = 0.0  # brain_client.sim.launch.py
# The directive an agent with no prompt runs under (prompt.build_system_prompt),
# which is what the live benchmark's empty_directive runs.
DEFAULT_DIRECTIVE = "Be a helpful home robot."

WAIT = "wait"
STOP_SKILL = "stop_current_skill"
GO_TO_POINT_IN_VIEW = "go_to_point_in_view"
NAVIGATE = "navigate_to_position"

WITHHELD = {
    "search_memory": "no memory tier in-process; the live benchmark withholds it too (prime_brain.sh)",
}

# Nav2 as mars_nav configures it.
NAV_V_MAX = 0.45
NAV_W_MAX = 0.6
NAV_XY_TOL_M = 0.10
NAV_YAW_TOL_RAD = 0.10
NAV_PLANNER_TOL_M = 0.10
NAV_PROGRESS_M = 0.5
NAV_PROGRESS_S = 10.0
# Nav2 keeps off obstacles: its costmaps inflate 0.25-0.3 m around them, steeply
# penalised, past a 0.165 m half-width footprint. The probe's planner tries a
# route this far clear first and takes the gate's tighter one only if none exists.
NAV_CLEARANCE_M = 0.20
# A turn to the goal heading that stops closing for this long is blocked.
NAV_SETTLE_STALL_S = 3.0
# nav_to_pose.xml's recovery: BackUp backup_dist 0.15 at 0.3 m/s, then retry; two retries.
NAV_RECOVERIES = 2
NAV_BACKUP_M = 0.15
NAV_BACKUP_V = 0.3

# pick_any_object / approach.py.
PICK_TILT_DEG = -20.0
PICK_SETTLE_S = 1.2
# The head back to level after a pick (head.set_position(0) in its finally).
HEAD_LEVEL_S = 1.0
PICK_SCAN_RAD = (0.0, -math.radians(30), math.radians(60))
PICK_SWEET_X_M = 0.285
PICK_DRIVE_V = 0.10
# The approach servo parks to drive_tol_m 0.015; the follower here settles coarser.
PICK_PARK_TOL_M = 0.03
# How far off the object's bearing a short park may be and still grasp.
PICK_CENTRED_RAD = math.radians(20)
PICK_ROT_W = 0.5
PICK_GRASP_S = 6.0
# How far from the base an object may sit at the grasp and still be caught:
# the park plus what the arm reaches past it.
GRASP_REACH_M = PICK_SWEET_X_M + 0.12
# Nothing above the lens is ever picked: its pixel is above the horizon, and
# the floor projection the skill localises with has no answer for it.
PICK_Z_MAX_M = SIM_CAM_HEIGHT_M
PICK_VERIFY_BACKUP_M = 0.15
# A detection further than this is not a grasp target a floor approach starts
# from (grounding.MAX_RANGE_M, the same projection's useful range).
DETECT_RANGE_M = 3.5
# Widest thing the gripper closes on. The largest task item, the photo frame,
# is 0.23 m; people, the dog, crates and doors are all wider.
GRASP_SPAN_MAX_M = 0.30
# Where a released item lands, ahead of the base.
# Where a released item lands on a robot with no arm model (the unit tests'
# stand-ins); a simulated robot drops it from its claw.
DROP_AHEAD_M = 0.30
DROP_SETTLE_S = 1.5
CLOSE_GRIPPER_S = 1.0
WAVE_S = 6.0  # metadata start_pose_time 2 s, then the recorded replay


# -- reading main ------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    # (name, type, default) -- default is REQUIRED when the parameter has none.
    params: list[tuple[str, str, object]] = field(default_factory=list)


REQUIRED = object()


def _module_constants(tree: ast.Module) -> dict:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise LookupError(name)


def _code_skill(path: Path, class_name: str) -> ToolSpec:
    """name, guidelines and parameters of one code skill, the catalog's way:
    guidelines() if the class defines it, else its docstring; parameters from
    execute()'s signature, minus self and **kwargs."""
    tree = ast.parse(path.read_text())
    consts = _module_constants(tree)
    cls = _class(tree, class_name)
    description = inspect.cleandoc(ast.get_docstring(cls, clean=False) or "")
    params: list[tuple[str, str, object]] = []
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "guidelines":
            ret = node.body[-1]
            if isinstance(ret, ast.Return) and ret.value is not None:
                description = eval(compile(ast.Expression(ret.value), str(path), "eval"), {}, dict(consts))  # noqa: S307
        elif node.name == "execute":
            args = node.args.args[1:]
            defaults = [REQUIRED] * (len(args) - len(node.args.defaults)) + [
                ast.literal_eval(d) for d in node.args.defaults
            ]
            for arg, default in zip(args, defaults, strict=True):
                params.append((arg.arg, ast.unparse(arg.annotation) if arg.annotation else "str", default))
    return ToolSpec(_snake(class_name), description, params)


def _physical_skill(folder: Path) -> ToolSpec:
    meta = json.loads((folder / "metadata.json").read_text())
    params = [(k, str(v.get("type", "str")), REQUIRED) for k, v in (meta.get("inputs") or {}).items()]
    return ToolSpec(meta["name"], meta.get("guidelines") or meta["name"], params)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def roster(agent_file: Path = DEFAULT_AGENT) -> list[ToolSpec]:
    """The skills `agent_file`'s get_skills() returns, in its order."""
    tree = ast.parse(agent_file.read_text())
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("innate_skills"):
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_skills":
            ret = [n for n in ast.walk(node) if isinstance(n, ast.Return)][0]
            names = [elt.id for elt in ret.value.elts]
    specs = []
    for name in names:
        rel = Path(*imports[name].split("."))
        module = WORKSPACE / rel.with_suffix(".py")
        specs.append(_code_skill(module, name) if module.is_file() else _physical_skill(WORKSPACE / rel))
    return specs


def _brain_tool_description(constant: str) -> str:
    tree = ast.parse((BRAIN / "tools.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == constant:
            for kw in node.value.keywords:
                if kw.arg == "description":
                    return ast.literal_eval(kw.value)
    raise LookupError(constant)


def stop_description(running: str, user_spoke: bool) -> str:
    """tools.build_tools' stop_current_skill text (test_innate_roster checks it against the source)."""
    return f"Abort the currently running skill ({running}). " + (
        "Only when the user asks you to stop or switch task, or the skill is clearly "
        "failing. Questions and conversation are NOT reasons to stop — answer those "
        "in text and let the skill continue."
        if user_spoke
        else "Use when it is clearly failing, no longer makes sense, or the user asks for something else."
    )


def system_prompt(directive: str = DEFAULT_DIRECTIVE) -> str:
    consts = _module_constants(ast.parse((BRAIN / "prompt.py").read_text()))
    return consts["_SYSTEM_PROMPT"].format(directive=directive, identity="")


def load_grounding():
    """brain/grounding.py itself -- a pure module -- without importing the
    brain package (whose __init__ chain needs ROS)."""
    spec = importlib.util.spec_from_file_location("innate_brain_grounding", BRAIN / "grounding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arm_poses() -> dict[str, list[float]]:
    """pick_any_object's own arm poses (j1-5, radians) and its fold time:
    NAV_ARM to search and approach with the arm clear of the head camera,
    CARRY_ARM to hold what it picked, and Manipulation.REST after a miss."""
    pick = _module_constants(ast.parse((WORKSPACE / "innate_skills" / "pick_any_object.py").read_text()))
    manip = ast.parse((REPO / "ros2_ws/src/brain/brain_client/brain_client/robot/manipulation.py").read_text())
    rest = None
    for node in ast.walk(manip):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "REST":
            rest = ast.literal_eval(node.value)
    return {"nav": pick["NAV_ARM"], "carry": pick["CARRY_ARM"], "rest": rest[:5], "fold_s": pick["FOLD_S"]}


def move_arm(mars, joints: list[float]) -> None:
    if hasattr(mars, "set_joint_target"):
        for i, value in enumerate(joints[:5], 1):
            mars.set_joint_target(f"joint{i}", float(value))


def emotions() -> dict:
    tree = ast.parse((WORKSPACE / "innate_skills" / "head_emotion.py").read_text())
    return _module_constants(tree)["EMOTIONS"]


# -- the menu ------------------------------------------------------------------


def available(specs: list[ToolSpec]) -> list[ToolSpec]:
    """The roster this process can run, plus the brain's built-ins, in the
    order build_tools declares them."""
    tools = [s for s in specs if s.name not in WITHHELD]
    if any(s.name == NAVIGATE for s in tools):
        tools.append(
            ToolSpec(
                GO_TO_POINT_IN_VIEW,
                _brain_tool_description("_GO_TO_POINT_IN_VIEW_DECLARATION"),
                [("y", "int", REQUIRED), ("x", "int", REQUIRED)],
            )
        )
    tools.append(ToolSpec(WAIT, _brain_tool_description("_WAIT_DECLARATION")))
    return tools


def _signature(spec: ToolSpec) -> str:
    parts = []
    for name, typ, default in spec.params:
        parts.append(f"{name}: {typ}" + ("" if default is REQUIRED else f" = {default!r}"))
    return f"{spec.name}({', '.join(parts)})"


REPLY_FORMAT = """Reply with ONE JSON object:
  {"say": "words to speak, or leave this out", "action": "<tool name>", "args": {...}}
"say" is your plain text: it is spoken aloud. "action" is the one tool you call this turn."""


def menu(
    tools: list[ToolSpec],
    *,
    running: str | None = None,
    user_spoke: bool = False,
    blind: bool = False,
    directive: str = DEFAULT_DIRECTIVE,
) -> str:
    """What the model is told: main's system prompt, the tools build_tools
    would declare this turn, and the reply envelope."""
    if running is not None:
        stop = ToolSpec(STOP_SKILL, stop_description(running, user_spoke))
        shown = [stop] if user_spoke else [stop, next(t for t in tools if t.name == WAIT)]
    else:
        shown = [t for t in tools if not (blind and t.name == GO_TO_POINT_IN_VIEW)]
    lines = [system_prompt(directive).rstrip(), "", "Your tools this turn:"]
    for t in shown:
        lines.append(f"- {_signature(t)}")
        lines += [f"    {ln}" for ln in t.description.splitlines()]
    lines += ["", REPLY_FORMAT]
    if blind:
        lines.insert(
            0,
            "YOU HAVE NO CAMERA in this configuration. You cannot see anything at all, so you "
            "cannot identify objects, count them, or read their colours. If you are asked to do "
            "something that needs sight, say that you cannot see.\n",
        )
    return "\n".join(lines)


def coerce_args(spec: ToolSpec, args: dict) -> dict:
    """Arguments as the skill would receive them, or ValueError naming the
    parameter -- the brain's runner rejects a malformed call the same way."""
    out = {}
    for name, typ, default in spec.params:
        if name not in args or args[name] is None:
            if default is REQUIRED:
                raise ValueError(f"missing required argument '{name}'")
            out[name] = default
            continue
        value = args[name]
        if typ in ("float", "int"):
            value = float(value)
            out[name] = int(round(value)) if typ == "int" else value
        elif typ == "bool":
            out[name] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes")
        else:
            out[name] = str(value)
    return out


# -- what the head camera can see ------------------------------------------------

_COLOURS = {
    "red",
    "green",
    "blue",
    "yellow",
    "orange",
    "purple",
    "pink",
    "white",
    "black",
    "grey",
    "gray",
    "brown",
    "teal",
}
# A vision model reads these off the object; groups are words it would treat as one thing.
_KIND_GROUPS = (
    ("cup", "mug"),
    ("jar",),
    ("carton", "box"),
    ("tin", "can"),
    ("book", "hardback"),
    ("ball",),
    ("cube", "block"),
    ("bar",),
    ("bottle", "medicine", "pills"),
    ("documents", "document", "papers", "box"),
    ("phone", "mobile", "cellphone", "smartphone"),
    ("photo", "frame", "picture"),
    ("towels", "towel"),
    ("teapot", "kettle", "pot"),
)
_CONTENTS = {"jam", "honey", "curd", "oats", "rice", "tea", "oil", "paint", "medicine"}
_SMALL = {"small", "smallest", "little", "tiny", "short", "shortest"}
_LARGE = {"large", "largest", "big", "biggest", "tall", "tallest"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _kinds(words) -> set[int]:
    return {i for i, group in enumerate(_KIND_GROUPS) for w in words if w in group}


def descriptor(name: str, title: str, kind_phrase: str) -> dict:
    """What a camera can tell about a prop: its kind, colour and contents --
    from the prop's own words, filtered to that vocabulary, so ids like
    "stray", "pick", "040" and "brew" never reach the matcher."""
    words = set(_words(name.replace("_", " "))) | set(_words(title)) | set(_words(kind_phrase))
    return {
        "kinds": _kinds(words),
        "colours": {("grey" if w == "gray" else w) for w in words if w in _COLOURS},
        "contents": {w for w in words if w in _CONTENTS},
    }


def match_score(prompt: str, desc: dict) -> float | None:
    """None when the description rules the prop out, else how well it fits."""
    words = _words(prompt)
    score = 0.0
    want_kinds = _kinds(words)
    if want_kinds:
        if not want_kinds & desc["kinds"]:
            return None
        score += 1.0
    want_colours = {("grey" if w == "gray" else w) for w in words if w in _COLOURS}
    if want_colours and desc["colours"]:
        if not want_colours & desc["colours"]:
            return None
        score += 2.0
    want_contents = {w for w in words if w in _CONTENTS}
    if want_contents and desc["contents"]:
        if not want_contents & desc["contents"]:
            return None
        score += 2.0
    return score


def size_preference(prompt: str) -> int:
    words = set(_words(prompt))
    return -1 if words & _SMALL else (1 if words & _LARGE else 0)


def camera_pose(mars) -> tuple[np.ndarray, np.ndarray]:
    body = mars.model.body("robot_camera_optical_frame").id
    return mars.data.xpos[body].copy(), mars.data.xmat[body].reshape(3, 3).copy()


def project(mars, point) -> tuple[float, float, float] | None:
    """(u, v, depth) of a world point in the 640x480 head frame, or None
    behind the lens. The optical frame looks down +z with +y down."""
    from mars_sim_driver.constants import CAMERA_CX, CAMERA_CY, CAMERA_FX, CAMERA_FY

    pos, rot = camera_pose(mars)
    pc = rot.T @ (np.asarray(point, dtype=float) - pos)
    if pc[2] < 0.05:
        return None
    return CAMERA_CX + CAMERA_FX * pc[0] / pc[2], CAMERA_CY + CAMERA_FY * pc[1] / pc[2], float(pc[2])


def line_of_sight(mars, name: str, point) -> bool:
    """The first thing a ray from the lens hits on its way to the prop is the prop."""
    import mujoco
    from mars_sim_driver import world

    pos, _rot = camera_pose(mars)
    vec = np.asarray(point, dtype=float) - pos
    dist = float(np.linalg.norm(vec))
    unit = vec / dist
    groups = np.zeros(6, dtype=np.uint8)
    groups[world.VISUAL_GROUP] = 1
    geom = np.array([-1], dtype=np.int32)
    body = mars.props._addr[name][0]
    travelled = 0.0
    # The lens sits inside the head's own visual shell, and the arm can be in
    # front of it: the robot's own geoms are stepped through, not counted.
    for _ in range(8):
        hit = mujoco.mj_ray(mars.model, mars.data, pos + unit * travelled, unit, groups, 1, -1, geom)
        if hit < 0:
            return True
        hit_body = int(mars.model.geom_bodyid[geom[0]])
        if hit_body == body or travelled + hit >= dist - 0.03:
            return True
        if not mars.model.body(hit_body).name.startswith("robot"):
            return False
        travelled += hit + 0.005
    return False


def visible_props(mars, exclude: str | None = None) -> list[dict]:
    """Props the head camera sees right now, with their image position."""
    from mars_sim_driver.constants import CAMERA_HEIGHT, CAMERA_WIDTH

    out = []
    x, y, _yaw = mars.pose()
    for name, pose in mars.object_poses().items():
        p = mars.props.props.get(name)
        if p is None or name == exclude:
            continue
        uvd = project(mars, pose[:3])
        if uvd is None:
            continue
        u, v, depth = uvd
        if not (0 <= u < CAMERA_WIDTH and 0 <= v < CAMERA_HEIGHT):
            continue
        if math.hypot(pose[0] - x, pose[1] - y) > DETECT_RANGE_M or not line_of_sight(mars, name, pose[:3]):
            continue
        out.append({"name": name, "u": u, "v": v, "depth": depth, "pose": pose, "prop": p})
    return out


def detect(mars, prompt: str, kind_phrase, held: str | None = None) -> dict | None:
    """The prop a vision model asked for `prompt` would point at, among those
    in view: the best description match, then the most central."""
    best, best_key = [], None
    for cand in visible_props(mars, exclude=held):
        p = cand["prop"]
        score = match_score(prompt, descriptor(cand["name"], p.title, kind_phrase(cand["name"])))
        if score is None:
            continue
        key = score
        if best_key is None or key > best_key:
            best, best_key = [cand], key
        elif key == best_key:
            best.append(cand)
    if not best:
        return None
    pref = size_preference(prompt)
    if pref and len(best) > 1:
        size = [max(c["prop"].size) for c in best]
        pick = size.index(max(size) if pref > 0 else min(size))
        return best[pick]
    from mars_sim_driver.constants import CAMERA_WIDTH

    return min(best, key=lambda c: abs(c["u"] - CAMERA_WIDTH / 2))


# How far past the finger joints, along the gripper, a held object's centre
# sits. At the joints themselves a mug enveloped the wrist camera's lens and
# hung below the claw in the head camera; here the jaws close on its top.
GRIP_REACH_M = 0.05
# Half the span an object can have and still sit between the jaws at GRIP_REACH_M.
GRIP_JAW_HALF_M = 0.04
# The claw's own reach past that point, horizontally: a released item is set
# down beyond it.
CLAW_CLEAR_M = 0.06
# How far a released item falls: the claw holds it just off the floor.
RELEASE_DROP_M = 0.01


def grip_point(mars, extra: float = 0.0) -> np.ndarray:
    """Between the gripper's fingertips, wherever the arm is; `extra` further
    out for an object bigger than the jaws, whose centre sits past them."""
    m, d = mars.model, mars.data
    knuckles = 0.5 * (d.xpos[m.body("robot_link61").id] + d.xpos[m.body("robot_link62").id])
    axis = knuckles - d.xpos[m.body("robot_link5").id]
    return knuckles + (GRIP_REACH_M + extra) * axis / np.linalg.norm(axis)


def set_held(mars, name: str, held: bool) -> None:
    """A held object rides in the claw, where the wrist camera shows it; its
    collisions are off while it does, so it cannot shove the arm or the robot
    it is carried by. Restored exactly on release."""
    model = getattr(mars, "model", None)
    if model is None:
        return
    saved = mars.__dict__.setdefault("_held_geoms", {})
    body = mars.props._addr[name][0]
    if held:
        geoms = [g for g in range(model.ngeom) if model.geom_bodyid[g] == body]
        saved[name] = [(g, int(model.geom_contype[g]), int(model.geom_conaffinity[g])) for g in geoms]
        for g in geoms:
            model.geom_contype[g] = 0
            model.geom_conaffinity[g] = 0
    else:
        for g, contype, conaffinity in saved.pop(name, []):
            model.geom_contype[g] = contype
            model.geom_conaffinity[g] = conaffinity


def graspable(mars, name: str) -> bool:
    """Not a person, an animal or furniture: a dynamic prop the hand can span."""
    prop = getattr(getattr(mars, "props", None), "props", {}).get(name)
    if prop is None:
        return True
    return not prop.kinematic and 2 * max(prop.size) <= GRASP_SPAN_MAX_M


def on_floor(mars, name: str, pose) -> bool:
    prop = mars.props.props[name]
    rest = prop.rest_z if prop.rest_z is not None else prop.size[2]
    return float(pose[2]) - rest < 0.03


def widen(nav, clearance_m: float):
    """A copy of a NavMap with obstacles grown to `clearance_m` from its own radius."""
    from navplan import ROBOT_RADIUS_M, _inflate

    extra = max(0, int(round((clearance_m - ROBOT_RADIUS_M) / nav.res)))
    wide = object.__new__(type(nav))
    wide.__dict__.update(nav.__dict__)
    wide.blocked = _inflate(nav.blocked, extra)
    return wide


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi
