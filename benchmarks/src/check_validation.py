#!/usr/bin/env python3
import time
from pathlib import Path
from typing import Dict, List

from .vlm_utils import get_representative_frames, evaluate_with_vlm


def validate_location_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Validate if the robot is within the specified location bounding box.
    This is a placeholder for future implementation.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        **kwargs: Additional context data

    Returns:
        True if the check passes, False otherwise
    """
    # TODO: Implement location validation using robot position data
    print(f"Location check '{check_id}' - Not implemented yet")
    return False


def validate_primitive_check(check_id: str, check_data: Dict, **kwargs) -> bool:
    """
    Validate if the specified primitive was called with appropriate arguments.
    This is a placeholder for future implementation.

    Args:
        check_id: Identifier for the check
        check_data: Check configuration data
        **kwargs: Additional context data

    Returns:
        True if the check passes, False otherwise
    """
    # TODO: Implement primitive call validation using LLM for argument verification
    print(f"Primitive check '{check_id}' - Not implemented yet")
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
) -> Dict[str, bool]:
    """
    Validate all checks in the configuration and return the updated status.

    Args:
        expectations: Benchmark expectations configuration
        check_status: Current status of checks
        first_person_dir: Directory with first-person view frames
        chase_dir: Directory with chase view frames
        chat_log: List of chat messages
        messages: List of scheduled messages
        metrics: Benchmark metrics
        save_metrics_callback: Function to save metrics

    Returns:
        Updated check status dictionary
    """
    if not expectations.get("checks"):
        return check_status

    print("Validating checks...")
    updated_status = check_status.copy()

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
            passed = validate_location_check(check_id, check)
        elif check_type == "primitive":
            passed = validate_primitive_check(check_id, check)
        elif check_type == "compound":
            passed = validate_compound_check(check_id, check)
        elif check_type == "sequence":
            passed = validate_sequence_check(check_id, check)
        elif check_type == "vlm_verification":
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
        use_frames: Whether to use frames in evaluation

    Returns:
        True if benchmark should stop, False otherwise
    """
    if not stop_criterion:
        return False

    # Get representative frames from the benchmark so far
    frames = (
        get_representative_frames(first_person_dir, chase_dir) if use_frames else []
    )
    if not frames and use_frames:
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
) -> Dict:
    """
    Evaluate whether the benchmark was successful based on the success criterion.

    Args:
        success_criterion: The criterion to evaluate
        first_person_dir: Directory with first-person frames
        chase_dir: Directory with chase frames
        chat_log: List of chat messages
        metrics: Benchmark metrics

    Returns:
        Dictionary with success status and reason
    """
    if not success_criterion:
        return {"success": False, "reason": "No success criterion defined"}

    # Get representative frames from the benchmark
    frames = get_representative_frames(first_person_dir, chase_dir, comprehensive=True)
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
