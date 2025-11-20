#!/usr/bin/env python3
import os
import cv2
import numpy as np
import base64
import time
import io
from typing import Optional, Tuple
from pydantic import BaseModel
from PIL import Image

from brain_client.primitive_types import Primitive, PrimitiveResult, RobotStateType
from google import genai
from google.genai.types import GenerateContentConfig, Part


class BoundingBox(BaseModel):
    """Represents a bounding box with normalized coordinates (0-1000) and label."""
    box_2d: list[int]  # [y_min, x_min, y_max, x_max] in normalized coords (0-1000)
    label: str

class RotateFollow(Primitive):
    """
    Primitive that uses Gemini API to detect a target object and tracks it
    by rotating the robot base (horizontal) and tilting the head (vertical)
    to keep the object centered in the camera view.
    """

    def __init__(self, logger):
        super().__init__(logger)
        self.last_main_camera_image_b64 = None
        self.last_head_position = None
        self._cancel_requested = False
        self.gemini_client = None
        
    @property
    def name(self):
        return "rotate_follow"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """Require camera image and head position for object detection and tracking."""
        return [
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64,
            RobotStateType.LAST_HEAD_POSITION
        ]

    def update_robot_state(self, **kwargs):
        """Store the latest camera image and head position."""
        self.last_main_camera_image_b64 = kwargs.get(
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value
        )
        self.last_head_position = kwargs.get(
            RobotStateType.LAST_HEAD_POSITION.value
        )

    def guidelines(self):
        return (
            "Track and follow a target object by rotating the robot base (horizontal) "
            "and tilting the head (vertical) to keep it centered in the camera view. "
            "Uses Gemini API to detect the target, then OpenCV tracker to follow it. "
            "Parameters: target (str, e.g., 'person', 'red cup'), "
            "duration (float, optional, default 60.0 seconds, use -1 for infinite)."
        )

    def _initialize_gemini_client(self) -> bool:
        """Initialize Gemini client if not already done."""
        if self.gemini_client is not None:
            return True
        
        try:
            api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            if not api_key:
                self.logger.error("GOOGLE_API_KEY or GEMINI_API_KEY not found in environment")
                return False
            
            self.gemini_client = genai.Client(api_key=api_key)
            self.logger.info("Gemini 2.5 Flash client initialized successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Gemini client: {e}")
            return False

    def _detect_target_with_gemini(
        self, image_b64: str, target_description: str
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Use Gemini API with structured output to detect target object and return bounding box.
        
        Args:
            image_b64: Base64-encoded image
            target_description: Description of what to find (e.g., "person", "red cup")
            
        Returns:
            Tuple of (x, y, width, height) or None if detection fails
        """
        try:
            # Initialize client if needed
            if not self._initialize_gemini_client():
                return None
            
            # Decode image
            image_data = base64.b64decode(image_b64)
            nparr = np.frombuffer(image_data, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if frame is None:
                self.logger.error("Failed to decode image")
                return None
            
            height, width = frame.shape[:2]
            
            # Convert frame to PIL Image and then to bytes
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_image = Image.fromarray(frame_rgb)
            
            # Convert PIL image to bytes
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format='JPEG')
            img_byte_arr.seek(0)
            image_bytes = img_byte_arr.getvalue()
            
            # Create config with structured output (single BoundingBox)
            config = GenerateContentConfig(
                system_instruction="""
                Return a single bounding box for the requested object.
                If multiple instances exist, return the most prominent or central one.
                Never return masks.
                """,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=BoundingBox,
            )
            
            # Create the prompt
            full_prompt = f"Find and return the bounding box for: {target_description}."
            
            self.logger.info(f"Querying Gemini for: '{target_description}'...")
            
            # Generate content with structured output
            response = self.gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    full_prompt,
                ],
                config=config,
            )
            
            # Parse structured response (single BoundingBox)
            bbox_data = response.parsed
            
            if not bbox_data:
                self.logger.error(f"Gemini could not find: {target_description}")
                return None
            
            self.logger.info(f"Found: {bbox_data.label}")
            
            # Convert normalized coordinates (0-1000) to pixel coordinates
            # Format: [y_min, x_min, y_max, x_max] normalized
            y_min = int(bbox_data.box_2d[0] / 1000 * height)
            x_min = int(bbox_data.box_2d[1] / 1000 * width)
            y_max = int(bbox_data.box_2d[2] / 1000 * height)
            x_max = int(bbox_data.box_2d[3] / 1000 * width)
            
            # Return bbox in (x, y, width, height) format for OpenCV tracker
            bbox_x = x_min
            bbox_y = y_min
            bbox_width = x_max - x_min
            bbox_height = y_max - y_min
            
            self.logger.info(
                f"Detected {target_description} at bbox: ({bbox_x}, {bbox_y}, {bbox_width}, {bbox_height})"
            )
            
            return (bbox_x, bbox_y, bbox_width, bbox_height)
            
        except Exception as e:
            self.logger.error(f"Gemini detection failed: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None

    def execute(self, target: str, duration: float = 60.0):
        """
        Track and follow a target object by rotating to keep it centered.
        
        Args:
            target: Description of what to follow (e.g., "person", "red cup")
            duration: How long to track in seconds (use -1 for infinite)
            
        Returns:
            Tuple of (message, status)
        """
        if self.mobility is None:
            return "Mobility interface not available", PrimitiveResult.FAILURE
        
        if self.head is None:
            return "Head interface not available", PrimitiveResult.FAILURE
        
        if not self.last_main_camera_image_b64:
            return "No camera image available", PrimitiveResult.FAILURE
        
        if not self.last_head_position:
            return "No head position data available", PrimitiveResult.FAILURE
        
        self._cancel_requested = False
        
        self._send_feedback(f"Detecting {target} using Gemini API...")
        
        # Detect target with Gemini
        bbox = self._detect_target_with_gemini(self.last_main_camera_image_b64, target)
        
        if bbox is None:
            return f"Failed to detect {target}", PrimitiveResult.FAILURE
        
        # Decode current image for tracker initialization
        try:
            image_data = base64.b64decode(self.last_main_camera_image_b64)
            nparr = np.frombuffer(image_data, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if frame is None:
                return "Failed to decode camera image", PrimitiveResult.FAILURE
            
        except Exception as e:
            self.logger.error(f"Image decode error: {e}")
            return f"Image decode error: {e}", PrimitiveResult.FAILURE
        
        # Initialize OpenCV CSRT tracker
        tracker = cv2.TrackerCSRT_create()
        tracker.init(frame, bbox)
        
        height, width = frame.shape[:2]
        center_x = width / 2
        center_y = height / 2
        
        # Get head position limits
        min_head_angle = self.last_head_position.get("min_angle", -25.0)
        max_head_angle = self.last_head_position.get("max_angle", 15.0)
        
        self._send_feedback(f"Tracker initialized. Following {target}...")
        
        # Tracking loop
        start_time = time.time()
        last_feedback_time = start_time
        tracking_active = True
        
        # PID-like control parameters for base rotation (horizontal)
        angular_speed_scale = 0.003  # rad/s per pixel error
        max_angular_speed = 1.0  # rad/s
        deadband_x = 30  # pixels - don't rotate if within this range of center
        
        # PID-like control parameters for head tilt (vertical)
        head_angle_scale = 0.045  # degrees per pixel error
        max_head_adjustment = 2.0  # max degrees per update
        deadband_y = 30  # pixels - don't tilt if within this range of center
        
        while tracking_active:
            # Check timeout
            elapsed = time.time() - start_time
            if duration > 0 and elapsed >= duration:
                self._send_feedback(f"Tracking duration ({duration}s) reached")
                break
            
            # Check cancellation
            if self._cancel_requested:
                self._send_feedback("Tracking canceled")
                break
            
            # Get fresh image (continuously updated by action server at 10Hz)
            if not self.last_main_camera_image_b64:
                self.logger.warn("No camera image available, stopping tracking")
                break
            
            try:
                # Decode the continuously-updated base64 image
                image_data = base64.b64decode(self.last_main_camera_image_b64)
                nparr = np.frombuffer(image_data, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if frame is None:
                    self.logger.warn("Failed to decode frame, stopping tracking")
                    break
                
            except Exception as e:
                self.logger.error(f"Frame decode error: {e}")
                break
            
            # Update tracker
            success, bbox = tracker.update(frame)
            
            if success:
                x, y, w, h = [int(v) for v in bbox]
                
                # Calculate center of bounding box
                bbox_center_x = x + w / 2
                bbox_center_y = y + h / 2
                
                # Calculate horizontal error from image center
                error_x = bbox_center_x - center_x
                
                # Calculate vertical error from image center
                # Positive error_y = object is below center (need to tilt down = negative angle)
                # Negative error_y = object is above center (need to tilt up = positive angle)
                error_y = bbox_center_y - center_y
                
                # HORIZONTAL CONTROL: Base rotation
                if abs(error_x) > deadband_x:
                    # Calculate angular velocity (positive = CCW, negative = CW)
                    # If object is to the right (error_x > 0), rotate CW (negative)
                    angular_vel = -error_x * angular_speed_scale
                    angular_vel = max(-max_angular_speed, min(max_angular_speed, angular_vel))
                    
                    # Send rotation command
                    self.mobility.send_cmd_vel(linear_x=0.0, angular_z=angular_vel)
                else:
                    # Object centered horizontally, stop rotation
                    self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                
                # VERTICAL CONTROL: Head tilt
                if abs(error_y) > deadband_y and self.last_head_position:
                    # Get current head angle
                    current_head_angle = self.last_head_position.get("current_position", 0.0)
                    
                    # Calculate angle adjustment
                    # If object is below center (error_y > 0), tilt down (negative)
                    # If object is above center (error_y < 0), tilt up (positive)
                    angle_adjustment = -error_y * head_angle_scale
                    angle_adjustment = max(-max_head_adjustment, min(max_head_adjustment, angle_adjustment))
                    
                    # Calculate new target angle
                    target_head_angle = current_head_angle + angle_adjustment
                    
                    # Clamp to limits
                    target_head_angle = max(min_head_angle, min(max_head_angle, target_head_angle))
                    
                    # Send head tilt command
                    self.head.set_position(int(target_head_angle))
                
                # Periodic feedback
                if time.time() - last_feedback_time >= 2.0:
                    current_head = self.last_head_position.get("current_position", 0.0) if self.last_head_position else 0.0
                    self._send_feedback(
                        f"Tracking {target}: horiz_err={error_x:.0f}px, vert_err={error_y:.0f}px, "
                        f"head={current_head:.1f}°, elapsed={elapsed:.1f}s"
                    )
                    last_feedback_time = time.time()
                    
            else:
                # Tracking lost
                self._send_feedback(f"Tracking lost for {target}")
                tracking_active = False
                break
            
            # Small sleep to prevent CPU hogging
            time.sleep(0.05)  # 20 Hz update rate
        
        # Stop the robot
        self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
        
        if self._cancel_requested:
            return f"Tracking of {target} canceled", PrimitiveResult.CANCELLED
        elif not tracking_active:
            return f"Lost track of {target}", PrimitiveResult.FAILURE
        else:
            return f"Completed tracking {target} for {elapsed:.1f}s", PrimitiveResult.SUCCESS

    def cancel(self):
        """Cancel the tracking operation."""
        self._cancel_requested = True
        if self.mobility is not None:
            try:
                self.mobility.send_cmd_vel(0.0, 0.0)
            except Exception:
                pass
        return "RotateFollow tracking canceled"
