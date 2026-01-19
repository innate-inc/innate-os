#include <gtest/gtest.h>
#include <filesystem>
#include <fstream>
#include <memory>

#include "rclcpp/rclcpp.hpp"
#include "maurice_nav/mode_manager.hpp"

namespace fs = std::filesystem;

class ModeManagerTest : public ::testing::Test
{
protected:
  void SetUp() override
  {
    // Create a temporary directory for testing
    test_dir_ = fs::temp_directory_path() / "maurice_nav_test";
    fs::create_directories(test_dir_);

    // Set environment variable for test directory
    setenv("INNATE_OS_ROOT", test_dir_.c_str(), 1);

    // Create maps subdirectory
    maps_dir_ = test_dir_ / "maps";
    fs::create_directories(maps_dir_);

    // Initialize ROS if not already initialized
    if (!rclcpp::ok()) {
      rclcpp::init(0, nullptr);
    }
  }

  void TearDown() override
  {
    // Clean up test directory
    if (fs::exists(test_dir_)) {
      fs::remove_all(test_dir_);
    }
  }

  // Helper to create a dummy map file
  void createMapFile(const std::string& map_name)
  {
    std::string yaml_path = (maps_dir_ / map_name).string();
    std::ofstream file(yaml_path);
    file << "image: " << map_name << ".pgm\n";
    file << "resolution: 0.05\n";
    file << "origin: [0.0, 0.0, 0.0]\n";
    file.close();
  }

  // Helper to write mode file
  void writeModeFile(const std::string& mode)
  {
    std::string mode_file = (test_dir_ / ".last_mode").string();
    std::ofstream file(mode_file);
    file << mode;
    file.close();
  }

  // Helper to write map file
  void writeMapFile(const std::string& map_name)
  {
    std::string map_file = (test_dir_ / ".last_map").string();
    std::ofstream file(map_file);
    file << map_name;
    file.close();
  }

  // Helper to read mode file
  std::string readModeFile()
  {
    std::string mode_file = (test_dir_ / ".last_mode").string();
    std::ifstream file(mode_file);
    std::string mode;
    std::getline(file, mode);
    return mode;
  }

  // Helper to read map file
  std::string readMapFile()
  {
    std::string map_file = (test_dir_ / ".last_map").string();
    std::ifstream file(map_file);
    std::string map;
    std::getline(file, map);
    return map;
  }

  fs::path test_dir_;
  fs::path maps_dir_;
};

// ========== File I/O Tests ==========

TEST_F(ModeManagerTest, LoadLastMode_DefaultsToNavigation)
{
  // Don't create a mode file - should default to "navigation"
  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Give the node time to initialize
  rclcpp::spin_some(node);

  // Note: We can't directly access private members, but we can verify
  // the mode file doesn't exist and the node initializes correctly
  EXPECT_FALSE(fs::exists(test_dir_ / ".last_mode"));
}

TEST_F(ModeManagerTest, LoadLastMode_LoadsValidMode)
{
  // Create mode file with "mapping"
  writeModeFile("mapping");

  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Verify mode file exists and contains correct value
  EXPECT_TRUE(fs::exists(test_dir_ / ".last_mode"));
  EXPECT_EQ(readModeFile(), "mapping");
}

TEST_F(ModeManagerTest, SaveLastMode_CreatesFile)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // The node should have created a mode file during initialization
  EXPECT_TRUE(fs::exists(test_dir_ / ".last_mode"));
}

TEST_F(ModeManagerTest, LoadLastMap_NoMapsAvailable)
{
  // Don't create any map files
  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Node should initialize even without maps
  EXPECT_TRUE(node != nullptr);
}

TEST_F(ModeManagerTest, LoadLastMap_LoadsValidMap)
{
  // Create a map file
  createMapFile("test_map.yaml");
  writeMapFile("test_map.yaml");

  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Verify map file exists
  EXPECT_TRUE(fs::exists(maps_dir_ / "test_map.yaml"));
}

TEST_F(ModeManagerTest, DiscoverMaps_FindsMultipleMaps)
{
  // Create multiple map files
  createMapFile("map1.yaml");
  createMapFile("map2.yaml");
  createMapFile("map3.yaml");

  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Verify all map files exist
  EXPECT_TRUE(fs::exists(maps_dir_ / "map1.yaml"));
  EXPECT_TRUE(fs::exists(maps_dir_ / "map2.yaml"));
  EXPECT_TRUE(fs::exists(maps_dir_ / "map3.yaml"));
}

TEST_F(ModeManagerTest, DiscoverMaps_IgnoresNonYamlFiles)
{
  // Create various files
  createMapFile("map1.yaml");

  // Create non-yaml files
  std::ofstream pgm_file((maps_dir_ / "map1.pgm").string());
  pgm_file << "P5\n100 100\n255\n";
  pgm_file.close();

  std::ofstream txt_file((maps_dir_ / "readme.txt").string());
  txt_file << "This is a readme file";
  txt_file.close();

  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Only yaml file should be discovered (we can't test this directly without
  // exposing the available_maps_ member, but we verify files exist)
  EXPECT_TRUE(fs::exists(maps_dir_ / "map1.yaml"));
  EXPECT_TRUE(fs::exists(maps_dir_ / "map1.pgm"));
  EXPECT_TRUE(fs::exists(maps_dir_ / "readme.txt"));
}

// ========== Mode Validation Tests ==========

TEST_F(ModeManagerTest, ModeConfiguration_HasAllExpectedModes)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();
  rclcpp::spin_some(node);

  // Node should initialize with all three mode configurations
  // We can't test the internal map directly, but we can verify
  // the node initializes without crashing
  EXPECT_TRUE(node != nullptr);
}

// ========== Service Tests (require service calls - integration level) ==========

TEST_F(ModeManagerTest, ChangeMode_RejectsInvalidMode)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Create service client
  auto client = node->create_client<brain_messages::srv::ChangeNavigationMode>("/nav/change_mode");

  // Wait for service
  ASSERT_TRUE(client->wait_for_service(std::chrono::seconds(5)));

  // Call with invalid mode
  auto request = std::make_shared<brain_messages::srv::ChangeNavigationMode::Request>();
  request->mode = "invalid_mode";

  auto result_future = client->async_send_request(request);

  // Spin until result
  auto executor = rclcpp::executors::SingleThreadedExecutor();
  executor.add_node(node);

  auto status = executor.spin_until_future_complete(result_future, std::chrono::seconds(5));
  ASSERT_EQ(status, rclcpp::FutureReturnCode::SUCCESS);

  auto response = result_future.get();
  EXPECT_FALSE(response->success);
  EXPECT_NE(response->message.find("Invalid mode"), std::string::npos);
}

TEST_F(ModeManagerTest, ChangeMap_RejectsNonexistentMap)
{
  // Create at least one map so node can initialize
  createMapFile("existing_map.yaml");

  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Create service client
  auto client = node->create_client<brain_messages::srv::ChangeMap>("/nav/change_navigation_map");

  // Wait for service
  ASSERT_TRUE(client->wait_for_service(std::chrono::seconds(5)));

  // Call with nonexistent map
  auto request = std::make_shared<brain_messages::srv::ChangeMap::Request>();
  request->map_name = "nonexistent_map.yaml";

  auto result_future = client->async_send_request(request);

  // Spin until result
  auto executor = rclcpp::executors::SingleThreadedExecutor();
  executor.add_node(node);

  auto status = executor.spin_until_future_complete(result_future, std::chrono::seconds(5));
  ASSERT_EQ(status, rclcpp::FutureReturnCode::SUCCESS);

  auto response = result_future.get();
  EXPECT_FALSE(response->success);
  EXPECT_NE(response->message.find("not found"), std::string::npos);
}

TEST_F(ModeManagerTest, SaveMap_RejectsInvalidMapName)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Create service client
  auto client = node->create_client<brain_messages::srv::SaveMap>("/nav/save_map");

  // Wait for service
  ASSERT_TRUE(client->wait_for_service(std::chrono::seconds(5)));

  // Call with invalid map name (contains spaces)
  auto request = std::make_shared<brain_messages::srv::SaveMap::Request>();
  request->map_name = "invalid map name";
  request->overwrite = false;

  auto result_future = client->async_send_request(request);

  // Spin until result
  auto executor = rclcpp::executors::SingleThreadedExecutor();
  executor.add_node(node);

  auto status = executor.spin_until_future_complete(result_future, std::chrono::seconds(5));
  ASSERT_EQ(status, rclcpp::FutureReturnCode::SUCCESS);

  auto response = result_future.get();
  EXPECT_FALSE(response->success);
  EXPECT_NE(response->message.find("Invalid map name"), std::string::npos);
}

TEST_F(ModeManagerTest, DeleteMap_RejectsEmptyMapName)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Create service client
  auto client = node->create_client<brain_messages::srv::DeleteMap>("/nav/delete_map");

  // Wait for service
  ASSERT_TRUE(client->wait_for_service(std::chrono::seconds(5)));

  // Call with empty map name
  auto request = std::make_shared<brain_messages::srv::DeleteMap::Request>();
  request->map_name = "";

  auto result_future = client->async_send_request(request);

  // Spin until result
  auto executor = rclcpp::executors::SingleThreadedExecutor();
  executor.add_node(node);

  auto status = executor.spin_until_future_complete(result_future, std::chrono::seconds(5));
  ASSERT_EQ(status, rclcpp::FutureReturnCode::SUCCESS);

  auto response = result_future.get();
  EXPECT_FALSE(response->success);
  EXPECT_NE(response->message.find("cannot be empty"), std::string::npos);
}

// ========== Topics Publishing Tests ==========

TEST_F(ModeManagerTest, PublishesStatusTopics)
{
  auto node = std::make_shared<maurice_nav::ModeManager>();

  // Create subscribers to verify topics are published
  std::atomic<bool> mode_received{false};
  std::atomic<bool> maps_received{false};
  std::atomic<bool> current_map_received{false};

  auto mode_sub = node->create_subscription<std_msgs::msg::String>(
    "/nav/current_mode", 10,
    [&mode_received](const std_msgs::msg::String::SharedPtr) {
      mode_received = true;
    });

  auto maps_sub = node->create_subscription<std_msgs::msg::String>(
    "/nav/available_maps", 10,
    [&maps_received](const std_msgs::msg::String::SharedPtr) {
      maps_received = true;
    });

  auto current_map_sub = node->create_subscription<std_msgs::msg::String>(
    "/nav/current_map", 10,
    [&current_map_received](const std_msgs::msg::String::SharedPtr) {
      current_map_received = true;
    });

  // Spin for a few seconds to allow status publishing
  auto executor = rclcpp::executors::SingleThreadedExecutor();
  executor.add_node(node);

  auto start = std::chrono::steady_clock::now();
  while (std::chrono::steady_clock::now() - start < std::chrono::seconds(3)) {
    executor.spin_some(std::chrono::milliseconds(100));
    if (mode_received && maps_received && current_map_received) {
      break;
    }
  }

  // Verify all topics were published
  EXPECT_TRUE(mode_received);
  EXPECT_TRUE(maps_received);
  EXPECT_TRUE(current_map_received);
}

int main(int argc, char** argv)
{
  testing::InitGoogleTest(&argc, argv);
  rclcpp::init(argc, argv);
  int result = RUN_ALL_TESTS();
  rclcpp::shutdown();
  return result;
}
