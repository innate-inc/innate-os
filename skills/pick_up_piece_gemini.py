#!/usr/bin/env python3
"""
Pick Up Piece Gemini Skill - Move above a chess square, tilt the wrist camera, and capture an image.
"""

import base64
import io
import json
import time
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType

from google import genai
from google.genai import types


CALIBRATION_FILE = Path.home() / "board_calibration.json"
POSITION_STATE_FILE = Path.home() / "robot_position_state.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/gemini"

# Max base driving speed in m/s
DRIVE_SPEED = 0.02
# Number of squares to offset by when driving
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


class PickUpPieceGemini(Skill):
    """Move above a chess square, tilt camera to look at it, and capture an image."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    mobility = Interface(InterfaceType.MOBILITY)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    # Fixed orientation - pointing downward
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57

    # Pitch offset for tilting camera to look at the square
    CAMERA_PITCH_OFFSET = -0.48
    CAMERA_TILT_X_OFFSET = -0.02  # shift back 2.5cm when tilting to compensate

    # Heights in meters
    HEIGHT_SAFE = 0.2   # 20cm safe travel height (won't knock pieces)
    HEIGHT_ABOVE = 0.15  # 10cm above board for positioning
    HEIGHT_PICK = 0.08       # 8cm - pick height for tall pieces
    HEIGHT_PICK_PAWN = 0.05   # 5cm - pick height for pawns (2cm lower)

    # Gripper parameters
    GRIPPER_OPEN_PERCENT = 40
    GRIPPER_CLOSE_STRENGTH = 0.4

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
            self.logger.info("[PickUpPieceGemini] Gemini configured (google-genai SDK)")

    def _load_position_state(self) -> float:
        """Load current robot X offset from state file. 0.0 = calibration origin."""
        if not POSITION_STATE_FILE.exists():
            return 0.0
        try:
            data = json.loads(POSITION_STATE_FILE.read_text())
            return float(data.get("offset_x", 0.0))
        except Exception:
            return 0.0

    def _save_position_state(self, offset_x: float):
        """Save current robot X offset to state file."""
        try:
            POSITION_STATE_FILE.write_text(json.dumps({"offset_x": offset_x}))
        except Exception as e:
            self.logger.error(f"[PickUpPieceGemini] Failed to save position state: {e}")

    def _compute_square_size_x(self, calibration: dict) -> float:
        """Compute the X-direction size of one square from calibration corners."""
        tl = calibration["top_left"]
        tr = calibration["top_right"]
        bl = calibration["bottom_left"]
        br = calibration["bottom_right"]
        avg_top_x = (tl["x"] + tr["x"]) / 2.0
        avg_bottom_x = (bl["x"] + br["x"]) / 2.0
        return (avg_top_x - avg_bottom_x) / 7.0

    def _compute_square_size_y(self, calibration: dict) -> float:
        """Compute the Y-direction size of one square from calibration corners."""
        tl = calibration["top_left"]
        tr = calibration["top_right"]
        bl = calibration["bottom_left"]
        br = calibration["bottom_right"]
        avg_left_y = (tl["y"] + bl["y"]) / 2.0
        avg_right_y = (tr["y"] + br["y"]) / 2.0
        return (avg_left_y - avg_right_y) / 7.0

    def _update_calibration(self, calibration: dict, corr_x: float, corr_y: float) -> dict:
        """Shift all 4 calibration corners by the correction and save to disk.

        The correction represents how far off the calibration is:
        the arm needed to move by (corr_x, corr_y) to reach the actual square center,
        so the calibration underestimates by that amount. Shift corners accordingly.
        """
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
            self.logger.info(
                f"[PickUpPieceGemini] Calibration updated: shifted all corners by "
                f"dX={corr_x:+.4f}m dY={corr_y:+.4f}m"
            )
        except Exception as e:
            self.logger.error(f"[PickUpPieceGemini] Failed to save calibration: {e}")

        return updated

    def _drive_to_offset(self, target_offset: float, current_offset: float) -> float:
        """
        Drive the robot base so that its X offset equals target_offset.
        Positive offset = robot moved forward (toward board).
        Returns the new offset after driving.
        """
        delta = target_offset - current_offset
        if abs(delta) < 0.001:
            return current_offset

        if self.mobility is None:
            self.logger.warn("[PickUpPieceGemini] Mobility interface not available, skipping drive")
            return current_offset

        direction = 1.0 if delta > 0 else -1.0
        drive_duration = abs(delta) / DRIVE_SPEED

        self.logger.info(
            f"[PickUpPieceGemini] Driving {'forward' if direction > 0 else 'backward'} "
            f"{abs(delta):.4f}m at {DRIVE_SPEED} m/s for {drive_duration:.1f}s"
        )
        self._send_feedback(f"Driving {'forward' if direction > 0 else 'backward'} {abs(delta)*100:.1f}cm...")

        self.mobility.send_cmd_vel(linear_x=direction * DRIVE_SPEED, angular_z=0.0, duration=drive_duration)

        # Wait for the drive to complete
        start = time.time()
        while time.time() - start < drive_duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                elapsed = time.time() - start
                partial = current_offset + direction * DRIVE_SPEED * elapsed
                self._save_position_state(partial)
                return partial
            time.sleep(0.1)

        self._save_position_state(target_offset)
        return target_offset

    @property
    def name(self):
        return "pick_up_piece_gemini"

    def guidelines(self):
        return (
            "Move above a chess square, tilt the wrist camera, and capture an image. "
            "Requires 'square' parameter in chess notation (e.g., 'A1', 'H8'). "
            "Uses calibration data to calculate position. "
            "Moves above square, tilts pitch by -0.5 rad to look at it, captures and saves image."
        )

    def _load_calibration(self):
        """Load calibration data from file."""
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except:
            return None

    def _square_to_position(self, square: str, calibration: dict) -> tuple[float, float] | None:
        """
        Convert chess square (e.g., 'A1') to XY position using bilinear interpolation.

        Coordinate system (from robot's perspective):
        - X positive = forward (away from robot)
        - Y positive = left, Y negative = right

        Board layout:
        - top_left: high X, high Y (forward-left) = A8
        - top_right: high X, low Y (forward-right) = H8
        - bottom_left: low X, high Y (close-left) = A1
        - bottom_right: low X, low Y (close-right) = H1
        """
        if len(square) != 2:
            return None

        file_char = square[0].upper()
        rank_char = square[1]

        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None

        # Convert to 0-7 indices
        file_idx = ord(file_char) - ord('A')  # A=0, H=7
        rank_idx = int(rank_char) - 1          # 1=0, 8=7

        # Normalize to 0-1 range
        u = file_idx / 7.0  # 0 at A, 1 at H
        v = rank_idx / 7.0  # 0 at rank 1, 1 at rank 8

        # Get corner positions
        tl = calibration.get("top_left")
        tr = calibration.get("top_right")
        bl = calibration.get("bottom_left")
        br = calibration.get("bottom_right")

        if not all([tl, tr, bl, br]):
            return None

        # Bilinear interpolation
        # Mapping: u=0 is file A (left), u=1 is file H (right)
        #          v=0 is rank 1 (bottom/close), v=1 is rank 8 (top/far)
        # So: A1=(u=0,v=0)=bottom_left, H1=(u=1,v=0)=bottom_right
        #     A8=(u=0,v=1)=top_left, H8=(u=1,v=1)=top_right

        x = (1-u)*(1-v)*bl["x"] + u*(1-v)*br["x"] + (1-u)*v*tl["x"] + u*v*tr["x"]
        y = (1-u)*(1-v)*bl["y"] + u*(1-v)*br["y"] + (1-u)*v*tl["y"] + u*v*tr["y"]

        return x, y

    def _save_capture(self, square: str):
        """Save the current wrist camera image to a file."""
        if not self.image:
            self.logger.warning("[PickUpPieceGemini] No wrist camera image available")
            return None
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = CAPTURES_DIR / f"square_{square}_{ts}.jpg"
            path.write_bytes(base64.b64decode(self.image))
            self.logger.info(f"[PickUpPieceGemini] Image saved: {path}")
            return path
        except Exception as e:
            self.logger.error(f"[PickUpPieceGemini] Failed to save image: {e}")
            return None

    def execute(self, square: str, place_square: str | None = None, is_pawn: bool = True, speed: float = 1.0):
        """
        Pick up a piece from the specified square using Gemini vision for precision.

        Args:
            square: Source square in chess notation (e.g., 'A4', 'E2')
            place_square: Target square to place piece on (e.g., 'D5'). If None, just pick up.
            is_pawn: If True, descend lower for shorter pawn pieces
            speed: Speed multiplier (1.0 = normal, 2.0 = twice as fast, etc.)
        """
        speed = max(0.1, speed)
        def d(seconds: float) -> float:
            """Scale a duration by the speed factor."""
            return seconds / speed
        def w(seconds: float):
            """Sleep for a scaled duration."""
            time.sleep(seconds / speed)
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        # Load calibration
        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found. Run board calibration first.", SkillResult.FAILURE

        # Calculate position
        pos = self._square_to_position(square, calibration)
        if pos is None:
            return f"Invalid square '{square}' or incomplete calibration", SkillResult.FAILURE

        x, y = pos

        rank = int(square[1])
        yaw = self.FIXED_YAW + 1.57 if rank <= 4 else self.FIXED_YAW
        pick_height = self.HEIGHT_PICK_PAWN if is_pawn else self.HEIGHT_PICK

        # Drive base to optimal position for this rank
        square_size = self._compute_square_size_x(calibration)
        current_offset = self._load_position_state()
        if rank <= 3:
            target_offset = -(DRIVE_SQUARES * square_size)
        else:
            target_offset = 0.0

        current_offset = self._drive_to_offset(target_offset, current_offset)
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Adjust arm X to compensate for robot base movement
        x_adj = x - current_offset

        self.logger.info(
            f"[PickUpPieceGemini] Target {square} at X={x:.4f} (adj={x_adj:.4f}), "
            f"Y={y:.4f} (yaw={yaw:.2f}, offset={current_offset:.4f})"
        )
        self._send_feedback(f"Moving to {square} at X={x_adj:.4f}, Y={y:.4f}")

        tilted_pitch = self.FIXED_PITCH + self.CAMERA_PITCH_OFFSET

        # Step 1: Move to safe height at current position first
        self.logger.info(f"[PickUpPieceGemini] Step 1: Moving to safe height {self.HEIGHT_SAFE}m")
        self._send_feedback("Moving to safe height...")
        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose:
            curr_x, curr_y = current_pose["position"]["x"], current_pose["position"]["y"]
            success = self.manipulation.move_to_cartesian_pose(
                x=x_adj, y=curr_y, z=self.HEIGHT_SAFE,
                roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
                duration=d(1)
            )
            if not success:
                return "Failed to move to safe height", SkillResult.FAILURE
            w(1.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 2: Move horizontally to above target square
        self.logger.info(f"[PickUpPieceGemini] Step 2: Moving above {square}")
        self._send_feedback(f"Moving above {square}...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x_adj, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(2)
        )
        if not success:
            return "Failed to move above square", SkillResult.FAILURE
        w(2.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 3: Apply tilt X offset for camera viewing
        self.logger.info(
            f"[PickUpPieceGemini] Step 3: Shifting X by {self.CAMERA_TILT_X_OFFSET}m for camera view"
        )
        self._send_feedback("Adjusting position for camera view...")
        tilt_x = x_adj + self.CAMERA_TILT_X_OFFSET
        success = self.manipulation.move_to_cartesian_pose(
            x=tilt_x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(2)
        )
        if not success:
            return "Failed to tilt camera", SkillResult.FAILURE
        w(2.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 4: Capture and save image
        self.logger.info("[PickUpPieceGemini] Step 4: Capturing image")
        self._send_feedback("Capturing image...")
        saved_path = self._save_capture(square)

        if saved_path:
            msg = f"Image captured for {square} and saved to {saved_path}"
        else:
            msg = f"Moved above {square} but failed to capture image"
            self.logger.warning(f"[PickUpPieceGemini] {msg}")

        # Step 4b: Analyze square bbox with Gemini and compute correction
        self.logger.info("[PickUpPieceGemini] Step 4b: Analyzing square bbox with Gemini")
        self._send_feedback("Analyzing square with Gemini...")
        analysis = self._analyze_square_bbox(square, calibration)

        if analysis and not self._cancelled:
            corr_x, corr_y = analysis

            # Update calibration based on measured offset
            if abs(corr_x) > 0.001 or abs(corr_y) > 0.001:
                calibration = self._update_calibration(calibration, corr_x, corr_y)
                self.logger.info(f"[PickUpPieceGemini] Calibration updated dX={corr_x:+.4f} dY={corr_y:+.4f}")

            if abs(corr_x) > 0.002 or abs(corr_y) > 0.002:
                # Step 4c: Reposition arm using correction
                new_x = x_adj + corr_x
                new_y = y + corr_y
                self.logger.info(
                    f"[PickUpPieceGemini] Step 4c: Repositioning dX={corr_x:+.4f} dY={corr_y:+.4f} "
                    f"-> X={new_x:.4f}, Y={new_y:.4f}"
                )
                self._send_feedback(f"Adjusting position by dX={corr_x*100:+.1f}cm dY={corr_y*100:+.1f}cm...")
                success = self.manipulation.move_to_cartesian_pose(
                    x=new_x, y=new_y, z=self.HEIGHT_ABOVE,
                    roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
                    duration=d(1.5)
                )
                if success:
                    w(1.0)
                    x_adj = new_x
                    y = new_y
            else:
                self.logger.info("[PickUpPieceGemini] Correction too small, skipping reposition")

        # Undo tilt X offset to position gripper over the piece
        x_adj -= self.CAMERA_TILT_X_OFFSET
        self.logger.info(f"[PickUpPieceGemini] Undid tilt X offset -> X={x_adj:.4f}")

        # Step 5: Open gripper
        self.logger.info("[PickUpPieceGemini] Step 5: Opening gripper")
        self._send_feedback("Opening gripper...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        w(1.5)

        # Step 6: Descend to pick height (two stages for vertical trajectory)
        mid_pick_z = (self.HEIGHT_ABOVE + pick_height) / 2.0
        self.logger.info(f"[PickUpPieceGemini] Step 6a: Descending to mid height {mid_pick_z:.3f}m")
        self._send_feedback("Descending to pick...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x_adj, y=y, z=mid_pick_z,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(1.67)
        )
        if not success:
            return "Failed to descend to mid pick height", SkillResult.FAILURE
        w(1.67)

        self.logger.info(f"[PickUpPieceGemini] Step 6b: Descending to pick height {pick_height}m")
        success = self.manipulation.move_to_cartesian_pose(
            x=x_adj, y=y, z=pick_height,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(1.67)
        )
        if not success:
            return "Failed to descend to pick height", SkillResult.FAILURE
        w(1.67)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 7: Close gripper to grab piece
        self.logger.info("[PickUpPieceGemini] Step 7: Closing gripper")
        self._send_feedback("Grabbing piece...")
        self.manipulation.close_gripper(strength=self.GRIPPER_CLOSE_STRENGTH, blocking=True)
        w(2.0)

        # Step 8: Lift to safe height (two stages for vertical trajectory)
        grip_position = self.manipulation.GRIPPER_CLOSED - self.GRIPPER_CLOSE_STRENGTH
        mid_lift_z = (pick_height + self.HEIGHT_SAFE) / 2.0
        self.logger.info(f"[PickUpPieceGemini] Step 8a: Lifting to mid height {mid_lift_z:.3f}m")
        self._send_feedback("Lifting piece...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x_adj, y=y, z=mid_lift_z,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(1.67),
            gripper_position=grip_position
        )
        if not success:
            return "Failed to lift to mid height", SkillResult.FAILURE
        w(1.67)

        self.logger.info(f"[PickUpPieceGemini] Step 8b: Lifting to {self.HEIGHT_SAFE}m")
        success = self.manipulation.move_to_cartesian_pose(
            x=x_adj, y=y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=yaw,
            duration=d(1.67),
            gripper_position=grip_position
        )
        if not success:
            return "Failed to lift", SkillResult.FAILURE
        w(1.67)

        # If no place square, just drive back and finish
        if place_square is None:
            self.logger.info("[PickUpPieceGemini] Driving back to calibration origin")
            self._send_feedback("Returning to base position...")
            current_offset = self._drive_to_offset(0.0, current_offset)
            msg = f"Picked up piece from {square}"
            self.logger.info(f"[PickUpPieceGemini] Complete: {msg}")
            self._send_feedback(msg)
            return msg, SkillResult.SUCCESS

        # ========== PLACE PIECE ON TARGET SQUARE ==========
        place_pos = self._square_to_position(place_square, calibration)
        if place_pos is None:
            return f"Invalid place square '{place_square}'", SkillResult.FAILURE

        place_x, place_y = place_pos
        place_rank = int(place_square[1])
        place_yaw = self.FIXED_YAW + 1.57 if place_rank <= 4 else self.FIXED_YAW
        place_height = self.HEIGHT_PICK_PAWN if is_pawn else self.HEIGHT_PICK

        # Drive base if needed for the place rank
        if place_rank <= 3:
            place_target_offset = -(DRIVE_SQUARES * square_size)
        else:
            place_target_offset = 0.0

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        current_offset = self._drive_to_offset(place_target_offset, current_offset)
        place_x_adj = place_x - current_offset

        # Undo tilt X offset to position gripper over the piece
        place_x_adj -= self.CAMERA_TILT_X_OFFSET
        self.logger.info(f"[PickUpPieceGemini] Undid tilt X offset -> place X={place_x_adj:.4f}")

        self.logger.info(
            f"[PickUpPieceGemini] Placing on {place_square} at X={place_x:.4f} (adj={place_x_adj:.4f}), "
            f"Y={place_y:.4f}"
        )

        # Step 10: Move above place square
        self.logger.info(f"[PickUpPieceGemini] Step 10: Moving above {place_square}")
        self._send_feedback(f"Moving above {place_square}...")
        success = self.manipulation.move_to_cartesian_pose(
            x=place_x_adj, y=place_y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=place_yaw,
            duration=d(2),
            gripper_position=grip_position
        )
        if not success:
            return "Failed to move above place square", SkillResult.FAILURE
        w(2.5)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 11: Descend to place height (two stages for vertical trajectory)
        mid_place_z = (self.HEIGHT_ABOVE + place_height) / 2.0
        self.logger.info(f"[PickUpPieceGemini] Step 11a: Descending to mid height {mid_place_z:.3f}m")
        self._send_feedback("Descending to place...")
        success = self.manipulation.move_to_cartesian_pose(
            x=place_x_adj, y=place_y, z=mid_place_z,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=place_yaw,
            duration=d(1.67),
            gripper_position=grip_position
        )
        if not success:
            return "Failed to descend to mid place height", SkillResult.FAILURE
        w(1.67)

        self.logger.info(f"[PickUpPieceGemini] Step 11b: Descending to place height {place_height}m")
        success = self.manipulation.move_to_cartesian_pose(
            x=place_x_adj, y=place_y, z=place_height,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=place_yaw,
            duration=d(1.67),
            gripper_position=grip_position
        )
        if not success:
            return "Failed to descend to place height", SkillResult.FAILURE
        w(1.67)

        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # Step 12: Release piece
        self.logger.info("[PickUpPieceGemini] Step 12: Releasing piece")
        self._send_feedback("Releasing piece...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        w(1.5)

        # Step 13: Lift back to safe height (two stages for vertical trajectory)
        mid_lift_z = (place_height + self.HEIGHT_SAFE) / 2.0
        self.logger.info(f"[PickUpPieceGemini] Step 13a: Lifting to mid height {mid_lift_z:.3f}m")
        self._send_feedback("Lifting after place...")
        success = self.manipulation.move_to_cartesian_pose(
            x=place_x_adj, y=place_y, z=mid_lift_z,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=place_yaw,
            duration=d(1.67)
        )
        if not success:
            return "Failed to lift to mid height after placing", SkillResult.FAILURE
        w(1.67)

        self.logger.info(f"[PickUpPieceGemini] Step 13b: Lifting to {self.HEIGHT_SAFE}m")
        success = self.manipulation.move_to_cartesian_pose(
            x=place_x_adj, y=place_y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=tilted_pitch, yaw=place_yaw,
            duration=d(1.67)
        )
        if not success:
            return "Failed to lift after placing", SkillResult.FAILURE
        w(1.67)

        # Drive back to calibration origin
        self.logger.info("[PickUpPieceGemini] Driving back to calibration origin")
        self._send_feedback("Returning to base position...")
        current_offset = self._drive_to_offset(0.0, current_offset)

        msg = f"Moved piece from {square} to {place_square}"
        self.logger.info(f"[PickUpPieceGemini] Complete: {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def _analyze_square_bbox(self, square: str, calibration: dict):
        """Ask Gemini for the bbox of the square and save annotated visualization.

        Returns (x_correction, y_correction) or None on failure.
        Spatial mapping: image top=+X, image right=-Y.
        """
        if not self.gemini_client or not self.image:
            self.logger.warning("[PickUpPieceGemini] Skipping bbox analysis (no client or image)")
            return None

        try:
            from PIL import Image, ImageDraw

            img = Image.open(io.BytesIO(base64.b64decode(self.image)))
            w, h = img.size
            img_cx, img_cy = w // 2, h // 2

            # Draw red dot at image center and encode for Gemini
            img_for_gemini = img.copy()
            draw_gemini = ImageDraw.Draw(img_for_gemini)
            draw_gemini.ellipse([img_cx - 7, img_cy - 7, img_cx + 7, img_cy + 7],
                                fill='red', outline='white', width=2)
            buf_gemini = io.BytesIO()
            img_for_gemini.save(buf_gemini, format='JPEG', quality=90)
            annotated_b64 = base64.b64encode(buf_gemini.getvalue()).decode('utf-8')

            prompt = (
                "This image shows a chessboard from above. There is a red dot on the board. "
                "Return the bounding box of the chessboard square the red dot is on. "
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

            self.logger.info(f"[PickUpPieceGemini] Gemini response: {result}")

            if not box_2d or len(box_2d) != 4 or not any(v > 0 for v in box_2d):
                self.logger.warning(f"[PickUpPieceGemini] Gemini returned invalid bbox: {box_2d}")
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
            # Image vertical (top-bottom) corresponds to robot X
            m_per_px_vert = sq_size_x / bbox_h_px
            # Image horizontal (left-right) corresponds to robot Y
            m_per_px_horiz = sq_size_y / bbox_w_px

            # Pixel offset: bbox center relative to image center
            dx_px = bbox_cx - img_cx  # positive = bbox is right of center
            dy_px = bbox_cy - img_cy  # positive = bbox is below center

            # Convert to robot coordinates
            # Image top = +X, so bbox above center (dy_px < 0) means move +X
            x_correction = -dy_px * m_per_px_vert
            # Image right = -Y, so bbox right of center (dx_px > 0) means move -Y
            y_correction = -dx_px * m_per_px_horiz

            dist_m = (x_correction**2 + y_correction**2) ** 0.5

            self.logger.info(
                f"[PickUpPieceGemini] Gemini bbox: box_2d={box_2d} "
                f"px=[{x1},{y1},{x2},{y2}] bbox_center=({bbox_cx},{bbox_cy}) "
                f"img_center=({img_cx},{img_cy}) dist={dist_px:.0f}px"
            )
            self.logger.info(
                f"[PickUpPieceGemini] Correction: dx_px={dx_px} dy_px={dy_px} "
                f"-> x_corr={x_correction:+.4f}m y_corr={y_correction:+.4f}m "
                f"(sq_px={bbox_w_px}x{bbox_h_px} sq_m={sq_size_y:.4f}x{sq_size_x:.4f} dist={dist_m*100:.1f}cm)"
            )

            # Draw annotated image
            draw = ImageDraw.Draw(img)

            # Image center point (red crosshair)
            draw.ellipse([img_cx - 7, img_cy - 7, img_cx + 7, img_cy + 7],
                         fill='red', outline='white', width=2)
            draw.line([(img_cx - 16, img_cy), (img_cx + 16, img_cy)], fill='red', width=2)
            draw.line([(img_cx, img_cy - 16), (img_cx, img_cy + 16)], fill='red', width=2)

            # Bbox rectangle (yellow)
            draw.rectangle([x1, y1, x2, y2], outline='yellow', width=3)

            # Bbox center (lime dot)
            draw.ellipse([bbox_cx - 5, bbox_cy - 5, bbox_cx + 5, bbox_cy + 5],
                         fill='lime', outline='white', width=1)

            # Line from image center to bbox center (cyan)
            draw.line([(img_cx, img_cy), (bbox_cx, bbox_cy)], fill='cyan', width=2)

            # Distance label
            label = f"dist={dist_px:.0f}px corr=({x_correction:+.3f},{y_correction:+.3f})m"
            draw.text((10, 10), label, fill='cyan')

            # Save
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            path = CAPTURES_DIR / f"bbox_{square}_{ts}.jpg"
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=90)
            path.write_bytes(buf.getvalue())
            self.logger.info(f"[PickUpPieceGemini] Bbox visualization saved: {path}")

            return (x_correction, y_correction)

        except Exception as e:
            self.logger.error(f"[PickUpPieceGemini] Bbox analysis failed: {e}")
            return None

    def cancel(self):
        """Cancel the operation."""
        self._cancelled = True
        return "Pick up piece gemini cancelled"
