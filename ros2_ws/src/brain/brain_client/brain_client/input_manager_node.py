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
import time
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import SetBool
import json
from typing import Dict, Any

from brain_client.input_loader import InputLoader
from brain_client.input_types import InputDevice
from brain_client.client.proxy_client import ProxyClient
from brain_client.logging_config import UniversalLogger


class InputManagerNode(Node):
    """
    ROS node that manages input devices.
    
    This node acts as a bridge between ROS topics and pure Python input devices.
    Input device authors don't need to know about ROS - they just implement
    the InputDevice interface and access proxy services via self.proxy.
    """
    
    def __init__(self):
        super().__init__('input_manager_node')
        
        # Wrap ROS logger with UniversalLogger
        ros_logger = self.get_logger()
        self.logger = UniversalLogger(enabled=True, wrapped_logger=ros_logger)
        
        self.logger.info("🔌 Starting Input Manager Node...")
        
        # --- Declare proxy config parameters ---
        # Credentials come from environment (INNATE_PROXY_URL, INNATE_SERVICE_KEY)
        # Config comes from ROS parameters (can be set via launch file)
        self.declare_parameter("openai_realtime_model", "gpt-4o-realtime-preview")
        self.declare_parameter("openai_realtime_url", "wss://api.openai.com/v1/realtime")
        self.declare_parameter("openai_transcribe_model", "gpt-4o-mini-transcribe")
        self.declare_parameter("cartesia_voice_id", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
        
        # Build config dict from params
        proxy_config = {
            "openai_realtime_model": self.get_parameter("openai_realtime_model").value,
            "openai_realtime_url": self.get_parameter("openai_realtime_url").value,
            "openai_transcribe_model": self.get_parameter("openai_transcribe_model").value,
            "cartesia_voice_id": self.get_parameter("cartesia_voice_id").value,
        }
        
        # Create proxy client (credentials from env, config from params)
        try:
            self.proxy = ProxyClient(config=proxy_config)
            if self.proxy.is_available():
                self.logger.info(f"✅ Proxy client initialized (URL: {self.proxy.proxy_url[:30]}...)")
            else:
                self.logger.warning("⚠️ Proxy not configured - input devices won't have proxy access")
                self.proxy = None
        except Exception as e:
            self.logger.warning(f"⚠️ Could not initialize proxy client: {e}")
            self.proxy = None
        
        # Load input devices from user workspace
        innate_os_root = os.environ.get('INNATE_OS_ROOT', 
                                        os.path.join(os.path.expanduser('~'), 'innate-os'))
        
        input_directories = [
            os.path.join(innate_os_root, 'inputs'),
        ]
        
        # Pass proxy to loader - it will inject into each input device
        self.input_loader = InputLoader(ros_logger, proxy=self.proxy)
        input_classes = self.input_loader.load_inputs_from_directories(input_directories)
        self.input_devices: Dict[str, InputDevice] = self.input_loader.create_input_instances(
            input_classes, ros_logger
        )
        
        if not self.input_devices:
            self.logger.warning("⚠️ No input devices found!")
        else:
            self.logger.info(f"✅ Loaded {len(self.input_devices)} input devices: {list(self.input_devices.keys())}")
        
        # Set data callback for all input devices
        for input_device in self.input_devices.values():
            input_device.set_data_callback(self._handle_device_data)
        
        # Initialize all input devices (one-time setup)
        for name, input_device in self.input_devices.items():
            try:
                if input_device.initialize():
                    self.logger.info(f"✅ Input device '{name}' initialized")
                else:
                    self.logger.error(f"❌ Input device '{name}' failed to initialize")
            except Exception as e:
                self.logger.error(f"❌ Error initializing input device '{name}': {e}")
        
        # Publishers for sending data to brain_client
        self.chat_in_pub = self.create_publisher(String, '/brain/chat_in', 10)
        self.custom_pub = self.create_publisher(String, '/input_manager/custom', 10)
        
        # Subscribe to active inputs list from brain_client
        self.active_inputs_sub = self.create_subscription(
            String,
            '/input_manager/active_inputs',
            self.handle_active_inputs,
            10
        )
        
        # Subscribe to TTS status for ducking (suppress mic while robot speaks)
        self.tts_status_sub = self.create_subscription(
            String,
            '/tts/is_playing',
            self.handle_tts_status,
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
        
        self.logger.info("✅ Input Manager Node started successfully")
    
    def _handle_device_data(self, device_name: str, data: Dict[str, Any], data_type: str):
        """
        Callback when an input device wants to send data to the agent.
        
        This publishes the data on appropriate topics that brain_client subscribes to.
        
        Args:
            device_name: Name of the input device
            data: Data to send (can be a string or dict)
            data_type: Type of data (chat_in, sensory_input, etc.)
        """
        try:
            # Convert string to dict if needed
            if isinstance(data, str):
                text = data
            else:
                text = data.get('text', '')
            # Format chat messages in standard JavaScript format
            if data_type == "chat_in":
                data_dict = {
                    "text": text,
                    "sender": "user",
                    "timestamp": time.time()
                }
            else:
                # For non-chat data, preserve original structure and add device source
                if isinstance(data, str):
                    data_dict = {"text": data}
                else:
                    data_dict = data.copy()
                data_dict['input_device'] = device_name
            
            # Serialize to JSON
            json_data = json.dumps(data_dict)
            msg = String()
            msg.data = json_data
            
            # Publish to appropriate topic
            if data_type == "chat_in":
                self.chat_in_pub.publish(msg)
                self.logger.debug(f"📤 Published brain/chat_in from '{device_name}': {text[:50]}")
            elif data_type == "custom":
                self.custom_pub.publish(msg)
                self.logger.debug(f"📤 Published custom data from '{device_name}'")
            else:
                self.logger.warning(f"Unknown data type '{data_type}' from device '{device_name}'. Use 'chat_in' or 'custom'.")
                
        except Exception as e:
            self.logger.error(f"Error handling data from device '{device_name}': {e}")
    
    def handle_active_inputs(self, msg: String):
        """
        Handle active inputs list from brain_client.
        
        Message format: {"inputs": ["micro", "camera"]}
        """
        self.logger.info(f"📥 Received active_inputs message: {msg.data}")
        try:
            import json
            data = json.loads(msg.data)
            required_inputs = data.get('inputs', [])
            self.logger.info(f"🎯 Processing inputs: {required_inputs}")
            
            # Activate/deactivate devices based on requirements
            for name, device in self.input_devices.items():
                was_active = device.is_active()
                should_be_active = name in required_inputs
                
                if should_be_active and not was_active:
                    # Activate device
                    device.set_active(True)
                    device.on_open()
                    self.logger.info(f"🔌 Opened input device: {name}")
                elif not should_be_active and was_active:
                    # Deactivate device
                    device.on_close()
                    device.set_active(False)
                    self.logger.info(f"💤 Closed input device: {name}")
                    
        except Exception as e:
            self.logger.error(f"Error handling active inputs: {e}")
    
    def handle_tts_status(self, msg: String):
        """
        Handle TTS status updates and notify input devices that support ducking.
        """
        try:
            is_playing = msg.data.lower() in ('true', '1', 'playing')
            
            # Update devices that support ducking (have set_tts_playing method)
            for device in self.input_devices.values():
                if hasattr(device, 'set_tts_playing'):
                    device.set_tts_playing(is_playing)
        except Exception as e:
            self.logger.error(f"Error handling TTS status: {e}")
    
    def handle_set_input_active(self, request, response):
        """
        Service to activate/deactivate all input devices (for manual control).
        
        Request format: SetBool with data=true/false
        """
        try:
            # Activate/deactivate all devices
            for name, device in self.input_devices.items():
                was_active = device.is_active()
                should_be_active = request.data
                
                if should_be_active and not was_active:
                    # Activate device
                    device.set_active(True)
                    device.on_open()
                    self.logger.info(f"🔌 Opened input device: {name}")
                elif not should_be_active and was_active:
                    # Deactivate device
                    device.on_close()
                    device.set_active(False)
                    self.logger.info(f"💤 Closed input device: {name}")
            
            response.success = True
            response.message = f"All inputs {'activated' if request.data else 'deactivated'}"
        except Exception as e:
            response.success = False
            response.message = str(e)
            self.logger.error(f"Error in set_input_active: {e}")
        
        return response
    
    def destroy_node(self):
        """Clean up all input devices."""
        self.logger.info("Shutting down input devices...")
        for name, device in self.input_devices.items():
            try:
                # Close if active
                if device.is_active():
                    device.on_close()
                # Final shutdown
                device.shutdown()
                self.logger.info(f"✅ Shut down input device '{name}'")
            except Exception as e:
                self.logger.error(f"Error shutting down input device '{name}': {e}")
        
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

