"""Cross the south crosswalk, using actual traffic contact as the failure signal."""

from mars_sim_driver.challenges import Challenge, Goal, Predicate


class SafeCrossing(Predicate):
    def reset(self):
        self._armed = False

    def update(self, state, events):
        x, y, _ = state.robot
        if abs(y + 5.3) > 0.8:
            self._armed = False
            return False
        # Returning to the starting curb readies another try in the same
        # world; there is no scene-reset button in the first-run experience.
        if x >= 4.5:
            self._armed = True
        if state.traffic_contact:
            self._armed = False
        return self._armed and x <= -4.5


CHALLENGE = Challenge(
    id="other_side",
    title="The other side",
    brief="Help MARS reach the opposite sidewalk using the crosswalk, without hitting traffic.",
    environments=("intersection",),
    setup=[],
    goals=[Goal("Cross safely to the opposite sidewalk", SafeCrossing())],
    agent_guidance=(
        "Help the user cross the south crosswalk from the east sidewalk to the west sidewalk. "
        "SearchMemory has the crosswalk approach and the opposite curb. First reach the near curb; look for traffic, "
        "then cross inside the stripes when clear. Do not wander into the intersection or take a shortcut around the crossing. "
        "If you touch a car, return to the starting curb before trying again. Ask for a new instruction after a failure; "
        "never claim success until the challenge says passed."
    ),
)
