#pragma once

#include <filesystem>
#include <string>
#include <system_error>

namespace mars_cam {

/**
 * @brief The /dev/v4l/by-id path of the camera whose name contains @p pattern, preferring its
 * -video-index0 capture node, or "" when no such camera is plugged in.
 *
 * Open this path, never the /dev/videoN it points to: a USB drop re-enumerates the camera under a
 * new number, and a hub reset can hand a camera's old number to the other camera.
 */
inline std::string findCameraByIdPath(const std::string& pattern) {
    std::error_code ec;
    std::string first_match;
    for (const auto& entry : std::filesystem::directory_iterator("/dev/v4l/by-id", ec)) {
        const std::string name = entry.path().filename().string();
        if (name.find(pattern) == std::string::npos) {
            continue;
        }
        if (name.find("-video-index0") != std::string::npos) {
            return entry.path().string();
        }
        if (first_match.empty()) {
            first_match = entry.path().string();
        }
    }
    return first_match;
}

/** @brief The /dev/videoN a by-id path currently points to, for logs; the path itself if unresolvable. */
inline std::string currentVideoNode(const std::string& by_id_path) {
    std::error_code ec;
    const auto node = std::filesystem::canonical(by_id_path, ec);
    return ec ? by_id_path : node.string();
}

}  // namespace mars_cam
