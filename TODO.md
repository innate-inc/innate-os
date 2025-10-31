# TODO List

## System Status Monitoring
- [ ] Add a system status checker node in `app.launch.py`
  - Should monitor the health/status of other ROS nodes in the system
  - Publish status information on a topic (e.g., `/system/status`)
  - Enable the app to reliably know when all systems are ready
  - Could include: node status, hardware readiness, network connectivity, etc.

## Robot Name Display
- [ ] Fix robot name display consistency
  - Clean up how the robot name appears in the app
  - Fix Bluetooth broadcast name formatting
  - Fix WiFi hotspot name formatting
  - Ensure consistent naming across all interfaces

