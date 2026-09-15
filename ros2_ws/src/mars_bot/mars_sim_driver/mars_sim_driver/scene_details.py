"""Authored finishes and landmarks for the primitive benchmark rooms.

These are ordinary room geoms, shared by the robot camera and browser. Details
are non-colliding; task routes and furniture support surfaces stay explicit in
the room sidecars. Thin layers have separate depths to avoid z-fighting.
"""

import math

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
    pad(room, "book_delivery_mat", 4.0, 2.5, 0.43, (0.18, 0.48, 0.54, 1))
    Panel(room, "lobby_art", (4, 3.524, 1.52), 0.72, 0.70, yaw=180).artwork()


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
    pad(room, "delivery_pad", -1.18, -0.83, 0.24, CREAM)
    # Shelf price rails and a back-wall display give the room a shop identity.
    for x in (-0.9, -0.3, 0.3, 0.9):
        box(room, "shelf_ticket", (0.055, 0.003, 0.025), (x, 1.479, 0.10), WHITE)


def counter(room):
    # Replace the original square outlines with one clear delivery marker.
    room.geoms[:] = [g for g in room.geoms if g.name not in {"edge", "spot"}]
    recolor(room, (0.79, 0.75, 0.66, 1), (0.37, 0.22, 0.12, 1))
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
    # The existing coloured bands identify rooms. Complete the furniture so
    # a kitchen, study and bedroom can also be identified by their contents.
    for x in (-2.40, -1.90, -1.40):
        box(room, "kitchen_front", (0.22, 0.003, 0.078), (x, 1.773, 0.12), TEAL)
        box(room, "kitchen_handle", (0.065, 0.006, 0.008), (x, 1.760, 0.16), BRASS)
    for x in (-2.2, -1.9):
        disc(room, "kitchen_hob", 0.075, (x, 2.0, 0.244), INK)
    Panel(room, "kitchen_window", (-1.6, 2.222, 0.87), 0.72, 0.50, (0.43, 0.61, 0.66, 1), 180)
    box(room, "monitor_base", (0.12, 0.08, 0.01), (1.85, 2.02, 0.25), INK)
    box(room, "monitor_stem", (0.015, 0.015, 0.10), (1.85, 2.06, 0.35), INK)
    Panel(room, "study_monitor", (1.85, 2.06, 0.47), 0.42, 0.28, (0.17, 0.30, 0.40, 1), 180)
    Panel(room, "bedroom_art", (2.35, -2.222, 0.89), 0.56, 0.55).artwork()
    # A framed green exit pictogram over the existing store/porch doorway.
    sign = Panel(room, "exit_sign", (-2.45, -2.222, 1.03), 0.48, 0.24, TEAL)
    sign.rect("door", -0.08, 0, 0.095, 0.15, WHITE)
    sign.rect("arrow_shaft", 0.09, 0, 0.15, 0.025, WHITE)
    sign.rect("arrow_top", 0.15, 0.035, 0.025, 0.09, WHITE)
    for x in (-2.45, -1.15):
        disc(room, "exit_waymarker", 0.06, (x, -1.9 if x < -2 else -0.95, 0.008), (0.25, 0.64, 0.37, 1))
    # Closed alarm boxes on walls, away from task props and all door openings.
    for x, y in ((-0.72, 2.22), (2.75, 2.22), (2.75, -2.22)):
        box(room, "fire_alarm", (0.06, 0.013, 0.09), (x, y, 0.91), (0.68, 0.06, 0.04, 1))
