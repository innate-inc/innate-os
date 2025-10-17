#!/usr/bin/env python3
"""
Microphone Input Device

Interfaces with voice_client to get voice transcripts and forward them to the agent.
This is a pure Python class with NO ROS dependencies.
"""
import json
import time
import queue
import threading
from typing import Dict, Any
from brain_client.input_types import InputDevice


class MicroInput(InputDevice):
    """
    Microphone input device that receives voice transcripts.
    
    Note: This works in conjunction with voice_client_node which handles the
    actual microphone/OpenAI connection. This device activates/deactivates
    the forwarding of transcripts to the agent based on directive needs.
    """

    def __init__(self, logger=None):
        super().__init__(logger)
        self._data_queue = queue.Queue()
        self._thread = None
        self._stop_event = threading.Event()

    @property
    def name(self) -> str:
        return "micro"

    def on_open(self):
        """
        Start listening for voice input.
        
        In a real implementation, this might:
        - Connect to a voice service
        - Start a listening thread
        - Enable a hardware microphone
        
        For now, this sets up infrastructure for receiving data.
        The actual data comes from voice_client_node via a bridge.
        """
        if self.logger:
            self.logger.info("🎤 MicroInput activated")
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()

    def on_close(self):
        """Stop listening for voice input."""
        if self.logger:
            self.logger.info("🎤 MicroInput deactivated")
        
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def receive_transcript(self, transcript_data: Dict[str, Any]):
        """
        Called by InputManagerNode when a transcript is available.
        
        This is the bridge between voice_client and this input device.
        """
        if self.is_active():
            self._data_queue.put(transcript_data)

    def _process_loop(self):
        """Background thread that processes transcripts and sends to agent."""
        while not self._stop_event.is_set():
            try:
                # Get transcript from queue with timeout
                data = self._data_queue.get(timeout=0.5)
                
                # Extract text
                if isinstance(data, str):
                    parsed = json.loads(data)
                else:
                    parsed = data
                
                text = parsed.get('text', '')
                
                if text:
                    # Send to agent
                    self.send_data({
                        "text": text,
                        "sender": "user",
                        "source": "microphone",
                        "timestamp": parsed.get('timestamp', time.time())
                    }, data_type="chat_in")
                    
                    if self.logger:
                        self.logger.info(f"🎤 [MicroInput] Forwarded: {text}")
                
            except queue.Empty:
                continue
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error processing microphone data: {e}")

    def get_description(self) -> str:
        return "Microphone voice input via voice_client"

    def get_config(self) -> Dict[str, Any]:
        config = super().get_config()
        config.update({
            "description": "Receives voice transcripts from voice_client_node"
        })
        return config


