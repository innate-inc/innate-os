"""Transport-agnostic core of the virtual MARS: headless MuJoCo
physics of the apartment + real mars.urdf, with the two robot cameras
rendered offscreen. node.py wraps this in a ROS 2 node that
impersonates the mars_cam/base drivers; this module has no ROS dependency so
it can be developed and tested anywhere (see sim/sandbox/test_driver_core.py).

Model building (URDF attach with visual meshes, planar base, drive/joint
servos) lives in world.py, shared with the sim/sandbox dev tools.
"""

import contextlib
import hashlib
import io
import math
import os
import sys
from pathlib import Path

import mujoco
import numpy as np
from PIL import Image

from . import world
from .constants import (
    CAMERA_FOVY,
    CAMERA_FX,
    CAMERA_FY,
    CAMERA_HEIGHT,
    CAMERA_PRINCIPAL_PIXEL,
    CAMERA_WIDTH,
    WIDE_CAMERA_FOVY,
    WRIST_CAMERA_FOVY,
)
from .drive_limits import clamp_cmd_vel
from .props import PropRegistry
from .world import ARM_HOME, SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG

# Stop the base if the last Twist is stale, like a real base watchdog.
CMD_VEL_TIMEOUT_S = 0.5

# name -> (body carrying the camera, URDF-frame forward/up of the lens, vertical fov).
# MuJoCo cameras look down their local -Z with +Y up; _camera_quat converts.
CAMERAS = {
    "main": ("robot_camera_optical_frame", (0, 0, 1), (0, -1, 0), CAMERA_FOVY),
    "wrist": ("robot_arm_camera_link", (1, 0, 0), (0, 0, 1), WRIST_CAMERA_FOVY),
    # Identical mount and orientation to main -- fov is the only difference.
    "wide": ("robot_camera_optical_frame", (0, 0, 1), (0, -1, 0), WIDE_CAMERA_FOVY),
}
JPEG_QUALITY = 80  # matches main_camera_driver.cpp
# Post-render ACES tone map approximating sim/viewer's Three.js output
# (ACESFilmicToneMapping); exposure calibrated visually against it.
TONEMAP_EXPOSURE = 2.5

# Arm/head PD servo -- apartmentWorker.ts's tuned defaults (reactive feel),
# torque-clamped; the URDF's per-joint damping=5 caps speed at LIMIT/5 rad/s.
KP_JOINT = 50.0
KD_JOINT = 1.0
EFFORT_LIMIT = 50.0  # N*m
# The gripper runs on mars.urdf's real joint6 rating instead. 50 N*m on a
# 45mm finger is 1.1kN of pinch: it ejects whatever it grabs and shakes the
# contact solver, where 2 N*m is the ~44N the actual servo delivers. Its
# velocity term is zero because the finger's 2e-5 inertia makes an explicit
# -kd*qvel unstable at this timestep; world.FINGER_DAMPING damps it instead.
GRIPPER_EFFORT_LIMIT = 2.0  # N*m
KD_GRIPPER = 0.0

# arm_control.cpp's "intelligent joint limits": when joint1 swings the arm
# across the robot's front arc, joint2 may not stay folded up (negative =
# arm up) -- the arm must duck under the head instead of sweeping through
# it. Ramps back to the full range at the arc's edges. The real floor is
# -0.5, but the simplified collision boxes (chassis + shoulder shaft)
# still overlap ~9mm there where the real parts clear, so the sim ducks
# to -0.25, the shallowest pose that clears.
JOINT2_GUARD_MIN = -0.25


def joint2_min_target(joint1_target: float, full_min: float) -> float:
    """joint2 target floor for a given joint1 target -- the same piecewise
    ramp as arm_control.cpp applyLimitsAndConvertToEncoder, with the sim's
    joint range lower bound as the full limit."""
    if joint1_target < -1.35 or joint1_target >= 1.25:
        return full_min
    if joint1_target < -1.0:
        t = -(joint1_target + 1.0) / 0.35
    elif joint1_target < 1.0:
        t = 0.0
    else:
        t = (joint1_target - 1.0) / 0.25
    return JOINT2_GUARD_MIN + t * (full_min - JOINT2_GUARD_MIN)


def encode_jpeg(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _build_tonemap_lut() -> np.ndarray:
    linear = (np.arange(256, dtype=np.float32) / 255.0) ** 2.2 * TONEMAP_EXPOSURE
    mapped = np.clip(linear * (2.51 * linear + 0.03) / (linear * (2.43 * linear + 0.59) + 0.14), 0, 1)
    return (mapped ** (1 / 2.2) * 255).astype(np.uint8)


# The map is per-channel uint8 -> uint8, so a 256-entry LUT replaces two pow()
# passes over every pixel of every frame.
_TONEMAP_LUT = _build_tonemap_lut()


def _tonemap(rgb: np.ndarray) -> np.ndarray:
    return _TONEMAP_LUT[rgb]


def _navigation_grid(base_grid: np.ndarray, seen_free: np.ndarray, seen_occ: np.ndarray) -> np.ndarray:
    """Clip a lidar-authored map to the apartment's authored floor.

    Lidar remains the sole source of free/occupied geometry inside the
    apartment so AMCL and Nav2 see the same thin walls and furniture legs.
    ``base_grid`` contributes only its known-vs-unknown footprint: a beam may
    cross the simulator's infinite ground plane, but it cannot turn a cell
    without authored apartment floor into navigable space.
    """
    grid = np.full(base_grid.shape, -1, dtype=np.int8)
    interior = base_grid != -1
    grid[seen_free & interior] = 0
    grid[seen_occ & interior] = 100  # lidar endpoint wins over a grazing clear
    return grid


def _camera_quat(forward, up) -> np.ndarray:
    z = -np.array(forward, dtype=float)
    y = np.array(up, dtype=float)
    x = np.cross(y, z)
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, np.column_stack([x, y, z]).flatten())
    return quat


# Apartment meshes (decomposed hulls + textured rooms), generated by
# sim/tools -- see world.default_assets_dir (VIRTUAL_MARS_ASSETS overrides).
ASSETS_DIR = world.default_assets_dir()


def _texture_cap(render_w: int) -> int | None:
    """Resolution cap for the visual-room texture atlases. 2048 is visually
    indistinguishable from the source 4096 on a full-res 640x480 camera (the
    parquet grain survives); half-res renders can't see past 1024. Each 4096
    atlas costs ~50MB in the model plus ~50MB per GL renderer context, so the
    cap is most of the world server's memory diet.
    VIRTUAL_MARS_TEXTURE_MAX overrides (0 = keep original resolution)."""
    raw = os.environ.get("VIRTUAL_MARS_TEXTURE_MAX", "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            value = int(raw)
            if value == 0:
                return None
            if value > 0:
                return value
    return 2048 if render_w > CAMERA_WIDTH // 2 else 1024


def _model_cache_path(xml: str, asset_files: list[Path]) -> Path:
    """Cache location for the compiled model, keyed by everything that shapes
    it: the generated MJCF (which embeds resolved mesh/texture paths, so the
    texture cap and asset locations are covered), the referenced files'
    mtime+size, and the MuJoCo version. Compiling 1300+ convex hulls costs
    minutes on weak machines and leaves ~0.4GB of heap debris; loading the
    saved binary takes ~50ms and only the model's real ~120MB."""
    digest = hashlib.sha256()
    digest.update(mujoco.__version__.encode())
    digest.update(xml.encode())
    # Cameras are added to the spec after the XML is built, so they are not in
    # `xml` -- without this a cache written before a camera was added or its fov
    # changed loads happily and silently lacks it.
    digest.update(repr(sorted(CAMERAS.items())).encode())
    for path in sorted(set(asset_files)):
        try:
            st = path.stat()
        except OSError:
            continue
        digest.update(f"{path}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return ASSETS_DIR / ".model_cache" / f"world-{digest.hexdigest()[:16]}.mjb"


def release_freed_heap() -> None:
    """Hand freed heap pages back to the OS (Linux/glibc only, no-op
    elsewhere). MjSpec.compile churns through ~0.5-1GB of scratch (qhull,
    texture decode) that glibc keeps after free -- on a 4GB machine that
    phantom gigabyte is the difference between the stack fitting in RAM and
    the physics thread living in swap."""
    if sys.platform != "linux":
        return
    import ctypes

    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)


class VirtualMars:
    def __init__(
        self,
        split_dir: Path | None = None,
        render_wh: tuple[int, int] | None = None,
        depth_render_wh: tuple[int, int] | None = None,
    ):
        # render_wh / depth_render_wh override the offscreen render
        # resolutions (default: the camera-native CAMERA_WIDTH x
        # CAMERA_HEIGHT). The ROS driver renders RGB at half res and depth at
        # the pointcloud's native res -- software-GL cost scales with fill
        # rate -- and upscales at the wire; direct/notebook users keep full res.
        self._render_w, self._render_h = render_wh or (CAMERA_WIDTH, CAMERA_HEIGHT)
        self._depth_w, self._depth_h = depth_render_wh or (self._render_w, self._render_h)
        rooms = world.find_decomposed_rooms(split_dir or ASSETS_DIR / "apartment_split_v2")
        if not rooms:
            raise RuntimeError(
                f"no decomposed rooms under {split_dir or ASSETS_DIR} -- "
                "run decompose_rooms.py or set VIRTUAL_MARS_ASSETS"
            )
        visual_dir = ASSETS_DIR / "apartment_visual"
        visual_rooms = world.find_visual_rooms(visual_dir) if visual_dir.is_dir() else {}

        # Droppable props: sidecars from the tracked source dir plus any the
        # asset bundle shipped, each parked off-map until something places it.
        self.props = PropRegistry.load([world.repo_root() / "sim" / "props", ASSETS_DIR / "props"])
        xml = world.build_world_xml(
            rooms,
            include_placeholder_robot=False,
            visual_rooms=visual_rooms,
            texture_max=_texture_cap(self._render_w),
            props=self.props,
        )
        # Lidar rays hit only the textured visual meshes (true surfaces, like
        # a real lidar) when available -- without them, fall back to all
        # groups (the collision hulls, ~1cm inflated).
        self._lidar_groups = None
        if visual_rooms:
            self._lidar_groups = np.zeros(6, dtype=np.uint8)
            self._lidar_groups[world.VISUAL_GROUP] = 1

        urdf_path = world.default_urdf_path()
        asset_files = [
            urdf_path,
            # Everything that shapes the model AFTER the cache lookup has to
            # key the cache too, or an edit loads a stale .mjb: world.py
            # (planar base, contact tuning), core.py (cameras, texture cap)
            # and constants.py (camera FOVs baked into the compiled cameras).
            Path(world.__file__),
            Path(__file__),
            Path(__file__).with_name("constants.py"),
            *(f for pieces in rooms.values() for f in pieces),
            *(p for obj in visual_rooms.values() for p in (obj, obj.with_suffix(".png"))),
            *(f for f in urdf_path.parent.rglob("*") if f.suffix in (".stl", ".dae", ".obj", ".png", ".urdf")),
            # A prop mesh appearing (or being republished) changes the world.
            *self.props.asset_files(),
        ]
        cache_path = _model_cache_path(xml, asset_files)
        self.model = None
        if cache_path.exists():
            with contextlib.suppress(Exception):  # noqa: BLE001 -- corrupt cache falls back to compiling
                self.model = mujoco.MjModel.from_binary_path(str(cache_path))
        if self.model is None:
            world_spec = mujoco.MjSpec.from_string(xml)
            robot_spec = world.load_robot_spec(urdf_path)
            world.add_planar_base(robot_spec)
            world.tune_contacts(robot_spec)
            world_spec.attach(robot_spec, frame=world_spec.worldbody.add_frame(), prefix="robot_")

            for cam_name, (body_name, forward, up, fovy) in CAMERAS.items():
                cam = world_spec.body(body_name).add_camera()
                cam.name = cam_name
                # The head renders the measured lens (anisotropic focal,
                # off-centre principal point — no fovy can express it). The
                # wrist keeps a fovy: its servo constants are tuned against one.
                # So does nav: the policy trained on a 110 deg PINHOLE, and
                # rendering it through the real lens would hand the checkpoint
                # a geometry it never saw.
                if cam_name in ("wrist", "wide"):
                    cam.fovy = fovy
                else:
                    cam.resolution = [CAMERA_WIDTH, CAMERA_HEIGHT]
                    cam.focal_pixel = [CAMERA_FX, CAMERA_FY]
                    cam.principal_pixel = list(CAMERA_PRINCIPAL_PIXEL)
                    cam.sensor_size = [0.0064, 0.0048]  # any size; the pixel forms override it
                cam.quat = _camera_quat(forward, up)

            self.model = world_spec.compile()
            del world_spec, robot_spec  # spec copies of every mesh/texture
            with contextlib.suppress(OSError):
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                for stale in cache_path.parent.glob("world-*.mjb"):
                    if stale != cache_path:
                        stale.unlink(missing_ok=True)
                mujoco.mj_saveModel(self.model, str(cache_path), None)
        world.style_robot_geoms(self.model)
        # The planar base pins z at the plane, so the ground's 7mm contact
        # margin reads as permanent penetration -- huge normal force whose
        # friction cone glues the base. The worker's ground has no margin.
        self.model.geom("ground").margin = 0.0
        # znear is a fraction of stat.extent; pin it to 0.03m (the viewer's
        # camera near plane) so the cameras clip their own housing shells
        # instead of rendering them.
        self.model.vis.map.znear = 0.03 / self.model.stat.extent
        # The textured rooms carry baked lighting; brighten toward the
        # Three.js render (ambient 1.2 + ACES exposure 1.5 there).
        self.model.vis.headlight.ambient[:] = 0.5
        self.model.vis.headlight.diffuse[:] = 0.5
        self.data = mujoco.MjData(self.model)

        self._base_id = self.model.body("robot_base_link").id
        self._base = {}  # "x"/"y"/"yaw" -> (qpos_adr, dof_adr)
        for short in ("x", "y", "yaw"):
            jid = self.model.joint(f"robot_base_{short}").id
            self._base[short] = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid])

        self._joints = {}  # name -> (qpos_adr, dof_adr, home)
        for name, home in ARM_HOME.items():
            jid = self.model.joint(f"robot_{name}").id
            self._joints[name] = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid], home)
        self._joint2_full_min = float(self.model.jnt_range[self.model.joint("robot_joint2").id][0])
        mimic_name, mimic_source, mimic_mult = world.MIMIC_JOINT
        jid = self.model.joint(f"robot_{mimic_name}").id
        self._mimic = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid], mimic_source, mimic_mult)
        # Servo (kd, torque ceiling) per dof: the two gripper fingers run on the
        # real servo's rating, everything else on the generic arm clamp.
        self._servo = {dadr: (KD_JOINT, EFFORT_LIMIT) for _q, dadr, _home in self._joints.values()}
        for dadr in (self._joints[mimic_source][1], self._mimic[1]):
            self._servo[dadr] = (KD_GRIPPER, GRIPPER_EFFORT_LIMIT)

        # Props are parked off-map in the model, so they exist only once
        # something places them (and only then does anything report them).
        # mj_resetData restores the parked pose, so reset() just has to forget
        # which ones were out.
        self.props.bind(self.model)

        self._renderer: mujoco.Renderer | None = None
        self._depth_renderer: mujoco.Renderer | None = None
        self._cmd_vx = 0.0
        self._cmd_wz = 0.0
        self._cmd_sim_time = -math.inf
        self._hold = None  # (x, y, yaw) the stopped base is keeping, or None
        self._still_since = None  # sim time the base went quiet, or None
        self.world_epoch = -1
        self.reset()
        release_freed_heap()

    def reset(self) -> None:
        self.world_epoch += 1
        mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[self._base["x"][0]] = SPAWN_X
        self.data.qpos[self._base["y"][0]] = SPAWN_Y
        self.data.qpos[self._base["yaw"][0]] = math.radians(SPAWN_YAW_DEG)
        for qadr, _dadr, home in self._joints.values():
            self.data.qpos[qadr] = home
        mq, _md, source, mult = self._mimic
        self.data.qpos[mq] = mult * ARM_HOME[source]
        self.props.mark_all_parked()  # mj_resetData already re-parked every prop
        self._cmd_vx = self._cmd_wz = 0.0
        self._cmd_sim_time = -math.inf
        self._hold = None
        self._still_since = None
        mujoco.mj_forward(self.model, self.data)

    def set_cmd_vel(self, vx: float, wz: float) -> None:
        self._cmd_vx, self._cmd_wz = clamp_cmd_vel(vx, wz)
        self._cmd_sim_time = self.data.time

    def set_joint_target(self, name: str, value: float) -> None:
        if name not in self._joints:
            return
        qadr, dadr, _home = self._joints[name]
        # The gripper close is commanded 0.6 rad past the mechanical stop
        # (hardware squeezes at its current limit there). Clamped, the error
        # still saturates the torque ceiling; the blades stop at the stop.
        jid = self.model.joint(f"robot_{name}").id
        if self.model.jnt_limited[jid]:
            lo, hi = self.model.jnt_range[jid]
            value = max(lo, min(hi, value))
        self._joints[name] = (qadr, dadr, value)

    def joint_targets(self) -> dict[str, float]:
        return {name: target for name, (_q, _d, target) in self._joints.items()}

    def step(self, duration: float) -> None:
        """Advance the sim by `duration` seconds, applying servos each step."""
        end = self.data.time + duration
        while self.data.time < end:
            self._apply_control()
            mujoco.mj_step(self.model, self.data)
            if not np.all(np.isfinite(self.data.qpos)):
                self.reset()
                return

    def _apply_control(self) -> None:
        d = self.data
        dof_x, dof_y, dof_yaw = (self._base[k][1] for k in ("x", "y", "yaw"))

        lin = math.hypot(d.qvel[dof_x], d.qvel[dof_y])
        if lin > world.MAX_BASE_LINEAR_SPEED:
            d.qvel[dof_x] *= world.MAX_BASE_LINEAR_SPEED / lin
            d.qvel[dof_y] *= world.MAX_BASE_LINEAR_SPEED / lin
        if abs(d.qvel[dof_yaw]) > world.MAX_BASE_ANGULAR_SPEED:
            d.qvel[dof_yaw] = math.copysign(world.MAX_BASE_ANGULAR_SPEED, d.qvel[dof_yaw])

        expired = d.time - self._cmd_sim_time > CMD_VEL_TIMEOUT_S
        vx = 0.0 if expired else self._cmd_vx
        wz = 0.0 if expired else self._cmd_wz

        yaw = d.qpos[self._base["yaw"][0]]
        cos, sin = math.cos(yaw), math.sin(yaw)
        v_forward = d.qvel[dof_x] * cos + d.qvel[dof_y] * sin
        v_lateral = -d.qvel[dof_x] * sin + d.qvel[dof_y] * cos

        force_forward = world.KP_FORWARD * (vx - v_forward)
        force_lateral = -world.KP_LATERAL * v_lateral
        torque_yaw = world.KP_YAW * (wz - d.qvel[dof_yaw])
        hold_x, hold_y, hold_yaw = self._station_keeping(vx, wz)
        d.xfrc_applied[self._base_id, 0] = force_forward * cos - force_lateral * sin + hold_x
        d.xfrc_applied[self._base_id, 1] = force_forward * sin + force_lateral * cos + hold_y
        d.xfrc_applied[self._base_id, 5] = torque_yaw + hold_yaw

        # Re-clamp joint2 every step from the current joint1 target, like the
        # real node re-clamps every control cycle from the latest command.
        j2_min = joint2_min_target(self._joints["joint1"][2], self._joint2_full_min)
        for name, (qadr, dadr, target) in self._joints.items():
            if name == "joint2":
                target = max(target, j2_min)
            # Structural sag (world.STRUCT_STIFFNESS / ARM_BACKLASH_RAD):
            # the LINK settles below the encoder target under gravity load
            # (qfrc_bias ~= gravity torque at the near-static poses where
            # sag matters).
            bias = d.qfrc_bias[dadr]
            target -= bias / world.STRUCT_STIFFNESS + world.ARM_BACKLASH_RAD * math.tanh(bias / world.BACKLASH_TANH_NM)
            kd, limit = self._servo[dadr]
            torque = KP_JOINT * (target - d.qpos[qadr]) - kd * d.qvel[dadr]
            d.qfrc_applied[dadr] = max(-limit, min(limit, torque))
        mq, md, source, mult = self._mimic
        kd, limit = self._servo[md]
        target = mult * self._joints[source][2]
        torque = KP_JOINT * (target - d.qpos[mq]) - kd * d.qvel[md]
        d.qfrc_applied[md] = max(-limit, min(limit, torque))

    def _station_keeping(self, vx: float, wz: float) -> tuple[float, float, float]:
        """World-frame (fx, fy, torque_z) holding the stopped base where it
        stopped; zero while driving. The drive above is velocity-only, so
        with a zero command nothing else pulls the base back and the arm's
        reaction torque walks the robot -- real wheels and gearing don't
        give that ground away. The pose latches only after HOLD_SETTLE_S of
        quiet, so a skill's per-camera-frame cmd_vel gaps never anchor a
        base that is still meant to be driving."""
        d = self.data
        if vx or wz:
            self._hold = self._still_since = None
            return 0.0, 0.0, 0.0
        qx, qy, qyaw = (self._base[k][0] for k in ("x", "y", "yaw"))
        if self._still_since is None:
            self._still_since = d.time
        if d.time - self._still_since < world.HOLD_SETTLE_S:
            return 0.0, 0.0, 0.0
        if self._hold is None:
            self._hold = (float(d.qpos[qx]), float(d.qpos[qy]), float(d.qpos[qyaw]))
        hx, hy, hyaw = self._hold
        yaw_err = math.atan2(math.sin(hyaw - d.qpos[qyaw]), math.cos(hyaw - d.qpos[qyaw]))
        return (
            world.KP_HOLD_LINEAR * (hx - d.qpos[qx]),
            world.KP_HOLD_LINEAR * (hy - d.qpos[qy]),
            world.KP_HOLD_YAW * yaw_err,
        )

    def update_camera(self, camera: str) -> None:
        """Snapshot sim state into the renderer's scene (fast; call under the
        physics lock). The GL render itself (read_rgb) can then run outside."""
        if self._renderer is None:
            self._renderer = mujoco.Renderer(self.model, height=self._render_h, width=self._render_w)
        self._renderer.update_scene(self.data, camera=camera)
        # Shadows/reflections cost ~3x on software GL (242 -> 71 ms/frame).
        self._renderer.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = 0
        self._renderer.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0

    def read_rgb(self) -> np.ndarray:
        """Tone-mapped uint8 RGB of the last update_camera snapshot. Slow
        (software GL) -- do not hold the physics lock across this."""
        return _tonemap(self._renderer.render())

    def render_rgb(self, camera: str) -> np.ndarray:
        self.update_camera(camera)
        return self.read_rgb()

    def render_jpeg(self, camera: str) -> bytes:
        return encode_jpeg(self.render_rgb(camera))

    def update_depth(self, camera: str) -> None:
        """Depth counterpart of update_camera. The robot's own geoms (group 0)
        are excluded: the real stereo pipeline can't resolve the arm/head that
        close to the lens (max_disparity limit + edge/speckle filters), so the
        robot never appears in its own depth -- without this, STVL marks the
        arm as an obstacle at the footprint and Nav2 can't plan."""
        if self._depth_renderer is None:
            self._depth_renderer = mujoco.Renderer(self.model, height=self._depth_h, width=self._depth_w)
            self._depth_renderer.enable_depth_rendering()
            self._depth_scene_option = mujoco.MjvOption()
            self._depth_scene_option.geomgroup[0] = 0
        self._depth_renderer.update_scene(self.data, camera=camera, scene_option=self._depth_scene_option)

    def read_depth(self) -> np.ndarray:
        return self._depth_renderer.render()

    def render_depth(self, camera: str) -> np.ndarray:
        """Depth image in meters (float32, render height x width)."""
        self.update_depth(camera)
        return self.read_depth()

    def lidar_scan(self, n_rays: int, max_range: float) -> np.ndarray:
        """360-degree planar scan from the base_laser mount, CCW from the
        robot's +x. Distances in meters; max_range where nothing was hit."""
        body_id = self.model.body("robot_base_laser").id
        origin = self.data.xpos[body_id].copy()
        _x, _y, yaw = self.pose()
        angles = yaw + np.arange(n_rays) * (2 * math.pi / n_rays)
        vecs = np.zeros((n_rays, 3))
        vecs[:, 0] = np.cos(angles)
        vecs[:, 1] = np.sin(angles)

        geomids = np.full(n_rays, -1, dtype=np.int32)
        dists = np.full(n_rays, -1.0)
        mujoco.mj_multiRay(
            self.model,
            self.data,
            origin,
            vecs.flatten(),
            self._lidar_groups,
            1,  # include static geoms
            self._base_id,  # exclude the robot's own base body
            geomids,
            dists,
            None,  # surface normals not needed
            n_rays,
            max_range,
        )
        dists[geomids == -1] = max_range
        return np.minimum(dists, max_range)

    def occupancy_grid(self, resolution: float = 0.05) -> tuple[np.ndarray, float, float]:
        """Rasterize the collision world into a nav occupancy grid (values
        -1/0/100, row-major from the origin cell); returns (grid, origin_x,
        origin_y). Occupied = static collision triangles clipped to the
        robot-height slab and projected (height-exact: a probe-ray scheme
        loses any wall taller than the probe start). Free vs unknown =
        downward floor rays, with the robot parked out of bounds."""
        if self._lidar_groups is None:
            # The decomposed collision rooms deliberately omit floor faces;
            # without the authored visual rooms there is no trustworthy way
            # to distinguish apartment floor from the infinite ground plane.
            # Refuse an unsafe map instead of silently making exterior ground
            # navigable (or returning an unusable all-unknown grid).
            raise RuntimeError("navigation-map export requires the authored apartment_visual room meshes")

        wall_min, wall_top = 0.10, 1.4  # above rugs/thresholds; below ceilings

        # Apartment bounds from its geoms' bounding spheres.
        apt = self.model.body("apartment").id
        geom_ids = [i for i in range(self.model.ngeom) if self.model.geom_bodyid[i] == apt]
        mujoco.mj_forward(self.model, self.data)
        centers = self.data.geom_xpos[geom_ids]
        radii = self.model.geom_rbound[geom_ids]
        xmin, ymin = (centers[:, :2] - radii[:, None]).min(axis=0) - 0.3
        xmax, ymax = (centers[:, :2] + radii[:, None]).max(axis=0) + 0.3

        # Park the robot outside the map so the floor rays don't see it.
        saved = self.data.qpos.copy()
        self.data.qpos[self._base["x"][0]] = xmax + 50.0
        mujoco.mj_forward(self.model, self.data)

        width = int(np.ceil((xmax - xmin) / resolution))
        height = int(np.ceil((ymax - ymin) / resolution))
        xs = xmin + (np.arange(width) + 0.5) * resolution
        ys = ymin + (np.arange(height) + 0.5) * resolution
        gx, gy = np.meshgrid(xs, ys)  # row-major: y rows, x cols
        n = width * height

        grid = np.full(n, -1, dtype=np.int8)
        origins = np.column_stack([gx.ravel(), gy.ravel(), np.full(n, wall_top)])
        # mj_multiRay shares one origin, so cast per cell (one-time, ~40k rays, ~1s).
        geomid_out = np.zeros(1, dtype=np.int32)
        down = np.array([0.0, 0.0, -1.0])
        for i in range(n):
            d = mujoco.mj_ray(self.model, self.data, origins[i], down, self._lidar_groups, 1, -1, geomid_out)
            hit = int(geomid_out[0])
            if hit != -1 and d >= 0 and self.model.geom_bodyid[hit] == apt:
                grid[i] = 0  # an authored apartment surface below: interior floor
        grid = grid.reshape(height, width)
        grid[self._rasterize_static_slab(wall_min, wall_top, xmin, ymin, width, height, resolution)] = 100

        self.data.qpos[:] = saved
        mujoco.mj_forward(self.model, self.data)
        return grid, float(xmin), float(ymin)

    def lidar_occupancy_grid(
        self,
        resolution: float = 0.05,
        origin_stride_m: float = 0.4,
        n_rays: int = 720,
        max_range: float = 12.0,
    ) -> tuple[np.ndarray, float, float]:
        """Rasterize a lidar-consistent map clipped to authored floor.

        A virtual SLAM pass casts horizontal rays at the real laser height
        against the same visual surfaces ``lidar_scan()`` hits.  Lidar beams
        establish observable free space and obstacle endpoints.  The collision
        grid is used only to clip those observations to authored apartment
        floor; its thicker robot-height furniture projection is deliberately
        not fused into the localization map.

        Returns (grid, origin_x, origin_y) like ``occupancy_grid()``.
        """
        # Reachable-floor candidates for scan origins (also fixes the bounds).
        base_grid, xmin, ymin = self.occupancy_grid(resolution)
        height, width = base_grid.shape

        # True laser height above the floor, from the model itself.
        mujoco.mj_forward(self.model, self.data)
        laser_z = float(self.data.xpos[self.model.body("robot_base_laser").id][2])

        # Park the robot far away so rays never hit it.
        saved = self.data.qpos.copy()
        self.data.qpos[self._base["x"][0]] += 100.0
        mujoco.mj_forward(self.model, self.data)

        try:
            stride = max(1, int(round(origin_stride_m / resolution)))
            rows, cols = np.nonzero(base_grid == 0)
            pick = (rows % stride == 0) & (cols % stride == 0)
            origin_xy = np.column_stack(
                [xmin + (cols[pick] + 0.5) * resolution, ymin + (rows[pick] + 0.5) * resolution]
            )

            angles = np.arange(n_rays) * (2.0 * math.pi / n_rays)
            vecs = np.zeros((n_rays, 3))
            vecs[:, 0] = np.cos(angles)
            vecs[:, 1] = np.sin(angles)
            step = resolution * 0.9
            ts = (np.arange(int(max_range / step)) + 0.5) * step  # sample offsets along a beam

            def cells(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
                c = ((x - xmin) / resolution).astype(np.int64)
                r = ((y - ymin) / resolution).astype(np.int64)
                ok = (c >= 0) & (c < width) & (r >= 0) & (r < height)
                return r[ok], c[ok]

            seen_free = np.zeros((height, width), dtype=bool)
            seen_occ = np.zeros((height, width), dtype=bool)
            geomids = np.full(n_rays, -1, dtype=np.int32)
            dists = np.full(n_rays, -1.0)
            for ox, oy in origin_xy:
                origin = np.array([ox, oy, laser_z])
                geomids.fill(-1)
                dists.fill(-1.0)
                mujoco.mj_multiRay(
                    self.model,
                    self.data,
                    origin,
                    vecs.flatten(),
                    self._lidar_groups,
                    1,
                    self._base_id,
                    geomids,
                    dists,
                    None,
                    n_rays,
                    max_range,
                )
                hit = (geomids != -1) & (dists >= 0)
                if not hit.any():
                    continue
                d = dists[hit]
                dirs = vecs[hit, :2]
                # Clear along each beam, stopping one cell short of the surface.
                mask = ts[None, :] < (d - resolution)[:, None]
                px = ox + (dirs[:, 0:1] * ts[None, :])[mask]
                py = oy + (dirs[:, 1:2] * ts[None, :])[mask]
                r, c = cells(px, py)
                seen_free[r, c] = True
                # Endpoints are the surfaces the lidar actually returns from.
                r, c = cells(ox + dirs[:, 0] * d, oy + dirs[:, 1] * d)
                seen_occ[r, c] = True
        finally:
            # Restore even if a ray call raises: a leaked parked pose would
            # corrupt every later scan/step on this instance.
            self.data.qpos[:] = saved
            mujoco.mj_forward(self.model, self.data)

        return _navigation_grid(base_grid, seen_free, seen_occ), float(xmin), float(ymin)

    def _rasterize_static_slab(
        self, zlo: float, zhi: float, xmin: float, ymin: float, width: int, height: int, resolution: float
    ) -> np.ndarray:
        """Boolean (height, width) mask of cells touched by static collision
        triangles within the z-slab [zlo, zhi] (clipped before projecting)."""
        from PIL import ImageDraw

        robot_bodies = set()
        for b in range(self.model.nbody):
            parent = b
            while parent != 0:
                if parent == self._base_id:
                    robot_bodies.add(b)
                    break
                parent = self.model.body_parentid[parent]

        polys: list[np.ndarray] = []
        for g in range(self.model.ngeom):
            if self.model.geom_type[g] != mujoco.mjtGeom.mjGEOM_MESH:
                continue  # the only non-mesh static geom is the ground plane
            if self.model.geom_bodyid[g] in robot_bodies:
                continue
            did = self.model.geom_dataid[g]
            vadr, vnum = self.model.mesh_vertadr[did], self.model.mesh_vertnum[did]
            fadr, fnum = self.model.mesh_faceadr[did], self.model.mesh_facenum[did]
            verts = self.model.mesh_vert[vadr : vadr + vnum]
            faces = self.model.mesh_face[fadr : fadr + fnum]
            world_verts = verts @ self.data.geom_xmat[g].reshape(3, 3).T + self.data.geom_xpos[g]
            tris = world_verts[faces]  # (nf, 3, 3)
            zs = tris[:, :, 2]
            polys.extend(tris[(zs.max(axis=1) > zlo) & (zs.min(axis=1) < zhi)])

        def clip_z(poly: np.ndarray, z: float, keep_above: bool) -> np.ndarray:
            """Sutherland-Hodgman against one z-plane."""
            sign = 1.0 if keep_above else -1.0
            out: list[np.ndarray] = []
            for i in range(len(poly)):
                a, b = poly[i], poly[(i + 1) % len(poly)]
                da, db = sign * (a[2] - z), sign * (b[2] - z)
                if da >= 0:
                    out.append(a)
                if (da >= 0) != (db >= 0):
                    out.append(a + (b - a) * (da / (da - db)))
            return np.asarray(out)

        mask_img = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask_img)
        for tri in polys:
            poly = clip_z(clip_z(tri, zlo, True), zhi, False)
            if len(poly) < 2:
                continue
            px = [((x - xmin) / resolution, (y - ymin) / resolution) for x, y, _z in poly]
            if len(px) == 2:
                draw.line(px, fill=255, width=1)
            else:
                draw.polygon(px, fill=255, outline=255)
        return np.asarray(mask_img) > 0

    def pose(self) -> tuple[float, float, float]:
        return (
            float(self.data.qpos[self._base["x"][0]]),
            float(self.data.qpos[self._base["y"][0]]),
            float(self.data.qpos[self._base["yaw"][0]]),
        )

    def velocity(self) -> tuple[float, float, float]:
        """(v_forward, v_lateral, wz) in the base frame, for odometry."""
        dof_x, dof_y, dof_yaw = (self._base[k][1] for k in ("x", "y", "yaw"))
        yaw = self.data.qpos[self._base["yaw"][0]]
        cos, sin = math.cos(yaw), math.sin(yaw)
        vx, vy = self.data.qvel[dof_x], self.data.qvel[dof_y]
        return (vx * cos + vy * sin, -vx * sin + vy * cos, float(self.data.qvel[dof_yaw]))

    def head_pitch_deg(self) -> float:
        qadr, _dadr, _t = self._joints["joint_head"]
        return math.degrees(float(self.data.qpos[qadr]))

    def joint_positions(self) -> dict[str, float]:
        return {name: float(self.data.qpos[qadr]) for name, (qadr, _d, _t) in self._joints.items()}

    def encoder_positions(self) -> dict[str, float]:
        """What the real servo encoders would read: link angle plus the
        structural deflection due to gravity. The driver publishes THIS on /joint_states --
        real sag happens past the encoders, invisible to FK. The observer
        truth stream keeps joint_positions(), so viewers draw the sag."""
        out = {}
        for name, (qadr, dadr, _t) in self._joints.items():
            bias = self.data.qfrc_bias[dadr]
            sag = bias / world.STRUCT_STIFFNESS + world.ARM_BACKLASH_RAD * math.tanh(bias / world.BACKLASH_TANH_NM)
            out[name] = float(self.data.qpos[qadr] + sag)
        return out

    def object_poses(self) -> dict[str, list[float]]:
        """[x, y, z, qw, qx, qy, qz] per prop in world frame. Props parked off-map
        are omitted."""
        return self.props.poses(self.data)

    def object_centers(self) -> dict[str, tuple[float, float]]:
        """xy of each out prop's visual CENTRE (props.py center_offset), which
        is what a distance to a prop should mean -- the human scan stands
        feet-at-origin, so its raw body xy is where its feet are. The challenge
        judge measures against these; parked props are absent."""
        return {
            name: center for name in self.props.out if (center := self.props.center_xy(self.data, name)) is not None
        }

    def prop_manifest(self) -> list[dict]:
        """What every prop is and how to draw it (props.py), for the viewer."""
        return self.props.manifest()

    def drop_prop_at(self, name: str, x: float, y: float, yaw: float = 0.0) -> bool:
        """Release one prop above (x, y) and let physics settle it onto
        whatever is below (floor, sofa, table). False when prop does not exist."""
        if not self.props.drop_at(self.data, name, x, y, yaw):
            return False
        mujoco.mj_forward(self.model, self.data)
        return True

    def place_prop_at_robot(self, name: str) -> bool:
        """Set one prop down at rest, at its own reach offset from the robot's
        current pose. False when prop does not exist."""
        if not self.props.place_at_robot(self.data, name, self.pose()):
            return False
        mujoco.mj_forward(self.model, self.data)
        return True

    def place_group(self, group: str = "manipulation") -> int:
        """Set down every prop in `group` at its own reach offset from the
        robot's current pose, and return how many landed. Props outside the
        group stay where they are -- use remove_all_props() to clear."""
        placed = self.props.place_group(self.data, group, self.pose())
        mujoco.mj_forward(self.model, self.data)
        return placed

    def remove_all_props(self) -> None:
        """Send every prop back off-map -- the state the world starts in, and
        what reset() leaves behind."""
        self.props.park_all(self.data)
        mujoco.mj_forward(self.model, self.data)

    def remove_prop(self, name: str) -> bool:
        """Send one prop back off-map."""
        if not self.props.park(self.data, name):
            return False
        mujoco.mj_forward(self.model, self.data)
        return True
