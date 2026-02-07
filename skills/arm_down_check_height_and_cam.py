#!/usr/bin/env python3
"""
Arm Down Check Height and Cam - Move arm down, detect contact, analyze image with
an internal vision agent that provides actionable movement guidance.
"""

import base64
import json
import time
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType


def _load_env_file(env_path: Path) -> dict:
    """Load environment variables from a file."""
    env_vars = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars


# Gripper center position in the image (pixel coordinates)
# This is where the end effector tip touches the surface
GRIPPER_CENTER_X = 320  # horizontal center of 640px image
GRIPPER_CENTER_Y = 380  # measured from top of 480px image

# Search bounds for binary search
X_MIN, X_MAX = 0.1, 0.4
Y_MIN, Y_MAX = -0.2, 0.1

VISION_PROMPT = """You are a vision system for a robot arm on a chessboard.
The camera is a FISHEYE wrist camera pointing straight down.

A RED DOT with crosshair marks the EXACT CENTER of the gripper on the surface.

TARGET: {target}

{history_section}

SPATIAL MAPPING (image directions to robot movement):
- TOP of image = forward (+X)
- BOTTOM of image = backward (-X)
- RIGHT of image = robot's right (-Y)
- LEFT of image = robot's left (+Y)

Answer with ONLY this JSON (no markdown):
{{
  "on_target": true or false,
  "target_visible": true or false,
  "move_x": "forward" or "backward" or "none",
  "move_y": "left" or "right" or "none",
  "reasoning": "brief description of what you see"
}}

Rules:
- on_target=true ONLY if the red dot is clearly on the center of the target square.
- If the target square is visible, say which direction the red dot needs to move to reach it.
- If you see mostly carpet/floor (overshot the board), the board edge tells you which way to go back.
- "none" means that axis is already aligned with the target.
- Be honest about what you see. Do NOT guess if the board is not visible."""


class ArmDownCheckHeightAndCam(Skill):
    """Move arm down, detect contact, analyze image with internal vision agent."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)
    
    TARGET_Z = 0.0
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57  # Pointing downward
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._step_history = []  # Persists across calls within same process
        # Binary search bounds (narrow as we get directional feedback)
        self._x_low = X_MIN
        self._x_high = X_MAX
        self._y_low = Y_MIN
        self._y_high = Y_MAX
        
        # Load Gemini config (same pattern as scan_for_objects)
        env_path = Path(__file__).parent / ".env.scan"
        env_vars = _load_env_file(env_path)
        self.api_key = env_vars.get("GEMINI_API_KEY", "")
        self.vision_model = None
        
        if self.api_key and self.api_key != "your_gemini_api_key_here":
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self.vision_model = genai.GenerativeModel("gemini-3-flash-preview")
                self.logger.info("[ArmDownCheck] Vision agent configured (Gemini)")
            except Exception as e:
                self.logger.warning(f"[ArmDownCheck] Could not configure Gemini: {e}")
        else:
            self.logger.warning("[ArmDownCheck] GEMINI_API_KEY not set - will send raw images as fallback")
    
    @property
    def name(self):
        return "arm_down_check_height_and_cam"
    
    def guidelines(self):
        return (
            "Move the arm down while keeping the current XY position. "
            "Detects contact when motor load drops (surface supports arm weight). "
            "On contact: analyzes wrist camera image and returns movement guidance. "
            "The feedback will tell you WHERE the target is and SUGGEST exact X,Y coordinates to move to. "
            "Optional parameter: target_description (what to look for, default: 'center of the top right square'). "
            "Follow the suggested coordinates in the feedback rather than guessing."
        )
    
    def execute(self, target_description: str = "center of the top right square", duration: int = 2):
        """
        Move arm down to Z=0, detect contact, analyze image.
        
        Args:
            target_description: What the arm should be centered on
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
        
        # Open gripper so camera can see past the fingers
        self.manipulation.open_gripper(percent=60.0, duration=0.3)
        time.sleep(0.3)
        
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
                    # Go back up to Z=0.1 FIRST so camera gets a clear overhead view
                    self._send_feedback("Going back up to Z=0.1...")
                    self.manipulation.move_to_cartesian_pose(
                        x=x, y=y, z=0.1,
                        roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
                        duration=1
                    )
                    time.sleep(1)
                    
                    # Capture fresh image from Z=0.1 (clear overhead view)
                    overhead_image = self.image
                    
                    # Save raw + annotated images to file for reference
                    if overhead_image:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        captures_dir = Path("/home/jetson1/innate-os/captures")
                        captures_dir.mkdir(parents=True, exist_ok=True)
                        raw_path = captures_dir / f"contact_{timestamp}.jpg"
                        raw_path.write_bytes(base64.b64decode(overhead_image))
                        annotated_b64 = self._annotate_image(overhead_image)
                        ann_path = captures_dir / f"annotated_{timestamp}.jpg"
                        ann_path.write_bytes(base64.b64decode(annotated_b64))
                        self.logger.info(f"Images saved: {raw_path}, {ann_path}")
                    
                    # Analyze overhead image with internal vision agent
                    guidance = self._analyze_image(
                        image_b64=overhead_image,
                        current_x=x,
                        current_y=y,
                        contact_z=z_pos,
                        target_description=target_description,
                    )
                    
                    # Send guidance as feedback
                    self._send_feedback(guidance)
                    
                    return f"Contact at Z={z_pos:.3f}. {guidance}", SkillResult.SUCCESS
            
            time.sleep(0.05)
        
        return f"Arm moved down to Z={self.TARGET_Z} (no contact detected)", SkillResult.SUCCESS
    
    def _annotate_image(self, image_b64):
        """Draw a red crosshair on the image at the gripper center point."""
        try:
            from PIL import Image, ImageDraw
            import io
            
            img_data = base64.b64decode(image_b64)
            img = Image.open(io.BytesIO(img_data))
            draw = ImageDraw.Draw(img)
            
            cx, cy = GRIPPER_CENTER_X, GRIPPER_CENTER_Y
            
            # Red filled circle with white outline
            r = 7
            draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill='red', outline='white', width=2)
            # Crosshair lines
            cl = 16
            draw.line([(cx-cl, cy), (cx+cl, cy)], fill='red', width=2)
            draw.line([(cx, cy-cl), (cx, cy+cl)], fill='red', width=2)
            
            # Encode back to base64 JPEG
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
        except Exception as e:
            self.logger.warning(f"[ArmDownCheck] Could not annotate image: {e}")
            return image_b64  # Return original on failure
    
    def _analyze_image(self, image_b64, current_x, current_y, contact_z, target_description):
        """Analyze wrist camera image with internal vision agent using binary search."""
        
        # Fallback if no vision model configured
        if not self.vision_model or not image_b64:
            fallback = f"Contact at Z={contact_z:.3f}, position X={current_x:.4f} Y={current_y:.4f} (no vision analysis available)"
            self._step_history.append({
                "step": len(self._step_history) + 1,
                "x": current_x, "y": current_y,
                "guidance": fallback,
            })
            return fallback
        
        # Draw red crosshair on image at gripper center
        annotated_b64 = self._annotate_image(image_b64)
        
        # Build history section for the prompt
        if self._step_history:
            history_lines = ["PREVIOUS STEPS:"]
            for entry in self._step_history[-6:]:
                history_lines.append(
                    f"  Step {entry['step']}: X={entry['x']:.4f}, Y={entry['y']:.4f} -> {entry['guidance']}"
                )
            history_section = "\n".join(history_lines)
        else:
            history_section = "PREVIOUS STEPS: None (first check)"
        
        prompt = VISION_PROMPT.format(
            target=target_description,
            history_section=history_section,
        )
        
        try:
            import google.generativeai as genai
            
            image_part = {"mime_type": "image/jpeg", "data": annotated_b64}
            
            response = self.vision_model.generate_content(
                [prompt, image_part],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                ),
            )
            
            result = json.loads(response.text.strip())
            
            # Use binary search to compute next position from direction
            guidance = self._binary_search_step(result, current_x, current_y)
            
            # Record in history
            short_summary = result.get("reasoning", "")[:100]
            move_x = result.get("move_x", "none")
            move_y = result.get("move_y", "none")
            self._step_history.append({
                "step": len(self._step_history) + 1,
                "x": current_x,
                "y": current_y,
                "guidance": f"move_x={move_x}, move_y={move_y}. {short_summary}",
            })
            
            self.logger.info(f"[ArmDownCheck] Vision: move_x={move_x}, move_y={move_y} -> {guidance}")
            return guidance
            
        except Exception as e:
            self.logger.error(f"[ArmDownCheck] Vision analysis failed: {e}")
            fallback = f"Vision analysis failed. Position X={current_x:.4f} Y={current_y:.4f}, Z={contact_z:.3f}"
            self._step_history.append({
                "step": len(self._step_history) + 1,
                "x": current_x, "y": current_y,
                "guidance": f"(analysis failed: {e})",
            })
            return fallback
    
    def _binary_search_step(self, result, current_x, current_y):
        """Compute next position using binary search based on directional feedback."""
        on_target = result.get("on_target", False)
        reasoning = result.get("reasoning", "")
        
        if on_target:
            return (
                f"ON TARGET at X={current_x:.4f}, Y={current_y:.4f}. {reasoning}"
            )
        
        move_x = result.get("move_x", "none")
        move_y = result.get("move_y", "none")
        
        # Update search bounds based on direction
        # If told to move forward (+X), current position is too low -> raise x_low
        # If told to move backward (-X), current position is too high -> lower x_high
        if move_x == "forward":
            self._x_low = current_x
        elif move_x == "backward":
            self._x_high = current_x
        
        # If told to move right (-Y), current position Y is too high -> lower y_high
        # If told to move left (+Y), current position Y is too low -> raise y_low
        if move_y == "right":
            self._y_high = current_y
        elif move_y == "left":
            self._y_low = current_y
        
        # Next position = midpoint of remaining search space
        next_x = (self._x_low + self._x_high) / 2.0
        next_y = (self._y_low + self._y_high) / 2.0
        
        # Clamp to valid range
        next_x = max(X_MIN, min(X_MAX, next_x))
        next_y = max(Y_MIN, min(Y_MAX, next_y))
        
        range_x = self._x_high - self._x_low
        range_y = self._y_high - self._y_low
        
        return (
            f"Move {move_x}/{move_y}. "
            f"MOVE TO X={next_x:.4f}, Y={next_y:.4f}. "
            f"Search range: X=[{self._x_low:.3f},{self._x_high:.3f}]({range_x*100:.1f}cm) "
            f"Y=[{self._y_low:.3f},{self._y_high:.3f}]({range_y*100:.1f}cm). "
            f"{reasoning}"
        )
    
    def reset_history(self):
        """Reset the step history and binary search bounds."""
        self._step_history = []
        self._x_low = X_MIN
        self._x_high = X_MAX
        self._y_low = Y_MIN
        self._y_high = Y_MAX
        self.logger.info("[ArmDownCheck] Step history and search bounds reset")
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
