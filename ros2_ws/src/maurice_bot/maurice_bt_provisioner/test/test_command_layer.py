"""
Tests for the BT provisioner command layer.

Exercises the command handlers with mocked nmcli subprocess calls so we can
verify the full dispatch path (JSON in -> handler -> nmcli args -> JSON out)
without needing Bluetooth or a live NetworkManager.
"""

import json
import sys
import os
from unittest.mock import patch, MagicMock

# Ensure the package is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', 'maurice_bt_provisioner'),
)

# We need to mock bluezero before importing simple_bt_service since it may not
# be installed in the test environment.
sys.modules['bluezero'] = MagicMock()
sys.modules['bluezero.adapter'] = MagicMock()
sys.modules['bluezero.peripheral'] = MagicMock()

import nmcli_utils
from nmcli_utils import _run_nmcli

# Mock robot_info.json read before importing simple_bt_service since
# load_robot_name() runs at module level and the file won't exist in CI.
with patch('builtins.open', MagicMock(return_value=__import__('io').StringIO('{"robot_name": "TEST"}'))):
    import simple_bt_service
    from simple_bt_service import BleProvisionerServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server():
    """Build a BleProvisionerServer with a mocked adapter and no real BLE."""
    mock_adapter = MagicMock()
    mock_adapter.address = 'AA:BB:CC:DD:EE:FF'
    with patch.object(nmcli_utils, '_run_nmcli', return_value=(True, '', None)):
        server = BleProvisionerServer(mock_adapter)
    server._ble_characteristic = MagicMock(is_notifying=True)
    return server


def _send_command(server, payload: dict) -> dict:
    """Simulate a BLE write and return the parsed JSON response."""
    raw = bytes(json.dumps(payload), 'utf-8')
    result_bytes = server.write_callback(list(raw))
    return json.loads(bytes(result_bytes).decode('utf-8'))


# ---------------------------------------------------------------------------
# scan_wifi
# ---------------------------------------------------------------------------

class TestScanWifi:
    """Verify scan_wifi dispatches the right nmcli call and returns SSIDs."""

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_scan_returns_visible_ssids(self, mock_run):
        """Should return deduplicated SSIDs from nmcli wifi list --rescan."""
        mock_run.return_value = (True, 'FBI_VAN_6\nOfficeNet\nOfficeNet\n', None)
        server = _make_server()

        resp = _send_command(server, {'command': 'scan_wifi'})

        assert resp['status'] == 'success'
        assert 'FBI_VAN_6' in resp['visible_ssids']
        assert 'OfficeNet' in resp['visible_ssids']
        # Deduplicated
        assert len([s for s in resp['visible_ssids'] if s == 'OfficeNet']) == 1

        # Verify the actual nmcli invocation uses --rescan yes and sudo
        scan_call = [
            c for c in mock_run.call_args_list
            if any('wifi' in str(a) and 'rescan' in str(a) for a in c.args)
        ]
        assert len(scan_call) >= 1
        args = scan_call[-1]
        cmd_list = args[0][0]  # first positional arg
        assert '--rescan' in cmd_list
        assert 'yes' in cmd_list
        assert args[1].get('use_sudo') is True or \
            ('use_sudo' in (args[1] if len(args) > 1 else {}) and args[1]['use_sudo'])

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_scan_failure_returns_error(self, mock_run):
        mock_run.return_value = (False, None, 'Wi-Fi is disabled')
        server = _make_server()

        resp = _send_command(server, {'command': 'scan_wifi'})

        assert resp['status'] == 'error'
        assert resp['visible_ssids'] == []


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    """Verify get_status returns configured networks, active SSID, and IP."""

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_status_with_office_network(self, mock_run):
        """Simulate a device connected to an office network."""
        def side_effect(cmd_list, **kwargs):
            cmd = ' '.join(cmd_list)
            # nmcli_get_wifi_connections – list connections
            if 'connection' in cmd and 'NAME,TYPE,UUID' in cmd:
                return (True, 'FBI_VAN_6:802-11-wireless:uuid-1\n', None)
            # nmcli_get_wifi_connections – priority lookup
            if 'autoconnect-priority' in cmd:
                return (True, 'connection.autoconnect-priority:10\n', None)
            # nmcli_get_active_wifi_ssid
            if 'ACTIVE,SSID' in cmd:
                return (True, 'yes:FBI_VAN_6\n', None)
            # nmcli_get_active_ipv4_address – device status
            if 'DEVICE,TYPE,STATE' in cmd:
                return (True, 'wlP1p1s0:wifi:connected\n', None)
            # nmcli_get_active_ipv4_address – IP lookup
            if 'IP4.ADDRESS' in cmd:
                return (True, 'IP4.ADDRESS:192.168.1.42/24\n', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {'command': 'get_status'})

        assert resp['status'] == 'success'
        assert resp['active_ssid'] == 'FBI_VAN_6'
        assert resp['active_ip'] == '192.168.1.42'
        assert any(n['ssid'] == 'FBI_VAN_6' for n in resp['networks'])

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_status_no_active_connection(self, mock_run):
        """Device with saved networks but no active wifi."""
        def side_effect(cmd_list, **kwargs):
            cmd = ' '.join(cmd_list)
            if 'connection' in cmd and 'NAME,TYPE,UUID' in cmd:
                return (True, 'FBI_VAN_6:802-11-wireless:uuid-1\n', None)
            if 'autoconnect-priority' in cmd:
                return (True, 'connection.autoconnect-priority:5\n', None)
            if 'ACTIVE,SSID' in cmd:
                return (True, 'no:FBI_VAN_6\n', None)
            if 'DEVICE,TYPE,STATE' in cmd:
                return (True, 'wlP1p1s0:wifi:disconnected\n', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {'command': 'get_status'})

        assert resp['status'] == 'success'
        assert resp['active_ssid'] is None
        assert resp['active_ip'] is None


# ---------------------------------------------------------------------------
# update_network
# ---------------------------------------------------------------------------

class TestUpdateNetwork:
    """Verify update_network builds the correct nmcli connect/modify calls."""

    @patch.object(simple_bt_service, 'time')
    @patch.object(nmcli_utils, '_run_nmcli')
    def test_add_new_network_with_password(self, mock_run, mock_time):
        """Adding a brand-new office network should use 'device wifi connect'."""
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((' '.join(cmd_list), kwargs))
            cmd = ' '.join(cmd_list)
            # connection exists check
            if '-g' in cmd_list and 'NAME' in cmd and 'connection' in cmd:
                return (True, '\n', None)  # no existing connections
            # device wifi connect
            if 'device' in cmd and 'wifi' in cmd and 'connect' in cmd:
                return (True, 'connected', None)
            # modify priority
            if 'connection' in cmd and 'modify' in cmd:
                return (True, '', None)
            # IP checks for _trigger_service_restart
            if 'DEVICE,TYPE,STATE' in cmd:
                return (True, 'wlP1p1s0:wifi:connected\n', None)
            if 'IP4.ADDRESS' in cmd:
                return (True, 'IP4.ADDRESS:192.168.1.100/24\n', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {
            'command': 'update_network',
            'data': {'ssid': 'OfficeNet', 'password': 'secret123', 'priority': 20}
        })

        assert resp['status'] == 'success'

        # Should have called 'nmcli device wifi connect OfficeNet ...'
        connect_cmds = [c for c, kw in captured_cmds
                        if 'device wifi connect' in c and 'OfficeNet' in c]
        assert len(connect_cmds) >= 1
        assert 'password' in connect_cmds[0]
        assert 'secret123' in connect_cmds[0]

    @patch.object(simple_bt_service, 'time')
    @patch.object(nmcli_utils, '_run_nmcli')
    def test_update_existing_network_priority_only(self, mock_run, mock_time):
        """Updating priority of an existing profile should modify, not recreate."""
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((' '.join(cmd_list), kwargs))
            cmd = ' '.join(cmd_list)
            if '-g' in cmd_list and 'NAME' in cmd and 'connection' in cmd:
                return (True, 'FBI_VAN_6\n', None)  # exists
            if 'connection' in cmd and 'modify' in cmd:
                return (True, '', None)
            if 'connection' in cmd and 'up' in cmd:
                return (True, 'activated', None)
            if 'DEVICE,TYPE,STATE' in cmd:
                return (True, 'wlP1p1s0:wifi:connected\n', None)
            if 'IP4.ADDRESS' in cmd:
                return (True, 'IP4.ADDRESS:192.168.1.42/24\n', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {
            'command': 'update_network',
            'data': {'ssid': 'FBI_VAN_6', 'priority': 50}
        })

        assert resp['status'] == 'success'

        # Should NOT have called 'device wifi connect' — just modify + up
        connect_cmds = [c for c, _ in captured_cmds if 'device wifi connect' in c]
        assert len(connect_cmds) == 0

        modify_cmds = [c for c, _ in captured_cmds if 'connection modify' in c]
        assert len(modify_cmds) >= 1
        assert 'autoconnect-priority' in modify_cmds[0]
        assert '50' in modify_cmds[0]


# ---------------------------------------------------------------------------
# remove_network
# ---------------------------------------------------------------------------

class TestRemoveNetwork:
    """Verify remove_network dispatches nmcli connection delete with sudo."""

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_remove_existing_network(self, mock_run):
        def side_effect(cmd_list, **kwargs):
            cmd = ' '.join(cmd_list)
            if '-g' in cmd_list and 'NAME' in cmd:
                return (True, 'OldNetwork\nFBI_VAN_6\n', None)
            if 'connection' in cmd and 'delete' in cmd:
                return (True, '', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {
            'command': 'remove_network',
            'data': {'ssid': 'OldNetwork'}
        })

        assert resp['status'] == 'success'
        delete_calls = [c for c in mock_run.call_args_list
                        if 'delete' in ' '.join(c[0][0])]
        assert len(delete_calls) == 1
        assert delete_calls[0][1].get('use_sudo') is True

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_remove_nonexistent_network(self, mock_run):
        mock_run.return_value = (True, 'FBI_VAN_6\n', None)
        server = _make_server()

        resp = _send_command(server, {
            'command': 'remove_network',
            'data': {'ssid': 'DoesNotExist'}
        })

        assert resp['status'] == 'error'
        assert 'not found' in resp['message']


# ---------------------------------------------------------------------------
# connect_network
# ---------------------------------------------------------------------------

class TestConnectNetwork:
    """Verify connect_network scans then activates with sudo."""

    @patch.object(simple_bt_service, 'time')
    @patch.object(nmcli_utils, '_run_nmcli')
    def test_connect_to_visible_network(self, mock_run, mock_time):
        captured_cmds = []

        def side_effect(cmd_list, **kwargs):
            captured_cmds.append((' '.join(cmd_list), kwargs))
            cmd = ' '.join(cmd_list)
            # scan_for_ssid -> scan_for_visible_ssids
            if 'wifi' in cmd and 'list' in cmd and 'rescan' in cmd:
                return (True, 'FBI_VAN_6\nOfficeNet\n', None)
            # nmcli connection up
            if 'connection' in cmd and 'up' in cmd:
                return (True, 'activated', None)
            # IP checks
            if 'DEVICE,TYPE,STATE' in cmd:
                return (True, 'wlP1p1s0:wifi:connected\n', None)
            if 'IP4.ADDRESS' in cmd:
                return (True, 'IP4.ADDRESS:192.168.1.42/24\n', None)
            return (True, '', None)

        mock_run.side_effect = side_effect
        server = _make_server()

        resp = _send_command(server, {
            'command': 'connect_network',
            'data': {'ssid': 'FBI_VAN_6'}
        })

        assert resp['status'] == 'success'

        # Should have called connection up with sudo
        up_calls = [kw for c, kw in captured_cmds if 'connection up' in c]
        assert len(up_calls) >= 1
        assert up_calls[0].get('use_sudo') is True


# ---------------------------------------------------------------------------
# Unknown / malformed commands
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Verify bad input is handled gracefully."""

    def test_unknown_command(self):
        server = _make_server()
        resp = _send_command(server, {'command': 'reboot_robot'})
        assert resp['status'] == 'error'
        assert resp['command'] == 'reboot_robot'

    def test_missing_command_field(self):
        server = _make_server()
        resp = _send_command(server, {'foo': 'bar'})
        assert resp['status'] == 'error'

    def test_invalid_json(self):
        server = _make_server()
        raw = b'this is not json'
        result_bytes = server.write_callback(list(raw))
        resp = json.loads(bytes(result_bytes).decode('utf-8'))
        assert resp['status'] == 'error'
        assert 'JSON' in resp['message']

    @patch.object(nmcli_utils, '_run_nmcli')
    def test_update_network_missing_ssid(self, mock_run):
        mock_run.return_value = (True, '', None)
        server = _make_server()
        resp = _send_command(server, {
            'command': 'update_network',
            'data': {'password': 'noSSID'}
        })
        assert resp['status'] == 'error'
        assert 'SSID' in resp['message']


# ---------------------------------------------------------------------------
# nmcli command argument verification
# ---------------------------------------------------------------------------

class TestNmcliCommandShape:
    """
    Verify that the nmcli commands built by nmcli_utils are structurally
    correct — right subcommands, flags, and field selections.
    """

    @patch('nmcli_utils.subprocess')
    def test_rescan_command_shape(self, mock_subprocess):
        """scan_for_visible_ssids should build a well-formed list --rescan cmd."""
        mock_result = MagicMock()
        mock_result.stdout = 'TestNet\n'
        mock_result.stderr = ''
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        nmcli_utils.nmcli_scan_for_visible_ssids()

        call_args = mock_subprocess.run.call_args[0][0]
        # Should have sudo prefix
        assert call_args[0] == 'sudo'
        # Core command structure
        assert 'nmcli' in call_args
        assert 'device' in call_args
        assert 'wifi' in call_args
        assert 'list' in call_args
        assert '--rescan' in call_args
        assert 'yes' in call_args
        # Terse SSID-only output
        assert '-t' in call_args
        ssid_idx = call_args.index('-f')
        assert call_args[ssid_idx + 1] == 'SSID'

    @patch('nmcli_utils.subprocess')
    def test_scan_for_ssid_delegates(self, mock_subprocess):
        """scan_for_ssid should call scan_for_visible_ssids (single scan path)."""
        mock_result = MagicMock()
        mock_result.stdout = 'FBI_VAN_6\nOtherNet\n'
        mock_result.stderr = ''
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        success, found, err = nmcli_utils.nmcli_scan_for_ssid('FBI_VAN_6')

        assert success is True
        assert found is True
        # Only one subprocess call — no separate rescan
        assert mock_subprocess.run.call_count == 1

    @patch('nmcli_utils.subprocess')
    def test_delete_connection_uses_sudo(self, mock_subprocess):
        """delete_connection should run with sudo."""
        mock_result = MagicMock()
        mock_result.stdout = 'TargetNet\n'
        mock_result.stderr = ''
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        nmcli_utils.nmcli_delete_connection('TargetNet')

        # Find the delete call (second call — first is the existence check)
        calls = mock_subprocess.run.call_args_list
        delete_calls = [c for c in calls if 'delete' in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][0][0] == 'sudo'
