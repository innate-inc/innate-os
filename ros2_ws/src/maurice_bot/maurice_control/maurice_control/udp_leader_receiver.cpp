/**
 * UDP Leader Receiver Node (C++)
 *
 * Receives leader arm positions via UDP, converts to radians, and publishes
 * directly to /mars/arm/commands for low-latency teleoperation.
 *
 * Network Protocol:
 * - Port: 9999 (default, configurable)
 * - Packet Format (38 bytes, little-endian):
 *   - Bytes 0-1:   Magic header (0xAA55)
 *   - Bytes 2-5:   Sequence number (uint32)
 *   - Bytes 6-13:  Timestamp (double, ms since epoch)
 *   - Bytes 14-17: Servo 1 position (int32)
 *   - Bytes 18-21: Servo 2 position (int32)
 *   - Bytes 22-25: Servo 3 position (int32)
 *   - Bytes 26-29: Servo 4 position (int32)
 *   - Bytes 30-33: Servo 5 position (int32)
 *   - Bytes 34-37: Servo 6 position (int32)
 *
 * Reset Packet Format (6 bytes, little-endian):
 *   - Bytes 0-1: Reset magic header (0xAA56)
 *   - Bytes 2-5: Sequence number (uint32)
 */

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_srvs/srv/set_bool.hpp>
#include <cmath>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>

#include <atomic>
#include <thread>
#include <mutex>
#include <cstring>
#include <chrono>
#include <array>

namespace maurice_control {

// Packet format constants
constexpr uint16_t MAGIC_HEADER = 0xAA55;
constexpr uint16_t RESET_MAGIC_HEADER = 0xAA56;
constexpr size_t PACKET_SIZE = 38;
constexpr size_t RESET_PACKET_SIZE = 6;
constexpr size_t NUM_SERVOS = 6;

// Packed structures for efficient packet parsing
#pragma pack(push, 1)
struct DataPacket {
    uint16_t magic_header;
    uint32_t sequence;
    double timestamp;
    int32_t servo_positions[NUM_SERVOS];
};

struct ResetPacket {
    uint16_t magic_header;
    uint32_t sequence;
};
#pragma pack(pop)

static_assert(sizeof(DataPacket) == PACKET_SIZE, "DataPacket size mismatch");
static_assert(sizeof(ResetPacket) == RESET_PACKET_SIZE, "ResetPacket size mismatch");

// Compile-time endianness check - packet format assumes little-endian architecture
#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)
static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
              "This code requires a little-endian architecture. "
              "Big-endian systems need byte-order conversion for multi-byte fields.");
#else
#warning "Cannot detect system endianness at compile-time. Assuming little-endian architecture."
#endif

class UdpLeaderReceiver : public rclcpp::Node {
public:
    UdpLeaderReceiver() : Node("udp_leader_receiver") {
        // Declare parameters
        this->declare_parameter("port", 9999);
        this->declare_parameter("auto_start", true);
        this->declare_parameter("log_rate", 10.0);

        // Get parameters
        port_ = this->get_parameter("port").as_int();
        auto_start_ = this->get_parameter("auto_start").as_bool();
        log_rate_ = this->get_parameter("log_rate").as_double();

        // Publisher for arm commands (best effort QoS for low-latency teleoperation)
        auto qos = rclcpp::QoS(1).best_effort();
        commands_pub_ = this->create_publisher<std_msgs::msg::Float64MultiArray>(
            "/mars/arm/commands", qos);

        // Service for start/stop
        start_stop_service_ = this->create_service<std_srvs::srv::SetBool>(
            "/udp_leader_receiver/start",
            std::bind(&UdpLeaderReceiver::handle_start_stop, this,
                      std::placeholders::_1, std::placeholders::_2));

        // Initialize state
        socket_fd_ = -1;
        running_.store(false);
        packet_count_.store(0);
        error_count_.store(0);
        out_of_order_count_.store(0);
        last_sequence_ = -1;
        last_log_time_ = std::chrono::steady_clock::now();

        RCLCPP_INFO(this->get_logger(), 
            "UDP Leader Receiver initialized on port %d -> /mars/arm/commands [C++]", port_);

        // Auto-start if configured
        if (auto_start_) {
            start_receiver();
        }
    }

    ~UdpLeaderReceiver() {
        stop_receiver();
    }

private:
    bool start_receiver() {
        std::lock_guard<std::mutex> lock(mutex_);

        if (running_.load()) {
            RCLCPP_WARN(this->get_logger(), "UDP receiver already running");
            return false;
        }

        try {
            // Create UDP socket
            socket_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
            if (socket_fd_ < 0) {
                RCLCPP_ERROR(this->get_logger(), "Failed to create socket: %s", strerror(errno));
                return false;
            }

            // Set socket options
            int reuse = 1;
            if (setsockopt(socket_fd_, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0) {
                RCLCPP_WARN(this->get_logger(), "Failed to set SO_REUSEADDR: %s", strerror(errno));
            }

            // Set receive buffer size for better performance
            int rcvbuf = 262144;  // 256KB receive buffer
            if (setsockopt(socket_fd_, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf)) < 0) {
                RCLCPP_WARN(this->get_logger(), "Failed to set SO_RCVBUF: %s", strerror(errno));
            }

            // Bind socket
            struct sockaddr_in addr;
            std::memset(&addr, 0, sizeof(addr));
            addr.sin_family = AF_INET;
            addr.sin_addr.s_addr = INADDR_ANY;
            addr.sin_port = htons(port_);

            if (bind(socket_fd_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
                RCLCPP_ERROR(this->get_logger(), "Failed to bind socket: %s", strerror(errno));
                close(socket_fd_);
                socket_fd_ = -1;
                return false;
            }

            // Set non-blocking mode for clean shutdown
            int flags = fcntl(socket_fd_, F_GETFL, 0);
            fcntl(socket_fd_, F_SETFL, flags | O_NONBLOCK);

            // Reset sequence tracking
            last_sequence_ = -1;
            out_of_order_count_.store(0);

            // Start receiver thread
            running_.store(true);
            receiver_thread_ = std::thread(&UdpLeaderReceiver::receiver_loop, this);

            RCLCPP_INFO(this->get_logger(), "UDP receiver started on port %d", port_);
            return true;

        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to start UDP receiver: %s", e.what());
            running_.store(false);
            if (socket_fd_ >= 0) {
                close(socket_fd_);
                socket_fd_ = -1;
            }
            return false;
        }
    }

    bool stop_receiver() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!running_.load()) {
                return false;
            }
            running_.store(false);
        }

        // Wait for thread to finish
        if (receiver_thread_.joinable()) {
            receiver_thread_.join();
        }

        // Close socket
        if (socket_fd_ >= 0) {
            close(socket_fd_);
            socket_fd_ = -1;
        }

        RCLCPP_INFO(this->get_logger(), "UDP receiver stopped");
        return true;
    }

    void receiver_loop() {
        RCLCPP_INFO(this->get_logger(), "UDP receiver loop started");

        std::array<uint8_t, 1024> buffer;
        struct sockaddr_in sender_addr;
        socklen_t sender_len = sizeof(sender_addr);

        struct pollfd pfd;
        pfd.fd = socket_fd_;
        pfd.events = POLLIN;

        while (running_.load()) {
            // Poll with 100ms timeout for responsive shutdown
            int poll_result = poll(&pfd, 1, 100);

            if (poll_result < 0) {
                if (errno != EINTR && running_.load()) {
                    RCLCPP_ERROR(this->get_logger(), "Poll error: %s", strerror(errno));
                    error_count_++;
                }
                continue;
            }

            if (poll_result == 0) {
                // Timeout, continue loop
                continue;
            }

            if (pfd.revents & POLLIN) {
                ssize_t recv_len = recvfrom(socket_fd_, buffer.data(), buffer.size(), 0,
                                            reinterpret_cast<struct sockaddr*>(&sender_addr),
                                            &sender_len);

                if (recv_len < 0) {
                    if (errno != EAGAIN && errno != EWOULDBLOCK && running_.load()) {
                        RCLCPP_ERROR(this->get_logger(), "Receive error: %s", strerror(errno));
                        error_count_++;
                    }
                    continue;
                }

                // Process the packet
                process_packet(buffer.data(), static_cast<size_t>(recv_len), sender_addr);
            }
        }

        RCLCPP_INFO(this->get_logger(), "UDP receiver loop ended");
    }

    void process_packet(const uint8_t* data, size_t len, const struct sockaddr_in& addr) {
        // Check for reset packet first
        if (len == RESET_PACKET_SIZE) {
            process_reset_packet(data, addr);
            return;
        }

        // Check packet size for normal data packets
        if (len != PACKET_SIZE) {
            char addr_str[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &addr.sin_addr, addr_str, sizeof(addr_str));
            RCLCPP_WARN(this->get_logger(),
                        "Invalid packet size: %zu bytes (expected %zu) from %s:%d",
                        len, PACKET_SIZE, addr_str, ntohs(addr.sin_port));
            error_count_++;
            return;
        }

        // Parse packet using packed struct (assumes little-endian architecture)
        const DataPacket* packet = reinterpret_cast<const DataPacket*>(data);

        // Validate magic header
        if (packet->magic_header != MAGIC_HEADER) {
            char addr_str[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &addr.sin_addr, addr_str, sizeof(addr_str));
            RCLCPP_WARN(this->get_logger(),
                        "Invalid magic header: 0x%04X (expected 0x%04X) from %s:%d",
                        packet->magic_header, MAGIC_HEADER, addr_str, ntohs(addr.sin_port));
            error_count_++;
            return;
        }

        // Check sequence number
        if (is_out_of_order(packet->sequence)) {
            out_of_order_count_++;
            return;
        }

        // Update last sequence number
        last_sequence_ = static_cast<int64_t>(packet->sequence);

        // Convert to radians and publish to /mars/arm/commands
        auto msg = std_msgs::msg::Float64MultiArray();
        msg.data.reserve(NUM_SERVOS);
        for (size_t i = 0; i < NUM_SERVOS; ++i) {
            // Convert: radians = (position - 2048) * (2π / 4096)
            double radians = (static_cast<double>(packet->servo_positions[i]) - 2048.0) 
                             * (2.0 * M_PI / 4096.0);
            msg.data.push_back(radians);
        }
        commands_pub_->publish(msg);

        // Update statistics
        packet_count_++;

        // Periodic logging
        auto current_time = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration<double>(current_time - last_log_time_).count();
        if (elapsed >= log_rate_) {
            RCLCPP_INFO(this->get_logger(),
                        "Stats - Packets: %lu, Errors: %lu, Out-of-order: %lu, "
                        "Last seq: %ld, Timestamp: %.2fms, Positions: [%d, %d, %d, %d, %d, %d]",
                        packet_count_.load(), error_count_.load(), out_of_order_count_.load(),
                        last_sequence_.load(), packet->timestamp,
                        packet->servo_positions[0], packet->servo_positions[1],
                        packet->servo_positions[2], packet->servo_positions[3],
                        packet->servo_positions[4], packet->servo_positions[5]);
            last_log_time_ = current_time;
        }
    }

    void process_reset_packet(const uint8_t* data, const struct sockaddr_in& addr) {
        const ResetPacket* packet = reinterpret_cast<const ResetPacket*>(data);

        // Validate reset magic header
        if (packet->magic_header != RESET_MAGIC_HEADER) {
            char addr_str[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &addr.sin_addr, addr_str, sizeof(addr_str));
            RCLCPP_WARN(this->get_logger(),
                        "Invalid reset magic header: 0x%04X (expected 0x%04X) from %s:%d",
                        packet->magic_header, RESET_MAGIC_HEADER, addr_str, ntohs(addr.sin_port));
            error_count_++;
            return;
        }

        // Reset sequence tracking
        int64_t old_sequence = last_sequence_;
        last_sequence_ = -1;
        out_of_order_count_.store(0);

        char addr_str[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &addr.sin_addr, addr_str, sizeof(addr_str));
        RCLCPP_INFO(this->get_logger(),
                    "Sequence tracking reset via UDP packet from %s:%d "
                    "(reset_seq=%u, old_last_seq=%ld)",
                    addr_str, ntohs(addr.sin_port), packet->sequence, old_sequence);
    }

    bool is_out_of_order(uint32_t sequence) {
        if (last_sequence_ == -1) {
            // First packet
            return false;
        }

        // If sequence <= last_sequence, it's out of order or duplicate
        return sequence <= static_cast<uint32_t>(last_sequence_);
    }

    void handle_start_stop(
        const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
        std::shared_ptr<std_srvs::srv::SetBool::Response> response) {

        if (request->data) {
            // Start request
            bool success = start_receiver();
            response->success = success;
            response->message = success ? "UDP receiver started" : "Failed to start UDP receiver";
        } else {
            // Stop request
            bool success = stop_receiver();
            response->success = success;
            response->message = success ? "UDP receiver stopped" : "Failed to stop UDP receiver";
        }
    }

    // Parameters
    int port_;
    bool auto_start_;
    double log_rate_;

    // Socket
    int socket_fd_;

    // Threading
    std::atomic<bool> running_;
    std::thread receiver_thread_;
    std::mutex mutex_;

    // Statistics
    std::atomic<uint64_t> packet_count_;
    std::atomic<uint64_t> error_count_;
    std::atomic<uint64_t> out_of_order_count_;
    std::atomic<int64_t> last_sequence_;
    std::chrono::steady_clock::time_point last_log_time_;

    // ROS interfaces
    rclcpp::Publisher<std_msgs::msg::Float64MultiArray>::SharedPtr commands_pub_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr start_stop_service_;
};

}  // namespace maurice_control

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_control::UdpLeaderReceiver>();
    try {
        rclcpp::spin(node);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(node->get_logger(), "Exception: %s", e.what());
    }
    rclcpp::shutdown();
    return 0;
}
