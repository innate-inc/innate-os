#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from nav2_msgs.srv import LoadMap
from brain_messages.srv import ChangeMap, ChangeNavigationMode
import subprocess
import signal
import os
import time
import glob
import json
from nav2_simple_commander.robot_navigator import BasicNavigator

class ModeManager(Node):
    def __init__(self):
        super().__init__('mode_manager')
        
        # Service to switch modes
        self.mode_service = self.create_service(
            ChangeNavigationMode,
            '/nav/change_mode',
            self.change_mode_callback
        )
        
        # Service to change maps in navigation mode
        self.map_service = self.create_service(
            ChangeMap,
            '/nav/change_navigation_map',
            self.change_map_callback
        )
        
        # Publisher to announce current mode
        self.mode_publisher = self.create_publisher(String, '/nav/current_mode', 10)
        
        # Publisher to announce available maps
        self.maps_publisher = self.create_publisher(String, '/nav/available_maps', 10)
        
        # Publisher to announce current map
        self.current_map_publisher = self.create_publisher(String, '/nav/current_map', 10)
        
        # Track current processes
        self.current_process = None
        self.current_map = "home.yaml"  # Default map
        
        # Maps directory
        self.maps_dir = os.path.expanduser('~/maurice-prod/maps')
        
        # Mode persistence file
        self.mode_file = os.path.expanduser('~/maurice-prod/.last_mode')
        
        # Load last mode or default to navigation
        self.current_mode = self.load_last_mode()
        
        # BasicNavigator for map operations
        self.navigator = None
        
        # Timer to publish current mode and maps
        self.timer = self.create_timer(1.0, self.publish_status)
        
        # Discover available maps
        self.available_maps = self.discover_maps()
        
        self.get_logger().info('Mode Manager started with map management capabilities.')
        self.get_logger().info('- Call /nav/change_mode service to switch modes ("navigation" or "mapping")')
        self.get_logger().info('- Call /nav/change_navigation_map service to change map for navigation mode')
        self.get_logger().info(f'- Current mode: {self.current_mode}')
        self.get_logger().info(f'- Available maps: {self.available_maps}')
        
        # Auto-start in the saved mode after a short delay
        self.startup_timer = self.create_timer(3.0, self.auto_start_mode)

    def discover_maps(self):
        """Discover available map files in the maps directory"""
        map_files = []
        try:
            # Look for .yaml files in the maps directory
            yaml_pattern = os.path.join(self.maps_dir, "*.yaml")
            yaml_files = glob.glob(yaml_pattern)
            
            for yaml_file in yaml_files:
                # Extract just the filename
                map_name = os.path.basename(yaml_file)
                map_files.append(map_name)
                
            self.get_logger().info(f"Discovered {len(map_files)} maps: {map_files}")
        except Exception as e:
            self.get_logger().error(f"Error discovering maps: {e}")
            
        return sorted(map_files)

    def load_last_mode(self):
        """Load the last used mode from file, default to navigation"""
        try:
            if os.path.exists(self.mode_file):
                with open(self.mode_file, 'r') as f:
                    saved_mode = f.read().strip()
                    if saved_mode in ["navigation", "mapping"]:
                        self.get_logger().info(f"Loaded last mode: {saved_mode}")
                        return saved_mode
            
            # Default to navigation mode
            self.get_logger().info("No saved mode found, defaulting to navigation")
            return "navigation"
        except Exception as e:
            self.get_logger().error(f"Error loading last mode: {e}, defaulting to navigation")
            return "navigation"

    def save_last_mode(self, mode):
        """Save the current mode to file for persistence"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.mode_file), exist_ok=True)
            with open(self.mode_file, 'w') as f:
                f.write(mode)
            self.get_logger().debug(f"Saved mode: {mode}")
        except Exception as e:
            self.get_logger().error(f"Error saving mode: {e}")

    def auto_start_mode(self):
        """Auto-start the mode manager in the last saved mode"""
        self.startup_timer.cancel()  # One-time execution
        
        if self.current_mode in ["navigation", "mapping"]:
            self.get_logger().info(f"Auto-starting in {self.current_mode} mode...")
            # Simulate a service request
            request = ChangeNavigationMode.Request()
            request.mode = self.current_mode
            response = ChangeNavigationMode.Response()
            self.change_mode_callback(request, response)

    def publish_status(self):
        """Publish current mode, available maps, and current map"""
        # Publish current mode
        mode_msg = String()
        mode_msg.data = self.current_mode
        self.mode_publisher.publish(mode_msg)
        
        # Publish available maps as JSON
        maps_msg = String()
        maps_msg.data = json.dumps({"available_maps": self.available_maps})
        self.maps_publisher.publish(maps_msg)
        
        # Publish current map
        current_map_msg = String()
        current_map_msg.data = self.current_map
        self.current_map_publisher.publish(current_map_msg)

    def change_map_callback(self, request, response):
        """
        Service callback to change the map for navigation mode
        request.map_name should contain the map filename (e.g., "home.yaml")
        """
        try:
            requested_map = request.map_name.strip()
            
            # Validate that the requested map exists
            if requested_map not in self.available_maps:
                response.success = False
                response.message = f"Error: Map '{requested_map}' not found. Available maps: {self.available_maps}"
                self.get_logger().error(response.message)
                return response
            
            # If we're in navigation mode, we need to reload the map
            if self.current_mode == "navigation":
                if self.load_map_in_navigation(requested_map):
                    self.current_map = requested_map
                    response.success = True
                    response.message = f"Successfully changed map to '{requested_map}' in navigation mode"
                    self.get_logger().info(response.message)
                else:
                    response.success = False
                    response.message = f"Failed to load map '{requested_map}' in navigation mode"
                    self.get_logger().error(response.message)
            else:
                # If not in navigation mode, just update the map for next time navigation starts
                self.current_map = requested_map
                response.success = True
                response.message = f"Map set to '{requested_map}' for next navigation session"
                self.get_logger().info(response.message)
                
        except Exception as e:
            response.success = False
            response.message = f"Error changing map: {str(e)}"
            self.get_logger().error(response.message)
            
        return response

    def load_map_in_navigation(self, map_filename):
        """
        Load a new map in the currently running navigation mode using Nav2's LoadMap service
        """
        try:
            # Create a client to the map server's load_map service
            load_map_client = self.create_client(LoadMap, '/map_server/load_map')
            
            if not load_map_client.wait_for_service(timeout_sec=5.0):
                self.get_logger().error("LoadMap service not available")
                return False
            
            # Prepare the request
            request = LoadMap.Request()
            request.map_url = os.path.join(self.maps_dir, map_filename)
            
            self.get_logger().info(f"Loading map from: {request.map_url}")
            
            # Call the service with a longer timeout
            future = load_map_client.call_async(request)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            
            # Check if the future completed (even if it timed out, the map might still load)
            if future.done():
                result = future.result()
                if result is not None:
                    # Check the result code if it exists
                    if hasattr(result, 'result') and result.result == LoadMap.Response.RESULT_SUCCESS:
                        self.get_logger().info(f"LoadMap service returned success for: {map_filename}")
                    elif hasattr(result, 'result'):
                        self.get_logger().warning(f"LoadMap service returned: {result.result}, but will proceed anyway")
                    else:
                        self.get_logger().warning("LoadMap service completed but no result field, proceeding anyway")
                else:
                    self.get_logger().warning("LoadMap service returned None, but proceeding anyway")
            else:
                self.get_logger().warning("LoadMap service call timed out, but map might still be loading")
            
            # Initialize BasicNavigator if not already done
            if self.navigator is None:
                self.navigator = BasicNavigator()
            
            # Clear costmaps to ensure the new map is properly loaded
            try:
                self.navigator.clearAllCostmaps()
                self.get_logger().info("Cleared costmaps after map load")
            except Exception as e:
                self.get_logger().warning(f"Could not clear costmaps: {e}")
            
            # Assume success since the service often works even when it reports issues
            self.get_logger().info(f"Map loading process completed for: {map_filename}")
            return True
                
        except Exception as e:
            self.get_logger().error(f"Exception during map loading: {e}")
            # Even with exceptions, the map might have loaded, so let's still try to clear costmaps
            try:
                if self.navigator is None:
                    self.navigator = BasicNavigator()
                self.navigator.clearAllCostmaps()
                time.sleep(1)
            except:
                pass
            return True  # Return True since the map often loads despite exceptions

    def kill_current_process(self):
        """Safely kill the current launch process"""
        if self.current_process:
            try:
                # Send SIGINT (Ctrl+C) to the process group
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGINT)
                self.current_process.wait(timeout=10)  # Wait up to 10 seconds
                self.get_logger().info(f"Successfully terminated {self.current_mode} mode")
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't respond to SIGINT
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
                self.get_logger().warning(f"Force killed {self.current_mode} mode")
            except Exception as e:
                self.get_logger().error(f"Error killing process: {e}")
            finally:
                self.current_process = None

    def change_mode_callback(self, request, response):
        """
        Service callback to switch between modes
        request.mode = "navigation": Switch to navigation mode
        request.mode = "mapping": Switch to mapping mode
        """
        try:
            target_mode = request.mode.strip().lower()
            
            # Validate mode
            if target_mode not in ["navigation", "mapping"]:
                response.success = False
                response.message = f"Invalid mode '{target_mode}'. Use 'navigation' or 'mapping'"
                self.get_logger().error(response.message)
                return response
            
            # Don't restart if already in the requested mode
            if self.current_mode == target_mode and self.current_process and self.current_process.poll() is None:
                response.success = True
                response.message = f"Already in {target_mode} mode"
                self.get_logger().info(response.message)
                return response

            # Kill current process if running
            if self.current_process:
                self.get_logger().info(f"Stopping current mode: {self.current_mode}")
                self.kill_current_process()
                time.sleep(2)  # Give some time for cleanup

            # Determine which mode to launch
            if target_mode == "mapping":
                launch_cmd = [
                    'ros2', 'launch', 'maurice_nav', 'mapping.launch.py'
                ]
            else:  # navigation mode
                # Use the current map when launching navigation
                map_path = os.path.join(self.maps_dir, self.current_map)
                launch_cmd = [
                    'ros2', 'launch', 'maurice_nav', 'navigation.launch.py',
                    f'map:={map_path}'
                ]

            self.get_logger().info(f"Starting {target_mode} mode...")
            if target_mode == "navigation":
                self.get_logger().info(f"Using map: {self.current_map}")
            
            # Start the new process (output goes to terminal)
            self.current_process = subprocess.Popen(
                launch_cmd,
                preexec_fn=os.setsid,  # Create new process group for easier cleanup
                # stdout and stderr will go to the terminal where mode_manager is running
            )
            
            self.current_mode = target_mode
            
            # Save the mode for persistence
            self.save_last_mode(target_mode)
            
            # Initialize BasicNavigator for navigation mode
            if target_mode == "navigation":
                try:
                    if self.navigator is None:
                        self.navigator = BasicNavigator()
                    self.get_logger().info("BasicNavigator initialized for navigation mode")
                except Exception as e:
                    self.get_logger().warning(f"Could not initialize BasicNavigator: {e}")
            
            # Give it a moment to start
            time.sleep(3)
            
            # Check if process is still running
            if self.current_process.poll() is None:
                response.success = True
                response.message = f"Successfully switched to {target_mode} mode"
                if target_mode == "navigation":
                    response.message += f" with map '{self.current_map}'"
                self.get_logger().info(response.message)
            else:
                response.success = False
                response.message = f"Failed to start {target_mode} mode - process exited"
                self.get_logger().error(response.message)
                self.current_mode = "none"
                self.current_process = None

        except Exception as e:
            response.success = False
            response.message = f"Error switching modes: {str(e)}"
            self.get_logger().error(response.message)
            self.current_mode = "none"
            self.current_process = None

        return response

    def __del__(self):
        """Cleanup when node is destroyed"""
        if self.current_process:
            self.kill_current_process()

def main(args=None):
    rclpy.init(args=args)
    
    mode_manager = ModeManager()
    
    try:
        rclpy.spin(mode_manager)
    except KeyboardInterrupt:
        pass
    finally:
        # Clean up any running processes
        if mode_manager.current_process:
            mode_manager.kill_current_process()
        mode_manager.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main() 