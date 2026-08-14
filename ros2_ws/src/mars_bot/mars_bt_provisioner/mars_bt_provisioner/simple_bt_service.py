#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import logging
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from bluezero import adapter, peripheral
from dotenv import dotenv_values
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

# Path to the helper script for restarting services (adjust if moved)
RESTART_SCRIPT_PATH = "/usr/local/bin/restart_robot_networking.sh"
SYSTEM_ENV_PATH = "/etc/innate.env"

INNATE_OS_ROOT = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
IDENTITY_TOOL_PATH = os.path.join(INNATE_OS_ROOT, "scripts", "identity", "innate-identity")
IDENTIFY_SOUND = os.path.join(INNATE_OS_ROOT, "config", "sounds", "turnon.mp3")

# The identity blob is validated here and again by the root helper. No size check: a
# payload this layer can see already crossed the air, and the bound that matters is the
# helper's, enforced by root.
ENV_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*=")
# /etc/innate.env is injected into every ROS process's environment, so a blob is env
# injection by construction. The unprovisioned-only rule is the real gate; this narrows
# what the open window buys an attacker.
FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_")

# The root helper's exit codes and refusal wording; scripts/identity/innate-identity is
# authoritative for both.
EXIT_ALREADY_PROVISIONED = 3
EXIT_BAD_PAYLOAD = 4
ALREADY_PROVISIONED_MSG = "Already provisioned — delete /etc/innate.env to de-provision"


def _stderr_summary(result: subprocess.CompletedProcess, default: str) -> str:
    """The last line a failed helper wrote, or a default when it said nothing."""
    lines = (result.stderr or "").strip().splitlines()
    return lines[-1] if lines else default


def system_env() -> dict[str, str | None]:
    """/etc/innate.env as a dict. Absent or unreadable reads as empty: this service has
    to start on a robot whose env file post_update.sh has not fixed the mode on yet."""
    try:
        return dotenv_values(SYSTEM_ENV_PATH)
    except OSError:
        return {}


def has_service_key(env: dict[str, str | None]) -> bool:
    """A robot is provisioned exactly when /etc/innate.env holds a service key."""
    return bool((env.get("INNATE_SERVICE_KEY") or "").strip())


def is_provisioned() -> bool:
    return has_service_key(system_env())


def load_robot_name() -> str:
    """The advertised name, provisioned or not — and it must never raise.

    robot_info.json is created by the ROS app, so on a never-booted robot it does not
    exist yet; with Restart=on-failure that used to crash-loop this service on exactly
    the robot that needs BLE most. Behind it, innate-seed.service is ordered before this
    one, so the env file holds a name by the time we read it — bare "MARS" is only what
    is left when the seed refused for want of a device-tree serial.
    """
    try:
        with open(os.path.join(INNATE_OS_ROOT, "data", "robot_info.json")) as f:
            name = json.load(f).get("robot_name")
        if name:
            return name
    except (OSError, ValueError):
        pass

    return (system_env().get("ROBOT_NAME") or "").strip() or "MARS"


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

    def _send_notification_threadsafe(self, response):
        """Schedule a BLE notification on the GLib main loop (safe from any thread)."""

        def _do_send():
            if self._ble_characteristic and self._ble_characteristic.is_notifying:
                try:
                    response_bytes = bytes(json.dumps(response), "utf-8")
                    self._ble_characteristic.set_value(list(response_bytes))
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
                    logger.info(f"Background connect to '{ssid}' succeeded, waiting for stabilisation…")
                    time.sleep(5)
                    self._trigger_service_restart()
                    self._send_notification_threadsafe(
                        {
                            "command": command,
                            "status": "success",
                            "message": f"Connected to {ssid}",
                        }
                    )
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

    # --- Identity ---
    def handle_identify(self, data):
        """Handle identify command: play the boot tune so an operator can tell which
        physical robot this is. Must work with ros-app down, so it plays the file
        directly rather than asking the ROS stack."""
        command = data.get("command")
        logger.info(f"Handling {command} command")
        threading.Thread(target=self._play_identify, args=(command,), daemon=True).start()
        return {"command": command, "status": "in_progress", "message": "Playing identify tune…"}

    def _play_identify(self, command):
        # XDG_RUNTIME_DIR has to be set explicitly: this unit does not preserve it, and
        # without it the player cannot reach the audio session.
        try:
            result = subprocess.run(
                ["gst-play-1.0", IDENTIFY_SOUND],
                capture_output=True,
                text=True,
                timeout=60,
                env=dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}"),
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._send_notification_threadsafe(
                {"command": command, "status": "error", "message": f"Playback failed: {e}"}
            )
            return

        if result.returncode != 0:
            # A nonzero exit is what separates "couldn't play it" from "played it and
            # you heard nothing" — the operator needs to know which.
            message = f"Playback failed: {_stderr_summary(result, 'unknown error')}"
            self._send_notification_threadsafe({"command": command, "status": "error", "message": message})
            return

        self._send_notification_threadsafe({"command": command, "status": "success", "message": "Played identify tune"})

    def handle_get_identity(self, data):
        """Handle get_identity command. /etc/innate.env goes back as-is for the client to
        parse, minus the one value in it that is a secret."""
        command = data.get("command")
        logger.info(f"Handling {command} command")
        env = system_env()

        return {
            "command": command,
            "status": "success",
            "env": {key: value for key, value in env.items() if key != "INNATE_SERVICE_KEY"},
            # The mDNS name, not the system one: every other robot URL is <robot>.local.
            "hostname": f"{socket.gethostname()}.local",
            "provisioned": has_service_key(env),
        }

    def handle_set_identity(self, data):
        """Handle set_identity command.

        The payload is forwarded to the root helper unparsed: this layer validates its
        shape and refuses when already provisioned, but it never reads the identity
        apart from that. The helper re-checks both as root — the caller's word for
        "unprovisioned" is not what gates the write.
        """
        command = data.get("command")
        logger.info(f"Handling {command} command")
        payload = data.get("data", {})

        error = self._reject_identity_payload(payload)
        if error:
            logger.warning(f"Rejecting set_identity payload: {error}")
            return {"command": command, "status": "error", "message": error}

        threading.Thread(target=self._apply_identity, args=(command, payload), daemon=True).start()
        return {"command": command, "status": "in_progress", "message": "Applying identity…"}

    def _reject_identity_payload(self, payload):
        """Reason to refuse the blob, or None. Never quotes a value back at the client."""
        if is_provisioned():
            return ALREADY_PROVISIONED_MSG
        if not isinstance(payload, dict):
            return "data must be an object"

        env_text = payload.get("env")
        password = payload.get("password", "")
        if not isinstance(env_text, str) or not isinstance(password, str):
            return "expected {env, password, wipe_data?}"
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in password):
            return "password contains control characters"

        for raw_line in env_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not ENV_LINE_RE.match(line):
                return "malformed env line"
            key, value = line.split("=", 1)
            if key.startswith(FORBIDDEN_ENV_PREFIXES):
                return f"refusing loader-steering variable {key}"
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
                return f"value of {key} contains control characters"
        return None

    def _teardown_and_reboot(self, wipe_data):
        """Stop ros-app, clean up after the new identity, then reboot — one detached
        child, so the reply is already on the wire.

        The stop has to come first. A running ros-app recreates robot_info.json from its
        launch-time environment within a second, so a --reset-info ahead of it is undone
        before the reboot and the robot comes back under its old name.
        """
        tool = shlex.quote(IDENTITY_TOOL_PATH)
        steps = ["/usr/bin/systemctl stop ros-app.service", f"{tool} --reset-info"]
        if wipe_data:
            steps.append(f"{tool} --wipe-data")
        steps.append("reboot")
        script = "; ".join(f"sudo {step}" for step in steps)
        subprocess.Popen(["setsid", "sh", "-c", f"sleep 2; {script}"], start_new_session=True)

    def _apply_identity(self, command, payload):
        """Run the root helper, reply, then hand the rest to a detached child.

        The reply has to be on the wire before the reboot: rebooting inline would race
        systemd's teardown of this unit against GLib.idle_add and silently drop it.
        """
        try:
            result = subprocess.run(
                ["sudo", IDENTITY_TOOL_PATH, "--write"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._send_notification_threadsafe(
                {"command": command, "status": "error", "message": f"Identity helper failed: {e}"}
            )
            return

        if result.returncode != 0:
            message = {
                EXIT_ALREADY_PROVISIONED: ALREADY_PROVISIONED_MSG,
                EXIT_BAD_PAYLOAD: "Bad identity payload",
            }.get(result.returncode, _stderr_summary(result, "Identity helper failed"))
            logger.error(f"Identity helper exited {result.returncode}: {message}")
            self._send_notification_threadsafe({"command": command, "status": "error", "message": message})
            return

        applied = {}
        stdout_lines = (result.stdout or "").strip().splitlines()
        if stdout_lines:
            try:
                applied = json.loads(stdout_lines[-1])
            except ValueError:
                logger.warning("Identity helper did not print an identity on stdout")

        self._send_notification_threadsafe(
            {
                "command": command,
                "status": "success",
                "robot_id": applied.get("robot_id"),
                "robot_name": applied.get("robot_name"),
                "message": "Identity applied, rebooting",
            }
        )
        self._teardown_and_reboot(bool(payload.get("wipe_data")))

    # --- BLE Callbacks ---
    def write_callback(self, value, options=None):
        """Handle write requests from BLE clients."""
        logger.debug(f"write_callback invoked with value (type {type(value)}): {value}")
        logger.debug(f"Options: {options}")
        response = None
        try:
            value_str = bytes(value).decode("utf-8")
            data = json.loads(value_str)
            command = data.get("command")
            # Never log the raw write: a set_identity payload carries the service key
            # and the login password.
            logger.info(f"Received write: {value_str if command != 'set_identity' else 'set_identity (redacted)'}")

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
            elif command == "identify":
                response = self.handle_identify(data)
            elif command == "get_identity":
                response = self.handle_get_identity(data)
            elif command == "set_identity":
                response = self.handle_set_identity(data)
            else:
                response = self.handle_unknown_command(command)

        except json.JSONDecodeError:
            # Length only: a truncated set_identity write is still full of secrets.
            logger.error(f"Failed to decode JSON in a {len(value)}-byte write")
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
                response_bytes = bytes(json.dumps(response), "utf-8")
                self._ble_characteristic.set_value(list(response_bytes))
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
