#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from bluezero import adapter, peripheral
from gi.repository import GLib
from nmcli_utils import (
    nmcli_add_or_modify_connection,
    nmcli_connect,
    nmcli_delete_connection,
    nmcli_get_active_ipv4_address,
    nmcli_get_active_wifi_ssid,
    nmcli_get_wifi_connections,
    nmcli_scan_for_ssid,
    nmcli_scan_for_visible_ssids,
    nmcli_set_autoconnect,
)

# IMPORTANT NOTES
# 1. Don't forget to set the conf file (sudo nano /etc/bluetooth/main.conf) with ControllerMode on "le", not "dual". It's a HUGE reason why it might not work or very rarely.


# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", stream=sys.stdout
)
logger = logging.getLogger("BLE_Server")

# BLE UUIDs
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHARACTERISTIC_UUID = "abcdef01-1234-5678-1234-56789abcdef0"

# BlueZ truncates each notification to ATT_MTU-3 bytes, so any response larger
# than the negotiated MTU must be split across notifications. Chunks carry raw
# JSON fragments and every message ends with a "\n" terminator (JSON.dumps
# never emits a raw newline): the app buffers notifications until it sees the
# newline, parses the line, and on parse failure drops it and resyncs at the
# next newline — so one truncated or garbled message can't poison the buffer
# for every response after it. JSON.parse tolerates the trailing newline, so
# clients that treat a single notification as a whole response still work.
# 182 is exactly ATT_MTU-3 for the 185 iOS negotiates by default. Android
# clients must request an MTU >= 185 or chunks get truncated on the air (as
# all large payloads already were) — truncation now surfaces as a dropped
# message instead of a wedged session.
NOTIFY_CHUNK_SIZE = 182

# Path to the helper script for restarting services (adjust if moved)
RESTART_SCRIPT_PATH = "/usr/local/bin/restart_robot_networking.sh"


# Load robot name from robot_info.json
def load_robot_name():
    """Load robot name from robot_info.json file."""
    mars_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    robot_info_path = os.path.join(mars_root, "data", "robot_info.json")

    with open(robot_info_path) as f:
        data = json.load(f)
        return data.get("robot_name", "MARS")


ROBOT_NAME = load_robot_name()


# --- BLE Server Class ---
class BleProvisionerServer:
    def __init__(self, adapter_obj: adapter.Adapter):
        """Initialize the BLE Provisioner Server."""
        self.adapter = adapter_obj
        self._ble_characteristic = None
        self.peripheral = None
        self._connected_device = None
        self._connection_lock = threading.Lock()
        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"Initial IPv4 address: {self._current_ip_address}")

    # --- Helper to Trigger Service Restart ---
    def _trigger_service_restart(self):
        """Calls the restart script via sudo if the IP address has changed."""
        new_ip = nmcli_get_active_ipv4_address()
        logger.info(f"Checking for IP change. Current: {self._current_ip_address}, New: {new_ip}")

        if new_ip != self._current_ip_address:
            logger.warning(
                f"IP address changed from {self._current_ip_address} to {new_ip}. Triggering service restarts."
            )
            try:
                # Use run with check=True to raise an exception if the script fails
                # Use capture_output=True to get stdout/stderr
                # Use text=True for easier handling of stdout/stderr
                result = subprocess.run(
                    ["sudo", RESTART_SCRIPT_PATH], check=True, capture_output=True, text=True, timeout=30
                )  # Add a timeout
                logger.info(f"Service restart script executed successfully. Output:\n{result.stdout}")
                self._current_ip_address = new_ip  # Update stored IP only on success
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to execute restart script '{RESTART_SCRIPT_PATH}': {e}")
                logger.error(f"stdout: {e.stdout}")
                logger.error(f"stderr: {e.stderr}")
                # Decide if you want to update self._current_ip_address here or retry
            except FileNotFoundError:
                logger.error(
                    f"Error: Restart script '{RESTART_SCRIPT_PATH}' not found. Ensure it's installed correctly and the path is right."
                )
            except subprocess.TimeoutExpired:
                logger.error(f"Error: Restart script '{RESTART_SCRIPT_PATH}' timed out after 30 seconds.")
            except Exception as e:
                logger.error(f"An unexpected error occurred during restart script execution: {e}", exc_info=True)
        else:
            logger.info("IP address unchanged. No service restart needed.")

    # --- Thread-safe BLE helpers ---

    def _notify_chunked(self, characteristic, response):
        """Send *response* (dict) as newline-terminated <=NOTIFY_CHUNK_SIZE notifications."""
        payload = bytes(json.dumps(response), "utf-8") + b"\n"
        chunks = [payload[i : i + NOTIFY_CHUNK_SIZE] for i in range(0, len(payload), NOTIFY_CHUNK_SIZE)]
        for chunk in chunks:
            characteristic.set_value(list(chunk))
        if len(chunks) > 1:
            logger.info(f"Notification sent in {len(chunks)} chunks ({len(payload)} bytes)")

    def _send_notification_threadsafe(self, response):
        """Schedule a BLE notification on the GLib main loop (safe from any thread)."""

        def _do_send():
            if self._ble_characteristic and self._ble_characteristic.is_notifying:
                try:
                    self._notify_chunked(self._ble_characteristic, response)
                    logger.info(f"Async notification sent: {response}")
                except Exception as e:
                    logger.error(f"Error sending async notification: {e}")
            else:
                logger.warning(f"Cannot send notification (client not subscribed): {response}")
            return False  # one-shot; do not repeat

        GLib.idle_add(_do_send)

    def _background_connect(self, command, ssid, enable_autoconnect_on_success=True):
        """Scan + connect in a daemon thread; notify the BLE client when done."""
        try:
            with self._connection_lock:
                self._current_ip_address = nmcli_get_active_ipv4_address()

                success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
                if not success_scan:
                    logger.warning(f"Pre-connect scan failed: {err_scan}")
                elif not visible:
                    logger.warning(f"Network '{ssid}' not visible in scan, attempting connect anyway")

                success, msg_or_err = nmcli_connect(ssid)

                if success:
                    if enable_autoconnect_on_success:
                        nmcli_set_autoconnect(ssid, True)
                    # Notify the phone before the IP-stabilisation wait and the
                    # service restarts: those take 15-45 s and the app only
                    # needs to know the WiFi connection succeeded.
                    self._send_notification_threadsafe(
                        {
                            "command": command,
                            "status": "success",
                            "message": f"Connected to {ssid}",
                        }
                    )
                    logger.info(f"Background connect to '{ssid}' succeeded, waiting for stabilisation…")
                    time.sleep(5)
                    self._trigger_service_restart()
                else:
                    logger.error(f"Background connect to '{ssid}' failed: {msg_or_err}")
                    self._send_notification_threadsafe(
                        {
                            "command": command,
                            "status": "error",
                            "message": f"Connection failed for {ssid}: {msg_or_err}",
                        }
                    )
        except Exception as e:
            logger.error(f"Background connect error: {e}", exc_info=True)
            self._send_notification_threadsafe(
                {
                    "command": command,
                    "status": "error",
                    "message": f"Connection error: {str(e)}",
                }
            )

    # --- Command Handlers ---
    def handle_get_status(self, data):
        """Handle get_status command."""
        command = data.get("command")
        logger.info(f"Handling {command} command")

        # Get configured networks
        success_list, networks, error_msg_list = nmcli_get_wifi_connections()

        # Get active network
        active_ssid = nmcli_get_active_wifi_ssid()
        # Note: nmcli_get_active_wifi_ssid handles its own logging/errors, returns None on failure

        # Get active IPv4 address — Wi-Fi only: with Wi-Fi forgotten the
        # static ethernet interface still has an IP (192.168.50.2), and the
        # app would present the robot as network-connected and try to reach
        # an address the phone has no route to.
        active_ip = nmcli_get_active_ipv4_address(wifi_only=True)
        # Note: nmcli_get_active_ipv4_address handles its own logging/errors, returns None on failure

        if success_list:
            # Include both configured list and active SSID in success response
            return {
                "command": command,
                "status": "success",
                "networks": networks,
                "active_ssid": active_ssid,  # Will be SSID string or None
                "active_ip": active_ip,  # Will be IPv4 string or None
            }
        else:
            # If fetching the list failed, report that error, but still include active SSID if found
            return {
                "command": command,
                "status": "error",
                "message": error_msg_list,
                "networks": [],  # Return empty list on error
                "active_ssid": active_ssid,
                "active_ip": active_ip,
            }

    def handle_update_network(self, data):
        """Handle update_network command.

        Saves the connection profile with autoconnect disabled (~1-2 s), then
        dispatches scan+connect to a background thread so the BLE callback
        returns quickly.  The background thread sends a follow-up notification
        with the final result.
        """
        command = data.get("command")
        logger.info(f"Handling {command} command")
        network_data = data.get("data", {})
        ssid = network_data.get("ssid")
        password = network_data.get("password")
        priority = network_data.get("priority", 10)

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required"}

        success, err = nmcli_add_or_modify_connection(ssid, password, priority, autoconnect=False)
        if not success:
            return {"command": command, "status": "error", "message": err}

        threading.Thread(
            target=self._background_connect,
            args=(command, ssid),
            kwargs={"enable_autoconnect_on_success": True},
            daemon=True,
        ).start()

        return {"command": command, "status": "in_progress", "message": f"Connecting to {ssid}…"}

    def handle_remove_network(self, data):
        """Handle remove_network command."""
        command = data.get("command")
        logger.info(f"Handling {command} command")
        ssid = data.get("data", {}).get("ssid")

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
        command = data.get("command")
        logger.info(f"Handling {command} command")

        # Force a fresh sweep: this is the one path where the user explicitly
        # asked for scan results, and a hotspot enabled seconds ago won't be in
        # NM's cache — --rescan auto would return the same stale list on every
        # retap for ~30 s. The internal pre-connect scans keep the softer auto
        # mode; here the app shows a scanning state, so the ~13 s sweep is the
        # freshness the user asked for.
        success, ssids, error_msg = nmcli_scan_for_visible_ssids(force_rescan=True)

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

        Returns an immediate ``in_progress`` response and dispatches the slow
        scan+connect to a background thread.  The profile already exists so
        autoconnect is left as-is.
        """
        command = data.get("command")
        logger.info(f"Handling {command} command")
        ssid = data.get("data", {}).get("ssid")

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for connection"}

        threading.Thread(
            target=self._background_connect,
            args=(command, ssid),
            kwargs={"enable_autoconnect_on_success": False},
            daemon=True,
        ).start()

        return {"command": command, "status": "in_progress", "message": f"Connecting to {ssid}…"}

    # --- BLE Callbacks ---
    def write_callback(self, value, options=None):
        """Handle write requests from BLE clients."""
        logger.debug(f"write_callback invoked with value (type {type(value)}): {value}")
        logger.debug(f"Options: {options}")
        response = None
        try:
            value_str = bytes(value).decode("utf-8")
            logger.info(f"Received write: {value_str}")
            data = json.loads(value_str)
            command = data.get("command")

            if command == "get_status":
                response = self.handle_get_status(data)
            elif command == "update_network":
                response = self.handle_update_network(data)
            elif command == "remove_network":
                response = self.handle_remove_network(data)
            elif command == "connect_network":
                response = self.handle_connect_network(data)
            elif command == "scan_wifi":
                response = self.handle_scan_wifi(data)
            else:
                response = self.handle_unknown_command(command)

        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON: {value_str}")
            response = {"command": "unknown", "status": "error", "message": "Invalid JSON format"}
        except Exception as e:
            # Try to get command from data if available, otherwise use 'unknown'
            cmd_for_log = data.get("command", "unknown") if "data" in locals() else "unknown"
            logger.error(f"Error processing command '{cmd_for_log}': {e}", exc_info=True)
            response = {"command": cmd_for_log, "status": "error", "message": f"Server error: {str(e)}"}

        # Send response via notification if possible
        if response and self._ble_characteristic and self._ble_characteristic.is_notifying:
            logger.info(f"Sending notification response: {response}")
            try:
                self._notify_chunked(self._ble_characteristic, response)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")

        # Return value for compatibility (though notifications are preferred)
        return bytes(json.dumps(response), "utf-8") if response else b""

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
        return bytes(json.dumps(response), "utf-8")

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

        logger.info(
            f"Adapter properties: Discoverable={self.adapter.discoverable}, Powered={self.adapter.powered}, Pairable={self.adapter.pairable}"
        )

        # If already discoverable, disable it so bluezero's LE AdvertisingManager
        # doesn't hit a "Busy" error competing with legacy discoverable mode.
        # publish() will re-enable advertising via the LE path.
        if self.adapter.discoverable:
            logger.info(
                "Adapter is already discoverable, temporarily disabling to avoid Busy conflict with LE advertising..."
            )
            try:
                self.adapter.discoverable = False
                time.sleep(1)
                logger.info(f"Discoverable disabled. Current state: Discoverable={self.adapter.discoverable}")
            except Exception as e:
                logger.warning(f"Could not disable discoverable state: {e}")

        try:
            # Create Peripheral - AdvertisingManager will set discoverable=True internally
            logger.info(f"Creating BLE peripheral with local_name='{ROBOT_NAME}'...")
            self.peripheral = peripheral.Peripheral(
                adapter_address, local_name=ROBOT_NAME, appearance=192
            )  # 192: Generic Computer
            logger.info("Peripheral created successfully.")

            # Set connection callbacks
            if hasattr(self.peripheral, "on_connect"):
                self.peripheral.on_connect = self.on_connect
                logger.info("on_connect callback registered.")
            else:
                logger.warning("Peripheral has no on_connect attribute — connection events won't be tracked.")

            if hasattr(self.peripheral, "on_disconnect"):
                self.peripheral.on_disconnect = self.on_disconnect
                logger.info("on_disconnect callback registered.")
            else:
                logger.warning("Peripheral has no on_disconnect attribute — disconnect events won't be tracked.")

            # Add service
            logger.info(f"Adding GATT service {SERVICE_UUID}...")
            self.peripheral.add_service(srv_id=1, uuid=SERVICE_UUID, primary=True)

            # Add characteristic
            logger.info(f"Adding GATT characteristic {CHARACTERISTIC_UUID}...")
            self.peripheral.add_characteristic(
                srv_id=1,
                chr_id=1,
                uuid=CHARACTERISTIC_UUID,
                value=[],  # Initial value is empty
                notifying=False,
                flags=["read", "write", "write-without-response", "notify"],
                read_callback=self.read_callback,
                write_callback=self.write_callback,
                notify_callback=self.notify_callback,
            )

            logger.info(f"Service {SERVICE_UUID} and Characteristic {CHARACTERISTIC_UUID} added.")

            # Set up signal handler for graceful shutdown
            def signal_handler(sig, frame):
                logger.info(f"Signal {sig} received, stopping BLE service...")
                self.stop()

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            # Patch bluezero's registration callbacks to use our logger
            # (upstream ad callbacks use bare print(), GATT error just warns)
            import bluezero.advertisement as _adv
            import bluezero.GATT as _gatt

            _adv_adapter = self.adapter

            _adv.register_ad_cb = lambda: logger.info("BLE advertisement registered successfully.")

            def _on_ad_error(error):
                logger.error(f"BLE advertisement registration FAILED: {error}")
                # Fallback: re-enable legacy discoverable so the device name
                # is still visible even if LE advertising failed
                try:
                    _adv_adapter.discoverable = True
                    logger.warning("Fell back to legacy discoverable mode.")
                except Exception as e:
                    logger.error(f"Failed to enable legacy discoverable fallback: {e}")

            _adv.register_ad_error_cb = _on_ad_error
            _gatt.register_app_cb = lambda: logger.info("GATT application registered successfully.")
            _gatt.register_app_error_cb = lambda error: logger.error(f"GATT application registration FAILED: {error}")

            # Start advertising
            logger.info("Calling peripheral.publish() to register GATT app and start advertisement...")
            self.peripheral.publish()
            logger.info("BLE Server is running. Press Ctrl+C to stop.")

        except Exception as e:
            logger.critical(f"Failed to initialize or start BLE service: {e}", exc_info=True)
            logger.critical(
                f"Adapter state at failure: Discoverable={self.adapter.discoverable}, Powered={self.adapter.powered}"
            )
            self.stop()

    def stop(self):
        """Stop the BLE server gracefully."""
        logger.info("Stopping BLE server...")
        if self.peripheral:
            # Check if mainloop exists and quit if so
            if hasattr(self.peripheral, "mainloop") and self.peripheral.mainloop:
                try:
                    self.peripheral.mainloop.quit()
                    logger.info("Main event loop stopped.")
                except Exception as e:
                    logger.error(f"Error stopping mainloop: {e}")

                # Note: bluezero might handle some of this implicitly on mainloop quit
                self.peripheral = None  # Clear reference
            else:
                logger.info("Peripheral not found, skipping cleanup. We are also not stopping the mainloop.")

        # self.save_networks() # Removed call
        logger.info("BLE Provisioner Server stopped.")


# --- Main Execution Block ---
if __name__ == "__main__":
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
