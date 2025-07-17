#!/usr/bin/env python3
"""
Test script to demonstrate the new validation system that:
1. First runs deterministic checks every 10 seconds
2. Then runs VLM evaluation with both success and early stop criteria
3. Returns "continue", "success", or "stop" based on the evaluation
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.vlm_utils import evaluate_periodic_with_vlm
from src.check_validation import validate_location_check, validate_primitive_check
import time

def test_deterministic_checks():
    """Test deterministic check validation."""
    print("=== Testing Deterministic Checks ===\n")
    
    # Test location check
    location_check = {
        "coordinates": [0.5, -4.0, 3.5, -8.0]  # TV area coordinates
    }
    
    # Simulate robot positions - first not in area, then in area
    position_history_1 = [(1.0, 2.0), (1.5, 1.5), (2.0, 1.0)]
    position_history_2 = [(1.0, 2.0), (1.5, 0.0), (2.0, -5.0)]  # Last position is in TV area
    
    result_1 = validate_location_check(
        "reached_tv_area", 
        location_check, 
        position_history=position_history_1
    )
    print(f"Location check (robot NOT in area): {result_1}")
    
    result_2 = validate_location_check(
        "reached_tv_area", 
        location_check, 
        position_history=position_history_2
    )
    print(f"Location check (robot IN area): {result_2}")
    
    # Test primitive check
    primitive_check = {
        "primitive_name": "send_email"
    }
    
    # Simulate chat log without the primitive
    chat_log_1 = [
        {"sender": "vision_agent_output", "text": '{"next_task": {"type": "navigate_in_sight"}}'},
        {"sender": "user", "text": "Hello robot"},
    ]
    
    # Simulate chat log with the primitive
    chat_log_2 = [
        {"sender": "vision_agent_output", "text": '{"next_task": {"type": "navigate_in_sight"}}'},
        {"sender": "vision_agent_output", "text": '{"next_task": {"type": "send_email"}}'},
        {"sender": "user", "text": "Hello robot"},
    ]
    
    result_3 = validate_primitive_check(
        "email_sent",
        primitive_check,
        chat_log=chat_log_1
    )
    print(f"Primitive check (email NOT sent): {result_3}")
    
    result_4 = validate_primitive_check(
        "email_sent",
        primitive_check,
        chat_log=chat_log_2
    )
    print(f"Primitive check (email sent): {result_4}")
    
    return {
        "reached_tv_area": result_2,
        "email_sent": result_4,
    }

def test_vlm_evaluation(deterministic_check_status):
    """Test VLM evaluation with mock data."""
    print("\n=== Testing VLM Evaluation ===\n")
    
    # Mock success and early stop criteria
    success_criterion = "Did the robot successfully navigate to the TV and stop directly under it?"
    early_stop_criterion = "Should this benchmark be stopped early if the robot appears stuck?"
    
    # Mock chat log with coordinates
    chat_log_with_coordinates = [
        {
            "sender": "user",
            "text": "Find the TV in the house",
            "time_since_start": 2.0,
            "coordinates": "(1.00, 2.00)"
        },
        {
            "sender": "vision_agent_output",
            "text": '{"next_task": {"type": "navigate_in_sight", "target": "TV"}}',
            "time_since_start": 5.0,
            "coordinates": "(1.50, 1.50)"
        },
        {
            "sender": "vision_agent_output",
            "text": '{"next_task": {"type": "navigate_in_sight", "target": "TV"}}',
            "time_since_start": 15.0,
            "coordinates": "(2.00, -5.00)"
        }
    ]
    
    # Mock metrics
    metrics = {
        "start_timestamp": time.time() - 60,  # 60 seconds ago
    }
    
    # Note: This would normally require actual image frames and API key
    # For demonstration, we'll show what the call would look like
    print("VLM Evaluation would be called with:")
    print(f"Success criterion: {success_criterion}")
    print(f"Early stop criterion: {early_stop_criterion}")
    print(f"Deterministic check status: {deterministic_check_status}")
    print(f"Chat log entries: {len(chat_log_with_coordinates)}")
    print(f"Latest coordinates: {chat_log_with_coordinates[-1]['coordinates']}")
    
    # Mock result (what VLM would return)
    if deterministic_check_status.get("reached_tv_area", False):
        mock_result = {
            "action": "success",
            "reason": "Robot has reached the TV area and appears to be positioned correctly under it",
            "reflection": "The robot successfully navigated to the TV coordinates and the deterministic location check passed"
        }
    else:
        mock_result = {
            "action": "continue",
            "reason": "Robot is still navigating towards the TV area",
            "reflection": "The robot is making progress but hasn't reached the target location yet"
        }
    
    print(f"Mock VLM result: {mock_result}")
    return mock_result

def main():
    """Run the complete test demonstrating the new validation system."""
    print("Testing the New Validation System")
    print("=" * 50)
    
    # Stage 1: Test deterministic checks
    deterministic_status = test_deterministic_checks()
    
    # Stage 2: Test VLM evaluation
    vlm_result = test_vlm_evaluation(deterministic_status)
    
    print(f"\n=== Summary ===")
    print(f"Deterministic checks: {deterministic_status}")
    print(f"VLM action: {vlm_result['action']}")
    print(f"VLM reason: {vlm_result['reason']}")
    
    print(f"\n=== New System Flow ===")
    print("1. Every 10 seconds, run deterministic checks (location, primitive, etc.)")
    print("2. Run VLM evaluation with both success and early stop criteria")
    print("3. VLM returns 'continue', 'success', or 'stop'")
    print("4. Handle the result appropriately in the benchmark runner")

if __name__ == "__main__":
    main() 