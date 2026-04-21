#include <hdf5.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <numeric>
#include <random>
#include <string>
#include <vector>

#include <fcntl.h>
#include <sys/resource.h>
#include <unistd.h>

// Match the real recorder parameters
static constexpr int IMG_H        = 480;
static constexpr int IMG_W        = 640;
static constexpr int IMG_C        = 3;
static constexpr size_t IMG_BYTES = IMG_H * IMG_W * IMG_C;  // 921,600
static constexpr int NUM_CAMERAS  = 2;
static constexpr int ACTION_DIM   = 12;
static constexpr int QPOS_DIM     = 7;
static constexpr int QVEL_DIM     = 7;
static constexpr double TARGET_HZ = 30.0;

struct TimingResult {
    std::string name;
    int num_timesteps;
    std::vector<double> latencies_us;
    double total_wall_s;
    double cpu_user_s;
    double cpu_sys_s;
    size_t file_size_bytes;
};

static double rusage_user(const struct rusage& r) {
    return r.ru_utime.tv_sec + r.ru_utime.tv_usec * 1e-6;
}

static double rusage_sys(const struct rusage& r) {
    return r.ru_stime.tv_sec + r.ru_stime.tv_usec * 1e-6;
}

static size_t file_size(const std::string& path) {
    try {
        return std::filesystem::file_size(path);
    } catch (...) {
        return 0;
    }
}

static void print_report(const TimingResult& r) {
    auto lat = r.latencies_us;
    std::sort(lat.begin(), lat.end());
    size_t n = lat.size();

    double mean = std::accumulate(lat.begin(), lat.end(), 0.0) / n;
    double p50  = lat[n / 2];
    double p95  = lat[(size_t)(n * 0.95)];
    double p99  = lat[(size_t)(n * 0.99)];
    double pmax = lat.back();

    double ideal_s   = r.num_timesteps / TARGET_HZ;
    double budget_us = 1e6 / TARGET_HZ;
    double cpu_total = r.cpu_user_s + r.cpu_sys_s;
    double cpu_pct   = (cpu_total / r.total_wall_s) * 100.0;

    double size_mb = r.file_size_bytes / (1024.0 * 1024.0);
    double throughput_mbs = (size_mb / r.total_wall_s);

    printf("\n=== %s ===\n", r.name.c_str());
    printf("  Timesteps: %d   Target: %.0f Hz (%.1f us budget/step)\n",
           r.num_timesteps, TARGET_HZ, budget_us);
    printf("  Wall time: %.3f s  (ideal: %.3f s)  ratio: %.2fx\n",
           r.total_wall_s, ideal_s, r.total_wall_s / ideal_s);
    printf("  Latency (us):  mean=%.0f  p50=%.0f  p95=%.0f  p99=%.0f  max=%.0f\n",
           mean, p50, p95, p99, pmax);
    printf("  CPU time:  user=%.3f s  sys=%.3f s  total=%.3f s  (%.1f%% of 1 core)\n",
           r.cpu_user_s, r.cpu_sys_s, cpu_total, cpu_pct);
    printf("  File size: %.1f MB   Throughput: %.1f MB/s\n", size_mb, throughput_mbs);
    printf("  Keeps up with 30 Hz: %s\n", (mean < budget_us && p99 < budget_us * 3) ? "YES" : "NO (or marginal)");
}

// ---------------------------------------------------------------------------
// Strategy 1: Raw write() baseline
// ---------------------------------------------------------------------------
static TimingResult bench_raw_write(int N, const std::vector<uint8_t>& img_buf,
                                    const double* action, const double* qpos,
                                    const double* qvel, const std::string& dir) {
    TimingResult result;
    result.name = "Raw write()";
    result.num_timesteps = N;
    result.latencies_us.reserve(N);

    std::string path = dir + "/raw_bench.bin";
    int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { perror("open"); std::exit(1); }

    struct rusage ru_start, ru_end;
    getrusage(RUSAGE_SELF, &ru_start);
    auto t0 = std::chrono::steady_clock::now();

    for (int t = 0; t < N; ++t) {
        auto ts = std::chrono::steady_clock::now();

        for (int c = 0; c < NUM_CAMERAS; ++c)
            write(fd, img_buf.data(), IMG_BYTES);
        write(fd, action, ACTION_DIM * sizeof(double));
        write(fd, qpos, QPOS_DIM * sizeof(double));
        write(fd, qvel, QVEL_DIM * sizeof(double));

        auto te = std::chrono::steady_clock::now();
        result.latencies_us.push_back(
            std::chrono::duration<double, std::micro>(te - ts).count());
    }
    fsync(fd);

    auto t1 = std::chrono::steady_clock::now();
    getrusage(RUSAGE_SELF, &ru_end);

    result.total_wall_s = std::chrono::duration<double>(t1 - t0).count();
    result.cpu_user_s   = rusage_user(ru_end) - rusage_user(ru_start);
    result.cpu_sys_s    = rusage_sys(ru_end)  - rusage_sys(ru_start);
    result.file_size_bytes = file_size(path);

    close(fd);
    return result;
}

// ---------------------------------------------------------------------------
// Strategy 2: HDF5 contiguous (pre-allocated), hyperslab per timestep
// ---------------------------------------------------------------------------
static TimingResult bench_h5_contiguous(int N, const std::vector<uint8_t>& img_buf,
                                        const double* action, const double* qpos,
                                        const double* qvel, const std::string& dir) {
    TimingResult result;
    result.name = "HDF5 contiguous (pre-alloc)";
    result.num_timesteps = N;
    result.latencies_us.reserve(N);

    std::string path = dir + "/bench_contiguous.h5";
    hid_t file = H5Fcreate(path.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);

    // Pre-allocate image datasets [N, H, W, C]
    hsize_t img_dims[4] = {(hsize_t)N, IMG_H, IMG_W, IMG_C};
    hid_t img_spaces[NUM_CAMERAS], img_dsets[NUM_CAMERAS];
    hid_t img_grp = H5Gcreate2(file, "/observations", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    hid_t img_sub = H5Gcreate2(img_grp, "images", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

    for (int c = 0; c < NUM_CAMERAS; ++c) {
        std::string name = "camera_" + std::to_string(c + 1);
        img_spaces[c] = H5Screate_simple(4, img_dims, nullptr);
        img_dsets[c] = H5Dcreate2(img_sub, name.c_str(), H5T_NATIVE_UINT8,
                                   img_spaces[c], H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    }

    // Pre-allocate numeric datasets [N, dim]
    hsize_t act_dims[2] = {(hsize_t)N, ACTION_DIM};
    hid_t act_space = H5Screate_simple(2, act_dims, nullptr);
    hid_t act_dset  = H5Dcreate2(file, "/action", H5T_NATIVE_DOUBLE,
                                  act_space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

    hsize_t qp_dims[2] = {(hsize_t)N, QPOS_DIM};
    hid_t qp_space = H5Screate_simple(2, qp_dims, nullptr);
    hid_t qp_dset  = H5Dcreate2(img_grp, "qpos", H5T_NATIVE_DOUBLE,
                                  qp_space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

    hsize_t qv_dims[2] = {(hsize_t)N, QVEL_DIM};
    hid_t qv_space = H5Screate_simple(2, qv_dims, nullptr);
    hid_t qv_dset  = H5Dcreate2(img_grp, "qvel", H5T_NATIVE_DOUBLE,
                                  qv_space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

    // Mem spaces for a single timestep
    hsize_t one_img[4] = {1, IMG_H, IMG_W, IMG_C};
    hid_t mem_img = H5Screate_simple(4, one_img, nullptr);

    hsize_t one_act[2] = {1, ACTION_DIM};
    hid_t mem_act = H5Screate_simple(2, one_act, nullptr);

    hsize_t one_qp[2] = {1, QPOS_DIM};
    hid_t mem_qp = H5Screate_simple(2, one_qp, nullptr);

    hsize_t one_qv[2] = {1, QVEL_DIM};
    hid_t mem_qv = H5Screate_simple(2, one_qv, nullptr);

    struct rusage ru_start, ru_end;
    getrusage(RUSAGE_SELF, &ru_start);
    auto t0 = std::chrono::steady_clock::now();

    for (int t = 0; t < N; ++t) {
        auto ts = std::chrono::steady_clock::now();

        hsize_t img_start[4] = {(hsize_t)t, 0, 0, 0};
        hsize_t img_count[4] = {1, IMG_H, IMG_W, IMG_C};

        for (int c = 0; c < NUM_CAMERAS; ++c) {
            hid_t fspace = H5Dget_space(img_dsets[c]);
            H5Sselect_hyperslab(fspace, H5S_SELECT_SET, img_start, nullptr, img_count, nullptr);
            H5Dwrite(img_dsets[c], H5T_NATIVE_UINT8, mem_img, fspace, H5P_DEFAULT, img_buf.data());
            H5Sclose(fspace);
        }

        hsize_t act_start[2] = {(hsize_t)t, 0};
        hsize_t act_count[2] = {1, ACTION_DIM};
        hid_t fs_act = H5Dget_space(act_dset);
        H5Sselect_hyperslab(fs_act, H5S_SELECT_SET, act_start, nullptr, act_count, nullptr);
        H5Dwrite(act_dset, H5T_NATIVE_DOUBLE, mem_act, fs_act, H5P_DEFAULT, action);
        H5Sclose(fs_act);

        hsize_t qp_start[2] = {(hsize_t)t, 0};
        hsize_t qp_count[2] = {1, QPOS_DIM};
        hid_t fs_qp = H5Dget_space(qp_dset);
        H5Sselect_hyperslab(fs_qp, H5S_SELECT_SET, qp_start, nullptr, qp_count, nullptr);
        H5Dwrite(qp_dset, H5T_NATIVE_DOUBLE, mem_qp, fs_qp, H5P_DEFAULT, qpos);
        H5Sclose(fs_qp);

        hsize_t qv_start[2] = {(hsize_t)t, 0};
        hsize_t qv_count[2] = {1, QVEL_DIM};
        hid_t fs_qv = H5Dget_space(qv_dset);
        H5Sselect_hyperslab(fs_qv, H5S_SELECT_SET, qv_start, nullptr, qv_count, nullptr);
        H5Dwrite(qv_dset, H5T_NATIVE_DOUBLE, mem_qv, fs_qv, H5P_DEFAULT, qvel);
        H5Sclose(fs_qv);

        auto te = std::chrono::steady_clock::now();
        result.latencies_us.push_back(
            std::chrono::duration<double, std::micro>(te - ts).count());
    }

    H5Fflush(file, H5F_SCOPE_GLOBAL);
    auto t1 = std::chrono::steady_clock::now();
    getrusage(RUSAGE_SELF, &ru_end);

    result.total_wall_s = std::chrono::duration<double>(t1 - t0).count();
    result.cpu_user_s   = rusage_user(ru_end) - rusage_user(ru_start);
    result.cpu_sys_s    = rusage_sys(ru_end)  - rusage_sys(ru_start);

    // Cleanup
    H5Sclose(mem_img); H5Sclose(mem_act); H5Sclose(mem_qp); H5Sclose(mem_qv);
    for (int c = 0; c < NUM_CAMERAS; ++c) { H5Dclose(img_dsets[c]); H5Sclose(img_spaces[c]); }
    H5Dclose(act_dset); H5Sclose(act_space);
    H5Dclose(qp_dset);  H5Sclose(qp_space);
    H5Dclose(qv_dset);  H5Sclose(qv_space);
    H5Gclose(img_sub); H5Gclose(img_grp);
    H5Fclose(file);

    result.file_size_bytes = file_size(path);
    return result;
}

// ---------------------------------------------------------------------------
// Strategy 3 & 4: HDF5 chunked (extendable), optionally with gzip
// ---------------------------------------------------------------------------
static TimingResult bench_h5_chunked(int N, const std::vector<uint8_t>& img_buf,
                                     const double* action, const double* qpos,
                                     const double* qvel, const std::string& dir,
                                     bool use_gzip) {
    TimingResult result;
    result.name = use_gzip ? "HDF5 chunked + gzip" : "HDF5 chunked (extendable)";
    result.num_timesteps = N;
    result.latencies_us.reserve(N);

    std::string path = dir + (use_gzip ? "/bench_chunked_gzip.h5" : "/bench_chunked.h5");
    hid_t file = H5Fcreate(path.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);

    // Initial dims: 0 along time axis; max: unlimited
    hsize_t img_init[4] = {0, IMG_H, IMG_W, IMG_C};
    hsize_t img_max[4]  = {H5S_UNLIMITED, IMG_H, IMG_W, IMG_C};

    hsize_t act_init[2] = {0, ACTION_DIM};
    hsize_t act_max[2]  = {H5S_UNLIMITED, ACTION_DIM};

    hsize_t qp_init[2] = {0, QPOS_DIM};
    hsize_t qp_max[2]  = {H5S_UNLIMITED, QPOS_DIM};

    hsize_t qv_init[2] = {0, QVEL_DIM};
    hsize_t qv_max[2]  = {H5S_UNLIMITED, QVEL_DIM};

    // Chunk sizes: 1 timestep per chunk for images (each chunk ~900 KB),
    // 30 timesteps for numerics (tiny data, amortize metadata)
    hsize_t img_chunk[4] = {1, IMG_H, IMG_W, IMG_C};
    hsize_t act_chunk[2] = {30, ACTION_DIM};
    hsize_t qp_chunk[2]  = {30, QPOS_DIM};
    hsize_t qv_chunk[2]  = {30, QVEL_DIM};

    auto make_dcpl = [&](const hsize_t* chunk, int rank) {
        hid_t dcpl = H5Pcreate(H5P_DATASET_CREATE);
        H5Pset_chunk(dcpl, rank, chunk);
        if (use_gzip) H5Pset_deflate(dcpl, 1);  // level 1 = fast
        return dcpl;
    };

    hid_t img_grp = H5Gcreate2(file, "/observations", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    hid_t img_sub = H5Gcreate2(img_grp, "images", H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);

    hid_t img_fspaces[NUM_CAMERAS], img_dsets[NUM_CAMERAS];
    for (int c = 0; c < NUM_CAMERAS; ++c) {
        std::string name = "camera_" + std::to_string(c + 1);
        img_fspaces[c] = H5Screate_simple(4, img_init, img_max);
        hid_t dcpl = make_dcpl(img_chunk, 4);
        img_dsets[c] = H5Dcreate2(img_sub, name.c_str(), H5T_NATIVE_UINT8,
                                   img_fspaces[c], H5P_DEFAULT, dcpl, H5P_DEFAULT);
        H5Pclose(dcpl);
    }

    hid_t act_fspace = H5Screate_simple(2, act_init, act_max);
    hid_t dcpl_act = make_dcpl(act_chunk, 2);
    hid_t act_dset = H5Dcreate2(file, "/action", H5T_NATIVE_DOUBLE,
                                 act_fspace, H5P_DEFAULT, dcpl_act, H5P_DEFAULT);
    H5Pclose(dcpl_act);

    hid_t qp_fspace = H5Screate_simple(2, qp_init, qp_max);
    hid_t dcpl_qp = make_dcpl(qp_chunk, 2);
    hid_t qp_dset = H5Dcreate2(img_grp, "qpos", H5T_NATIVE_DOUBLE,
                                 qp_fspace, H5P_DEFAULT, dcpl_qp, H5P_DEFAULT);
    H5Pclose(dcpl_qp);

    hid_t qv_fspace = H5Screate_simple(2, qv_init, qv_max);
    hid_t dcpl_qv = make_dcpl(qv_chunk, 2);
    hid_t qv_dset = H5Dcreate2(img_grp, "qvel", H5T_NATIVE_DOUBLE,
                                 qv_fspace, H5P_DEFAULT, dcpl_qv, H5P_DEFAULT);
    H5Pclose(dcpl_qv);

    // Mem spaces for single-timestep writes
    hsize_t one_img[4] = {1, IMG_H, IMG_W, IMG_C};
    hid_t mem_img = H5Screate_simple(4, one_img, nullptr);

    hsize_t one_act[2] = {1, ACTION_DIM};
    hid_t mem_act = H5Screate_simple(2, one_act, nullptr);

    hsize_t one_qp[2] = {1, QPOS_DIM};
    hid_t mem_qp = H5Screate_simple(2, one_qp, nullptr);

    hsize_t one_qv[2] = {1, QVEL_DIM};
    hid_t mem_qv = H5Screate_simple(2, one_qv, nullptr);

    struct rusage ru_start, ru_end;
    getrusage(RUSAGE_SELF, &ru_start);
    auto t0 = std::chrono::steady_clock::now();

    for (int t = 0; t < N; ++t) {
        auto ts = std::chrono::steady_clock::now();
        hsize_t new_t = (hsize_t)(t + 1);

        // Extend and write images
        hsize_t img_ext[4] = {new_t, IMG_H, IMG_W, IMG_C};
        hsize_t img_start[4] = {(hsize_t)t, 0, 0, 0};
        hsize_t img_count[4] = {1, IMG_H, IMG_W, IMG_C};

        for (int c = 0; c < NUM_CAMERAS; ++c) {
            H5Dset_extent(img_dsets[c], img_ext);
            hid_t fsp = H5Dget_space(img_dsets[c]);
            H5Sselect_hyperslab(fsp, H5S_SELECT_SET, img_start, nullptr, img_count, nullptr);
            H5Dwrite(img_dsets[c], H5T_NATIVE_UINT8, mem_img, fsp, H5P_DEFAULT, img_buf.data());
            H5Sclose(fsp);
        }

        // Extend and write action
        hsize_t act_ext[2] = {new_t, ACTION_DIM};
        hsize_t act_start[2] = {(hsize_t)t, 0};
        hsize_t act_count[2] = {1, ACTION_DIM};
        H5Dset_extent(act_dset, act_ext);
        hid_t fs_a = H5Dget_space(act_dset);
        H5Sselect_hyperslab(fs_a, H5S_SELECT_SET, act_start, nullptr, act_count, nullptr);
        H5Dwrite(act_dset, H5T_NATIVE_DOUBLE, mem_act, fs_a, H5P_DEFAULT, action);
        H5Sclose(fs_a);

        // Extend and write qpos
        hsize_t qp_ext[2] = {new_t, QPOS_DIM};
        hsize_t qp_start[2] = {(hsize_t)t, 0};
        hsize_t qp_count[2] = {1, QPOS_DIM};
        H5Dset_extent(qp_dset, qp_ext);
        hid_t fs_qp = H5Dget_space(qp_dset);
        H5Sselect_hyperslab(fs_qp, H5S_SELECT_SET, qp_start, nullptr, qp_count, nullptr);
        H5Dwrite(qp_dset, H5T_NATIVE_DOUBLE, mem_qp, fs_qp, H5P_DEFAULT, qpos);
        H5Sclose(fs_qp);

        // Extend and write qvel
        hsize_t qv_ext[2] = {new_t, QVEL_DIM};
        hsize_t qv_start[2] = {(hsize_t)t, 0};
        hsize_t qv_count[2] = {1, QVEL_DIM};
        H5Dset_extent(qv_dset, qv_ext);
        hid_t fs_qv = H5Dget_space(qv_dset);
        H5Sselect_hyperslab(fs_qv, H5S_SELECT_SET, qv_start, nullptr, qv_count, nullptr);
        H5Dwrite(qv_dset, H5T_NATIVE_DOUBLE, mem_qv, fs_qv, H5P_DEFAULT, qvel);
        H5Sclose(fs_qv);

        auto te = std::chrono::steady_clock::now();
        result.latencies_us.push_back(
            std::chrono::duration<double, std::micro>(te - ts).count());
    }

    H5Fflush(file, H5F_SCOPE_GLOBAL);
    auto t1 = std::chrono::steady_clock::now();
    getrusage(RUSAGE_SELF, &ru_end);

    result.total_wall_s = std::chrono::duration<double>(t1 - t0).count();
    result.cpu_user_s   = rusage_user(ru_end) - rusage_user(ru_start);
    result.cpu_sys_s    = rusage_sys(ru_end)  - rusage_sys(ru_start);

    // Cleanup
    H5Sclose(mem_img); H5Sclose(mem_act); H5Sclose(mem_qp); H5Sclose(mem_qv);
    for (int c = 0; c < NUM_CAMERAS; ++c) { H5Dclose(img_dsets[c]); H5Sclose(img_fspaces[c]); }
    H5Dclose(act_dset); H5Sclose(act_fspace);
    H5Dclose(qp_dset);  H5Sclose(qp_fspace);
    H5Dclose(qv_dset);  H5Sclose(qv_fspace);
    H5Gclose(img_sub); H5Gclose(img_grp);
    H5Fclose(file);

    result.file_size_bytes = file_size(path);
    return result;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
int main(int argc, char** argv) {
    int N = 1800;  // default: 60s at 30 Hz
    if (argc > 1) N = std::atoi(argv[1]);
    if (N <= 0) N = 1800;

    printf("HDF5 Streaming Benchmark\n");
    printf("========================\n");
    printf("Timesteps: %d  (%.1f s at %.0f Hz)\n", N, N / TARGET_HZ, TARGET_HZ);
    printf("Images: %d x %dx%dx%d = %.2f MB/step\n",
           NUM_CAMERAS, IMG_H, IMG_W, IMG_C,
           (NUM_CAMERAS * IMG_BYTES) / (1024.0 * 1024.0));
    printf("Numerics: action[%d] + qpos[%d] + qvel[%d] = %lu bytes/step\n",
           ACTION_DIM, QPOS_DIM, QVEL_DIM,
           (ACTION_DIM + QPOS_DIM + QVEL_DIM) * sizeof(double));
    printf("Total data rate: ~%.1f MB/s\n",
           (NUM_CAMERAS * IMG_BYTES + (ACTION_DIM + QPOS_DIM + QVEL_DIM) * sizeof(double))
           * TARGET_HZ / (1024.0 * 1024.0));

    std::string bench_dir = "/tmp/hdf5_bench";
    std::filesystem::create_directories(bench_dir);

    // Generate synthetic data
    printf("\nGenerating synthetic data...\n");
    std::vector<uint8_t> img_buf(IMG_BYTES);
    std::mt19937 rng(42);
    std::uniform_int_distribution<uint8_t> dist(0, 255);
    for (auto& b : img_buf) b = dist(rng);

    double action[ACTION_DIM], qpos[QPOS_DIM], qvel[QVEL_DIM];
    for (int i = 0; i < ACTION_DIM; ++i) action[i] = 0.1 * i;
    for (int i = 0; i < QPOS_DIM; ++i)   qpos[i] = 0.5 * i;
    for (int i = 0; i < QVEL_DIM; ++i)    qvel[i] = 0.01 * i;

    printf("Running benchmarks (this may take a few minutes)...\n");

    // Drop page cache between runs for fairness
    auto drop_caches = []() {
        sync();
        // May fail without root — that's fine, still useful
        FILE* f = fopen("/proc/sys/vm/drop_caches", "w");
        if (f) { fprintf(f, "3"); fclose(f); }
    };

    // Run all 4 strategies
    drop_caches();
    auto r1 = bench_raw_write(N, img_buf, action, qpos, qvel, bench_dir);
    print_report(r1);

    drop_caches();
    auto r2 = bench_h5_contiguous(N, img_buf, action, qpos, qvel, bench_dir);
    print_report(r2);

    drop_caches();
    auto r3 = bench_h5_chunked(N, img_buf, action, qpos, qvel, bench_dir, false);
    print_report(r3);

    drop_caches();
    auto r4 = bench_h5_chunked(N, img_buf, action, qpos, qvel, bench_dir, true);
    print_report(r4);

    // Summary comparison table
    printf("\n\n============ SUMMARY ============\n");
    printf("%-30s %10s %10s %10s %10s %10s\n",
           "Strategy", "Wall(s)", "CPU(%)", "p50(us)", "p99(us)", "Size(MB)");
    printf("%-30s %10s %10s %10s %10s %10s\n",
           "------------------------------", "----------", "----------",
           "----------", "----------", "----------");

    auto print_row = [](const TimingResult& r) {
        auto lat = r.latencies_us;
        std::sort(lat.begin(), lat.end());
        size_t n = lat.size();
        double p50 = lat[n / 2];
        double p99 = lat[(size_t)(n * 0.99)];
        double cpu_pct = ((r.cpu_user_s + r.cpu_sys_s) / r.total_wall_s) * 100.0;
        printf("%-30s %10.2f %9.1f%% %10.0f %10.0f %10.1f\n",
               r.name.c_str(), r.total_wall_s, cpu_pct, p50, p99,
               r.file_size_bytes / (1024.0 * 1024.0));
    };

    print_row(r1);
    print_row(r2);
    print_row(r3);
    print_row(r4);

    double budget = 1e6 / TARGET_HZ;
    printf("\n30 Hz budget: %.0f us/step. Total data: ~%.0f MB\n",
           budget, N * NUM_CAMERAS * IMG_BYTES / (1024.0 * 1024.0));

    return 0;
}
