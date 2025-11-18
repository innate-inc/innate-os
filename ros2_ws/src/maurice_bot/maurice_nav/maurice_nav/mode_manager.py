#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from brain_messages.srv import ChangeMap, ChangeNavigationMode, SaveMap, DeleteMap
import subprocess
import signal
import os
import time
import glob
import json
from nav2_simple_commander.robot_navigator import BasicNavigator

# TF2 imports for transform lookup
import tf2_ros
from geometry_msgs.msg import Pose
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry

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
        
        # Service to save current map in mapping mode
        self.save_map_service = self.create_service(
            SaveMap,
            '/nav/save_map',
            self.save_map_callback
        )
        
        # Service to delete a map
        self.delete_map_service = self.create_service(
            DeleteMap,
            '/nav/delete_map',
            self.delete_map_callback
        )
        
        # Publisher to announce current mode
        self.mode_publisher = self.create_publisher(String, '/nav/current_mode', 10)
        
        # Publisher to announce available maps
        self.maps_publisher = self.create_publisher(String, '/nav/available_maps', 10)
        
        # Publisher to announce current map
        self.current_map_publisher = self.create_publisher(String, '/nav/current_map', 10)
        
        # Track current processes
        self.current_process = None
        
        # Use environment variable if set, otherwise construct from HOME
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        
        # Maps directory
        self.maps_dir = os.path.join(maurice_root, 'maps')
        
        # Mode persistence file
        self.mode_file = os.path.join(maurice_root, '.last_mode')
        
        # Map persistence file  
        self.map_file = os.path.join(maurice_root, '.last_map')
        
        # BasicNavigator for map operations
        self.navigator = None
        
        # Discover available maps first (needed for loading last map)
        self.available_maps = self.discover_maps()
        
        # Load last mode or default to navigation
        self.current_mode = self.load_last_mode()
        
        # Load last map or default to home.yaml (must be after discovering maps)
        self.current_map = self.load_last_map()
        
        # Timer to publish current mode and maps
        self.timer = self.create_timer(1.0, self.publish_status)

        # --- TF2: Mapping pose publisher ---
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.mapping_pose_pub = self.create_publisher(Odometry, '/mapping_pose', 10)
        # Subscribe to odometry topic (for mapping_pose publishing)
        self.odom_sub = self.create_subscription(
            Odometry,
            '/odom',
            self.odom_callback,
            20  # queue size
        )
        
        self.get_logger().info('Mode Manager started with map management capabilities.')
        self.get_logger().info('- Call /nav/change_mode service to switch modes ("navigation" or "mapping")')
        self.get_logger().info('- Call /nav/change_navigation_map service to change map (restarts navigation if running)')
        self.get_logger().info('- Call /nav/save_map service to save current map with new name (mapping mode only, set overwrite=true to replace existing maps)')
        self.get_logger().info('- Call /nav/delete_map service to delete a saved map (cannot delete active map while navigation is running)')
        self.get_logger().info(f'- Current mode: {self.current_mode} (loaded from persistence)')
        self.get_logger().info(f'- Current map: {self.current_map} (loaded from persistence)')
        self.get_logger().info(f'- Available maps: {self.available_maps}')
        
        # Auto-start in the saved mode after a short delay
        self.startup_timer = self.create_timer(3.0, self.auto_start_mode)

    def odom_callback(self, msg):
        # Only publish mapping_pose in mapping mode
        if getattr(self, 'current_mode', None) != 'mapping':
            return
        try:
            tf_time = rclpy.time.Time()
            tf: TransformStamped = self.tf_buffer.lookup_transform('map', 'base_link', tf_time)
            odom_msg = Odometry()
            odom_msg.header.stamp = msg.header.stamp
            odom_msg.header.frame_id = 'map'
            odom_msg.child_frame_id = 'base_link'
            odom_msg.pose.pose.position.x = tf.transform.translation.x
            odom_msg.pose.pose.position.y = tf.transform.translation.y
            odom_msg.pose.pose.position.z = tf.transform.translation.z
            odom_msg.pose.pose.orientation = tf.transform.rotation
            # Set covariance to zeros (or small value if desired)
            odom_msg.pose.covariance = [0.0] * 36
            # Set twist to zero
            odom_msg.twist.twist.linear.x = 0.0
            odom_msg.twist.twist.linear.y = 0.0
            odom_msg.twist.twist.linear.z = 0.0
            odom_msg.twist.twist.angular.x = 0.0
            odom_msg.twist.twist.angular.y = 0.0
            odom_msg.twist.twist.angular.z = 0.0
            odom_msg.twist.covariance = [0.0] * 36
            self.mapping_pose_pub.publish(odom_msg)
        except Exception as e:
            self.get_logger().warn(f"[mapping_pose] TF lookup failed in odom_callback: {e}")


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
                    if saved_mode in ["navigation", "mapping", "mapfree"]:
                        self.get_logger().info(f"Loaded last mode: {saved_mode}")
                        return saved_mode
            # Default to navigation mode
            self.get_logger().info("No saved mode found, defaulting to navigation")
            return "navigation"
        except Exception as e:
            self.get_logger().error(f"Error loading last mode: {e}, defaulting to navigation")
            return "navigation"

    def load_last_map(self):
        """Load the last used map from file, default to home.yaml"""
        try:
            if os.path.exists(self.map_file):
                with open(self.map_file, 'r') as f:
                    saved_map = f.read().strip()
                    # Validate that the saved map exists
                    if saved_map and saved_map in self.available_maps:
                        self.get_logger().info(f"Loaded last map: {saved_map}")
                        return saved_map
                    else:
                        self.get_logger().warning(f"Saved map '{saved_map}' not found, defaulting to home.yaml")
            
            # Default to home.yaml
            default_map = "home.yaml"
            if default_map in self.available_maps:
                self.get_logger().info(f"No saved map found, defaulting to {default_map}")
                return default_map
            elif self.available_maps:
                # If home.yaml doesn't exist, use the first available map
                first_map = self.available_maps[0]
                self.get_logger().info(f"Default map '{default_map}' not found, using first available: {first_map}")
                return first_map
            else:
                # No maps available, fallback to home.yaml anyway
                self.get_logger().warning("No maps available, using home.yaml as fallback")
                return "home.yaml"
        except Exception as e:
            self.get_logger().error(f"Error loading last map: {e}, defaulting to home.yaml")
            return "home.yaml"

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

    def save_last_map(self, map_name):
        """Save the current map to file for persistence"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.map_file), exist_ok=True)
            with open(self.map_file, 'w') as f:
                f.write(map_name)
            self.get_logger().debug(f"Saved map: {map_name}")
        except Exception as e:
            self.get_logger().error(f"Error saving map: {e}")

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
        elif self.current_mode == "mapfree":
            self.get_logger().info("Auto-starting in mapfree mode (local Nav2)")
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
            
            # Update the current map
            old_map = self.current_map
            self.current_map = requested_map
            
            # Save the new map choice for persistence
            self.save_last_map(requested_map)
            
            # If we're in navigation mode, restart navigation with the new map
            if self.current_mode == "navigation" and self.current_process and self.current_process.poll() is None:
                self.get_logger().info(f"Restarting navigation with new map: {requested_map}")
                
                # Kill current navigation process
                self.kill_current_process()
                time.sleep(2)  # Give some time for cleanup
                
                # Start navigation with the new map
                map_path = os.path.join(self.maps_dir, self.current_map)
                launch_cmd = [
                    'ros2', 'launch', 'maurice_nav', 'navigation.launch.py',
                    f'map:={map_path}'
                ]
                
                self.get_logger().info(f"Launching navigation with map: {self.current_map}")
                
                # Start the new process
                self.current_process = subprocess.Popen(
                    launch_cmd,
                    preexec_fn=os.setsid,  # Create new process group for easier cleanup
                )
                
                # Give it a moment to start
                time.sleep(3)
                
                # Check if process is still running
                if self.current_process.poll() is None:
                    response.success = True
                    response.message = f"Successfully changed map to '{requested_map}' and restarted navigation"
                    self.get_logger().info(response.message)
                else:
                    response.success = False
                    response.message = f"Failed to restart navigation with map '{requested_map}'"
                    self.get_logger().error(response.message)
                    self.current_mode = "none"
                    self.current_process = None
            else:
                # If not in navigation mode, just update the map for next time navigation starts
                response.success = True
                response.message = f"Map set to '{requested_map}' for next navigation session"
                self.get_logger().info(response.message)
                
        except Exception as e:
            response.success = False
            response.message = f"Error changing map: {str(e)}"
            self.get_logger().error(response.message)
            
        return response

    def delete_map_callback(self, request, response):
        """
        Service callback to delete a saved map (.yaml and associated image)
        Accepts either the map base name (e.g., "home") or filename (e.g., "home.yaml")
        """
        try:
            requested_name = request.map_name.strip()
            if not requested_name:
                response.success = False
                response.message = "Map name cannot be empty"
                self.get_logger().error(response.message)
                return response

            # Normalize to YAML filename
            map_yaml_name = requested_name if requested_name.endswith('.yaml') else f"{requested_name}.yaml"

            # Validate that the map exists
            if map_yaml_name not in self.available_maps:
                response.success = False
                response.message = f"Error: Map '{map_yaml_name}' not found. Available maps: {self.available_maps}"
                self.get_logger().error(response.message)
                return response

            # Prevent deleting the active map while navigation is running
            if map_yaml_name == self.current_map and self.current_mode == "navigation" and self.current_process and self.current_process.poll() is None:
                response.success = False
                response.message = f"Cannot delete the active map '{map_yaml_name}' while navigation is running. Change map or stop navigation first."
                self.get_logger().error(response.message)
                return response

            # If we are deleting the current map (but not running navigation), pick a fallback
            if map_yaml_name == self.current_map:
                remaining_maps = [m for m in self.available_maps if m != map_yaml_name]
                fallback = None
                if "home.yaml" in remaining_maps:
                    fallback = "home.yaml"
                elif remaining_maps:
                    fallback = remaining_maps[0]
                else:
                    # No maps left, default to home.yaml placeholder
                    fallback = "home.yaml"

                self.current_map = fallback
                self.save_last_map(fallback)
                self.get_logger().info(f"Current map changed to '{fallback}' prior to deletion of '{map_yaml_name}'")

            # Delete files (.yaml and associated image files)
            base_name = os.path.splitext(map_yaml_name)[0]
            yaml_path = os.path.join(self.maps_dir, map_yaml_name)
            pgm_path = os.path.join(self.maps_dir, f"{base_name}.pgm")
            png_path = os.path.join(self.maps_dir, f"{base_name}.png")

            removed_any = False
            for path in [yaml_path, pgm_path, png_path]:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed_any = True
                except Exception as e:
                    self.get_logger().warning(f"Could not remove '{path}': {e}")

            if not removed_any:
                response.success = False
                response.message = f"No files found to delete for map '{map_yaml_name}'"
                self.get_logger().error(response.message)
                return response

            # Refresh available maps list
            self.available_maps = self.discover_maps()

            response.success = True
            response.message = f"Successfully deleted map '{map_yaml_name}'"
            self.get_logger().info(response.message)

        except Exception as e:
            response.success = False
            response.message = f"Error deleting map: {str(e)}"
            self.get_logger().error(response.message)

        return response

    def save_map_callback(self, request, response):
        """
        Service callback to save the current map with a new name
        Only works when in mapping mode
        request.map_name should contain the new map name (e.g., "my_new_map")
        request.overwrite: if true, allows overwriting existing maps
        """
        try:
            map_name = request.map_name.strip()
            
            # Validate we're in mapping mode
            if self.current_mode != "mapping":
                response.success = False
                response.message = f"Cannot save map - not in mapping mode. Current mode: {self.current_mode}"
                self.get_logger().error(response.message)
                return response
            
            # Validate map name
            if not map_name or not map_name.replace('_', '').replace('-', '').isalnum():
                response.success = False
                response.message = f"Invalid map name '{map_name}'. Use alphanumeric characters, underscores, and hyphens only."
                self.get_logger().error(response.message)
                return response
            
            # Check if map already exists
            map_yaml_name = f"{map_name}.yaml"
            is_overwriting = map_yaml_name in self.available_maps
            if is_overwriting:
                if not request.overwrite:
                    response.success = False
                    response.message = f"Map '{map_yaml_name}' already exists. Set overwrite=true to replace it, or choose a different name."
                    self.get_logger().error(response.message)
                    return response
                else:
                    self.get_logger().info(f"Overwriting existing map: {map_yaml_name}")
                    # Remove old files before saving new ones
                    try:
                        old_yaml = os.path.join(self.maps_dir, map_yaml_name)
                        old_pgm = os.path.join(self.maps_dir, f"{map_name}.pgm")
                        if os.path.exists(old_yaml):
                            os.remove(old_yaml)
                        if os.path.exists(old_pgm):
                            os.remove(old_pgm)
                        self.get_logger().info(f"Removed old map files for: {map_name}")
                    except Exception as e:
                        self.get_logger().warning(f"Could not remove old map files: {e}")
            
            # Ensure maps directory exists
            os.makedirs(self.maps_dir, exist_ok=True)
            
            # Create full path for the map (without extension)
            map_path = os.path.join(self.maps_dir, map_name)
            
            self.get_logger().info(f"Saving current map as: {map_name}")
            
            # Use nav2_map_server map_saver_cli to save the map
            save_cmd = [
                'ros2', 'run', 'nav2_map_server', 'map_saver_cli',
                '-f', map_path,
                '--ros-args', '-p', 'save_map_timeout:=5000.0'
            ]
            
            # Run the map saver command
            result = subprocess.run(
                save_cmd,
                capture_output=True,
                text=True,
                timeout=30  # 30 second timeout
            )
            
            if result.returncode == 0:
                # Check if the files were actually created
                yaml_file = f"{map_path}.yaml"
                pgm_file = f"{map_path}.pgm"
                
                if os.path.exists(yaml_file) and os.path.exists(pgm_file):
                    response.success = True
                    action_word = "overwritten" if is_overwriting else "saved"
                    response.message = f"Successfully {action_word} map as '{map_name}.yaml'"
                    self.get_logger().info(response.message)
                    
                    # Refresh available maps list
                    self.available_maps = self.discover_maps()
                    self.get_logger().info(f"Updated available maps: {self.available_maps}")
                else:
                    response.success = False
                    response.message = f"Map saver completed but files not found at {map_path}"
                    self.get_logger().error(response.message)
            else:
                response.success = False
                response.message = f"Map saver failed with return code {result.returncode}: {result.stderr}"
                self.get_logger().error(response.message)
                
        except subprocess.TimeoutExpired:
            response.success = False
            response.message = "Map saving timed out after 30 seconds"
            self.get_logger().error(response.message)
        except Exception as e:
            response.success = False
            response.message = f"Error saving map: {str(e)}"
            self.get_logger().error(response.message)
            
        return response

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
            if target_mode not in ["navigation", "mapping", "mapfree"]:
                response.success = False
                response.message = f"Invalid mode '{target_mode}'. Use 'navigation', 'mapping', or 'mapfree'"
                self.get_logger().error(response.message)
                return response
            
            # Don't restart if already in the requested mode
            if self.current_mode == target_mode and (
                (self.current_process and self.current_process.poll() is None)):
                response.success = True
                response.message = f"Already in {target_mode} mode"
                self.get_logger().info(response.message)
                return response

            # Set mode to switching
            self.current_mode = "switching"
            self.publish_status() # Immediately publish the change

            # For mapfree, launch local-only Nav2 (planner, controller, costmaps) without map/AMCL
            if target_mode == "mapfree":
                # Stop anything running
                if self.current_process:
                    self.get_logger().info(f"Stopping current mode: {self.current_mode}")
                    self.kill_current_process()
                    time.sleep(2)
                launch_cmd = [
                    'ros2', 'launch', 'maurice_nav', 'mapfree_local_nav.launch.py'
                ]
                self.get_logger().info("Starting mapfree local navigation stack...")
                self.current_process = subprocess.Popen(
                    launch_cmd,
                    preexec_fn=os.setsid,
                )
                self.save_last_mode("mapfree")
                time.sleep(3)
                if self.current_process.poll() is None:
                    response.success = True
                    response.message = "Switched to mapfree mode (local Nav2 running)"
                    self.current_mode = "mapfree"
                else:
                    response.success = False
                    response.message = "Failed to start mapfree local navigation"
                    self.current_mode = "none"
                    self.current_process = None
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
            
            # Set the current mode after launch attempt
            self.current_mode = target_mode
            
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