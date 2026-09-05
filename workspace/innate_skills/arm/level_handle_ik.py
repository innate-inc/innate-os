# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Level handle-approach IK, validated before any joint command is sent."""

import math
from pathlib import Path

import numpy as np
import PyKDL as kdl
from ament_index_python.packages import get_package_share_directory
from urdf_parser_py.urdf import URDF

from mars_arm.urdf import treeFromUrdfModel


class LevelHandleIK:
    def __init__(self):
        model = URDF.from_xml_file(str(Path(get_package_share_directory("mars_sim")) / "urdf/mars.urdf"))
        ok, tree = treeFromUrdfModel(model)
        if not ok:
            raise ValueError("Cannot load arm kinematics")
        self.chain = tree.getChain("base_link", "ee_link")
        self.names = [
            self.chain.getSegment(i).getJoint().getName()
            for i in range(self.chain.getNrOfSegments())
            if self.chain.getSegment(i).getJoint().getType() != kdl.Joint.Fixed
        ]
        self.limits = [(model.joint_map[n].limit.lower, model.joint_map[n].limit.upper) for n in self.names]
        self.fk = kdl.ChainFkSolverPos_recursive(self.chain)
        # Yaw follows lateral reach; roll and pitch must remain level.
        self.ik = kdl.ChainIkSolverPos_LMA(self.chain, np.array([1.0, 1.0, 1.0, 0.2, 0.2, 0.001]))

    def solve(self, target, current):
        if len(current) != len(self.names) or not all(math.isfinite(v) for v in (*target, *current)):
            raise ValueError("Invalid arm state for level approach")
        frame = kdl.Frame(kdl.Rotation.Identity(), kdl.Vector(*target))
        candidates = []
        for values in (current, [0.0] * len(current)):
            seed, out = kdl.JntArray(len(current)), kdl.JntArray(len(current))
            for i, v in enumerate(values):
                seed[i] = v
            self.ik.CartToJnt(seed, frame, out)
            joints = [math.atan2(math.sin(out[i]), math.cos(out[i])) for i in range(len(current))]
            if not all(lo <= v <= hi for v, (lo, hi) in zip(joints, self.limits, strict=True)):
                continue
            for i, v in enumerate(joints):
                out[i] = v
            actual = kdl.Frame()
            self.fk.JntToCart(out, actual)
            position = tuple(actual.p[i] for i in range(3))
            roll, pitch, _yaw = actual.M.GetRPY()
            if math.dist(position, target) > 0.005 or max(abs(roll), abs(pitch)) > math.radians(3):
                continue
            candidates.append(joints)
        if not candidates:
            raise ValueError(
                f"No level, joint-limit-valid solution for wrist target {tuple(round(v, 3) for v in target)}"
            )
        return min(candidates, key=lambda q: sum((v - old) ** 2 for v, old in zip(q, current, strict=True)))
