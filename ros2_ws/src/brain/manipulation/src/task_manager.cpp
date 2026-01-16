#include "manipulation/task_manager.hpp"

#include <fstream>
#include <filesystem>
#include <iostream>
#include <hdf5.h>

namespace fs = std::filesystem;

namespace manipulation {

TaskManager::TaskManager(const std::string& base_data_directory)
    : base_data_directory_(base_data_directory) {}

void TaskManager::start_new_task(const std::string& task_name, double data_frequency,
                                  const std::string& primitive_type) {
    current_task_name_ = task_name;
    current_task_dir_ = base_data_directory_ + "/" + task_name;
    std::string data_dir = current_task_dir_ + "/data";
    std::string dataset_metadata_path = data_dir + "/dataset_metadata.json";

    if (fs::exists(dataset_metadata_path)) {
        std::cout << "Dataset for '" << task_name << "' already exists. Resuming." << std::endl;
        resume_task(task_name);
        return;
    }

    // Create data directory
    fs::create_directories(data_dir);
    
    // Initialize metadata
    metadata_ = {
        {"data_frequency", data_frequency},
        {"number_of_episodes", 0},
        {"episodes", nlohmann::json::array()}
    };
    save_metadata();
    
    // Create primitive metadata
    create_primitive_metadata(task_name, primitive_type);
}

void TaskManager::resume_task(const std::string& task_name) {
    current_task_name_ = task_name;
    current_task_dir_ = base_data_directory_ + "/" + task_name;
    std::string metadata_path = current_task_dir_ + "/data/dataset_metadata.json";
    
    if (!fs::exists(metadata_path)) {
        throw std::runtime_error("Dataset metadata not found for task '" + task_name + "' at " + metadata_path);
    }
    load_metadata();
}

void TaskManager::add_episode(EpisodeData& episode_data,
                               const std::string& start_timestamp,
                               const std::string& end_timestamp) {
    std::string data_dir = current_task_dir_ + "/data";
    fs::create_directories(data_dir);

    int episode_id = metadata_["number_of_episodes"].get<int>();
    std::string file_name = "episode_" + std::to_string(episode_id) + ".h5";
    std::string file_path = data_dir + "/" + file_name;

    // Save the episode
    episode_data.save_file(file_path);

    // Update metadata
    nlohmann::json episode_info = {
        {"episode_id", episode_id},
        {"file_name", file_name},
        {"start_timestamp", start_timestamp},
        {"end_timestamp", end_timestamp}
    };
    metadata_["episodes"].push_back(episode_info);
    metadata_["number_of_episodes"] = episode_id + 1;
    save_metadata();
}

void TaskManager::end_task() {
    save_metadata();
    current_task_name_.clear();
    current_task_dir_.clear();
    metadata_ = nullptr;
}

int TaskManager::get_number_of_episodes() const {
    if (metadata_.is_null() || !metadata_.contains("number_of_episodes")) {
        return 0;
    }
    return metadata_["number_of_episodes"].get<int>();
}

void TaskManager::save_metadata() {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("No active task directory to save metadata.");
    }
    std::string data_dir = current_task_dir_ + "/data";
    fs::create_directories(data_dir);
    std::string metadata_path = data_dir + "/dataset_metadata.json";
    
    std::ofstream file(metadata_path);
    if (!file.is_open()) {
        throw std::runtime_error("Failed to open metadata file for writing: " + metadata_path);
    }
    file << metadata_.dump(4);
}

void TaskManager::load_metadata() {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("No active task directory to load metadata from.");
    }
    std::string metadata_path = current_task_dir_ + "/data/dataset_metadata.json";
    
    std::ifstream file(metadata_path);
    if (!file.is_open()) {
        throw std::runtime_error("Failed to open metadata file: " + metadata_path);
    }
    file >> metadata_;
}

void TaskManager::create_primitive_metadata(const std::string& task_name, const std::string& primitive_type) {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("No active task directory to create primitive metadata.");
    }

    std::string primitive_metadata_path = current_task_dir_ + "/metadata.json";
    
    if (fs::exists(primitive_metadata_path)) {
        std::cout << "Primitive metadata.json already exists for '" << task_name << "'. Skipping creation." << std::endl;
        return;
    }

    nlohmann::json primitive_metadata = {
        {"name", task_name},
        {"type", primitive_type},
        {"guidelines", ""},
        {"guidelines_when_running", ""},
        {"inputs", nlohmann::json::object()},
        {"execution", nlohmann::json::object()}
    };

    std::ofstream file(primitive_metadata_path);
    if (!file.is_open()) {
        throw std::runtime_error("Failed to create primitive metadata file: " + primitive_metadata_path);
    }
    file << primitive_metadata.dump(4);
    std::cout << "Created primitive metadata.json for '" << task_name << "' (type: " << primitive_type << ")" << std::endl;
}

std::optional<nlohmann::json> TaskManager::get_enriched_metadata_for_task(const std::string& task_directory,
                                                                            std::string& error_msg) {
    std::string data_dir = task_directory + "/data";
    std::string metadata_file_path = data_dir + "/dataset_metadata.json";
    std::string task_name = fs::path(task_directory).filename().string();

    if (!fs::exists(task_directory) || !fs::is_directory(task_directory)) {
        error_msg = "Task directory " + task_directory + " not found or is not a directory.";
        return std::nullopt;
    }

    if (!fs::exists(metadata_file_path)) {
        error_msg = "dataset_metadata.json not found in " + data_dir + ".";
        return std::nullopt;
    }

    nlohmann::json dataset_metadata;
    try {
        std::ifstream file(metadata_file_path);
        file >> dataset_metadata;
    } catch (const std::exception& e) {
        error_msg = "Error reading dataset_metadata.json in " + data_dir + ": " + e.what();
        return std::nullopt;
    }

    nlohmann::json processed_episodes = nlohmann::json::array();
    if (dataset_metadata.contains("episodes") && dataset_metadata["episodes"].is_array()) {
        for (const auto& episode_info : dataset_metadata["episodes"]) {
            int num_timesteps = 0;
            std::string episode_file_name = episode_info.value("file_name", "");
            std::string episode_file_path = data_dir + "/" + episode_file_name;

            if (!episode_file_name.empty() && fs::exists(episode_file_path)) {
                hid_t file_id = H5Fopen(episode_file_path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
                if (file_id >= 0) {
                    if (H5Lexists(file_id, "/action", H5P_DEFAULT) > 0) {
                        hid_t dataset = H5Dopen2(file_id, "/action", H5P_DEFAULT);
                        if (dataset >= 0) {
                            hid_t dataspace = H5Dget_space(dataset);
                            hsize_t dims[2];
                            H5Sget_simple_extent_dims(dataspace, dims, nullptr);
                            num_timesteps = static_cast<int>(dims[0]);
                            H5Sclose(dataspace);
                            H5Dclose(dataset);
                        }
                    }
                    H5Fclose(file_id);
                }
            }

            processed_episodes.push_back({
                {"episode_id", "episode_" + std::to_string(episode_info.value("episode_id", 0))},
                {"start_time", episode_info.value("start_timestamp", "N/A")},
                {"end_time", episode_info.value("end_timestamp", "N/A")},
                {"num_timesteps", num_timesteps},
                {"file_name", episode_file_name}
            });
        }
    }

    nlohmann::json enriched_metadata = {
        {"task_name", task_name},
        {"task_directory", task_directory},
        {"data_frequency", dataset_metadata.value("data_frequency", 0)},
        {"number_of_episodes", dataset_metadata.value("number_of_episodes", 0)},
        {"episodes", processed_episodes}
    };

    return enriched_metadata;
}

std::vector<nlohmann::json> TaskManager::get_all_tasks_summary() {
    std::vector<nlohmann::json> all_tasks_summary;
    
    if (!fs::exists(base_data_directory_) || !fs::is_directory(base_data_directory_)) {
        std::cerr << "Base data directory " << base_data_directory_ << " does not exist or is not a directory." << std::endl;
        return all_tasks_summary;
    }

    for (const auto& entry : fs::directory_iterator(base_data_directory_)) {
        if (!entry.is_directory()) continue;

        std::string error_msg;
        auto metadata_opt = get_enriched_metadata_for_task(entry.path().string(), error_msg);

        if (metadata_opt) {
            all_tasks_summary.push_back(*metadata_opt);
        } else if (!error_msg.empty()) {
            std::cerr << "Skipping task directory " << entry.path().filename().string() 
                      << " due to error: " << error_msg << std::endl;
        }
    }

    return all_tasks_summary;
}

std::tuple<bool, std::string, std::string> TaskManager::get_task_metadata_by_directory(const std::string& task_directory) {
    std::string error_msg;
    auto metadata_opt = get_enriched_metadata_for_task(task_directory, error_msg);

    if (metadata_opt) {
        return {true, "Metadata retrieved successfully.", metadata_opt->dump(4)};
    } else {
        if (error_msg.find("not found") != std::string::npos) {
            return {false, "Task at directory '" + task_directory + "' not found.", "{}"};
        }
        return {false, error_msg, "{}"};
    }
}

std::tuple<bool, std::string> TaskManager::update_task_metadata_by_directory(const std::string& task_directory,
                                                                               const std::string& json_metadata_update) {
    std::string metadata_file_path = task_directory + "/metadata.json";

    if (!fs::exists(task_directory) || !fs::is_directory(task_directory)) {
        return {false, "Task directory '" + task_directory + "' not found or is not a directory."};
    }

    if (!fs::exists(metadata_file_path)) {
        return {false, "Metadata file for task at '" + task_directory + "' not found."};
    }

    nlohmann::json update_data;
    try {
        update_data = nlohmann::json::parse(json_metadata_update);
    } catch (const std::exception& e) {
        return {false, std::string("Invalid JSON format in update: ") + e.what()};
    }

    try {
        std::ifstream infile(metadata_file_path);
        nlohmann::json metadata;
        infile >> metadata;
        infile.close();

        for (auto& [key, value] : update_data.items()) {
            metadata[key] = value;
        }

        std::ofstream outfile(metadata_file_path);
        outfile << metadata.dump(4);
        return {true, "Metadata updated successfully."};
    } catch (const std::exception& e) {
        return {false, std::string("Failed to read or write metadata at ") + task_directory + ": " + e.what()};
    }
}

}  // namespace manipulation
