#!/usr/bin/env python3
"""
State management for LiveAgent.

Simple rule: If user doesn't speak for too long → go proactive.
             When user speaks → back to conversation.
"""

from enum import Enum
from typing import Optional, Dict, Any
import time


class AgentMode(Enum):
    CONVERSATION = "conversation"
    PROACTIVE = "proactive"


class ActivityType(Enum):
    IDLE = "idle"
    SPEAKING = "speaking"
    EXECUTING_SKILL = "executing_skill"


class AgentState:
    """
    State manager for LiveAgent.
    
    Usage:
        state = AgentState()
        
        state.on_user_input()           # User started speaking
        state.on_user_speech("hello")   # User said something
        
        state.start_speaking("Hi!")     # Robot TTS
        state.stop_speaking()
        
        state.start_skill("wave", {})   # Skill execution
        state.stop_skill()
        
        state.on_model_response("Hi", complete=False)  # Model output
    """
    
    def __init__(self):
        self._mode = AgentMode.CONVERSATION
        self._activity = ActivityType.IDLE
        self._activity_name: Optional[str] = None
        self._last_user_input_time = time.time()
        self._last_idle_time = time.time()
        self._last_model_turn_complete_time = time.time()
        self._proactive_turn_pending = False
    
    # ==================== ACTIONS ====================
    
    def on_user_input(self) -> None:
        """User started speaking. Switch to conversation mode."""
        self._mode = AgentMode.CONVERSATION
        self._last_user_input_time = time.time()
        self._proactive_turn_pending = False
    
    def on_user_speech(self, text: str) -> None:
        """User said something (complete utterance)."""
        pass  # Hook for logging/events if needed
    
    def start_speaking(self, text: str) -> None:
        """Robot starting TTS with given text."""
        self._activity = ActivityType.SPEAKING
        self._activity_name = "speaking"
    
    def stop_speaking(self) -> None:
        """Robot finished TTS."""
        self._activity = ActivityType.IDLE
        self._activity_name = None
        self._last_idle_time = time.time()
    
    def start_skill(self, name: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Starting skill execution."""
        self._activity = ActivityType.EXECUTING_SKILL
        self._activity_name = name
    
    def stop_skill(self) -> None:
        """Skill finished."""
        self._activity = ActivityType.IDLE
        self._activity_name = None
        self._last_idle_time = time.time()
    
    def go_proactive(self) -> None:
        """Switch to proactive mode."""
        self._mode = AgentMode.PROACTIVE
        # Reset timer so first proactive prompt fires immediately
        self._last_model_turn_complete_time = 0
    
    def on_model_response(self, text: str, complete: bool) -> None:
        """Model produced output text."""
        if complete:
            self._last_model_turn_complete_time = time.time()
            self._proactive_turn_pending = False
    
    def on_proactive_prompt_sent(self) -> None:
        """Mark that we're waiting for proactive turn to complete."""
        self._proactive_turn_pending = True
    
    def on_image_sent(self) -> None:
        """Image was sent to Gemini."""
        pass  # Hook for logging/events if needed
    
    # ==================== QUERIES ====================
    
    def is_speaking(self) -> bool:
        """Use to block mic input during TTS."""
        return self._activity == ActivityType.SPEAKING
    
    def is_in_conversation(self) -> bool:
        """Check if in conversation mode."""
        return self._mode == AgentMode.CONVERSATION
    
    def is_proactive(self) -> bool:
        """Check if in proactive mode."""
        return self._mode == AgentMode.PROACTIVE
    
    def is_idle(self) -> bool:
        """Check if not doing anything."""
        return self._activity == ActivityType.IDLE
    
    def time_since_model_turn_complete(self) -> float:
        """Seconds since model finished its turn."""
        return time.time() - self._last_model_turn_complete_time
    
    def is_proactive_turn_pending(self) -> bool:
        """Check if waiting for proactive response."""
        return self._proactive_turn_pending
    
    def should_go_proactive(self, user_silence_timeout: float = 10.0, idle_duration: float = 5.0) -> bool:
        """True if user hasn't spoken for user_silence_timeout AND agent has been idle for idle_duration."""
        if self._mode != AgentMode.CONVERSATION:
            return False
        if self._activity != ActivityType.IDLE:
            return False
        
        time_since_user = time.time() - self._last_user_input_time
        time_idle = time.time() - self._last_idle_time
        
        return time_since_user > user_silence_timeout and time_idle > idle_duration
    
    # ==================== STATUS ====================
    
    def get_mode_str(self) -> str:
        """Get mode string for logging."""
        return "CONV" if self._mode == AgentMode.CONVERSATION else "PROACTIVE"
    
    def get_activity(self) -> ActivityType:
        """Get current activity type."""
        return self._activity
    
    def get_activity_name(self) -> Optional[str]:
        """Get activity name."""
        return self._activity_name
    
    def get_status(self) -> str:
        """Human-readable status string."""
        mode = self.get_mode_str()
        if self._activity == ActivityType.IDLE:
            return f"[{mode}] idle"
        return f"[{mode}] {self._activity_name}"
    
    def __repr__(self) -> str:
        return self.get_status()

