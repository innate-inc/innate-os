#!/usr/bin/env python3
import logging
import json
import signal
import sys
import os

from ament_index_python.packages import get_package_share_directory
from bluezero import async_tools
from bluezero import adapter
from bluezero import peripheral

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   stream=sys.stdout)
logger = logging.getLogger('BLE_Server')

# BLE UUIDs
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHARACTERISTIC_UUID = 'abcdef01-1234-5678-1234-56789abcdef0'

class BleProvisionerServer:
    def __init__(self, adapter_obj: adapter.Adapter, package_name='maurice_bt_provisioner'):
        """Initialize the BLE Provisioner Server."""
        self.adapter = adapter_obj
        self.package_name = package_name
        self.networks = []
        self._ble_characteristic = None
        self.peripheral = None

        # Get package share directory for storing networks file
        try:
            pkg_share = get_package_share_directory(self.package_name)
            self.networks_file = os.path.join(pkg_share, 'wifi_networks.json')
        except Exception as e:
            logger.error(f"Could not find package '{self.package_name}'. Storing networks file in current directory. Error: {e}")
            self.networks_file = 'wifi_networks.json' # Fallback

    def load_networks(self):
        """Load networks from the JSON file."""
        try:
            if os.path.exists(self.networks_file):
                with open(self.networks_file, 'r') as f:
                    self.networks = json.load(f)
                    logger.info(f"Loaded {len(self.networks)} networks from {self.networks_file}")
            else:
                logger.info(f"No networks file found at {self.networks_file}, starting with empty list")
                self.networks = []
        except Exception as e:
            logger.error(f"Error loading networks from {self.networks_file}: {e}")
            self.networks = []

    def save_networks(self):
        """Save networks to the JSON file."""
        try:
            with open(self.networks_file, 'w') as f:
                json.dump(self.networks, f, indent=2)
                logger.info(f"Saved {len(self.networks)} networks to {self.networks_file}")
        except Exception as e:
            logger.error(f"Error saving networks to {self.networks_file}: {e}")

    # --- Command Handlers ---
    def handle_get_status(self, data):
        """Handle get_status command."""
        logger.info("Handling get_status command")
        return {"status": "success", "networks": self.networks}

    def handle_update_network(self, data):
        """Handle update_network command."""
        logger.info("Handling update_network command")
        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        
        if ssid:
            found = False
            for net in self.networks:
                if net['ssid'] == ssid:
                    net['priority'] = network_data.get('priority', 10)
                    found = True
                    break
            
            if not found:
                self.networks.append({
                    'ssid': ssid,
                    'priority': network_data.get('priority', 10)
                })
            
            self.save_networks()
            return {"status": "success", "message": f"Network {ssid} updated"}
        else:
            return {"status": "error", "message": "SSID required"}

    def handle_connect_network(self, data):
        """Handle connect_network command."""
        logger.info("Handling connect_network command")
        # TODO: Implement actual network connection logic here
        return {"status": "success", "message": "Connecting to highest priority network (simulation)"}

    def handle_remove_network(self, data):
        """Handle remove_network command."""
        logger.info("Handling remove_network command")
        ssid = data.get('data', {}).get('ssid')
        initial_length = len(self.networks)
        
        logger.info(f"Removing network: {ssid} and networks are now: {self.networks}")
        
        if ssid:
            self.networks = [net for net in self.networks if net['ssid'] != ssid]
            if len(self.networks) < initial_length:
                self.save_networks()
                logger.info(f"Removed network: {ssid}")
                return {"status": "success", "message": f"Network {ssid} removed"}
            else:
                logger.warning(f"Network not found for removal: {ssid}")
                return {"status": "error", "message": "Network not found"}
        else:
             return {"status": "error", "message": "SSID required for removal"}

    def handle_unknown_command(self, command):
        """Handle unknown commands."""
        logger.warning(f"Unknown command received: {command}")
        return {"status": "error", "message": "Unknown command"}

    # --- BLE Callbacks ---
    def write_callback(self, value, options=None):
        """Handle write requests from BLE clients."""
        logger.debug(f"write_callback invoked with value (type {type(value)}): {value}")
        logger.debug(f"Options: {options}")
        response = None
        try:
            value_str = bytes(value).decode('utf-8')
            logger.info(f"Received write: {value_str}")
            
            data = json.loads(value_str)
            command = data.get('command')
            
            if command == 'get_status':
                response = self.handle_get_status(data)
            elif command == 'update_network':
                response = self.handle_update_network(data)
            elif command == 'connect_network':
                response = self.handle_connect_network(data)
            elif command == 'remove_network':
                response = self.handle_remove_network(data)
            else:
                response = self.handle_unknown_command(command)
            
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON: {value_str}")
            response = {"status": "error", "message": "Invalid JSON format"}
        except Exception as e:
            logger.error(f"Error processing command '{command if 'command' in locals() else 'unknown'}': {e}", exc_info=True)
            response = {"status": "error", "message": f"Server error: {str(e)}"}
        
        # Send response via notification if possible
        if response and self._ble_characteristic and self._ble_characteristic.is_notifying:
            logger.info(f"Sending notification response: {response}")
            try:
                response_bytes = bytes(json.dumps(response), 'utf-8')
                self._ble_characteristic.set_value(list(response_bytes))
            except Exception as e:
                 logger.error(f"Error sending notification: {e}")

        # Return value for compatibility (though notifications are preferred)
        return bytes(json.dumps(response), 'utf-8') if response else b''

    def read_callback(self):
        """Handle read requests from BLE clients."""
        logger.info("Read request received")
        response = {"status": "connected", "networks": self.networks}
        return bytes(json.dumps(response), 'utf-8')

    def notify_callback(self, notifying, characteristic):
        """Handle notification subscription changes."""
        logger.info(f"Notifications {'started' if notifying else 'stopped'}")
        if notifying:
            self._ble_characteristic = characteristic
        else:
            self._ble_characteristic = None # Clear reference when client unsubscribes

    # --- Connection Callbacks ---
    def on_connect(self, device):
        logger.info(f"Client connected: {device.address}")
    
    def on_disconnect(self, device):
        logger.info(f"Client disconnected: {device.address}")
        self._ble_characteristic = None # Clear characteristic on disconnect

    # --- Main Server Logic ---
    def start(self):
        """Initialize and run the BLE server."""
        logger.info("Starting BLE WiFi Provisioner Server")
        self.load_networks() # Load networks at startup

        adapter_address = self.adapter.address
        logger.info(f"Using adapter at address: {adapter_address}")
        logger.info(f"Adapter properties: Discoverable={self.adapter.discoverable}, Powered={self.adapter.powered}, Pairable={self.adapter.pairable}")

        try:
            self.peripheral = peripheral.Peripheral(adapter_address,
                                                 local_name='ROS2_BLE_Provisioner',
                                                 appearance=192) # 192: Generic Computer

            # Set connection callbacks
            if hasattr(self.peripheral, 'on_connect'):
                self.peripheral.on_connect = self.on_connect
            if hasattr(self.peripheral, 'on_disconnect'):
                self.peripheral.on_disconnect = self.on_disconnect
            
            # Add service
            self.peripheral.add_service(srv_id=1, uuid=SERVICE_UUID, primary=True)
            
            # Add characteristic
            self.peripheral.add_characteristic(
                srv_id=1, 
                chr_id=1, 
                uuid=CHARACTERISTIC_UUID,
                value=[], # Initial value is empty
                notifying=False,
                flags=['read', 'write', 'write-without-response', 'notify'],
                read_callback=self.read_callback,
                write_callback=self.write_callback,
                notify_callback=self.notify_callback
            )
            
            logger.info(f"Service {SERVICE_UUID} and Characteristic {CHARACTERISTIC_UUID} added.")
            
            # Set up signal handler for graceful shutdown
            # Define inside start() to capture self
            def signal_handler(sig, frame):
                logger.info("SIGINT received, stopping BLE service...")
                self.stop()

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler) # Handle termination signal too
            
            # Start advertising
            logger.info("Starting BLE advertisement...")
            self.peripheral.publish()
            logger.info("BLE Server is running. Press Ctrl+C to stop.")
        
        except Exception as e:
            logger.critical(f"Failed to initialize or start BLE service: {e}", exc_info=True)
            self.stop() # Attempt cleanup even on startup failure

    def stop(self):
        """Stop the BLE server gracefully."""
        logger.info("Stopping BLE server...")
        if self.peripheral:
            # Check if mainloop exists and quit if so
            if hasattr(self.peripheral, 'mainloop') and self.peripheral.mainloop:
                try:
                    self.peripheral.mainloop.quit()
                    logger.info("Main event loop stopped.")
                except Exception as e:
                    logger.error(f"Error stopping mainloop: {e}")
            # Clean up peripheral resources (disconnect, remove services etc.)
            # Note: bluezero might handle some of this implicitly on mainloop quit
            # self.peripheral.remove_service(1) # Example cleanup if needed
            self.peripheral = None # Clear reference
        
        self.save_networks() # Save networks before exiting
        logger.info("BLE Provisioner Server stopped.")

# --- Main Execution Block ---
if __name__ == '__main__':
    # Find a Bluetooth adapter
    available_adapters = list(adapter.Adapter.available())
    if not available_adapters:
        logger.error("No Bluetooth adapters found. Please ensure Bluetooth is enabled and accessible.")
        sys.exit(1)
    
    # Use the first available adapter
    selected_adapter = available_adapters[0]
    
    # Create and start the server
    ble_server = BleProvisionerServer(selected_adapter)
    ble_server.start()