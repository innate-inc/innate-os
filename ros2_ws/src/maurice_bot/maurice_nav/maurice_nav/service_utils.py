"""Utilities for calling ROS2 services with timeout handling."""

import time
from lifecycle_msgs.msg import Transition
from lifecycle_msgs.srv import GetState, ChangeState


def call_service(service_clients: dict, logger, service_name: str, request, timeout_sec: float = 2.0):
    """
    Generic helper to call any ROS2 service with timeout.
    Uses pre-created clients from the dictionary.
    Args:
        service_clients: Dictionary of service clients
        logger: ROS2 logger instance
        service_name: Full service name (e.g., '/node_name/get_state')
        request: The service request object
        timeout_sec: Timeout for both service availability and response
    Returns:
        The service response if successful, None otherwise
    """
    client = service_clients[service_name]
    
    try:
        if not client.wait_for_service(timeout_sec=timeout_sec):
            logger.info(f"Service '{service_name}' not available")
            return None
        
        # result = client.call(request)
        future = client.call_async(request)
        start_time = time.time()
        while not future.done():
            # logger.info(f"Service not done yet")
            elapsed = time.time() - start_time
            if elapsed >= timeout_sec:
                logger.warning(f"Timeout waiting for service '{service_name}' after {elapsed:.2f}s")
                return None
            time.sleep(.1)
        
        if future.done():
            result = future.result()
            if result is not None:
                return result
            else:
                logger.info(f"Service '{service_name}' returned None")
                return None
        else:
            logger.info(f"Timeout calling service '{service_name}'")
            return None
            
    except Exception as e:
        logger.warning(f"Exception calling service '{service_name}': {e}")
        return None


def get_node_state(service_clients: dict, logger, node_name: str) -> int:
    """Helper to get the current state of a node. Returns state ID or None if failed."""
    get_state_request = GetState.Request()
    get_state_result = call_service(
        service_clients,
        logger,
        f'/{node_name}/get_state',
        get_state_request,
        timeout_sec=5.0
    )
    
    if get_state_result is None:
        return None
    
    return get_state_result.current_state.id


def send_lifecycle_transition(service_clients: dict, logger, node_name: str, transition_id: int) -> bool:
    """Helper to send a single lifecycle transition to a node."""
    change_state_request = ChangeState.Request()
    change_state_request.transition = Transition()
    change_state_request.transition.id = transition_id
    
    result = call_service(
        service_clients,
        logger,
        f'/{node_name}/change_state',
        change_state_request,
        timeout_sec=8.0
    )
    
    return result is not None and result.success


def transition_node(service_clients: dict, logger, node_name: str, target_state: int, only_up: bool = False) -> bool:
    """
    Transition a node to a target state (ACTIVE, INACTIVE, or UNCONFIGURED).
    Intelligently transitions up or down as needed.
    Args:
        service_clients: Dictionary of service clients
        logger: ROS2 logger instance
        node_name: Name of the node to transition
        target_state: Target state ID (State.PRIMARY_STATE_ACTIVE, INACTIVE, or UNCONFIGURED)
        only_up: If True, only allow upward transitions (return True if already at or above target)
    Returns: True if transition succeeded, False otherwise
    """
    from lifecycle_msgs.msg import State
    
    try:
        # Get current state
        current_state = get_node_state(service_clients, logger, node_name)
        
        if current_state is None:
            logger.warning(f"Failed to get state for {node_name}")
            return False
        
        # Already at target state
        if current_state == target_state:
            logger.debug(f"{node_name} already at target state {target_state}")
            return True
        
        # Transition down (ACTIVE -> INACTIVE -> UNCONFIGURED)
        if current_state > target_state:
            # If only_up is True, don't transition down - just return True (already "higher" than target)
            if only_up:
                logger.debug(f"{node_name} at state {current_state}, skipping downward transition (only_up=True)")
                return True
            # Deactivate if needed
            if current_state == State.PRIMARY_STATE_ACTIVE:
                if not send_lifecycle_transition(service_clients, logger, node_name, Transition.TRANSITION_DEACTIVATE):
                    logger.warning(f"Failed to deactivate {node_name}")
                    return False
                
                current_state = State.PRIMARY_STATE_INACTIVE
                if current_state == target_state:
                    return True
            
            # Cleanup if needed
            if current_state == State.PRIMARY_STATE_INACTIVE and target_state == State.PRIMARY_STATE_UNCONFIGURED:
                if not send_lifecycle_transition(service_clients, logger, node_name, Transition.TRANSITION_CLEANUP):
                    logger.warning(f"Failed to cleanup {node_name}")
                    return False
                
                return True
        
        # Transition up (UNCONFIGURED -> INACTIVE -> ACTIVE)
        else:
            # Configure if needed
            if current_state == State.PRIMARY_STATE_UNCONFIGURED:
                if not send_lifecycle_transition(service_clients, logger, node_name, Transition.TRANSITION_CONFIGURE):
                    logger.warning(f"Failed to configure {node_name}")
                    return False
                
                current_state = State.PRIMARY_STATE_INACTIVE
                if current_state == target_state:
                    return True
            
            # Activate if needed
            if current_state == State.PRIMARY_STATE_INACTIVE and target_state == State.PRIMARY_STATE_ACTIVE:
                if not send_lifecycle_transition(service_clients, logger, node_name, Transition.TRANSITION_ACTIVATE):
                    logger.warning(f"Failed to activate {node_name}")
                    return False
                
                return True
        
        return True
        
    except Exception as e:
        logger.warning(f"Error transitioning {node_name}: {e}")
        return False
