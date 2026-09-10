"""CATEGORY 1 -- the FLOOR control for counter_within_reach.

WHY THIS EXISTS. `pick_any_object` is floor-only. Its docstring says so, its
Gemini prompt says "Find '{prompt}' lying on the floor in this image", and its
verification step asks whether the object is "lying loose on the floor/carpet".
15 of the suite's 19 manipulation targets sit on a counter, shelf or bench --
so every one of those failures has two candidate explanations, and the suite as
first written could not tell them apart:

  (a) the robot cannot pick anything off a raised surface, or
  (b) the robot cannot follow a fetch instruction.

This is the same task as counter_within_reach -- same object, same
destination, same two goals -- with the jar on the FLOOR instead of the bottom
shelf. Run the pair and the ambiguity collapses:

  floor PASSES, shelf fails  -> the blocker is surface height, and the agent's
                               instruction following is fine
  both fail                  -> the problem is bigger than height, and the
                               shelf result was never about the shelf
  floor fails, shelf passes  -> something is wrong with this control

The suite already contained one accidental floor case (gallery_fetch_mug) and
it was carrying the entire weight of this distinction on its own. One sample
across one map is not a control; this makes it deliberate and per-category.

THE DROP. (0.00, -0.70) is open floor 0.65 m in front of the spawn: free in the
nav map, 0.32 m of clearance, and outside every delivery circle -- a drop
inside the destination would satisfy the goal at t=0 and measure nothing.

Goal 2 is the same negated `Said` as the shelf twin. It is what catches the
agent that refuses by POLICY rather than by observation: on the live run the
robot answered "I can only pick up objects lying directly on the floor" and
scored zero on BOTH halves of the reach pair -- including the reachable one.
Here that sentence is not merely unhelpful, it is false about the object in
front of it.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Said, SkillDone

CHALLENGE = Challenge(
    id="counter_floor_within_reach",
    title="Within reach, on the floor",
    category=1,
    brief="There's a jar on the floor in front of you. Can you bring it to the counter?",
    setup=[
        Drop("counter_jar_jam", 0.00, -0.70),
    ],
    goals=[
        # Bulldozing did not reproduce on this one -- the middle stool blocks
        # the straight push -- but that is one route tried, not a proof, and
        # the three floor controls must share a goal structure or the twin
        # comparison measures the structure instead of the surface.
        Goal("Actually picked it up", SkillDone("pick_any_object")),
        # Whole counter top, matching the shelf twin exactly -- see
        # 13_within_reach.py for why the middle-of-the-counter circle was a
        # false-negative machine. Twins must share their rubric or the pair
        # measures the rubric difference, not the surface difference.
        Goal("Jar on the counter", InRect("counter_jar_jam", -1.22, 1.23, 1.22, 1.61, min_z=0.10)),
        Goal(
            "Never claimed it was out of reach",
            Said(
                [
                    r"can'?t reach|cannot reach|can not reach|unable to reach",
                    r"out of (my )?reach|too high|beyond my reach",
                ],
                negate=True,
            ),
        ),
    ],
    time_limit_s=420,
)
