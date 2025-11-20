#!/usr/bin/env python3
import logging
import json
import subprocess
import time
import os
import sys
import signal

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
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
)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - BLE_Provisioner - %(levelname)s - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('BLE_Provisioner')

# --- BlueZ / D-Bus constants ---
BLUEZ_SERVICE_NAME   = 'org.bluez'
DBUS_OM_IFACE        = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE      = 'org.freedesktop.DBus.Properties'
GATT_MANAGER_IFACE   = 'org.bluez.GattManager1'
LE_ADV_MGR_IFACE     = 'org.bluez.LEAdvertisingManager1'
LE_ADV_IFACE         = 'org.bluez.LEAdvertisement1'
GATT_SERVICE_IFACE   = 'org.bluez.GattService1'
GATT_CHRC_IFACE      = 'org.bluez.GattCharacteristic1'

# --- BLE UUIDs (same as the old Bluezero server) ---
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHAR_UUID    = 'abcdef01-1234-5678-1234-56789abcdef0'

# Path to helper script for restarting services
RESTART_SCRIPT_PATH = "/usr/local/bin/restart_robot_networking.sh"


# --- Robot name helper ---
def load_robot_name():
    root = os.environ.get(
        'INNATE_OS_ROOT',
        os.path.join(os.path.expanduser('~'), 'innate-os'),
    )
    info_path = os.path.join(root, 'data', 'robot_info.json')
    try:
        with open(info_path, 'r') as f:
            data = json.load(f)
        return data.get('robot_name', 'MARS')
    except Exception as e:
        logger.warning(f"Failed to load robot_info.json, using default name: {e}")
        return 'MARS'


ROBOT_NAME = load_robot_name()


# --- D-Bus error helpers ---
class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.NotSupported'


# --- GATT Application / Service base classes ---

class Application(dbus.service.Object):
    """Implements ObjectManager.GetManagedObjects for our GATT tree."""

    def __init__(self, bus):
        self.path = '/'
        self.bus = bus
        self.services = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    def get_services(self):
        return self.services

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.get_characteristics():
                response[chrc.get_path()] = chrc.get_properties()
        return response


class Service(dbus.service.Object):
    """org.bluez.GattService1 implementation."""

    def __init__(self, bus, index, uuid, primary):
        self.path = f'/com/innate/ble/service{index}'
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self._characteristics = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, chrc):
        self._characteristics.append(chrc)

    def get_characteristics(self):
        return self._characteristics

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': dbus.Boolean(self.primary),
                'Characteristics': dbus.Array(
                    [c.get_path() for c in self._characteristics],
                    signature='o',
                ),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE,
                         in_signature='s',
                         out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_SERVICE_IFACE]


class Characteristic(dbus.service.Object):
    """Base org.bluez.GattCharacteristic1 implementation."""

    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE,
                         in_signature='s',
                         out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE,
                         in_signature='a{sv}',
                         out_signature='ay')
    def ReadValue(self, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE,
                         in_signature='aya{sv}')
    def WriteValue(self, value, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        # dbus.service handles the actual signal emission
        pass


# --- WiFi Provisioning Characteristic ---

class ProvisioningCharacteristic(Characteristic):
    """
    Same semantics as your old Bluezero characteristic:
    - JSON in WriteValue
    - JSON out via notifications + characteristic value
    """

    def __init__(self, bus, index, service):
        super().__init__(
            bus,
            index,
            CHAR_UUID,
            ['read', 'write', 'write-without-response', 'notify'],
            service,
        )
        self.notifying = False
        self._value = []
        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"Initial IPv4 address: {self._current_ip_address}")

    # --- helpers: restart script ---
    def _trigger_service_restart(self):
        new_ip = nmcli_get_active_ipv4_address()
        logger.info(f"Checking for IP change. Current: {self._current_ip_address}, New: {new_ip}")

        if new_ip != self._current_ip_address:
            logger.warning(f"IP address changed from {self._current_ip_address} to {new_ip}. Triggering service restarts.")
            try:
                result = subprocess.run(
                    ['sudo', RESTART_SCRIPT_PATH],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                logger.info(f"Service restart script executed successfully. Output:\n{result.stdout}")
                self._current_ip_address = new_ip
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to execute restart script '{RESTART_SCRIPT_PATH}': {e}")
                logger.error(f"stdout: {e.stdout}")
                logger.error(f"stderr: {e.stderr}")
            except FileNotFoundError:
                logger.error(f"Restart script '{RESTART_SCRIPT_PATH}' not found.")
            except subprocess.TimeoutExpired:
                logger.error(f"Restart script '{RESTART_SCRIPT_PATH}' timed out.")
            except Exception as e:
                logger.error(f"Unexpected error during restart script execution: {e}", exc_info=True)
        else:
            logger.info("IP address unchanged. No service restart needed.")

    # --- old command handlers, ported 1:1 ---

    def handle_get_status(self, data):
        command = data.get('command')
        logger.info(f"Handling {command} command")

        success_list, networks, error_msg_list = nmcli_get_wifi_connections()
        active_ssid = nmcli_get_active_wifi_ssid()
        active_ip = nmcli_get_active_ipv4_address()

        if success_list:
            return {
                "command": command,
                "status": "success",
                "networks": networks,
                "active_ssid": active_ssid,
                "active_ip": active_ip,
            }
        else:
            return {
                "command": command,
                "status": "error",
                "message": error_msg_list,
                "networks": [],
                "active_ssid": active_ssid,
                "active_ip": active_ip,
            }

    def handle_update_network(self, data):
        command = data.get('command')
        logger.info(f"Handling {command} command")

        network_data = data.get('data', {})
        ssid = network_data.get('ssid')
        password = network_data.get('password')
        priority = network_data.get('priority', 10)

        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required"}

        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"IP before update/connect attempt: {self._current_ip_address}")

        success_update, err_update = nmcli_add_or_modify_connection(ssid, password, priority)
        if not success_update:
            return {"command": command, "status": "error", "message": err_update}

        logger.info(f"Network profile '{ssid}' updated successfully.")

        success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
        if not success_scan:
            return {
                "command": command,
                "status": "warning",
                "message": f"Network {ssid} updated, but scan failed: {err_scan}",
            }

        if not visible:
            logger.info(f"Target network '{ssid}' not visible after scan. Profile saved.")
            return {
                "command": command,
                "status": "success",
                "message": f"Network {ssid} updated. Network not currently visible for connection.",
            }

        logger.info(f"Target network '{ssid}' is visible. Attempting connection...")
        success_connect, connect_msg_or_err = nmcli_connect(ssid)

        if success_connect:
            logger.info(f"Connection initiated for {ssid}. Waiting for network stabilization...")
            time.sleep(5)
            self._trigger_service_restart()
            return {
                "command": command,
                "status": "success",
                "message": f"Network {ssid} updated and connection initiated.",
            }
        else:
            logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
            return {
                "command": command,
                "status": "warning",
                "message": f"Network {ssid} updated, but connection attempt failed: {connect_msg_or_err}",
            }

    def handle_remove_network(self, data):
        command = data.get('command')
        logger.info(f"Handling {command} command")

        ssid = data.get('data', {}).get('ssid')
        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for removal"}

        logger.info(f"Attempting to remove network profile: {ssid}")
        success, error_msg = nmcli_delete_connection(ssid)

        if success:
            logger.info(f"Successfully deleted NetworkManager profile: {ssid}")
            return {
                "command": command,
                "status": "success",
                "message": f"Network profile {ssid} removed",
            }
        else:
            logger.warning(f"Failed to remove network profile '{ssid}': {error_msg}")
            return {"command": command, "status": "error", "message": error_msg}

    def handle_scan_wifi(self, data):
        command = data.get('command')
        logger.info(f"Handling {command} command")

        success, ssids, error_msg = nmcli_scan_for_visible_ssids()
        if success:
            return {"command": command, "status": "success", "visible_ssids": ssids}
        else:
            return {
                "command": command,
                "status": "error",
                "message": error_msg,
                "visible_ssids": [],
            }

    def handle_connect_network(self, data):
        command = data.get('command')
        logger.info(f"Handling {command} command")

        ssid = data.get('data', {}).get('ssid')
        if not ssid:
            return {"command": command, "status": "error", "message": "SSID required for connection"}

        logger.info(f"Attempting to connect to network: {ssid}")

        self._current_ip_address = nmcli_get_active_ipv4_address()
        logger.info(f"IP before connect attempt: {self._current_ip_address}")

        success_scan, visible, err_scan = nmcli_scan_for_ssid(ssid)
        if not success_scan:
            logger.warning(f"Scan for '{ssid}' failed before connection attempt: {err_scan}")
        elif not visible:
            logger.warning(f"Network '{ssid}' not visible, connection attempt might fail.")

        success_connect, connect_msg_or_err = nmcli_connect(ssid)

        if success_connect:
            logger.info(f"Connection initiated for {ssid}. Waiting for network stabilization...")
            time.sleep(5)
            self._trigger_service_restart()
            return {
                "command": command,
                "status": "success",
                "message": f"Connection initiated for {ssid}",
            }
        else:
            logger.error(f"Connection attempt failed for {ssid}: {connect_msg_or_err}")
            return {
                "command": command,
                "status": "error",
                "message": f"Connection attempt failed for {ssid}: {connect_msg_or_err}",
            }

    def handle_unknown_command(self, command):
        logger.warning(f"Unknown command received: {command}")
        return {
            "command": command or "unknown",
            "status": "error",
            "message": "Unknown command",
        }

    # --- GATT Read/Write/Notify ---

    @dbus.service.method(GATT_CHRC_IFACE,
                         in_signature='a{sv}',
                         out_signature='ay')
    def ReadValue(self, options):
        logger.info("ReadValue -> read_status")
        success, networks, error_msg = nmcli_get_wifi_connections()
        response_status = "success" if success else "error"

        response = {
            "command": "read_status",
            "status": response_status,
            "networks": networks,
        }
        if error_msg:
            response["message"] = error_msg

        response_bytes = json.dumps(response).encode('utf-8')
        self._value = [dbus.Byte(b) for b in response_bytes]
        return self._value

    @dbus.service.method(GATT_CHRC_IFACE,
                         in_signature='aya{sv}')
    def WriteValue(self, value, options):
        logger.debug(f"WriteValue raw: {value} options={options}")
        response = None
        cmd_for_log = "unknown"

        try:
            value_bytes = bytes(value)
            value_str = value_bytes.decode('utf-8')
            logger.info(f"WriteValue received: {value_str}")

            try:
                data = json.loads(value_str)
                command = data.get('command')
                cmd_for_log = command or "unknown"
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON: {value_str}")
                response = {
                    "command": "unknown",
                    "status": "error",
                    "message": "Invalid JSON format",
                }
            else:
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

        except Exception as e:
            logger.error(f"Error processing command '{cmd_for_log}': {e}", exc_info=True)
            response = {
                "command": cmd_for_log,
                "status": "error",
                "message": f"Server error: {str(e)}",
            }

        if response is not None:
            response_bytes = json.dumps(response).encode('utf-8')
            self._value = [dbus.Byte(b) for b in response_bytes]
            logger.info(f"Sending response: {response}")
            if self.notifying:
                self.PropertiesChanged(
                    GATT_CHRC_IFACE,
                    {'Value': self._value},
                    [],
                )

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        if self.notifying:
            logger.info("StartNotify called, already notifying")
            return
        logger.info("StartNotify called, enabling notifications")
        self.notifying = True
        if self._value:
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {'Value': self._value},
                [],
            )

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        if not self.notifying:
            logger.info("StopNotify called, but not notifying")
            return
        logger.info("StopNotify called, disabling notifications")
        self.notifying = False


# --- Advertisement object ---

class SimpleAdvertisement(dbus.service.Object):
    """
    LEAdvertisement1 implementation.
    Type='peripheral', advertises SERVICE_UUID and robot name.
    """

    PATH_BASE = '/com/innate/ble/advertisement'

    def __init__(self, bus, index, service_uuids, local_name):
        self.path = f'{self.PATH_BASE}{index}'
        self.bus = bus
        self.service_uuids = service_uuids or []
        self.local_name = local_name
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        props = {
            'Type': 'peripheral',
            'IncludeTxPower': dbus.Boolean(True),
        }
        if self.service_uuids:
            props['ServiceUUIDs'] = dbus.Array(self.service_uuids, signature='s')
        if self.local_name:
            props['LocalName'] = dbus.String(self.local_name)
        return {LE_ADV_IFACE: props}

    @dbus.service.method(DBUS_PROP_IFACE,
                         in_signature='s',
                         out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADV_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADV_IFACE]

    @dbus.service.method(LE_ADV_IFACE,
                         in_signature='',
                         out_signature='')
    def Release(self):
        logger.info("Advertisement released")


# --- Adapter discovery ---

def find_adapter(bus):
    """
    Find adapter that exposes both GattManager1 and LEAdvertisingManager1.
    """
    om = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, '/'),
        DBUS_OM_IFACE,
    )
    objects = om.GetManagedObjects()
    for path, ifaces in objects.items():
        if GATT_MANAGER_IFACE in ifaces and LE_ADV_MGR_IFACE in ifaces:
            return path
    return None


# --- Main ---

def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adapter_path = find_adapter(bus)
    if adapter_path is None:
        logger.error("No adapter with GattManager1 + LEAdvertisingManager1 found")
        sys.exit(1)

    logger.info(f"Using adapter at {adapter_path}")

    adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
    adapter_props = dbus.Interface(adapter_obj, DBUS_PROP_IFACE)

    # Set alias, power, discoverable
    logger.info(f"Setting adapter alias to '{ROBOT_NAME}'")
    adapter_props.Set('org.bluez.Adapter1', 'Alias', dbus.String(ROBOT_NAME))
    adapter_props.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(True))
    adapter_props.Set('org.bluez.Adapter1', 'Discoverable', dbus.Boolean(True))

    # Log current adapter properties for sanity
    cur_alias = adapter_props.Get('org.bluez.Adapter1', 'Alias')
    cur_name  = adapter_props.Get('org.bluez.Adapter1', 'Name')
    disc      = adapter_props.Get('org.bluez.Adapter1', 'Discoverable')
    logger.info(f"Adapter Name='{cur_name}', Alias='{cur_alias}', Discoverable={disc}")

    gatt_manager = dbus.Interface(adapter_obj, GATT_MANAGER_IFACE)
    adv_manager = dbus.Interface(adapter_obj, LE_ADV_MGR_IFACE)

    app = Application(bus)
    service = Service(bus, 0, SERVICE_UUID, True)
    chrc = ProvisioningCharacteristic(bus, 0, service)
    service.add_characteristic(chrc)
    app.add_service(service)

    adv = SimpleAdvertisement(bus, 0, [SERVICE_UUID], ROBOT_NAME)

    mainloop = GLib.MainLoop()

    def on_app_registered():
        logger.info("GATT application registered")

    def on_app_error(error):
        logger.error(f"Failed to register GATT application: {error}")
        mainloop.quit()

    def on_adv_registered():
        logger.info("Advertisement registered")

    def on_adv_error(error):
        logger.error(f"Failed to register advertisement: {error}")
        mainloop.quit()

    logger.info("Registering GATT application...")
    gatt_manager.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=on_app_registered,
        error_handler=on_app_error,
    )

    logger.info("Registering advertisement...")
    adv_manager.RegisterAdvertisement(
        adv.get_path(),
        {},
        reply_handler=on_adv_registered,
        error_handler=on_adv_error,
    )

    def handle_signal(sig, _frame):
        logger.info(f"Received signal {sig}, stopping BLE")
        try:
            adv_manager.UnregisterAdvertisement(adv.get_path())
        except dbus.exceptions.DBusException as e:
            logger.warning(f"UnregisterAdvertisement failed: {e}")
        mainloop.quit()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    mainloop.run()


if __name__ == '__main__':
    main()
