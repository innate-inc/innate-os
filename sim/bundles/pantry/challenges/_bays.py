"""Shared geometry for the Pantry stocktake ladder.

Underscore-prefixed so load_challenges skips it. The four levels share one room
and one set of stock, and differ only in what is asked; a copy of these
coordinates per file would drift, and drifted coordinates are how a challenge
quietly starts asking for something that is not there.

COORDINATES are MuJoCo, converted by the porter as (x, y, z)_rbx -> (x, -z).

    +y is NORTH (bay B), +x is EAST (bay C).

              x=-2.5                        x=+2.5
      y=1.8   +--------- BAY B (cobalt) -------+
              |          cartons               |
              | BAY A                    BAY C |
              | amber                   forest |
              | jars      <- aisle ->     tins |
              |                                |
      y=-1.8  +------- door / delivery --------+

EVERY STOCK POSITION IS ON THE FRONT LIP of its deck, 5 cm in, not at the
centre. A deck is 0.32 m deep, so its centre is 0.16 m back; add the planner's
0.145 m obstacle inflation and an item at the centre is 0.305 m from the
nearest cell the robot can occupy, against an arm that reaches 0.29 m. These
numbers were checked against the harness's reach constants and the nav grid,
not by eye.
"""

# Front-of-deck coordinate for each bay: the fixed axis the stock sits on.
BAY_A_X = -2.24  # west wall, jars
BAY_B_Y = 1.54  # north wall, cartons
BAY_C_X = 2.24  # east wall, tins

# --- bay A: jars, on decks 1 and 2 ---------------------------------------
# Spread far enough along a 2.2 m bay that a single 70-degree frame from the
# aisle cannot hold all four. Counting them requires more than one look, which
# is the entire point of a counting challenge.
JAR_HONEY = (BAY_A_X, 0.80)
JAR_JAM = (BAY_A_X, 0.25)
JAR_PICK = (BAY_A_X, -0.30)
JAR_CURD = (BAY_A_X, -0.85)

# --- bay B: cartons, plus the one that does not belong -------------------
CARTON_OATS = (-0.75, BAY_B_Y)
CARTON_RICE = (-0.25, BAY_B_Y)
CARTON_TEA = (0.75, BAY_B_Y)
# A jar standing in the carton bay. Nothing about the jar is wrong; what is
# wrong is where it is. That makes it the only task in this map that cannot be
# done by looking at one object -- the robot has to compare the object against
# the bay it is standing in.
JAR_STRAY = (0.25, BAY_B_Y)

# --- bay C: tins ----------------------------------------------------------
TIN_LARGE = (BAY_C_X, 0.55)
TIN_SMALL = (BAY_C_X, -0.55)

# --- delivery bench by the door -------------------------------------------
# Moved west of the door: the first layout put a 0.46 m deep bench 12 cm from
# the start pad, so the robot spawned pressed against it and its first frame
# was the inside of a carton. Spawn clearance is worth checking on every map,
# and checking it means looking from the pad at 0.25 m.
CARTON_NEW = (-1.65, -1.16)
JAR_NEW = (-1.35, -1.16)

# Where a thing counts as "put away" in each bay: the bay as a RECT covering
# the whole rack shelf, with a height floor so the ground never counts. The
# first version used a 0.75-0.85 m circle at each bay's middle, which covers
# barely a third of each rack: a probe agent shelved the delivery carton
# "immediately beside the tea carton" -- the literal brief, on the carton
# shelf -- and scored 0 because the tea carton sits at x=0.75 and the circle
# ended at 0.85. "With the other cartons" means the carton bay, so the goal
# is the bay. (x0, y0, x1, y1).
PUT_A_RECT = (BAY_A_X - 0.25, -1.15, BAY_A_X + 0.25, 1.10)  # jar bay, west wall
PUT_B_RECT = (-2.3, BAY_B_Y - 0.25, 2.3, BAY_B_Y + 0.25)  # carton bay, north wall
