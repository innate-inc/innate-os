#include <opencv2/opencv.hpp>
#include <iostream>
#include <string>
#include <thread>
#include <chrono>

int main(int argc, char** argv) {
    std::string devicePath = "/dev/video0";
    std::string outputPath = "frame.jpg";
    int width  = 3840;
    int height = 1080;
    int warmupFrames = 5;

    if (argc >= 2) devicePath = argv[1];
    if (argc >= 3) outputPath = argv[2];
    if (argc >= 5) {
        width = std::stoi(argv[3]);
        height = std::stoi(argv[4]);
    }

    std::cout << "Opening camera: " << devicePath << std::endl;

    cv::VideoCapture cap;
    if (!cap.open(devicePath, cv::CAP_V4L2)) {
        std::cerr << "ERROR: Failed to open device " << devicePath << std::endl;
        return 1;
    }

    // Try MJPG to reach high resolutions on UVC devices
    cap.set(cv::CAP_PROP_FOURCC, cv::VideoWriter::fourcc('M','J','P','G')); 
    cap.set(cv::CAP_PROP_FRAME_WIDTH,  width);
    cap.set(cv::CAP_PROP_FRAME_HEIGHT, height);

    // Optional: request 30 FPS (device may ignore)
    cap.set(cv::CAP_PROP_FPS, 30);

    // Verify the actual settings
    int actualWidth  = static_cast<int>(cap.get(cv::CAP_PROP_FRAME_WIDTH));
    int actualHeight = static_cast<int>(cap.get(cv::CAP_PROP_FRAME_HEIGHT));
    double actualFps = cap.get(cv::CAP_PROP_FPS);
    std::cout << "Camera configured to: " << actualWidth << "x" << actualHeight
              << " @ " << (actualFps > 0 ? actualFps : 0) << " FPS" << std::endl;

    // Warm-up frames (allow auto-exposure/white-balance to settle)
    cv::Mat frame;
    for (int i = 0; i < warmupFrames; ++i) {
        if (!cap.read(frame)) {
            std::cerr << "WARNING: Failed to read warm-up frame " << i << std::endl;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }

    // Capture one frame
    if (!cap.read(frame)) {
        std::cerr << "ERROR: Failed to capture frame from device" << std::endl;
        return 2;
    }

    if (frame.empty()) {
        std::cerr << "ERROR: Captured frame is empty" << std::endl;
        return 3;
    }

    std::cout << "Captured frame size: " << frame.cols << "x" << frame.rows << std::endl;

    // Save as JPEG (default quality ~95). Lower quality to reduce size if needed.
    std::vector<int> params = { cv::IMWRITE_JPEG_QUALITY, 90 };
    if (!cv::imwrite(outputPath, frame, params)) {
        std::cerr << "ERROR: Failed to write image to " << outputPath << std::endl;
        return 4;
    }

    std::cout << "Saved image to: " << outputPath << std::endl;

    // Optional: split into left/right 1920x1080 images (ELP dual camera)
    if (frame.cols >= 2 * 1920 && frame.rows >= 1080) {
        try {
            cv::Rect leftRoi(0, 0, frame.cols / 2, frame.rows);
            cv::Rect rightRoi(frame.cols / 2, 0, frame.cols / 2, frame.rows);
            cv::Mat left = frame(leftRoi);
            cv::Mat right = frame(rightRoi);
            cv::imwrite("frame_left.jpg", left, params);
            cv::imwrite("frame_right.jpg", right, params);
            std::cout << "Also saved split images: frame_left.jpg, frame_right.jpg" << std::endl;
        } catch (const std::exception& e) {
            std::cerr << "NOTE: Could not split and save left/right images: " << e.what() << std::endl;
        }
    }

    cap.release();
    return 0;
}



