// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Integration fixture: real FK + streaming writer, including rollback and moves.
#include "manipulation/ee_kinematics.hpp"
#include "manipulation/episode_data.hpp"
#include <fstream>
#include <iterator>
#include <stdexcept>

int main(int argc, char** argv) {
    if (argc != 3)
        return 2;
    std::ifstream input(argv[1]);
    std::string xml((std::istreambuf_iterator<char>(input)), {});
    manipulation::EeKinematics fk(xml);
    manipulation::EpisodeData episode;
    episode.open_file(argv[2]);
    // Deliberately noncanonical observation ordering exercises name-based FK.
    std::vector<std::string> names = {"joint6", "joint4", "joint1", "joint5", "joint2", "joint3"};
    episode.set_kinematics(xml, names, {"/mars/main_camera/left/image_raw", "/mars/arm/image_raw"});
    std::vector<double> q = {1., 0.8, 0.1, 0.0, -0.5, 0.4};
    std::vector<cv::Mat> images(2, cv::Mat(24, 32, CV_8UC3, cv::Scalar(10, 20, 200)));
    for (int i = 0; i < 5; ++i) {
        q[2] += 0.01;
        std::vector<double> action = {q[2], q[4], q[5], q[1], q[3], q[0], 0., 0.};
        episode.add_timestep(action, q, std::vector<double>(6), images, 1000. + i * .1,
                             {1000. + i * .1, 1000. + i * .1}, 25., fk.pose(names, q));
    }
    // Error after EE write must roll back every dataset.
    bool rejected = false;
    try {
        episode.add_timestep(std::vector<double>(8), q, {0.}, images, 1001., {1001., 1001.}, 25., fk.pose(names, q));
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    if (!rejected || episode.get_episode_length() != 5)
        return 3;
    manipulation::EpisodeData moved(std::move(episode));
    moved.finalize();
    try {
        fk.pose({"joint1"}, {0.});
        return 4;
    } catch (const std::exception&) {
    }
    return 0;
}
