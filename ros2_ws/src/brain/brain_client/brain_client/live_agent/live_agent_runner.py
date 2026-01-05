#!/usr/bin/env python3
"""
LiveAgentRunner - Runs Gemini Live API for LiveAgent directives.

This is the embedded engine that handles real-time voice conversation
with the robot. It runs in a background thread and uses the same
skill execution infrastructure as CloudAgent.
"""

import asyncio
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Any, Union, Dict, List

import cv2
import pyaudio
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image as PILImage
from rclpy.action import ActionClient
from std_msgs.msg import String

from brain_messages.action import ExecutePrimitive
from brain_client.agent_types import LiveAgent
from .state import AgentState, ActivityType
from .text_processor import TextProcessor

load_dotenv()


# ==================== CONSTANTS ====================

class AudioConfig:
    """Audio configuration for Gemini Live API."""
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    SAMPLE_RATE = 16000  # Gemini expects 16kHz
    CHUNK_SIZE = 1024
    QUEUE_SIZE = 5


class ImageConfig:
    """Image processing configuration."""
    RESIZE_WIDTH = 640
    RESIZE_HEIGHT = 480
    JPEG_QUALITY = 80


# ==================== ACTION TYPES ====================

@dataclass
class SpeakAction:
    """Action to speak text via TTS."""
    text: str


@dataclass
class SkillAction:
    """Action to execute a skill call."""
    session: Any
    function_call: Any


Action = Union[SpeakAction, SkillAction]


class LiveAgentRunner:
    """
    Runs the Gemini Live API loop for LiveAgent.
    
    Handles:
    - Microphone input streaming to Gemini
    - Periodic image updates to Gemini
    - Model response processing and TTS
    - Skill execution via ExecutePrimitive action
    - Proactive behavior when user is silent
    - Gaze tracking (optional)
    
    Args:
        node: ROS2 node (from BrainClientNode)
        agent: LiveAgent instance with config
        skill_client: ExecutePrimitive action client
        skills_dict: Dictionary of available skills
        tts_handler: TTSHandler for speech synthesis
        logger: ROS logger
        gaze_controller: Optional gaze controller for person tracking
        chat_out_pub: ROS publisher for chat messages to app
        chat_history: List to append chat history
    """
    
    def __init__(
        self,
        node,
        agent: LiveAgent,
        skill_client: ActionClient,
        skills_dict: Dict[str, Any],
        tts_handler,
        logger,
        gaze_controller=None,
        chat_out_pub=None,
        chat_history=None,
    ):
        self.node = node
        self.agent = agent
        self.skill_client = skill_client
        self.skills_dict = skills_dict
        self.tts_handler = tts_handler
        self.logger = logger
        self.gaze_controller = gaze_controller
        self.chat_out_pub = chat_out_pub
        self.chat_history = chat_history
        
        # Load config from agent
        self.system_instruction = agent.get_system_instruction()
        self.proactive_prompt = agent.get_proactive_prompt()
        self.proactive_timeout = agent.get_proactive_timeout()
        self.image_interval = agent.get_image_interval()
        self.voice_name = agent.get_voice_name()
        
        # Hardcoded timing
        self.proactive_idle_buffer = 3.0
        self.proactive_prompt_delay = 3.0
        
        # Get API key from environment
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        
        # Runtime state
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Components initialized in start()
        self.state: Optional[AgentState] = None
        self.text_processor: Optional[TextProcessor] = None
        self.pya: Optional[pyaudio.PyAudio] = None
        self.audio_stream: Optional[Any] = None
        self.audio_queue_mic: Optional[asyncio.Queue] = None
        self.action_queue: Optional[asyncio.Queue] = None
        self.current_action_task: Optional[asyncio.Task] = None
        
        # Image handling - will use node's camera subscription
        self.latest_image = None
        self.image_lock = threading.Lock()
        
        self.logger.info(f"LiveAgentRunner initialized for agent '{agent.id}'")
    
    def start(self) -> None:
        """Start the live agent in background thread."""
        if self.running:
            self.logger.warning("LiveAgentRunner already running")
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
        self.logger.info("🎙️ LiveAgentRunner started")
    
    def stop(self) -> None:
        """Stop the live agent."""
        self.logger.info("🎙️ Stopping LiveAgentRunner...")
        self.running = False
        
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        if self.gaze_controller:
            self.gaze_controller.stop()
        
        # Clean up audio (try/except needed - resources may be in bad state)
        if self.audio_stream:
            try:
                self.audio_stream.close()
            except Exception:
                pass
        if self.pya:
            try:
                self.pya.terminate()
            except Exception:
                pass
        
        self.logger.info("🎙️ LiveAgentRunner stopped")
    
    def update_image(self, image) -> None:
        """Update the latest camera image (called from BrainClientNode)."""
        with self.image_lock:
            self.latest_image = image
    
    def _run_async_loop(self) -> None:
        """Run the async event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self.logger.error(f"LiveAgentRunner error: {e}")
        finally:
            self._loop.close()
            self._loop = None
    
    async def _main(self) -> None:
        """Main Gemini Live API loop."""
        # Initialize components
        self.state = AgentState()
        self.text_processor = TextProcessor()
        self.pya = pyaudio.PyAudio()
        self.audio_queue_mic = asyncio.Queue(maxsize=AudioConfig.QUEUE_SIZE)
        self.action_queue = asyncio.Queue()
        
        # Initialize Gemini client
        client = genai.Client(api_key=self.api_key)
        
        # Build tool declarations from skills
        tools = self._build_tools(types)
        
        # Configure Gemini Live session
        config = {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": self.voice_name}
                }
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "system_instruction": self.system_instruction,
            "tools": tools,
        }
        
        model_name = "gemini-2.0-flash-exp"
        
        try:
            self.logger.info(f"Connecting to Gemini Live API ({model_name})...")
            async with client.aio.live.connect(model=model_name, config=config) as session:
                self.logger.info("✅ Connected to Gemini Live API")
                
                await asyncio.gather(
                    self._listen_audio(),
                    self._send_audio(session),
                    self._receive_and_dispatch(session, types),
                    self._action_worker(),
                    self._monitor_idle(),
                    self._send_images(session),
                    self._gaze_monitor(),
                    self._proactive_worker(session),
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Gemini Live API error: {e}")
    
    def _build_tools(self, types) -> List:
        """Build Gemini tool declarations from available skills."""
        declarations = []
        
        for skill_name in self.agent.get_skills():
            if skill_name not in self.skills_dict:
                self.logger.warning(f"Skill '{skill_name}' not found in skills_dict")
                continue
            
            skill = self.skills_dict[skill_name]
            metadata = getattr(skill, 'metadata', {})
            
            # Get description from metadata (try 'description' first, then 'guidelines')
            description = metadata.get('description') or metadata.get('guidelines') or f"Execute {skill_name} skill"
            
            # Get parameters - use full schema if provided, otherwise build from 'inputs'
            if 'parameters' in metadata:
                # Full Gemini-style parameters schema provided
                parameters = metadata['parameters']
            else:
                # Build from simple 'inputs' dict (legacy format)
                inputs = metadata.get('inputs', {})
                properties = {}
                required = []
                
                for param_name, param_type in inputs.items():
                    param_schema = {"type": "string"}  # Default
                    if "float" in str(param_type).lower():
                        param_schema = {"type": "number"}
                    elif "int" in str(param_type).lower():
                        param_schema = {"type": "integer"}
                    elif "bool" in str(param_type).lower():
                        param_schema = {"type": "boolean"}
                    
                    properties[param_name] = param_schema
                
                parameters = {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            
            # Create function declaration
            func_decl = types.FunctionDeclaration(
                name=skill_name,
                description=description,
                parameters=parameters,
            )
            declarations.append(func_decl)
            self.logger.info(f"🔧 Registered skill: {skill_name}")
        
        if declarations:
            return [types.Tool(function_declarations=declarations)]
        return []
    
    # ==================== AUDIO HANDLING ====================
    
    async def _listen_audio(self) -> None:
        """Listen for audio from microphone and queue it."""
        try:
            mic_info = self.pya.get_default_input_device_info()
            self.audio_stream = await asyncio.to_thread(
                self.pya.open,
                format=AudioConfig.FORMAT,
                channels=AudioConfig.CHANNELS,
                rate=AudioConfig.SAMPLE_RATE,
                input=True,
                input_device_index=mic_info["index"],
                frames_per_buffer=AudioConfig.CHUNK_SIZE,
            )
            
            self.logger.info("🎤 Microphone active")
            
            kwargs = {"exception_on_overflow": False}
            while self.running:
                data = await asyncio.to_thread(
                    self.audio_stream.read,
                    AudioConfig.CHUNK_SIZE,
                    **kwargs
                )
                await self.audio_queue_mic.put({"data": data, "mime_type": "audio/pcm"})
                
        except Exception as e:
            self.logger.error(f"Microphone error: {e}")
    
    async def _send_audio(self, session) -> None:
        """Send mic audio to Gemini."""
        while self.running:
            msg = await self.audio_queue_mic.get()
            await session.send_realtime_input(audio=msg)
    
    # ==================== IMAGE HANDLING ====================
    
    def _get_image_jpeg(self) -> Optional[bytes]:
        """Get latest image as JPEG bytes for Gemini."""
        with self.image_lock:
            if self.latest_image is None:
                return None
            
            try:
                rgb_image = cv2.cvtColor(self.latest_image, cv2.COLOR_BGR2RGB)
                pil_image = PILImage.fromarray(rgb_image)
                pil_image = pil_image.resize((ImageConfig.RESIZE_WIDTH, ImageConfig.RESIZE_HEIGHT))
                
                buffer = io.BytesIO()
                pil_image.save(buffer, format='JPEG', quality=ImageConfig.JPEG_QUALITY)
                return buffer.getvalue()
            except Exception as e:
                self.logger.warning(f"Image encoding error: {e}")
                return None
    
    async def _send_images(self, session) -> None:
        """Send camera images to Gemini periodically."""
        while self.running:
            image_bytes = self._get_image_jpeg()
            if image_bytes:
                try:
                    await session.send(input={
                        "mime_type": "image/jpeg",
                        "data": image_bytes
                    })
                    self.state.on_image_sent()
                except Exception as e:
                    self.logger.warning(f"Image send error: {e}")
            
            await asyncio.sleep(self.image_interval)
    
    # ==================== IDLE/PROACTIVE ====================
    
    async def _monitor_idle(self) -> None:
        """Switch to proactive mode when user is silent too long AND agent is idle."""
        while self.running:
            if self.state.should_go_proactive(
                user_silence_timeout=self.proactive_timeout,
                idle_duration=self.proactive_idle_buffer
            ):
                self.logger.info("User silent & idle - switching to PROACTIVE")
                self.state.go_proactive()
            await asyncio.sleep(1.0)
    
    async def _proactive_worker(self, session) -> None:
        """Send proactive prompts when in proactive mode."""
        while self.running:
            if (self.state.is_proactive() 
                and self.state.is_idle()
                and not self.state.is_proactive_turn_pending()
                and self.state.time_since_model_turn_complete() > self.proactive_prompt_delay):
                
                self.logger.info("🤖 PROACTIVE: Sending prompt")
                self.state.on_proactive_prompt_sent()
                try:
                    await session.send_client_content(
                        turns=[{"role": "user", "parts": [{"text": self.proactive_prompt}]}],
                        turn_complete=True
                    )
                except Exception as e:
                    self.logger.error(f"Failed to send proactive prompt: {e}")
            await asyncio.sleep(1.0)
    
    # ==================== GAZE CONTROL ====================
    
    async def _gaze_monitor(self) -> None:
        """Control gaze tracking based on agent state."""
        if not self.gaze_controller:
            return
        
        gaze_active = False
        
        while self.running:
            # Gaze ON when: CONVERSATION mode AND NOT executing skill
            should_gaze = (
                self.state.is_in_conversation() and 
                self.state.get_activity() != ActivityType.EXECUTING_SKILL
            )
            
            if should_gaze and not gaze_active:
                self.gaze_controller.start()
                self.logger.info("👁️ Gaze tracking enabled")
                gaze_active = True
            elif not should_gaze and gaze_active:
                self.gaze_controller.stop()
                self.logger.info("👁️ Gaze tracking paused")
                gaze_active = False
            
            await asyncio.sleep(0.5)
    
    # ==================== ACTION WORKER ====================
    
    async def _action_worker(self) -> None:
        """Process action queue sequentially."""
        while self.running:
            try:
                action = await asyncio.wait_for(self.action_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            
            self.current_action_task = asyncio.current_task()
            
            try:
                if isinstance(action, SpeakAction):
                    await self._speak(action.text)
                elif isinstance(action, SkillAction):
                    await self._execute_skill(action.session, action.function_call)
            except asyncio.CancelledError:
                self.logger.info("Action cancelled")
            
            self.current_action_task = None
    
    async def _speak(self, text: str) -> None:
        """Speak text with TTS."""
        self.logger.info(f"🤖 Speaking: {text[:50]}...")
        self.state.start_speaking(text)
        try:
            if self.tts_handler and self.tts_handler.is_available():
                await asyncio.to_thread(self.tts_handler.speak_text, text)
            else:
                self.logger.warning("TTS not available, skipping speech")
        finally:
            self.state.stop_speaking()
    
    async def _execute_skill(self, session, fc) -> None:
        """Execute a skill call via ExecutePrimitive action."""
        skill_name = fc.name
        skill_params = dict(fc.args) if hasattr(fc, 'args') else {}
        
        # Stop gaze immediately when skill starts
        if self.gaze_controller:
            self.gaze_controller.stop()
        
        await asyncio.sleep(0.3)
        
        self.logger.info(f"🔧 Skill: {skill_name} {skill_params}")
        self.state.start_skill(skill_name, skill_params)
        
        try:
            # Execute via ROS action
            result = await self._send_skill_goal(skill_name, skill_params)
            
            await session.send_tool_response(function_responses=[
                types.FunctionResponse(id=fc.id, name=fc.name, response=result)
            ])
        except asyncio.CancelledError:
            self.logger.info(f"Skill {skill_name} interrupted")
            await session.send_tool_response(function_responses=[
                types.FunctionResponse(
                    id=fc.id, name=fc.name,
                    response={
                        "status": "interrupted_by_user",
                        "message": "User interrupted. Do NOT retry."
                    }
                )
            ])
        finally:
            self.state.stop_skill()
    
    async def _send_skill_goal(self, skill_name: str, params: dict) -> dict:
        """Send skill goal and wait for result."""
        goal = ExecutePrimitive.Goal()
        goal.primitive_type = skill_name
        goal.inputs = json.dumps(params)
        
        # Wait for action server
        if not self.skill_client.wait_for_server(timeout_sec=5.0):
            return {"success": False, "message": "Skill server not available"}
        
        # Send goal
        future = self.skill_client.send_goal_async(goal)
        
        # Wait for goal acceptance
        goal_handle = await asyncio.get_event_loop().run_in_executor(
            None, lambda: future.result()
        )
        
        if not goal_handle.accepted:
            return {"success": False, "message": "Skill goal rejected"}
        
        # Wait for result
        result_future = goal_handle.get_result_async()
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: result_future.result()
        )
        
        return {
            "success": result.result.success,
            "message": result.result.message
        }
    
    def _handle_interrupt(self) -> None:
        """Cancel current action (skill execution) on interrupt."""
        if self.current_action_task and not self.current_action_task.done():
            self.current_action_task.cancel()
            self.logger.info("Cancelled current action")
    
    # ==================== CHAT PUBLISHING ====================
    
    def _publish_chat(self, text: str, sender: str = "robot") -> None:
        """Publish chat message to the mobile app."""
        if not text.strip():
            return
        
        chat_entry = {
            "sender": sender,
            "text": text.strip(),
            "timestamp": time.time()
        }
        
        if self.chat_history is not None:
            self.chat_history.append(chat_entry)
        
        if self.chat_out_pub is not None:
            out_msg = String(data=json.dumps(chat_entry))
            self.chat_out_pub.publish(out_msg)
    
    # ==================== RECEIVE & DISPATCH ====================
    
    async def _receive_and_dispatch(self, session, types) -> None:
        """Receive Gemini transcripts and dispatch to action queue."""
        buffer = ""
        user_buffer = ""
        
        while self.running:
            turn = session.receive()
            
            async for response in turn:
                if not self.running:
                    break
                
                sc = response.server_content
                
                # Interrupted - cancel current action
                if sc and sc.interrupted:
                    self.logger.info("🛑 Model interrupted by user")
                    self.state.on_user_input()
                    self._handle_interrupt()
                
                # User speech
                if sc and sc.input_transcription and sc.input_transcription.text:
                    if not user_buffer:
                        self.state.on_user_input()
                    user_buffer += sc.input_transcription.text
                    self.logger.debug(f"👤 User: {sc.input_transcription.text}")
                
                # Flush user speech when model responds
                if user_buffer.strip() and (response.tool_call or (sc and sc.model_turn)):
                    self.logger.info(f"👤 User said: '{user_buffer.strip()}'")
                    self.state.on_user_speech(user_buffer.strip())
                    self._publish_chat(user_buffer.strip(), "user")  # Publish to app
                    user_buffer = ""
                
                # Tool/skill calls
                if response.tool_call:
                    self.logger.info("Model called skill")
                    if buffer.strip():
                        self.state.on_model_response(buffer.strip(), complete=False)
                        self.action_queue.put_nowait(SpeakAction(text=buffer.strip()))
                        buffer = ""
                    self._queue_skill_calls(session, response)
                
                # Model speech -> queue TTS
                if sc and sc.output_transcription and sc.output_transcription.text:
                    buffer = self._handle_model_text(sc.output_transcription.text, buffer)
                
                # Turn complete
                if sc and sc.turn_complete:
                    self.logger.debug("🤖 Turn complete")
                    buffer = self._handle_turn_complete(buffer)
    
    def _queue_skill_calls(self, session, response) -> None:
        """Queue skill calls for execution."""
        for fc in response.tool_call.function_calls:
            self.action_queue.put_nowait(SkillAction(session=session, function_call=fc))
    
    def _handle_model_text(self, text: str, buffer: str) -> str:
        """Process model output transcription, queue complete sentences."""
        complete, buffer = self.text_processor.extract_complete_sentences(buffer, text)
        if complete.strip():
            self.state.on_model_response(complete.strip(), complete=False)
            self._publish_chat(complete.strip(), "robot")  # Publish to app
            self.action_queue.put_nowait(SpeakAction(text=complete.strip()))
        return buffer
    
    def _handle_turn_complete(self, buffer: str) -> str:
        """Queue any remaining buffered text on turn completion."""
        if buffer.strip():
            self.state.on_model_response(buffer.strip(), complete=False)
            self._publish_chat(buffer.strip(), "robot")  # Publish to app
            self.action_queue.put_nowait(SpeakAction(text=buffer.strip()))
        self.state.on_model_response("", complete=True)
        return ""

