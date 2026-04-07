#!/usr/bin/env python3
import logging
import json
import signal
import sys
import subprocess
import time
import os
import threading

from bluezero import adapter
from bluezero import peripheral
from gi.repository import GLib

# Import NetworkManager utilities
from nmcli_utils import (
    nmcli_get_wifi_connections,
    nmcli_add_or_modify_connection,
    nmcli_delete_connection,
    nmcli_scan_for_ssid,
    nmcli_connect,
    nmcli_get_active_wifi_ssid,
    nmcli_get_active_ipv4_address,
    nmcli_scan_for_visible_ssids,
    nmcli_set_autoconnect,
)

# IMPORTANT NOTES
# 1. Don't forget to set the conf file (sudo nano /etc/bluetooth/main.conf) with ControllerMode on "le", not "dual". It's a HUGE reason why it might not work or very rarely.


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

# Load robot name from robot_info.json
def load_robot_name():
    """Load robot name from robot_info.json file."""
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    robot_info_path = os.path.join(maurice_root, 'data', 'robot_info.json')
    
    with open(robot_info_path, 'r') as f:
        data = json.load(f)
        return data.get('robot_name', 'MARS')

ROBOT_NAME = load_robot_name()

# --- BLE Server Class ---
class BleProvisionerServer:
    def __init__(self, adapter_obj: adapter.Adapter):
        """Initialize the BLE Provisioner Server."""
        self.adapter = adapter_obj
        self._ble_characteristic = None
        self.peripheral = None
        self._connected_device = None  # Track connected device for disconnect
        self._current_ip_address = nmcli_get_active_ipv4_address() # Store initial IP
        self._connection_lock = threading.Lock()
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

    # --- Notification helpers ---

    def _send_notification(self, response):
        """Send a BLE notification. Must be called from the GLib main thread."""
        if self._ble_characteristic and self._ble_characteristic.is_notifying:
            try:
                response_bytes = bytes(json.dumps(response), 'utf-8')
                self._ble_characteristic.set_value(list(response_bytes))
                logger.info(f"Sent notification: {response}")
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return False  # returning False prevents GLib.idle_add from re-invoking

    def _send_notification_threadsafe(self, response):
        """Schedule a BLE notification on the GLib main loop (safe from any thread)."""
        GLib.idle_add(self._send_notification, response)

    # --- Background connect ---

    def _background_connect(self, command, ssid, enable_autoconnect_on_success):
        """Run scan + connect in a background thread, send notification when done."""
        try:
            success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
            if not success_scan:
                logger.warning(f"Scan for '{ssid}' failed: {err_scan}")
            elif not visible:
                logger.info(f"Target network '{ssid}' not visible after scan.")
                if command == 'update_network':
                    self._send_notification_threadsafe({
                        "command": command, "status": "success",
                        "message": f"Network {ssid} saved. Not currently visible for connection.",
                    })
                    return
                # For connect_network, still try — cached credentials may work

            logger.info(f"Attempting connection to '{ssid}'...")
            success_connect, connect_msg_or_err = nmcli_connect(ssid)

            if success_connect:
                if enable_autoconnect_on_success:
                    nmcli_set_autoconnect(ssid, True)
                logger.info(f"Connection initiated for {ssid}. Waiting for network stabilization...")
                time.sleep(5)
                self._trigger_service_restart()
                self._send_notification_threadsafe({
                    "command": command, "status": "success",
                    "message": f"Connected to {ssid}.",
                })
            else:
                logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
                self._send_notification_threadsafe({
                    "command": command, "status": "error",
                    "message": f"Connection failed for {ssid}: {connect_msg_or_err}",
                })
        except Exception as e:
            logger.error(f"Background connect error for '{ssid}': {e}", exc_info=True)
            self._send_notification_threadsafe({
                "command": command, "status": "error",
                "message": f"Connection error: {str(e)}",
            })
        finally:
            self._connection_lock.release()

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
        """Handle update_network command.

        Saves the profile synchronously (fast) with autoconnect DISABLED,
        then dispatches scan + connect to a background thread so the BLE
        callback returns immediately.  On success the thread enables
        autoconnect; on failure the poisoned profile never auto-retries.
        """
        command = data.get('command')
        logger.info(f"Handling {command} command")
        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        password = network_data.get('password')
        priority = network_data.get('priority', 10)

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required"}

        if not self._connection_lock.acquire(blocking=False):
            return {"command": command, "status": "error", "message": "Another connection operation is in progress"}

        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"IP before update/connect attempt: {self._current_ip_address}")

        success_update, err_update = nmcli_add_or_modify_connection(
            ssid, password, priority, autoconnect=False,
        )
        if not success_update:
            self._connection_lock.release()
            return {"command": command, "status": "error", "message": err_update}

        logger.info(f"Network profile '{ssid}' saved (autoconnect disabled). Starting background connect...")
        threading.Thread(
            target=self._background_connect,
            args=(command, ssid, True),
            daemon=True,
        ).start()

        return {"command": command, "status": "in_progress", "message": f"Profile saved. Connecting to {ssid}..."}

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
        """Handle connect_network command.

        Dispatches scan + connect to a background thread so the BLE
        callback returns immediately.  Existing profiles already have
        autoconnect configured, so we don't touch it here.
        """
        command = data.get('command')
        logger.info(f"Handling {command} command")
        ssid = data.get('data', {}).get('ssid')

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for connection"}

        if not self._connection_lock.acquire(blocking=False):
            return {"command": command, "status": "error", "message": "Another connection operation is in progress"}

        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"IP before connect attempt: {self._current_ip_address}")

        logger.info(f"Starting background connect for known network '{ssid}'...")
        threading.Thread(
            target=self._background_connect,
            args=(command, ssid, False),
            daemon=True,
        ).start()

        return {"command": command, "status": "in_progress", "message": f"Connecting to {ssid}..."}

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
            self._ble_characteristic = None  # Clear reference when client unsubscribes
            logger.info(f"Connected device: {self._connected_device}")
            # Disconnect client when they stop notifications
            if self._connected_device:
                try:
                    logger.info(f"Disconnecting client {self._connected_device.address} after notifications stopped")
                    self._connected_device.disconnect()
                except Exception as e:
                    logger.warning(f"Failed to disconnect client: {e}")

    # --- Connection Callbacks ---
    def on_connect(self, device):
        logger.info(f"Client connected: {device.address}")
        self._connected_device = device
    
    def on_disconnect(self, device):
        logger.info(f"Client disconnected: {device.address}")
        self._ble_characteristic = None  # Clear characteristic on disconnect
        self._connected_device = None  # Clear device reference

    # --- Main Server Logic ---
    def start(self):
        """Initialize and run the BLE server."""
        logger.info("Starting BLE WiFi Provisioner Server")
        # self.load_networks() # Removed call

        adapter_address = self.adapter.address
        logger.info(f"Using adapter at address: {adapter_address}")
        
        # Set adapter alias to match robot name
        try:
            self.adapter.alias = ROBOT_NAME
            logger.info(f"Set adapter alias to '{ROBOT_NAME}'")
        except Exception as e:
            logger.warning(f"Failed to set adapter alias: {e}")
        
        # Check current discoverable state
        is_discoverable = self.adapter.discoverable
        logger.info(f"Adapter properties: Discoverable={is_discoverable}, Powered={self.adapter.powered}, Pairable={self.adapter.pairable}")
        
        # If already discoverable, temporarily disable to avoid Busy error when AdvertisingManager tries to set it
        if is_discoverable:
            logger.info("Adapter is already discoverable, temporarily disabling to avoid conflict...")
            try:
                self.adapter.discoverable = False
                time.sleep(0.5)  # Brief pause to let the state change
                logger.info("Temporarily disabled discoverable state")
            except Exception as e:
                logger.warning(f"Could not disable discoverable state: {e}")

        try:
            # Create Peripheral - AdvertisingManager will set discoverable=True internally
            self.peripheral = peripheral.Peripheral(adapter_address,
                                                 local_name=ROBOT_NAME,
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
            # Note: discoverable is already set to True by AdvertisingManager inside Peripheral
            # No need to set it again here (removed redundant line)
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

                # Note: bluezero might handle some of this implicitly on mainloop quit
                self.peripheral = None # Clear reference
            else:
                logger.info("Peripheral not found, skipping cleanup. We are also not stopping the mainloop.")
        
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