#include "manipulation/episode_data.hpp"

#include <stdexcept>
#include <algorithm>
#include <numeric>
#include <hdf5.h>

namespace manipulation {

EpisodeData::EpisodeData()
    : camera_names_set_(false) {}

EpisodeData::EpisodeData(const std::vector<std::string>& camera_names)
    : camera_names_(camera_names), camera_names_set_(true) {
    for (const auto& cam : camera_names_) {
        images_[cam] = std::vector<cv::Mat>();
        image_timestamps_[cam] = std::vector<double>();
    }
}

void EpisodeData::add_timestep(
    const std::vector<double>& action,
    const std::vector<double>& qpos,
    const std::vector<double>& qvel,
    const std::vector<cv::Mat>& images,
    double arm_timestamp,
    const std::vector<double>& image_timestamps) {
    
    if (!camera_names_set_) {
        // Dynamically set camera names based on number of images
        for (size_t i = 0; i < images.size(); ++i) {
            std::string cam_name = "camera_" + std::to_string(i + 1);
            camera_names_.push_back(cam_name);
            images_[cam_name] = std::vector<cv::Mat>();
            image_timestamps_[cam_name] = std::vector<double>();
        }
        camera_names_set_ = true;
    } else if (images.size() != camera_names_.size()) {
        throw std::runtime_error("Expected " + std::to_string(camera_names_.size()) + 
                                 " images, but got " + std::to_string(images.size()));
    }

    actions_.push_back(action);
    qpos_.push_back(qpos);
    qvel_.push_back(qvel);

    if (arm_timestamp >= 0.0) {
        arm_timestamps_.push_back(arm_timestamp);
    }

    for (size_t idx = 0; idx < camera_names_.size(); ++idx) {
        const auto& cam = camera_names_[idx];
        images_[cam].push_back(images[idx].clone());
        
        if (idx < image_timestamps.size()) {
            image_timestamps_[cam].push_back(image_timestamps[idx]);
        }
    }
}

void EpisodeData::add_termination_data() {
    if (actions_.empty()) {
        return;
    }

    size_t total_timesteps = actions_.size();

    for (size_t i = 0; i < total_timesteps; ++i) {
        // Linear progression from 0 to 1
        double linear_value = (total_timesteps > 1) 
            ? static_cast<double>(i) / static_cast<double>(total_timesteps - 1) 
            : 0.0;
        
        // Termination: 0 for all except last 10 timesteps
        double termination_value = 0.0;
        if (total_timesteps >= 10) {
            if (i >= total_timesteps - 10) {
                termination_value = 1.0;
            }
        } else {
            termination_value = 1.0;
        }

        actions_[i].push_back(linear_value);
        actions_[i].push_back(termination_value);
    }
}

void EpisodeData::save_file(const std::string& path) {
    // Add termination data before saving
    add_termination_data();

    hid_t file_id = H5Fcreate(path.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
    if (file_id < 0) {
        throw std::runtime_error("Failed to create HDF5 file: " + path);
    }

    // Save actions
    if (!actions_.empty()) {
        size_t num_timesteps = actions_.size();
        size_t action_dim = actions_[0].size();
        
        std::vector<double> flat_actions;
        flat_actions.reserve(num_timesteps * action_dim);
        for (const auto& action : actions_) {
            flat_actions.insert(flat_actions.end(), action.begin(), action.end());
        }

        hsize_t dims[2] = {num_timesteps, action_dim};
        hid_t dataspace = H5Screate_simple(2, dims, nullptr);
        hid_t dataset = H5Dcreate2(file_id, "/action", H5T_NATIVE_DOUBLE, dataspace,
                                    H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
        H5Dwrite(dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_actions.data());
        H5Dclose(dataset);
        H5Sclose(dataspace);
    }

    // Save timestamps
    bool has_arm_ts = !arm_timestamps_.empty();
    bool has_img_ts = false;
    for (const auto& [cam, ts] : image_timestamps_) {
        if (!ts.empty()) {
            has_img_ts = true;
            break;
        }
    }

    if (has_arm_ts || has_img_ts) {
        hid_t ts_group = H5Gcreate2(file_id, "/timestamps", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
        
        if (has_arm_ts) {
            hsize_t ts_dims[1] = {arm_timestamps_.size()};
            hid_t ts_space = H5Screate_simple(1, ts_dims, nullptr);
            hid_t ts_dataset = H5Dcreate2(ts_group, "arm", H5T_NATIVE_DOUBLE, ts_space,
                                          H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
            H5Dwrite(ts_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, arm_timestamps_.data());
            H5Dclose(ts_dataset);
            H5Sclose(ts_space);
        }

        if (has_img_ts) {
            hid_t img_ts_group = H5Gcreate2(ts_group, "images", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
            for (const auto& [cam_name, ts_list] : image_timestamps_) {
                if (!ts_list.empty()) {
                    hsize_t img_ts_dims[1] = {ts_list.size()};
                    hid_t img_ts_space = H5Screate_simple(1, img_ts_dims, nullptr);
                    hid_t img_ts_dataset = H5Dcreate2(img_ts_group, cam_name.c_str(), H5T_NATIVE_DOUBLE,
                                                       img_ts_space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
                    H5Dwrite(img_ts_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, ts_list.data());
                    H5Dclose(img_ts_dataset);
                    H5Sclose(img_ts_space);
                }
            }
            H5Gclose(img_ts_group);
        }
        H5Gclose(ts_group);
    }

    // Check for observations
    bool has_qpos = !qpos_.empty() && !qpos_[0].empty();
    bool has_qvel = !qvel_.empty() && !qvel_[0].empty();
    bool has_images = false;
    for (const auto& [cam, imgs] : images_) {
        if (!imgs.empty()) {
            has_images = true;
            break;
        }
    }

    if (has_qpos || has_qvel || has_images) {
        hid_t obs_group = H5Gcreate2(file_id, "/observations", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

        if (has_qpos) {
            size_t num_timesteps = qpos_.size();
            size_t qpos_dim = qpos_[0].size();
            
            std::vector<double> flat_qpos;
            flat_qpos.reserve(num_timesteps * qpos_dim);
            for (const auto& q : qpos_) {
                flat_qpos.insert(flat_qpos.end(), q.begin(), q.end());
            }

            hsize_t qpos_dims[2] = {num_timesteps, qpos_dim};
            hid_t qpos_space = H5Screate_simple(2, qpos_dims, nullptr);
            hid_t qpos_dataset = H5Dcreate2(obs_group, "qpos", H5T_NATIVE_DOUBLE, qpos_space,
                                             H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
            H5Dwrite(qpos_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_qpos.data());
            H5Dclose(qpos_dataset);
            H5Sclose(qpos_space);
        }

        if (has_qvel) {
            size_t num_timesteps = qvel_.size();
            size_t qvel_dim = qvel_[0].size();
            
            std::vector<double> flat_qvel;
            flat_qvel.reserve(num_timesteps * qvel_dim);
            for (const auto& q : qvel_) {
                flat_qvel.insert(flat_qvel.end(), q.begin(), q.end());
            }

            hsize_t qvel_dims[2] = {num_timesteps, qvel_dim};
            hid_t qvel_space = H5Screate_simple(2, qvel_dims, nullptr);
            hid_t qvel_dataset = H5Dcreate2(obs_group, "qvel", H5T_NATIVE_DOUBLE, qvel_space,
                                             H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
            H5Dwrite(qvel_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_qvel.data());
            H5Dclose(qvel_dataset);
            H5Sclose(qvel_space);
        }

        if (has_images) {
            hid_t img_group = H5Gcreate2(obs_group, "images", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
            
            for (const auto& [cam_name, img_list] : images_) {
                if (img_list.empty()) continue;

                size_t num_frames = img_list.size();
                int height = img_list[0].rows;
                int width = img_list[0].cols;
                int channels = img_list[0].channels();

                // Flatten all images into a single buffer
                std::vector<uint8_t> flat_images;
                flat_images.reserve(num_frames * height * width * channels);
                
                for (const auto& img : img_list) {
                    cv::Mat continuous;
                    if (!img.isContinuous()) {
                        continuous = img.clone();
                    } else {
                        continuous = img;
                    }
                    flat_images.insert(flat_images.end(), 
                                       continuous.data, 
                                       continuous.data + (height * width * channels));
                }

                hsize_t img_dims[4] = {num_frames, static_cast<hsize_t>(height), 
                                        static_cast<hsize_t>(width), static_cast<hsize_t>(channels)};
                hid_t img_space = H5Screate_simple(4, img_dims, nullptr);
                hid_t img_dataset = H5Dcreate2(img_group, cam_name.c_str(), H5T_NATIVE_UINT8,
                                                img_space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
                H5Dwrite(img_dataset, H5T_NATIVE_UINT8, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_images.data());
                H5Dclose(img_dataset);
                H5Sclose(img_space);
            }
            H5Gclose(img_group);
        }
        H5Gclose(obs_group);
    }

    H5Fclose(file_id);
}

void EpisodeData::clear() {
    actions_.clear();
    qpos_.clear();
    qvel_.clear();
    arm_timestamps_.clear();
    
    for (auto& [cam, ts] : image_timestamps_) {
        ts.clear();
    }
    for (auto& [cam, imgs] : images_) {
        imgs.clear();
    }
}

size_t EpisodeData::get_episode_length() const {
    return actions_.size();
}

}  // namespace manipulation
