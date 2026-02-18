#!/usr/bin/env python3
"""
Pick Up Piece Simple Skill - Pick up and place chess pieces using only
calibration data and arm orientation, without Gemini vision or base driving.

Orientation strategy by rank/column:
  Ranks 4-6:  pitch=1.57 (straight down), yaw=0
  Ranks 7-8:  pitch=1.09 (tilted, 1.57-0.48), yaw=0
  Ranks 1-3, cols A-D:  pitch=1.57, yaw=-1.57 (rotated left)
  Ranks 1-3, cols E-H:  pitch=1.57, yaw=+1.57 (rotated right)

Shortcut: ranks 5-6 can reach ranks 7-8 directly with tilted
orientation, skipping the relay handoff.
"""

import json
import time
from pathlib import Path

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult

CALIBRATION_FILE = Path.home() / "board_calibration.json"


class PickUpPieceSimple(Skill):
    """Pick up a chess piece and place it on another square using calibration
    positions only.  No vision correction, no base driving."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # Orientation constants
    FIXED_ROLL = 0.0
    PITCH_DOWN = 1.57  # straight down
    PITCH_TILTED = 1.57 - 0.48  # tilted for far ranks (7-8)
    YAW_CENTER = 0.0
    YAW_LEFT = 1.57  # rotated left for ranks 1-3, cols A-D
    YAW_RIGHT = -1.57  # rotated right for ranks 1-3, cols E-H

    # Heights in meters
    HEIGHT_SAFE = 0.15  # 20cm safe travel height
    HEIGHT_PICK_TALL = 0.06  # 8cm pick height for tall pieces (king, queen)
    HEIGHT_PICK_SHORT = 0.04  # 4cm pick height for short pieces (everything else)

    # Gripper parameters
    GRIPPER_OPEN_PERCENT = 50
    GRIPPER_CLOSE_STRENGTH = 0.4
    GRIPPER_MIN_WAIT = 1.0  # minimum seconds to wait for gripper to actuate

    # Number of intermediate Z steps for vertical moves
    VERTICAL_STEPS = 4

    # Phase speed multipliers (> 1.0 = faster, < 1.0 = slower, on top of speed)
    PHASE_AIR = 1.5  # lateral moves at safe height
    PHASE_LIFT = 1.3  # lifting after grab/release
    PHASE_DESCENT_FAR = 1.2  # top portion of descent
    PHASE_DESCENT_NEAR = 0.6  # near-board portion of descent

    # Ranks 7-8 adjustments
    TILTED_SPEED_FACTOR = 0.75  # extra caution multiplier for tilted moves
    HEIGHT_SAFE_TILTED = 0.18  # higher safe height to avoid bumps

    # Relay rank for intermediary handoff when crossing rank 6/7 boundary
    RELAY_RANK = 5

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._speed = 1.0

    @property
    def name(self):
        return "pick_up_piece_simple"

    def guidelines(self):
        return (
            "Pick up a piece from one square and place it on another without "
            "using Gemini vision or base driving.  Uses arm orientation changes "
            "to reach all ranks.  Parameters: square (source, e.g. 'E2'), "
            "place_square (target, e.g. 'E4'), is_pawn (bool), speed (float)."
        )

    # ── Helpers ───────────────────────────────────────────────────────

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return None

    def _square_to_position(self, square, calibration):
        """Convert chess notation (e.g. 'E4') to (x, y, z) robot coordinates."""
        if len(square) != 2:
            return None
        file_char = square[0].upper()
        rank_char = square[1]
        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None

        file_idx = ord(file_char) - ord("A")  # A=0, H=7
        rank_idx = int(rank_char) - 1  # 1=0, 8=7

        u = file_idx / 7.0  # 0 at A, 1 at H
        v = rank_idx / 7.0  # 0 at rank 1, 1 at rank 8

        tl = calibration.get("top_left")
        tr = calibration.get("top_right")
        bl = calibration.get("bottom_left")
        br = calibration.get("bottom_right")
        if not all([tl, tr, bl, br]):
            return None

        x = (1 - u) * (1 - v) * bl["x"] + u * (1 - v) * br["x"] + (1 - u) * v * tl["x"] + u * v * tr["x"]
        y = (1 - u) * (1 - v) * bl["y"] + u * (1 - v) * br["y"] + (1 - u) * v * tl["y"] + u * v * tr["y"]
        z = (1 - u) * (1 - v) * bl.get("z", 0) + u * (1 - v) * br.get("z", 0) + (1 - u) * v * tl.get("z", 0) + u * v * tr.get("z", 0)
        return x, y, z

    def _orientation_for_square(self, square):
        """Return (pitch, yaw) for reaching a given square.

        Ranks 4-6:  straight down, center yaw
        Ranks 7-8:  tilted pitch, center yaw
        Ranks 1-3:  straight down, yaw left (A-D) or right (E-H)
        """
        rank = int(square[1])
        col = square[0].upper()

        if rank >= 7:
            return self.PITCH_TILTED, self.YAW_CENTER
        elif rank >= 4:
            return self.PITCH_DOWN, self.YAW_CENTER
        else:  # ranks 1-3
            if col in "ABCD":
                return self.PITCH_DOWN, self.YAW_LEFT
            else:
                return self.PITCH_DOWN, self.YAW_RIGHT

    def _d(self, seconds: float) -> float:
        """Scale a duration by the speed factor."""
        return seconds / self._speed

    def _w(self, seconds: float):
        """Sleep for a scaled duration."""
        time.sleep(seconds / self._speed)

    def _gripper_wait(self, seconds: float):
        """Wait for a gripper operation. Scales with speed but never below GRIPPER_MIN_WAIT."""
        time.sleep(max(seconds / self._speed, self.GRIPPER_MIN_WAIT))

    def _move_arm(self, x, y, z, pitch, yaw, duration, wait=None, gripper_position=None):
        """Move arm to pose and optionally wait. Returns True on success."""
        kwargs = dict(x=x, y=y, z=z, roll=self.FIXED_ROLL, pitch=pitch, yaw=yaw, duration=self._d(duration))
        if gripper_position is not None:
            kwargs["gripper_position"] = gripper_position
        success = self.manipulation.move_to_cartesian_pose(**kwargs)
        if success and wait is not None:
            self._w(wait)
        return success

    def _vertical_move(self, x, y, from_z, to_z, pitch, yaw, gripper_position=None, caution=1.0):
        """Move vertically in VERTICAL_STEPS increments with fixed X, Y.

        Per-segment speed profiling:
          - Descent: fast at top (PHASE_DESCENT_FAR), slow near board (PHASE_DESCENT_NEAR)
          - Lift: uniformly faster (PHASE_LIFT)
        caution: extra multiplier (< 1.0 = more cautious, e.g. for tilted moves).

        Tries the smooth trajectory service first (no stop between steps).
        Falls back to individual moves if the service isn't available.
        Returns error string or None.
        """
        descending = to_z < from_z
        direction = "Descending" if descending else "Lifting"
        n = self.VERTICAL_STEPS

        # Compute per-segment durations (actual seconds)
        seg_durs = []
        for i in range(n):
            if descending:
                frac = (i + 0.5) / n  # midpoint of segment, 0=top 1=bottom
                phase = self.PHASE_DESCENT_FAR + (self.PHASE_DESCENT_NEAR - self.PHASE_DESCENT_FAR) * frac
            else:
                phase = self.PHASE_LIFT
            seg_durs.append(1.0 / (self._speed * phase * caution))

        # Build waypoint poses (including from_z so the trajectory starts there)
        poses = []
        for i in range(n + 1):
            frac = i / n
            z = from_z + (to_z - from_z) * frac
            poses.append(dict(x=x, y=y, z=z, roll=self.FIXED_ROLL, pitch=pitch, yaw=yaw))

        # Try smooth trajectory first
        try:
            success = self.manipulation.move_cartesian_trajectory(
                poses,
                segment_durations=seg_durs,
                gripper_position=gripper_position,
            )
            if success:
                self.logger.info(
                    f"[PickUpPieceSimple] {direction} trajectory complete "
                    f"({n} segments, durs={[f'{d:.2f}' for d in seg_durs]})"
                )
                return None
            self.logger.warning("[PickUpPieceSimple] Trajectory service failed, falling back to step-by-step")
        except Exception as e:
            self.logger.warning(f"[PickUpPieceSimple] Trajectory not available ({e}), falling back")

        # Fallback: individual moves with per-segment durations
        for i in range(1, n + 1):
            frac = i / n
            z = from_z + (to_z - from_z) * frac
            dur = seg_durs[i - 1]
            self.logger.info(f"[PickUpPieceSimple] {direction} step {i}/{n} -> z={z:.3f}m ({dur:.2f}s)")
            kwargs = dict(x=x, y=y, z=z, roll=self.FIXED_ROLL, pitch=pitch, yaw=yaw, duration=dur)
            if gripper_position is not None:
                kwargs["gripper_position"] = gripper_position
            if not self.manipulation.move_to_cartesian_pose(**kwargs):
                return f"Failed at {direction.lower()} step {i}/{n} z={z:.3f}m"
            time.sleep(dur)
            if self._cancelled:
                return "Cancelled"
        return None

    def _go_to_safe_pose(self, pitch, yaw):
        """Return arm to the resting safe pose."""
        self._move_arm(0.15, 0.1, 0.1, pitch, yaw, 2, wait=2.0)

    def _needs_relay(self, src_square, dst_square):
        """Check if move crosses the rank 6/7 boundary requiring a relay.

        Ranks 5-6 can reach 7-8 directly with tilted orientation,
        so no relay is needed for that transition.
        """
        src_rank = int(src_square[1])
        dst_rank = int(dst_square[1])
        # Ranks 5-6 <-> 7-8 can be done directly with tilted pitch
        if src_rank in (5, 6) and dst_rank >= 7:
            return False
        if dst_rank in (5, 6) and src_rank >= 7:
            return False
        return (src_rank <= 6) != (dst_rank <= 6)

    def _relay_position(self, src_square, dst_square, calibration):
        """Compute a relay square on rank 5 for intermediary handoff.

        Uses the file of whichever square is in ranks 1-6 so the relay
        stays close to the reachable side of the board.
        Returns (relay_square_str, (x, y, z)) or (relay_square_str, None).
        """
        src_rank = int(src_square[1])
        if src_rank <= 6:
            file_char = src_square[0].upper()
        else:
            file_char = dst_square[0].upper()
        relay_square = f"{file_char}{self.RELAY_RANK}"
        pos = self._square_to_position(relay_square, calibration)
        return relay_square, pos

    def _do_pick_place(
        self,
        src_x,
        src_y,
        dst_x,
        dst_y,
        pick_height,
        src_pitch,
        src_yaw,
        dst_pitch,
        dst_yaw,
        src_label,
        dst_label,
        safe_height=None,
        caution=1.0,
    ):
        """Single pick-and-place cycle: pick from src, place at dst.

        Uses src orientation for picking, dst orientation for placing.
        Phase-aware speeds: air travel is fast, lift is fast, descent
        progressively slows near the board.  Gripper waits are never rushed.

        Args:
            safe_height: Override for HEIGHT_SAFE (e.g. higher for tilted).
            caution: Multiplier < 1.0 makes everything more cautious.
        Returns error string or None on success.
        """
        if safe_height is None:
            safe_height = self.HEIGHT_SAFE

        # Compute gripper positions in radians so every trajectory command
        # carries an explicit gripper target (avoids stale _arm_state reads).
        open_grip = self.manipulation.GRIPPER_CLOSED + (
            self.manipulation.GRIPPER_OPEN - self.manipulation.GRIPPER_CLOSED
        ) * (self.GRIPPER_OPEN_PERCENT / 100.0)
        closed_grip = self.manipulation.GRIPPER_CLOSED - self.GRIPPER_CLOSE_STRENGTH

        # Phase-adjusted base durations for air moves (before _d scaling)
        air_dur = 2.0 / (self.PHASE_AIR * caution)
        air_wait = 2.5 / (self.PHASE_AIR * caution)

        # Move above source at safe height (FAST – in the air)
        self._send_feedback(f"Moving above {src_label}...")
        if not self._move_arm(src_x, src_y, safe_height, src_pitch, src_yaw, air_dur, wait=air_wait):
            return f"Failed to move above {src_label}"
        if self._cancelled:
            return "Cancelled"

        # Open gripper and wait for it to fully open before descending
        self._send_feedback("Opening gripper...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        self._gripper_wait(1.5)

        # Descend to pick height (PROGRESSIVE – fast top, slow near board)
        self._send_feedback(f"Descending to pick from {src_label}...")
        err = self._vertical_move(
            src_x, src_y, safe_height, pick_height, src_pitch, src_yaw, gripper_position=open_grip, caution=caution
        )
        if err:
            return f"Pick descent failed: {err}"

        # Grab (NEUTRAL – gripper waits are never rushed)
        self._send_feedback("Grabbing piece...")
        self.manipulation.close_gripper(strength=self.GRIPPER_CLOSE_STRENGTH, blocking=True)
        self._gripper_wait(2.0)
        grip_position = closed_grip

        # Lift to safe height (FAST – lifting via PHASE_LIFT)
        self._send_feedback("Lifting piece...")
        err = self._vertical_move(
            src_x, src_y, pick_height, safe_height, src_pitch, src_yaw, gripper_position=grip_position, caution=caution
        )
        if err:
            return f"Lift failed: {err}"
        if self._cancelled:
            return "Cancelled"

        # Move above destination at safe height (FAST – in the air)
        self._send_feedback(f"Moving above {dst_label}...")
        if not self._move_arm(
            dst_x, dst_y, safe_height, dst_pitch, dst_yaw, air_dur, wait=air_wait, gripper_position=grip_position
        ):
            return f"Failed to move above {dst_label}"
        if self._cancelled:
            return "Cancelled"

        # Descend to place height (PROGRESSIVE)
        self._send_feedback(f"Descending to place on {dst_label}...")
        err = self._vertical_move(
            dst_x, dst_y, safe_height, pick_height, dst_pitch, dst_yaw, gripper_position=grip_position, caution=caution
        )
        if err:
            return f"Place descent failed: {err}"

        # Release (NEUTRAL)
        self._send_feedback("Releasing piece...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        self._gripper_wait(1.5)

        # Lift to safe height (FAST)
        self._send_feedback("Lifting after place...")
        err = self._vertical_move(
            dst_x, dst_y, pick_height, safe_height, dst_pitch, dst_yaw, gripper_position=open_grip, caution=caution
        )
        if err:
            return f"Post-place lift failed: {err}"

        return None

    # ── Main logic ────────────────────────────────────────────────────

    TALL_PIECES = {"king", "queen"}

    def execute(self, square: str, place_square: str, piece: str = "pawn", speed: float = 1.0):
        """
        Pick up a piece from square and place it on place_square.

        When a move crosses the rank 6/7 boundary the arm cannot reach
        ranks 7-8 with a vertical gripper, so we relay through an
        intermediary square on rank 5: place the piece there, reorient
        the gripper (tilted ~0.48 rad for 7-8, vertical for 1-6), then
        pick up again and continue to the destination.

        Args:
            square: Source square in chess notation (e.g. 'A4')
            place_square: Target square (e.g. 'D5')
            piece: Piece type ('king', 'queen', 'rook', 'bishop', 'knight', 'pawn')
            speed: Speed multiplier (1.0 = normal)
        """
        self._speed = max(0.1, min(speed, 3.0))
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found. Run board calibration first.", SkillResult.FAILURE

        src_pos = self._square_to_position(square, calibration)
        if src_pos is None:
            return f"Invalid source square '{square}'", SkillResult.FAILURE
        dst_pos = self._square_to_position(place_square, calibration)
        if dst_pos is None:
            return f"Invalid target square '{place_square}'", SkillResult.FAILURE

        src_x, src_y, src_board_z = src_pos
        dst_x, dst_y, dst_board_z = dst_pos
        is_tall = piece.strip().lower() in self.TALL_PIECES
        base_pick_height = self.HEIGHT_PICK_TALL if is_tall else self.HEIGHT_PICK_SHORT
        src_pitch, src_yaw = self._orientation_for_square(square)
        dst_pitch, dst_yaw = self._orientation_for_square(place_square)

        self.logger.info(
            f"[PickUpPieceSimple] Pick {square} ({src_x:.4f},{src_y:.4f},z={src_board_z:.4f}) "
            f"pitch={src_pitch:.2f} yaw={src_yaw:.2f} -> "
            f"Place {place_square} ({dst_x:.4f},{dst_y:.4f},z={dst_board_z:.4f}) "
            f"pitch={dst_pitch:.2f} yaw={dst_yaw:.2f}"
        )

        src_rank = int(square[1])
        dst_rank = int(place_square[1])

        if self._needs_relay(square, place_square):
            # ── Two-step relay through intermediary ──
            relay_sq, relay_pos = self._relay_position(square, place_square, calibration)
            if relay_pos is None:
                return "Failed to compute relay position", SkillResult.FAILURE
            relay_x, relay_y, relay_board_z = relay_pos
            relay_pick_height = base_pick_height + relay_board_z

            self.logger.info(f"[PickUpPieceSimple] Relay through {relay_sq} ({relay_x:.4f},{relay_y:.4f},z={relay_board_z:.4f})")

            # Leg 1: pick from source, place at relay (keep source orientation)
            # Tilted caution if source is rank 7-8
            leg1_tilted = src_rank >= 7
            src_pick_height = base_pick_height + src_board_z
            self._send_feedback(f"Relay leg 1: {square} -> {relay_sq}")
            err = self._do_pick_place(
                src_x,
                src_y,
                relay_x,
                relay_y,
                src_pick_height,
                src_pitch,
                src_yaw,
                src_pitch,
                src_yaw,
                square,
                f"relay {relay_sq}",
                safe_height=self.HEIGHT_SAFE_TILTED if leg1_tilted else None,
                caution=self.TILTED_SPEED_FACTOR if leg1_tilted else 1.0,
            )
            if err:
                return f"Relay leg 1 failed: {err}", SkillResult.FAILURE
            if self._cancelled:
                return "Cancelled", SkillResult.CANCELLED

            # Leg 2: pick from relay, place at destination (destination orientation)
            # Tilted caution if destination is rank 7-8
            leg2_tilted = dst_rank >= 7
            dst_pick_height = base_pick_height + dst_board_z
            self._send_feedback(f"Relay leg 2: {relay_sq} -> {place_square}")
            err = self._do_pick_place(
                relay_x,
                relay_y,
                dst_x,
                dst_y,
                relay_pick_height,
                dst_pitch,
                dst_yaw,
                dst_pitch,
                dst_yaw,
                f"relay {relay_sq}",
                place_square,
                safe_height=self.HEIGHT_SAFE_TILTED if leg2_tilted else None,
                caution=self.TILTED_SPEED_FACTOR if leg2_tilted else 1.0,
            )
            if err:
                return f"Relay leg 2 failed: {err}", SkillResult.FAILURE
        else:
            # ── Direct move (no orientation change needed) ──
            # Ranks 5-6 <-> 7-8: use tilted orientation for both ends
            cross_56_78 = (src_rank in (5, 6) and dst_rank >= 7) or (dst_rank in (5, 6) and src_rank >= 7)
            if cross_56_78:
                src_pitch, src_yaw = self.PITCH_TILTED, self.YAW_CENTER
                dst_pitch, dst_yaw = self.PITCH_TILTED, self.YAW_CENTER
                self.logger.info(
                    f"[PickUpPieceSimple] Rank {src_rank}->{dst_rank}: direct tilted (no relay)"
                )
            any_tilted = src_rank >= 7 or dst_rank >= 7 or cross_56_78
            pick_height = base_pick_height + src_board_z
            err = self._do_pick_place(
                src_x,
                src_y,
                dst_x,
                dst_y,
                pick_height,
                src_pitch,
                src_yaw,
                dst_pitch,
                dst_yaw,
                square,
                place_square,
                safe_height=self.HEIGHT_SAFE_TILTED if any_tilted else None,
                caution=self.TILTED_SPEED_FACTOR if any_tilted else 1.0,
            )
            if err:
                return f"Move failed: {err}", SkillResult.FAILURE

        # ── Return to safe pose ──
        self._send_feedback("Returning to safe pose...")
        self._go_to_safe_pose(self.PITCH_DOWN, self.YAW_CENTER)

        msg = f"Moved piece from {square} to {place_square}"
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Pick up piece simple cancelled"
