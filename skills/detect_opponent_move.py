#!/usr/bin/env python3
"""
Skill that detects the opponent's last chess move by comparing the known
board state (FEN) against what the cameras currently see.

Uses two images (main camera wide view + wrist camera overhead close-up)
and asks Gemini to identify which legal move was played.  The board state
is persisted in ~/chess_game_state.json.
"""

import base64
import json
import time
from pathlib import Path

import chess
from google import genai
from google.genai import types

from brain_client.skill_types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)

# ── Paths ─────────────────────────────────────────────────────────────
GAME_STATE_FILE = Path.home() / "chess_game_state.json"
CALIBRATION_FILE = Path.home() / "board_calibration.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/detect_move"


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


# ── ASCII board renderer ──────────────────────────────────────────────


def _fen_to_ascii(fen: str) -> str:
    """Render a FEN string as a labelled ASCII board (rank 8 at top)."""
    board = chess.Board(fen)
    lines = []
    for rank in range(7, -1, -1):  # 8 down to 1
        row = []
        for file in range(8):  # a to h
            piece = board.piece_at(chess.square(file, rank))
            row.append(piece.symbol() if piece else ".")
        lines.append(f"  {rank + 1}  {' '.join(row)}")
    lines.append("     a b c d e f g h")
    return "\n".join(lines)


class DetectOpponentMove(Skill):
    """Detect the opponent's last move using Gemini vision + FEN state."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    head = Interface(InterfaceType.HEAD)
    main_image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
    wrist_image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    # Observation pose: arm roughly centred above the board, looking down
    OBS_X = 0.20
    OBS_Y = -0.03
    OBS_Z = 0.18
    OBS_PITCH = 1.57  # straight down
    OBS_YAW = 0.0
    FIXED_ROLL = 0.0

    # Head tilt for looking at the board (degrees, negative = down)
    HEAD_TILT_DOWN = -20
    HEAD_TILT_NEUTRAL = 0

    # Confidence threshold: below this triggers a second Gemini call
    CONFIDENCE_THRESHOLD = 0.7

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
            self.logger.info("[DetectOpponentMove] Gemini configured")
        else:
            self.logger.warning("[DetectOpponentMove] GEMINI_API_KEY not set in .env.scan")

    # ── Metadata ──────────────────────────────────────────────────────

    @property
    def name(self):
        return "detect_opponent_move"

    def guidelines(self):
        return (
            "Detect the opponent's last chess move. The robot positions its "
            "wrist camera above the board, captures images from both the main "
            "and wrist cameras, then asks Gemini which legal move was played. "
            "The board state (FEN) is stored in ~/chess_game_state.json. "
            "Optionally pass robot_color='white' or 'black' to set perspective."
        )

    # ── State persistence ─────────────────────────────────────────────

    def _load_game_state(self) -> dict | None:
        """Load game state from JSON. Returns dict or None."""
        if not GAME_STATE_FILE.exists():
            return None
        try:
            return json.loads(GAME_STATE_FILE.read_text())
        except Exception as e:
            self.logger.error(f"[DetectOpponentMove] Failed to load game state: {e}")
            return None

    def _save_game_state(self, fen: str, move_history: list, last_move: str, robot_color: str):
        """Persist game state to JSON."""
        turn = "white" if " w " in fen else "black"
        state = {
            "fen": fen,
            "move_history": move_history,
            "last_detected_move": last_move,
            "turn": turn,
            "robot_color": robot_color,
        }
        GAME_STATE_FILE.write_text(json.dumps(state, indent=2))
        self.logger.info(f"[DetectOpponentMove] State saved: {GAME_STATE_FILE}")

    def _init_game_state(self, robot_color: str) -> dict:
        """Create a fresh game state with the standard starting position."""
        fen = chess.STARTING_FEN
        state = {
            "fen": fen,
            "move_history": [],
            "last_detected_move": None,
            "turn": "white",
            "robot_color": robot_color,
        }
        GAME_STATE_FILE.write_text(json.dumps(state, indent=2))
        self.logger.info("[DetectOpponentMove] Initialised new game state")
        return state

    # ── Arm positioning ───────────────────────────────────────────────

    def _move_to_observation_pose(self) -> bool:
        """Move arm above the board centre and tilt head down."""
        # Tilt head down to look at the board
        if self.head:
            self.head.set_position(self.HEAD_TILT_DOWN)
            time.sleep(0.5)

        self._send_feedback("Moving arm to observation pose...")
        success = self.manipulation.move_to_cartesian_pose(
            x=self.OBS_X,
            y=self.OBS_Y,
            z=self.OBS_Z,
            roll=self.FIXED_ROLL,
            pitch=self.OBS_PITCH,
            yaw=self.OBS_YAW,
            duration=2.0,
        )
        if success:
            time.sleep(2.5)
        return success

    def _go_to_safe_pose(self):
        """Return arm to resting safe pose and head to neutral."""
        self.manipulation.move_to_cartesian_pose(
            x=0.15,
            y=0.1,
            z=0.1,
            roll=self.FIXED_ROLL,
            pitch=self.OBS_PITCH,
            yaw=self.OBS_YAW,
            duration=2.0,
        )
        if self.head:
            self.head.set_position(self.HEAD_TILT_NEUTRAL)
        time.sleep(2.5)

    # ── Image capture ─────────────────────────────────────────────────

    def _capture_images(self) -> tuple:
        """Grab latest main + wrist camera images.

        Returns (main_b64, wrist_b64). Either may be None.
        """
        # Give the camera a moment to refresh after arm settles
        time.sleep(0.5)
        main_b64 = self.main_image
        wrist_b64 = self.wrist_image

        # Save captures for debugging
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            if main_b64:
                (CAPTURES_DIR / f"main_{ts}.jpg").write_bytes(base64.b64decode(main_b64))
            if wrist_b64:
                (CAPTURES_DIR / f"wrist_{ts}.jpg").write_bytes(base64.b64decode(wrist_b64))
        except Exception as e:
            self.logger.warning(f"[DetectOpponentMove] Failed to save captures: {e}")

        return main_b64, wrist_b64

    # ── Board context builder ─────────────────────────────────────────

    def _build_board_context(self, fen: str) -> tuple:
        """Build text context from the current FEN.

        Returns (context_str, legal_moves_uci_list, board).
        """
        board = chess.Board(fen)
        ascii_board = _fen_to_ascii(fen)
        legal_moves = [m.uci() for m in board.legal_moves]
        turn = "White" if board.turn == chess.WHITE else "Black"

        context = (
            f"CURRENT BOARD STATE (FEN): {fen}\n\n"
            f"ASCII board:\n{ascii_board}\n\n"
            f"It is {turn}'s turn.\n"
            f"Legal moves ({len(legal_moves)}): {', '.join(legal_moves)}\n"
        )
        return context, legal_moves, board

    # ── Debug helpers ──────────────────────────────────────────────────

    def _save_gemini_inputs(self, label: str, prompt: str, main_b64: str | None, wrist_b64: str | None):
        """Save the prompt and images sent to Gemini for debugging."""
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            (CAPTURES_DIR / f"{label}_prompt_{ts}.txt").write_text(prompt)
            if main_b64:
                (CAPTURES_DIR / f"{label}_main_{ts}.jpg").write_bytes(base64.b64decode(main_b64))
            if wrist_b64:
                (CAPTURES_DIR / f"{label}_wrist_{ts}.jpg").write_bytes(base64.b64decode(wrist_b64))
            self.logger.info(f"[DetectOpponentMove] Saved {label} inputs to {CAPTURES_DIR}")
        except Exception as e:
            self.logger.warning(f"[DetectOpponentMove] Failed to save {label} inputs: {e}")

    # ── Gemini calls ──────────────────────────────────────────────────

    def _ask_gemini_detect_move(self, board_context: str, main_b64: str | None, wrist_b64: str | None) -> dict | None:
        """Stage 1: Ask Gemini which move was played.

        Returns parsed dict {move_uci, confidence, reasoning} or None.
        """
        prompt = (
            "You are analyzing a physical chess board to detect the opponent's last move.\n\n"
            f"{board_context}\n"
            "I am providing camera images of the board:\n"
            "- Image 1: Wide-angle main camera view of the board.\n"
            "- Image 2: Close-up overhead wrist camera view of the board.\n\n"
            "Compare the CURRENT FEN state (the state BEFORE the opponent moved) "
            "with what you see in the images (the state AFTER the opponent moved).\n"
            "Identify which piece moved and where it went.\n\n"
            "IMPORTANT: The move MUST be one of the legal moves listed above.\n\n"
            "Return ONLY JSON:\n"
            '{"move_uci": "<from_square><to_square>", "confidence": 0.0-1.0, '
            '"reasoning": "brief explanation"}'
        )

        contents = [prompt]
        if main_b64:
            contents.append(types.Part.from_bytes(data=base64.b64decode(main_b64), mime_type="image/jpeg"))
        if wrist_b64:
            contents.append(types.Part.from_bytes(data=base64.b64decode(wrist_b64), mime_type="image/jpeg"))

        # Save what we're sending to Gemini for debugging
        self._save_gemini_inputs("stage1", prompt, main_b64, wrist_b64)

        try:
            response = self.gemini_client.models.generate_content(
                model="gemini-3-pro-preview",
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            result = json.loads(response.text.strip())
            self.logger.info(f"[DetectOpponentMove] Stage 1 result: {result}")
            return result
        except Exception as e:
            self.logger.error(f"[DetectOpponentMove] Gemini stage 1 failed: {e}")
            return None

    def _ask_gemini_confirm(
        self, board_context: str, candidates: list, main_b64: str | None, wrist_b64: str | None
    ) -> dict | None:
        """Stage 2: Disambiguation when stage 1 had low confidence.

        Narrows the candidate list and asks Gemini to pick one.
        """
        prompt = (
            "You are analyzing a physical chess board to confirm which move was played.\n\n"
            f"{board_context}\n"
            f"The most likely moves are: {', '.join(candidates)}\n\n"
            "I am providing two camera images (wide + close-up overhead).\n"
            "Look carefully at which squares changed and which piece is now where.\n\n"
            "Return ONLY JSON:\n"
            '{"move_uci": "<from_square><to_square>", "confidence": 0.0-1.0, '
            '"reasoning": "brief explanation"}'
        )

        contents = [prompt]
        if main_b64:
            contents.append(types.Part.from_bytes(data=base64.b64decode(main_b64), mime_type="image/jpeg"))
        if wrist_b64:
            contents.append(types.Part.from_bytes(data=base64.b64decode(wrist_b64), mime_type="image/jpeg"))

        # Save what we're sending to Gemini for debugging
        self._save_gemini_inputs("stage2", prompt, main_b64, wrist_b64)

        try:
            response = self.gemini_client.models.generate_content(
                model="gemini-3-pro-preview",
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            result = json.loads(response.text.strip())
            self.logger.info(f"[DetectOpponentMove] Stage 2 result: {result}")
            return result
        except Exception as e:
            self.logger.error(f"[DetectOpponentMove] Gemini stage 2 failed: {e}")
            return None

    # ── Validation & application ──────────────────────────────────────

    def _validate_and_apply(self, board: chess.Board, move_uci: str) -> tuple:
        """Validate a UCI move and apply it.

        Returns (new_fen, san_str) or raises ValueError.
        """
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError as err:
            raise ValueError(f"Invalid UCI notation: {move_uci}") from err

        if move not in board.legal_moves:
            raise ValueError(f"Move {move_uci} is not legal. Legal moves: {[m.uci() for m in board.legal_moves]}")

        san = board.san(move)
        board.push(move)
        return board.fen(), san

    # ── Main execute ──────────────────────────────────────────────────

    def execute(self, robot_color: str = "white"):
        """
        Detect the opponent's last move.

        Args:
            robot_color: 'white' or 'black' — which side the robot plays.
                         On first call, initialises the game state.
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        if self.gemini_client is None:
            return "Gemini not configured (check .env.scan)", SkillResult.FAILURE

        # ── 1. Load or initialise game state ──
        state = self._load_game_state()
        if state is None:
            self._send_feedback("No game state found — initialising new game.")
            state = self._init_game_state(robot_color)

        fen = state["fen"]
        move_history = list(state.get("move_history", []))
        robot_color = state.get("robot_color", robot_color)

        self.logger.info(f"[DetectOpponentMove] Current FEN: {fen}")
        self._send_feedback(f"Current board state loaded. Turn: {state.get('turn', '?')}")

        # ── 2. Move arm to observation pose ──
        if not self._move_to_observation_pose():
            return "Failed to move arm to observation pose", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # ── 3. Capture images ──
        self._send_feedback("Capturing images...")
        main_b64, wrist_b64 = self._capture_images()

        if not main_b64 and not wrist_b64:
            self._go_to_safe_pose()
            return "No camera images available", SkillResult.FAILURE

        # ── 4. Build board context ──
        board_context, legal_moves, board = self._build_board_context(fen)

        if not legal_moves:
            self._go_to_safe_pose()
            return "No legal moves — game may be over", SkillResult.SUCCESS

        # ── 5. Stage 1: Ask Gemini ──
        self._send_feedback(f"Asking Gemini to identify the move ({len(legal_moves)} legal moves)...")
        result = self._ask_gemini_detect_move(board_context, main_b64, wrist_b64)

        if result is None:
            self._go_to_safe_pose()
            return "Gemini failed to analyse the board", SkillResult.FAILURE

        move_uci = result.get("move_uci", "")
        confidence = float(result.get("confidence", 0.0))
        reasoning = result.get("reasoning", "")

        self.logger.info(f"[DetectOpponentMove] Stage 1: move={move_uci} conf={confidence:.2f} reason={reasoning}")

        # ── 6. Stage 2: Confirm if low confidence ──
        if confidence < self.CONFIDENCE_THRESHOLD and move_uci in legal_moves:
            self._send_feedback(f"Low confidence ({confidence:.0%}) on {move_uci}. Running confirmation...")
            # Build candidate list: the detected move + a few neighbours
            candidates = [move_uci]
            for m in legal_moves:
                if m != move_uci and len(candidates) < 8:
                    # Prefer moves from or to the same squares
                    if m[:2] == move_uci[:2] or m[2:4] == move_uci[2:4]:
                        candidates.append(m)
            # Pad with random legal moves if too few
            for m in legal_moves:
                if m not in candidates and len(candidates) < 8:
                    candidates.append(m)

            result2 = self._ask_gemini_confirm(board_context, candidates, main_b64, wrist_b64)
            if result2:
                move2 = result2.get("move_uci", "")
                conf2 = float(result2.get("confidence", 0.0))
                if conf2 > confidence and move2 in legal_moves:
                    move_uci = move2
                    confidence = conf2
                    reasoning = result2.get("reasoning", reasoning)
                    self.logger.info(f"[DetectOpponentMove] Stage 2 override: move={move_uci} conf={confidence:.2f}")

        # ── 7. Validate and apply ──
        self._send_feedback(f"Detected move: {move_uci} (confidence: {confidence:.0%})")

        try:
            new_fen, san = self._validate_and_apply(board, move_uci)
        except ValueError as e:
            self._go_to_safe_pose()
            return f"Move validation failed: {e}", SkillResult.FAILURE

        # ── 8. Save state ──
        move_history.append(move_uci)
        self._save_game_state(new_fen, move_history, move_uci, robot_color)

        # ── 9. Return to safe pose ──
        self._go_to_safe_pose()

        msg = f"Opponent played {san} ({move_uci}). Confidence: {confidence:.0%}. {reasoning}"
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Move detection cancelled"
