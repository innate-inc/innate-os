// CUDA domain-transform recursive filter (DTF-RF).
//
// Faithful implementation of: "Domain Transform for Edge-Aware Image
//                               and Video Processing"
//                              Gastal & Oliveira, SIGGRAPH 2011
//
// This is the Recursive Filtering (RF) mode — Section 3.3 of the paper.
//
// Self-guided variant for disparity filtering: the disparity image is both
// the guide (for computing the domain transform) and the signal being filtered.
// Invalid pixels (disparity ≤ 0) break the recursive chain.
//
// Key differences from a naïve implementation:
//
//   1. Domain transform derivatives are computed from the ORIGINAL image,
//      not the in-place filtered result.  This prevents edge erosion across
//      iterations.  (See Eq. 11 in the paper.)
//
//   2. Sigma decreases per iteration following Eq. 14:
//        σ_H(i) = σ_s · √3 · 2^(N−i−1) / √(4^N − 1)
//      This gives broad smoothing first, then fine refinement — essential
//      for convergence to the 2D ideal.
//
//   3. The feedback coefficient `a` and per-pixel weight `V = a^D` follow
//      the paper's Appendix:
//        a = exp(−√2 / σ_H(i))
//        D(x) = 1 + (σ_s / σ_r) · |I_orig(x) − I_orig(x−1)|
//        V(x) = a^D(x) = exp(−√2 · D(x) / σ_H(i))
//
// Architecture:
//   One thread per row (horizontal) or per column (vertical).
//   Rows/columns are independent → embarrassingly parallel.
//   At 160×120, the entire image (2 copies × 76 KB) fits in L2 cache.
//
// Performance: ~0.2–0.5 ms total at 160×120 on Jetson Orin Nano.

#include <cuda_runtime.h>
#include <cstdio>
#include <cmath>

// =============================================================================
// Horizontal pass — one thread per row.
//
// Forward scan  (left → right):  F[x] += V_fwd[x] · (F[x−1] − F[x])
// Backward scan (right → left):  F[x] += V_bwd[x] · (F[x+1] − F[x])
//
// V_fwd[x] = exp(coeff · (1 + σ_s/σ_r · |I_orig[x] − I_orig[x−1]|))
// V_bwd[x] = exp(coeff · (1 + σ_s/σ_r · |I_orig[x+1] − I_orig[x]|))
//
// coeff = −√2 / σ_H(i)   (negative, pre-computed on host per iteration)
//
// Invalid pixels (≤ 0 in either orig or filtered image) → skip.
// =============================================================================
__global__ void dt_horizontal_kernel(
    float* __restrict__ img,        // being-filtered image (in-place)
    const float* __restrict__ orig, // original disparity (read-only)
    const int width,
    const int height,
    const float ss_over_sr,         // σ_s / σ_r
    const float coeff)              // −√2 / σ_H(i)
{
  const int y = blockIdx.x * blockDim.x + threadIdx.x;
  if (y >= height) return;

  float* __restrict__ F = img  + y * width;
  const float*        I = orig + y * width;

  // ── Forward (left → right) ──────────────────────────────────────────
  for (int x = 1; x < width; ++x) {
    if (F[x] <= 0.0f || F[x - 1] <= 0.0f) continue;
    if (I[x] <= 0.0f || I[x - 1] <= 0.0f) continue;

    float D = 1.0f + ss_over_sr * fabsf(I[x] - I[x - 1]);
    float V = __expf(coeff * D);
    F[x] += V * (F[x - 1] - F[x]);
  }

  // ── Backward (right → left) ─────────────────────────────────────────
  for (int x = width - 2; x >= 0; --x) {
    if (F[x] <= 0.0f || F[x + 1] <= 0.0f) continue;
    if (I[x + 1] <= 0.0f || I[x] <= 0.0f) continue;

    float D = 1.0f + ss_over_sr * fabsf(I[x + 1] - I[x]);
    float V = __expf(coeff * D);
    F[x] += V * (F[x + 1] - F[x]);
  }
}

// =============================================================================
// Vertical pass — one thread per column.
//
// Same logic as horizontal but with stride = width between adjacent rows.
// At ≤320×240 the entire working set fits in L2, so strided access is fine.
// =============================================================================
__global__ void dt_vertical_kernel(
    float* __restrict__ img,
    const float* __restrict__ orig,
    const int width,
    const int height,
    const float ss_over_sr,
    const float coeff)
{
  const int x = blockIdx.x * blockDim.x + threadIdx.x;
  if (x >= width) return;

  // ── Forward (top → bottom) ──────────────────────────────────────────
  for (int y = 1; y < height; ++y) {
    const int cur  = y * width + x;
    const int prev = cur - width;

    if (img[cur] <= 0.0f || img[prev] <= 0.0f) continue;
    if (orig[cur] <= 0.0f || orig[prev] <= 0.0f) continue;

    float D = 1.0f + ss_over_sr * fabsf(orig[cur] - orig[prev]);
    float V = __expf(coeff * D);
    img[cur] += V * (img[prev] - img[cur]);
  }

  // ── Backward (bottom → top) ─────────────────────────────────────────
  for (int y = height - 2; y >= 0; --y) {
    const int cur  = y * width + x;
    const int next = cur + width;

    if (img[cur] <= 0.0f || img[next] <= 0.0f) continue;
    if (orig[cur] <= 0.0f || orig[next] <= 0.0f) continue;

    float D = 1.0f + ss_over_sr * fabsf(orig[next] - orig[cur]);
    float V = __expf(coeff * D);
    img[cur] += V * (img[next] - img[cur]);
  }
}

// =============================================================================
// Host wrapper — called from C++.
//
// Parameters match the paper:
//   sigma_s  — spatial filter standard deviation  (Eq. 2)
//   sigma_r  — range filter standard deviation    (Eq. 2)
//   N        — number of iterations               (Eq. 14)
//
// Uses persistent device buffers to avoid malloc/free per frame.
// The caller must not call this concurrently (fine — filter chain is
// single-threaded in processFrame).
// =============================================================================

// Persistent GPU state (module-level)
static float*       d_img      = nullptr;   // working copy
static float*       d_orig     = nullptr;   // original (for derivatives)
static size_t       d_buf_size = 0;
static cudaStream_t dt_stream  = nullptr;

// CUDA event timing (persistent — avoid create/destroy per frame)
static cudaEvent_t ev_start    = nullptr;
static cudaEvent_t ev_uploaded = nullptr;
static cudaEvent_t ev_kernels  = nullptr;
static cudaEvent_t ev_end      = nullptr;
static int         dt_log_ctr  = 0;

extern "C"
void cuda_domain_transform_filter(
    float* h_img,           // host: cv::Mat.ptr<float>(), continuous
    int    width,
    int    height,
    float  sigma_s,         // spatial σ  (paper Eq. 2)
    float  sigma_r,         // range σ    (paper Eq. 2)
    int    N)               // iterations (paper Eq. 14)
{
  const size_t nbytes = static_cast<size_t>(width) * height * sizeof(float);

  // ── Lazy-init CUDA stream + events ──────────────────────────────────
  if (!dt_stream) {
    cudaStreamCreateWithFlags(&dt_stream, cudaStreamNonBlocking);
    cudaEventCreate(&ev_start);
    cudaEventCreate(&ev_uploaded);
    cudaEventCreate(&ev_kernels);
    cudaEventCreate(&ev_end);
  }

  // ── Lazy-init / resize device buffers ───────────────────────────────
  if (nbytes > d_buf_size) {
    if (d_img)  cudaFree(d_img);
    if (d_orig) cudaFree(d_orig);
    cudaMalloc(&d_img,  nbytes);
    cudaMalloc(&d_orig, nbytes);
    d_buf_size = nbytes;
  }

  // ── Record: start ──────────────────────────────────────────────────
  cudaEventRecord(ev_start, dt_stream);

  // ── Upload host → device (working copy + original) ─────────────────
  cudaMemcpyAsync(d_img,  h_img, nbytes, cudaMemcpyHostToDevice, dt_stream);
  cudaMemcpyAsync(d_orig, h_img, nbytes, cudaMemcpyHostToDevice, dt_stream);

  // ── Record: upload done ────────────────────────────────────────────
  cudaEventRecord(ev_uploaded, dt_stream);

  // ── Pre-compute constants ───────────────────────────────────────────
  const float ss_over_sr = sigma_s / sigma_r;
  const float sqrt2      = sqrtf(2.0f);
  const float sqrt3      = sqrtf(3.0f);

  //  4^N − 1 = (2^N)^2 − 1
  const float denom = sqrtf(powf(4.0f, static_cast<float>(N)) - 1.0f);

  const int threads = 128;

  // ── Iterate: H + V per iteration (paper Eq. 14 + Section 3.3) ──────
  for (int i = 0; i < N; ++i) {
    // Eq. 14: σ_H(i) = σ_s · √3 · 2^(N−i−1) / √(4^N − 1)
    float sigma_H_i = sigma_s * sqrt3
                    * powf(2.0f, static_cast<float>(N - i - 1))
                    / denom;

    // Feedback coefficient: a = exp(−√2 / σ_H(i))   →   V = a^D = exp(coeff · D)
    float coeff = -sqrt2 / sigma_H_i;

    dt_horizontal_kernel
        <<<(height + threads - 1) / threads, threads, 0, dt_stream>>>
        (d_img, d_orig, width, height, ss_over_sr, coeff);

    dt_vertical_kernel
        <<<(width + threads - 1) / threads, threads, 0, dt_stream>>>
        (d_img, d_orig, width, height, ss_over_sr, coeff);
  }

  // ── Record: kernels done ───────────────────────────────────────────
  cudaEventRecord(ev_kernels, dt_stream);

  // ── Download device → host ──────────────────────────────────────────
  cudaMemcpyAsync(h_img, d_img, nbytes, cudaMemcpyDeviceToHost, dt_stream);

  // ── Record: download done ──────────────────────────────────────────
  cudaEventRecord(ev_end, dt_stream);
  cudaStreamSynchronize(dt_stream);

  // ── Log breakdown every ~1 s (throttled by frame count) ────────────
  if (++dt_log_ctr >= 10) {   // ~10 fps → ~1 s
    dt_log_ctr = 0;
    float ms_upload = 0, ms_kernels = 0, ms_download = 0, ms_total = 0;
    cudaEventElapsedTime(&ms_upload,   ev_start,    ev_uploaded);
    cudaEventElapsedTime(&ms_kernels,  ev_uploaded,  ev_kernels);
    cudaEventElapsedTime(&ms_download, ev_kernels,   ev_end);
    cudaEventElapsedTime(&ms_total,    ev_start,     ev_end);
    fprintf(stderr,
        "[domain_transform.cu] %dx%d %d iter | "
        "upload %.2f ms | kernels %.2f ms | download %.2f ms | total %.2f ms\n",
        width, height, N, ms_upload, ms_kernels, ms_download, ms_total);
  }
}

extern "C"
void cuda_domain_transform_cleanup()
{
  if (d_img)        { cudaFree(d_img);              d_img      = nullptr; }
  if (d_orig)       { cudaFree(d_orig);             d_orig     = nullptr; }
  d_buf_size = 0;
  if (ev_start)     { cudaEventDestroy(ev_start);    ev_start    = nullptr; }
  if (ev_uploaded)  { cudaEventDestroy(ev_uploaded);  ev_uploaded = nullptr; }
  if (ev_kernels)   { cudaEventDestroy(ev_kernels);   ev_kernels  = nullptr; }
  if (ev_end)       { cudaEventDestroy(ev_end);       ev_end      = nullptr; }
  if (dt_stream)    { cudaStreamDestroy(dt_stream);   dt_stream   = nullptr; }
}
