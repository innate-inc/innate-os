// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <Eigen/Geometry>
#include <string>
#include <vector>

namespace mars_arm {

// MuJoCo's default, so the sim and the robot compensate against one number.
static constexpr double kGravity = 9.81;

// What holding torque each servo must supply to keep the arm in a pose, read
// straight off mars.urdf. Equals MuJoCo's qfrc_bias on the same file to machine
// precision — see mars_arm/README.md for the check.
class GravityModel {
   public:
    // Joint names are URDF names in chain order. A revolute joint NOT named
    // here is lumped into its parent at q=0: that is the mimicked second
    // gripper blade, whose 12 g travel a millimetre over the gripper's range.
    // Throws if the URDF is unreadable or a named joint is missing.
    GravityModel(const std::string& urdf_path, const std::vector<std::string>& joint_names);

    // Holding torque per named joint, N*m, in that joint's own URDF axis sign.
    std::vector<double> holdingTorques(const std::vector<double>& q) const;

    size_t size() const {
        return joints_.size();
    }

   private:
    struct Joint {
        Eigen::Isometry3d origin = Eigen::Isometry3d::Identity();  // parent joint's frame -> this one at q=0
        Eigen::Vector3d axis = Eigen::Vector3d::UnitZ();           // unit, in this joint's frame
        double mass = 0.0;                     // child link plus everything rigidly hung off it
        Eigen::Vector3d com = Eigen::Vector3d::Zero();  // that aggregate's COM, in the child link frame
        int parent = -1;                       // index into joints_; -1 = carried by the root link
        std::vector<int> subtree;              // this index and every descendant of it
    };

    std::vector<Joint> joints_;
    std::vector<int> order_;  // joints_ indices with every parent before its children
};

}  // namespace mars_arm
