# Client Commands Reference

This document describes all available methods for clients to send commands to the robot in the Innate Simulation environment.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [HTTP REST API](#http-rest-api)
  - [Robot Control](#robot-control)
  - [Video Feeds](#video-feeds)
  - [Configuration](#configuration)
- [WebSocket Communication](#websocket-communication)
  - [Chat Interface](#chat-interface)
- [ROS Bridge (Advanced)](#ros-bridge-advanced)
  - [Topics](#topics)
  - [Services](#services)
- [Examples](#examples)

---

## Overview

The Innate Simulation provides three main interfaces for client communication:

| Interface | Base URL | Use Case |
|-----------|----------|----------|
| **HTTP REST API** | `http://localhost:8000` | Robot control, video feeds, configuration |
| **WebSocket (Chat)** | `ws://localhost:8000/ws/chat` | Real-time chat with robot |
| **ROS Bridge** | `ws://localhost:9090` | Low-level control, sensor data |

---

## Quick Start

### Send a Chat Message

```python
import asyncio
import websockets
import json

async def send_message(text):
    uri = "ws://localhost:8000/ws/chat?user_id=my_client"
    async with websockets.connect(uri) as ws:
        await ws.send(text)
        response = await ws.recv()
        print(json.loads(response))

asyncio.run(send_message("Hello robot!"))
```

### Reset the Robot

```bash
curl -X POST http://localhost:8000/reset_robot
```

### Get Robot Position

```bash
curl http://localhost:8000/get_robot_position
```

---

## HTTP REST API

All HTTP endpoints are served on **port 8000** by default.

### Robot Control

#### GET `/get_robot_position`

Returns the current 3D position and timestamp of the robot.

**Response:**
```json
{
  "position": [12.5, 7.3, 0.2],
  "timestamp": 1682574329.456
}
```

**Example:**
```bash
curl http://localhost:8000/get_robot_position
```

---

#### POST `/reset_robot`

Resets the robot to its starting position. Optionally loads a memory state and/or sets a custom pose.

**Request Body (all fields optional):**
```json
{
  "memory_state": "init_mem_patrol_mode",
  "position": [1.0, 2.0, 0.0],
  "orientation": [1.0, 0.0, 0.0, 0.0]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `memory_state` | string | Identifier for memory state to load |
| `position` | `[x, y, z]` | Robot position coordinates |
| `orientation` | `[w, x, y, z]` | Robot orientation as quaternion |

**Response:**
```json
{
  "status": "reset_enqueued",
  "memory_state": "init_mem_patrol_mode",
  "pose": {
    "position": [1.0, 2.0, 0.0],
    "orientation": [1.0, 0.0, 0.0, 0.0]
  }
}
```

**Examples:**
```bash
# Simple reset
curl -X POST http://localhost:8000/reset_robot

# Reset with memory state
curl -X POST http://localhost:8000/reset_robot \
  -H "Content-Type: application/json" \
  -d '{"memory_state": "security_patrol_mode"}'

# Reset with custom pose
curl -X POST http://localhost:8000/reset_robot \
  -H "Content-Type: application/json" \
  -d '{"position": [5.0, 3.0, 0.0], "orientation": [1.0, 0.0, 0.0, 0.0]}'
```

---

#### POST `/set_directive`

Updates the robot's behavioral directive (agent personality/mode).

**Request Body:**
```json
{
  "text": "security_patrol"
}
```

**Response:**
```json
{
  "status": "directive_enqueued"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/set_directive \
  -H "Content-Type: application/json" \
  -d '{"text": "security_patrol"}'
```

---

#### POST `/set_brain_active`

Activates or deactivates the robot's brain (AI processing).

**Request Body:**
```json
{
  "active": true
}
```

**Response:**
```json
{
  "status": "brain_command_enqueued"
}
```

**Example:**
```bash
# Activate brain
curl -X POST http://localhost:8000/set_brain_active \
  -H "Content-Type: application/json" \
  -d '{"active": true}'

# Deactivate brain
curl -X POST http://localhost:8000/set_brain_active \
  -H "Content-Type: application/json" \
  -d '{"active": false}'
```

---

#### GET `/get_available_agents`

Returns the list of available agents/directives configured in the robot brain.

**Response:**
```json
{
  "agents": [
    {
      "id": "security_patrol",
      "display_name": "Security Patrol",
      "display_icon": "base64_encoded_icon_data",
      "prompt": "You are a security robot...",
      "skills": ["patrol", "surveillance", "alert"]
    }
  ],
  "current_agent_id": "security_patrol",
  "startup_agent_id": "default_agent"
}
```

---

### Video Feeds

#### GET `/video_feed`

Streams the robot's first-person camera feed (MJPEG).

**Content-Type:** `multipart/x-mixed-replace; boundary=frame`

**Example (display in browser):**
```html
<img src="http://localhost:8000/video_feed" />
```

---

#### GET `/video_feed_chase`

Streams the third-person (chase) camera feed (MJPEG).

**Example:**
```html
<img src="http://localhost:8000/video_feed_chase" />
```

---

#### GET `/video_feeds_ready`

Checks if the simulation is running and video feeds are available.

**Response:**
```json
{
  "ready": true,
  "message": "Simulation is running"
}
```

---

### Configuration

#### POST `/set_environment`

Configures the simulation environment. Can either provide a configuration directly or load from a preset file.

**Option 1 - Load from file:**
```json
{
  "config_name": "walking_man_path"
}
```

**Option 2 - Direct configuration:**
```json
{
  "config": {
    "objects": [
      {
        "type": "human",
        "position": [5.0, 3.0, 0.0],
        "animation": "walking"
      }
    ]
  }
}
```

**Available preset configs:** Located in `data/environments/`
- `default.json`
- `laying_man_corner.json`
- `walking_man_origin.json`
- `walking_man_path.json`

**Response:**
```json
{
  "status": "success",
  "message": "Environment configuration command sent to simulation.",
  "source": "file: walking_man_path.json"
}
```

**Examples:**
```bash
# Load preset environment
curl -X POST http://localhost:8000/set_environment \
  -H "Content-Type: application/json" \
  -d '{"config_name": "walking_man_path"}'

# Custom configuration
curl -X POST http://localhost:8000/set_environment \
  -H "Content-Type: application/json" \
  -d '{"config": {"objects": [{"type": "box", "position": [2, 2, 0]}]}}'
```

---

#### POST `/shutdown`

Gracefully shuts down the simulator.

**Response:**
```json
{
  "status": "success",
  "message": "Shutdown initiated"
}
```

---

## WebSocket Communication

### Chat Interface

**WebSocket URL:** `ws://localhost:8000/ws/chat?user_id=<your_user_id>`

The chat WebSocket allows real-time bidirectional communication with the robot.

#### Connection

Connect with a unique user ID to identify your client:

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/chat?user_id=client_001");
```

#### Sending Messages

Send plain text messages to the robot:

```javascript
ws.send("Hello, robot! Can you patrol the building?");
```

#### Receiving Messages

Messages from the robot are received as JSON:

```json
{
  "sender": "robot",
  "text": "Hello! I'll start patrolling the building now.",
  "timestamp": 1682574329.456
}
```

#### Message Format

| Field | Type | Description |
|-------|------|-------------|
| `sender` | string | `"user"`, `"robot"`, or `"system"` |
| `text` | string | Message content |
| `timestamp` | float | Unix timestamp |

#### Full Example

```python
import asyncio
import websockets
import json

async def chat_client():
    uri = "ws://localhost:8000/ws/chat?user_id=python_client"
    
    async with websockets.connect(uri) as websocket:
        # Send a message
        await websocket.send("Go to the kitchen")
        
        # Receive responses
        while True:
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=30)
                message = json.loads(response)
                print(f"[{message['sender']}]: {message['text']}")
            except asyncio.TimeoutError:
                break

asyncio.run(chat_client())
```

```javascript
// JavaScript/Browser example
const ws = new WebSocket("ws://localhost:8000/ws/chat?user_id=browser_client");

ws.onopen = () => {
  console.log("Connected to robot");
  ws.send("Hello robot!");
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log(`[${message.sender}]: ${message.text}`);
};

ws.onerror = (error) => {
  console.error("WebSocket error:", error);
};
```

---

## ROS Bridge (Advanced)

For advanced users, direct ROS topic communication is available via rosbridge on **port 9090**.

### Topics

#### Publishing to Robot

| Topic | Message Type | Description |
|-------|--------------|-------------|
| `/cmd_vel` | `geometry_msgs/msg/Twist` | Velocity commands (linear.x, angular.z) |
| `/mars/arm/commands` | `std_msgs/msg/Float64MultiArray` | Arm joint positions (6 joints) |
| `/sim_navigation/global_plan` | `nav_msgs/msg/Path` | Navigation path |
| `/sim_navigation/cancel` | `std_msgs/msg/Bool` | Cancel navigation |

#### Subscribing from Robot

| Topic | Message Type | Description |
|-------|--------------|-------------|
| `/odom` | `nav_msgs/msg/Odometry` | Robot odometry (position, orientation, velocity) |
| `/map` | `nav_msgs/msg/OccupancyGrid` | Occupancy grid map |
| `/mars/main_camera/image/compressed` | `sensor_msgs/msg/CompressedImage` | Main camera (JPEG) |
| `/mars/arm/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | Arm wrist camera |
| `/camera/depth/image_raw` | `sensor_msgs/msg/Image` | Depth camera |
| `/mars/arm/state` | `sensor_msgs/msg/JointState` | Current arm joint positions |
| `/sim_navigation/status` | `std_msgs/msg/String` | Navigation status |
| `/sim_navigation/feedback` | `geometry_msgs/msg/Point` | Distance to navigation goal |

### Services

| Service | Type | Description |
|---------|------|-------------|
| `/mars/arm/goto_js` | `maurice_msgs/srv/GotoJS` | Move arm to joint positions over duration |
| `/brain/reset_brain` | `brain_messages/srv/ResetBrain` | Reset robot brain with optional memory state |
| `/brain/set_brain_active` | `std_srvs/srv/SetBool` | Activate/deactivate brain |
| `/brain/get_chat_history` | `brain_messages/srv/GetChatHistory` | Retrieve chat history |
| `/brain/get_available_directives` | `brain_messages/srv/GetAvailableDirectives` | Get available agents |

### ROS Bridge Example

```python
import asyncio
import websockets
import json

async def send_velocity_command():
    uri = "ws://localhost:9090"
    
    async with websockets.connect(uri) as ws:
        # Advertise /cmd_vel topic
        advertise = {
            "op": "advertise",
            "topic": "/cmd_vel",
            "type": "geometry_msgs/msg/Twist"
        }
        await ws.send(json.dumps(advertise))
        
        # Publish velocity command (move forward)
        publish = {
            "op": "publish",
            "topic": "/cmd_vel",
            "msg": {
                "linear": {"x": 0.5, "y": 0.0, "z": 0.0},
                "angular": {"x": 0.0, "y": 0.0, "z": 0.0}
            }
        }
        await ws.send(json.dumps(publish))
        print("Velocity command sent!")

asyncio.run(send_velocity_command())
```

### Subscribe to Odometry

```python
import asyncio
import websockets
import json

async def subscribe_odom():
    uri = "ws://localhost:9090"
    
    async with websockets.connect(uri) as ws:
        # Subscribe to /odom
        subscribe = {
            "op": "subscribe",
            "topic": "/odom",
            "type": "nav_msgs/msg/Odometry"
        }
        await ws.send(json.dumps(subscribe))
        
        # Receive odometry messages
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("topic") == "/odom":
                pose = data["msg"]["pose"]["pose"]
                pos = pose["position"]
                print(f"Robot at: ({pos['x']:.2f}, {pos['y']:.2f})")

asyncio.run(subscribe_odom())
```

---

## Examples

### Python: Complete Control Client

```python
import asyncio
import aiohttp
import websockets
import json

class RobotClient:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.ws_url = base_url.replace("http", "ws") + "/ws/chat"
        
    async def get_position(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/get_robot_position") as resp:
                return await resp.json()
    
    async def reset(self, memory_state=None, position=None, orientation=None):
        payload = {}
        if memory_state:
            payload["memory_state"] = memory_state
        if position:
            payload["position"] = position
        if orientation:
            payload["orientation"] = orientation
            
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/reset_robot",
                json=payload
            ) as resp:
                return await resp.json()
    
    async def set_directive(self, directive):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/set_directive",
                json={"text": directive}
            ) as resp:
                return await resp.json()
    
    async def chat(self, message, timeout=30):
        """Send a chat message and wait for response."""
        async with websockets.connect(f"{self.ws_url}?user_id=robot_client") as ws:
            await ws.send(message)
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=timeout)
                return json.loads(response)
            except asyncio.TimeoutError:
                return None

# Usage
async def main():
    client = RobotClient()
    
    # Get current position
    pos = await client.get_position()
    print(f"Robot at: {pos['position']}")
    
    # Reset robot
    await client.reset()
    
    # Send chat message
    response = await client.chat("Hello, what can you do?")
    if response:
        print(f"Robot says: {response['text']}")

asyncio.run(main())
```

### JavaScript: Browser Dashboard

```html
<!DOCTYPE html>
<html>
<head>
    <title>Robot Control</title>
</head>
<body>
    <h1>Robot Dashboard</h1>
    
    <!-- Video feeds -->
    <div>
        <h2>Camera Feeds</h2>
        <img id="main-feed" src="http://localhost:8000/video_feed" width="640">
        <img id="chase-feed" src="http://localhost:8000/video_feed_chase" width="320">
    </div>
    
    <!-- Position display -->
    <div id="position">Position: Loading...</div>
    
    <!-- Chat -->
    <div>
        <h2>Chat</h2>
        <div id="messages"></div>
        <input type="text" id="chat-input" placeholder="Type a message...">
        <button onclick="sendMessage()">Send</button>
    </div>
    
    <!-- Controls -->
    <div>
        <h2>Controls</h2>
        <button onclick="resetRobot()">Reset Robot</button>
        <button onclick="setDirective('security_patrol')">Security Mode</button>
    </div>

    <script>
        const BASE_URL = "http://localhost:8000";
        let ws;

        // Connect to chat WebSocket
        function connectChat() {
            ws = new WebSocket(`ws://localhost:8000/ws/chat?user_id=dashboard`);
            ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                document.getElementById("messages").innerHTML += 
                    `<p><b>${msg.sender}:</b> ${msg.text}</p>`;
            };
        }

        function sendMessage() {
            const input = document.getElementById("chat-input");
            ws.send(input.value);
            input.value = "";
        }

        async function resetRobot() {
            await fetch(`${BASE_URL}/reset_robot`, { method: "POST" });
        }

        async function setDirective(directive) {
            await fetch(`${BASE_URL}/set_directive`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: directive })
            });
        }

        // Update position periodically
        setInterval(async () => {
            const resp = await fetch(`${BASE_URL}/get_robot_position`);
            const data = await resp.json();
            document.getElementById("position").textContent = 
                `Position: (${data.position[0].toFixed(2)}, ${data.position[1].toFixed(2)})`;
        }, 1000);

        connectChat();
    </script>
</body>
</html>
```

---

## Error Handling

### HTTP Error Responses

| Status Code | Meaning |
|-------------|---------|
| `200` | Success |
| `400` | Bad request (invalid parameters) |
| `500` | Server error (simulation not initialized) |
| `503` | Service unavailable (queue full) |

### WebSocket Error Handling

```javascript
ws.onerror = (error) => {
    console.error("Connection error:", error);
};

ws.onclose = (event) => {
    if (!event.wasClean) {
        console.log("Connection lost, reconnecting...");
        setTimeout(connectChat, 1000);
    }
};
```

---

## Notes

1. **Default Port**: The simulation server runs on port **8000** by default.
2. **CORS**: CORS is enabled for all origins, allowing browser-based clients.
3. **Message Queues**: Commands are queued and processed asynchronously. Queue full errors indicate the system is overloaded.
4. **User IDs**: Provide unique user IDs for WebSocket chat connections to enable proper message routing.
5. **ROS Bridge**: Port **9090** requires Innate OS (robot brain) to be running for full functionality.
