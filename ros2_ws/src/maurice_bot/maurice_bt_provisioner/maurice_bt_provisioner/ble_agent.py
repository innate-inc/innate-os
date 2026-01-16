#!/usr/bin/env python3
"""
Simple BlueZ pairing agent that auto-accepts pairing requests (NoInputNoOutput capability).
This enables "Just Works" pairing for BLE devices.
"""

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/org/bluez/innate_agent"

class Agent(dbus.service.Object):
    """BlueZ pairing agent with NoInputNoOutput capability."""

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        print("Agent released")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"AuthorizeService: device={device}, uuid={uuid}")
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        print(f"RequestPinCode: device={device}")
        return "0000"

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        print(f"RequestPasskey: device={device}")
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        print(f"DisplayPasskey: device={device}, passkey={passkey:06d}, entered={entered}")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        print(f"DisplayPinCode: device={device}, pincode={pincode}")

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"RequestConfirmation: device={device}, passkey={passkey:06d} - auto-accepting")
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print(f"RequestAuthorization: device={device} - auto-accepting")
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        print("Pairing canceled")


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # Create and register the agent
    agent = Agent(bus, AGENT_PATH)

    # Get the AgentManager
    manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.AgentManager1"
    )

    # Register agent with NoInputNoOutput capability for "Just Works" pairing
    manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    print(f"Agent registered at {AGENT_PATH} with NoInputNoOutput capability")

    # Make this the default agent
    manager.RequestDefaultAgent(AGENT_PATH)
    print("Agent set as default")

    # Run the main loop
    mainloop = GLib.MainLoop()
    try:
        print("BLE pairing agent running...")
        mainloop.run()
    except KeyboardInterrupt:
        print("\nShutting down agent")
        manager.UnregisterAgent(AGENT_PATH)


if __name__ == "__main__":
    main()
