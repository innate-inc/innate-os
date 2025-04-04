#!/usr/bin/env python3
import logging
import json
import signal
import sys

from bluezero import adapter
from bluezero import peripheral

# Import NetworkManager utilities
from .nmcli_utils import (
    nmcli_get_wifi_connections,
    nmcli_add_or_modify_connection,
    nmcli_delete_connection,
    nmcli_scan_for_ssid,
    nmcli_connect,
    nmcli_get_active_wifi_ssid
)

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   stream=sys.stdout)
logger = logging.getLogger('BLE_Server')

# BLE UUIDs
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHARACTERISTIC_UUID = 'abcdef01-1234-5678-1234-56789abcdef0'

# --- BLE Server Class ---
class BleProvisionerServer:
    def __init__(self, adapter_obj: adapter.Adapter):
        """Initialize the BLE Provisioner Server."""
        self.adapter = adapter_obj
        self._ble_characteristic = None
        self.peripheral = None
        # self.nm_interface = NetworkManagerInterface() # Removed instantiation

    # --- Command Handlers ---
    def handle_get_status(self, data):
        """Handle get_status command."""
        logger.info("Handling get_status command")
        
        # Get configured networks
        success_list, networks, error_msg_list = nmcli_get_wifi_connections()
        
        # Get active network
        active_ssid = nmcli_get_active_wifi_ssid()
        # Note: nmcli_get_active_wifi_ssid handles its own logging/errors, returns None on failure

        if success_list:
            # Include both configured list and active SSID in success response
            return {
                "status": "success", 
                "networks": networks, 
                "active_ssid": active_ssid # Will be SSID string or None
            }
        else:
             # If fetching the list failed, report that error, but still include active SSID if found
             return {
                 "status": "error", 
                 "message": error_msg_list,
                 "networks": [], # Return empty list on error
                 "active_ssid": active_ssid
             }

    def handle_update_network(self, data):
        """Handle update_network command."""
        logger.info("Handling update_network command")
        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        password = network_data.get('password') # Get password if provided
        priority = network_data.get('priority', 10) # Default priority

        if not ssid:
            return {"status": "error", "message": "SSID required"}

        # Step 1: Add or Modify the connection profile
        success_update, err_update = nmcli_add_or_modify_connection(ssid, password, priority)
        if not success_update:
            return {"status": "error", "message": err_update}
        
        # If update succeeded, proceed to scan and connect
        logger.info(f"Network profile '{ssid}' updated successfully.")

        # Step 2: Scan for the network
        success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
        if not success_scan:
            # Report as warning: profile saved, but scan failed
            return {"status": "warning", "message": f"Network {ssid} updated, but scan failed: {err_scan}"}

        if not visible:
             logger.info(f"Target network '{ssid}' not visible after scan. Profile saved.")
             return {"status": "success", "message": f"Network {ssid} updated. Network not currently visible for connection."}

        # Step 3: Attempt connection if visible
        logger.info(f"Target network '{ssid}' is visible. Attempting connection...")
        success_connect, connect_msg_or_err = nmcli_connect(ssid)
        if success_connect:
            logger.info(f"Connection initiated for {ssid}. Details: {connect_msg_or_err}")
            return {"status": "success", "message": f"Network {ssid} updated and connection initiated."}
        else:
            logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
            return {"status": "warning", "message": f"Network {ssid} updated, but connection attempt failed: {connect_msg_or_err}"}

    def handle_remove_network(self, data):
        """Handle remove_network command."""
        logger.info("Handling remove_network command")
        ssid = data.get('data', {}).get('ssid')

        if not ssid:
            return {"status": "error", "message": "SSID required for removal"}

        logger.info(f"Attempting to remove network profile: {ssid}")
        success, error_msg = nmcli_delete_connection(ssid)

        if success:
            logger.info(f"Successfully deleted NetworkManager profile: {ssid}")
            return {"status": "success", "message": f"Network profile {ssid} removed"}
        else:
            logger.warning(f"Failed to remove network profile '{ssid}': {error_msg}")
            # Pass the error message from the utility function back to the client
            return {"status": "error", "message": error_msg}

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
        logger.info("Read request received - fetching networks from NetworkManager")
        success, networks, error_msg = nmcli_get_wifi_connections()

        response_status = "success" if success else "error"
        response = {"status": response_status, "networks": networks}
        if error_msg:
            response["message"] = error_msg
        
        logger.info(f"Read callback returning status '{response_status}' with {len(networks)} networks.")
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
        # self.load_networks() # Removed call

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
        
        # self.save_networks() # Removed call
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