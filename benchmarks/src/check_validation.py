#!/usr/bin/env python3
import time
import requests
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from .vlm_utils import get_representative_frames, evaluate_with_vlm


def get_robot_position(
    base_url: str = "http://localhost:8000",
) -> Optional[Tuple[float, float]]:
    """
    Get the current 2D position (x, y) of the robot from the API.

    Args:
        base_url: The base URL for the API

    Returns:
        Tuple containing (x, y) position or None if position cannot be retrieved
    """
    try:
        response = requests.get(f"{base_url}/get_robot_position")
        position_data = response.json()

        # Extract x and y coordinates
        if "position" in position_data and len(position_data["position"]) >= 2:
            return (position_data["position"][0], position_data["position"][1])

        return None
    except Exception as e:
        print(f"Error getting robot position: {e}")
        return None


def validate_location_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Validate if the robot was within the specified 2D location bounding box
    at any point during the benchmark.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data containing coordinates [x1, y1, x2, y2]
        **kwargs: Additional context data including position_history

    Returns:
        True if the robot was in the specified area at any point, False otherwise
    """
    # Get coordinates from check data
    coordinates = check_data.get("coordinates")
    if not coordinates or len(coordinates) != 4:
        print(f"Error: Invalid coordinates format for location check '{check_id}'")
        print("Expected format: [x1, y1, x2, y2] defining a 2D bounding box")
        return False

    # Parse the coordinates for the bounding box
    x1, y1, x2, y2 = coordinates

    # Ensure x1 <= x2 and y1 <= y2
    x_min, x_max = min(x1, x2), max(x1, x2)
    y_min, y_max = min(y1, y2), max(y1, y2)

    # Get position history
    position_history = kwargs.get("position_history", [])
    if not position_history:
        print(f"Warning: No position history available for check '{check_id}'")

        # Fallback to current position if no history
        base_url = kwargs.get("base_url", "http://localhost:8000")
        current_position = get_robot_position(base_url)
        if current_position:
            position_history = [current_position]
        else:
            return False

    # Check if robot was within the bounding box at any point
    for position in position_history:
        x, y = position
        if (x_min <= x <= x_max) and (y_min <= y <= y_max):
            print(
                f"Location check '{check_id}' PASSED: Robot was at ({x}, {y}), "
                f"within bounds ({x_min}, {y_min}) to ({x_max}, {y_max})"
            )
            return True

    # If we get here, the robot was never in the bounding box
    print(
        f"Location check '{check_id}' FAILED: Robot never entered "
        f"bounds ({x_min}, {y_min}) to ({x_max}, {y_max})"
    )
    return False


def validate_primitive_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Simply check if the specified primitive was called in the chat logs.
    No argument validation - just verify the primitive appears in the chat history.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        **kwargs: Additional context data including chat_log

    Returns:
        True if the primitive was called, False otherwise
    """
    primitive_name = check_data.get("primitive_name")
    if not primitive_name:
        print(f"Error: No primitive name specified for check '{check_id}'")
        return False

    # Get chat log from kwargs
    chat_log = kwargs.get("chat_log", [])
    if not chat_log:
        print(f"Warning: No chat log available for check '{check_id}'")
        return False

    # Look for the primitive in vision_agent_output messages
    for message in chat_log:
        sender = message.get("sender", "")
        text = message.get("text", "")

        # Check if this is a vision agent output message
        if sender == "vision_agent_output" and text:
            try:
                # Parse the JSON content
                agent_output = json.loads(text)

                # Check if there's a next_task with the specified primitive type
                next_task = agent_output.get("next_task", {})
                if next_task and next_task.get("type") == primitive_name:
                    print(
                        f"Primitive check '{check_id}' PASSED: Found '{primitive_name}'"
                    )
                    return True
            except json.JSONDecodeError:
                # If the text isn't valid JSON, just skip this message
                continue

    print(
        f"Primitive check '{check_id}' FAILED: '{primitive_name}' not found in chat log"
    )
    return False


def validate_compound_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Validate if a primitive was called while in a specific location.
    This is a placeholder for future implementation.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        **kwargs: Additional context data

    Returns:
        True if the check passes, False otherwise
    """
    # TODO: Implement compound validation logic
    print(f"Compound check '{check_id}' - Not implemented yet")
    return False


def validate_sequence_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Validate if a sequence of checks occurred in the specified order.
    This is a placeholder for future implementation.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        **kwargs: Additional context data

    Returns:
        True if the check passes, False otherwise
    """
    # TODO: Implement sequence validation logic
    print(f"Sequence check '{check_id}' - Not implemented yet")
    return False


def validate_vlm_check(
    check_id: str,
    check_data: Dict,
    first_person_dir: Path,
    chase_dir: Path,
    chat_log: List,
    messages: List,
    metrics: Dict,
    save_metrics_callback: callable,
) -> bool:
    """
    Use a VLM to verify a specific aspect of the robot's behavior.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        first_person_dir: Directory containing first-person view frames
        chase_dir: Directory containing chase view frames
        chat_log: List of chat messages
        messages: List of scheduled messages
        metrics: Benchmark metrics with timestamps
        save_metrics_callback: Callback function to save metrics

    Returns:
        True if the check passes, False otherwise
    """
    # Get verification prompt from check data
    verification_prompt = check_data.get("verification_prompt")
    if not verification_prompt:
        print(f"Error: No verification prompt for VLM check '{check_id}'")
        return False

    # Get time-related context for this check
    context = f"Check ID: {check_id}\n"
    if "description" in check_data:
        context += f"Description: {check_data['description']}\n"

    # Add timing information
    current_time = time.time()
    benchmark_running_time = current_time - metrics["start_timestamp"]
    context += (
        f"Current benchmark running time: {round(benchmark_running_time, 2)} seconds\n"
    )

    # Add related message information if any messages are triggered by this check
    related_messages = []
    for message in messages:
        if (
            message.get("trigger_type") == "check"
            and message.get("check_id") == check_id
        ):
            related_messages.append(message)

    if related_messages:
        context += "Related messages that will be triggered when this check passes:\n"
        for i, msg in enumerate(related_messages):
            delay = msg.get("delay", 0)
            text = msg.get("text", "")
            context += f"  Message {i+1}: '{text}' (delay: {delay}s)\n"

    # Enhanced verification prompt with context
    enhanced_prompt = f"{context}\nVerification task: {verification_prompt}"

    # Get representative frames for analysis
    frames = get_representative_frames(first_person_dir, chase_dir)
    if not frames:
        print(f"Warning: No frames available for VLM check '{check_id}'")
        return False

    # Evaluate using VLM
    try:
        result = evaluate_with_vlm(
            enhanced_prompt,
            frames,
            chat_log=chat_log,
            metrics=metrics,
            is_stop_check=False,
        )

        # Log the result
        passed = result.get("success", False)
        reason = result.get("reason", "No reason provided")

        if passed:
            print(f"Check '{check_id}' passed: {reason}")
        else:
            print(f"Check '{check_id}' failed: {reason}")

        # Store the result in metrics
        if "check_results" not in metrics:
            metrics["check_results"] = {}

        metrics["check_results"][check_id] = {
            "passed": passed,
            "time": time.time() - metrics["start_timestamp"],
            "reason": reason,
        }
        save_metrics_callback()  # Save metrics via callback

        return passed

    except Exception as e:
        print(f"Error in VLM check '{check_id}': {e}")
        return False


def validate_checks(
    expectations: Dict,
    check_status: Dict,
    first_person_dir: Path,
    chase_dir: Path,
    chat_log: List,
    messages: List,
    metrics: Dict,
    save_metrics_callback: callable,
    base_url: str = "http://localhost:8000",
    position_history: List[Tuple[float, float]] = None,
) -> Dict[str, bool]:
    """
    Validate all checks in the configuration and return the updated status.
    This is only called at the end of the benchmark run.

    Args:
        expectations: Benchmark expectations configuration
        check_status: Current status of checks
        first_person_dir: Directory with first-person view frames
        chase_dir: Directory with chase view frames
        chat_log: List of chat messages
        messages: List of scheduled messages
        metrics: Benchmark metrics
        save_metrics_callback: Function to save metrics
        base_url: Base URL for the API
        position_history: List of robot positions recorded during the benchmark

    Returns:
        Updated check status dictionary
    """
    if not expectations.get("checks"):
        return check_status

    print("Validating all checks at the end of benchmark run...")
    updated_status = check_status.copy()

    # Common kwargs to pass to all validation functions
    validation_kwargs = {
        "chat_log": chat_log,
        "base_url": base_url,
        "first_person_dir": first_person_dir,
        "chase_dir": chase_dir,
        "messages": messages,
        "metrics": metrics,
        "save_metrics_callback": save_metrics_callback,
        "position_history": position_history or [],
    }

    # Print summary of available data for validation
    print(f"Position history: {len(position_history or [])} points")
    print(f"Chat log: {len(chat_log)} messages")

    for check in expectations["checks"]:
        check_id = check.get("id")
        check_type = check.get("type")

        if not check_id or not check_type:
            continue

        # Skip checks that have already passed
        if updated_status.get(check_id, False):
            continue

        # Validate based on check type
        passed = False
        if check_type == "location":
            passed = validate_location_check(check_id, check, **validation_kwargs)
        elif check_type == "primitive":
            passed = validate_primitive_check(check_id, check, **validation_kwargs)
        elif check_type == "compound":
            passed = validate_compound_check(check_id, check, **validation_kwargs)
        elif check_type == "sequence":
            passed = validate_sequence_check(check_id, check, **validation_kwargs)
        elif check_type == "vlm_verification":
            # For VLM checks, we need to pass specific arguments
            passed = validate_vlm_check(
                check_id,
                check,
                first_person_dir,
                chase_dir,
                chat_log,
                messages,
                metrics,
                save_metrics_callback,
            )
        else:
            print(f"Warning: Unknown check type '{check_type}' for check '{check_id}'")
            continue

        # Update check status
        if passed:
            updated_status[check_id] = True
            # Record the time when the check passed
            if "start_timestamp" in metrics and metrics["start_timestamp"]:
                current_time = time.time()
                elapsed = current_time - metrics["start_timestamp"]
                # Store the completion time in the metrics
                if "check_completion_times" not in metrics:
                    metrics["check_completion_times"] = {}
                metrics["check_completion_times"][check_id] = elapsed
                print(f"Check '{check_id}' completed at {elapsed:.2f} seconds")
                save_metrics_callback()  # Save updated metrics

    return updated_status


def evaluate_stop_criterion(
    stop_criterion: str,
    first_person_dir: Path,
    chase_dir: Path,
    chat_log: List,
    metrics: Dict,
    save_metrics_callback: callable,
    use_frames: bool = True,
) -> bool:
    """
    Evaluate if the benchmark should stop early based on the stop criterion.

    Args:
        stop_criterion: The criterion to evaluate
        first_person_dir: Directory with first-person frames
        chase_dir: Directory with chase frames
        chat_log: List of chat messages
        metrics: Benchmark metrics
        save_metrics_callback: Function to save metrics
        use_frames: Whether to use frames in evaluation (5 most recent if True)

    Returns:
        True if benchmark should stop, False otherwise
    """
    if not stop_criterion:
        return False

    # Get frames from the benchmark so far if use_frames is True
    frames = []
    if use_frames:
        frames = get_representative_frames(first_person_dir, chase_dir)
        if not frames:
            return False

    # Evaluate the stop criterion using VLM
    result = evaluate_with_vlm(
        stop_criterion,
        frames,
        chat_log=chat_log,
        metrics=metrics,
        is_stop_check=True,
    )

    if result.get("should_stop", False):
        # Log the reason for stopping
        print(f"Stop criterion met: {result.get('reason', 'Unknown reason')}")

        # Save the stop decision to metrics
        metrics["early_stop"] = {
            "triggered": True,
            "time": time.time() - metrics["start_timestamp"],
            "reason": result.get("reason", "Unknown reason"),
        }
        save_metrics_callback()

        return True

    return False


def evaluate_final_success(
    success_criterion: str,
    first_person_dir: Path,
    chase_dir: Path,
    chat_log: List,
    metrics: Dict,
    use_frames: bool = True,
) -> Dict:
    """
    Evaluate whether the benchmark was successful based on the success criterion.

    Args:
        success_criterion: The criterion to evaluate
        first_person_dir: Directory with first-person frames
        chase_dir: Directory with chase frames
        chat_log: List of chat messages
        metrics: Benchmark metrics
        use_frames: Whether to use frames in evaluation (5 most recent if True)

    Returns:
        Dictionary with success status and reason
    """
    if not success_criterion:
        return {"success": False, "reason": "No success criterion defined"}

    # Get frames from the benchmark if use_frames is True
    frames = []
    if use_frames:
        frames = get_representative_frames(
            first_person_dir, chase_dir, use_recent_frames=True
        )
        if not frames:
            return {"success": False, "reason": "No frames available for evaluation"}

    # Evaluate the success criterion using VLM
    return evaluate_with_vlm(
        success_criterion,
        frames,
        chat_log=chat_log,
        metrics=metrics,
        is_stop_check=False,
    )
