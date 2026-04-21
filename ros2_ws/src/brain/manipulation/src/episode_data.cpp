#include "manipulation/episode_data.hpp"

#include <algorithm>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <system_error>

namespace manipulation {

namespace {

// Throws std::runtime_error if `result` is a negative HDF5 status/identifier.
// Use for any H5* call in the streaming hot path so silent data loss is
// impossible (disk full, EIO, invalid handle, etc. all surface as exceptions).
inline void h5_check(long long result, const std::string& what) {
    if (result < 0) {
        throw std::runtime_error("HDF5 error in " + what);
    }
}

// Wrapper around H5Dcreate2 that creates a chunked, extendable dataset with
// auto-created intermediate groups. Returns the dataset hid.
hid_t create_chunked_dataset(hid_t file, const std::string& path, hid_t dtype,
                             int rank, const hsize_t* init_dims,
                             const hsize_t* max_dims, const hsize_t* chunk_dims) {
    hid_t fspace = H5Screate_simple(rank, init_dims, max_dims);
    hid_t dcpl = H5Pcreate(H5P_DATASET_CREATE);
    H5Pset_chunk(dcpl, rank, chunk_dims);
    hid_t lcpl = H5Pcreate(H5P_LINK_CREATE);
    H5Pset_create_intermediate_group(lcpl, 1);
    hid_t dset = H5Dcreate2(file, path.c_str(), dtype, fspace, lcpl, dcpl, H5P_DEFAULT);
    H5Pclose(lcpl);
    H5Pclose(dcpl);
    H5Sclose(fspace);
    if (dset < 0) {
        throw std::runtime_error("Failed to create HDF5 dataset: " + path);
    }
    return dset;
}

}  // namespace

EpisodeData::EpisodeData()
    : file_path_(),
      file_created_(false),
      timestep_count_(0),
      camera_names_set_(false),
      img_h_(0),
      img_w_(0),
      img_c_(0),
      action_dim_(0),
      qpos_dim_(0),
      qvel_dim_(0),
      file_id_(-1),
      action_dset_(-1),
      qpos_dset_(-1),
      qvel_dset_(-1),
      arm_ts_dset_(-1) {}

EpisodeData::EpisodeData(const std::vector<std::string>& camera_names)
    : EpisodeData() {
    camera_names_ = camera_names;
    camera_names_set_ = true;
}

EpisodeData::~EpisodeData() {
    close_handles();
}

void EpisodeData::steal_from(EpisodeData& other) noexcept {
    file_path_ = std::move(other.file_path_);
    file_created_ = other.file_created_;
    timestep_count_ = other.timestep_count_;
    camera_names_ = std::move(other.camera_names_);
    camera_names_set_ = other.camera_names_set_;
    img_h_ = other.img_h_;
    img_w_ = other.img_w_;
    img_c_ = other.img_c_;
    action_dim_ = other.action_dim_;
    qpos_dim_ = other.qpos_dim_;
    qvel_dim_ = other.qvel_dim_;
    file_id_ = other.file_id_;
    action_dset_ = other.action_dset_;
    qpos_dset_ = other.qpos_dset_;
    qvel_dset_ = other.qvel_dset_;
    arm_ts_dset_ = other.arm_ts_dset_;
    image_dsets_ = std::move(other.image_dsets_);
    image_ts_dsets_ = std::move(other.image_ts_dsets_);

    other.file_created_ = false;
    other.timestep_count_ = 0;
    other.file_id_ = -1;
    other.action_dset_ = -1;
    other.qpos_dset_ = -1;
    other.qvel_dset_ = -1;
    other.arm_ts_dset_ = -1;
}

EpisodeData::EpisodeData(EpisodeData&& other) noexcept : EpisodeData() {
    steal_from(other);
}

EpisodeData& EpisodeData::operator=(EpisodeData&& other) noexcept {
    if (this != &other) {
        close_handles();
        steal_from(other);
    }
    return *this;
}

void EpisodeData::close_handles() {
    auto safe_close = [](hid_t& h) {
        if (h >= 0) {
            H5Dclose(h);
            h = -1;
        }
    };
    safe_close(action_dset_);
    safe_close(qpos_dset_);
    safe_close(qvel_dset_);
    safe_close(arm_ts_dset_);
    for (auto& [name, h] : image_dsets_) {
        if (h >= 0) {
            H5Dclose(h);
            h = -1;
        }
    }
    for (auto& [name, h] : image_ts_dsets_) {
        if (h >= 0) {
            H5Dclose(h);
            h = -1;
        }
    }
    image_dsets_.clear();
    image_ts_dsets_.clear();
    if (file_id_ >= 0) {
        H5Fclose(file_id_);
        file_id_ = -1;
    }
}

void EpisodeData::open_file(const std::string& path) {
    if (file_id_ >= 0 || file_created_) {
        throw std::runtime_error("EpisodeData::open_file: episode already open");
    }
    file_path_ = path;
    file_created_ = false;
    timestep_count_ = 0;
}

void EpisodeData::create_file_and_datasets(
    const std::vector<double>& action,
    const std::vector<double>& qpos,
    const std::vector<double>& qvel,
    const std::vector<cv::Mat>& images) {

    if (file_path_.empty()) {
        throw std::runtime_error("EpisodeData: open_file() must be called before add_timestep()");
    }

    action_dim_ = action.size();
    qpos_dim_ = qpos.size();
    qvel_dim_ = qvel.size();

    if (!camera_names_set_) {
        for (size_t i = 0; i < images.size(); ++i) {
            camera_names_.push_back("camera_" + std::to_string(i + 1));
        }
        camera_names_set_ = true;
    } else if (images.size() != camera_names_.size()) {
        throw std::runtime_error(
            "EpisodeData: expected " + std::to_string(camera_names_.size()) +
            " images, but got " + std::to_string(images.size()));
    }

    if (!images.empty()) {
        img_h_ = images[0].rows;
        img_w_ = images[0].cols;
        img_c_ = images[0].channels();
    }

    // Make sure the parent directory exists; create the file (truncate any partial leftover).
    std::filesystem::path p(file_path_);
    if (p.has_parent_path()) {
        std::error_code ec;
        std::filesystem::create_directories(p.parent_path(), ec);
    }

    file_id_ = H5Fcreate(file_path_.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
    if (file_id_ < 0) {
        throw std::runtime_error("Failed to create HDF5 file: " + file_path_);
    }

    // /action: width = action_dim + 2 trailing termination columns. Rows are
    // chunked at 30 (i.e. ~1s @ 30Hz) since each row is tiny.
    {
        const hsize_t init_dims[2] = {0, action_dim_ + 2};
        const hsize_t max_dims[2] = {H5S_UNLIMITED, action_dim_ + 2};
        const hsize_t chunk_dims[2] = {30, action_dim_ + 2};
        action_dset_ = create_chunked_dataset(file_id_, "/action", H5T_NATIVE_DOUBLE,
                                              2, init_dims, max_dims, chunk_dims);
    }

    // /timestamps/arm
    {
        const hsize_t init_dims[1] = {0};
        const hsize_t max_dims[1] = {H5S_UNLIMITED};
        const hsize_t chunk_dims[1] = {30};
        arm_ts_dset_ = create_chunked_dataset(file_id_, "/timestamps/arm", H5T_NATIVE_DOUBLE,
                                              1, init_dims, max_dims, chunk_dims);
    }

    if (qpos_dim_ > 0) {
        const hsize_t init_dims[2] = {0, qpos_dim_};
        const hsize_t max_dims[2] = {H5S_UNLIMITED, qpos_dim_};
        const hsize_t chunk_dims[2] = {30, qpos_dim_};
        qpos_dset_ = create_chunked_dataset(file_id_, "/observations/qpos", H5T_NATIVE_DOUBLE,
                                            2, init_dims, max_dims, chunk_dims);
    }

    if (qvel_dim_ > 0) {
        const hsize_t init_dims[2] = {0, qvel_dim_};
        const hsize_t max_dims[2] = {H5S_UNLIMITED, qvel_dim_};
        const hsize_t chunk_dims[2] = {30, qvel_dim_};
        qvel_dset_ = create_chunked_dataset(file_id_, "/observations/qvel", H5T_NATIVE_DOUBLE,
                                            2, init_dims, max_dims, chunk_dims);
    }

    // Per-camera image and timestamp datasets. One image per chunk keeps each
    // chunk ~900KB which is a sensible HDF5 unit (avoids both tiny chunks and
    // multi-MB chunks that hurt read locality).
    for (const auto& cam : camera_names_) {
        const std::string img_path = "/observations/images/" + cam;
        const hsize_t img_init[4] = {0, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
        const hsize_t img_max[4] = {H5S_UNLIMITED, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
        const hsize_t img_chunk[4] = {1, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
        image_dsets_[cam] = create_chunked_dataset(file_id_, img_path, H5T_NATIVE_UINT8,
                                                   4, img_init, img_max, img_chunk);

        const std::string ts_path = "/timestamps/images/" + cam;
        const hsize_t ts_init[1] = {0};
        const hsize_t ts_max[1] = {H5S_UNLIMITED};
        const hsize_t ts_chunk[1] = {30};
        image_ts_dsets_[cam] = create_chunked_dataset(file_id_, ts_path, H5T_NATIVE_DOUBLE,
                                                      1, ts_init, ts_max, ts_chunk);
    }

    file_created_ = true;
}

void EpisodeData::add_timestep(
    const std::vector<double>& action,
    const std::vector<double>& qpos,
    const std::vector<double>& qvel,
    const std::vector<cv::Mat>& images,
    double arm_timestamp,
    const std::vector<double>& image_timestamps) {

    if (!file_created_) {
        create_file_and_datasets(action, qpos, qvel, images);
    } else if (images.size() != camera_names_.size()) {
        throw std::runtime_error(
            "EpisodeData: image count changed mid-episode (expected " +
            std::to_string(camera_names_.size()) + ", got " +
            std::to_string(images.size()) + ")");
    }

    const hsize_t t = static_cast<hsize_t>(timestep_count_);
    const hsize_t new_t = t + 1;

    auto write_2d_row = [&](hid_t dset, hsize_t width, const double* data,
                            const std::string& name) {
        const hsize_t new_extent[2] = {new_t, width};
        h5_check(H5Dset_extent(dset, new_extent), "H5Dset_extent(" + name + ")");

        hid_t fspace = H5Dget_space(dset);
        h5_check(fspace, "H5Dget_space(" + name + ")");
        const hsize_t start[2] = {t, 0};
        const hsize_t count[2] = {1, width};
        h5_check(H5Sselect_hyperslab(fspace, H5S_SELECT_SET, start, nullptr, count, nullptr),
                 "H5Sselect_hyperslab(" + name + ")");

        const hsize_t mem_dims[2] = {1, width};
        hid_t mspace = H5Screate_simple(2, mem_dims, nullptr);
        h5_check(mspace, "H5Screate_simple(" + name + " mem)");
        herr_t wr = H5Dwrite(dset, H5T_NATIVE_DOUBLE, mspace, fspace, H5P_DEFAULT, data);
        H5Sclose(mspace);
        H5Sclose(fspace);
        h5_check(wr, "H5Dwrite(" + name + ")");
    };

    auto write_1d_scalar = [&](hid_t dset, double value, const std::string& name) {
        const hsize_t new_extent[1] = {new_t};
        h5_check(H5Dset_extent(dset, new_extent), "H5Dset_extent(" + name + ")");

        hid_t fspace = H5Dget_space(dset);
        h5_check(fspace, "H5Dget_space(" + name + ")");
        const hsize_t start[1] = {t};
        const hsize_t count[1] = {1};
        h5_check(H5Sselect_hyperslab(fspace, H5S_SELECT_SET, start, nullptr, count, nullptr),
                 "H5Sselect_hyperslab(" + name + ")");

        const hsize_t mem_dims[1] = {1};
        hid_t mspace = H5Screate_simple(1, mem_dims, nullptr);
        h5_check(mspace, "H5Screate_simple(" + name + " mem)");
        herr_t wr = H5Dwrite(dset, H5T_NATIVE_DOUBLE, mspace, fspace, H5P_DEFAULT, &value);
        H5Sclose(mspace);
        H5Sclose(fspace);
        h5_check(wr, "H5Dwrite(" + name + ")");
    };

    try {
        // /action: write the first action_dim_ columns; trailing termination
        // columns stay 0 until finalize() rewrites them.
        {
            std::vector<double> row(action_dim_ + 2, 0.0);
            const size_t copy_n = std::min(action.size(), action_dim_);
            std::copy(action.begin(), action.begin() + copy_n, row.begin());
            write_2d_row(action_dset_, action_dim_ + 2, row.data(), "/action");
        }

        if (qpos_dset_ >= 0) {
            if (qpos.size() != qpos_dim_) {
                throw std::runtime_error("EpisodeData: qpos size changed mid-episode");
            }
            write_2d_row(qpos_dset_, qpos_dim_, qpos.data(), "/observations/qpos");
        }

        if (qvel_dset_ >= 0) {
            if (qvel.size() != qvel_dim_) {
                throw std::runtime_error("EpisodeData: qvel size changed mid-episode");
            }
            write_2d_row(qvel_dset_, qvel_dim_, qvel.data(), "/observations/qvel");
        }

        if (arm_ts_dset_ >= 0) {
            // Always extend so its row count matches timestep_count_; use 0.0
            // as a sentinel when no timestamp was given.
            write_1d_scalar(arm_ts_dset_,
                            arm_timestamp >= 0.0 ? arm_timestamp : 0.0,
                            "/timestamps/arm");
        }

        for (size_t c = 0; c < camera_names_.size(); ++c) {
            const auto& cam = camera_names_[c];
            const cv::Mat& img = images[c];
            if (img.rows != img_h_ || img.cols != img_w_ || img.channels() != img_c_) {
                throw std::runtime_error("EpisodeData: image shape changed mid-episode for " + cam);
            }
            cv::Mat continuous = img.isContinuous() ? img : img.clone();

            const std::string img_name = "/observations/images/" + cam;
            const hsize_t new_extent[4] = {new_t, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
            h5_check(H5Dset_extent(image_dsets_[cam], new_extent),
                     "H5Dset_extent(" + img_name + ")");

            hid_t fspace = H5Dget_space(image_dsets_[cam]);
            h5_check(fspace, "H5Dget_space(" + img_name + ")");
            const hsize_t start[4] = {t, 0, 0, 0};
            const hsize_t count[4] = {1, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
            h5_check(H5Sselect_hyperslab(fspace, H5S_SELECT_SET, start, nullptr, count, nullptr),
                     "H5Sselect_hyperslab(" + img_name + ")");

            const hsize_t mem_dims[4] = {1, (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
            hid_t mspace = H5Screate_simple(4, mem_dims, nullptr);
            if (mspace < 0) {
                H5Sclose(fspace);
                h5_check(mspace, "H5Screate_simple(" + img_name + " mem)");
            }
            herr_t wr = H5Dwrite(image_dsets_[cam], H5T_NATIVE_UINT8, mspace, fspace,
                                 H5P_DEFAULT, continuous.data);
            H5Sclose(mspace);
            H5Sclose(fspace);
            h5_check(wr, "H5Dwrite(" + img_name + ")");

            const double img_ts = (c < image_timestamps.size()) ? image_timestamps[c] : 0.0;
            write_1d_scalar(image_ts_dsets_[cam], img_ts, "/timestamps/images/" + cam);
        }
    } catch (...) {
        // Partial failure: some datasets may have been extended+written while
        // others didn't. Shrink every dataset back to the last good timestep
        // count so row counts stay consistent and finalize() works. Rollback
        // is best-effort — we rethrow whatever triggered this path.
        truncate_datasets_to(timestep_count_);
        throw;
    }

    timestep_count_++;
}

void EpisodeData::truncate_datasets_to(size_t rows) noexcept {
    auto shrink_1d = [&](hid_t dset) {
        if (dset < 0) return;
        const hsize_t extent[1] = {static_cast<hsize_t>(rows)};
        H5Dset_extent(dset, extent);
    };
    auto shrink_2d = [&](hid_t dset, hsize_t width) {
        if (dset < 0) return;
        const hsize_t extent[2] = {static_cast<hsize_t>(rows), width};
        H5Dset_extent(dset, extent);
    };

    shrink_2d(action_dset_, action_dim_ + 2);
    if (qpos_dset_ >= 0) shrink_2d(qpos_dset_, qpos_dim_);
    if (qvel_dset_ >= 0) shrink_2d(qvel_dset_, qvel_dim_);
    shrink_1d(arm_ts_dset_);
    for (auto& [name, dset] : image_dsets_) {
        if (dset < 0) continue;
        const hsize_t extent[4] = {static_cast<hsize_t>(rows),
                                   (hsize_t)img_h_, (hsize_t)img_w_, (hsize_t)img_c_};
        H5Dset_extent(dset, extent);
    }
    for (auto& [name, dset] : image_ts_dsets_) {
        shrink_1d(dset);
    }
}

void EpisodeData::finalize() {
    if (!file_created_ || timestep_count_ == 0) {
        // Nothing useful was written; treat as cancel.
        cancel();
        return;
    }

    const size_t T = timestep_count_;
    std::vector<double> term(T * 2, 0.0);
    for (size_t i = 0; i < T; ++i) {
        const double linear = (T > 1) ? static_cast<double>(i) / static_cast<double>(T - 1) : 0.0;
        double termination = 0.0;
        if (T >= 10) {
            termination = (i >= T - 10) ? 1.0 : 0.0;
        } else {
            termination = 1.0;
        }
        term[i * 2 + 0] = linear;
        term[i * 2 + 1] = termination;
    }

    hid_t fspace = H5Dget_space(action_dset_);
    h5_check(fspace, "H5Dget_space(/action finalize)");
    const hsize_t start[2] = {0, action_dim_};
    const hsize_t count[2] = {static_cast<hsize_t>(T), 2};
    h5_check(H5Sselect_hyperslab(fspace, H5S_SELECT_SET, start, nullptr, count, nullptr),
             "H5Sselect_hyperslab(/action finalize)");

    const hsize_t mem_dims[2] = {static_cast<hsize_t>(T), 2};
    hid_t mspace = H5Screate_simple(2, mem_dims, nullptr);
    if (mspace < 0) {
        H5Sclose(fspace);
        h5_check(mspace, "H5Screate_simple(/action finalize mem)");
    }
    herr_t wr = H5Dwrite(action_dset_, H5T_NATIVE_DOUBLE, mspace, fspace,
                         H5P_DEFAULT, term.data());
    H5Sclose(mspace);
    H5Sclose(fspace);
    h5_check(wr, "H5Dwrite(/action finalize)");

    close_handles();
}

void EpisodeData::cancel() {
    close_handles();
    if (!file_path_.empty()) {
        std::error_code ec;
        std::filesystem::remove(file_path_, ec);
    }
    file_path_.clear();
    file_created_ = false;
    timestep_count_ = 0;
}

}  // namespace manipulation
