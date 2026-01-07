#!/usr/bin/env python3
"""
Gaze Controller for LiveAgent - Person Tracking.

Provides autonomous person tracking using face detection.
Uses head tilt for vertical gaze and wheel rotation for horizontal pan.
"""

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple


# =============================================================================
# Face Detection
# =============================================================================

class FaceDetector:
    """
    Face detector using InspireFace SDK.
    Falls back to no detection if InspireFace is not available.
    """
    
    def __init__(self, min_detection_confidence: float = 0.5):
        self._min_confidence = min_detection_confidence
        self._session = None
        
        try:
            # Lazy import: inspireface is heavy (~1s), only load when FaceDetector is created
            import inspireface as isf
            param = isf.SessionCustomParameter()
            self._session = isf.InspireFaceSession(
                param=param,
                detect_mode=isf.HF_DETECT_MODE_ALWAYS_DETECT,
                max_detect_num=10
            )
            self._session.set_detection_confidence_threshold(min_detection_confidence)
            print(f"✓ InspireFace initialized (confidence: {min_detection_confidence})")
        except ImportError:
            print("⚠️ InspireFace not available - gaze tracking disabled")
        except Exception as e:
            print(f"⚠️ InspireFace initialization failed: {e}")
    
    @property
    def is_available(self) -> bool:
        return self._session is not None
    
    def detect(self, frame) -> List[dict]:
        """
        Detect faces in frame.
        
        Returns:
            List of dicts with 'center_x', 'center_y', 'width', 'height', 'confidence'
            Coordinates are normalized [0, 1]
        """
        if not self._session:
            return []
        
        try:
            h, w = frame.shape[:2]
            detections = self._session.face_detection(frame)
            
            faces = []
            for face in detections:
                x1, y1, x2, y2 = face.location
                box_w = (x2 - x1) / w
                box_h = (y2 - y1) / h
                cx = (x1 + x2) / 2 / w
                cy = (y1 + y2) / 2 / h
                
                faces.append({
                    'center_x': cx,
                    'center_y': cy,
                    'width': box_w,
                    'height': box_h,
                    'confidence': 0.9
                })
            
            return faces
        except Exception:
            return []


# =============================================================================
# Gaze State
# =============================================================================

class GazeState(Enum):
    IDLE = "idle"
    PURSUIT = "pursuit"


@dataclass
class GazeTarget:
    tilt: float
    pan: float = 0.0
    priority: int = 0
    source: str = "manual"


# =============================================================================
# Gaze Controller
# =============================================================================

class GazeController:
    """
    Gaze controller with head tilt and wheel-based pan.
    """
    
    MIN_TILT = -25
    MAX_TILT = 15
    PAN_GAIN = 0.4
    CAMERA_HFOV = 120.0
    CAMERA_VFOV = 50.0
    
    def __init__(
        self,
        head_command_fn: Callable[[int], None],
        wheel_rotate_fn: Optional[Callable[[float, float], None]] = None,
        update_rate: float = 30.0
    ):
        self._head_command = head_command_fn
        self._wheel_rotate = wheel_rotate_fn
        self._update_rate = update_rate
        
        self._state = GazeState.IDLE
        self._current_tilt = 0.0
        self._target: Optional[GazeTarget] = None
        self._last_commanded_tilt = 0
        
        self._running = False
        self._tilt_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        self._last_pan_time = 0.0
        self._pan_cooldown = 0.5
    
    def start(self):
        if self._running:
            return
        self._running = True
        self._tilt_thread = threading.Thread(target=self._tilt_loop, daemon=True)
        self._tilt_thread.start()
    
    def stop(self):
        self._running = False
        if self._tilt_thread:
            self._tilt_thread.join(timeout=1.0)
            self._tilt_thread = None
    
    def look_at(self, tilt: float, pan: float = 0.0, priority: int = 0, source: str = "manual"):
        tilt = max(self.MIN_TILT, min(self.MAX_TILT, tilt))
        
        with self._lock:
            if self._target is None or priority >= self._target.priority:
                self._target = GazeTarget(tilt=tilt, pan=pan, priority=priority, source=source)
                self._state = GazeState.PURSUIT
                
                if self._wheel_rotate and abs(pan) > 5:
                    self._execute_pan(pan)
    
    def set_idle(self):
        with self._lock:
            self._target = None
            self._state = GazeState.IDLE
    
    def track_face(self, face: dict, frame_shape: Tuple[int, int]):
        pan_error = (face['center_x'] - 0.5) * self.CAMERA_HFOV
        error_normalized = 0.5 - face['center_y']
        tilt_error_degrees = error_normalized * self.CAMERA_VFOV
        
        Kp = 0.3
        tilt_correction = tilt_error_degrees * Kp
        new_tilt = self._current_tilt + tilt_correction
        new_tilt = max(self.MIN_TILT, min(self.MAX_TILT, new_tilt))
        
        self.look_at(tilt=new_tilt, pan=pan_error, priority=1, source="face")
    
    def _execute_pan(self, pan_degrees: float):
        if not self._wheel_rotate:
            return
        
        now = time.time()
        if now - self._last_pan_time < self._pan_cooldown:
            return
        
        angular_speed = -math.copysign(self.PAN_GAIN, pan_degrees)
        duration = min(abs(pan_degrees) / 30.0, 0.5)
        
        if duration > 0.05:
            self._wheel_rotate(angular_speed, duration)
            self._last_pan_time = now
    
    @property
    def state(self) -> GazeState:
        return self._state
    
    @property
    def current_tilt(self) -> float:
        return self._current_tilt
    
    def _tilt_loop(self):
        dt = 1.0 / self._update_rate
        
        while self._running:
            loop_start = time.time()
            
            with self._lock:
                new_tilt = self._compute_tilt()
            
            tilt_int = int(round(new_tilt))
            tilt_int = max(self.MIN_TILT, min(self.MAX_TILT, tilt_int))
            
            if tilt_int != self._last_commanded_tilt:
                self._head_command(tilt_int)
                self._last_commanded_tilt = tilt_int
            
            self._current_tilt = new_tilt
            
            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)
    
    def _compute_tilt(self) -> float:
        if self._state == GazeState.PURSUIT:
            if self._target is None:
                self._state = GazeState.IDLE
                return self._current_tilt
            return self._target.tilt
        return self._current_tilt


# =============================================================================
# Person Tracker
# =============================================================================

class PersonTracker:
    """
    Autonomous person tracking using face detection.
    
    FaceDetector is lazy-loaded on first start() for faster agent startup.
    """
    
    def __init__(
        self,
        gaze: GazeController,
        get_frame_fn: Callable[[], Optional[tuple]],
        perception_rate: float = 5.0
    ):
        self._gaze = gaze
        self._get_frame = get_frame_fn
        self._rate = perception_rate
        
        # Lazy init: FaceDetector created on first start() to speed up agent startup
        self._detector: Optional[FaceDetector] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        self._last_face_time = 0.0
        self._face_timeout = 5.0
        self._tracked_face_center: Optional[Tuple[float, float]] = None
        self._switch_threshold = 1.5
        self._match_radius = 0.15
    
    @property
    def is_available(self) -> bool:
        return self._detector is not None and self._detector.is_available
    
    def _init_detector(self):
        """Initialize FaceDetector in background thread."""
        self._detector = FaceDetector(min_detection_confidence=0.3)
    
    def start(self):
        if self._running:
            return
        
        self._running = True
        self._gaze.start()
        self._thread = threading.Thread(target=self._perception_loop, daemon=True)
        self._thread.start()
        
        # Lazy init: Load FaceDetector in background thread (non-blocking)
        if self._detector is None:
            threading.Thread(target=self._init_detector, daemon=True).start()
    
    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._gaze.stop()
    
    def _perception_loop(self):
        dt = 1.0 / self._rate
        
        while self._running:
            loop_start = time.time()
            
            result = self._get_frame()
            if result is not None:
                frame, shape = result
                self._process_frame(frame, shape)
            
            if time.time() - self._last_face_time > self._face_timeout:
                if self._gaze.state != GazeState.IDLE:
                    self._gaze.look_at(tilt=0, pan=0, priority=0, source="neutral")
                    self._gaze.set_idle()
                    self._tracked_face_center = None
            
            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)
    
    def _process_frame(self, frame, shape: Tuple[int, int]):
        # Skip if detector not yet initialized (loading in background)
        if self._detector is None:
            return
        
        faces = self._detector.detect(frame)
        
        if not faces:
            return
        
        best = self._select_face(faces)
        self._tracked_face_center = (best['center_x'], best['center_y'])
        self._last_face_time = time.time()
        self._gaze.track_face(best, shape)
    
    def _select_face(self, faces: List[dict]) -> dict:
        largest = max(faces, key=lambda f: f['width'] * f['height'])
        
        if self._tracked_face_center is None:
            return largest
        
        prev_cx, prev_cy = self._tracked_face_center
        
        def distance_to_tracked(f):
            dx = f['center_x'] - prev_cx
            dy = f['center_y'] - prev_cy
            return math.sqrt(dx*dx + dy*dy)
        
        closest_match = min(faces, key=distance_to_tracked)
        match_dist = distance_to_tracked(closest_match)
        
        if match_dist < self._match_radius:
            return closest_match
        return largest


# =============================================================================
# ROS2 Integration for LiveAgent
# =============================================================================

class ROSGazeController:
    """
    ROS2-integrated gaze controller for LiveAgent.
    
    Uses BrainClientNode's existing camera subscription and hardware interfaces.
    
    Usage:
        gaze = ROSGazeController(node, logger)
        gaze.start()  # Begin autonomous tracking
        gaze.stop()   # Stop tracking
    """
    
    def __init__(
        self,
        node,
        logger,
        cmd_vel_topic: str = "/cmd_vel"
    ):
        self._node = node
        self._logger = logger
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._gaze = None
        self._tracker = None
        self._available = False
        
        # Import hardware interfaces (inline to avoid circular imports)
        try:
            from brain_client.head_interface import HeadInterface
            from brain_client.mobility_interface import MobilityInterface
            
            self._head = HeadInterface(node, logger)
            self._mobility = MobilityInterface(node, logger, cmd_vel_topic)
            
            self._gaze = GazeController(
                head_command_fn=self._head.set_position,
                wheel_rotate_fn=self._mobility.rotate_in_place
            )
            
            self._tracker = PersonTracker(
                gaze=self._gaze,
                get_frame_fn=self._get_frame
            )
            
            self._available = self._tracker.is_available
            if self._available:
                logger.info("👁️ Gaze controller initialized")
            else:
                logger.warning("👁️ Gaze controller initialized (face detection unavailable)")
        except Exception as e:
            logger.error(f"Failed to initialize gaze controller: {e}")
    
    @property
    def is_available(self) -> bool:
        return self._available
    
    def update_frame(self, frame) -> None:
        """Update latest camera frame (called from LiveAgentRunner)."""
        with self._frame_lock:
            self._latest_frame = frame
    
    def _get_frame(self) -> Optional[tuple]:
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            frame = self._latest_frame.copy()
        return frame, (frame.shape[0], frame.shape[1])
    
    def start(self):
        """Start autonomous person tracking."""
        if self._tracker:
            self._tracker.start()
            self._logger.info("👁️ Gaze tracking started")
    
    def stop(self):
        """Stop person tracking."""
        if self._tracker:
            self._tracker.stop()
            self._logger.info("👁️ Gaze tracking stopped")
    
    @property
    def state(self) -> GazeState:
        if self._gaze:
            return self._gaze.state
        return GazeState.IDLE

