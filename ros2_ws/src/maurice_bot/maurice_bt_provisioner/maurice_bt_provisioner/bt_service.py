#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from bluezero import peripheral
import bluetooth
import json
import hashlib
import os
import subprocess

CONFIG_FILE = 'wifi_config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    else:
        return {"networks": []}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)

class BLEProvisioner(Node):
    def __init__(self):
        super().__init__('ble_provisioner')
        self.get_logger().info("Starting BLE Provisioner Node")

        # Define custom service and characteristic UUIDs
        self.service_uuid = '12345678-1234-5678-1234-56789abcdef0'
        self.char_uuid = 'abcdef01-1234-5678-1234-56789abcdef0'
        
        # Get the adapter's BD_ADDR
        adapter_addr = bluetooth.read_local_bdaddr()[0]
        self.get_logger().info(f"Adapter address: {adapter_addr}")
        
        # Create a Bluezero Peripheral instance.
        self.ble_peripheral = peripheral.Peripheral(adapter_addr,
                                                    local_name='ROS2_BLE_Provisioner')

        # Add a service (srv_id can be any integer identifier)
        self.ble_peripheral.add_service(srv_id=1, uuid=self.service_uuid, primary=True)
        
        # Add a characteristic to the service with the "write" property.
        self.ble_peripheral.add_characteristic(srv_id=1,
                                               chr_id=1,
                                               uuid=self.char_uuid,
                                               value=[],  # initial value
                                               notifying=False,
                                               flags=['write'],
                                               write_callback=self.on_write)
        # Publish the BLE service (i.e., start advertising)
        self.ble_peripheral.publish()
        self.get_logger().info("BLE advertising started")

    def on_write(self, value, options):
        """
        Callback function when the BLE client writes to the characteristic.
        Expected JSON commands:
          - update_network:
            {"command": "update_network", "data": {"ssid": "MySSID", "password": "MyPassword", "priority": 10}}
          - get_status:
            {"command": "get_status"}
          - connect_network:
            {"command": "connect_network"}
        """
        try:
            message = value.decode('utf-8')
            self.get_logger().info(f"Received BLE data: {message}")
            data = json.loads(message)
            command = data.get("command", "").lower()
            
            if command == "update_network":
                net_data = data.get("data", {})
                ssid = net_data.get("ssid")
                password = net_data.get("password")
                priority = net_data.get("priority", 0)
                if not ssid or not password:
                    self.get_logger().error("Missing SSID or password in update_network command.")
                    return
                # Compute SHA-256 hash of the password
                pw_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
                self.get_logger().info(f"Updating network: SSID={ssid}, Priority={priority}, Password hash={pw_hash}")
                # Load existing configuration
                config = load_config()
                networks = config.get("networks", [])
                # Update if the SSID already exists; otherwise, append new entry.
                updated = False
                for net in networks:
                    if net.get("ssid") == ssid:
                        net["password_hash"] = pw_hash
                        net["priority"] = priority
                        updated = True
                        break
                if not updated:
                    networks.append({"ssid": ssid, "password_hash": pw_hash, "priority": priority})
                config["networks"] = networks
                save_config(config)
                self.get_logger().info(f"Network configuration updated in {CONFIG_FILE}")
                # Optionally, add a system call to store the Wi-Fi configuration securely.
                
            elif command == "get_status":
                config = load_config()
                networks = config.get("networks", [])
                # Build a status response (sensitive details omitted)
                status = {"networks": [{"ssid": net.get("ssid"), "priority": net.get("priority")} for net in networks]}
                status_msg = json.dumps({"command": "status", "data": status})
                self.get_logger().info(f"Status: {status_msg}")
                # You could update a BLE characteristic and notify the client here.
                
            elif command == "connect_network":
                config = load_config()
                networks = config.get("networks", [])
                if not networks:
                    self.get_logger().error("No networks configured.")
                    return
                # Select the network with the highest priority
                selected = max(networks, key=lambda n: n.get("priority", 0))
                ssid = selected.get("ssid")
                self.get_logger().info(f"Attempting to connect to network: {ssid}")
                # Execute a local command to connect to the Wi-Fi network.
                # This assumes that the network's configuration has been stored in the OS (e.g., via nmcli).
                result = subprocess.run(["nmcli", "connection", "up", ssid],
                                        capture_output=True, text=True)
                if result.returncode == 0:
                    self.get_logger().info(f"Successfully connected to {ssid}")
                else:
                    self.get_logger().error(f"Failed to connect to {ssid}: {result.stderr}")
                
            else:
                self.get_logger().warn(f"Unknown command received: {command}")
                
        except Exception as e:
            self.get_logger().error(f"Error processing BLE data: {e}")

    def destroy_node(self):
        # Stop BLE advertising before shutting down.
        self.ble_peripheral.unpublish()
        super().destroy_node()

def main(args=None):
    rclpy.init(args=args)
    node = BLEProvisioner()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("BLE Provisioner Node shutting down")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
