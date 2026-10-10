"""A small contemporary gallery, shared by physics and every camera.

The central floor stays open for the can circuit and floor pickup. Artwork is
built from shallow coloured shapes inside frames: it is part of the room in
both MuJoCo and the browser, with no camera-only decoration or external assets.
"""

import math

from mars_sim_driver.statics import Geom, Room

CREAM = (0.86, 0.83, 0.76, 1.0)
OAK = (0.49, 0.32, 0.19, 1.0)
CHARCOAL = (0.095, 0.11, 0.12, 1.0)
PAPER = (0.93, 0.88, 0.77, 1.0)
GEOMS = []


def box(name, half, pos, color, *, collide=True, quat=(1.0, 0.0, 0.0, 0.0)):
    GEOMS.append(Geom("box", half, pos, rgba=color, name=name, collide=collide, quat=quat))


# Warm oak floor, pale plaster, and a dark green accent wall. All seams are
# raised decorative surfaces, clear of the floor to avoid coplanar flicker.
box("floor", (4.5, 4.5, 0.05), (0, 0, -0.05), (0.57, 0.42, 0.28, 1))
for i in range(30):
    tone = (0.54 + (i % 4) * 0.015, 0.39 + (i % 4) * 0.014, 0.25 + (i % 4) * 0.012, 1)
    box(f"oak_board_{i}", (0.148, 4.42, 0.0008), (-4.35 + i * 0.3, 0, 0.0008), tone, collide=False)
for side, half, pos, color in (
    ("east", (0.075, 4.5, 1.2), (4.5, 0, 1.2), CREAM),
    ("west", (0.075, 4.5, 1.2), (-4.5, 0, 1.2), CREAM),
    ("north", (4.5, 0.075, 1.2), (0, 4.5, 1.2), CREAM),
    ("south", (4.5, 0.075, 1.2), (0, -4.5, 1.2), (0.16, 0.24, 0.23, 1)),
):
    box(f"wall_{side}", half, pos, color)
    trim_half = (half[0], half[1], 0.045)
    trim_pos = (pos[0] * 0.981, pos[1] * 0.981, 0.045)
    box(f"skirting_{side}", trim_half, trim_pos, CHARCOAL, collide=False)
box("ceiling", (4.5, 4.5, 0.025), (0, 0, 2.425), (0.92, 0.90, 0.85, 1), collide=False)


# The thin green starting pad remains an unambiguous delivery destination.
box("startpad_edge", (0.34, 0.34, 0.002), (0, 0, 0.005), (0.16, 0.25, 0.20, 1), collide=False)
box("startpad", (0.30, 0.30, 0.002), (0, 0, 0.01), (0.24, 0.64, 0.37, 1), collide=False)

# Four ascending plinths. The low mug at (-3, 3.2) deliberately has NO plinth:
# its task asks for a floor pickup and the live pickup skill assumes the floor.
for i, (x, height) in enumerate(((-1.5, 0.10), (0, 0.20), (1.5, 0.30), (3, 0.50))):
    box(f"plinth_{i}", (0.17, 0.17, height / 2), (x, 3.2, height / 2), (0.76, 0.74, 0.69, 1))
    box(f"plinth_cap_{i}", (0.19, 0.19, 0.01), (x, 3.2, height - 0.01), (0.92, 0.90, 0.83, 1))

# A quiet bench against the back wall, outside the ring and all approach paths.
box("bench_seat", (0.9, 0.22, 0.035), (0, -3.8, 0.36), OAK)
for x in (-0.7, 0.7):
    box("bench_leg", (0.035, 0.18, 0.16), (x, -3.8, 0.16), CHARCOAL)


def painting(wall, along, design):
    """Local u/v canvas coordinates mapped onto each inward-facing wall."""
    phi = {"south": 0, "east": math.pi / 2, "north": math.pi, "west": -math.pi / 2}[wall]
    tx, ty = math.cos(phi), math.sin(phi)
    nx, ny = -ty, tx
    center = (tx * along - nx * 4.425, ty * along - ny * 4.425)
    rotation = (math.cos(phi / 2), 0, 0, math.sin(phi / 2))
    tag = f"painting_{wall}_{along:g}"

    def at(u, v, depth):
        return (center[0] + tx * u + nx * depth, center[1] + ty * u + ny * depth, 1.18 + v)

    def rect(label, u, v, w, h, color, depth=0.073):
        box(f"{tag}_{label}", (w / 2, 0.001, h / 2), at(u, v, depth), color, collide=False, quat=rotation)

    def disc(label, u, v, radius, color, depth=0.077):
        q = math.sqrt(0.5)
        rotation_disc = (math.cos(phi / 2) * q, -math.cos(phi / 2) * q, -math.sin(phi / 2) * q, math.sin(phi / 2) * q)
        GEOMS.append(Geom("cylinder", (radius, 0.001), at(u, v, depth), quat=rotation_disc, rgba=color, name=f"{tag}_{label}", collide=False))

    box(f"{tag}_frame", (0.75, 0.024, 0.57), at(0, 0, 0.036), CHARCOAL, collide=False, quat=rotation)
    rect("mat", 0, 0, 1.42, 1.06, PAPER, 0.063)
    rect("canvas", 0, 0, 1.25, 0.89, (0.77, 0.79, 0.73, 1), 0.067)
    if design == 0:  # Sunset, layered coast and an ochre sun.
        rect("sky", 0, 0.12, 1.25, 0.65, (0.79, 0.53, 0.36, 1))
        disc("sun", 0.24, 0.18, 0.16, (0.98, 0.79, 0.39, 1))
        rect("sea", 0, -0.20, 1.25, 0.25, (0.23, 0.40, 0.45, 1), 0.081)
        rect("shore", 0, -0.375, 1.25, 0.14, (0.83, 0.74, 0.54, 1), 0.085)
        rect("reflection", 0.22, -0.20, 0.22, 0.015, (0.92, 0.75, 0.44, 1), 0.089)
    elif design == 1:  # Blue-circle colour study.
        rect("field", -0.24, 0, 0.42, 0.89, (0.21, 0.36, 0.48, 1))
        disc("blue_circle", 0.14, 0.04, 0.29, (0.11, 0.28, 0.45, 1))
        disc("ochre_circle", -0.24, -0.19, 0.12, (0.84, 0.56, 0.24, 1), 0.081)
        rect("line", 0.31, 0.03, 0.025, 0.72, PAPER, 0.085)
    elif design == 2:  # Three stylised stems on a warm ground.
        rect("ground", 0, 0, 1.25, 0.89, (0.86, 0.76, 0.59, 1))
        for i, u in enumerate((-0.34, 0, 0.34)):
            rect(f"stem_{i}", u, -0.12, 0.023, 0.55, (0.24, 0.33, 0.23, 1), 0.077)
            disc(f"leaf_{i}", u + 0.06, 0.04 + 0.12 * (i % 2), 0.115, (0.31, 0.43, 0.28, 1), 0.081)
            disc(f"flower_{i}", u - 0.04, 0.21, 0.095, (0.67, 0.32, 0.27, 1), 0.085)
    else:  # Balanced blocks, inspired by architectural facades.
        rect("ochre", -0.27, 0.12, 0.43, 0.61, (0.79, 0.50, 0.23, 1))
        rect("ink", 0.24, -0.11, 0.41, 0.57, (0.20, 0.28, 0.31, 1), 0.077)
        disc("terracotta", 0.20, 0.24, 0.14, (0.64, 0.28, 0.22, 1), 0.081)
        rect("baseline", 0, -0.34, 1.03, 0.025, PAPER, 0.085)
    # Small artwork label and a brass picture light: both visible, neither a
    # task answer. Every layer is separated in depth, including the frame.
    rect("caption", 0.55, -0.67, 0.28, 0.08, PAPER, 0.04)
    box(f"{tag}_light", (0.26, 0.05, 0.022), at(0, 0.70, 0.10), (0.53, 0.43, 0.25, 1), collide=False, quat=rotation)


for i, wall in enumerate(("south", "east", "north", "west")):
    for j, along in enumerate((-2.15, 2.15)):
        painting(wall, along, (i + j) % 4)

for x in (-3.8, 3.8):
    GEOMS.append(Geom("cylinder", (0.17, 0.16), (x, -3.8, 0.16), rgba=(0.63, 0.36, 0.24, 1), name="planter"))
    GEOMS.append(Geom("cylinder", (0.14, 0.012), (x, -3.8, 0.32), rgba=(0.15, 0.12, 0.08, 1), name="soil", collide=False))
    GEOMS.append(Geom("cylinder", (0.018, 0.28), (x, -3.8, 0.58), rgba=OAK, name="plant_stem", collide=False))
    for dx, dy, z, radius in ((0, 0, 0.92, 0.23), (0.13, 0.04, 0.72, 0.18), (-0.12, -0.03, 0.79, 0.19)):
        GEOMS.append(Geom("sphere", (radius,), (x + dx, -3.8 + dy, z), rgba=(0.21, 0.34, 0.23, 1), name="plant_foliage", collide=False))

ROOM = Room(name="gallery", title="Gallery", spawn=(0.0, 0.0, 90.0), geoms=GEOMS)
