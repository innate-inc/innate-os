#!/usr/bin/env python3
"""
Arm Down Check Height and Cam - Move arm down, detect contact, capture wrist image.
"""

import base64
import time
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType


class ArmDownCheckHeightAndCam(Skill):
    """Move the arm down to Z=0 while maintaining current XY position."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)
    
    TARGET_Z = 0.0
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57  # Pointing downward
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "arm_down_check_height_and_cam"
    
    def guidelines(self):
        return (
            "Move the arm down while keeping the current XY position. "
            "Detects contact when motor load drops (surface supports arm weight). "
            "On contact: captures wrist camera image, saves to file, then returns to Z=0.1. "
            "No parameters required."
        )
    
    def execute(self, duration: int = 3):
        """
        Move arm down to Z=0.
        
        Args:
            duration: Motion duration in seconds
        """
        self._cancelled = False
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        # Get current pose
        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose is None:
            return "Could not get current arm pose", SkillResult.FAILURE
        
        x = current_pose["position"]["x"]
        y = current_pose["position"]["y"]
        
        self.logger.info(
            f"Moving arm down to Z={self.TARGET_Z} from current XY=({x}, {y})"
        )
        
        success = self.manipulation.move_to_cartesian_pose(
            x=x,
            y=y,
            z=self.TARGET_Z,
            roll=self.FIXED_ROLL,
            pitch=self.FIXED_PITCH,
            yaw=self.FIXED_YAW,
            duration=duration
        )
        
        if not success:
            return "Failed to solve IK or send arm command", SkillResult.FAILURE
        
        # Wait for motion to complete with load-based contact detection
        # When arm rests on surface, load drops because surface supports the weight
        start_time = time.time()
        CONTACT_THRESHOLD = 10.0  # Contact detected when load drops below 10%
        
        while time.time() - start_time < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            
            # Read J2 load and Z position
            motor_load = self.manipulation.get_motor_load()
            fk_pose = self.manipulation.get_current_end_effector_pose()
            
            j2_load = motor_load[1] if motor_load and len(motor_load) > 1 else None
            z_pos = fk_pose["position"]["z"] if fk_pose else None
            
            if j2_load is not None and z_pos is not None:
                self._send_feedback(f"J2={j2_load:.1f}% | Z={z_pos:.3f}")
                
                # Contact detected when load drops below threshold (surface supports arm)
                if abs(j2_load) < CONTACT_THRESHOLD:
                    self._send_feedback(f"Contact detected at Z={z_pos:.3f} (load={j2_load:.1f}%)")
                    
                    # Capture and save image
                    if self.image:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        img_path = Path(f"/home/jetson1/innate-os/captures/contact_{timestamp}.jpg")
                        img_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        img_data = base64.b64decode(self.image)
                        img_path.write_bytes(img_data)
                        self._send_feedback(f"Image saved to {img_path}")
                        self.logger.info(f"Contact image saved to {img_path}")
                    
                    # Go back up to Z=0.1
                    self._send_feedback("Going back up to Z=0.1...")
                    self.manipulation.move_to_cartesian_pose(
                        x=x, y=y, z=0.1,
                        roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
                        duration=2
                    )
                    time.sleep(2)
                    
                    return f"Contact at Z={z_pos:.3f}, image saved, returned to Z=0.1", SkillResult.SUCCESS
            
            time.sleep(0.05)
        
        return f"Arm moved down to Z={self.TARGET_Z}", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
