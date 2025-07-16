#!/usr/bin/env python3
"""
Test script to verify periodic validation of location checks.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.check_validation import validate_location_check

def test_location_check():
    """Test the location check validation with simulated robot positions."""
    
    # Test configuration from navigation_in_sight_complex_test.yaml
    check_data = {
        "coordinates": [0.5, -4.0, 3.5, -8.0]  # TV area coordinates
    }
    
    # Test case 1: Robot not in TV area
    position_history_1 = [(1.0, 2.0), (1.5, 1.5), (2.0, 1.0)]
    result_1 = validate_location_check(
        "reached_tv_area", 
        check_data, 
        position_history=position_history_1
    )
    print(f"Test 1 (robot not in TV area): {result_1}")
    assert result_1 == False, "Robot should not be in TV area"
    
    # Test case 2: Robot enters TV area
    position_history_2 = [(1.0, 2.0), (1.5, 0.0), (2.0, -5.0)]  # Last position is in TV area
    result_2 = validate_location_check(
        "reached_tv_area", 
        check_data, 
        position_history=position_history_2
    )
    print(f"Test 2 (robot in TV area): {result_2}")
    assert result_2 == True, "Robot should be in TV area"
    
    # Test case 3: Robot briefly enters TV area then leaves
    position_history_3 = [(1.0, 2.0), (2.0, -6.0), (1.0, 2.0)]  # Middle position is in TV area
    result_3 = validate_location_check(
        "reached_tv_area", 
        check_data, 
        position_history=position_history_3
    )
    print(f"Test 3 (robot briefly in TV area): {result_3}")
    assert result_3 == True, "Robot should be detected as having been in TV area"
    
    print("All location check tests passed!")

if __name__ == "__main__":
    test_location_check() 