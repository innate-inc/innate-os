# Input Devices System

## Overview

Input devices allow directives to specify which sensory inputs they need, similar to how they specify primitives. Input devices are **pure Python classes with NO ROS dependencies**.

## How It Works

```
Directive specifies inputs → InputManagerNode activates devices → Devices collect data → Send to agent
```

### 1. Directive Declares Inputs

```python
class HelloWorld(Directive):
    def get_inputs(self) -> List[str]:
        return ["micro"]  # Need microphone input
```

### 2. Create an Input Device (Pure Python!)

```python
# inputs/my_sensor_input.py

from brain_client.input_types import InputDevice
import threading
import time

class MySensorInput(InputDevice):
    def __init__(self, logger=None):
        super().__init__(logger)
        self._thread = None
        self._stop_event = threading.Event()
    
    @property
    def name(self) -> str:
        return "my_sensor"  # Used in directives
    
    def on_open(self):
        """Start collecting data"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._collect, daemon=True)
        self._thread.start()
    
    def on_close(self):
        """Stop collecting data"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
    
    def _collect(self):
        """
        Background thread that actively collects data.
        Note: This is just one way to implement it - NOT mandatory!
        You could use websockets, callbacks, events, etc.
        """
        while not self._stop_event.is_set():
            # Get your data
            value = read_sensor()
            
            # Send to agent
            self.send_data({
                "value": value,
                "timestamp": time.time()
            }, data_type="custom")
            
            time.sleep(1.0)
```

**Key Points:**
- NO ROS imports allowed
- Implement `on_open()` to start data collection
- Implement `on_close()` to stop/cleanup
- Call `self.send_data()` to send data to agent
- Run long operations in background threads

### 3. Data Flow

```
1. User loads directive → get_inputs() returns ["micro"]
2. BrainClientNode publishes active inputs list
3. InputManagerNode calls device.on_open()
4. Device starts thread/connection and collects data
5. Device calls self.send_data(data, "chat_in") or self.send_data(data, "custom")
6. InputManagerNode publishes to /input_manager/chat_in or /input_manager/custom
7. BrainClientNode forwards to agent
```

## Architecture

```
┌─────────────────────────────────────────┐
│ Input Devices (Pure Python)             │
│ - NO ROS dependencies                   │
│ - on_open() / on_close() lifecycle      │
│ - Active data collection                │
│ - Callback via self.send_data()         │
└─────────────────────────────────────────┘
                ↕ Pure Python calls
┌─────────────────────────────────────────┐
│ InputManagerNode (ROS Bridge)           │
│ - ONLY place with ROS code              │
│ - Subscribes to ROS topics              │
│ - Calls device.on_open()/on_close()     │
│ - Handles device.send_data() callbacks  │
│ - Publishes to brain_client topics      │
└─────────────────────────────────────────┘
                ↕ ROS topics
┌─────────────────────────────────────────┐
│ BrainClientNode                         │
│ - Subscribes to /input_manager/*        │
│ - Forwards to agent via websocket       │
│ - Publishes active inputs list          │
└─────────────────────────────────────────┘
```

## Required Methods

Input devices must implement:

- `name` (property) - Unique device identifier
- `on_open()` - Start data collection (don't block!)
- `on_close()` - Stop data collection and cleanup

**How you implement `on_open()`/`on_close()` is up to you:**
- Start threads with loops
- Open websocket connections
- Register callbacks
- Set flags
- Whatever makes sense for your input!

Optional:
- `initialize()` - One-time setup when node starts
- `shutdown()` - Final cleanup when node stops

## Data Types

When calling `self.send_data(data, data_type)`:

- `"chat_in"` - Text from user (voice, keyboard, etc.)
- `"custom"` - Any other data (sensors, vision, etc.)

## Example: Microphone Input

```python
class MicroInput(InputDevice):
    def __init__(self, logger=None):
        super().__init__(logger)
        self._queue = queue.Queue()
        self._thread = None
        self._stop_event = threading.Event()
    
    @property
    def name(self) -> str:
        return "micro"
    
    def on_open(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
    
    def on_close(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
    
    def receive_transcript(self, data):
        """Called by InputManagerNode when transcript available"""
        if self.is_active():
            self._queue.put(data)
    
    def _process_loop(self):
        while not self._stop_event.is_set():
            try:
                data = self._queue.get(timeout=0.5)
                parsed = json.loads(data)
                text = parsed.get('text', '')
                
                if text:
                    self.send_data({
                        "text": text,
                        "source": "microphone"
                    }, data_type="chat_in")
            except queue.Empty:
                continue
```

## Running

Start the input manager node:

```bash
ros2 run brain_client input_manager_node
```

Or add to your launch file:

```python
Node(
    package='brain_client',
    executable='input_manager_node',
    name='input_manager_node'
)
```

## File Structure

```
innate-os/
├── inputs/                           # Put input devices here
│   └── micro_input.py               # Example: microphone
├── directives/
│   └── hello_world_directive.py     # Uses get_inputs()
└── ros2_ws/src/brain/brain_client/
    └── brain_client/
        ├── input_types.py           # Base class
        ├── input_loader.py          # Auto-discovery
        └── input_manager_node.py    # ROS bridge
```

## Summary

1. **Directives** specify inputs via `get_inputs()` → `["micro", "camera"]`
2. **Input devices** are pure Python → NO ROS, just `on_open()`/`on_close()`/`send_data()`
3. **InputManagerNode** handles ROS → Only place with ROS code
4. **Automatic loading** → Drop `*_input.py` file in `inputs/`, it's loaded
5. **Active lifecycle** → Devices start/stop based on directive needs

