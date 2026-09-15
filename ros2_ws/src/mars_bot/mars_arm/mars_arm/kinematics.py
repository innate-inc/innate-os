# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Innate's URDF/KDL solver, usable with or without a ROS node."""

import math

import PyKDL as kdl
from urdf_parser_py.urdf import URDF

from mars_arm.urdf import treeFromUrdfModel


class ArmKinematics:
    def __init__(self, urdf_path, eps=0.0001, maxiter=2000):
        model = URDF.from_xml_file(str(urdf_path))
        ok, tree = treeFromUrdfModel(model, quiet=True)
        if not ok or tree is None:
            raise RuntimeError("URDF→KDL parse error")
        self.chain = tree.getChain("base_link", "ee_link")
        self.fk_solver = kdl.ChainFkSolverPos_recursive(self.chain)
        self.ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=eps, maxiter=maxiter)
        self.joint_names = [
            self.chain.getSegment(i).getJoint().getName()
            for i in range(self.chain.getNrOfSegments())
            if self.chain.getSegment(i).getJoint().getType() != kdl.Joint.Fixed
        ]
        self.limits = [(model.joint_map[n].limit.lower, model.joint_map[n].limit.upper) for n in self.joint_names]

    def try_seed(self, seed, target):
        """Same acceptance and scoring as the Innate IK topic node."""
        out = kdl.JntArray(self.chain.getNrOfJoints())
        result = self.ik_solver.CartToJnt(seed, target, out)
        if result < 0 and result not in (-100, -101):
            return False, None, float("inf")
        actual = kdl.Frame()
        self.fk_solver.JntToCart(out, actual)
        position_error = (target.p - actual.p).Norm()
        angle_error = (target.M.Inverse() * actual.M).GetRotAngle()[0]
        return True, out, position_error + 0.1 * abs(angle_error)

    def solve(self, target, current, *, enforce_limits=False):
        """Current/zero multi-start, matching the ROS node's selection policy.

        The simulation adapter can reject joint-limit violations. The ROS
        node retains its existing downstream actuator limit policy.
        """
        best, best_score, best_seed = None, float("inf"), None
        for name, seed in (("current", current), ("zeros", kdl.JntArray(self.chain.getNrOfJoints()))):
            success, out, score = self.try_seed(seed, target)
            if not success:
                continue
            if enforce_limits:
                normalized = [math.atan2(math.sin(out[i]), math.cos(out[i])) for i in range(out.rows())]
                if any(
                    not low - 1e-6 <= v <= high + 1e-6 for v, (low, high) in zip(normalized, self.limits, strict=True)
                ):
                    continue
            if score < best_score:
                best, best_score, best_seed = out, score, name
        return best, best_score, best_seed
