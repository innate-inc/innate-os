#!/usr/bin/env python3
"""
Calibrate Drive Skill - Measure driving accuracy using Gemini vision
before and after driving backward. Saves a correction scale factor.
"""

import base64
import io
import json
import math
import time
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType

from PIL import Image, ImageDraw
from google import genai
from google.genai import types


CALIBRATION_FILE = Path.home() / "board_calibration.json"
POSITION_STATE_FILE = Path.home() / "robot_position_state.json"
DRIVE_CALIBRATION_FILE = Path.home() / "drive_calibration.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/gemini"

DRIVE_SPEED = 0.02
DRIVE_SQUARES = 3


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


class CalibrateDrive(Skill):
    """Measure driving accuracy using Gemini vision on A1 before/after driving."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    mobility = Interface(InterfaceType.MOBILITY)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57
    CAMERA_PITCH_OFFSET = -0.48
    CAMERA_TILT_X_OFFSET = -0.02
    ARM_ORIGIN_X = 0.05
    ARM_ORIGIN_Y = -0.05
    HEIGHT_SAFE = 0.2
    HEIGHT_ABOVE = 0.15

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._init_gemini()

    def _init_gemini(self):
        env_vars = _load_env_file(Path(__file__).parent / ".env.scan")
        api_key = env_vars.get("GEMINI_API_KEY", "")
        self.gemini_client = None
        if api_key and api_key != "your_gemini_api_key_here":
            self.gemini_client = genai.Client(api_key=api_key)

    @property
    def name(self):
        return "calibrate_drive"

    def guidelines(self):
        return (
            "Calibrate driving accuracy by looking at A1 before and after driving backward. "
            "Saves a scale factor to ~/drive_calibration.json."
        )

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except:
            return None

    def _load_position_state(self) -> float:
        try:
            return float(json.loads(POSITION_STATE_FILE.read_text()).get("offset_x", 0.0))
        except:
            return 0.0

    def _save_position_state(self, offset_x: float):
        POSITION_STATE_FILE.write_text(json.dumps({"offset_x": offset_x}))

    def _compute_square_size_x(self, cal: dict) -> float:
        return ((cal["top_left"]["x"] + cal["top_right"]["x"]) / 2.0 -
                (cal["bottom_left"]["x"] + cal["bottom_right"]["x"]) / 2.0) / 7.0

    def _compute_square_size_y(self, cal: dict) -> float:
        return ((cal["top_left"]["y"] + cal["bottom_left"]["y"]) / 2.0 -
                (cal["top_right"]["y"] + cal["bottom_right"]["y"]) / 2.0) / 7.0

    def _drive_to_offset(self, target: float, current: float) -> float:
        delta = target - current
        if abs(delta) < 0.001 or self.mobility is None:
            return current
        direction = 1.0 if delta > 0 else -1.0
        dur = abs(delta) / DRIVE_SPEED
        self._send_feedback(f"Driving {'fwd' if direction > 0 else 'back'} {abs(delta)*100:.1f}cm...")
        self.mobility.send_cmd_vel(linear_x=direction * DRIVE_SPEED, angular_z=0.0, duration=dur)
        start = time.time()
        while time.time() - start < dur:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                partial = current + direction * DRIVE_SPEED * (time.time() - start)
                self._save_position_state(partial)
                return partial
            time.sleep(0.1)
        self._save_position_state(target)
        return target

    def _move_arm(self, x, y, z, pitch, yaw, duration, wait=None):
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z, roll=self.FIXED_ROLL, pitch=pitch, yaw=yaw, duration=duration)
        if success and wait:
            time.sleep(wait)
        return success

    def _capture_and_measure(self, label, cal, x, y, pitch, yaw):
        """Apply tilt, capture, measure A1 with Gemini. Returns (corr_x, corr_y) or None."""
        tilt_x = x + self.CAMERA_TILT_X_OFFSET
        self._move_arm(tilt_x, y, self.HEIGHT_ABOVE, pitch, yaw, 2, wait=2.5)
        self._send_feedback(f"Capturing {label}...")
        time.sleep(0.5)
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (CAPTURES_DIR / f"drive_{label}_{ts}.jpg").write_bytes(base64.b64decode(self.image))
        except:
            pass
        self._send_feedback(f"Analyzing {label} with Gemini...")
        return self._find_a1_correction(cal, x, y)

    def _find_a1_correction(self, cal, x, y):
        """Ask Gemini for A1 bbox and compute correction. Returns (corr_x, corr_y) or None."""
        try:
            img = Image.open(io.BytesIO(base64.b64decode(self.image)))
            w, h = img.size
            cx, cy = w // 2, h // 2

            img_g = img.copy()
            ImageDraw.Draw(img_g).ellipse([cx-7, cy-7, cx+7, cy+7], fill='red', outline='white', width=2)
            buf = io.BytesIO()
            img_g.save(buf, format='JPEG', quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode()

            response = self.gemini_client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[
                    "This image shows a chessboard from above. There is a red dot. "
                    "Find the bottom-left square of the chessboard. "
                    "Return ONLY JSON: {\"box_2d\": [ymin, xmin, ymax, xmax]} normalized 0-1000.",
                    types.Part.from_bytes(data=base64.b64decode(b64), mime_type="image/jpeg"),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=128),
                ),
            )
            box = json.loads(response.text.strip()).get("box_2d", [0,0,0,0])
            if not box or len(box) != 4 or not any(v > 0 for v in box):
                return None

            y1, x1, y2, x2 = [int(box[i]/1000*(h if i%2==0 else w)) for i in range(4)]
            bcx, bcy = (x1+x2)//2, (y1+y2)//2

            sq_x = self._compute_square_size_x(cal)
            sq_y = self._compute_square_size_y(cal)
            m_v = sq_x / max(y2-y1, 1)
            m_h = sq_y / max(x2-x1, 1)

            raw_x = -(bcy - cy) * m_v
            raw_y = -(bcx - cx) * m_h

            angle = math.atan2(-(y - self.ARM_ORIGIN_Y), x - self.ARM_ORIGIN_X)
            corr_x = math.cos(angle)*raw_x - math.sin(angle)*raw_y
            corr_y = math.sin(angle)*raw_x + math.cos(angle)*raw_y

            self.logger.info(f"[CalibrateDrive] A1 corr=({corr_x:+.4f},{corr_y:+.4f})m angle={math.degrees(angle):.1f}")

            # Save annotated
            draw = ImageDraw.Draw(img)
            draw.ellipse([cx-7, cy-7, cx+7, cy+7], fill='red', outline='white', width=2)
            draw.rectangle([x1, y1, x2, y2], outline='yellow', width=3)
            draw.ellipse([bcx-5, bcy-5, bcx+5, bcy+5], fill='lime', outline='white', width=1)
            draw.line([(cx, cy), (bcx, bcy)], fill='cyan', width=2)
            draw.text((10, 10), f"A1 corr=({corr_x:+.3f},{corr_y:+.3f})m", fill='cyan')
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=90)
            (CAPTURES_DIR / f"drive_bbox_{ts}.jpg").write_bytes(out.getvalue())

            return (corr_x, corr_y)
        except Exception as e:
            self.logger.error(f"[CalibrateDrive] A1 analysis failed: {e}")
            return None

    def execute(self):
        """Calibrate driving by measuring A1 before and after driving backward."""
        self._cancelled = False
        cal = self._load_calibration()
        if cal is None:
            return "No calibration data found", SkillResult.FAILURE

        pitch = self.FIXED_PITCH + self.CAMERA_PITCH_OFFSET
        yaw = self.FIXED_YAW
        ref_x, ref_y = cal["bottom_left"]["x"], cal["bottom_left"]["y"]
        sq_size = self._compute_square_size_x(cal)
        drive_dist = DRIVE_SQUARES * sq_size

        self.logger.info(f"[CalibrateDrive] A1=({ref_x:.4f},{ref_y:.4f}) drive={drive_dist*100:.1f}cm")

        # 1. Ensure at offset 0
        offset = self._load_position_state()
        offset = self._drive_to_offset(0.0, offset)

        # 2. Move above A1, measure baseline
        self._send_feedback("Measuring baseline at A1...")
        self._move_arm(ref_x, ref_y, self.HEIGHT_SAFE, pitch, yaw, 1.5, wait=1.5)
        self._move_arm(ref_x, ref_y, self.HEIGHT_ABOVE, pitch, yaw, 2, wait=2.5)
        before = self._capture_and_measure("before", cal, ref_x, ref_y, pitch, yaw)
        if before is None:
            return "Failed to measure baseline at A1", SkillResult.FAILURE
        self.logger.info(f"[CalibrateDrive] Before: corr_x={before[0]:+.4f}m")

        # 3. Lift and drive backward
        self._move_arm(ref_x, ref_y, self.HEIGHT_SAFE, pitch, yaw, 1.5, wait=1.5)
        offset = self._drive_to_offset(-drive_dist, offset)

        # 4. Move above A1 (adjusted), measure after
        x_adj = ref_x - offset
        self._send_feedback("Measuring A1 after driving...")
        self._move_arm(x_adj, ref_y, self.HEIGHT_SAFE, pitch, yaw, 1.5, wait=1.5)
        self._move_arm(x_adj, ref_y, self.HEIGHT_ABOVE, pitch, yaw, 2, wait=2.5)
        after = self._capture_and_measure("after", cal, x_adj, ref_y, pitch, yaw)
        if after is None:
            self._drive_to_offset(0.0, offset)
            return "Failed to measure A1 after driving", SkillResult.FAILURE
        self.logger.info(f"[CalibrateDrive] After: corr_x={after[0]:+.4f}m")

        # 5. Compute scale factor
        drive_error = after[0] - before[0]
        actual = drive_dist + drive_error
        scale = actual / drive_dist if drive_dist > 0 else 1.0
        self.logger.info(
            f"[CalibrateDrive] Error={drive_error*100:+.2f}cm "
            f"commanded={drive_dist*100:.1f}cm actual={actual*100:.1f}cm scale={scale:.4f}"
        )

        # 6. Save
        DRIVE_CALIBRATION_FILE.write_text(json.dumps({"scale_factor": round(scale, 4)}, indent=2))
        self.logger.info(f"[CalibrateDrive] Saved to {DRIVE_CALIBRATION_FILE}")

        # 7. Drive back and return to safe pose
        self._move_arm(x_adj, ref_y, self.HEIGHT_SAFE, pitch, yaw, 1.5, wait=1.5)
        self._drive_to_offset(0.0, offset)
        self._move_arm(0.15, 0.1, 0.1, pitch, yaw, 2, wait=2.5)

        error_cm = abs(drive_error) * 100
        msg = (
            f"Drive calibration done. Error: {error_cm:.1f}cm over {drive_dist*100:.1f}cm. "
            f"Scale factor: {scale:.4f}. Saved to ~/drive_calibration.json"
        )
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Drive calibration cancelled"
