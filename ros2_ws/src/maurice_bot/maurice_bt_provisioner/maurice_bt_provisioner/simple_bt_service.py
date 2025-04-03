#!/usr/bin/env python3
import logging
import json
import signal
import sys

from bluezero import async_tools
from bluezero import adapter
from bluezero import peripheral

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   stream=sys.stdout)
logger = logging.getLogger('BLE_Server')

# BLE UUIDs - Using the same as in your React app
SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'
CHARACTERISTIC_UUID = 'abcdef01-1234-5678-1234-56789abcdef0'

# Mock network data
networks = [
    {"ssid": "Home_WiFi", "priority": 10},
    {"ssid": "Office_Network", "priority": 5}
]

# Store the characteristic object for sending notifications
_ble_characteristic = None

def write_callback(value, options=None):
    """Handle write requests from clients"""
    logger.info(f"write_callback invoked with value (type {type(value)}): {value}")
    logger.info(f"Options: {options}")
    try:
        # Convert bytes to string
        value_str = bytes(value).decode('utf-8')
        logger.info(f"Received write: {value_str}")
        
        # Parse the JSON command
        data = json.loads(value_str)
        command = data.get('command')
        
        response = None
        
        if command == 'get_status':
            logger.info("Handling get_status command")
            response = {"status": "success", "networks": networks}
        
        elif command == 'update_network':
            logger.info("Handling update_network command")
            network_data = data.get('data', {})
            ssid = network_data.get('ssid')
            
            if ssid:
                # Check if network exists, update it or add new one
                found = False
                for net in networks:
                    if net['ssid'] == ssid:
                        net['priority'] = network_data.get('priority', 10)
                        found = True
                        break
                
                if not found:
                    networks.append({
                        'ssid': ssid,
                        'priority': network_data.get('priority', 10)
                    })
                
                response = {"status": "success", "message": f"Network {ssid} updated"}
            else:
                response = {"status": "error", "message": "SSID required"}
        
        elif command == 'connect_network':
            logger.info("Handling connect_network command")
            response = {"status": "success", "message": "Connecting to highest priority network"}
        
        else:
            logger.warning(f"Unknown command: {command}")
            response = {"status": "error", "message": "Unknown command"}
        
        if response:
            logger.info(f"Response: {response}")
            
            # Send the response as a notification if the characteristic is available
            if _ble_characteristic and _ble_characteristic.is_notifying:
                response_bytes = bytes(json.dumps(response), 'utf-8')
                logger.info(f"Sending notification with response: {response_bytes}")
                _ble_characteristic.set_value(list(response_bytes))
                
        # No return value needed for notifications, but still return for backward compatibility
        # with existing clients that expect a return value
        return bytes(json.dumps(response), 'utf-8') if response else None
    
    except Exception as e:
        logger.error(f"Error processing command: {e}")
        error_response = {"status": "error", "message": str(e)}
        
        # Send error via notification if possible
        if _ble_characteristic and _ble_characteristic.is_notifying:
            error_bytes = bytes(json.dumps(error_response), 'utf-8')
            _ble_characteristic.set_value(list(error_bytes))
            
        return bytes(json.dumps(error_response), 'utf-8')

def read_callback():
    """Handle read requests"""
    logger.info("Read request received")
    response = {"status": "connected", "networks": networks}
    return bytes(json.dumps(response), 'utf-8')

def notify_callback(notifying, characteristic):
    """Handle notification setup"""
    global _ble_characteristic
    logger.info(f"Notification {'started' if notifying else 'stopped'}")
    
    # Store the characteristic object for later use in write_callback
    if notifying:
        _ble_characteristic = characteristic
    else:
        _ble_characteristic = None

def main(adapter: adapter.Adapter):
    """Initialize and run the BLE server"""
    logger.info("Starting BLE WiFi Provisioner Server")

    adapter_address = adapter.address
    
    # Print adapter address
    logger.info(f"Using adapter at address: {adapter_address}")

    print(f"Is discoverable: {adapter.discoverable}, is powered: {adapter.powered}, is pairable: {adapter.pairable}, pairable timeout: {adapter.pairabletimeout}, discoverable timeout: {adapter.discoverabletimeout}")
    
    # Create peripheral
    wifi_provisioner = peripheral.Peripheral(adapter_address,
                                             local_name='ROS2_BLE_Provisioner',
                                             appearance=0)
    
    # Add connection callbacks (if supported)
    def on_connect(device):
        logger.info("Client connected!")
    
    def on_disconnect(device):
        logger.info("Client disconnected!")
    
    # Set callbacks if supported
    if hasattr(wifi_provisioner, 'on_connect'):
        wifi_provisioner.on_connect = on_connect
    if hasattr(wifi_provisioner, 'on_disconnect'):
        wifi_provisioner.on_disconnect = on_disconnect
    
    # Add service
    wifi_provisioner.add_service(srv_id=1, uuid=SERVICE_UUID, primary=True)
    
    # Add characteristic with read and write capabilities
    wifi_provisioner.add_characteristic(
        srv_id=1, 
        chr_id=1, 
        uuid=CHARACTERISTIC_UUID,
        value=[],
        notifying=False,
        flags=['read', 'write', 'write-without-response', 'notify'],
        read_callback=read_callback,
        write_callback=write_callback,
        notify_callback=notify_callback
    )
    
    logger.info(f"Characteristic added with UUID: {CHARACTERISTIC_UUID}")
    logger.info(f"Characteristic flags: read, write, write-without-response, notify")
    logger.info(f"Read callback: {read_callback.__name__}, Write callback: {write_callback.__name__}")
    
    # Set up signal handler for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Stopping BLE service...")
        wifi_provisioner.mainloop.quit()

    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Start advertising
        logger.info("Starting BLE advertisement...")
        wifi_provisioner.publish()
    except Exception as e:
        logger.error(f"Error in BLE advertisement: {e}")
        return

if __name__ == '__main__':
    available_adapters = list(adapter.Adapter.available())
    if not available_adapters:
        logger.error("No Bluetooth adapters found")
        sys.exit(1)
    
    main(available_adapters[0])