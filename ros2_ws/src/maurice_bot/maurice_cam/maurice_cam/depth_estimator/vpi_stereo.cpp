// VPI stereo disparity: resource management, async submit, sync, extract.

#include "maurice_cam/stereo_depth_estimator.hpp"

// Macro to check VPI status and log errors (bool-returning functions only)
#define CHECK_VPI_STATUS(status, msg) \
  if ((status) != VPI_SUCCESS) { \
    char vpi_err[256]; \
    vpiGetLastStatusMessage(vpi_err, sizeof(vpi_err)); \
    RCLCPP_ERROR(this->get_logger(), "%s: %s", msg, vpi_err); \
    return false; \
  }

namespace maurice_cam
{

// =============================================================================
// Initialise VPI stream, images, and SGM payload
// =============================================================================
bool StereoDepthEstimator::initializeVPI()
{
  VPIStatus status;

  // Create VPI stream with CUDA backend
  status = vpiStreamCreate(VPI_BACKEND_CUDA, &vpi_stream_);
  CHECK_VPI_STATUS(status, "Failed to create VPI stream");

  // Disparity output — S16 Q10.5 (divide by 32 → pixels)
  status = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_S16,
                          VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_disparity_);
  CHECK_VPI_STATUS(status, "Failed to create disparity image");

  // Confidence map
  status = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U16,
                          VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_confidence_);
  CHECK_VPI_STATUS(status, "Failed to create confidence image");

  // SGM payload
  VPIStereoDisparityEstimatorCreationParams create_params;
  vpiInitStereoDisparityEstimatorCreationParams(&create_params);
  create_params.maxDisparity = max_disparity_;
  create_params.includeDiagonals = include_diagonals_;

  status = vpiCreateStereoDisparityEstimator(VPI_BACKEND_CUDA, calib_width_, calib_height_,
                                              VPI_IMAGE_FORMAT_U8, &create_params, &stereo_payload_);
  CHECK_VPI_STATUS(status, "Failed to create stereo disparity estimator");

  RCLCPP_INFO(this->get_logger(), "VPI SGM CUDA created: %dx%d, maxDisp=%d, diagonals=%d",
              calib_width_, calib_height_, max_disparity_, include_diagonals_);
  return true;
}

// =============================================================================
// Release all VPI resources
// =============================================================================
void StereoDepthEstimator::cleanupVPI()
{
  if (vpi_stream_) vpiStreamSync(vpi_stream_);
  if (stereo_payload_) vpiPayloadDestroy(stereo_payload_);
  if (vpi_disparity_) vpiImageDestroy(vpi_disparity_);
  if (vpi_confidence_) vpiImageDestroy(vpi_confidence_);
  if (vpi_stream_) vpiStreamDestroy(vpi_stream_);

  vpi_stream_ = nullptr;
  stereo_payload_ = nullptr;
  vpi_disparity_ = nullptr;
  vpi_confidence_ = nullptr;
}

// =============================================================================
// Wrap rectified mono images and submit SGM to CUDA (async)
// =============================================================================
bool StereoDepthEstimator::submitSGM(const cv::Mat& left_rect, const cv::Mat& right_rect)
{
  VPIStatus status;

  status = vpiImageCreateWrapperOpenCVMat(left_rect, VPI_IMAGE_FORMAT_U8,
                                           VPI_BACKEND_CUDA, &vpi_left_wrap_);
  if (status != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "Failed to wrap left rectified image");
    return false;
  }

  status = vpiImageCreateWrapperOpenCVMat(right_rect, VPI_IMAGE_FORMAT_U8,
                                           VPI_BACKEND_CUDA, &vpi_right_wrap_);
  if (status != VPI_SUCCESS) {
    vpiImageDestroy(vpi_left_wrap_); vpi_left_wrap_ = nullptr;
    RCLCPP_ERROR(this->get_logger(), "Failed to wrap right rectified image");
    return false;
  }

  VPIStereoDisparityEstimatorParams params;
  vpiInitStereoDisparityEstimatorParams(&params);
  params.maxDisparity = max_disparity_;
  params.confidenceThreshold = confidence_threshold_;
  params.minDisparity = min_disparity_;
  params.p1 = p1_;
  params.p2 = p2_;
  params.uniqueness = static_cast<float>(uniqueness_);

  vpiStreamSync(vpi_stream_);
  status = vpiSubmitStereoDisparityEstimator(vpi_stream_, VPI_BACKEND_CUDA, stereo_payload_,
                                              vpi_left_wrap_, vpi_right_wrap_,
                                              vpi_disparity_, vpi_confidence_, &params);
  if (status != VPI_SUCCESS) {
    cleanupSGMWraps();
    RCLCPP_ERROR(this->get_logger(), "Failed to compute stereo disparity");
    return false;
  }

  return true;
}

// =============================================================================
// Wait for the GPU to finish the SGM computation
// =============================================================================
void StereoDepthEstimator::syncSGM()
{
  vpiStreamSync(vpi_stream_);
}

// =============================================================================
// Lock VPI result, convert Q10.5 → float, zero border, return CV_32FC1
// =============================================================================
cv::Mat StereoDepthEstimator::extractDisparity()
{
  VPIImageData data;
  VPIStatus status = vpiImageLockData(vpi_disparity_, VPI_LOCK_READ,
                                       VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &data);
  if (status != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "Failed to lock disparity image");
    return {};
  }

  const float SCALE = 32.0f;
  const float max_valid = static_cast<float>(max_disparity_);
  const int16_t* src = reinterpret_cast<const int16_t*>(data.buffer.pitch.planes[0].data);
  const int pitch = data.buffer.pitch.planes[0].pitchBytes / sizeof(int16_t);

  cv::Mat disp(calib_height_, calib_width_, CV_32FC1);
  for (int y = 0; y < calib_height_; y++) {
    const int16_t* row_src = src + y * pitch;
    float* row_dst = disp.ptr<float>(y);
    for (int x = 0; x < calib_width_; x++) {
      float d = static_cast<float>(row_src[x]) / SCALE;
      row_dst[x] = (d >= max_valid || d <= 0.0f) ? 0.0f : d;
    }
  }

  vpiImageUnlock(vpi_disparity_);

  // Zero out border pixels to eliminate edge artifacts from stereo matching
  if (disparity_border_margin_ > 0) {
    const int m = disparity_border_margin_;
    for (int y = 0; y < m && y < calib_height_; y++) {
      float* top = disp.ptr<float>(y);
      float* bot = disp.ptr<float>(calib_height_ - 1 - y);
      for (int x = 0; x < calib_width_; x++) {
        top[x] = 0.0f;
        bot[x] = 0.0f;
      }
    }
    for (int y = m; y < calib_height_ - m; y++) {
      float* row = disp.ptr<float>(y);
      for (int x = 0; x < m && x < calib_width_; x++) {
        row[x] = 0.0f;
        row[calib_width_ - 1 - x] = 0.0f;
      }
    }
  }

  return disp;
}

// =============================================================================
// Destroy temporary per-frame VPI image wrappers
// =============================================================================
void StereoDepthEstimator::cleanupSGMWraps()
{
  if (vpi_left_wrap_)  { vpiImageDestroy(vpi_left_wrap_);  vpi_left_wrap_  = nullptr; }
  if (vpi_right_wrap_) { vpiImageDestroy(vpi_right_wrap_); vpi_right_wrap_ = nullptr; }
}

} // namespace maurice_cam
