#!/usr/bin/env python3
import logging
import json
import signal
import sys
import subprocess
import time

from bluezero import adapter
from bluezero import peripheral

# Import NetworkManager utilities
from nmcli_utils import (
    nmcli_get_wifi_connections,
    nmcli_add_or_modify_connection,
    nmcli_delete_connection,
    nmcli_scan_for_ssid,
    nmcli_connect,
    nmcli_get_active_wifi_ssid,
    nmcli_get_active_ipv4_address,
    nmcli_scan_for_visible_ssids
)

# Set up logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   stream=sys.stdout)
logger = logging.getLogger('BLE_Server')

# BLE UUIDs
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHARACTERISTIC_UUID = 'abcdef01-1234-5678-1234-56789abcdef0'

# Path to the helper script for restarting services (adjust if moved)
RESTART_SCRIPT_PATH = "/usr/local/bin/restart_robot_networking.sh"

# --- BLE Server Class ---
class BleProvisionerServer:
    def __init__(self, adapter_obj: adapter.Adapter):
        """Initialize the BLE Provisioner Server."""
        self.adapter = adapter_obj
        self._ble_characteristic = None
        self.peripheral = None
        self._current_ip_address = nmcli_get_active_ipv4_address() # Store initial IP
        logger.info(f"Initial IPv4 address: {self._current_ip_address}")

    # --- Helper to Trigger Service Restart ---
    def _trigger_service_restart(self):
        """Calls the restart script via sudo if the IP address has changed."""
        new_ip = nmcli_get_active_ipv4_address()
        logger.info(f"Checking for IP change. Current: {self._current_ip_address}, New: {new_ip}")

        if new_ip != self._current_ip_address:
            logger.warning(f"IP address changed from {self._current_ip_address} to {new_ip}. Triggering service restarts.")
            try:
                # Use run with check=True to raise an exception if the script fails
                # Use capture_output=True to get stdout/stderr
                # Use text=True for easier handling of stdout/stderr
                result = subprocess.run(['sudo', RESTART_SCRIPT_PATH], 
                                        check=True, 
                                        capture_output=True, 
                                        text=True, 
                                        timeout=30) # Add a timeout
                logger.info(f"Service restart script executed successfully. Output:\n{result.stdout}")
                self._current_ip_address = new_ip # Update stored IP only on success
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to execute restart script '{RESTART_SCRIPT_PATH}': {e}")
                logger.error(f"stdout: {e.stdout}")
                logger.error(f"stderr: {e.stderr}")
                # Decide if you want to update self._current_ip_address here or retry
            except FileNotFoundError:
                 logger.error(f"Error: Restart script '{RESTART_SCRIPT_PATH}' not found. Ensure it's installed correctly and the path is right.")
            except subprocess.TimeoutExpired:
                logger.error(f"Error: Restart script '{RESTART_SCRIPT_PATH}' timed out after 30 seconds.")
            except Exception as e:
                 logger.error(f"An unexpected error occurred during restart script execution: {e}", exc_info=True)
        else:
            logger.info("IP address unchanged. No service restart needed.")

    # --- Command Handlers ---
    def handle_get_status(self, data):
        """Handle get_status command."""
        command = data.get('command')
        logger.info(f"Handling {command} command")
        
        # Get configured networks
        success_list, networks, error_msg_list = nmcli_get_wifi_connections()
        
        # Get active network
        active_ssid = nmcli_get_active_wifi_ssid()
        # Note: nmcli_get_active_wifi_ssid handles its own logging/errors, returns None on failure
        
        # Get active IPv4 address
        active_ip = nmcli_get_active_ipv4_address()
        # Note: nmcli_get_active_ipv4_address handles its own logging/errors, returns None on failure

        if success_list:
            # Include both configured list and active SSID in success response
            return {
                "command": command,
                "status": "success", 
                "networks": networks, 
                "active_ssid": active_ssid, # Will be SSID string or None
                "active_ip": active_ip # Will be IPv4 string or None
            }
        else:
             # If fetching the list failed, report that error, but still include active SSID if found
             return {
                 "command": command,
                 "status": "error", 
                 "message": error_msg_list,
                 "networks": [], # Return empty list on error
                 "active_ssid": active_ssid,
                 "active_ip": active_ip
             }

    def handle_update_network(self, data):
        """Handle update_network command."""
        command = data.get('command')
        logger.info(f"Handling {command} command")
        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        password = network_data.get('password') # Get password if provided
        priority = network_data.get('priority', 10) # Default priority

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required"}

        # --- Store current IP before attempting changes --- 
        self._current_ip_address = nmcli_get_active_ipv4_address() 
        logger.info(f"IP before update/connect attempt: {self._current_ip_address}")

        # Step 1: Add or Modify the connection profile
        success_update, err_update = nmcli_add_or_modify_connection(ssid, password, priority)
        if not success_update:
            return {"command": command, "status": "error", "message": err_update}
        
        logger.info(f"Network profile '{ssid}' updated successfully.")

        # Step 2: Scan for the network
        success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
        if not success_scan:
            # Report as warning: profile saved, but scan failed
            return {"command": command, "status": "warning", "message": f"Network {ssid} updated, but scan failed: {err_scan}"}

        if not visible:
             logger.info(f"Target network '{ssid}' not visible after scan. Profile saved.")
             # Even if not visible now, the profile is saved. Don't trigger restart yet.
             return {"command": command, "status": "success", "message": f"Network {ssid} updated. Network not currently visible for connection."}

        # Step 3: Attempt connection if visible
        logger.info(f"Target network '{ssid}' is visible. Attempting connection...")
        success_connect, connect_msg_or_err = nmcli_connect(ssid)
        
        # --- Check IP and restart services AFTER connection attempt --- 
        if success_connect:
            logger.info(f"Connection initiated for {ssid}. Waiting briefly for network stabilization...")
            time.sleep(5) # Give the network/DHCP some time
            self._trigger_service_restart() # Check IP and restart if needed
            return {"command": command, "status": "success", "message": f"Network {ssid} updated and connection initiated."}
        else:
            logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
            # Don't trigger restart if connection failed
            return {"command": command, "status": "warning", "message": f"Network {ssid} updated, but connection attempt failed: {connect_msg_or_err}"}

    def handle_remove_network(self, data):
        """Handle remove_network command."""
        command = data.get('command')
        logger.info(f"Handling {command} command")
        ssid = data.get('data', {}).get('ssid')

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for removal"}

        logger.info(f"Attempting to remove network profile: {ssid}")
        success, error_msg = nmcli_delete_connection(ssid)

        if success:
            logger.info(f"Successfully deleted NetworkManager profile: {ssid}")
            return {"command": command, "status": "success", "message": f"Network profile {ssid} removed"}
        else:
            logger.warning(f"Failed to remove network profile '{ssid}': {error_msg}")
            # Pass the error message from the utility function back to the client
            return {"command": command, "status": "error", "message": error_msg}

    def handle_scan_wifi(self, data):
        """Handle scan_wifi command.
        Performs a scan and returns a list of visible SSIDs.
        """
        command = data.get('command')
        logger.info(f"Handling {command} command")

        success, ssids, error_msg = nmcli_scan_for_visible_ssids()

        if success:
            return {"command": command, "status": "success", "visible_ssids": ssids}
        else:
            return {"command": command, "status": "error", "message": error_msg, "visible_ssids": []}

    def handle_unknown_command(self, command):
        """Handle unknown commands."""
        logger.warning(f"Unknown command received: {command}")
        # Use the received command string, or 'unknown' if None
        return {"command": command or "unknown", "status": "error", "message": "Unknown command"}

    def handle_connect_network(self, data):
        """Handle connect_network command."""
        command = data.get('command')
        logger.info(f"Handling {command} command")
        ssid = data.get('data', {}).get('ssid')

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for connection"}

        logger.info(f"Attempting to connect to network: {ssid}")
        
        # --- Store current IP before attempting changes --- 
        self._current_ip_address = nmcli_get_active_ipv4_address() 
        logger.info(f"IP before connect attempt: {self._current_ip_address}")

        # Check if the network is visible (optional but good practice)
        success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
        if not success_scan:
            logger.warning(f"Scan for '{ssid}' failed before connection attempt: {err_scan}")
        elif not visible:
            logger.warning(f"Network '{ssid}' not visible, connection attempt might fail.")
            # Allow connection attempt anyway

        # Attempt connection
        success_connect, connect_msg_or_err = nmcli_connect(ssid)

        # --- Check IP and restart services AFTER connection attempt --- 
        if success_connect:
            logger.info(f"Connection initiated for {ssid}. Waiting briefly for network stabilization...")
            time.sleep(5) # Give the network/DHCP some time
            self._trigger_service_restart() # Check IP and restart if needed
            return {"command": command, "status": "success", "message": f"Connection initiated for {ssid}"}
        else:
            logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
            # Don't trigger restart if connection failed
            return {"command": command, "status": "error", "message": f"Connection attempt failed for {ssid}: {connect_msg_or_err}"}

    # --- BLE Callbacks ---
    def write_callback(self, value, options=None):
        """Handle write requests from BLE clients."""
        logger.debug(f"write_callback invoked with value (type {type(value)}): {value}")
        logger.debug(f"Options: {options}")
        response = None
        try:
            value_str = bytes(value).decode('utf-8')
            logger.info(f"Received write: {value_str}")
            
            # Load JSON data first to get the command, even for error reporting
            try:
                data = json.loads(value_str)
                command = data.get('command')
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON: {value_str}")
                # Respond with error, indicating unknown command due to parse failure
                response = {"command": "unknown", "status": "error", "message": "Invalid JSON format"}
                # Jump to sending response directly
                if self._ble_characteristic and self._ble_characteristic.is_notifying:
                    logger.info(f"Sending notification response: {response}")
                    try:
                        response_bytes = bytes(json.dumps(response), 'utf-8')
                        self._ble_characteristic.set_value(list(response_bytes))
                    except Exception as e:
                        logger.error(f"Error sending notification: {e}")
                return bytes(json.dumps(response), 'utf-8') if response else b''
            
            if command == 'get_status':
                response = self.handle_get_status(data)
            elif command == 'update_network':
                response = self.handle_update_network(data)
            elif command == 'remove_network':
                response = self.handle_remove_network(data)
            elif command == 'connect_network':
                response = self.handle_connect_network(data)
            elif command == 'scan_wifi':
                response = self.handle_scan_wifi(data)
            else:
                response = self.handle_unknown_command(command)
            
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON: {value_str}")
            # This block should technically not be reached due to inner try-except,
            # but included for safety.
            response = {"command": "unknown", "status": "error", "message": "Invalid JSON format"}
        except Exception as e:
            # Try to get command from data if available, otherwise use 'unknown'
            cmd_for_log = data.get('command', 'unknown') if 'data' in locals() else 'unknown'
            logger.error(f"Error processing command '{cmd_for_log}': {e}", exc_info=True)
            response = {"command": cmd_for_log, "status": "error", "message": f"Server error: {str(e)}"}
        
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
        # Add a command field to read response as well for consistency, e.g., 'read_status'
        response = {"command": "read_status", "status": response_status, "networks": networks}
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