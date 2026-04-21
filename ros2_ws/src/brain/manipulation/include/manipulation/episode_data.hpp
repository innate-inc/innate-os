#ifndef MANIPULATION_EPISODE_DATA_HPP_
#define MANIPULATION_EPISODE_DATA_HPP_

#include <map>
#include <string>
#include <vector>

#include <hdf5.h>
#include <opencv2/core.hpp>

namespace manipulation {

// Streams episode samples directly to an HDF5 file as they arrive instead
// of buffering everything in RAM. The file is created lazily on the first
// add_timestep() call, once payload shapes are known.
//
// Lifecycle:
//   1. EpisodeData ed;                              // construct (no IO)
//   2. ed.open_file("/path/to/file.h5.tmp");        // remember target path
//   3. ed.add_timestep(...) repeatedly              // first call creates file/datasets
//   4a. ed.finalize();                              // writes termination cols, closes file
//   4b. ed.cancel();                                // closes file and deletes it
//
// Move-only: HDF5 handles must not be duplicated.
class EpisodeData {
public:
    EpisodeData();
    explicit EpisodeData(const std::vector<std::string>& camera_names);
    ~EpisodeData();

    EpisodeData(const EpisodeData&) = delete;
    EpisodeData& operator=(const EpisodeData&) = delete;
    EpisodeData(EpisodeData&& other) noexcept;
    EpisodeData& operator=(EpisodeData&& other) noexcept;

    // Set the output path. Does not touch disk yet.
    void open_file(const std::string& path);

    // Append one timestep to the file. On the first call we create the
    // HDF5 file and all chunked extendable datasets sized from the inputs.
    void add_timestep(
        const std::vector<double>& action,
        const std::vector<double>& qpos,
        const std::vector<double>& qvel,
        const std::vector<cv::Mat>& images,
        double arm_timestamp = -1.0,
        const std::vector<double>& image_timestamps = {});

    // Write the termination columns of /action, then close the file.
    // Safe to call when no timesteps were written; in that case behaves
    // like cancel() (since an empty file is useless).
    void finalize();

    // Close any open handles and delete the file on disk.
    void cancel();

    size_t get_episode_length() const { return timestep_count_; }
    bool is_open() const { return file_id_ >= 0; }
    const std::string& get_file_path() const { return file_path_; }

private:
    void create_file_and_datasets(
        const std::vector<double>& action,
        const std::vector<double>& qpos,
        const std::vector<double>& qvel,
        const std::vector<cv::Mat>& images);
    void close_handles();
    void steal_from(EpisodeData& other) noexcept;

    std::string file_path_;
    bool file_created_;
    size_t timestep_count_;

    std::vector<std::string> camera_names_;
    bool camera_names_set_;

    int img_h_;
    int img_w_;
    int img_c_;
    size_t action_dim_;  // not including +2 termination columns
    size_t qpos_dim_;
    size_t qvel_dim_;

    hid_t file_id_;
    hid_t action_dset_;
    hid_t qpos_dset_;
    hid_t qvel_dset_;
    hid_t arm_ts_dset_;
    std::map<std::string, hid_t> image_dsets_;
    std::map<std::string, hid_t> image_ts_dsets_;
};

}  // namespace manipulation

#endif  // MANIPULATION_EPISODE_DATA_HPP_
