"""Droppable props: MuJoCo bodies parked off-map until something places them.
Manipulation targets (cube, can, sock, ...) are free-floating; stationary
scenery can be kinematic. Both differ only in what their sidecars say.

A sidecar is a Python module exporting ``PROP = Prop(...)`` (the same shape as
sim/challenges/), found under any props root. Python rather than data because
the tuning behind these numbers is the valuable part of them and only a comment
can carry it -- see the sock's grasp band or the ball's density.

Paths inside a sidecar resolve relative to that sidecar, so a tracked prop can
point at a bundled mesh (``../assets/humans/casual_man.obj``) and a bundled
pack can keep everything in its own directory.

Meshes are OPTIONAL at every level: a prop whose mesh the installed bundle
lacks degrades to its collision primitive, drawn instead of hidden, so the
world is always complete even when the assets are not.

Geom groups follow the rest of the world (see world.py): a primitive-only prop
is one VISUAL_GROUP geom doing render, lidar and physics; a mesh-backed prop
splits them -- the textured mesh in VISUAL_GROUP, the collider under it in
COLLISION_GROUP.
"""

import importlib.util
import math
from dataclasses import dataclass, field
from pathlib import Path

# Parking row for undropped props: far off-map, spaced wide enough that the
# biggest prop (a 1.7m human lying down) cannot touch its neighbours. Bodies
# still exist in the model while parked -- mj_resetData restores exactly this
# pose, which is what makes reset() re-parking free.
PARK_X0 = 15.0
PARK_Y = 15.0
PARK_PITCH = 2.0


@dataclass
class Prop:
    """One droppable prop. Field defaults are the "small rigid object" case;
    a sidecar overrides only what it cares about."""

    name: str
    label: str = "?"  # one glyph, the viewer's chip for this prop
    title: str = ""  # human-readable name (defaults to name)
    # Props laid out together by one click, or None for a prop that is only
    # ever placed deliberately. The manipulation set is what the arm practises
    # on, and putting the whole set down at once is the workflow that matters
    # there: drive somewhere, lay out a fresh set, grab. A group's reach
    # offsets have to be chosen so the whole set lands side by side.
    group: str | None = None

    # -- geometry --
    # Mesh path relative to the sidecar. None (or a file that isn't installed)
    # leaves the prop as its bare primitive.
    mesh: str | None = None
    mesh_scale: float = 1.0  # 0.01 for a centimetre-unit scan
    texture: str | None = None  # defaults to "<mesh stem>_basecolor.png"
    # How the prop collides: a primitive ("box"/"sphere"/"cylinder", sized by
    # `size`), "hull" (the visual mesh's own convex hull), or "pieces" (the
    # convex decomposition sitting next to the mesh as <stem>_collision_*.obj).
    collision: str = "box"
    size: tuple[float, ...] = (0.02, 0.02, 0.02)

    # -- contact model --
    density: float = 700.0
    condim: int = 4
    friction: tuple[float, float, float] = (1.0, 0.02, 0.001)
    solref: tuple[float, float] = (0.01, 1.0)
    solimp: tuple[float, ...] | None = None
    priority: int | None = None
    margin: float | None = None
    rgba: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)

    # -- placement --
    # Height of the body origin when the prop is set down at rest, and the
    # height it is RELEASED from when dropped onto unknown ground (high enough
    # to clear furniture, then physics settles it).
    rest_z: float = 0.02
    drop_z: float | None = None
    # Kinematic props are fixed from dynamics but remain placeable through
    # MuJoCo's mocap pose arrays. This is for stationary scenery such as an NPC,
    # not for manipulation targets that need gravity and contact response.
    kinematic: bool = field(default=False, kw_only=True)
    # Where the robot puts this prop when asked to place it in front of itself:
    # robot-frame metres. The manipulation props' values place them on an arc
    # the arm can reach top-down -- do NOT round them off.
    reach: tuple[float, float] = (0.6, 0.0)
    # Body-frame offset from the body origin to the visual CENTRE. Only
    # non-zero for a mesh whose origin is not its middle (the human scan stands
    # feet-at-origin), so distances to it mean what a reader expects.
    center_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)

    # -- browser view (see sim/viewer/src/props.ts) --
    # Passed through to the viewer verbatim; an absent or unfetchable glb makes
    # the browser build the same primitive physics uses.
    viewer: dict = field(default_factory=dict)

    # Directory the sidecar came from; every relative path above resolves
    # against it. Set by the loader, never by a sidecar.
    root: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.name.replace("_", " ").capitalize()
        if self.drop_z is None:
            self.drop_z = self.rest_z

    # -- resolved asset paths --

    @property
    def mesh_path(self) -> Path | None:
        """The installed mesh, or None when this prop has no mesh or the asset
        bundle did not ship it."""
        if self.mesh is None or self.root is None:
            return None
        path = (self.root / self.mesh).resolve()
        return path if path.is_file() else None

    @property
    def texture_path(self) -> Path | None:
        mesh = self.mesh_path
        if mesh is None:
            return None
        name = self.texture or f"{mesh.stem}_basecolor.png"
        path = (mesh.parent / name).resolve()
        return path if path.is_file() else None

    @property
    def collision_pieces(self) -> list[Path]:
        """Convex decomposition next to the mesh, in the mesh's body frame."""
        mesh = self.mesh_path
        if mesh is None or self.collision != "pieces":
            return []
        return sorted(mesh.parent.glob(f"{mesh.stem}_collision_*.obj"))

    def asset_files(self) -> list[Path]:
        """Everything on disk that shapes this prop's MJCF -- the model cache
        key has to see all of it or a re-published mesh won't take effect."""
        return [p for p in (self.mesh_path, self.texture_path) if p is not None] + self.collision_pieces

    # -- MJCF --

    # MuJoCo primitive standing in for a mesh collider, keyed by how many
    # numbers `size` carries -- "hull"/"pieces" name a mesh, not a shape, so a
    # prop whose mesh never shipped still needs something real to be.
    _PRIMITIVE_FOR_SIZE = {1: "sphere", 2: "cylinder", 3: "box"}

    def _primitive_geom(self, name: str, group: int, physical: bool) -> str:
        shape = self.collision
        if shape not in ("box", "sphere", "cylinder"):
            shape = self._PRIMITIVE_FOR_SIZE.get(len(self.size), "box")
        size = " ".join(f"{s:g}" for s in self.size)
        return self._geom(name, f'type="{shape}" size="{size}"', group, physical)

    def _geom(self, name: str, shape: str, group: int, physical: bool) -> str:
        """One geom. `physical` false makes it a pure visual surface: no
        contacts, no mass, so the collider underneath owns the dynamics."""
        friction = " ".join(f"{f:g}" for f in self.friction)
        solref = " ".join(f"{s:g}" for s in self.solref)
        extra = ""
        if not physical:
            extra += ' contype="0" conaffinity="0" density="0"'
        else:
            extra += f' density="{self.density:g}" condim="{self.condim}"'
            extra += f' friction="{friction}" solref="{solref}"'
            if self.solimp is not None:
                extra += f' solimp="{" ".join(f"{s:g}" for s in self.solimp)}"'
            if self.priority is not None:
                extra += f' priority="{self.priority}"'
            if self.margin is not None:
                extra += f' margin="{self.margin:g}"'
        return f'\n      <geom name="{name}" {shape}{extra}{{fill}}/>'.replace("{fill}", self._fill(group))

    def _fill(self, group: int) -> str:
        return f' rgba="{" ".join(f"{c:g}" for c in self.rgba)}" group="{group}"'

    def assets_xml(self, visual_group: int) -> str:
        """<asset> entries for this prop (nothing at all without a mesh)."""
        mesh, texture = self.mesh_path, self.texture_path
        if mesh is None:
            return ""
        scale = (
            f' scale="{self.mesh_scale:g} {self.mesh_scale:g} {self.mesh_scale:g}"' if self.mesh_scale != 1.0 else ""
        )
        out = f'\n    <mesh name="{self.name}" file="{mesh}"{scale}/>'
        if texture is not None:
            out += f'\n    <texture name="tex_{self.name}" type="2d" file="{texture}"/>'
            out += f'\n    <material name="mat_{self.name}" texture="tex_{self.name}" specular="0.1" shininess="0.1"/>'
        for i, piece in enumerate(self.collision_pieces):
            out += f'\n    <mesh name="{self.name}_col{i}" file="{piece}"/>'
        return out

    def body_xml(self, park_x: float, park_y: float, visual_group: int, collision_group: int) -> str:
        """The prop's dynamic or mocap body, parked at (park_x, park_y)."""
        mesh = self.mesh_path
        if mesh is None:
            # No mesh installed: the primitive IS the prop -- visible, lidar-
            # visible and collidable, all from one geom.
            geoms = self._primitive_geom(f"{self.name}_geom", visual_group, physical=True)
        else:
            material = f' material="mat_{self.name}"' if self.texture_path is not None else self._fill(visual_group)
            geoms = (
                f'\n      <geom name="{self.name}_visual" mesh="{self.name}" type="mesh"{material}'
                f' contype="0" conaffinity="0" density="0" group="{visual_group}"/>'
            )
            pieces = self.collision_pieces
            if pieces:
                for i in range(len(pieces)):
                    geoms += self._geom(
                        f"{self.name}_col{i}",
                        f'mesh="{self.name}_col{i}" type="mesh"',
                        collision_group,
                        physical=True,
                    )
            elif self.collision == "hull" or self.collision == "pieces":
                # "pieces" with none installed degrades to the mesh's own hull.
                geoms += self._geom(
                    f"{self.name}_collision", f'mesh="{self.name}" type="mesh"', collision_group, physical=True
                )
            else:
                geoms += self._primitive_geom(f"{self.name}_geom", collision_group, physical=True)
        body_kind = ' mocap="true"' if self.kinematic else ""
        joint = "" if self.kinematic else f'\n      <freejoint name="{self.name}_free"/>'
        return f"""
    <body name="{self.name}"{body_kind} pos="{park_x:.4f} {park_y:.4f} {self.rest_z:g}">{joint}{geoms}
    </body>"""

    # -- what the browser needs to draw and offer this prop --

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "title": self.title,
            "group": self.group,
            "collision": self.collision,
            "size": list(self.size),
            "rgba": list(self.rgba),
            "viewer": self.viewer,
        }


# --- loading ---


def load_props(roots: list[Path]) -> dict[str, Prop]:
    """Every prop under `roots`, later roots overriding earlier ones by name.
    A broken sidecar is skipped with a warning -- one bad prop must not take
    out the world."""
    found: dict[str, Prop] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"sim_prop_{path.stem}", path)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                prop: Prop = module.PROP
                prop.root = path.parent
                found[prop.name] = prop
            except Exception as exc:  # noqa: BLE001 -- a bad sidecar loses its prop, nothing else
                print(f"[props] skipping {path.name}: {exc!r}", flush=True)
    return found


class PropRegistry:
    """The props in one world: their MJCF, their addresses in the compiled
    model, and which of them are currently out on the floor.

    The method-name verbs are load-bearing for dynamic props: ``drop_*``
    releases from ``drop_z`` and lets physics settle the prop onto whatever is
    below, ``place_*`` sets it down at ``rest_z`` already at rest. Kinematic
    scenery snaps to the corresponding pose because gravity cannot move it.
    """

    def __init__(self, props: dict[str, Prop]):
        self.props = props
        # name -> (body id, qpos adr, dof adr, mocap id). Exactly one address
        # family is present: dynamic props use qpos/dof, kinematic props mocap.
        self._addr: dict[str, tuple[int, int | None, int | None, int | None]] = {}
        self.out: set[str] = set()  # on the floor rather than parked off-map

    @classmethod
    def load(cls, roots: list[Path]) -> "PropRegistry":
        registry = cls(load_props(roots))
        if registry.props:
            print(f"[props] loaded: {', '.join(registry.props)}", flush=True)
        return registry

    def park_xy(self, name: str) -> tuple[float, float]:
        return PARK_X0 + list(self.props).index(name) * PARK_PITCH, PARK_Y

    def asset_files(self) -> list[Path]:
        return [f for prop in self.props.values() for f in prop.asset_files()]

    # -- MJCF --

    def assets_xml(self, visual_group: int) -> str:
        return "".join(prop.assets_xml(visual_group) for prop in self.props.values())

    def bodies_xml(self, visual_group: int, collision_group: int) -> str:
        out = []
        for name, prop in self.props.items():
            park_x, park_y = self.park_xy(name)
            out.append(prop.body_xml(park_x, park_y, visual_group, collision_group))
        return "".join(out)

    # -- model binding --

    def bind(self, model) -> None:
        """Resolve each prop's addresses in the compiled model. Called once
        after the model is built (or loaded from cache)."""
        self._addr.clear()
        for name, prop in self.props.items():
            bid = model.body(name).id
            if prop.kinematic:
                mocap_id = int(model.body_mocapid[bid])
                if mocap_id < 0:
                    raise ValueError(f"kinematic prop {name!r} did not compile as a mocap body")
                self._addr[name] = (bid, None, None, mocap_id)
            else:
                jid = model.joint(f"{name}_free").id
                self._addr[name] = (bid, int(model.jnt_qposadr[jid]), int(model.jnt_dofadr[jid]), None)

    # -- placement (callers hold the sim lock) --

    def _set_pose(self, data, name: str, x: float, y: float, z: float, yaw: float) -> None:
        """Write one prop's pose and zero its velocity. The z is the caller's
        choice of drop_z or rest_z -- that choice IS drop-vs-place."""
        _bid, qadr, dadr, mocap_id = self._addr[name]
        half = yaw / 2
        quat = (math.cos(half), 0.0, 0.0, math.sin(half))
        if mocap_id is not None:
            data.mocap_pos[mocap_id] = [x, y, z]
            data.mocap_quat[mocap_id] = quat
        else:
            assert qadr is not None and dadr is not None
            data.qpos[qadr : qadr + 7] = [x, y, z, *quat]
            data.qvel[dadr : dadr + 6] = 0.0
        self.out.add(name)

    def drop_at(self, data, name: str, x: float, y: float, yaw: float = 0.0) -> bool:
        """Release a dynamic prop at drop_z, or snap a kinematic one there,
        yawed about +z. False when the prop does not exist."""
        if name not in self._addr:
            return False
        self._set_pose(data, name, x, y, self.props[name].drop_z, yaw)
        return True

    def place_at_robot(self, data, name: str, pose: tuple[float, float, float]) -> bool:
        """Set a prop down at rest_z at its `reach` offset from the robot's
        current pose. False when prop does not exist."""
        if name not in self._addr:
            return False
        prop = self.props[name]
        rx, ry, ryaw = pose
        forward, lateral = prop.reach
        cos, sin = math.cos(ryaw), math.sin(ryaw)
        x = rx + cos * forward - sin * lateral
        y = ry + sin * forward + cos * lateral
        self._set_pose(data, name, x, y, prop.rest_z, 0.0)
        return True

    def groups(self) -> list[str]:
        """Named groups in sidecar order, each appearing once (ungrouped props
        contribute nothing)."""
        return list(dict.fromkeys(p.group for p in self.props.values() if p.group))

    def place_group(self, data, group: str, pose: tuple[float, float, float]) -> int:
        """Set down every prop in one group at its own reach offset, leaving
        every other prop where it is. Returns how many landed."""
        placed = 0
        for name, prop in self.props.items():
            if prop.group == group:
                placed += bool(self.place_at_robot(data, name, pose))
        return placed

    def park(self, data, name: str) -> bool:
        """Send one prop back off-map."""
        if name not in self._addr:
            return False
        park_x, park_y = self.park_xy(name)
        self._set_pose(data, name, park_x, park_y, self.props[name].rest_z, 0.0)
        self.out.discard(name)
        return True

    def park_all(self, data) -> None:
        for name in list(self.props):
            self.park(data, name)

    def mark_all_parked(self) -> None:
        """Catch the bookkeeping up after mj_resetData, which has already
        restored every prop to its parked pose."""
        self.out.clear()

    # -- readout --

    def poses(self, data) -> dict[str, list[float]]:
        """Ground-truth {name: [x, y, z, qw, qx, qy, qz]} for props on the
        floor. Parked props are omitted, not reported at their parking spot."""
        return {
            name: [*map(float, data.xpos[bid]), *map(float, data.xquat[bid])]
            for name, (bid, _q, _d, _m) in self._addr.items()
            if name in self.out
        }

    def center_xy(self, data, name: str) -> tuple[float, float] | None:
        """xy of a prop's visual centre while it is out, correcting for an
        off-centre body origin (the human stands feet-at-origin). None while
        parked."""
        if name not in self.out or name not in self._addr:
            return None
        bid, _q, _d, _m = self._addr[name]
        ox, oy, oz = self.props[name].center_offset
        if ox == oy == oz == 0.0:
            return float(data.xpos[bid][0]), float(data.xpos[bid][1])
        w, qx, qy, qz = (float(v) for v in data.xquat[bid])
        # Rows 0/1 of the quaternion rotation matrix; z is irrelevant for xy.
        rx = (1 - 2 * (qy * qy + qz * qz)) * ox + 2 * (qx * qy - w * qz) * oy + 2 * (qx * qz + w * qy) * oz
        ry = 2 * (qx * qy + w * qz) * ox + (1 - 2 * (qx * qx + qz * qz)) * oy + 2 * (qy * qz - w * qx) * oz
        return float(data.xpos[bid][0]) + rx, float(data.xpos[bid][1]) + ry

    def manifest(self) -> list[dict]:
        """What the viewer needs to draw every prop and offer it as a button."""
        return [prop.manifest() for prop in self.props.values()]
