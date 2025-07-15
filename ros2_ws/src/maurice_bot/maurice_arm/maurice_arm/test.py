#!/usr/bin/env python3
"""
detect_ports.py

Scan all /dev/ttyACM* ports, try reading the Model Number (2 bytes)
at register 0 for IDs 1–10, and report how many servos and their types.
"""

import glob
from dynamixel_sdk import PortHandler, PacketHandler, COMM_SUCCESS

# --- CONFIG ---
PROTOCOL_VERSION  = 2.0
BAUDRATE          = 1_000_000
ADDR_MODEL_NUMBER = 0         # control table addr for Model Number (2 bytes)
ID_RANGE          = range(1, 11)

# human-friendly names for a couple of common models
MODEL_MAP = {
    1060: "XL430",
    1200: "XL330",
}

def detect_servos(dev_path):
    """Open `dev_path`, read model number at addr 0 for IDs 1–10."""
    port    = PortHandler(dev_path)
    handler = PacketHandler(PROTOCOL_VERSION)
    servos  = []

    if not port.openPort():
        print(f"[!] Cannot open {dev_path}")
        return servos
    port.setBaudRate(BAUDRATE)

    for sid in ID_RANGE:
        # read2ByteTxRx returns (value, comm_result, error)
        model_num, com_res, err = handler.read2ByteTxRx(
            port, sid, ADDR_MODEL_NUMBER
        )
        if com_res == COMM_SUCCESS:
            servos.append((sid, model_num))
        # else: no servo at that ID (or comm timed out)

    port.closePort()
    return servos

def main():
    print("Scanning for Dynamixel chains (torque ON, powered servos)…\n")
    for dev in sorted(glob.glob('/dev/ttyACM*')):
        found = detect_servos(dev)
        print(f"Port: {dev} → {len(found)} servo(s) detected")
        for sid, model in found:
            name = MODEL_MAP.get(model, "UNKNOWN")
            print(f"  • ID={sid:2d}  ModelNumber={model}  ({name})")
        print()

if __name__ == "__main__":
    main()
