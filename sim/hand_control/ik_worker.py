"""Isolated JSON worker for mars_arm's IK. No ROS, sockets or actuator access."""

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROBOT_URDF = next(
    path
    for package in ("mars_description", "mars_sim")
    if (path := REPO / f"ros2_ws/src/mars_bot/{package}/urdf/mars.urdf").is_file()
)
sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_arm"))
import PyKDL as kdl  # noqa: E402

from mars_arm.kinematics import ArmKinematics  # noqa: E402


def main():
    solver = ArmKinematics(ROBOT_URDF)
    print(
        json.dumps({"ready": True, "solver": "mars_arm.kinematics.ArmKinematics", "joints": solver.joint_names}),
        flush=True,
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            values = [*request["position"], *request["rpy"], *request["seed"]]
            if len(values) != 11 or not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                raise ValueError("Expected finite position, RPY, and five joint angles")
            position = kdl.Vector(*request["position"])
            offset_values = request.get("offset", [0, 0, 0])
            if len(offset_values) != 3 or not all(type(v) in (int, float) and math.isfinite(v) for v in offset_values):
                raise ValueError("Invalid grasp offset")
            offset = kdl.Vector(*offset_values)
            target = kdl.Frame(kdl.Rotation.RPY(*request["rpy"]), position)
            seed = kdl.JntArray(5)
            for i, value in enumerate(request["seed"]):
                seed[i] = value
            actual = kdl.Frame()
            solver.fk_solver.JntToCart(seed, actual)
            for _ in range(3):
                target.p = position - actual.M * offset
                out, score, source = solver.solve(target, seed, enforce_limits=True)
                if out is None:
                    break
                solver.fk_solver.JntToCart(out, actual)
                grasp_error = (actual.p + actual.M * offset - position).Norm()
                if grasp_error < 0.0005:
                    break
                seed = out
            if out is None:
                answer = {"solution": None}
            else:
                actual = kdl.Frame()
                solver.fk_solver.JntToCart(out, actual)
                answer = {
                    "solution": [math.atan2(math.sin(out[i]), math.cos(out[i])) for i in range(5)],
                    "position": [actual.p[i] for i in range(3)],
                    "rpy": list(actual.M.GetRPY()),
                    "position_error": (target.p - actual.p).Norm(),
                    "grasp_error": grasp_error,
                    "rotation_error": abs((target.M.Inverse() * actual.M).GetRotAngle()[0]),
                    "score": score,
                    "seed": source,
                }
            print(json.dumps(answer, allow_nan=False), flush=True)
        except (ValueError, KeyError, TypeError) as error:
            print(json.dumps({"error": str(error)}), flush=True)


if __name__ == "__main__":
    main()
