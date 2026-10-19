"""Stock recognition and sorting with jars on the left shelves and boxes on the other shelves.

Movable tasks use floor staging mats; counts are owned by this exact setup."""

import re

from mars_sim_driver.challenges import AllOf, Answered, Challenge, Drop, Goal, Hold, InCircle


class _NamesTheJar(Answered):
    """Answered's rules -- not negated, doubted or asked -- applied to "jar"
    or "jam", after removing the places jars belong: "the jar shelves", "the
    jar sorting mat" and "which jar is misfiled" name no item. Pattern lists
    tried first either failed "A jar is on the floor" or passed "It's not a
    jar, it's a carton"; the answer judge already knows how to tell."""

    _PLACES = re.compile(
        # Whole words: "a jar matching the ones on the shelf" names the item.
        r"\bjars?\s+(?:shel\w*|sort(?:ing)?|bays?|mats?|racks?|sections?|areas?|aisles?|storage)\b"
        r"|\b(?:which|what)\s+(?:\w+\s+)?jars?\b",
        re.IGNORECASE,
    )

    def _says_it(self, text: str) -> bool:
        return super()._says_it(self._PLACES.sub(" ", str(text)))


_NAMES_IT = _NamesTheJar(accept=["jar", "jam"])

CHALLENGE = Challenge(
    id="pantry_misfiled",
    title="Something is in the wrong bay",
    brief="An item has been left on the floor in front of the wrong stock bay. Go over to it and tell me what it is, then move it to the amber floor sorting mat in front of the jar shelves.",
    setup=[
        Drop(name="pantry_jar_stray", x=0.35, y=1.08, z=0.045),
        Drop(name="pantry_carton_oats", x=-0.75, y=1.54),
        Drop(name="pantry_carton_rice", x=-0.25, y=1.54),
        Drop(name="pantry_carton_tea", x=2.24, y=0.55, yaw_deg=-90),
        Drop(name="pantry_jar_honey", x=-2.24, y=0.8),
        Drop(name="pantry_jar_jam", x=-2.24, y=0.25),
    ],
    goals=[
        # An identification, not the word, and made at the item. The accept
        # list held the bare "jar", so "On my way to the jar shelves" -- or
        # the brief read back -- passed from t=0; and the jar shelves hold a
        # correctly shelved jam jar of the same red, so "I see jars of jam on
        # the left shelves" passed too. So: within 1.5 m of where the stray
        # was left (the shelf jars are 2.6 m from that spot), as
        # counter_out_of_reach judges its refusal at the shelf; the brief says
        # to go over to it. A fixed spot, not Near the stray: once picked up
        # the stray travels with the robot, all the way to the amber mat
        # 0.6 m from the shelved jam jar.
        Goal(
            label="Say what is misfiled",
            predicate=AllOf([InCircle("robot", 0.35, 1.08, 1.5), _NAMES_IT]),
        ),
        Goal(
            label="Jar on the amber jar sorting mat",
            predicate=Hold(
                inner=InCircle(target="pantry_jar_stray", x=-1.68, y=0.15, radius_m=0.24, min_z=0.0209, max_z=0.0464),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=480,
    category=2,
)
