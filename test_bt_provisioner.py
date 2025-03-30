import asyncio
import json
from bleak import BleakScanner, BleakClient

TARGET_NAME = "ROS2_BLE_Provisioner"
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_UUID = "abcdef01-1234-5678-1234-56789abcdef0"

async def test_ble_client():
    print("Scanning for BLE devices...")
    devices = await BleakScanner.discover()
    
    # Print details of all found devices.
    for device in devices:
        print(f"Found device: {device.name}, address: {device.address}")
    
    # Try to find the device by name.
    target = next((d for d in devices if d.name and TARGET_NAME in d.name), None)
    
    if target is None:
        print("Target device not found using name filter. Trying service UUID filter...")
        # Some devices might not show the name; try filtering by advertised service UUID.
        for d in devices:
            uuids = d.metadata.get("uuids", [])
            if uuids and any(SERVICE_UUID.lower() == u.lower() for u in uuids):
                target = d
                break

    if target is None:
        print("Target device not found. Please ensure your BLE provisioner is advertising.")
        return

    print(f"Connecting to {target.name} at {target.address}")
    async with BleakClient(target.address) as client:
        if client.is_connected:
            print("Connected!")
            # Send a sample command, for example, "get_status".
            command = {"command": "get_status"}
            command_bytes = json.dumps(command).encode("utf-8")
            await client.write_gatt_char(CHAR_UUID, command_bytes)
            print("Command sent: get_status")
            # Optionally, you can attempt to read a response if your peripheral supports reading.
            # response = await client.read_gatt_char(CHAR_UUID)
            # print("Response:", response.decode("utf-8"))
        else:
            print("Failed to connect.")

# Run the BLE test client
asyncio.run(test_ble_client())
