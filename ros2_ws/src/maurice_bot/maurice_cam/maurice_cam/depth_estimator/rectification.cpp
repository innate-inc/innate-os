// VPI GPU-accelerated rectification — zero-copy pipeline.
//
// All rectification uses VPI remap on CUDA.  Remap outputs are persistent
// VPIImages that feed directly into SGM without GPU↔CPU roundtrips.
//
//   submitRemap()      – wraps host cv::Mat, submits 2× async remap (L+R mono)
//   submitColorRemap() – wraps host cv::Mat, submits 1× async remap (L colour)
//   submitSGM()        – reads remap output VPIImages directly (vpi_stereo.cpp)
//   syncVPI()          – single vpiStreamSync at the end
//   lockRectifiedMono/Color() – lock GPU output → clone to cv::Mat for publishing

#include "maurice_cam/stereo_depth_estimator.hpp"

#include <vpi/WarpMap.h>
#include <vpi/algo/Remap.h>

namespace maurice_cam
{

// =============================================================================
// Resize both images to calibration resolution (CPU)
// =============================================================================
void StereoDepthEstimator::scaleToCalibRes(
    const cv::Mat& left_in, const cv::Mat& right_in,
    cv::Mat& left_out, cv::Mat& right_out)
{
  cv::resize(left_in,  left_out,  cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_in, right_out, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
}

// =============================================================================
// Convert OpenCV remap maps (CV_32FC1 x + y) into a VPI dense warp map
// =============================================================================
static bool buildVPIWarpMap(const cv::Mat& map_x, const cv::Mat& map_y,
                            VPIWarpMap& warp)
{
  const int w = map_x.cols;
  const int h = map_x.rows;

  memset(&warp, 0, sizeof(warp));
  warp.grid.numHorizRegions  = 1;
  warp.grid.numVertRegions   = 1;
  warp.grid.regionWidth[0]   = static_cast<int16_t>(w);
  warp.grid.regionHeight[0]  = static_cast<int16_t>(h);
  warp.grid.horizInterval[0] = 1;   // dense — 1 keypoint per pixel
  warp.grid.vertInterval[0]  = 1;

  VPIStatus st = vpiWarpMapAllocData(&warp);
  if (st != VPI_SUCCESS) return false;

  for (int y = 0; y < h; ++y) {
    const float* mx = map_x.ptr<float>(y);
    const float* my = map_y.ptr<float>(y);
    auto* row = reinterpret_cast<VPIKeypointF32*>(
        reinterpret_cast<uint8_t*>(warp.keypoints) + y * warp.pitchBytes);
    for (int x = 0; x < w; ++x) {
      row[x].x = mx[x];
      row[x].y = my[x];
    }
  }
  return true;
}

// =============================================================================
// One-time init: build warp maps → VPI remap payloads + persistent output images
// =============================================================================
bool StereoDepthEstimator::initVPIRemap()
{
  VPIWarpMap warp_left{}, warp_right{};
  if (!buildVPIWarpMap(map1_left_, map2_left_, warp_left)) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to build left warp map");
    return false;
  }
  if (!buildVPIWarpMap(map1_right_, map2_right_, warp_right)) {
    vpiWarpMapFreeData(&warp_left);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to build right warp map");
    return false;
  }

  VPIStatus st;

  // ── Remap payloads ──────────────────────────────────────────────────────
  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_left, &vpi_remap_left_);
  if (st != VPI_SUCCESS) {
    vpiWarpMapFreeData(&warp_left);  vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create left remap payload");
    return false;
  }

  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_right, &vpi_remap_right_);
  if (st != VPI_SUCCESS) {
    vpiPayloadDestroy(vpi_remap_left_); vpi_remap_left_ = nullptr;
    vpiWarpMapFreeData(&warp_left);  vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create right remap payload");
    return false;
  }

  // Colour remap uses the same warp as the left camera
  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_left, &vpi_remap_color_);
  if (st != VPI_SUCCESS) {
    vpiPayloadDestroy(vpi_remap_left_);  vpi_remap_left_  = nullptr;
    vpiPayloadDestroy(vpi_remap_right_); vpi_remap_right_ = nullptr;
    vpiWarpMapFreeData(&warp_left);  vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create colour remap payload");
    return false;
  }

  vpiWarpMapFreeData(&warp_left);
  vpiWarpMapFreeData(&warp_right);

  // ── Persistent output images (CUDA for GPU ops + CPU for locking) ──────
  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U8,
                      VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_left_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U8,
                      VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_right_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_BGR8,
                      VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_color_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  RCLCPP_INFO(this->get_logger(),
              "VPI remap initialized: %dx%d (CUDA, dense warp, bilinear)",
              calib_width_, calib_height_);
  return true;
}

// =============================================================================
// Cleanup all VPI remap resources
// =============================================================================
void StereoDepthEstimator::cleanupVPIRemap()
{
  if (vpi_remap_left_)      { vpiPayloadDestroy(vpi_remap_left_);      vpi_remap_left_      = nullptr; }
  if (vpi_remap_right_)     { vpiPayloadDestroy(vpi_remap_right_);     vpi_remap_right_     = nullptr; }
  if (vpi_remap_color_)     { vpiPayloadDestroy(vpi_remap_color_);     vpi_remap_color_     = nullptr; }
  if (vpi_rect_left_out_)   { vpiImageDestroy(vpi_rect_left_out_);     vpi_rect_left_out_   = nullptr; }
  if (vpi_rect_right_out_)  { vpiImageDestroy(vpi_rect_right_out_);    vpi_rect_right_out_  = nullptr; }
  if (vpi_rect_color_out_)  { vpiImageDestroy(vpi_rect_color_out_);    vpi_rect_color_out_  = nullptr; }
}

// =============================================================================
// Submit mono remap L + R to GPU stream (async — no sync, no lock)
//
// Creates lightweight per-frame input wrappers around the host cv::Mats.
// These must survive until after syncVPI() — destroyed in cleanupFrameWraps().
// =============================================================================
bool StereoDepthEstimator::submitRemap(
    const cv::Mat& left_gray, const cv::Mat& right_gray)
{
  VPIStatus st;

  st = vpiImageCreateWrapperOpenCVMat(left_gray, VPI_IMAGE_FORMAT_U8,
                                      VPI_BACKEND_CUDA, &vpi_frame_in_left_);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to wrap left input");
    return false;
  }

  st = vpiImageCreateWrapperOpenCVMat(right_gray, VPI_IMAGE_FORMAT_U8,
                                      VPI_BACKEND_CUDA, &vpi_frame_in_right_);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to wrap right input");
    cleanupFrameWraps();
    return false;
  }

  // Remap left (async)
  st = vpiSubmitRemap(vpi_stream_, VPI_BACKEND_CUDA, vpi_remap_left_,
                      vpi_frame_in_left_, vpi_rect_left_out_,
                      VPI_INTERP_LINEAR, VPI_BORDER_ZERO, 0);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to submit left remap");
    cleanupFrameWraps();
    return false;
  }

  // Remap right (async)
  st = vpiSubmitRemap(vpi_stream_, VPI_BACKEND_CUDA, vpi_remap_right_,
                      vpi_frame_in_right_, vpi_rect_right_out_,
                      VPI_INTERP_LINEAR, VPI_BORDER_ZERO, 0);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to submit right remap");
    cleanupFrameWraps();
    return false;
  }

  return true;
}

// =============================================================================
// Submit colour remap to GPU stream (async — no sync)
// =============================================================================
bool StereoDepthEstimator::submitColorRemap(const cv::Mat& left_bgr)
{
  VPIStatus st;

  st = vpiImageCreateWrapperOpenCVMat(left_bgr, VPI_IMAGE_FORMAT_BGR8,
                                      VPI_BACKEND_CUDA, &vpi_frame_in_color_);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to wrap colour input");
    return false;
  }

  st = vpiSubmitRemap(vpi_stream_, VPI_BACKEND_CUDA, vpi_remap_color_,
                      vpi_frame_in_color_, vpi_rect_color_out_,
                      VPI_INTERP_LINEAR, VPI_BORDER_ZERO, 0);
  if (st != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to submit colour remap");
    return false;
  }

  return true;
}

// =============================================================================
// Lock rectified mono outputs → cv::Mat  (call AFTER syncVPI)
// =============================================================================
void StereoDepthEstimator::lockRectifiedMono(
    cv::Mat& left_rect, cv::Mat& right_rect)
{
  VPIImageData data;
  cv::Mat tmp;

  vpiImageLockData(vpi_rect_left_out_, VPI_LOCK_READ,
                   VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &data);
  vpiImageDataExportOpenCVMat(data, &tmp);
  left_rect = tmp.clone();
  vpiImageUnlock(vpi_rect_left_out_);

  vpiImageLockData(vpi_rect_right_out_, VPI_LOCK_READ,
                   VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &data);
  vpiImageDataExportOpenCVMat(data, &tmp);
  right_rect = tmp.clone();
  vpiImageUnlock(vpi_rect_right_out_);
}

// =============================================================================
// Lock rectified colour output → cv::Mat  (call AFTER syncVPI)
// =============================================================================
cv::Mat StereoDepthEstimator::lockRectifiedColor()
{
  VPIImageData data;
  cv::Mat tmp;

  vpiImageLockData(vpi_rect_color_out_, VPI_LOCK_READ,
                   VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &data);
  vpiImageDataExportOpenCVMat(data, &tmp);
  cv::Mat result = tmp.clone();
  vpiImageUnlock(vpi_rect_color_out_);
  return result;
}

// =============================================================================
// Destroy per-frame VPI input wrappers  (call AFTER syncVPI)
// =============================================================================
void StereoDepthEstimator::cleanupFrameWraps()
{
  if (vpi_frame_in_left_)  { vpiImageDestroy(vpi_frame_in_left_);  vpi_frame_in_left_  = nullptr; }
  if (vpi_frame_in_right_) { vpiImageDestroy(vpi_frame_in_right_); vpi_frame_in_right_ = nullptr; }
  if (vpi_frame_in_color_) { vpiImageDestroy(vpi_frame_in_color_); vpi_frame_in_color_ = nullptr; }
}

} // namespace maurice_cam
