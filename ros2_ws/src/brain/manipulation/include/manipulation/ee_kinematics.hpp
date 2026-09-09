// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once
#include <cmath>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include <kdl/chainfksolverpos_recursive.hpp>
#include <kdl_parser/kdl_parser.hpp>

namespace manipulation {
// Same URDF and base_link -> ee_link chain as mars_arm/ik.py. Evaluate
// measured qpos, never the asynchronous /fk_pose topic or leader commands.
class EeKinematics {
 public:
    explicit EeKinematics(const std::string& xml) {
        KDL::Tree tree;
        if (!kdl_parser::treeFromString(xml, tree) || !tree.getChain("base_link", "ee_link", chain_)
            || chain_.getNrOfJoints() == 0)
            throw std::runtime_error("Cannot construct base_link -> ee_link FK chain");
    }
    std::vector<double> pose(const std::vector<std::string>& names, const std::vector<double>& positions) const {
        if (names.size() != positions.size()) throw std::runtime_error("Joint names/positions mismatch");
        std::map<std::string, double> joints;
        for (size_t i = 0; i < names.size(); ++i) {
            if (!std::isfinite(positions[i]) || !joints.emplace(names[i], positions[i]).second)
                throw std::runtime_error("Invalid or duplicate joint state");
        }
        KDL::JntArray q(chain_.getNrOfJoints());
        unsigned int i = 0;
        for (const auto& segment : chain_.segments) {
            const auto& joint = segment.getJoint();
            if (joint.getType() != KDL::Joint::None) q(i++) = joints.at(joint.getName());
        }
        KDL::Frame frame;
        KDL::ChainFkSolverPos_recursive solver(chain_);
        if (solver.JntToCart(q, frame) < 0) throw std::runtime_error("Recording FK failed");
        double x, y, z, w;
        frame.M.GetQuaternion(x, y, z, w);
        return {frame.p.x(), frame.p.y(), frame.p.z(), x, y, z, w};
    }
 private:
    KDL::Chain chain_;
};
}  // namespace manipulation
