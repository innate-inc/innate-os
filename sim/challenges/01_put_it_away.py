"""An untimed first mission, completed by the brick's actual resting place."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Predicate


class BrickInBox(Predicate):
    def update(self, state, events):
        brick, box = state.objects.get("lego"), state.objects.get("crate")
        if not brick or not box:
            return False
        # MuJoCo object poses are xyz + wxyz. Judge in the box's frame so a
        # nudged/rotated box still works, and require the brick below its rim.
        qw, qx, qy, qz = box[3:7]
        dx, dy, dz = (brick[i] - box[i] for i in range(3))
        x = (1 - 2 * (qy * qy + qz * qz)) * dx + 2 * (qx * qy + qw * qz) * dy + 2 * (qx * qz - qw * qy) * dz
        y = 2 * (qx * qy - qw * qz) * dx + (1 - 2 * (qx * qx + qz * qz)) * dy + 2 * (qy * qz + qw * qx) * dz
        z = 2 * (qx * qz + qw * qy) * dx + 2 * (qy * qz - qw * qx) * dy + (1 - 2 * (qx * qx + qy * qy)) * dz
        return abs(x) < 0.13 and abs(y) < 0.13 and -0.055 < z < 0.05


CHALLENGE = Challenge(
    id="put_it_away",
    title="Put it away",
    brief="A LEGO brick is on the living-room floor. Ask MARS to put it in the cardboard box.",
    environments=("apartment",),
    setup=[Drop("lego", -4.34, -0.47, yaw_deg=0), Drop("crate", -4.34, -0.90)],
    goals=[Goal("LEGO in the box", Hold(BrickInBox(), 2.0))],
    agent_guidance=(
        "Help the user clean up the red LEGO brick in the living room. The cardboard box is nearby. "
        "Invite them to ask you to pick up the brick, then suggest tossing it into the box or placing it carefully. "
        "Search your spatial memories if the box is out of view. Use PickAnyObject, then ThrowObject only when "
        "asked to throw and facing the nearby clear box, or DropInBox for careful placement. "
        "A missed throw is recoverable: tell the user where it landed and suggest asking you to try again."
    ),
)
