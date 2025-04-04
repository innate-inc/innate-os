#!/usr/bin/env python3
import logging
import json
import signal
import sys
import subprocess

from bluezero import adapter
from bluezero import peripheral

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   stream=sys.stdout)
logger = logging.getLogger('BLE_Server')
nm_logger = logging.getLogger('NetworkManager') # Dedicated logger for NM interactions

# BLE UUIDs
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHARACTERISTIC_UUID = 'abcdef01-1234-5678-1234-56789abcdef0'

# --- NetworkManager Utility Functions ---

def _run_nmcli(command_list, timeout=10, check=True, capture_output=True):
    """Runs an nmcli command using subprocess, handling common errors."""
    nm_logger.debug(f"Running nmcli command: {' '.join(command_list)}")
    try:
        result = subprocess.run(
            command_list, 
            capture_output=capture_output, 
            text=True, 
            check=check, 
            timeout=timeout
        )
        nm_logger.debug(f"nmcli stdout: {result.stdout.strip() if result.stdout else 'N/A'}")
        if result.stderr:
            nm_logger.debug(f"nmcli stderr: {result.stderr.strip()}")
        return True, result.stdout, result.stderr # Success
    except subprocess.CalledProcessError as e:
        nm_logger.error(f"nmcli command failed: {' '.join(command_list)}")
        nm_logger.error(f"  Return Code: {e.returncode}")
        nm_logger.error(f"  Stderr: {e.stderr.strip() if e.stderr else 'N/A'}")
        return False, e.stdout, e.stderr # Failed execution
    except subprocess.TimeoutExpired as e:
        nm_logger.error(f"nmcli command timed out: {' '.join(command_list)}")
        return False, None, "Command timed out." # Timeout
    except FileNotFoundError:
        nm_logger.error("nmcli command not found. Is NetworkManager installed and in PATH?")
        return False, None, "nmcli command not found." # Not found
    except Exception as e:
        nm_logger.error(f"Unexpected error running nmcli: {e}", exc_info=True)
        return False, None, f"Unexpected error: {str(e)}" # Other error

def nmcli_get_wifi_connections():
    """Retrieves a list of configured Wi-Fi connections with their priorities."""
    # Step 1: Get Name, Type, and UUID of all connections
    success_list, stdout_list, stderr_list = _run_nmcli(['nmcli', '-t', '-f', 'NAME,TYPE,UUID', 'connection', 'show'])
    if not success_list:
        return False, [], f"Failed to retrieve connection list: {stderr_list or 'Unknown error'}"
    
    networks = []
    wifi_connections_to_query = [] # Store (name, uuid) tuples

    try:
        list_lines = stdout_list.strip().split('\n') if stdout_list else []
        for line in list_lines:
            if not line:
                continue
            parts = line.split(':')
            if len(parts) == 3:
                name, type, uuid = parts
                if type == '802-11-wireless':
                    wifi_connections_to_query.append((name, uuid))
            else:
                nm_logger.warning(f"Could not parse nmcli connection list line: {line}")
    except Exception as e:
        nm_logger.error(f"Error parsing nmcli connection list output: {e}", exc_info=True)
        return False, [], f"Error parsing connection list: {str(e)}"

    # Step 2: Query priority for each Wi-Fi connection using its UUID
    for name, uuid in wifi_connections_to_query:
        priority = 0 # Default priority if lookup fails or not set
        success_detail, stdout_detail, stderr_detail = _run_nmcli(
            ['nmcli', '-t', '-f', 'connection.priority', 'connection', 'show', uuid]
        )
        
        if success_detail and stdout_detail:
            try:
                # Output is 'connection.priority:<value>', extract value
                priority_str = stdout_detail.strip().split(':')[-1]
                priority = int(priority_str) if priority_str else 0
            except (ValueError, IndexError) as e:
                nm_logger.warning(f"Could not parse priority for connection {name} (UUID: {uuid}): {stdout_detail}. Error: {e}. Using default priority 0.")
                priority = 0 # Fallback to default
        else:
            nm_logger.warning(f"Failed to get priority for connection {name} (UUID: {uuid}). Error: {stderr_detail or 'Unknown error'}. Using default priority 0.")
            priority = 0 # Fallback to default
        
        networks.append({'ssid': name, 'priority': priority})

    # Sort networks by priority (descending) then SSID (ascending) for consistent output
    networks.sort(key=lambda x: (-x['priority'], x['ssid']))
    
    return True, networks, None

def nmcli_connection_exists(ssid):
    """Checks if a NetworkManager connection profile exists for the given SSID."""
    success, stdout, stderr = _run_nmcli(['nmcli', '-g', 'NAME', 'connection', 'show'])
    if not success:
         return False, False, f"Failed check existence: {stderr or 'Unknown error'}"
    
    existing_connections = stdout.strip().split('\n') if stdout else []
    return True, ssid in existing_connections, None
    
def nmcli_add_or_modify_connection(ssid, password, priority):
    """Adds a new Wi-Fi connection or modifies an existing one."""
    success_check, exists, err_check = nmcli_connection_exists(ssid)
    if not success_check:
        return False, f"Failed to check if connection exists: {err_check}"

    base_cmd = ['nmcli', 'connection']
    if exists:
        nm_logger.info(f"Modifying existing connection: {ssid}")
        cmd = base_cmd + ['modify', ssid]
    else:
        nm_logger.info(f"Adding new connection: {ssid}")
        # Specify a default interface name like wlan0, adjust if necessary
        cmd = base_cmd + ['add', 'type', 'wifi', 'con-name', ssid, 'ifname', 'wlan0', 'ssid', ssid]
    
    # Common settings
    cmd.extend(['connection.autoconnect', 'yes', 'connection.priority', str(priority)])

    # Security settings
    if password:
        cmd.extend(['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password])
    else: # Open network
        cmd.extend(['wifi-sec.key-mgmt', 'none'])
    
    success, _, stderr = _run_nmcli(cmd)
    if not success:
         action = "modify" if exists else "add"
         return False, f"Failed to {action} connection '{ssid}': {stderr or 'Unknown error'}"
    return True, None # Success

def nmcli_delete_connection(ssid):
    """Deletes a Wi-Fi connection profile."""
    success_check, exists, err_check = nmcli_connection_exists(ssid)
    if not success_check:
        return False, f"Failed to check if connection exists before deletion: {err_check}"
    if not exists:
         return False, f"Network profile '{ssid}' not found."
    
    success, _, stderr = _run_nmcli(['nmcli', 'connection', 'delete', ssid])
    if not success:
        return False, f"Failed to delete connection '{ssid}': {stderr or 'Unknown error'}"
    return True, None # Success

def nmcli_scan_for_ssid(target_ssid):
    """Performs a Wi-Fi scan and checks if the target SSID is visible."""
    nm_logger.info("Performing Wi-Fi scan...")
    success, stdout, stderr = _run_nmcli(
        ['nmcli', '--terse', '--fields', 'SSID', 'device', 'wifi', 'list', '--rescan', 'yes'],
        timeout=15
    )
    if not success:
        return False, False, f"Wi-Fi scan failed: {stderr or 'Unknown error'}"
    
    visible_ssids = stdout.strip().split('\n') if stdout else []
    nm_logger.info(f"Visible SSIDs: {visible_ssids}")
    return True, target_ssid in visible_ssids, None

def nmcli_connect(ssid):
    """Attempts to activate (connect to) a given network profile."""
    nm_logger.info(f"Attempting to connect to network: {ssid}")
    success, stdout, stderr = _run_nmcli(
        ['nmcli', 'connection', 'up', ssid],
        timeout=30
    )
    if not success:
         return False, f"Connection attempt failed: {stderr or 'Unknown error'}"
    
    # Check stdout for confirmation message (optional, but can be useful)
    confirmation = stdout.strip() if stdout else "No output."
    nm_logger.info(f"Connection attempt output: {confirmation}")
    return True, confirmation # Success, return confirmation message

def nmcli_get_active_wifi_ssid():
    """Gets the SSID of the currently active Wi-Fi connection."""
    # Use 'nmcli dev wifi list' as it directly shows active state
    success, stdout, stderr = _run_nmcli(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi', 'list'])
    if not success:
        nm_logger.error(f"Failed to get active Wi-Fi status: {stderr or 'Unknown error'}")
        return None # Indicate error or inability to determine

    try:
        lines = stdout.strip().split('\n') if stdout else []
        for line in lines:
            if not line:
                continue
            parts = line.split(':')
            if len(parts) == 2:
                active, ssid = parts
                # ACTIVE field is 'yes' or 'no' in terse mode
                if active.lower() == 'yes':
                    nm_logger.info(f"Currently active Wi-Fi network: {ssid}")
                    return ssid
            else:
                 nm_logger.warning(f"Could not parse nmcli dev wifi list line: {line}")
        
        # If loop finishes without finding an active network
        nm_logger.info("No active Wi-Fi network found.")
        return None
    except Exception as e:
        nm_logger.error(f"Error parsing nmcli dev wifi list output: {e}", exc_info=True)
        return None # Indicate error

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