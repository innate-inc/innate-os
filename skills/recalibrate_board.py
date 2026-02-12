#!/usr/bin/env python3
"""
Recalibrate Board Skill - Hover above the believed top-right corner (H8),
use Gemini to locate the actual H8 square, and shift calibration accordingly.
"""

import base64
import io
import json
import math
import time
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType

from google import genai
from google.genai import types


CALIBRATION_FILE = Path.home() / "board_calibration.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/gemini"


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


class RecalibrateBoard(Skill):
    """Hover above the believed H8 square, ask Gemini to find the actual
    top-right corner square, and update the calibration file."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    # Fixed orientation
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57

    # Camera tilt
    CAMERA_PITCH_OFFSET = -0.48
    CAMERA_TILT_X_OFFSET = -0.02

    # Arm shoulder origin (for computing reach angle on 5DOF arm)
    ARM_ORIGIN_X = 0.05
    ARM_ORIGIN_Y = -0.05

    # Heights
    HEIGHT_SAFE = 0.2
    HEIGHT_ABOVE = 0.15

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._init_gemini()

    def _init_gemini(self):
        env_vars = _load_env_file(Path(__file__).parent / ".env.scan")
        self.api_key = env_vars.get("GEMINI_API_KEY", "")
        self.gemini_client = None
        if self.api_key and self.api_key != "your_gemini_api_key_here":
            self.gemini_client = genai.Client(api_key=self.api_key)
            self.logger.info("[RecalibrateBoard] Gemini configured")

    @property
    def name(self):
        return "recalibrate_board"

    def guidelines(self):
        return (
            "Recalibrate the chessboard by hovering above the believed top-right corner (H8) "
            "and using Gemini vision to find the actual H8 square. The calibration file is "
            "then shifted so that H8 lines up correctly. Run this when piece placement is drifting."
        )

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except:
            return None

    def _compute_square_size_x(self, calibration: dict) -> float:
        tl = calibration["top_left"]
        tr = calibration["top_right"]
        bl = calibration["bottom_left"]
        br = calibration["bottom_right"]
        avg_top_x = (tl["x"] + tr["x"]) / 2.0
        avg_bottom_x = (bl["x"] + br["x"]) / 2.0
        return (avg_top_x - avg_bottom_x) / 7.0

    def _compute_square_size_y(self, calibration: dict) -> float:
        tl = calibration["top_left"]
        tr = calibration["top_right"]
        bl = calibration["bottom_left"]
        br = calibration["bottom_right"]
        avg_left_y = (tl["y"] + bl["y"]) / 2.0
        avg_right_y = (tr["y"] + br["y"]) / 2.0
        return (avg_left_y - avg_right_y) / 7.0

    def execute(self):
        """
        Recalibrate by hovering above believed H8 and using Gemini to find
        the actual top-right corner square.
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        if not self.gemini_client:
            return "Gemini not configured", SkillResult.FAILURE

        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found", SkillResult.FAILURE

        # H8 = top_right corner
        tr = calibration["top_right"]
        x, y = tr["x"], tr["y"]

        tilted_pitch = self.FIXED_PITCH + self.CAMERA_PITCH_OFFSET
        yaw = self.FIXED_YAW

        self.logger.info(
            f"[RecalibrateBoard] Believed H8 at X={x:.4f}, Y={y:.4f}"
        )

        # Step 1: Move to safe height
        self.logger.info("[RecalibrateBoard] Step 1: Moving to safe height")
        self._send_feedback("Moving to safe height...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        if not success:
            return "Failed to move to safe height", SkillResult.FAILURE
        time.sleep(2.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 2: Move above believed H8
        self.logger.info("[RecalibrateBoard] Step 2: Moving above believed H8")
        self._send_feedback("Moving above H8...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        if not success:
            return "Failed to move above H8", SkillResult.FAILURE
        time.sleep(2.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 3: Apply tilt X offset for camera viewing
        tilt_x = x + self.CAMERA_TILT_X_OFFSET
        self.logger.info(f"[RecalibrateBoard] Step 3: Shifting X by {self.CAMERA_TILT_X_OFFSET}m for camera view")
        self._send_feedback("Adjusting position for camera view...")
        success = self.manipulation.move_to_cartesian_pose(
            x=tilt_x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=1.5
        )
        if not success:
            return "Failed to apply tilt offset", SkillResult.FAILURE
        time.sleep(2.0)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 4: Capture image
        self.logger.info("[RecalibrateBoard] Step 4: Capturing image")
        self._send_feedback("Capturing image...")
        time.sleep(0.5)

        if not self.image:
            return "No wrist camera image available", SkillResult.FAILURE

        # Save raw capture
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_path = CAPTURES_DIR / f"recalib_raw_{ts}.jpg"
            raw_path.write_bytes(base64.b64decode(self.image))
            self.logger.info(f"[RecalibrateBoard] Raw image saved: {raw_path}")
        except Exception as e:
            self.logger.warning(f"[RecalibrateBoard] Failed to save raw image: {e}")

        # Step 5: Ask Gemini to find the H8 square
        self.logger.info("[RecalibrateBoard] Step 5: Asking Gemini to find H8")
        self._send_feedback("Analyzing with Gemini...")

        correction = self._find_h8_correction(calibration, x, y)

        if correction is None:
            return "Gemini could not find H8 square", SkillResult.FAILURE

        corr_x, corr_y = correction

        if abs(corr_x) < 0.001 and abs(corr_y) < 0.001:
            msg = f"Calibration already accurate (dX={corr_x:+.4f}m dY={corr_y:+.4f}m)"
            self.logger.info(f"[RecalibrateBoard] {msg}")
            self._send_feedback(msg)
            return msg, SkillResult.SUCCESS

        # Step 6: Update calibration
        self.logger.info(
            f"[RecalibrateBoard] Step 6: Updating calibration dX={corr_x:+.4f} dY={corr_y:+.4f}"
        )
        self._send_feedback(f"Updating calibration by dX={corr_x*100:+.1f}cm dY={corr_y*100:+.1f}cm")

        updated = {}
        for corner in ["top_left", "top_right", "bottom_left", "bottom_right"]:
            c = calibration[corner]
            updated[corner] = {
                "x": c["x"] + corr_x,
                "y": c["y"] + corr_y,
                "z": c["z"]
            }

        try:
            CALIBRATION_FILE.write_text(json.dumps(updated, indent=2))
            self.logger.info("[RecalibrateBoard] Calibration file saved")
        except Exception as e:
            return f"Failed to save calibration: {e}", SkillResult.FAILURE

        # Step 7: Return arm to safe pose
        self.logger.info("[RecalibrateBoard] Step 7: Returning to safe pose")
        self._send_feedback("Returning to safe pose...")
        self.manipulation.move_to_cartesian_pose(
            x=0.15, y=0.1, z=0.1,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        time.sleep(2.5)

        error_cm = (corr_x**2 + corr_y**2) ** 0.5 * 100
        msg = (
            f"Recalibrated: shifted all corners by dX={corr_x*100:+.1f}cm dY={corr_y*100:+.1f}cm "
            f"(estimated error was {error_cm:.1f}cm)"
        )
        self.logger.info(f"[RecalibrateBoard] {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def _find_h8_correction(self, calibration: dict, x: float, y: float):
        """Ask Gemini to find the H8 square bbox and compute correction.

        Returns (x_correction, y_correction) or None on failure.
        Spatial mapping: image top=+X, image right=-Y.
        """
        try:
            from PIL import Image, ImageDraw

            img = Image.open(io.BytesIO(base64.b64decode(self.image)))
            w, h = img.size
            img_cx, img_cy = w // 2, h // 2

            # Draw red dot at image center for reference
            img_for_gemini = img.copy()
            draw_gemini = ImageDraw.Draw(img_for_gemini)
            draw_gemini.ellipse([img_cx - 7, img_cy - 7, img_cx + 7, img_cy + 7],
                                fill='red', outline='white', width=2)
            buf_gemini = io.BytesIO()
            img_for_gemini.save(buf_gemini, format='JPEG', quality=90)
            annotated_b64 = base64.b64encode(buf_gemini.getvalue()).decode('utf-8')

            prompt = (
                "This image shows a chessboard from above. There is a red dot on the board. "
                "Find the top-right square of the chessboard. "
                "Return the bounding box of that square. "
                "Return ONLY JSON with one key: "
                "\"box_2d\" (array of [ymin, xmin, ymax, xmax] normalized 0-1000)."
            )

            response = self.gemini_client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[
                    prompt,
                    types.Part.from_bytes(
                        data=base64.b64decode(annotated_b64),
                        mime_type="image/jpeg",
                    ),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=128),
                ),
            )
            result = json.loads(response.text.strip())
            box_2d = result.get("box_2d", [0, 0, 0, 0])

            self.logger.info(f"[RecalibrateBoard] Gemini response: {result}")

            if not box_2d or len(box_2d) != 4 or not any(v > 0 for v in box_2d):
                self.logger.warning(f"[RecalibrateBoard] Invalid bbox: {box_2d}")
                return None

            # Denormalize bbox [ymin, xmin, ymax, xmax] -> pixel coords
            y1 = int(box_2d[0] / 1000 * h)
            x1 = int(box_2d[1] / 1000 * w)
            y2 = int(box_2d[2] / 1000 * h)
            x2 = int(box_2d[3] / 1000 * w)
            bbox_cx = (x1 + x2) // 2
            bbox_cy = (y1 + y2) // 2
            bbox_w_px = max(x2 - x1, 1)
            bbox_h_px = max(y2 - y1, 1)

            dist_px = ((img_cx - bbox_cx) ** 2 + (img_cy - bbox_cy) ** 2) ** 0.5

            # Compute meters-per-pixel using bbox as one square
            sq_size_x = self._compute_square_size_x(calibration)
            sq_size_y = self._compute_square_size_y(calibration)
            m_per_px_vert = sq_size_x / bbox_h_px
            m_per_px_horiz = sq_size_y / bbox_w_px

            # Pixel offset: bbox center relative to image center
            dx_px = bbox_cx - img_cx  # positive = bbox is right of center
            dy_px = bbox_cy - img_cy  # positive = bbox is below center

            # Convert to robot coordinates (unrotated)
            # Image top = +X, so bbox above center (dy_px < 0) means move +X
            raw_x = -dy_px * m_per_px_vert
            # Image right = -Y, so bbox right of center (dx_px > 0) means move -Y
            raw_y = -dx_px * m_per_px_horiz

            # Rotate correction by arm reach angle (5DOF arm rotates camera in XY plane)
            angle = math.atan2(-(y - self.ARM_ORIGIN_Y), x - self.ARM_ORIGIN_X)
            x_correction = math.cos(angle) * raw_x - math.sin(angle) * raw_y
            y_correction = math.sin(angle) * raw_x + math.cos(angle) * raw_y
            self.logger.info(f"[RecalibrateBoard] Arm reach angle: {math.degrees(angle):.1f}deg")

            dist_m = (x_correction**2 + y_correction**2) ** 0.5

            self.logger.info(
                f"[RecalibrateBoard] H8 bbox: box_2d={box_2d} "
                f"px=[{x1},{y1},{x2},{y2}] bbox_center=({bbox_cx},{bbox_cy}) "
                f"img_center=({img_cx},{img_cy}) dist={dist_px:.0f}px"
            )
            self.logger.info(
                f"[RecalibrateBoard] Correction: dx_px={dx_px} dy_px={dy_px} "
                f"-> x_corr={x_correction:+.4f}m y_corr={y_correction:+.4f}m "
                f"(dist={dist_m*100:.1f}cm)"
            )

            # Draw annotated image
            draw = ImageDraw.Draw(img)

            # Image center (red crosshair)
            draw.ellipse([img_cx - 7, img_cy - 7, img_cx + 7, img_cy + 7],
                         fill='red', outline='white', width=2)
            draw.line([(img_cx - 16, img_cy), (img_cx + 16, img_cy)], fill='red', width=2)
            draw.line([(img_cx, img_cy - 16), (img_cx, img_cy + 16)], fill='red', width=2)

            # H8 bbox (yellow)
            draw.rectangle([x1, y1, x2, y2], outline='yellow', width=3)

            # Bbox center (lime)
            draw.ellipse([bbox_cx - 5, bbox_cy - 5, bbox_cx + 5, bbox_cy + 5],
                         fill='lime', outline='white', width=1)

            # Line from image center to bbox center (cyan)
            draw.line([(img_cx, img_cy), (bbox_cx, bbox_cy)], fill='cyan', width=2)

            # Label
            label = f"H8 dist={dist_px:.0f}px corr=({x_correction:+.3f},{y_correction:+.3f})m"
            draw.text((10, 10), label, fill='cyan')

            # Save
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            path = CAPTURES_DIR / f"recalib_bbox_{ts}.jpg"
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            path.write_bytes(buf.getvalue())
            self.logger.info(f"[RecalibrateBoard] Bbox visualization saved: {path}")

            return (x_correction, y_correction)

        except Exception as e:
            self.logger.error(f"[RecalibrateBoard] H8 analysis failed: {e}")
            return None

    def cancel(self):
        self._cancelled = True
        return "Recalibrate board cancelled"
