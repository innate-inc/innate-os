# Service credentials and ROS

`INNATE_SERVICE_KEY` is private process configuration for UniNavid, training,
and telemetry. Set it in the owner's `.env` or service environment and restart
the affected node. Do not put credentials in ROS parameters, launch arguments,
or a ROS topic: parameter services, `/parameter_events`, and topic subscriptions
are reachable through the robot/simulator webapp's rosbridge connection.

The former `service_key` ROS parameter and UniNavid `/brain/backend_config`
credential update subscription are removed. Existing YAML or command-line
parameter overrides must migrate to environment configuration. Owner auth still
uses the same `AuthProvider`; no credential is returned to the browser.

`tests/test_ros_credential_parameters.py` exercises a real node's ROS parameter
service and, with `INNATE_TEST_ROSBRIDGE=1`, the HTTP `/ws` relay through an
installed `rws_server`. Run in a sourced ROS workspace with its normal DDS/Zenoh
transport available. Tests inject only a synthetic canary and never start a
navigation goal or call a provider. The private environment value remains
available to auth while parameter queries return no credential and ordinary
configuration queries still work.
