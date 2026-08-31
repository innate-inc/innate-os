"""Static world geometry as MuJoCo primitives: fixed bodies that make up a
room's shell and its furniture.

A sidecar is a Python module exporting ``ROOM = Room(...)`` (the same shape as
props.py and sim/challenges/), found under any rooms root.

WHY PRIMITIVES AND NOT MESHES.  The apartment arrives as scanned geometry, so
it has to be convex-decomposed into hulls -- 1200+ of them. A room authored as
boxes and cylinders is already convex, so pushing it through the mesh path
would decompose shapes that were never non-convex to begin with: slower to
compile, heavier to simulate, and lossy in the one dimension a vision benchmark
cannot afford, since collision hulls render in a cycling debug palette rather
than their own colour. A box stays a box.

These rooms carry no external assets. Every number is inlined into the world
XML, so the model cache -- which keys on that XML -- already sees any sidecar
edit; there is no asset_files() to register (contrast props.py, where a
republished mesh changes the world without changing a single character of XML).

Geom groups follow the rest of the world (see world.py): a collidable geom goes
in the same group as the room collision geometry, and a decor geom -- floor
seams, skirting -- is VISUAL_GROUP with contacts disabled, so it is drawn but
never touched.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .sidecars import load_sidecars


@dataclass
class Geom:
    """One primitive. Sizes are MuJoCo HALF-extents in metres, poses are in
    world coordinates (z up) -- the exporter has already done any conversion."""

    type: str  # "box" | "sphere" | "cylinder" | "capsule"
    size: tuple[float, ...]
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    rgba: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)
    name: str = ""
    # False makes this a pure visual surface: drawn, never collided with.
    collide: bool = True
    friction: tuple[float, float, float] = (0.9, 0.01, 0.001)
    # 3, matching the world's ground plane -- NOT 4. The friction triple is
    # (sliding, torsional, rolling), and condim 4 switches the torsional term
    # on. At 0.01 that is ~0.3 N.m of resistance about z on a 3 kg base, which
    # exceeds what KP_YAW commands: the robot sat on its own floor turning 2
    # degrees in 1.5 s, looking for all the world like a dead controller.
    condim: int = 3
    # The apartment hulls use 7mm; flat authored primitives meet exactly, so
    # they do not need the seam margin and are better off without the gap.
    margin: float = 0.0

    def xml(self, name: str, group: int) -> str:
        size = " ".join(f"{v:g}" for v in self.size)
        pos = " ".join(f"{v:g}" for v in self.pos)
        rgba = " ".join(f"{v:g}" for v in self.rgba)
        out = f'      <geom name="{name}" type="{self.type}" size="{size}" pos="{pos}"'
        # A sphere is rotation-invariant; emitting a quaternion for one is noise.
        if self.type != "sphere":
            out += f' quat="{" ".join(f"{v:g}" for v in self.quat)}"'
        out += f' rgba="{rgba}" group="{group}"'
        if self.collide:
            friction = " ".join(f"{v:g}" for v in self.friction)
            out += f' friction="{friction}" condim="{self.condim}"'
            if self.margin:
                out += f' margin="{self.margin:g}"'
        else:
            out += ' contype="0" conaffinity="0"'
        return out + "/>"


@dataclass
class Room:
    """One static room: a shell plus whatever furniture is bolted down.

    Furniture lives here rather than in props.py when it is a landmark rather
    than a target -- a table on a freejoint drifts the first time the base
    clips it, silently moving the thing a long-horizon challenge navigates by.
    """

    name: str
    geoms: list[Geom] = field(default_factory=list)
    title: str = ""
    # Where the robot should start in this room, if it overrides the world
    # default. (x, y, yaw_deg).
    spawn: tuple[float, float, float] | None = None
    root: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.name.replace("_", " ").capitalize()

    def body_xml(self, visual_group: int, collision_group: int) -> str:
        """A fixed body (no freejoint) holding every geom.

        NOT nested under the "apartment" body: that one carries
        quat="0.7071068 0.7071068 0 0" to stand Y-up scan meshes upright, and
        these geoms are already authored z-up.
        """
        lines = []
        for i, g in enumerate(self.geoms):
            name = f"{self.name}_{g.name or 'geom'}_{i}"
            lines.append(g.xml(name, collision_group if g.collide else visual_group))
        body = "\n".join(lines)
        return f'\n    <body name="room_{self.name}" pos="0 0 0">\n{body}\n    </body>'


def load_rooms(roots: list[Path]) -> dict[str, Room]:
    """Every room under `roots`, later roots overriding earlier ones by name.
    A broken sidecar is skipped with a warning -- one bad room must not take
    out the world."""
    return load_sidecars(roots, "sim_room", "ROOM", "rooms", key=lambda r: r.name, set_root=True)


class RoomRegistry:
    """The static rooms in one world."""

    def __init__(self, rooms: dict[str, Room]):
        self.rooms = rooms

    @classmethod
    def load(cls, roots: list[Path]) -> "RoomRegistry":
        registry = cls(load_rooms(roots))
        if registry.rooms:
            counts = ", ".join(f"{n} ({len(r.geoms)} geoms)" for n, r in registry.rooms.items())
            print(f"[rooms] loaded: {counts}", flush=True)
        return registry

    def __bool__(self) -> bool:
        return bool(self.rooms)

    def bodies_xml(self, visual_group: int, collision_group: int) -> str:
        return "".join(r.body_xml(visual_group, collision_group) for r in self.rooms.values())

    def spawn(self) -> tuple[float, float, float] | None:
        """The first room that names a spawn wins; None leaves the world default."""
        for room in self.rooms.values():
            if room.spawn is not None:
                return room.spawn
        return None

    def manifest(self) -> list[dict]:
        """What a browser needs to draw these rooms.

        The observer roster carries props and challenges but nothing about the
        world, because the world has always been a mesh the viewer loads from
        the asset bundle. A room built from primitives ships no mesh, so the
        3D view renders an empty white box and the map looks like it failed to
        load. The viewer already builds primitives for props whose glb is
        missing (see sim/viewer/src/props.ts), so it needs the same shapes here
        -- sent once per connection, like the rest of the roster.
        """
        return [
            {
                "name": room.name,
                "title": room.title,
                "geoms": [
                    {
                        "type": g.type,
                        "size": list(g.size),
                        "pos": list(g.pos),
                        "quat": list(g.quat),
                        "rgba": list(g.rgba),
                        "collide": g.collide,
                    }
                    for g in room.geoms
                ],
            }
            for room in self.rooms.values()
        ]
