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
    """Hover above the believed H8 and A8 squares, use Gemini to locate
    the actual corners, and recompute the full board calibration using
    square geometry."""

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
            "Recalibrate the chessboard by hovering above the believed H8 and A8 squares, "
            "using Gemini vision to find their actual positions, and recomputing all four "
            "corners from square geometry. Run this when piece placement is drifting."
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

    # ── Helpers ──────────────────────────────────────────────────────

    def _go_to_safe_pose(self, tilted_pitch, yaw):
        """Return arm to the resting safe pose."""
        self.manipulation.move_to_cartesian_pose(
            x=0.15, y=0.1, z=0.1,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        time.sleep(2.5)

    def _get_capture_joint_angles(self):
        """Read joint 1 (base) and joint 5 (wrist yaw) positions in radians.

        Joint 1: 0 = straight ahead, negative = looking left, positive = right.
        Joint 5: 0 = neutral yaw.

        Returns (joint1_rad, joint5_rad).
        """
        j1, j5 = 0.0, 0.0
        try:
            self.manipulation.spin_node_to_refresh_topics(count=5, timeout_sec=0.01)
            if self.manipulation._arm_state is not None:
                pos = self.manipulation._arm_state.position
                if len(pos) >= 1:
                    j1 = pos[0]
                if len(pos) >= 5:
                    j5 = pos[4]
                self.logger.info(
                    f"[RecalibrateBoard] Joints at capture: j1={j1:.4f}rad ({math.degrees(j1):.1f}deg) "
                    f"j5={j5:.4f}rad ({math.degrees(j5):.1f}deg)"
                )
        except Exception as e:
            self.logger.warning(f"[RecalibrateBoard] Failed to read joint angles: {e}")
        return j1, j5

    def _move_to_corner_and_capture(self, x, y, corner_name, tilted_pitch, yaw):
        """Move above a corner, apply tilt offset, capture image, return to safe pose.

        Returns (captured_b64, joint1_rad, joint5_rad) tuple, or (None, 0.0, 0.0) on failure.
        """
        self.logger.info(f"[RecalibrateBoard] Moving above believed {corner_name} at X={x:.4f}, Y={y:.4f}")
        self._send_feedback(f"Moving above {corner_name}...")

        # Safe height
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        if not success:
            self.logger.warning(f"[RecalibrateBoard] Failed to move to safe height for {corner_name}")
            return None, 0.0, 0.0
        time.sleep(2.5)

        if self._cancelled:
            return None, 0.0, 0.0

        # Lower to capture height
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=2.0
        )
        if not success:
            self.logger.warning(f"[RecalibrateBoard] Failed to move above {corner_name}")
            return None, 0.0, 0.0
        time.sleep(2.5)

        if self._cancelled:
            return None, 0.0, 0.0

        # Apply camera tilt X offset
        tilt_x = x + self.CAMERA_TILT_X_OFFSET
        success = self.manipulation.move_to_cartesian_pose(
            x=tilt_x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=1.5
        )
        if not success:
            self.logger.warning(f"[RecalibrateBoard] Failed to apply tilt offset for {corner_name}")
            return None, 0.0, 0.0
        time.sleep(2.0)

        if self._cancelled:
            return None, 0.0, 0.0

        # Read joint angles at capture time
        joint1_rad, joint5_rad = self._get_capture_joint_angles()

        # Capture
        self.logger.info(f"[RecalibrateBoard] Capturing image at {corner_name}...")
        self._send_feedback(f"Capturing image at {corner_name}...")
        time.sleep(0.5)

        if not self.image:
            self.logger.warning(f"[RecalibrateBoard] No image available at {corner_name}")
            return None, 0.0, 0.0

        captured_b64 = self.image

        # Save raw capture
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_path = CAPTURES_DIR / f"recalib_raw_{corner_name}_{ts}.jpg"
            raw_path.write_bytes(base64.b64decode(captured_b64))
            self.logger.info(f"[RecalibrateBoard] Raw image saved: {raw_path}")
        except Exception as e:
            self.logger.warning(f"[RecalibrateBoard] Failed to save raw image: {e}")

        # Return to safe pose
        self.logger.info(f"[RecalibrateBoard] Returning to safe pose after {corner_name} capture")
        self._send_feedback("Returning to safe pose...")
        self._go_to_safe_pose(tilted_pitch, yaw)

        return captured_b64, joint1_rad, joint5_rad

    def _recompute_calibration_from_top_corners(self, new_a8, new_h8, z):
        """Given corrected A8 (top_left) and H8 (top_right) positions,
        derive A1 (bottom_left) and H1 (bottom_right) using square geometry.

        The board is a square: the bottom edge is obtained by rotating the
        top edge (A8→H8) 90° clockwise (toward the robot, i.e. -X direction).
        """
        # Top side vector: A8 → H8
        side_x = new_h8[0] - new_a8[0]
        side_y = new_h8[1] - new_a8[1]

        # Perpendicular vector pointing toward bottom of board (-X direction)
        # Rotate (side_x, side_y) by -90° → (side_y, -side_x)
        down_x = side_y
        down_y = -side_x

        # Bottom corners
        a1_x = new_a8[0] + down_x
        a1_y = new_a8[1] + down_y
        h1_x = new_h8[0] + down_x
        h1_y = new_h8[1] + down_y

        updated = {
            "top_left":     {"x": new_a8[0], "y": new_a8[1], "z": z},
            "top_right":    {"x": new_h8[0], "y": new_h8[1], "z": z},
            "bottom_left":  {"x": a1_x, "y": a1_y, "z": z},
            "bottom_right": {"x": h1_x, "y": h1_y, "z": z},
        }

        side_len = math.sqrt(side_x**2 + side_y**2)
        self.logger.info(
            f"[RecalibrateBoard] Square geometry: side={side_len*100:.1f}cm "
            f"A8=({new_a8[0]:.4f},{new_a8[1]:.4f}) H8=({new_h8[0]:.4f},{new_h8[1]:.4f}) "
            f"A1=({a1_x:.4f},{a1_y:.4f}) H1=({h1_x:.4f},{h1_y:.4f})"
        )

        return updated

    # ── Main execute ─────────────────────────────────────────────────

    def execute(self):
        """
        Recalibrate by capturing images at believed H8 and A8, computing
        corrections with Gemini, and recomputing all four corners using
        square geometry.
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        if not self.gemini_client:
            return "Gemini not configured", SkillResult.FAILURE

        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found", SkillResult.FAILURE

        tilted_pitch = self.FIXED_PITCH + self.CAMERA_PITCH_OFFSET
        yaw = self.FIXED_YAW

        tr = calibration["top_right"]
        tl = calibration["top_left"]
        h8_x, h8_y = tr["x"], tr["y"]
        a8_x, a8_y = tl["x"], tl["y"]
        z = tr["z"]

        self.logger.info(
            f"[RecalibrateBoard] Believed H8=({h8_x:.4f},{h8_y:.4f}) A8=({a8_x:.4f},{a8_y:.4f})"
        )

        # ── Phase 1: Capture at H8, then A8 (return to safe after each) ──

        self._send_feedback("Step 1: Capturing H8...")
        h8_b64, h8_j1, h8_j5 = self._move_to_corner_and_capture(h8_x, h8_y, "H8", tilted_pitch, yaw)
        if h8_b64 is None:
            return "Failed to capture H8 image", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        self._send_feedback("Step 2: Capturing A8...")
        a8_b64, a8_j1, a8_j5 = self._move_to_corner_and_capture(a8_x, a8_y, "A8", tilted_pitch, yaw)
        if a8_b64 is None:
            return "Failed to capture A8 image", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # ── Phase 2: Gemini analysis for both corners (arm is safe) ──

        self.logger.info("[RecalibrateBoard] Step 3: Analyzing H8 with Gemini")
        self._send_feedback("Analyzing H8 with Gemini...")
        h8_corr = self._find_corner_correction(
            calibration, h8_x, h8_y, h8_b64,
            corner_name="H8",
            prompt_corner="top-right",
            label="before_H8",
            joint1_rad=h8_j1,
            joint5_rad=h8_j5,
        )

        self.logger.info("[RecalibrateBoard] Step 4: Analyzing A8 with Gemini")
        self._send_feedback("Analyzing A8 with Gemini...")
        a8_corr = self._find_corner_correction(
            calibration, a8_x, a8_y, a8_b64,
            corner_name="A8",
            prompt_corner="top-left",
            label="before_A8",
            joint1_rad=a8_j1,
            joint5_rad=a8_j5,
        )

        if h8_corr is None and a8_corr is None:
            return "Gemini could not find either corner", SkillResult.FAILURE

        # Apply corrections to get actual corner positions
        h8_cx, h8_cy = h8_corr if h8_corr else (0.0, 0.0)
        a8_cx, a8_cy = a8_corr if a8_corr else (0.0, 0.0)

        new_h8 = (h8_x + h8_cx, h8_y + h8_cy)
        new_a8 = (a8_x + a8_cx, a8_y + a8_cy)

        self.logger.info(
            f"[RecalibrateBoard] H8 correction: ({h8_cx:+.4f}, {h8_cy:+.4f})m "
            f"A8 correction: ({a8_cx:+.4f}, {a8_cy:+.4f})m"
        )
        self._send_feedback(
            f"H8 shift: dX={h8_cx*100:+.1f}cm dY={h8_cy*100:+.1f}cm | "
            f"A8 shift: dX={a8_cx*100:+.1f}cm dY={a8_cy*100:+.1f}cm"
        )

        # ── Phase 3: Recompute full calibration from square geometry ──

        self.logger.info("[RecalibrateBoard] Step 5: Recomputing calibration from square geometry")
        self._send_feedback("Recomputing calibration from square geometry...")

        updated = self._recompute_calibration_from_top_corners(new_a8, new_h8, z)

        try:
            CALIBRATION_FILE.write_text(json.dumps(updated, indent=2))
            self.logger.info("[RecalibrateBoard] Calibration file saved")
        except Exception as e:
            return f"Failed to save calibration: {e}", SkillResult.FAILURE

        # ── Phase 4: Verification captures at new H8 and A8 ──

        new_h8_x, new_h8_y = new_h8
        new_a8_x, new_a8_y = new_a8

        self._send_feedback("Step 6: Verification — capturing new H8...")
        v_h8_b64, v_h8_j1, v_h8_j5 = self._move_to_corner_and_capture(new_h8_x, new_h8_y, "H8_verify", tilted_pitch, yaw)

        self._send_feedback("Step 7: Verification — capturing new A8...")
        v_a8_b64, v_a8_j1, v_a8_j5 = self._move_to_corner_and_capture(new_a8_x, new_a8_y, "A8_verify", tilted_pitch, yaw)

        # ── Phase 5: Verification Gemini analysis (no recalibration) ──

        if v_h8_b64:
            self.logger.info("[RecalibrateBoard] Step 8: Verification Gemini — H8")
            self._send_feedback("Verifying H8 with Gemini...")
            v_h8 = self._find_corner_correction(
                updated, new_h8_x, new_h8_y, v_h8_b64,
                corner_name="H8", prompt_corner="top-right", label="after_H8",
                joint1_rad=v_h8_j1, joint5_rad=v_h8_j5,
            )
            if v_h8:
                r = (v_h8[0]**2 + v_h8[1]**2) ** 0.5 * 100
                self._send_feedback(f"H8 residual: dX={v_h8[0]*100:+.1f}cm dY={v_h8[1]*100:+.1f}cm ({r:.1f}cm)")

        if v_a8_b64:
            self.logger.info("[RecalibrateBoard] Step 9: Verification Gemini — A8")
            self._send_feedback("Verifying A8 with Gemini...")
            v_a8 = self._find_corner_correction(
                updated, new_a8_x, new_a8_y, v_a8_b64,
                corner_name="A8", prompt_corner="top-left", label="after_A8",
                joint1_rad=v_a8_j1, joint5_rad=v_a8_j5,
            )
            if v_a8:
                r = (v_a8[0]**2 + v_a8[1]**2) ** 0.5 * 100
                self._send_feedback(f"A8 residual: dX={v_a8[0]*100:+.1f}cm dY={v_a8[1]*100:+.1f}cm ({r:.1f}cm)")

        h8_err = (h8_cx**2 + h8_cy**2) ** 0.5 * 100
        a8_err = (a8_cx**2 + a8_cy**2) ** 0.5 * 100
        msg = (
            f"Recalibrated from square geometry. "
            f"H8 error was {h8_err:.1f}cm, A8 error was {a8_err:.1f}cm."
        )
        self.logger.info(f"[RecalibrateBoard] {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    # ── Gemini corner analysis ───────────────────────────────────────

    def _find_corner_correction(self, calibration, x, y, image_b64,
                                corner_name, prompt_corner, label,
                                joint1_rad=0.0, joint5_rad=0.0):
        """Ask Gemini for the bbox of a specific corner square and compute
        the correction offset.

        Args:
            calibration: current calibration dict (for square size)
            x, y: believed corner position in robot coords
            image_b64: base64-encoded JPEG captured at that corner
            corner_name: display name (e.g. "H8")
            prompt_corner: description for Gemini (e.g. "top-right")
            label: filename label for saved visualization
            joint1_rad: base joint angle in radians at capture time
                        (0 = straight, negative = left, positive = right)
            joint5_rad: wrist yaw angle in radians at capture time
                        (0 = neutral)

        Returns (x_correction, y_correction) or None on failure.
        """
        try:
            from PIL import Image, ImageDraw

            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
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
                f"This image shows a chessboard from above. There is a red dot on the board. "
                f"Find the {prompt_corner} square of the chessboard. "
                f"Return the bounding box of that square. "
                f"Return ONLY JSON with one key: "
                f"\"box_2d\" (array of [ymin, xmin, ymax, xmax] normalized 0-1000)."
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

            self.logger.info(f"[RecalibrateBoard] {corner_name} Gemini response: {result}")

            if not box_2d or len(box_2d) != 4 or not any(v > 0 for v in box_2d):
                self.logger.warning(f"[RecalibrateBoard] Invalid bbox for {corner_name}: {box_2d}")
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

            # Convert to robot coordinates (camera frame, assuming straight ahead)
            # Image top = +X, so bbox above center (dy_px < 0) means move +X
            raw_x = -dy_px * m_per_px_vert
            # Image right = -Y, so bbox right of center (dx_px > 0) means move -Y
            raw_y = -dx_px * m_per_px_horiz

            # Effective image rotation from joint angles.
            # Camera is ~0.48 rad from vertical (CAMERA_PITCH_OFFSET).
            # Joint 1 (base) contribution is scaled by cos(tilt_from_vertical).
            # Joint 5 (wrist yaw) is subtracted (opposes base rotation in image).
            tilt_from_vertical = abs(self.CAMERA_PITCH_OFFSET)  # 0.48 rad
            alpha = joint1_rad * math.cos(tilt_from_vertical) - joint5_rad
            self.logger.info(
                f"[RecalibrateBoard] {corner_name} effective rotation: "
                f"j1={math.degrees(joint1_rad):.1f}deg * cos({tilt_from_vertical:.2f})={joint1_rad * math.cos(tilt_from_vertical):.4f} "
                f"- j5={math.degrees(joint5_rad):.1f}deg = alpha={math.degrees(alpha):.1f}deg"
            )

            # Clockwise rotation by alpha to transform camera-frame correction
            # into robot-frame correction.
            x_correction = math.cos(alpha) * raw_x + math.sin(alpha) * raw_y
            y_correction = -math.sin(alpha) * raw_x + math.cos(alpha) * raw_y
            self.logger.info(
                f"[RecalibrateBoard] {corner_name} "
                f"raw=({raw_x:+.4f},{raw_y:+.4f}) -> rotated=({x_correction:+.4f},{y_correction:+.4f})"
            )

            dist_m = (x_correction**2 + y_correction**2) ** 0.5

            self.logger.info(
                f"[RecalibrateBoard] {corner_name} bbox: box_2d={box_2d} "
                f"px=[{x1},{y1},{x2},{y2}] bbox_center=({bbox_cx},{bbox_cy}) "
                f"img_center=({img_cx},{img_cy}) dist={dist_px:.0f}px"
            )
            self.logger.info(
                f"[RecalibrateBoard] {corner_name} correction: dx_px={dx_px} dy_px={dy_px} "
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

            # Corner bbox (yellow)
            draw.rectangle([x1, y1, x2, y2], outline='yellow', width=3)

            # Bbox center (lime)
            draw.ellipse([bbox_cx - 5, bbox_cy - 5, bbox_cx + 5, bbox_cy + 5],
                         fill='lime', outline='white', width=1)

            # Line from image center to bbox center (cyan)
            draw.line([(img_cx, img_cy), (bbox_cx, bbox_cy)], fill='cyan', width=2)

            # True X axis (orange) — vertical line rotated by effective angle alpha.
            # In image coords: unrotated X axis points up (dx=0, dy=-1).
            # Clockwise rotation by alpha: dx = sin(alpha), dy = -cos(alpha).
            axis_len = min(w, h) // 3
            ax_dx = math.sin(alpha) * axis_len
            ax_dy = -math.cos(alpha) * axis_len
            # Draw from image center outward in both directions
            draw.line(
                [(img_cx - ax_dx, img_cy - ax_dy),
                 (img_cx + ax_dx, img_cy + ax_dy)],
                fill='orange', width=2,
            )
            # Small arrowhead at the "forward" end (the +X direction)
            draw.ellipse(
                [img_cx + ax_dx - 4, img_cy + ax_dy - 4,
                 img_cx + ax_dx + 4, img_cy + ax_dy + 4],
                fill='orange', outline='white', width=1,
            )
            # Axis label near the forward tip
            draw.text(
                (int(img_cx + ax_dx) + 6, int(img_cy + ax_dy) - 10),
                f"X ({math.degrees(alpha):+.1f}°)",
                fill='orange',
            )

            # Label
            text = f"{corner_name} dist={dist_px:.0f}px corr=({x_correction:+.3f},{y_correction:+.3f})m"
            draw.text((10, 10), text, fill='cyan')

            # Save
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            path = CAPTURES_DIR / f"recalib_{label}_{ts}.jpg"
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            path.write_bytes(buf.getvalue())
            self.logger.info(f"[RecalibrateBoard] {corner_name} visualization saved: {path}")

            return (x_correction, y_correction)

        except Exception as e:
            self.logger.error(f"[RecalibrateBoard] {corner_name} analysis failed: {e}")
            return None

    def cancel(self):
        self._cancelled = True
        return "Recalibrate board cancelled"
