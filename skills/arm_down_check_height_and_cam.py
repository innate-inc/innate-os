#!/usr/bin/env python3
"""
Arm Down Check Height and Cam
Move arm down, detect surface contact, capture wrist camera image,
and use Gemini to locate the target square via bounding box detection.
Binary search narrows the arm position until the gripper is on target.
"""

import base64
import io
import json
import time
from datetime import datetime
import google.generativeai as genai
from pathlib import Path

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRIPPER_CENTER_X = 320
GRIPPER_CENTER_Y = 380

X_MIN, X_MAX = 0.1, 0.4
Y_MIN, Y_MAX = -0.2, 0.1

ON_TARGET_THRESHOLD_PX = 30

IMG_WIDTH = 640
IMG_HEIGHT = 480

CAPTURES_DIR = Path("/home/jetson1/innate-os/captures")

CONTACT_THRESHOLD = 10.0

VISION_PROMPT = """Detect the top-right square of the chessboard in this image.
The camera is a FISHEYE wrist camera pointing straight down.

A RED DOT with crosshair marks the current gripper position.

TARGET: {target}

{history_section}

Return the bounding box as box_2d in [ymin, xmin, ymax, xmax] format normalized to 0-1000.
Also indicate which direction the red dot needs to move to reach the center of that square.

SPATIAL MAPPING:
- TOP of image = forward (+X)
- BOTTOM of image = backward (-X)
- RIGHT of image = robot's right (-Y)
- LEFT of image = robot's left (+Y)

Return ONLY this JSON (no markdown):
{{
  "target_visible": true or false,
  "box_2d": [ymin, xmin, ymax, xmax],
  "label": "top_right_square",
  "move_x": "forward" or "backward" or "none",
  "move_y": "left" or "right" or "none",
  "reasoning": "brief description of what you see"
}}

Rules:
- box_2d: normalized coordinates 0-1000 of the target square. Use [0,0,0,0] if not visible.
- move_x/move_y: direction the red dot needs to move to reach the target square center.
- If you see mostly carpet/floor (overshot the board), the board edge tells you which way to go back.
- "none" means that axis is already aligned with the target.
- Be honest about what you see. Do NOT guess if the board is not visible."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env_file(env_path: Path) -> dict:
    env_vars = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars


def _denormalize_box_2d(box_2d):
    """Convert Gemini's box_2d [ymin, xmin, ymax, xmax] (0-1000) to pixel [x1, y1, x2, y2]."""
    y1 = int(box_2d[0] / 1000 * IMG_HEIGHT)
    x1 = int(box_2d[1] / 1000 * IMG_WIDTH)
    y2 = int(box_2d[2] / 1000 * IMG_HEIGHT)
    x2 = int(box_2d[3] / 1000 * IMG_WIDTH)
    return [x1, y1, x2, y2]


def _draw_crosshair(draw, cx, cy):
    """Draw a red dot with crosshair lines."""
    draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill='red', outline='white', width=2)
    draw.line([(cx - 16, cy), (cx + 16, cy)], fill='red', width=2)
    draw.line([(cx, cy - 16), (cx, cy + 16)], fill='red', width=2)


def _draw_bbox_overlay(draw, bbox, bbox_center, on_target, dist_px):
    """Draw bbox rectangle, center dot, connecting line, and label."""
    if not bbox or not any(v > 0 for v in bbox):
        return
    color = 'lime' if on_target else 'yellow'
    draw.rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], outline=color, width=3)
    if bbox_center:
        bcx, bcy = bbox_center
        draw.ellipse([bcx - 5, bcy - 5, bcx + 5, bcy + 5], fill=color, outline='white', width=1)
        draw.line([(GRIPPER_CENTER_X, GRIPPER_CENTER_Y), (bcx, bcy)], fill=color, width=2)
    if dist_px is not None:
        label = f"{'ON TARGET' if on_target else 'OFF'} ({dist_px:.0f}px)"
        draw.text((10, 10), label, fill=color)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class ArmDownCheckHeightAndCam(Skill):
    """Move arm down, detect contact, analyze image with Gemini bbox detection."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    TARGET_Z = 0.0
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57

    # ---- init / metadata ----

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._step_history = []
        self._x_low, self._x_high = X_MIN, X_MAX
        self._y_low, self._y_high = Y_MIN, Y_MAX
        self._last_timestamp = None
        self._init_gemini()

    def _init_gemini(self):
        env_vars = _load_env_file(Path(__file__).parent / ".env.scan")
        self.api_key = env_vars.get("GEMINI_API_KEY", "")
        self.vision_model = None
        if self.api_key and self.api_key != "your_gemini_api_key_here":
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self.vision_model = genai.GenerativeModel("gemini-3-flash-preview")
                self.logger.info("[ArmDownCheck] Gemini configured")
            except Exception as e:
                self.logger.warning(f"[ArmDownCheck] Gemini init failed: {e}")

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

    # ---- execute ----

    def execute(self, target_description: str = "center of the top right square", duration: int = 2):
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose is None:
            return "Could not get current arm pose", SkillResult.FAILURE

        x = current_pose["position"]["x"]
        y = current_pose["position"]["y"]

        self.manipulation.open_gripper(percent=60.0, duration=0.3)
        time.sleep(0.3)

        self.logger.info(f"Moving arm down from XY=({x}, {y})")

        if not self._move_down(x, y, duration):
            return "Failed to solve IK or send arm command", SkillResult.FAILURE

        contact = self._wait_for_contact(x, y, duration)
        if contact is None:
            return f"Arm moved down to Z={self.TARGET_Z} (no contact detected)", SkillResult.SUCCESS

        z_pos = contact
        guidance = self._capture_and_analyze(x, y, z_pos, target_description)
        self._move_up(x, y)
        self._send_feedback(guidance)
        return f"Contact at Z={z_pos:.3f}. {guidance}", SkillResult.SUCCESS

    # ---- motion helpers ----

    def _move_down(self, x, y, duration):
        return self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.TARGET_Z,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=duration,
        )

    def _move_up(self, x, y):
        self._send_feedback("Going back up to Z=0.1...")
        self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=0.1,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=1,
        )
        time.sleep(1)

    def _wait_for_contact(self, x, y, duration):
        """Poll J2 load until contact detected. Returns contact Z or None."""
        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                return None
            motor_load = self.manipulation.get_motor_load()
            fk_pose = self.manipulation.get_current_end_effector_pose()
            j2 = motor_load[1] if motor_load and len(motor_load) > 1 else None
            z = fk_pose["position"]["z"] if fk_pose else None
            if j2 is not None and z is not None:
                self._send_feedback(f"J2={j2:.1f}% | Z={z:.3f}")
                if abs(j2) < CONTACT_THRESHOLD:
                    return z
            time.sleep(0.05)
        return None

    # ---- image capture & analysis ----

    def _capture_and_analyze(self, x, y, contact_z, target_description):
        """Capture surface image, save it, analyze with Gemini, save annotated."""
        image_b64 = self.image
        self._save_raw_image(image_b64)
        return self._analyze_image(image_b64, x, y, contact_z, target_description)

    def _save_raw_image(self, image_b64):
        if not image_b64:
            return
        self._last_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        path = CAPTURES_DIR / f"contact_{self._last_timestamp}.jpg"
        path.write_bytes(base64.b64decode(image_b64))
        self.logger.info(f"Raw image saved: {path}")

    def _analyze_image(self, image_b64, x, y, contact_z, target_description):
        if not self.vision_model or not image_b64:
            return self._record_step(x, y, f"No vision analysis (X={x:.4f} Y={y:.4f})")

        annotated_b64 = self._add_crosshair(image_b64)
        result = self._call_gemini(annotated_b64, target_description)
        if result is None:
            return self._record_step(x, y, f"Vision failed (X={x:.4f} Y={y:.4f})")

        abs_bbox, bbox_center, dist_px, on_target = self._compute_on_target(result)
        self._save_annotated_image(image_b64, abs_bbox, bbox_center, on_target, dist_px)

        result["on_target"] = on_target
        guidance = self._binary_search_step(result, x, y)

        move_x = result.get("move_x", "none")
        move_y = result.get("move_y", "none")
        dist_info = f" dist={dist_px:.0f}px" if dist_px is not None else ""
        summary = result.get("reasoning", "")[:100]
        self._record_step(x, y, f"move_x={move_x}, move_y={move_y}.{dist_info} {summary}")

        self.logger.info(f"[ArmDownCheck] {move_x}/{move_y} -> {guidance}")
        return guidance

    # ---- Gemini ----

    def _call_gemini(self, annotated_b64, target_description):
        """Call Gemini with annotated image. Returns parsed JSON or None."""
        history_section = self._build_history_section()
        prompt = VISION_PROMPT.format(target=target_description, history_section=history_section)
        try:
            response = self.vision_model.generate_content(
                [prompt, {"mime_type": "image/jpeg", "data": annotated_b64}],
                generation_config=genai.GenerationConfig(response_mime_type="application/json"),
            )
            return json.loads(response.text.strip())
        except Exception as e:
            self.logger.error(f"[ArmDownCheck] Gemini call failed: {e}")
            return None

    def _build_history_section(self):
        if not self._step_history:
            return "PREVIOUS STEPS: None (first check)"
        lines = ["PREVIOUS STEPS:"]
        for entry in self._step_history[-6:]:
            lines.append(f"  Step {entry['step']}: X={entry['x']:.4f}, Y={entry['y']:.4f} -> {entry['guidance']}")
        return "\n".join(lines)

    # ---- bbox / on-target ----

    def _compute_on_target(self, result):
        """Denormalize box_2d and check distance from gripper center."""
        box_2d = result.get("box_2d", [0, 0, 0, 0])
        if not box_2d or len(box_2d) != 4 or not any(v > 0 for v in box_2d):
            return None, None, None, False

        abs_bbox = _denormalize_box_2d(box_2d)
        bbox_cx = (abs_bbox[0] + abs_bbox[2]) / 2.0
        bbox_cy = (abs_bbox[1] + abs_bbox[3]) / 2.0
        bbox_center = (bbox_cx, bbox_cy)
        dist_px = ((bbox_cx - GRIPPER_CENTER_X) ** 2 + (bbox_cy - GRIPPER_CENTER_Y) ** 2) ** 0.5
        on_target = dist_px < ON_TARGET_THRESHOLD_PX

        self.logger.info(
            f"[ArmDownCheck] box_2d={box_2d} abs={abs_bbox} "
            f"center=({bbox_cx:.0f},{bbox_cy:.0f}) dist={dist_px:.0f}px on_target={on_target}"
        )
        return abs_bbox, bbox_center, dist_px, on_target

    # ---- binary search ----

    def _binary_search_step(self, result, current_x, current_y):
        """Narrow search bounds and return the next midpoint position."""
        if result.get("on_target", False):
            return f"ON TARGET at X={current_x:.4f}, Y={current_y:.4f}. {result.get('reasoning', '')}"

        move_x = result.get("move_x", "none")
        move_y = result.get("move_y", "none")

        if move_x == "forward":
            self._x_low = current_x
        elif move_x == "backward":
            self._x_high = current_x

        if move_y == "right":
            self._y_high = current_y
        elif move_y == "left":
            self._y_low = current_y

        next_x = max(X_MIN, min(X_MAX, (self._x_low + self._x_high) / 2.0))
        next_y = max(Y_MIN, min(Y_MAX, (self._y_low + self._y_high) / 2.0))
        rx = (self._x_high - self._x_low) * 100
        ry = (self._y_high - self._y_low) * 100

        return (
            f"Move {move_x}/{move_y}. "
            f"MOVE TO X={next_x:.4f}, Y={next_y:.4f}. "
            f"Search range: X=[{self._x_low:.3f},{self._x_high:.3f}]({rx:.1f}cm) "
            f"Y=[{self._y_low:.3f},{self._y_high:.3f}]({ry:.1f}cm). "
            f"{result.get('reasoning', '')}"
        )

    # ---- drawing / saving ----

    def _add_crosshair(self, image_b64):
        """Return base64 image with red crosshair at gripper center."""
        try:
            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
            _draw_crosshair(ImageDraw.Draw(img), GRIPPER_CENTER_X, GRIPPER_CENTER_Y)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
        except Exception as e:
            self.logger.warning(f"[ArmDownCheck] Crosshair draw failed: {e}")
            return image_b64

    def _save_annotated_image(self, image_b64, abs_bbox, bbox_center, on_target, dist_px):
        """Save image with crosshair + bbox overlay to captures dir."""
        try:
            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
            draw = ImageDraw.Draw(img)
            _draw_crosshair(draw, GRIPPER_CENTER_X, GRIPPER_CENTER_Y)
            _draw_bbox_overlay(draw, abs_bbox, bbox_center, on_target, dist_px)
            ts = self._last_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
            path = CAPTURES_DIR / f"annotated_{ts}.jpg"
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            path.write_bytes(buf.getvalue())
            self.logger.info(f"[ArmDownCheck] Annotated: {path}")
        except Exception as e:
            self.logger.warning(f"[ArmDownCheck] Annotated save failed: {e}")

    # ---- history ----

    def _record_step(self, x, y, guidance):
        self._step_history.append({
            "step": len(self._step_history) + 1,
            "x": x, "y": y, "guidance": guidance,
        })
        return guidance

    def reset_history(self):
        """Reset step history and binary search bounds."""
        self._step_history = []
        self._x_low, self._x_high = X_MIN, X_MAX
        self._y_low, self._y_high = Y_MIN, Y_MAX
        self.logger.info("[ArmDownCheck] History and bounds reset")

    def cancel(self):
        self._cancelled = True
        return "Arm motion cancelled"
