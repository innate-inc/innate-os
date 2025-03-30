#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from bluezero import peripheral
import bluetooth

class BLEProvisioner(Node):
    def __init__(self):
        super().__init__('ble_provisioner')
        self.get_logger().info("Starting BLE Provisioner Node")

        # Define custom service and characteristic UUIDs
        self.service_uuid = '12345678-1234-5678-1234-56789abcdef0'
        self.char_uuid = 'abcdef01-1234-5678-1234-56789abcdef0'
        
        # Replace with your adapter’s BD_ADDR (you can get it using bluetooth.read_local_bdaddr())
        adapter_addr = bluetooth.read_local_bdaddr()[0]

        print(f"Adapter address: {adapter_addr}")
        
        # Create a Bluezero Peripheral instance.
        # 'local_name' is the name that will appear during BLE scanning.
        self.ble_peripheral = peripheral.Peripheral(adapter_addr,
                                                    local_name='ROS2_BLE_Provisioner')

        # Add a service (srv_id can be any integer identifier)
        self.ble_peripheral.add_service(srv_id=1, uuid=self.service_uuid, primary=True)
        
        # Add a characteristic to the service with the "write" property.
        # When a client writes data, the on_write callback is invoked.
        self.ble_peripheral.add_characteristic(srv_id=1,
                                               chr_id=1,
                                               uuid=self.char_uuid,
                                               value=[],  # initial value (empty)
                                               notifying=False,
                                               flags=['write'],
                                               write_callback=self.on_write)
        
        # Publish the BLE service (i.e., start advertising)
        self.ble_peripheral.publish()
        self.get_logger().info("BLE advertising started")

    def on_write(self, value, options):
        """
        Callback function when the BLE client writes to the characteristic.
        'value' is a bytearray containing the data sent by the client.
        """
        try:
            credentials = value.decode('utf-8')
            self.get_logger().info(f"Received BLE data: {credentials}")
            # Here, parse the credentials (perhaps as JSON, e.g., {"ssid": "mySSID", "password": "myPass"})
            # and trigger the Wi-Fi reconfiguration process.
        except Exception as e:
            self.get_logger().error(f"Error decoding BLE data: {e}")

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
