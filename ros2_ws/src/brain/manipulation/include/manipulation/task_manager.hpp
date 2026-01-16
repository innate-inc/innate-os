#ifndef MANIPULATION_TASK_MANAGER_HPP_
#define MANIPULATION_TASK_MANAGER_HPP_

#include <string>
#include <vector>
#include <map>
#include <optional>
#include <nlohmann/json.hpp>

#include "manipulation/episode_data.hpp"

namespace manipulation {

class TaskManager {
public:
    explicit TaskManager(const std::string& base_data_directory);

    void start_new_task(const std::string& task_name, double data_frequency, 
                        const std::string& primitive_type = "learned");
    void resume_task(const std::string& task_name);
    void add_episode(EpisodeData& episode_data, 
                     const std::string& start_timestamp, 
                     const std::string& end_timestamp);
    void end_task();

    // Metadata accessors
    std::vector<nlohmann::json> get_all_tasks_summary();
    std::tuple<bool, std::string, std::string> get_task_metadata_by_directory(const std::string& task_directory);
    std::tuple<bool, std::string> update_task_metadata_by_directory(const std::string& task_directory, 
                                                                     const std::string& json_metadata_update);

    // Accessors
    const std::string& get_current_task_name() const { return current_task_name_; }
    const nlohmann::json& get_metadata() const { return metadata_; }
    bool has_metadata() const { return !metadata_.is_null(); }
    int get_number_of_episodes() const;

private:
    void save_metadata();
    void load_metadata();
    void create_primitive_metadata(const std::string& task_name, const std::string& primitive_type);
    std::optional<nlohmann::json> get_enriched_metadata_for_task(const std::string& task_directory, 
                                                                   std::string& error_msg);

    std::string base_data_directory_;
    std::string current_task_name_;
    std::string current_task_dir_;
    nlohmann::json metadata_;
};

}  // namespace manipulation

#endif  // MANIPULATION_TASK_MANAGER_HPP_
