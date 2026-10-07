"""Authored finishes and landmarks for the primitive benchmark rooms.

These are ordinary room geoms, shared by the robot camera and browser. Details
are non-colliding, except the doors that close doorways which opened onto
nothing; task routes and furniture support surfaces stay explicit in the room
sidecars. Thin layers have separate depths to avoid z-fighting.
"""

import math
from dataclasses import replace

from .statics import Geom

INK = (0.09, 0.13, 0.15, 1)
CREAM = (0.86, 0.81, 0.70, 1)
WHITE = (0.92, 0.91, 0.85, 1)
OAK = (0.42, 0.25, 0.12, 1)
TEAL = (0.12, 0.31, 0.30, 1)
BRASS = (0.64, 0.43, 0.17, 1)


def box(room, name, half, pos, color, quat=(1, 0, 0, 0)):
    room.geoms.append(Geom("box", half, pos, rgba=color, name=name, quat=quat, collide=False))


def disc(room, name, radius, pos, color, half=0.003, quat=(1, 0, 0, 0)):
    room.geoms.append(Geom("cylinder", (radius, half), pos, rgba=color, name=name, quat=quat, collide=False))


def pad(room, name, x, y, radius, color):
    disc(room, name + "_border", radius, (x, y, 0.003), INK, half=0.001)
    disc(room, name, radius - 0.025, (x, y, 0.006), color, half=0.001)


class Panel:
    """A wall panel whose local x/z axes are horizontal/vertical."""

    def __init__(self, room, name, pos, width, height, color=CREAM, yaw=0):
        self.room, self.name, self.pos = room, name, pos
        self.angle = math.radians(yaw)
        self.quat = (math.cos(self.angle / 2), 0, 0, math.sin(self.angle / 2))
        self.rect("frame", 0, 0, width, height, INK, 0)
        self.rect("paper", 0, 0, width - 0.035, height - 0.035, color, 0.007)

    def at(self, u, v, depth):
        c, s = math.cos(self.angle), math.sin(self.angle)
        return (self.pos[0] + c * u - s * depth, self.pos[1] + s * u + c * depth, self.pos[2] + v)

    def rect(self, name, x, z, w, h, color, depth=0.015):
        box(self.room, self.name + "_" + name, (w / 2, 0.002, h / 2), self.at(x, z, depth), color, self.quat)

    def circle(self, name, x, z, radius, color, depth=0.021):
        q, c, s = math.sqrt(0.5), math.cos(self.angle / 2), math.sin(self.angle / 2)
        disc(
            self.room,
            self.name + "_" + name,
            radius,
            self.at(x, z, depth),
            color,
            half=0.002,
            quat=(c * q, -c * q, -s * q, s * q),
        )

    def artwork(self):
        self.circle("sun", 0.12, 0.10, 0.12, BRASS)
        self.rect("hill", -0.13, -0.10, 0.23, 0.24, TEAL, 0.027)
        self.rect("sea", 0.05, -0.20, 0.42, 0.07, (0.18, 0.36, 0.48, 1), 0.033)


def recolor(room, walls, floor):
    for geom in room.geoms:
        if geom.name.startswith(
            ("wall", "perim", "corr", "divider", "roomn", "lobby", "ende", "endw", "halln", "halls")
        ):
            geom.rgba = walls
        if geom.name.startswith("floor"):
            geom.rgba = floor


def rounds(room):
    recolor(room, (0.78, 0.79, 0.73, 1), (0.44, 0.32, 0.20, 1))
    room.geoms[:] = [g for g in room.geoms if not g.name.startswith("seam")]
    # A woven corridor runner stops before the lobby entrance.
    box(room, "runner_edge", (5.85, 0.48, 0.001), (0, 0, 0.006), INK)
    box(room, "runner", (5.85, 0.43, 0.001), (0, 0, 0.010), (0.20, 0.29, 0.29, 1))
    for x in (-4.5, -1.5, 1.5, 4.5):
        box(room, "runner_stitch", (0.01, 0.39, 0.001), (x, 0, 0.014), BRASS)
    for x in (-4.2, -1.8, 0.7):
        Panel(room, "corridor_art", (x, 0.724, 1.25), 0.66, 0.70, yaw=180).artwork()
        box(room, "picture_light", (0.19, 0.045, 0.02), (x, 0.68, 1.70), BRASS)
    # Visible books fill the reading-room shelves, away from the pickup book.
    for level in (0.32, 0.64, 0.96):
        for i in range(7):
            color = (TEAL, BRASS, (0.43, 0.16, 0.14, 1))[i % 3]
            box(room, "shelved_book", (0.032, 0.07, 0.095), (-4.94 + i * 0.14, -3.45, level + 0.095), color)
    Panel(room, "bedroom_art", (-1.45, -3.72, 1.23), 0.70, 0.70).artwork()
    # Cabinet drawer fronts and handles make the green room read as a kitchen.
    for x in (0.8, 1.5, 2.2):
        box(room, "cabinet_front", (0.31, 0.004, 0.30), (x, -3.091, 0.44), TEAL)
        box(room, "cabinet_handle", (0.09, 0.012, 0.013), (x, -3.078, 0.65), BRASS)
    for x in (1.15, 1.55):
        disc(room, "hob", 0.10, (x, -3.39, 0.883), INK)
    # Bathroom mirror, basin inset and seat opening are set on visible faces.
    Panel(room, "bathroom_mirror", (4.0, -3.724, 1.20), 0.62, 0.58, (0.49, 0.65, 0.67, 1))
    box(room, "basin_well", (0.18, 0.14, 0.002), (4, -3.4, 0.823), (0.39, 0.55, 0.57, 1))
    box(room, "toilet_opening", (0.12, 0.15, 0.001), (5.4, -3.46, 0.404), INK)
    # Blue, as the brief calls it. It was teal, the same family as the
    # corridor runner, beside a door that is a saturated blue.
    pad(room, "book_delivery_mat", 4.0, 2.5, 0.43, (0.16, 0.36, 0.72, 1))
    Panel(room, "lobby_art", (4, 3.524, 1.52), 0.72, 0.70, yaw=180).artwork()
    # A ceiling on the 2.4 m wall tops, so the robot's camera sees a building
    # rather than a black band above every wall. Named "ceiling" because the
    # browser's overhead view leaves exactly that out.
    box(room, "ceiling", (6.0, 3.7, 0.025), (0, -0.1, 2.425), (0.93, 0.91, 0.86, 1))


def workshop(room):
    recolor(room, (0.65, 0.69, 0.66, 1), (0.26, 0.29, 0.29, 1))
    for g in room.geoms:
        if g.name in ("leg", "rail", "upright"):
            g.rgba = INK
        if g.name == "top":
            g.rgba = OAK
    for x in (-3, -1.5, 0, 1.5, 3):
        panel = Panel(room, "tool_board", (x, 2.924, 1.08), 1.04, 0.85, (0.43, 0.28, 0.14, 1), yaw=180)
        # Pegboard tools: hammer, spanner and screwdriver silhouettes.
        for u in (-0.31, 0, 0.31):
            panel.rect("tool_handle", u, -0.03, 0.045, 0.40, INK)
        panel.rect("hammer_head", -0.31, 0.19, 0.25, 0.09, WHITE, 0.023)
        panel.circle("spanner_head", 0, 0.19, 0.085, WHITE)
        panel.circle("spanner_open", 0, 0.22, 0.038, (0.43, 0.28, 0.14, 1), 0.028)
        panel.rect("screwdriver_grip", 0.31, -0.15, 0.085, 0.18, BRASS, 0.023)
        box(room, "bench_edge", (0.43, 0.011, 0.008), (x, 2.081, (x + 3) / 1.5 * 0.06 + 0.05), BRASS)
    # Wall storage, beyond the search route, with clear crate straps.
    for g in list(room.geoms):
        if g.name == "siden" and g.pos[1] < 0:
            box(room, "crate_band", (0.018, 0.002, 0.07), (g.pos[0], g.pos[1] + 0.012, g.pos[2]), INK, g.quat)
    for y in (-2.65, -1.05):
        box(room, "ramp_boundary", (0.65, 0.015, 0.001), (3.2, y, 0.003), BRASS)
    # Ceiling on the 2.4 m wall tops; see rounds().
    box(room, "ceiling", (4.0, 3.0, 0.025), (0, 0, 2.425), (0.93, 0.91, 0.86, 1))


def pantry(room):
    # Extend the grocery finish with category pictograms, never count answers.
    for name, pos, yaw, kind in (
        ("jar_sign", (-2.18, 0, 0.66), -90, "jar"),
        ("box_sign_back", (0, 1.48, 0.66), 180, "box"),
        ("box_sign_right", (2.18, 0, 0.66), 90, "box"),
    ):
        sign = Panel(room, name, pos, 0.52, 0.29, CREAM, yaw)
        sign.rect("body", 0, -0.02, 0.15, 0.13, TEAL)
        if kind == "jar":
            sign.rect("lid", 0, 0.066, 0.13, 0.027, BRASS, 0.023)
        else:
            sign.rect("tape", 0, -0.02, 0.025, 0.13, BRASS, 0.023)
    pad(room, "jar_sorting_pad", -1.68, 0.15, 0.25, (0.76, 0.48, 0.16, 1))
    pad(room, "box_sorting_pad", 0.0, 1.12, 0.25, (0.17, 0.36, 0.60, 1))
    # Slate, not cream: the delivery carton starts on this pad, and a cream
    # carton on a cream mat was hard to make out from the spawn.
    pad(room, "delivery_pad", -1.18, -0.83, 0.24, (0.33, 0.37, 0.37, 1))
    # A closed door in the south doorway. It was an opening onto unrendered
    # black, right where the stocktake sends the robot back to, and nothing
    # stopped a robot driving out of the world through it.
    _door(room, 0.0, -1.8, 0.4, 1.3)
    # Shelf price rails and a back-wall display give the room a shop identity.
    for x in (-0.9, -0.3, 0.3, 0.9):
        box(room, "shelf_ticket", (0.055, 0.003, 0.025), (x, 1.479, 0.10), WHITE)


def _door(room, x, y, half_width, wall_top):
    """A closed, collidable door filling a doorway in an x-running wall at y,
    with a lintel up to the wall top. The room side is +y."""
    room.geoms.append(Geom("box", (half_width, 0.03, 0.55), (x, y, 0.55), rgba=OAK, name="door"))
    box(room, "door_lintel", (half_width, 0.06, (wall_top - 1.1) / 2), (x, y, (wall_top + 1.1) / 2), WHITE)
    box(room, "door_panel", (half_width - 0.08, 0.002, 0.40), (x, y + 0.032, 0.58), (0.36, 0.21, 0.10, 1))
    box(room, "door_handle", (0.012, 0.010, 0.055), (x + half_width - 0.10, y + 0.042, 0.55), BRASS)


def counter(room):
    # Replace the original square outlines with one clear delivery marker.
    room.geoms[:] = [g for g in room.geoms if g.name not in {"edge", "spot"}]
    recolor(room, (0.79, 0.75, 0.66, 1), (0.37, 0.22, 0.12, 1))
    # Warm white wainscot, not green: the green cup stands on the pass in
    # front of the north wainscot, and green on green made a 5 px cup easy to
    # miss in "how many cups are on the counter". The south run stops at the
    # doorway now that it has a door.
    split = []
    for g in room.geoms:
        if g.name in ("wainn", "wains", "waine", "wainw"):
            g.rgba = WHITE
        if g.name in ("wains", "wainsr"):
            for x0, x1 in ((g.pos[0] - g.size[0], -0.5), (0.5, g.pos[0] + g.size[0])):
                split.append(replace(g, size=((x1 - x0) / 2, *g.size[1:]), pos=((x0 + x1) / 2, *g.pos[1:])))
    room.geoms[:] = [g for g in room.geoms if g.name not in ("wains", "wainsr")] + split
    _door(room, 0.0, -1.8, 0.5, 1.2)
    # Floor board seams topped out at 4 mm, level with the mat borders, and
    # flickered where they crossed. 2 mm keeps them under every mat.
    for g in room.geoms:
        if g.name == "board":
            g.size, g.pos = (*g.size[:2], 0.001), (*g.pos[:2], 0.001)
    # Fluted oak front and a dark stone top, at the original support height.
    for g in room.geoms:
        if g.name == "top" and abs(g.pos[0]) < 0.01:
            g.rgba = (0.12, 0.16, 0.16, 1)
    for i in range(23):
        box(room, "counter_flute", (0.018, 0.008, 0.087), (-1.10 + i * 0.10, 1.241, 0.11), OAK)
    for x in (-0.9, 0, 0.9):
        pad(room, "seat_delivery_mat", x, 0.22, 0.29, CREAM)
    pad(room, "counter_return_mat", 0, 1.02, 0.19, (0.24, 0.56, 0.39, 1))
    pad(room, "stock_return_mat", -1.65, -0.30, 0.23, (0.76, 0.48, 0.16, 1))
    for x in (-1.56, 1.56):
        panel = Panel(room, "cafe_print", (x, 1.72, 0.84), 0.47, 0.59, CREAM, yaw=180)
        panel.circle("coffee_bean", 0, 0.05, 0.10, OAK)
        panel.rect("bean_seam", 0, 0.05, 0.013, 0.16, CREAM, 0.027)
        panel.rect("caption", 0, -0.15, 0.23, 0.014, OAK)


def bridge(room):
    recolor(room, (0.66, 0.74, 0.71, 1), (0.21, 0.25, 0.28, 1))
    for g in room.geoms:
        if g.name.startswith(("side", "back", "front")):
            g.rgba = (0.53, 0.64, 0.62, 1)
        if g.name.startswith("gate"):
            g.rgba = TEAL
        if g.name.startswith("pad"):
            g.rgba = (0.33, 0.37, 0.37, 1)
    for y in (-1.8, -0.3, 1.2, 2.7, 4.2):
        box(room, "gate_header", (0.84, 0.06, 0.045), (0, y, 1.22), TEAL)
        # Symmetric entry trim: no indication of which side is correct.
        for x in (-0.82, -0.205, 0.205, 0.82):
            box(room, "gate_trim", (0.008, 0.011, 0.53), (x, y - 0.083, 0.61), BRASS)
        for x in (-0.52, 0.52):
            box(room, "threshold", (0.27, 0.09, 0.001), (x, y, 0.011), BRASS)
        box(room, "gate_light", (0.18, 0.025, 0.012), (0, y - 0.085, 1.16), WHITE)
    for x in (-0.825, 0.825):
        box(room, "continuous_guide", (0.004, 5.70, 0.010), (x, 2.1, 0.90), WHITE)


def blaze(room):
    recolor(room, (0.76, 0.72, 0.64, 1), (0.42, 0.29, 0.17, 1))
    # THE ONLY WAY OUT HAS TO LOOK OPEN. The store's green band and the south
    # skirting both ran straight across the store/porch doorway (x -2.81 to
    # -2.09): non-colliding, but on camera a green bar and a 10 cm sill across
    # the exit the brief sends the robot through, for a robot that cannot
    # climb. Both now stop at the door jambs.
    door_x0, door_x1 = -2.81, -2.09
    pieces = []
    for g in room.geoms:
        if g.name == "band_store":
            x1 = g.pos[0] + g.size[0]
            pieces.append(replace(g, size=((x1 - door_x1) / 2, *g.size[1:]), pos=((x1 + door_x1) / 2, *g.pos[1:])))
        if g.name == "sks":
            x0, x1 = g.pos[0] - g.size[0], g.pos[0] + g.size[0]
            for a, b in ((x0, door_x0), (door_x1, x1)):
                pieces.append(replace(g, size=((b - a) / 2, *g.size[1:]), pos=((a + b) / 2, *g.pos[1:])))
    room.geoms[:] = [g for g in room.geoms if g.name not in ("band_store", "sks")] + pieces
    # The existing coloured bands identify rooms. Complete the furniture so
    # a kitchen, study and bedroom can also be identified by their contents.
    # Fronts at y=1.766, clear of the rails' faces at 1.770 they used to
    # share and flicker against.
    for x in (-2.40, -1.90, -1.40):
        box(room, "kitchen_front", (0.22, 0.003, 0.078), (x, 1.766, 0.12), TEAL)
        box(room, "kitchen_handle", (0.065, 0.006, 0.008), (x, 1.757, 0.16), BRASS)
    for x in (-2.2, -1.9):
        disc(room, "kitchen_hob", 0.075, (x, 2.0, 0.244), INK)
    Panel(room, "kitchen_window", (-1.6, 2.222, 0.87), 0.72, 0.50, (0.43, 0.61, 0.66, 1), 180)
    box(room, "monitor_base", (0.12, 0.08, 0.01), (1.85, 2.02, 0.25), INK)
    box(room, "monitor_stem", (0.015, 0.015, 0.10), (1.85, 2.06, 0.35), INK)
    Panel(room, "study_monitor", (1.85, 2.06, 0.47), 0.42, 0.28, (0.17, 0.30, 0.40, 1), 180)
    Panel(room, "bedroom_art", (2.35, -2.222, 0.89), 0.56, 0.55).artwork()
    # A framed green exit pictogram on the wall beside the store/porch
    # doorway, arrow toward it. It used to hang in the doorway itself, which
    # has no lintel, so it floated in the opening. On a yaw-0 panel +u is
    # world +x, which a robot facing the wall sees on its LEFT; the door is
    # to the west, so the arrow runs toward -u.
    sign = Panel(room, "exit_sign", (-1.80, -2.222, 1.03), 0.48, 0.24, TEAL)
    sign.rect("door", 0.08, 0, 0.095, 0.15, WHITE)
    sign.rect("arrow_shaft", -0.09, 0, 0.15, 0.025, WHITE)
    sign.rect("arrow_top", -0.15, 0.035, 0.025, 0.09, WHITE)
    for x in (-2.45, -1.15):
        disc(room, "exit_waymarker", 0.06, (x, -1.9 if x < -2 else -0.95, 0.008), (0.25, 0.64, 0.37, 1))
    # Closed alarm boxes on walls, away from task props and all door openings.
    for x, y in ((-0.72, 2.22), (2.75, 2.22), (2.75, -2.22)):
        box(room, "fire_alarm", (0.06, 0.013, 0.09), (x, y, 0.91), (0.68, 0.06, 0.04, 1))
    # Ceiling over the house on the 1.3 m wall tops; the porch stays open.
    box(room, "ceiling", (3.2, 2.3, 0.025), (0, 0, 1.325), (0.93, 0.91, 0.86, 1))
    # NO BACKDROP PAST THE PORCH, though the way out still opens onto black.
    # A daylight panel there was tried: the map exporter's virtual lidar sees
    # it through the doorway, the building envelope grows to reach it, and
    # the nav map gained 312 free cells off a porch that drops 0.5 m to the
    # ground plane.


def _floor(room, name, x0, y0, x1, y1, color, z=0.0025):
    """A room-sized floor finish between its own walls, under every rug and mat."""
    box(room, name, ((x1 - x0) / 2, (y1 - y0) / 2, 0.001), ((x0 + x1) / 2, (y0 + y1) / 2, z), color)


def _grout(room, name, x0, y0, x1, y1, step, color):
    """Tile joints over a floor finish, one depth layer above it."""
    x = x0 + step
    while x < x1 - 0.01:
        box(room, name, (0.004, (y1 - y0) / 2, 0.0005), (x, (y0 + y1) / 2, 0.0040), color)
        x += step
    y = y0 + step
    while y < y1 - 0.01:
        box(room, name, ((x1 - x0) / 2, 0.004, 0.0005), ((x0 + x1) / 2, y, 0.0040), color)
        y += step


def household(room):
    # Every wall shared one grey and every floor one beige tile, so the four
    # rooms could be told apart only by their furniture -- from the robot's
    # camera at the spawn, a grey slab, a black sky and an orange post. The
    # tour challenge asks for the rooms BY NAME, so each one gets a floor, a
    # wall feature and furniture detail of its own. All of it is
    # non-colliding and clear of every route and door opening.
    recolor(room, (0.84, 0.81, 0.75, 1), (0.52, 0.47, 0.40, 1))
    room.geoms[:] = [g for g in room.geoms if not g.name.startswith("seam")]
    for g in room.geoms:
        # The sofa and duvet were the exact blue of the mug the robot is sent
        # to find; a fabric green and an indigo keep "the blue mug" unique.
        if g.name in ("seat", "back", "cush", "arm") and g.pos[1] > 2.0:
            g.rgba = (0.33, 0.45, 0.42, 1)
        if g.name == "duvet":
            g.rgba = (0.30, 0.33, 0.52, 1)
        if g.name == "top" and g.pos[0] > 0:  # kitchen worktops, not the bedside
            g.rgba = (0.17, 0.19, 0.21, 1)

    # Floors, each confined to its own room between the walls.
    _floor(room, "living_floor", -4.44, 0.06, -0.06, 3.44, (0.50, 0.35, 0.21, 1))
    _floor(room, "kitchen_floor", 0.06, 0.06, 3.94, 3.44, (0.75, 0.73, 0.67, 1))
    _grout(room, "kitchen_grout", 0.06, 0.06, 3.94, 3.44, 0.6, (0.58, 0.56, 0.51, 1))
    _floor(room, "bedroom_floor", -4.44, -3.94, -0.56, -0.06, (0.53, 0.48, 0.56, 1))
    _floor(room, "bathroom_floor", -0.44, -2.14, 1.74, -0.06, (0.72, 0.82, 0.82, 1))
    _grout(room, "bathroom_grout", -0.44, -2.14, 1.74, -0.06, 0.4, (0.55, 0.66, 0.67, 1))
    _floor(room, "utility_floor", 1.86, -3.94, 3.94, -0.06, (0.49, 0.49, 0.47, 1))
    _floor(room, "utility_floor", -0.44, -3.94, 1.74, -2.26, (0.49, 0.49, 0.47, 1))

    # A ceiling, so the robot's camera sees a room rather than black sky. Named
    # "ceiling" because the browser's overhead view leaves exactly that out.
    box(room, "ceiling", (4.25, 3.75, 0.025), (-0.25, -0.25, 2.43), (0.93, 0.91, 0.86, 1))

    # Living room: art over the sofa, a rug in front of it, a screen facing it.
    Panel(room, "living_art", (-3.4, 3.424, 1.42), 1.05, 0.72, yaw=180).artwork()
    box(room, "picture_light", (0.22, 0.045, 0.02), (-3.4, 3.38, 1.86), BRASS)
    box(room, "living_rug_border", (0.85, 0.50, 0.001), (-3.4, 1.65, 0.0045), INK)
    box(room, "living_rug", (0.80, 0.45, 0.001), (-3.4, 1.65, 0.0065), (0.62, 0.30, 0.22, 1))
    Panel(room, "living_tv", (-3.7, 0.076, 1.12), 1.0, 0.58, (0.10, 0.12, 0.14, 1))
    # The fetch's delivery mat, in front of where Casey stands. The station's
    # own pad and post sit north of it (from y=2.325), untouched. Every point
    # of the mat must satisfy the judge, which wants the mug within 1.0 m of
    # Casey at (-1.3, 2.95) and 0.45 m of (-1.3, 2.2): at 0.20 m and y=2.1 the
    # south rim reached 1.05 m from Casey, so a mug set down on the mat failed.
    pad(room, "casey_delivery_mat", -1.3, 2.12, 0.17, CREAM)

    # Kitchen: door fronts and handles on both runs of units, a hob, a sink
    # and a window over the worktop.
    for x in (1.1, 1.7, 2.3, 2.9):  # north run, front face at y = 2.8
        box(room, "cabinet_front", (0.27, 0.004, 0.30), (x, 2.794, 0.44), TEAL)
        box(room, "cabinet_handle", (0.09, 0.012, 0.013), (x, 2.781, 0.68), BRASS)
    for y in (1.05, 1.55, 2.05, 2.55):  # east run, front face at x = 3.3
        box(room, "cabinet_front", (0.004, 0.22, 0.30), (3.294, y, 0.44), TEAL)
        box(room, "cabinet_handle", (0.012, 0.09, 0.013), (3.281, y, 0.68), BRASS)
    for x in (2.4, 2.8):
        disc(room, "hob", 0.10, (x, 3.1, 0.883), INK)
    box(room, "sink_well", (0.18, 0.22, 0.002), (3.6, 1.6, 0.883), (0.62, 0.66, 0.68, 1))
    Panel(room, "kitchen_window", (1.6, 3.424, 1.55), 0.90, 0.62, (0.55, 0.70, 0.78, 1), yaw=180)

    # Bathroom: a mirror over the basin (kept off the doorway), the basin
    # well, the toilet opening, and a towel on the east wall.
    Panel(room, "bathroom_mirror", (1.25, -0.076, 1.30), 0.45, 0.62, (0.62, 0.72, 0.75, 1), yaw=180)
    box(room, "basin_well", (0.18, 0.14, 0.002), (1.2, -0.55, 0.823), (0.48, 0.60, 0.63, 1))
    box(room, "toilet_opening", (0.15, 0.11, 0.001), (0.24, -1.7, 0.404), INK)
    box(room, "towel", (0.004, 0.22, 0.30), (1.734, -1.4, 0.95), (0.80, 0.42, 0.36, 1))

    # Bedroom: art over the headboard and a rug beside the bed, clear of the
    # station pad the tour stops on.
    Panel(room, "bedroom_art", (-3.0, -3.924, 1.18), 0.95, 0.62).artwork()
    box(room, "bedroom_rug_border", (0.33, 0.68, 0.001), (-2.05, -2.2, 0.0045), INK)
    box(room, "bedroom_rug", (0.30, 0.65, 0.001), (-2.05, -2.2, 0.0065), (0.40, 0.48, 0.56, 1))
