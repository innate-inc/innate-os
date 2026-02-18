// Image preprocessing: scale to calibration resolution, mono/color rectification.
// Supports both OpenCV cv::remap and VPI GPU-accelerated remap (via USE_VPI_REMAP).

#include "maurice_cam/stereo_depth_estimator.hpp"

#ifdef USE_VPI_REMAP
#include <vpi/WarpMap.h>
#include <vpi/algo/Remap.h>
#endif

namespace maurice_cam
{

// =============================================================================
// Resize both images to calibration resolution
// =============================================================================
void StereoDepthEstimator::scaleToCalibRes(
    const cv::Mat& left_in, const cv::Mat& right_in,
    cv::Mat& left_out, cv::Mat& right_out)
{
  cv::resize(left_in,  left_out,  cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_in, right_out, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
}

// =============================================================================
// VPI Remap implementation — GPU-accelerated rectification
// =============================================================================
#ifdef USE_VPI_REMAP

// ---------------------------------------------------------------------------
// Convert OpenCV remap maps (CV_32FC1 x + y) into a VPI dense warp map
// ---------------------------------------------------------------------------
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
  warp.grid.horizInterval[0] = 1;   // dense
  warp.grid.vertInterval[0]  = 1;

  VPIStatus st = vpiWarpMapAllocData(&warp);
  if (st != VPI_SUCCESS) return false;

  // Fill keypoints from OpenCV maps
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

// ---------------------------------------------------------------------------
// One-time init: build warp maps + VPI payloads for left, right, and colour
// ---------------------------------------------------------------------------
bool StereoDepthEstimator::initVPIRemap()
{
  VPIWarpMap warp_left, warp_right;

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

  // Mono payloads (U8)
  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_left, &vpi_remap_left_);
  if (st != VPI_SUCCESS) {
    vpiWarpMapFreeData(&warp_left);
    vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create left remap payload");
    return false;
  }

  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_right, &vpi_remap_right_);
  if (st != VPI_SUCCESS) {
    vpiPayloadDestroy(vpi_remap_left_); vpi_remap_left_ = nullptr;
    vpiWarpMapFreeData(&warp_left);
    vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create right remap payload");
    return false;
  }

  // Colour payload reuses the left warp map
  st = vpiCreateRemap(VPI_BACKEND_CUDA, &warp_left, &vpi_remap_color_);
  if (st != VPI_SUCCESS) {
    vpiPayloadDestroy(vpi_remap_left_);  vpi_remap_left_  = nullptr;
    vpiPayloadDestroy(vpi_remap_right_); vpi_remap_right_ = nullptr;
    vpiWarpMapFreeData(&warp_left);
    vpiWarpMapFreeData(&warp_right);
    RCLCPP_ERROR(this->get_logger(), "VPI remap: failed to create colour remap payload");
    return false;
  }

  // Pre-allocate VPI output images (persistent across frames)
  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U8,
                       VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_left_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U8,
                       VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_right_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  st = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_BGR8,
                       VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_rect_color_out_);
  if (st != VPI_SUCCESS) { cleanupVPIRemap(); return false; }

  // Warp map data can be freed after payload creation
  vpiWarpMapFreeData(&warp_left);
  vpiWarpMapFreeData(&warp_right);

  RCLCPP_INFO(this->get_logger(),
              "VPI remap initialized: %dx%d (CUDA, dense, bilinear)",
              calib_width_, calib_height_);
  return true;
}

// ---------------------------------------------------------------------------
// Cleanup VPI remap resources
// ---------------------------------------------------------------------------
void StereoDepthEstimator::cleanupVPIRemap()
{
  if (vpi_remap_left_)      { vpiPayloadDestroy(vpi_remap_left_);      vpi_remap_left_      = nullptr; }
  if (vpi_remap_right_)     { vpiPayloadDestroy(vpi_remap_right_);     vpi_remap_right_     = nullptr; }
  if (vpi_remap_color_)     { vpiPayloadDestroy(vpi_remap_color_);     vpi_remap_color_     = nullptr; }
  if (vpi_rect_left_out_)   { vpiImageDestroy(vpi_rect_left_out_);     vpi_rect_left_out_   = nullptr; }
  if (vpi_rect_right_out_)  { vpiImageDestroy(vpi_rect_right_out_);    vpi_rect_right_out_  = nullptr; }
  if (vpi_rect_color_out_)  { vpiImageDestroy(vpi_rect_color_out_);    vpi_rect_color_out_  = nullptr; }
}

// ---------------------------------------------------------------------------
// Helper: wrap input cv::Mat, submit remap, sync, lock output, export cv::Mat
// ---------------------------------------------------------------------------
static cv::Mat vpiRemapSync(VPIStream stream, VPIPayload payload,
                            const cv::Mat& input, VPIImage vpi_out,
                            VPIImageFormat fmt)
{
  // Wrap input
  VPIImage vpi_in = nullptr;
  VPIStatus st = vpiImageCreateWrapperOpenCVMat(input, fmt, VPI_BACKEND_CUDA, &vpi_in);
  if (st != VPI_SUCCESS) return {};

  // Submit remap (async)
  st = vpiSubmitRemap(stream, VPI_BACKEND_CUDA, payload,
                      vpi_in, vpi_out, VPI_INTERP_LINEAR, VPI_BORDER_ZERO, 0);
  if (st != VPI_SUCCESS) { vpiImageDestroy(vpi_in); return {}; }

  // Sync — block until GPU done
  vpiStreamSync(stream);

  // Lock output and export to cv::Mat
  VPIImageData out_data;
  st = vpiImageLockData(vpi_out, VPI_LOCK_READ, VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &out_data);
  if (st != VPI_SUCCESS) { vpiImageDestroy(vpi_in); return {}; }

  cv::Mat result;
  vpiImageDataExportOpenCVMat(out_data, &result);
  result = result.clone();   // deep copy — we're about to unlock
  vpiImageUnlock(vpi_out);
  vpiImageDestroy(vpi_in);

  return result;
}

#endif // USE_VPI_REMAP

// =============================================================================
// Convert to grayscale (if colour) then apply stereo rectification maps
// =============================================================================
void StereoDepthEstimator::rectifyMono(
    const cv::Mat& left_scaled, const cv::Mat& right_scaled,
    cv::Mat& left_rect, cv::Mat& right_rect)
{
  cv::Mat left_gray, right_gray;
  if (left_scaled.channels() == 3) {
    cv::cvtColor(left_scaled,  left_gray,  cv::COLOR_BGR2GRAY);
    cv::cvtColor(right_scaled, right_gray, cv::COLOR_BGR2GRAY);
  } else {
    left_gray  = left_scaled;
    right_gray = right_scaled;
  }

#ifdef USE_VPI_REMAP
  if (vpi_remap_ready_) {
    left_rect  = vpiRemapSync(vpi_stream_, vpi_remap_left_,  left_gray,  vpi_rect_left_out_,  VPI_IMAGE_FORMAT_U8);
    right_rect = vpiRemapSync(vpi_stream_, vpi_remap_right_, right_gray, vpi_rect_right_out_, VPI_IMAGE_FORMAT_U8);
  } else {
    cv::remap(left_gray,  left_rect,  map1_left_,  map2_left_,  cv::INTER_LINEAR);
    cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);
  }
#else
  cv::remap(left_gray,  left_rect,  map1_left_,  map2_left_,  cv::INTER_LINEAR);
  cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);
#endif
}

// =============================================================================
// Rectify the colour image (needed for colour point cloud / colour rect topic)
// =============================================================================
void StereoDepthEstimator::rectifyColor(
    const cv::Mat& left_scaled, cv::Mat& left_color_rect)
{
#ifdef USE_VPI_REMAP
  if (vpi_remap_ready_)
    left_color_rect = vpiRemapSync(vpi_stream_, vpi_remap_color_, left_scaled, vpi_rect_color_out_, VPI_IMAGE_FORMAT_BGR8);
  else
    cv::remap(left_scaled, left_color_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
#else
  cv::remap(left_scaled, left_color_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
#endif
}

} // namespace maurice_cam
