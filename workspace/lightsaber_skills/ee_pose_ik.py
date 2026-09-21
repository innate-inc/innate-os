# SPDX-License-Identifier: Apache-2.0
"""Full-pose KDL IK with explicit residual and driver-limit checks."""

import math

import numpy as np
import PyKDL as kdl

from .level_handle_ik import LevelHandleIK


class EEPoseIK(LevelHandleIK):
    def __init__(self, joint_limits):
        super().__init__()
        if len(joint_limits) != len(self.names) or any(
            len(pair) != 2 or not all(math.isfinite(v) for v in pair) or pair[0] >= pair[1]
            for pair in joint_limits
        ):
            raise ValueError("Invalid driver joint limits")
        self.limits = tuple(tuple(pair) for pair in joint_limits)
        # Unlike level-handle IK, all three orientation axes matter here.
        self.ik = kdl.ChainIkSolverPos_LMA(self.chain, np.array([1., 1., 1., .2, .2, .2]))

    @staticmethod
    def _frame(pose):
        if len(pose) != 6 or not all(math.isfinite(v) for v in pose):
            raise ValueError("EE pose requires finite XYZ and RPY")
        return kdl.Frame(kdl.Rotation.RPY(*pose[3:]), kdl.Vector(*pose[:3]))

    @classmethod
    def errors(cls, actual, target):
        a, b = cls._frame(actual), cls._frame(target)
        return (a.p - b.p).Norm(), abs((a.M.Inverse() * b.M).GetRotAngle()[0])

    def pose(self, joints):
        if len(joints) != len(self.names) or not all(math.isfinite(v) for v in joints):
            raise ValueError("Invalid measured joints for FK")
        q = kdl.JntArray(len(joints))
        for i, value in enumerate(joints):
            q[i] = value
        frame = kdl.Frame()
        if self.fk.JntToCart(q, frame) < 0:
            raise ValueError("Measured end-effector FK failed")
        return (*tuple(frame.p[i] for i in range(3)), *frame.M.GetRPY())

    def solve(self, target, current):
        frame = self._frame(target)
        self.pose(current)  # Validate seed shape/finiteness before entering KDL.
        candidates, nearest = [], None
        for values in (current, [0.] * len(current)):
            seed, result = kdl.JntArray(len(current)), kdl.JntArray(len(current))
            for i, value in enumerate(values):
                seed[i] = value
            self.ik.CartToJnt(seed, frame, result)
            joints = [math.atan2(math.sin(result[i]), math.cos(result[i])) for i in range(len(current))]
            if not all(lo <= value <= hi for value, (lo, hi) in zip(joints, self.limits, strict=True)):
                continue
            actual = self.pose(joints)
            distance, angle = self.errors(actual, target)
            score = distance + .1 * angle
            if nearest is None or score < nearest[0]:
                nearest = (score, distance, angle, actual)
            # A solver return code alone does not prove a 6D target is reachable.
            if distance <= .005 and angle <= math.radians(3):
                candidates.append(joints)
        if not candidates:
            detail = "no solution within driver joint limits"
            if nearest:
                _, distance, angle, actual = nearest
                detail = (f"position residual={distance:.4f} m, orientation residual={angle:.3f} rad; "
                          f"nearest candidate ee_pose={tuple(round(v, 4) for v in actual)} (NOT executed)")
            raise ValueError(f"Requested EE pose is unreachable: {detail}. Revise XYZ and/or RPY.")
        return min(candidates, key=lambda q: sum((a - b) ** 2 for a, b in zip(q, current, strict=True)))
