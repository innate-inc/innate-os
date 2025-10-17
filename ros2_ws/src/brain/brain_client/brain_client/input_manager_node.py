#!/usr/bin/env python3
"""
Input Manager Node

A separate ROS node that manages input devices. This node:
1. Loads input device classes from directories
2. Subscribes to ROS topics specified by input devices
3. Calls input device process_data() when messages arrive
4. Publishes processed data from input devices
5. Handles activation/deactivation of inputs based on directives

This keeps ROS complexity separate from the input device implementations.
"""

import os
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import SetBool
import json
from typing import Dict, Any

from brain_client.input_loader import InputLoader
from brain_client.input_types import InputDevice


class InputManagerNode(Node):
    """
    ROS node that manages input devices.
    
    This node acts as a bridge between ROS topics and pure Python input devices.
    Input device authors don't need to know about ROS - they just implement
    the InputDevice interface.
    """
    
    def __init__(self):
        super().__init__('input_manager_node')
        
        self.get_logger().info("🔌 Starting Input Manager Node...")
        
        # Load input devices from directories
        innate_os_root = os.environ.get('INNATE_OS_ROOT', 
                                        os.path.join(os.path.expanduser('~'), 'innate-os'))
        
        input_directories = [
            os.path.join(innate_os_root, 'inputs'),
            os.path.join(os.path.dirname(__file__), 'inputs')
        ]
        
        self.input_loader = InputLoader(self.get_logger())
        input_classes = self.input_loader.load_inputs_from_directories(input_directories)
        self.input_devices: Dict[str, InputDevice] = self.input_loader.create_input_instances(
            input_classes, self.get_logger()
        )
        
        if not self.input_devices:
            self.get_logger().warning("⚠️ No input devices found!")
        else:
            self.get_logger().info(f"✅ Loaded {len(self.input_devices)} input devices: {list(self.input_devices.keys())}")
        
        # Set data callback for all input devices
        for input_device in self.input_devices.values():
            input_device.set_data_callback(self._handle_device_data)
        
        # Initialize all input devices (one-time setup)
        for name, input_device in self.input_devices.items():
            try:
                if input_device.initialize():
                    self.get_logger().info(f"✅ Input device '{name}' initialized")
                else:
                    self.get_logger().error(f"❌ Input device '{name}' failed to initialize")
            except Exception as e:
                self.get_logger().error(f"❌ Error initializing input device '{name}': {e}")
        
        # Subscribe to /chat_in (from voice_client) to bridge to MicroInput
        # This is a hardcoded bridge - in the future, could be configurable
        self.chat_in_sub = self.create_subscription(
            String,
            '/chat_in',
            self._handle_chat_in,
            10
        )
        self.get_logger().info("📡 Subscribed to /chat_in for voice transcripts")
        
        # Publishers for sending data to brain_client
        self.chat_in_pub = self.create_publisher(String, '/input_manager/chat_in', 10)
        self.custom_pub = self.create_publisher(String, '/input_manager/custom', 10)
        
        # Subscribe to active inputs list from brain_client
        self.active_inputs_sub = self.create_subscription(
            String,
            '/input_manager/active_inputs',
            self.handle_active_inputs,
            10
        )
        
        # Service to activate/deactivate specific inputs (kept for manual control)
        self.set_input_active_srv = self.create_service(
            SetBool,
            '/input_manager/set_input_active',
            self.handle_set_input_active
        )
        
        # Note: In future, add a custom service to set multiple inputs at once
        # based on directive requirements
        
        self.get_logger().info("✅ Input Manager Node started successfully")
    
    def _handle_chat_in(self, msg: String):
        """
        Bridge between voice_client and MicroInput device.
        
        This is where ROS meets the pure Python input devices.
        """
        try:
            # If we have a micro input device, pass the data to it
            if 'micro' in self.input_devices:
                micro_device = self.input_devices['micro']
                # Call the device's receive method (if it has one)
                if hasattr(micro_device, 'receive_transcript'):
                    micro_device.receive_transcript(msg.data)
        except Exception as e:
            self.get_logger().error(f"Error bridging chat_in to MicroInput: {e}")
    
    def _handle_device_data(self, device_name: str, data: Dict[str, Any], data_type: str):
        """
        Callback when an input device wants to send data to the agent.
        
        This publishes the data on appropriate topics that brain_client subscribes to.
        
        Args:
            device_name: Name of the input device
            data: Data to send
            data_type: Type of data (chat_in, sensory_input, etc.)
        """
        try:
            # Add device source to data
            data_with_source = data.copy()
            data_with_source['input_device'] = device_name
            
            # Serialize to JSON
            json_data = json.dumps(data_with_source)
            msg = String()
            msg.data = json_data
            
            # Publish to appropriate topic
            if data_type == "chat_in":
                self.chat_in_pub.publish(msg)
                self.get_logger().debug(f"📤 Published chat_in from '{device_name}': {data.get('text', '')[:50]}")
            elif data_type == "custom":
                self.custom_pub.publish(msg)
                self.get_logger().debug(f"📤 Published custom data from '{device_name}'")
            else:
                self.get_logger().warning(f"Unknown data type '{data_type}' from device '{device_name}'. Use 'chat_in' or 'custom'.")
                
        except Exception as e:
            self.get_logger().error(f"Error handling data from device '{device_name}': {e}")
    
    def handle_active_inputs(self, msg: String):
        """
        Handle active inputs list from brain_client.
        
        Message format: {"inputs": ["micro", "camera"]}
        """
        try:
            import json
            data = json.loads(msg.data)
            required_inputs = data.get('inputs', [])
            
            # Activate/deactivate devices based on requirements
            for name, device in self.input_devices.items():
                was_active = device.is_active()
                should_be_active = name in required_inputs
                
                if should_be_active and not was_active:
                    # Activate device
                    device.set_active(True)
                    device.on_open()
                    self.get_logger().info(f"🔌 Opened input device: {name}")
                elif not should_be_active and was_active:
                    # Deactivate device
                    device.on_close()
                    device.set_active(False)
                    self.get_logger().info(f"💤 Closed input device: {name}")
                    
        except Exception as e:
            self.get_logger().error(f"Error handling active inputs: {e}")
    
    def handle_set_input_active(self, request, response):
        """
        Service to activate/deactivate all input devices (for manual control).
        
        Request format: SetBool with data=true/false
        """
        try:
            # For now, this activates/deactivates all devices
            for device in self.input_devices.values():
                device.set_active(request.data)
            
            response.success = True
            response.message = f"All inputs {'activated' if request.data else 'deactivated'}"
        except Exception as e:
            response.success = False
            response.message = str(e)
        
        return response
    
    def destroy_node(self):
        """Clean up all input devices."""
        self.get_logger().info("Shutting down input devices...")
        for name, device in self.input_devices.items():
            try:
                # Close if active
                if device.is_active():
                    device.on_close()
                # Final shutdown
                device.shutdown()
                self.get_logger().info(f"✅ Shut down input device '{name}'")
            except Exception as e:
                self.get_logger().error(f"Error shutting down input device '{name}': {e}")
        
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = InputManagerNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

