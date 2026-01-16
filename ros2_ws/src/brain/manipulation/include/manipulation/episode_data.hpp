#ifndef MANIPULATION_EPISODE_DATA_HPP_
#define MANIPULATION_EPISODE_DATA_HPP_

#include <string>
#include <vector>
#include <map>
#include <opencv2/core.hpp>

namespace manipulation {

class EpisodeData {
public:
    EpisodeData();
    explicit EpisodeData(const std::vector<std::string>& camera_names);

    void add_timestep(
        const std::vector<double>& action,
        const std::vector<double>& qpos,
        const std::vector<double>& qvel,
        const std::vector<cv::Mat>& images,
        double arm_timestamp = -1.0,
        const std::vector<double>& image_timestamps = {}
    );

    void add_termination_data();
    void save_file(const std::string& path);
    void clear();
    size_t get_episode_length() const;

    // Accessors
    const std::vector<std::vector<double>>& get_actions() const { return actions_; }
    const std::vector<std::vector<double>>& get_qpos() const { return qpos_; }
    const std::vector<std::vector<double>>& get_qvel() const { return qvel_; }
    const std::map<std::string, std::vector<cv::Mat>>& get_images() const { return images_; }
    const std::vector<double>& get_arm_timestamps() const { return arm_timestamps_; }
    const std::map<std::string, std::vector<double>>& get_image_timestamps() const { return image_timestamps_; }

private:
    std::vector<std::string> camera_names_;
    bool camera_names_set_;

    std::vector<std::vector<double>> actions_;
    std::vector<std::vector<double>> qpos_;
    std::vector<std::vector<double>> qvel_;

    std::vector<double> arm_timestamps_;
    std::map<std::string, std::vector<double>> image_timestamps_;
    std::map<std::string, std::vector<cv::Mat>> images_;
};

}  // namespace manipulation

#endif  // MANIPULATION_EPISODE_DATA_HPP_
