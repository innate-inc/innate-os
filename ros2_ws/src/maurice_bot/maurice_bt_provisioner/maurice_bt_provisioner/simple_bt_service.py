#!/usr/bin/env python3
import logging
import json
import signal
import sys
import os
import subprocess

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
        # Fetch networks directly from NetworkManager
        nmcli_networks = []
        try:
            # Get connection names and types, terse format
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
                capture_output=True, text=True, check=True
            )
            output_lines = result.stdout.strip().split('\n')
            logger.debug(f"nmcli connection show output: {output_lines}")

            for line in output_lines:
                if not line:
                    continue
                parts = line.split(':')
                if len(parts) == 2:
                    name, type = parts
                    # NetworkManager uses '802-11-wireless' for Wi-Fi type
                    if type == '802-11-wireless':
                        # Here, we just add the SSID (connection name)
                        # If priority or other details are needed, 
                        # we would need additional nmcli calls per connection.
                        nmcli_networks.append({'ssid': name})
                else:
                    logger.warning(f"Could not parse nmcli output line: {line}")

            # Extract SSIDs for logging
            nmcli_ssids = [n['ssid'] for n in nmcli_networks]
            logger.info(f"Found {len(nmcli_networks)} Wi-Fi networks via nmcli: {nmcli_ssids}")
            return {"status": "success", "networks": nmcli_networks}

        except subprocess.CalledProcessError as e:
            logger.error(f"nmcli command failed during get_status: {e.cmd} - {e.stderr}")
            return {"status": "error", "message": f"Failed to retrieve networks from NetworkManager: {e.stderr}"}
        except FileNotFoundError:
            logger.error("nmcli command not found. Is NetworkManager installed and in PATH?")
            return {"status": "error", "message": "nmcli command not found."}
        except Exception as e:
            logger.error(f"Error parsing nmcli output during get_status: {e}", exc_info=True)
            return {"status": "error", "message": f"An unexpected error occurred while retrieving networks: {str(e)}"}

    def handle_update_network(self, data):
        """Handle update_network command."""
        logger.info("Handling update_network command")
        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        password = network_data.get('password')
        priority = network_data.get('priority', 10)

        if not ssid:
            return {"status": "error", "message": "SSID required"}

        # --- NetworkManager Interaction ---
        connection_exists = False
        try:
            # Check if connection profile already exists
            result = subprocess.run(['nmcli', '-g', 'NAME', 'connection', 'show'], capture_output=True, text=True, check=True)
            existing_connections = result.stdout.strip().split('\n')
            connection_exists = ssid in existing_connections
            logger.info(f"Existing connections: {existing_connections}, Checking for: {ssid}, Exists: {connection_exists}")

            if connection_exists:
                logger.info(f"Modifying existing NetworkManager connection: {ssid}")
                # Modify existing connection (ensure SSID and set priority, autoconnect)
                cmd_modify = ['nmcli', 'connection', 'modify', ssid, 'connection.autoconnect', 'yes', 'connection.priority', str(priority)]
                if password:
                     cmd_modify.extend(['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password])
                else: # Handle open networks
                    cmd_modify.extend(['wifi-sec.key-mgmt', 'none'])
                subprocess.run(cmd_modify, check=True)
            else:
                logger.info(f"Adding new NetworkManager connection: {ssid}")
                # Add new connection
                cmd_add = ['nmcli', 'connection', 'add', 'type', 'wifi', 'con-name', ssid, 'ifname', 'wlan0', 'ssid', ssid]
                cmd_add.extend(['connection.autoconnect', 'yes', 'connection.priority', str(priority)])
                if password:
                    cmd_add.extend(['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password])
                else: # Handle open networks
                    cmd_add.extend(['wifi-sec.key-mgmt', 'none'])
                subprocess.run(cmd_add, check=True)

            # --- Update Internal List ---
            found_internal = False
            for net in self.networks:
                if net['ssid'] == ssid:
                    net['priority'] = priority
                    # Optionally store password hash or indication if needed, omitted for simplicity
                    found_internal = True
                    break
            if not found_internal:
                self.networks.append({'ssid': ssid, 'priority': priority})
            self.save_networks() # Save internal list change

            # --- Scan and Attempt Connection ---
            logger.info("Performing Wi-Fi scan...")
            try:
                # Use --rescan yes to force a new scan
                scan_result = subprocess.run(['nmcli', '--terse', '--fields', 'SSID', 'device', 'wifi', 'list', '--rescan', 'yes'], capture_output=True, text=True, check=True, timeout=15)
                visible_ssids = scan_result.stdout.strip().split('\n')
                logger.info(f"Visible SSIDs: {visible_ssids}")

                if ssid in visible_ssids:
                    logger.info(f"Target network '{ssid}' is visible. Attempting connection...")
                    try:
                        # Attempt to bring the connection up
                        connect_result = subprocess.run(['nmcli', 'connection', 'up', ssid], capture_output=True, text=True, check=True, timeout=30)
                        logger.info(f"Connection attempt output for {ssid}: {connect_result.stdout.strip()}")
                        return {"status": "success", "message": f"Network {ssid} updated and connection initiated."}
                    except subprocess.CalledProcessError as connect_e:
                        logger.error(f"Failed to connect to {ssid}: {connect_e.stderr}")
                        return {"status": "warning", "message": f"Network {ssid} updated, but connection attempt failed: {connect_e.stderr}"}
                    except subprocess.TimeoutExpired:
                         logger.error(f"Connection attempt to {ssid} timed out.")
                         return {"status": "warning", "message": f"Network {ssid} updated, but connection attempt timed out."}
                else:
                    logger.info(f"Target network '{ssid}' not visible after scan. Profile saved.")
                    return {"status": "success", "message": f"Network {ssid} updated. Network not currently visible for connection."}

            except subprocess.CalledProcessError as scan_e:
                logger.error(f"Wi-Fi scan failed: {scan_e.stderr}")
                return {"status": "warning", "message": f"Network {ssid} updated, but Wi-Fi scan failed: {scan_e.stderr}"}
            except subprocess.TimeoutExpired:
                 logger.error("Wi-Fi scan timed out.")
                 return {"status": "warning", "message": f"Network {ssid} updated, but Wi-Fi scan timed out."}

        except subprocess.CalledProcessError as e:
            logger.error(f"nmcli command failed: {e.cmd} - {e.stderr}")
            return {"status": "error", "message": f"Failed to update network profile: {e.stderr}"}
        except FileNotFoundError:
            logger.error("nmcli command not found. Is NetworkManager installed and in PATH?")
            return {"status": "error", "message": "nmcli command not found."}
        except Exception as e:
            logger.error(f"Error during network update for {ssid}: {e}", exc_info=True)
            return {"status": "error", "message": f"An unexpected error occurred: {str(e)}"}

    def handle_remove_network(self, data):
        """Handle remove_network command."""
        logger.info("Handling remove_network command")
        ssid = data.get('data', {}).get('ssid')
        initial_length = len(self.networks)
        
        if not ssid:
            return {"status": "error", "message": "SSID required for removal"}

        logger.info(f"Attempting to remove network: {ssid}")

        # --- NetworkManager Interaction ---
        try:
            # Check if connection profile exists before attempting delete
            result = subprocess.run(['nmcli', '-g', 'NAME', 'connection', 'show'], capture_output=True, text=True, check=True)
            existing_connections = result.stdout.strip().split('\n')

            if ssid in existing_connections:
                logger.info(f"Deleting NetworkManager connection profile: {ssid}")
                subprocess.run(['nmcli', 'connection', 'delete', ssid], check=True)
            else:
                logger.warning(f"NetworkManager profile '{ssid}' not found for deletion.")
                # Proceed to remove from internal list anyway

        except subprocess.CalledProcessError as e:
            logger.error(f"nmcli command failed during delete: {e.cmd} - {e.stderr}")
            # Decide if this is a critical error or if we should still remove from internal list
            # For now, let's report error but still try to remove internally
            # return {"status": "error", "message": f"Failed to delete network profile: {e.stderr}"}
        except FileNotFoundError:
             logger.error("nmcli command not found. Is NetworkManager installed and in PATH?")
             return {"status": "error", "message": "nmcli command not found."}
        except Exception as e:
             logger.error(f"Error during network removal for {ssid}: {e}", exc_info=True)
             # return {"status": "error", "message": f"An unexpected error occurred: {str(e)}"}

        # --- Update Internal List ---
        initial_length_internal = len(self.networks)
        self.networks = [net for net in self.networks if net['ssid'] != ssid]

        if len(self.networks) < initial_length_internal:
            self.save_networks()
            logger.info(f"Removed network {ssid} from internal list.")
            return {"status": "success", "message": f"Network {ssid} removed"}
        else:
            logger.warning(f"Network {ssid} not found in internal list for removal.")
            # If nmcli delete succeeded but internal list didn't have it, still report success?
            # Or maybe it failed nmcli delete and wasn't in list either.
            return {"status": "error", "message": "Network not found"}

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