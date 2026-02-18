// Advanced disparity filters: domain-transform smoothing and temporal filtering.

#include "maurice_cam/stereo_depth_estimator.hpp"

#include <array>
#include <cmath>
#include <cstdint>

namespace maurice_cam
{

// =============================================================================
// Domain-transform edge-preserving filter
// =============================================================================
void StereoDepthEstimator::applyDomainTransform(cv::Mat& img)
{
  for (int i = 0; i < dt_iterations_; ++i) {
    dtPass(img, true);
    dtPass(img, false);
  }
}

// Fast exp approximation (Schraudolph 1999) — ~4× faster than std::exp,
// accurate to ~0.3% in the range [-4, 0] which is our operating range.
static inline float fast_expf(float x) {
  union { float f; int32_t i; } u;
  u.i = static_cast<int32_t>(12102203.0f * x + 1065353216.0f);
  return u.f;
}

void StereoDepthEstimator::dtPass(cv::Mat& img, bool horizontal)
{
  const float a = static_cast<float>(dt_alpha_);
  const float inv_d = -1.0f / static_cast<float>(dt_delta_);

  if (horizontal) {
    const int rows = img.rows, cols = img.cols;
    for (int y = 0; y < rows; ++y) {
      float* r = img.ptr<float>(y);
      for (int x = 1; x < cols; ++x) {
        if (r[x] <= 0 || r[x - 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x - 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x - 1] * w;
      }
      for (int x = cols - 2; x >= 0; --x) {
        if (r[x] <= 0 || r[x + 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x + 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x + 1] * w;
      }
    }
  } else {
    // Transpose → horizontal pass → transpose back (cache-friendly)
    cv::Mat t;
    cv::transpose(img, t);
    const int rows = t.rows, cols = t.cols;
    for (int y = 0; y < rows; ++y) {
      float* r = t.ptr<float>(y);
      for (int x = 1; x < cols; ++x) {
        if (r[x] <= 0 || r[x - 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x - 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x - 1] * w;
      }
      for (int x = cols - 2; x >= 0; --x) {
        if (r[x] <= 0 || r[x + 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x + 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x + 1] * w;
      }
    }
    cv::transpose(t, img);
  }
}

// =============================================================================
// Temporal filter (IIR blend + RealSense-style persistence)
// =============================================================================
void StereoDepthEstimator::applyTemporal(cv::Mat& img)
{
  const int total = img.rows * img.cols;

  // First frame or resolution change: initialise history
  if (prev_disparity_frame_.empty() || prev_disparity_frame_.size() != img.size()) {
    prev_disparity_frame_ = img.clone();
    temporal_history_.assign(total, 0);
    return;
  }

  const float a = static_cast<float>(temporal_alpha_);
  const float delta = static_cast<float>(temporal_delta_);
  const int persist = temporal_persistence_;

  // Pre-compute persistence bitmask LUT (same as RealSense).
  std::array<bool, 256> persist_lut;
  persist_lut.fill(false);
  if (persist > 0) {
    for (int i = 0; i < 256; ++i) {
      int b7 = !!(i & 1), b6 = !!(i & 2), b5 = !!(i & 4), b4 = !!(i & 8);
      int b3 = !!(i & 16), b2 = !!(i & 32), b1 = !!(i & 64), b0 = !!(i & 128);
      int sum8 = b0+b1+b2+b3+b4+b5+b6+b7;
      int sum3 = b0+b1+b2;
      int sum4 = b0+b1+b2+b3;
      int sum2 = b0+b1;
      int sum5 = b0+b1+b2+b3+b4;
      switch (persist) {
        case 1: persist_lut[i] = (sum8 >= 8); break;
        case 2: persist_lut[i] = (sum3 >= 2); break;
        case 3: persist_lut[i] = (sum4 >= 2); break;
        case 4: persist_lut[i] = (sum8 >= 2); break;
        case 5: persist_lut[i] = (sum2 >= 1); break;
        case 6: persist_lut[i] = (sum5 >= 1); break;
        case 7: persist_lut[i] = (sum8 >= 1); break;
        case 8: persist_lut[i] = true; break;
        default: break;
      }
    }
  }

  for (int y = 0; y < img.rows; ++y) {
    float* cur = img.ptr<float>(y);
    const float* prv = prev_disparity_frame_.ptr<float>(y);
    const int row_off = y * img.cols;
    for (int x = 0; x < img.cols; ++x) {
      const int idx = row_off + x;
      const bool cur_valid = (cur[x] > 0);
      const bool prv_valid = (prv[x] > 0);

      uint8_t h = temporal_history_[idx];
      h = static_cast<uint8_t>((h << 1) | (cur_valid ? 1 : 0));
      temporal_history_[idx] = h;

      if (cur_valid && prv_valid) {
        float diff = std::abs(cur[x] - prv[x]);
        if (diff < delta) {
          cur[x] = a * cur[x] + (1.f - a) * prv[x];
        }
      } else if (!cur_valid && prv_valid && persist > 0) {
        if (persist_lut[h]) {
          cur[x] = prv[x];
        }
      }
    }
  }
  prev_disparity_frame_ = img.clone();
}

} // namespace maurice_cam
