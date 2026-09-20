// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// gravity.cpp — static gravity torques for the arm, from mars.urdf
#include "mars_arm/gravity.hpp"

#include <urdf/model.h>

#include <functional>
#include <map>
#include <stdexcept>

namespace mars_arm {

namespace {

Eigen::Isometry3d toIsometry(const urdf::Pose& pose) {
    Eigen::Isometry3d t = Eigen::Isometry3d::Identity();
    t.linear() = Eigen::Quaterniond(pose.rotation.w, pose.rotation.x, pose.rotation.y, pose.rotation.z)
                     .normalized()
                     .toRotationMatrix();
    t.translation() = Eigen::Vector3d(pose.position.x, pose.position.y, pose.position.z);
    return t;
}

// Mass and first moment (mass * com) of a rigid group, in one shared frame.
struct Aggregate {
    double mass = 0.0;
    Eigen::Vector3d moment = Eigen::Vector3d::Zero();
};

}  // namespace

GravityModel::GravityModel(const std::string& urdf_path, const std::vector<std::string>& joint_names) {
    urdf::Model model;
    if (!model.initFile(urdf_path)) {
        throw std::runtime_error("gravity model: cannot parse URDF " + urdf_path);
    }

    std::map<std::string, int> actuated;
    for (size_t i = 0; i < joint_names.size(); ++i) {
        actuated[joint_names[i]] = static_cast<int>(i);
    }
    joints_.resize(joint_names.size());
    std::vector<bool> found(joint_names.size(), false);

    // Walk the tree once. `carrier` is the actuated joint whose child link this
    // subtree hangs off (-1 at the root, which is the arm's ground), and `to_carrier`
    // maps points from `link`'s frame into that link's frame. Everything between two
    // actuated joints — fixed frames, the mimicked blade — is rigid, so it folds into
    // the carrier's aggregate here and costs nothing at runtime.
    std::function<Aggregate(const urdf::LinkConstSharedPtr&, int, const Eigen::Isometry3d&)> collect =
        [&](const urdf::LinkConstSharedPtr& link, int carrier, const Eigen::Isometry3d& to_carrier) -> Aggregate {
        Aggregate agg;
        if (link->inertial) {
            agg.mass = link->inertial->mass;
            agg.moment = agg.mass * (to_carrier * toIsometry(link->inertial->origin).translation());
        }
        for (const auto& joint : link->child_joints) {
            const Eigen::Isometry3d to_child = to_carrier * toIsometry(joint->parent_to_joint_origin_transform);
            const urdf::LinkConstSharedPtr child = model.getLink(joint->child_link_name);
            if (!child) {
                throw std::runtime_error("gravity model: joint " + joint->name + " has no child link");
            }

            auto it = actuated.find(joint->name);
            if (it == actuated.end()) {
                const Aggregate sub = collect(child, carrier, to_child);
                agg.mass += sub.mass;
                agg.moment += sub.moment;
                continue;
            }

            const int index = it->second;
            if (joint->type != urdf::Joint::REVOLUTE && joint->type != urdf::Joint::CONTINUOUS) {
                throw std::runtime_error("gravity model: joint " + joint->name + " is not revolute");
            }
            Joint& entry = joints_[index];
            entry.parent = carrier;
            entry.origin = to_child;
            entry.axis = Eigen::Vector3d(joint->axis.x, joint->axis.y, joint->axis.z);
            if (entry.axis.norm() <= 0.0) {
                throw std::runtime_error("gravity model: joint " + joint->name + " has a zero axis");
            }
            entry.axis.normalize();

            const Aggregate sub = collect(child, index, Eigen::Isometry3d::Identity());
            entry.mass = sub.mass;
            entry.com = sub.mass > 0.0 ? Eigen::Vector3d(sub.moment / sub.mass) : Eigen::Vector3d::Zero();
            found[index] = true;
        }
        return agg;
    };
    collect(model.getRoot(), -1, Eigen::Isometry3d::Identity());

    for (size_t i = 0; i < found.size(); ++i) {
        if (!found[i]) {
            throw std::runtime_error("gravity model: joint " + joint_names[i] + " is not in " + urdf_path);
        }
        joints_[i].subtree.push_back(static_cast<int>(i));
    }
    for (size_t i = 0; i < joints_.size(); ++i) {
        for (int p = joints_[i].parent; p >= 0; p = joints_[p].parent) {
            joints_[p].subtree.push_back(static_cast<int>(i));
        }
    }

    // Parents first, so holdingTorques can compose frames in one forward pass
    // whatever order the caller named the joints in.
    std::vector<bool> placed(joints_.size(), false);
    while (order_.size() < joints_.size()) {
        const size_t before = order_.size();
        for (size_t i = 0; i < joints_.size(); ++i) {
            if (placed[i] || (joints_[i].parent >= 0 && !placed[joints_[i].parent])) {
                continue;
            }
            placed[i] = true;
            order_.push_back(static_cast<int>(i));
        }
        if (order_.size() == before) {
            throw std::runtime_error("gravity model: joint tree has a cycle");
        }
    }
}

std::vector<double> GravityModel::holdingTorques(const std::vector<double>& q) const {
    if (q.size() != joints_.size()) {
        throw std::invalid_argument("gravity model: expected " + std::to_string(joints_.size()) + " joint angles");
    }

    std::vector<Eigen::Isometry3d> frame(joints_.size());
    std::vector<Eigen::Vector3d> com(joints_.size());
    for (int i : order_) {
        const Joint& j = joints_[i];
        frame[i] = j.origin * Eigen::AngleAxisd(q[i], j.axis);
        if (j.parent >= 0) {
            frame[i] = frame[j.parent] * frame[i];
        }
        com[i] = frame[i] * j.com;
    }

    const Eigen::Vector3d gravity(0.0, 0.0, -kGravity);
    std::vector<double> torques(joints_.size(), 0.0);
    for (size_t i = 0; i < joints_.size(); ++i) {
        const Eigen::Vector3d axis = frame[i].linear() * joints_[i].axis;
        const Eigen::Vector3d pivot = frame[i].translation();
        double load = 0.0;
        for (int k : joints_[i].subtree) {
            load += axis.dot((com[k] - pivot).cross(joints_[k].mass * gravity));
        }
        torques[i] = -load;
    }
    return torques;
}

}  // namespace mars_arm
