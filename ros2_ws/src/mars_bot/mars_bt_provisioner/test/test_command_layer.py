# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Tests for the BT provisioner command layer.

Exercises the command handlers with mocked nmcli subprocess calls so we can
verify the full dispatch path (JSON in -> handler -> nmcli args -> JSON out)
without needing Bluetooth or a live NetworkManager.
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

# Ensure the package is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "mars_bt_provisioner"),
)

# We need to mock bluezero before importing simple_bt_service since it may not
# be installed in the test environment.
sys.modules["bluezero"] = MagicMock()
sys.modules["bluezero.adapter"] = MagicMock()
sys.modules["bluezero.peripheral"] = MagicMock()

import nmcli_utils  # noqa: E402
import simple_bt_service  # noqa: E402
from conftest import SERVICE_KEY, VALID_ENV  # noqa: E402
from simple_bt_service import BleProvisionerServer  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server():
    """Build a BleProvisionerServer with a mocked adapter and no real BLE."""
    mock_adapter = MagicMock()
    mock_adapter.address = "AA:BB:CC:DD:EE:FF"
    with patch.object(nmcli_utils, "_run_nmcli", return_value=(True, "", None)):
        server = BleProvisionerServer(mock_adapter)
    server._ble_characteristic = MagicMock(is_notifying=True)
    return server


def _send_command(server, payload: dict) -> dict:
    """Simulate a BLE write and return the parsed JSON response."""
    raw = bytes(json.dumps(payload), "utf-8")
    result_bytes = server.write_callback(list(raw))
    return json.loads(bytes(result_bytes).decode("utf-8"))


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    """Poll until a background handler thread has done its work."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# scan_wifi
# ---------------------------------------------------------------------------


class TestScanWifi:
    """Verify scan_wifi dispatches the right nmcli call and returns SSIDs."""

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_scan_returns_visible_ssids(self, mock_run):
        """Should return deduplicated SSIDs from nmcli wifi list --rescan."""
        mock_run.return_value = (True, "FBI_VAN_6\nOfficeNet\nOfficeNet\n", None)
        server = _make_server()

        resp = _send_command(server, {"command": "scan_wifi"})

        assert resp["status"] == "success"
        assert "FBI_VAN_6" in resp["visible_ssids"]
        assert "OfficeNet" in resp["visible_ssids"]
        # Deduplicated
        assert len([s for s in resp["visible_ssids"] if s == "OfficeNet"]) == 1

        # Verify the actual nmcli invocation uses --rescan yes and sudo
        scan_call = [c for c in mock_run.call_args_list if any("wifi" in str(a) and "rescan" in str(a) for a in c.args)]
        assert len(scan_call) >= 1
        args = scan_call[-1]
        cmd_list = args[0][0]  # first positional arg
        assert "--rescan" in cmd_list
        assert "yes" in cmd_list
        assert args[1].get("use_sudo") is True or (
            "use_sudo" in (args[1] if len(args) > 1 else {}) and args[1]["use_sudo"]
        )

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_scan_failure_returns_error(self, mock_run):
        mock_run.return_value = (False, None, "Wi-Fi is disabled")
        server = _make_server()

        resp = _send_command(server, {"command": "scan_wifi"})

        assert resp["status"] == "error"
        assert resp["visible_ssids"] == []


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Verify get_status returns configured networks, active SSID, and IP."""

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_status_with_office_network(self, mock_run):
        """Simulate a device connected to an office network."""

        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            # nmcli_get_wifi_connections – list connections
            if "connection" in cmd and "NAME,TYPE,UUID" in cmd:
                return (True, "FBI_VAN_6:802-11-wireless:uuid-1\n", None)
            # nmcli_get_wifi_connections – priority lookup
            if "autoconnect-priority" in cmd:
                return (True, "connection.autoconnect-priority:10\n", None)
            # nmcli_get_active_wifi_ssid
            if "ACTIVE,SSID" in cmd:
                return (True, "yes:FBI_VAN_6\n", None)
            # nmcli_get_active_ipv4_address – device status
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "wlP1p1s0:wifi:connected\n", None)
            # nmcli_get_active_ipv4_address – IP lookup
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.1.42/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "get_status"})

        assert resp["status"] == "success"
        assert resp["active_ssid"] == "FBI_VAN_6"
        assert resp["active_ip"] == "192.168.1.42"
        assert any(n["ssid"] == "FBI_VAN_6" for n in resp["networks"])

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_status_no_active_connection(self, mock_run):
        """Device with saved networks but no active wifi."""

        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            if "connection" in cmd and "NAME,TYPE,UUID" in cmd:
                return (True, "FBI_VAN_6:802-11-wireless:uuid-1\n", None)
            if "autoconnect-priority" in cmd:
                return (True, "connection.autoconnect-priority:5\n", None)
            if "ACTIVE,SSID" in cmd:
                return (True, "no:FBI_VAN_6\n", None)
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "wlP1p1s0:wifi:disconnected\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "get_status"})

        assert resp["status"] == "success"
        assert resp["active_ssid"] is None
        assert resp["active_ip"] is None

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_status_ethernet_only_reports_no_ip(self, mock_run):
        """WiFi forgotten, static ethernet still up: the status must not
        advertise the ethernet IP as if it were a reachable WiFi address."""

        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            if "connection" in cmd and "NAME,TYPE,UUID" in cmd:
                return (True, "", None)
            if "ACTIVE,SSID" in cmd:
                return (True, "", None)
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "eth0:ethernet:connected\nwlP1p1s0:wifi:disconnected\n", None)
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.50.2/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "get_status"})

        assert resp["status"] == "success"
        assert resp["active_ssid"] is None
        assert resp["active_ip"] is None

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_status_prefers_wifi_ip_over_ethernet(self, mock_run):
        """Ethernet enumerates before WiFi while both are connected: the
        reported IP must be the WiFi one, not the unroutable ethernet IP."""

        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            if "connection" in cmd and "NAME,TYPE,UUID" in cmd:
                return (True, "FBI_VAN_6:802-11-wireless:uuid-1\n", None)
            if "autoconnect-priority" in cmd:
                return (True, "connection.autoconnect-priority:10\n", None)
            if "ACTIVE,SSID" in cmd:
                return (True, "yes:FBI_VAN_6\n", None)
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "eth0:ethernet:connected\nwlP1p1s0:wifi:connected\n", None)
            if "IP4.ADDRESS" in cmd and "eth0" in cmd:
                return (True, "IP4.ADDRESS:192.168.50.2/24\n", None)
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.1.42/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "get_status"})

        assert resp["status"] == "success"
        assert resp["active_ssid"] == "FBI_VAN_6"
        assert resp["active_ip"] == "192.168.1.42"


# ---------------------------------------------------------------------------
# nmcli_connect retry
# ---------------------------------------------------------------------------


class TestNmcliConnectRetry:
    """Activation 'could not be found' failures get one rescan-and-retry."""

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_retries_after_rescan_when_ap_not_found(self, mock_run):
        calls = []

        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            calls.append(cmd)
            if "connection up" in cmd:
                if sum("connection up" in c for c in calls) == 1:
                    return (False, None, "Error: Connection activation failed: The Wi-Fi network could not be found")
                return (True, "Connection successfully activated", None)
            if "wifi list" in cmd:
                return (True, "INNATE_WIFI_SUPER\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect

        ok, msg = nmcli_utils.nmcli_connect("INNATE_WIFI_SUPER")

        assert ok
        # A rescan ran between the failed and successful activations
        up_indices = [i for i, c in enumerate(calls) if "connection up" in c]
        scan_indices = [i for i, c in enumerate(calls) if "wifi list" in c]
        assert len(up_indices) == 2
        assert any(up_indices[0] < s < up_indices[1] for s in scan_indices)

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_no_retry_on_terminal_errors(self, mock_run):
        mock_run.return_value = (False, None, "Error: Secrets were required, but not provided")

        ok, msg = nmcli_utils.nmcli_connect("INNATE_WIFI_SUPER")

        assert not ok
        up_calls = [c for c in mock_run.call_args_list if "up" in " ".join(c.args[0])]
        assert len(up_calls) == 1


# ---------------------------------------------------------------------------
# WPA3 profile creation
# ---------------------------------------------------------------------------


class TestWpa3ProfileCreation:
    """Save-only profiles pick SAE for WPA3-only APs, wpa-psk otherwise."""

    def _key_mgmt_of_add(self, mock_run):
        for c in mock_run.call_args_list:
            cmd_list = c.args[0]
            if "add" in cmd_list and "connection" in cmd_list:
                return cmd_list[cmd_list.index("wifi-sec.key-mgmt") + 1]
        return None

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_wpa3_only_ap_gets_sae(self, mock_run):
        def side_effect(cmd_list, **kwargs):
            if "SSID,SECURITY" in " ".join(cmd_list):
                return (True, "INNATE_WIFI_SUPER:WPA3\nOther:WPA2\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect

        ok, err = nmcli_utils.nmcli_add_or_modify_connection("INNATE_WIFI_SUPER", "pw", 10, autoconnect=False)

        assert ok
        assert self._key_mgmt_of_add(mock_run) == "sae"

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_mixed_wpa2_wpa3_ap_stays_wpa_psk(self, mock_run):
        def side_effect(cmd_list, **kwargs):
            if "SSID,SECURITY" in " ".join(cmd_list):
                return (True, "Innate_WIFI_FAST:WPA2 WPA3\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect

        ok, err = nmcli_utils.nmcli_add_or_modify_connection("Innate_WIFI_FAST", "pw", 10, autoconnect=False)

        assert ok
        assert self._key_mgmt_of_add(mock_run) == "wpa-psk"

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_unknown_ap_defaults_to_wpa_psk(self, mock_run):
        mock_run.return_value = (True, "", None)

        ok, err = nmcli_utils.nmcli_add_or_modify_connection("HiddenNet", "pw", 10, autoconnect=False)

        assert ok
        assert self._key_mgmt_of_add(mock_run) == "wpa-psk"


# ---------------------------------------------------------------------------
# update_network
# ---------------------------------------------------------------------------


class TestUpdateNetwork:
    """Verify update_network builds the correct nmcli connect/modify calls."""

    @patch.object(simple_bt_service, "time")
    @patch.object(nmcli_utils, "_run_nmcli")
    def test_add_new_network_with_password(self, mock_run, mock_time):
        """Adding a brand-new office network should save a profile carrying the PSK."""
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((" ".join(cmd_list), kwargs))
            cmd = " ".join(cmd_list)
            # connection exists check
            if "-g" in cmd_list and "NAME" in cmd and "connection" in cmd:
                return (True, "\n", None)  # no existing connections
            # device wifi connect
            if "device" in cmd and "wifi" in cmd and "connect" in cmd:
                return (True, "connected", None)
            # modify priority
            if "connection" in cmd and "modify" in cmd:
                return (True, "", None)
            # IP checks for _trigger_service_restart
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "wlP1p1s0:wifi:connected\n", None)
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.1.100/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(
            server,
            {"command": "update_network", "data": {"ssid": "OfficeNet", "password": "secret123", "priority": 20}},
        )

        # The connect runs in a background thread, so the write returns immediately.
        assert resp["status"] == "in_progress"

        add_cmds = [c for c, _ in captured_cmds if "connection add" in c and "OfficeNet" in c]
        assert len(add_cmds) == 1
        assert "wifi-sec.psk secret123" in add_cmds[0]

        # …then activated from the background thread.
        assert _wait_for(lambda: any("connection up" in c and "OfficeNet" in c for c, _ in captured_cmds))

    @patch.object(simple_bt_service, "time")
    @patch.object(nmcli_utils, "_run_nmcli")
    def test_update_existing_network_priority_only(self, mock_run, mock_time):
        """Updating priority of an existing profile should modify, not recreate."""
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((" ".join(cmd_list), kwargs))
            cmd = " ".join(cmd_list)
            if "-g" in cmd_list and "NAME" in cmd and "connection" in cmd:
                return (True, "FBI_VAN_6\n", None)  # exists
            if "connection" in cmd and "modify" in cmd:
                return (True, "", None)
            if "connection" in cmd and "up" in cmd:
                return (True, "activated", None)
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "wlP1p1s0:wifi:connected\n", None)
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.1.42/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "update_network", "data": {"ssid": "FBI_VAN_6", "priority": 50}})

        assert resp["status"] == "in_progress"
        assert _wait_for(lambda: any("connection up" in c for c, _ in captured_cmds))

        # Should NOT have called 'device wifi connect' — just modify + up
        connect_cmds = [c for c, _ in captured_cmds if "device wifi connect" in c]
        assert len(connect_cmds) == 0

        modify_cmds = [c for c, _ in captured_cmds if "connection modify" in c]
        assert len(modify_cmds) >= 1
        assert "autoconnect-priority" in modify_cmds[0]
        assert "50" in modify_cmds[0]


# ---------------------------------------------------------------------------
# remove_network
# ---------------------------------------------------------------------------


class TestRemoveNetwork:
    """Verify remove_network dispatches nmcli connection delete with sudo."""

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_remove_existing_network(self, mock_run):
        def side_effect(cmd_list, **kwargs):
            cmd = " ".join(cmd_list)
            if "-g" in cmd_list and "NAME" in cmd:
                return (True, "OldNetwork\nFBI_VAN_6\n", None)
            if "connection" in cmd and "delete" in cmd:
                return (True, "", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "remove_network", "data": {"ssid": "OldNetwork"}})

        assert resp["status"] == "success"
        delete_calls = [c for c in mock_run.call_args_list if "delete" in " ".join(c[0][0])]
        assert len(delete_calls) == 1
        assert delete_calls[0][1].get("use_sudo") is True

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_remove_nonexistent_network(self, mock_run):
        mock_run.return_value = (True, "FBI_VAN_6\n", None)
        server = _make_server()

        resp = _send_command(server, {"command": "remove_network", "data": {"ssid": "DoesNotExist"}})

        assert resp["status"] == "error"
        assert "not found" in resp["message"]


# ---------------------------------------------------------------------------
# connect_network
# ---------------------------------------------------------------------------


class TestConnectNetwork:
    """Verify connect_network scans then activates with sudo."""

    @patch.object(simple_bt_service, "time")
    @patch.object(nmcli_utils, "_run_nmcli")
    def test_connect_to_visible_network(self, mock_run, mock_time):
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((" ".join(cmd_list), kwargs))
            cmd = " ".join(cmd_list)
            # scan_for_ssid -> scan_for_visible_ssids
            if "wifi" in cmd and "list" in cmd and "rescan" in cmd:
                return (True, "FBI_VAN_6\nOfficeNet\n", None)
            # nmcli connection up
            if "connection" in cmd and "up" in cmd:
                return (True, "activated", None)
            # IP checks
            if "DEVICE,TYPE,STATE" in cmd:
                return (True, "wlP1p1s0:wifi:connected\n", None)
            if "IP4.ADDRESS" in cmd:
                return (True, "IP4.ADDRESS:192.168.1.42/24\n", None)
            return (True, "", None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {"command": "connect_network", "data": {"ssid": "FBI_VAN_6"}})

        assert resp["status"] == "in_progress"
        assert _wait_for(lambda: any("connection up" in c for c, _ in captured_cmds))

        # Should have called connection up with sudo
        up_calls = [kw for c, kw in captured_cmds if "connection up" in c]
        assert len(up_calls) >= 1
        assert up_calls[0].get("use_sudo") is True


# ---------------------------------------------------------------------------
# identify
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {"env": VALID_ENV, "password": "goodbot41"}


class TestIdentify:
    """The tune plays from a background thread and reports both phases."""

    @patch.object(simple_bt_service.subprocess, "run")
    def test_identify_returns_in_progress_then_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        server = _make_server()
        server._send_notification_threadsafe = MagicMock()

        resp = _send_command(server, {"command": "identify"})

        assert resp["status"] == "in_progress"
        assert _wait_for(lambda: server._send_notification_threadsafe.called)
        assert server._send_notification_threadsafe.call_args[0][0]["status"] == "success"
        play = [c for c in mock_run.call_args_list if "gst-play-1.0" in c.args[0]][0]
        # The unit does not preserve it, and without it there is no audio session.
        assert play.kwargs["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"

    @patch.object(simple_bt_service.subprocess, "run")
    def test_player_failure_is_distinguishable(self, mock_run):
        """A nonzero player exit must not read as 'played it and you heard nothing'."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such element\n")
        server = _make_server()
        server._send_notification_threadsafe = MagicMock()

        server._play_identify("identify")

        final = server._send_notification_threadsafe.call_args[0][0]
        assert final["status"] == "error"
        assert "No such element" in final["message"]


# ---------------------------------------------------------------------------
# get_identity
# ---------------------------------------------------------------------------


class TestGetIdentity:
    """The env file goes back for the client to parse — every key in it but the one
    that is a secret."""

    @patch.object(simple_bt_service, "system_env", return_value={})
    def test_unprovisioned_robot(self, mock_env):
        server = _make_server()

        resp = _send_command(server, {"command": "get_identity"})

        assert resp["status"] == "success"
        assert resp["provisioned"] is False
        assert resp["env"] == {}

    @patch.object(
        simple_bt_service,
        "system_env",
        return_value={"INNATE_SERVICE_KEY": SERVICE_KEY, "ROBOT_ID": "R7-41", "MODULE_SERIAL": "1424523016164"},
    )
    def test_provisioned_robot_never_echoes_the_key(self, mock_env):
        server = _make_server()

        resp = _send_command(server, {"command": "get_identity"})

        assert resp["provisioned"] is True
        assert resp["env"] == {"ROBOT_ID": "R7-41", "MODULE_SERIAL": "1424523016164"}
        assert SERVICE_KEY not in json.dumps(resp)

    @patch.object(
        simple_bt_service,
        "system_env",
        return_value={"COLOR_VARIANT": "blue", "SOME_KEY_ADDED_LATER": "1"},
    )
    def test_echoes_keys_it_has_never_heard_of(self, mock_env):
        """A denylist of one: a key added to the env file reaches the client without a
        robot-side change, which is the whole point of returning the env."""
        server = _make_server()

        resp = _send_command(server, {"command": "get_identity"})

        assert resp["env"] == {"COLOR_VARIANT": "blue", "SOME_KEY_ADDED_LATER": "1"}

    @patch.object(simple_bt_service, "system_env", return_value={})
    @patch.object(simple_bt_service.socket, "gethostname", return_value="mars-a1b2")
    def test_hostname_is_the_mdns_name(self, mock_hostname, mock_env):
        server = _make_server()

        resp = _send_command(server, {"command": "get_identity"})

        assert resp["hostname"] == "mars-a1b2.local"


# ---------------------------------------------------------------------------
# set_identity
# ---------------------------------------------------------------------------


class TestSetIdentity:
    """Accepted only while unprovisioned, validated before it reaches root, and
    forwarded to the helper unparsed."""

    def _server_unprovisioned(self):
        server = _make_server()
        server._send_notification_threadsafe = MagicMock()
        return server

    @patch.object(simple_bt_service, "is_provisioned", return_value=False)
    @patch.object(simple_bt_service.subprocess, "run")
    def test_blob_is_forwarded_on_stdin_never_argv(self, mock_run, mock_provisioned):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"robot_id": "R7-41", "robot_name": "MARS the 41st"}\n', stderr=""
        )
        server = self._server_unprovisioned()
        order = []
        server._send_notification_threadsafe = MagicMock(side_effect=lambda r: order.append("reply"))
        with patch.object(simple_bt_service.subprocess, "Popen") as mock_popen:
            mock_popen.side_effect = lambda *a, **kw: order.append("reboot") or MagicMock()
            resp = _send_command(server, {"command": "set_identity", "data": VALID_PAYLOAD})

            assert resp["status"] == "in_progress"
            assert _wait_for(lambda: mock_popen.called)

        write_call = [c for c in mock_run.call_args_list if "--write" in c.args[0]][0]
        assert write_call.args[0][:2] == ["sudo", simple_bt_service.IDENTITY_TOOL_PATH]
        assert SERVICE_KEY not in " ".join(write_call.args[0])
        assert json.loads(write_call.kwargs["input"]) == VALID_PAYLOAD

        final = server._send_notification_threadsafe.call_args[0][0]
        assert final["status"] == "success"
        assert final["robot_id"] == "R7-41"
        assert SERVICE_KEY not in json.dumps(final)
        assert "goodbot41" not in json.dumps(final)

        # The reply has to be on the wire first: a reboot racing systemd's teardown of
        # this unit against GLib.idle_add drops it silently.
        assert order == ["reply", "reboot"]

        # ros-app recreates robot_info.json from its launch-time env within a second, so
        # resetting it before the stop would be undone before the reboot lands.
        teardown = " ".join(mock_popen.call_args[0][0])
        assert teardown.index("systemctl stop ros-app") < teardown.index("--reset-info")
        assert teardown.index("--reset-info") < teardown.index("reboot")
        assert "--wipe-data" not in teardown

    @patch.object(simple_bt_service, "is_provisioned", return_value=False)
    @patch.object(simple_bt_service.subprocess, "run")
    def test_wipe_data_rides_along_as_its_own_call(self, mock_run, mock_provisioned):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"robot_id": "R7-41"}\n', stderr="")
        server = self._server_unprovisioned()

        with patch.object(simple_bt_service.subprocess, "Popen") as mock_popen:
            _send_command(server, {"command": "set_identity", "data": {**VALID_PAYLOAD, "wipe_data": True}})
            assert _wait_for(lambda: mock_popen.called)

        assert "--wipe-data" in " ".join(mock_popen.call_args[0][0])

    @patch.object(simple_bt_service, "is_provisioned", return_value=True)
    def test_refused_once_provisioned(self, mock_provisioned):
        server = self._server_unprovisioned()

        resp = _send_command(server, {"command": "set_identity", "data": VALID_PAYLOAD})

        assert resp["status"] == "error"
        assert "provisioned" in resp["message"].lower()

    @patch.object(simple_bt_service, "is_provisioned", return_value=False)
    @patch.object(simple_bt_service.subprocess, "run")
    def test_helper_refusal_maps_to_its_own_error(self, mock_run, mock_provisioned):
        """The root helper is the gate that counts: it re-reads the file itself."""
        mock_run.return_value = MagicMock(returncode=3, stdout="", stderr="already provisioned\n")
        server = self._server_unprovisioned()

        _send_command(server, {"command": "set_identity", "data": VALID_PAYLOAD})

        assert _wait_for(lambda: server._send_notification_threadsafe.called)
        final = server._send_notification_threadsafe.call_args[0][0]
        assert final["status"] == "error"
        assert "provisioned" in final["message"].lower()

    @patch.object(simple_bt_service, "is_provisioned", return_value=False)
    @patch.object(simple_bt_service.subprocess, "run")
    def test_malformed_blobs_never_reach_root(self, mock_run, mock_provisioned):
        server = self._server_unprovisioned()
        bad_envs = {
            "control character": f"INNATE_SERVICE_KEY={SERVICE_KEY}\nROBOT_ID=R7-41\x07\n",
            "malformed line": f"INNATE_SERVICE_KEY={SERVICE_KEY}\nrobot id: R7-41\n",
            "loader steering": f"INNATE_SERVICE_KEY={SERVICE_KEY}\nLD_PRELOAD=/tmp/evil.so\n",
        }

        for label, env in bad_envs.items():
            resp = _send_command(server, {"command": "set_identity", "data": {"env": env, "password": "goodbot41"}})
            assert resp["status"] == "error", label

        assert mock_run.call_count == 0


# ---------------------------------------------------------------------------
# Advertised name
# ---------------------------------------------------------------------------


class TestLoadRobotName:
    """The advertised name must resolve on a robot that has never booted the ROS app —
    that is the robot which most needs BLE."""

    def test_prefers_robot_info(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "robot_info.json").write_text('{"robot_name": "MARS the 41st"}')

        with patch.object(simple_bt_service, "INNATE_OS_ROOT", str(tmp_path)):
            assert simple_bt_service.load_robot_name() == "MARS the 41st"

    def test_falls_back_to_system_env(self, tmp_path):
        env_file = tmp_path / "innate.env"
        env_file.write_text("ROBOT_NAME=MARS the 41st\n")

        with (
            patch.object(simple_bt_service, "INNATE_OS_ROOT", str(tmp_path)),
            patch.object(simple_bt_service, "SYSTEM_ENV_PATH", str(env_file)),
        ):
            assert simple_bt_service.load_robot_name() == "MARS the 41st"

    def test_survives_an_unreadable_everything(self, tmp_path):
        with (
            patch.object(simple_bt_service, "INNATE_OS_ROOT", str(tmp_path)),
            patch.object(simple_bt_service, "SYSTEM_ENV_PATH", str(tmp_path / "missing.env")),
        ):
            assert simple_bt_service.load_robot_name() == "MARS"


# ---------------------------------------------------------------------------
# Unknown / malformed commands
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify bad input is handled gracefully."""

    def test_unknown_command(self):
        server = _make_server()
        resp = _send_command(server, {"command": "reboot_robot"})
        assert resp["status"] == "error"
        assert resp["command"] == "reboot_robot"

    def test_missing_command_field(self):
        server = _make_server()
        resp = _send_command(server, {"foo": "bar"})
        assert resp["status"] == "error"

    def test_invalid_json(self):
        server = _make_server()
        raw = b"this is not json"
        result_bytes = server.write_callback(list(raw))
        resp = json.loads(bytes(result_bytes).decode("utf-8"))
        assert resp["status"] == "error"
        assert "JSON" in resp["message"]

    @patch.object(nmcli_utils, "_run_nmcli")
    def test_update_network_missing_ssid(self, mock_run):
        mock_run.return_value = (True, "", None)
        server = _make_server()
        resp = _send_command(server, {"command": "update_network", "data": {"password": "noSSID"}})
        assert resp["status"] == "error"
        assert "SSID" in resp["message"]


# ---------------------------------------------------------------------------
# nmcli command argument verification
# ---------------------------------------------------------------------------


class TestNmcliCommandShape:
    """
    Verify that the nmcli commands built by nmcli_utils are structurally
    correct — right subcommands, flags, and field selections.
    """

    @patch("nmcli_utils.subprocess")
    def test_rescan_command_shape(self, mock_subprocess):
        """scan_for_visible_ssids should build a well-formed list --rescan cmd."""
        mock_result = MagicMock()
        mock_result.stdout = "TestNet\n"
        mock_result.stderr = ""
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        nmcli_utils.nmcli_scan_for_visible_ssids()

        call_args = mock_subprocess.run.call_args[0][0]
        # Should have sudo prefix
        assert call_args[0] == "sudo"
        # Core command structure
        assert "nmcli" in call_args
        assert "device" in call_args
        assert "wifi" in call_args
        assert "list" in call_args
        assert "--rescan" in call_args
        assert "yes" in call_args
        # Terse SSID-only output
        assert "-t" in call_args
        ssid_idx = call_args.index("-f")
        assert call_args[ssid_idx + 1] == "SSID"

    @patch("nmcli_utils.subprocess")
    def test_scan_for_ssid_delegates(self, mock_subprocess):
        """scan_for_ssid should call scan_for_visible_ssids (single scan path)."""
        mock_result = MagicMock()
        mock_result.stdout = "FBI_VAN_6\nOtherNet\n"
        mock_result.stderr = ""
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        success, found, err = nmcli_utils.nmcli_scan_for_ssid("FBI_VAN_6")

        assert success is True
        assert found is True
        # Only one subprocess call — no separate rescan
        assert mock_subprocess.run.call_count == 1

    @patch("nmcli_utils.subprocess")
    def test_delete_connection_uses_sudo(self, mock_subprocess):
        """delete_connection should run with sudo."""
        mock_result = MagicMock()
        mock_result.stdout = "TargetNet\n"
        mock_result.stderr = ""
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        nmcli_utils.nmcli_delete_connection("TargetNet")

        # Find the delete call (second call — first is the existence check)
        calls = mock_subprocess.run.call_args_list
        delete_calls = [c for c in calls if "delete" in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][0][0] == "sudo"
