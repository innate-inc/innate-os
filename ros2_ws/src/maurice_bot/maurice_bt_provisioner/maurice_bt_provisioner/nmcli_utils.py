import logging
import subprocess
import time # Added import

# Dedicated logger for NM interactions
nm_logger = logging.getLogger('NetworkManager') 

# Default WiFi interface name for the target hardware (e.g., Jetson Nano)
# TODO: Use a config file to set this
DEFAULT_WIFI_INTERFACE = 'wlP1p1s0'

# --- NetworkManager Utility Functions ---

def _run_nmcli(command_list, timeout=10, check=True, capture_output=True, use_sudo=False):
    """Runs an nmcli command using subprocess, handling common errors.
    
    Args:
        command_list: List of command arguments (e.g., ['nmcli', 'connection', 'show'])
        timeout: Command timeout in seconds
        check: Whether to raise exception on non-zero return code
        capture_output: Whether to capture stdout/stderr
        use_sudo: Whether to prefix the command with 'sudo'
    """
    if use_sudo:
        # Prepend 'sudo' to the command list
        command_list = ['sudo'] + command_list
    
    nm_logger.debug(f"Running nmcli command: {' '.join(command_list)}")
    try:
        result = subprocess.run(
            command_list, 
            capture_output=capture_output, 
            text=True, 
            check=check, 
            timeout=timeout
        )
        nm_logger.debug(f"nmcli stdout: {result.stdout.strip() if result.stdout else 'N/A'}")
        if result.stderr:
            nm_logger.debug(f"nmcli stderr: {result.stderr.strip()}")
        return True, result.stdout, result.stderr # Success
    except subprocess.CalledProcessError as e:
        nm_logger.error(f"nmcli command failed: {' '.join(command_list)}")
        nm_logger.error(f"  Return Code: {e.returncode}")
        nm_logger.error(f"  Stderr: {e.stderr.strip() if e.stderr else 'N/A'}")
        return False, e.stdout, e.stderr # Failed execution
    except subprocess.TimeoutExpired as e:
        nm_logger.error(f"nmcli command timed out: {' '.join(command_list)}")
        return False, None, "Command timed out." # Timeout
    except FileNotFoundError:
        nm_logger.error("nmcli command not found. Is NetworkManager installed and in PATH?")
        return False, None, "nmcli command not found." # Not found
    except Exception as e:
        nm_logger.error(f"Unexpected error running nmcli: {e}", exc_info=True)
        return False, None, f"Unexpected error: {str(e)}" # Other error

def nmcli_get_wifi_connections():
    """Retrieves a list of configured Wi-Fi connections with their priorities."""
    # Step 1: Get Name, Type, and UUID of all connections
    success_list, stdout_list, stderr_list = _run_nmcli(['nmcli', '-t', '-f', 'NAME,TYPE,UUID', 'connection', 'show'])
    if not success_list:
        return False, [], f"Failed to retrieve connection list: {stderr_list or 'Unknown error'}"
    
    networks = []
    wifi_connections_to_query = [] # Store (name, uuid) tuples

    try:
        list_lines = stdout_list.strip().split('\n') if stdout_list else []
        for line in list_lines:
            if not line:
                continue
            # Split into at most 3 parts: NAME, TYPE, UUID
            # UUID might contain colons, so it gets the remainder
            parts = line.split(':', 2) 
            if len(parts) == 3:
                name, type, uuid = parts
                if type == '802-11-wireless':
                    wifi_connections_to_query.append((name, uuid))
            else:
                nm_logger.warning(f"Could not parse nmcli connection list line: {line}")
    except Exception as e:
        nm_logger.error(f"Error parsing nmcli connection list output: {e}", exc_info=True)
        return False, [], f"Error parsing connection list: {str(e)}"

    # Step 2: Query priority for each Wi-Fi connection using its UUID
    for name, uuid in wifi_connections_to_query:
        priority = 0 # Default priority if lookup fails or not set
        success_detail, stdout_detail, stderr_detail = _run_nmcli(
            ['nmcli', '-t', '-f', 'connection.autoconnect-priority', 'connection', 'show', uuid]
        )
        
        if success_detail and stdout_detail:
            try:
                # Output is 'connection.autoconnect-priority:<value>', extract value
                priority_str = stdout_detail.strip().split(':')[-1]
                priority = int(priority_str) if priority_str else 0
            except (ValueError, IndexError) as e:
                nm_logger.warning(f"Could not parse priority for connection {name} (UUID: {uuid}): {stdout_detail}. Error: {e}. Using default priority 0.")
                priority = 0 # Fallback to default
        else:
            nm_logger.warning(f"Failed to get priority for connection {name} (UUID: {uuid}). Error: {stderr_detail or 'Unknown error'}. Using default priority 0.")
            priority = 0 # Fallback to default
        
        networks.append({'ssid': name, 'priority': priority})

    # Sort networks by priority (descending) then SSID (ascending) for consistent output
    networks.sort(key=lambda x: (-x['priority'], x['ssid']))
    
    return True, networks, None

def nmcli_connection_exists(ssid):
    """Checks if a NetworkManager connection profile exists for the given SSID."""
    success, stdout, stderr = _run_nmcli(['nmcli', '-g', 'NAME', 'connection', 'show'])
    if not success:
         return False, False, f"Failed check existence: {stderr or 'Unknown error'}"
    
    existing_connections = stdout.strip().split('\n') if stdout else []
    return True, ssid in existing_connections, None
    
def nmcli_add_or_modify_connection(ssid, password, priority):
    """Adds a new Wi-Fi connection or modifies an existing one.

    If a profile already exists, updates priority in place and reconnects.
    If no profile exists (or password is provided), deletes any stale profile
    and uses 'nmcli device wifi connect' which auto-negotiates security
    (WPA2/WPA3).

    Requires elevated privileges (uses sudo).
    """
    success_check, exists, err_check = nmcli_connection_exists(ssid)

    if not success_check:
        return False, f"Failed to check if connection exists: {err_check}"

    # If profile exists and no new password, just update priority and reconnect
    if exists and not password:
        nm_logger.info(f"Profile exists for {ssid}, updating priority")
        _run_nmcli([
            'nmcli', 'connection', 'modify', ssid,
            'connection.autoconnect', 'yes',
            'connection.autoconnect-priority', str(priority),
            'connection.interface-name', DEFAULT_WIFI_INTERFACE,
        ], use_sudo=True)
        success, _, stderr = _run_nmcli(
            ['nmcli', 'connection', 'up', ssid, 'ifname', DEFAULT_WIFI_INTERFACE],
            timeout=30, use_sudo=True,
        )
        if not success:
            return False, f"Failed to activate '{ssid}': {stderr or 'Unknown error'}"
        return True, None

    # New network or password change — delete stale profile and let nmcli
    # auto-negotiate security (WPA2/WPA3/open)
    if exists:
        nm_logger.info(f"Removing existing profile for {ssid} (password change)")
        success_del, _, stderr_del = _run_nmcli(
            ['nmcli', 'connection', 'delete', ssid], use_sudo=True
        )
        if not success_del:
            nm_logger.warning(f"Failed to remove old profile for '{ssid}': {stderr_del}")

    cmd = ['nmcli', 'device', 'wifi', 'connect', ssid, 'ifname', DEFAULT_WIFI_INTERFACE]
    if password:
        cmd.extend(['password', password])

    success, _, stderr = _run_nmcli(cmd, timeout=30, use_sudo=True)
    if not success:
        return False, f"Failed to connect to '{ssid}': {stderr or 'Unknown error'}"

    # Set autoconnect priority on the created profile
    success_mod, _, stderr_mod = _run_nmcli([
        'nmcli', 'connection', 'modify', ssid,
        'connection.autoconnect', 'yes',
        'connection.autoconnect-priority', str(priority),
        'connection.interface-name', DEFAULT_WIFI_INTERFACE,
    ], use_sudo=True)
    if not success_mod:
        nm_logger.warning(f"Connected but failed to set priority: {stderr_mod}")

    return True, None

def nmcli_delete_connection(ssid):
    """Deletes a Wi-Fi connection profile.
    
    Requires elevated privileges (uses sudo).
    """
    success_check, exists, err_check = nmcli_connection_exists(ssid)
    if not success_check:
        return False, f"Failed to check if connection exists before deletion: {err_check}"
    if not exists:
         return False, f"Network profile '{ssid}' not found."
    
    # Use sudo for privileged operations
    success, _, stderr = _run_nmcli(['nmcli', 'connection', 'delete', ssid], use_sudo=True)
    if not success:
        return False, f"Failed to delete connection '{ssid}': {stderr or 'Unknown error'}"
    return True, None # Success

def nmcli_scan_for_ssid(target_ssid):
    """Performs a Wi-Fi scan and checks if the target SSID is visible."""
    success, visible_ssids, err = nmcli_scan_for_visible_ssids()
    if not success:
        return False, False, err
    return True, target_ssid in visible_ssids, None


def nmcli_connect(ssid, ifname=DEFAULT_WIFI_INTERFACE):
    """Attempts to activate (connect to) a given network profile.

    Uses the DEFAULT_WIFI_INTERFACE constant if no interface is specified.
    Requires elevated privileges (uses sudo).

    Args:
        ssid (str): The name of the connection profile (SSID).
        ifname (str, optional): The specific interface name to use. 
                                Defaults to DEFAULT_WIFI_INTERFACE.
    """
    # Use the provided or default interface name
    target_interface = ifname if ifname else DEFAULT_WIFI_INTERFACE 
    
    nm_logger.info(f"Attempting to connect to network profile: {ssid}{f' on interface {target_interface}' if target_interface else ''}")
    
    # Base command - use sudo for privileged operations
    cmd = ['nmcli', 'connection', 'up', ssid] 
    
    # Add ifname if specified (or defaulted)
    if target_interface:
        cmd.extend(['ifname', target_interface])
        
    # Use sudo for privileged operations
    success, stdout, stderr = _run_nmcli(
        cmd, # Use the constructed command list
        timeout=30,
        use_sudo=True
    )
    if not success:
         # Provide more context in the error
         error_iface_part = f' on interface {target_interface}' if target_interface else ''
         return False, f"Connection attempt for '{ssid}'{error_iface_part} failed: {stderr or 'Unknown error'}"
    
    # Check stdout for confirmation message (optional, but can be useful)
    confirmation = stdout.strip() if stdout else "No output."
    nm_logger.info(f"Connection attempt output: {confirmation}")
    return True, confirmation # Success, return confirmation message

def nmcli_get_active_wifi_ssid():
    """Gets the SSID of the currently active Wi-Fi connection."""
    # Use 'nmcli dev wifi list' as it directly shows active state
    success, stdout, stderr = _run_nmcli(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi', 'list'])
    if not success:
        nm_logger.error(f"Failed to get active Wi-Fi status: {stderr or 'Unknown error'}")
        return None # Indicate error or inability to determine

    try:
        lines = stdout.strip().split('\n') if stdout else []
        for line in lines:
            if not line:
                continue
            parts = line.split(':')
            if len(parts) == 2:
                active, ssid = parts
                # ACTIVE field is 'yes' or 'no' in terse mode
                if active.lower() == 'yes':
                    nm_logger.info(f"Currently active Wi-Fi network: {ssid}")
                    return ssid
            else:
                 nm_logger.warning(f"Could not parse nmcli dev wifi list line: {line}")
        
        # If loop finishes without finding an active network
        nm_logger.info("No active Wi-Fi network found.")
        return None
    except Exception as e:
        nm_logger.error(f"Error parsing nmcli dev wifi list output: {e}", exc_info=True)
        return None # Indicate error

def nmcli_scan_for_visible_ssids(timeout=15):
    """Performs a Wi-Fi scan and returns a list of visible SSIDs."""
    nm_logger.info("Triggering Wi-Fi rescan...")
    nm_logger.info("Scanning and listing visible Wi-Fi networks...")
    success_list, stdout_list, stderr_list = _run_nmcli(
        ['nmcli', '-t', '-f', 'SSID', 'device', 'wifi', 'list', '--rescan', 'yes'],
        timeout=timeout,
        use_sudo=True
    )
    nm_logger.info(f"nmcli wifi list stdout: {stdout_list.strip() if stdout_list else 'N/A'}")
    if not success_list:
        nm_logger.error(f"Wi-Fi list after rescan failed: {stderr_list or 'Unknown error'}")
        return False, [], f"Wi-Fi list failed: {stderr_list or 'Unknown error'}"
    
    visible_ssids = []
    try:
        # Use stdout_list from the second command
        lines = stdout_list.strip().split('\n') if stdout_list else [] 
        # Filter out empty strings which can occur if a network has no SSID
        # Also remove duplicates immediately
        visible_ssids = list(set([ssid for ssid in lines if ssid])) 
        nm_logger.info(f"Visible SSIDs found: {visible_ssids}")
    except Exception as e:
        nm_logger.error(f"Error parsing scan results: {e}", exc_info=True)
        return False, [], f"Error parsing scan results: {str(e)}"
        
    return True, visible_ssids, None # Success, list of SSIDs, no error message

def nmcli_get_active_ipv4_address():
    """Gets the IPv4 address of the currently active network connection device."""
    # Step 1: Find the active device
    success_dev, stdout_dev, stderr_dev = _run_nmcli([
        'nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'
    ])
    if not success_dev:
        nm_logger.error(f"Failed to get device status: {stderr_dev or 'Unknown error'}")
        return None
    
    active_device = None
    try:
        lines = stdout_dev.strip().split('\n') if stdout_dev else []
        for line in lines:
            if not line:
                continue
            parts = line.split(':')
            if len(parts) == 3:
                device, type, state = parts
                # Look for a connected Wi-Fi or Ethernet device
                if state.lower() == 'connected' and ('wifi' in type.lower() or 'ethernet' in type.lower()):
                    active_device = device
                    nm_logger.info(f"Found active device: {active_device} (Type: {type})")
                    break # Found the first connected device
            else:
                nm_logger.warning(f"Could not parse nmcli device status line: {line}")
    except Exception as e:
        nm_logger.error(f"Error parsing nmcli device status output: {e}", exc_info=True)
        return None

    if not active_device:
        nm_logger.info("No active connected network device found.")
        return None

    # Step 2: Get the IP address for the active device
    success_ip, stdout_ip, stderr_ip = _run_nmcli([
        'nmcli', '-t', '-f', 'IP4.ADDRESS', 'device', 'show', active_device
    ])
    if not success_ip:
        nm_logger.error(f"Failed to get IP address for device {active_device}: {stderr_ip or 'Unknown error'}")
        return None

    ipv4_address = None
    try:
        ip_output = stdout_ip.strip()
        if ip_output:
            # Output is 'IP4.ADDRESS:<ip>/<prefixlen>', extract just the IP
            # Handle cases with multiple IPs (take the first one) and potential lack of prefix
            address_part = ip_output.split(':')[-1]
            if '/' in address_part:
                 ipv4_address = address_part.split('/')[0]
            else:
                 ipv4_address = address_part # No prefix found
            nm_logger.info(f"Found IPv4 address for {active_device}: {ipv4_address}")
        else:
            nm_logger.info(f"No IPv4 address found for active device {active_device}.")

    except Exception as e:
        nm_logger.error(f"Error parsing IP address output for {active_device}: {e}", exc_info=True)
        return None

    return ipv4_address 