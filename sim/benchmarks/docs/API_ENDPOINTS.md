# Benchmark API Endpoints

This document outlines the API endpoints used by the benchmarking system to interact with the robot simulation.

## Robot Position and Movement

### Get Robot Position

```
GET /get_robot_position
```

Returns the current 2D position (x, y) of the robot in the simulation environment.

**Response Format:**
```json
{
  "position": [x, y, z],  // The system only uses x and y coordinates
  "timestamp": 1234567890.123  // Unix timestamp
}
```

**Example Response:**
```json
{
  "position": [12.5, 7.3, 0.2],
  "timestamp": 1682574329.456
}
```

**Usage in Benchmarks:**
This endpoint is used for location checks to determine if the robot is within a specified 2D bounding box area, defined by [x1, y1, x2, y2] coordinates.

### Reset Robot

```
POST /reset_robot
```

Resets the robot to its starting position.

**Response Format:**
```json
{
  "status": "reset_enqueued"
}
```

## Directives and Communication

### Set Directive

```
POST /set_directive
```

**Request Body:**
```json
{
  "text": "Your directive text here"
}
```

**Response Format:**
```json
{
  "status": "directive_enqueued"
}
```

### Video Feeds

```
GET /video_feed
```
Provides the first-person camera feed from the robot.

```
GET /video_feed_chase
```
Provides the chase (third-person) camera feed.

```
GET /video_feeds_ready
```
Checks if the video feeds are ready.

**Response Format:**
```json
{
  "ready": true
}
```

## WebSocket Communication

The benchmarking system uses WebSockets to monitor and send chat messages.

**WebSocket URL:** `ws://localhost:8000/ws`

### Chat Message Format

```json
{
  "sender": "user" | "robot",
  "text": "Message content",
  "timestamp": 1234567890.123
}
```

## Integration Notes

1. All API endpoints use the base URL specified when starting the benchmark (default: `http://localhost:8000`).
2. The location check functionality requires the `/get_robot_position` endpoint to be functioning properly.
3. WebSocket communication is used for both monitoring chat and sending scheduled messages. 