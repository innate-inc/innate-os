import logging
import subprocess

# Dedicated logger for NM interactions
nm_logger = logging.getLogger('NetworkManager') 

# --- NetworkManager Utility Functions ---

def _run_nmcli(command_list, timeout=10, check=True, capture_output=True):
    """Runs an nmcli command using subprocess, handling common errors."""
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
        list_lines = stdout_list.strip().split('\\n') if stdout_list else []
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
            ['nmcli', '-t', '-f', 'connection.priority', 'connection', 'show', uuid]
        )
        
        if success_detail and stdout_detail:
            try:
                # Output is 'connection.priority:<value>', extract value
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
    
    existing_connections = stdout.strip().split('\\n') if stdout else []
    return True, ssid in existing_connections, None
    
def nmcli_add_or_modify_connection(ssid, password, priority):
    """Adds a new Wi-Fi connection or modifies an existing one."""
    success_check, exists, err_check = nmcli_connection_exists(ssid)
    if not success_check:
        return False, f"Failed to check if connection exists: {err_check}"

    base_cmd = ['nmcli', 'connection']
    if exists:
        nm_logger.info(f"Modifying existing connection: {ssid}")
        cmd = base_cmd + ['modify', ssid]
    else:
        nm_logger.info(f"Adding new connection: {ssid}")
        # Specify a default interface name like wlan0, adjust if necessary
        cmd = base_cmd + ['add', 'type', 'wifi', 'con-name', ssid, 'ifname', 'wlan0', 'ssid', ssid]
    
    # Common settings
    cmd.extend(['connection.autoconnect', 'yes', 'connection.priority', str(priority)])

    # Security settings
    if password:
        cmd.extend(['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password])
    else: # Open network
        cmd.extend(['wifi-sec.key-mgmt', 'none'])
    
    success, _, stderr = _run_nmcli(cmd)
    if not success:
         action = "modify" if exists else "add"
         return False, f"Failed to {action} connection '{ssid}': {stderr or 'Unknown error'}"
    return True, None # Success

def nmcli_delete_connection(ssid):
    """Deletes a Wi-Fi connection profile."""
    success_check, exists, err_check = nmcli_connection_exists(ssid)
    if not success_check:
        return False, f"Failed to check if connection exists before deletion: {err_check}"
    if not exists:
         return False, f"Network profile '{ssid}' not found."
    
    success, _, stderr = _run_nmcli(['nmcli', 'connection', 'delete', ssid])
    if not success:
        return False, f"Failed to delete connection '{ssid}': {stderr or 'Unknown error'}"
    return True, None # Success

def nmcli_scan_for_ssid(target_ssid):
    """Performs a Wi-Fi scan and checks if the target SSID is visible."""
    nm_logger.info("Performing Wi-Fi scan...")
    success, stdout, stderr = _run_nmcli(
        ['nmcli', '--terse', '--fields', 'SSID', 'device', 'wifi', 'list', '--rescan', 'yes'],
        timeout=15
    )
    if not success:
        return False, False, f"Wi-Fi scan failed: {stderr or 'Unknown error'}"
    
    visible_ssids = stdout.strip().split('\\n') if stdout else []
    nm_logger.info(f"Visible SSIDs: {visible_ssids}")
    return True, target_ssid in visible_ssids, None

def nmcli_connect(ssid):
    """Attempts to activate (connect to) a given network profile."""
    nm_logger.info(f"Attempting to connect to network: {ssid}")
    success, stdout, stderr = _run_nmcli(
        ['nmcli', 'connection', 'up', ssid],
        timeout=30
    )
    if not success:
         return False, f"Connection attempt failed: {stderr or 'Unknown error'}"
    
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
        lines = stdout.strip().split('\\n') if stdout else []
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