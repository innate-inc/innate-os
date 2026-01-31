from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import os
import time
import asyncio
from src.shared_queues import SharedQueues, ChatMessage, ChatSignal
from typing import Dict

router = APIRouter()

# Track connected clients by user ID
connected_clients: Dict[str, WebSocket] = {}
# Broadcast task
broadcast_task = None


@router.get("/")
def serve_react_app():
    """Serves the React frontend index.html from the pre-built dist folder."""
    frontend_build_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "frontend", "dist"
    )
    index_path = os.path.join(frontend_build_path, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


# Function to broadcast messages to all connected clients
async def broadcast_messages(shared_queues: SharedQueues):
    """Fetch messages from queue and broadcast to all connected clients.

    This ensures all users receive the same messages.
    """
    try:
        while True:
            try:
                # Get messages from the queue
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, shared_queues.chat_from_bridge.get
                )

                # Broadcast to all connected clients
                disconnected_clients = []
                for user_id, websocket in connected_clients.items():
                    try:
                        await websocket.send_json(
                            {
                                "sender": msg.sender,
                                "text": msg.text,
                                "timestamp": msg.timestamp,
                            }
                        )
                    except Exception:
                        # Mark client for removal if sending fails
                        disconnected_clients.append(user_id)

                # Clean up disconnected clients
                for user_id in disconnected_clients:
                    if user_id in connected_clients:
                        del connected_clients[user_id]
            except Exception:
                # Don't re-raise the exception to keep the loop running
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        # Task was cancelled, clean up
        pass


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket, user_id: str = None):
    """WebSocket endpoint for exchanging chat messages with the frontend."""
    await websocket.accept()

    # Get user_id from query parameters
    if not user_id:
        query_params = dict(websocket.query_params)
        user_id = query_params.get("user_id", "anonymous")

    # Add user to connected clients
    connected_clients[user_id] = websocket

    # Retrieve shared queues from the application's state
    shared_queues: SharedQueues = websocket.app.state.SHARED_QUEUES

    # Start the broadcast task if it's not already running
    global broadcast_task
    if broadcast_task is None or broadcast_task.done():
        broadcast_task = asyncio.create_task(broadcast_messages(shared_queues))

    # Upon connection, signal to the bridge that we're ready to receive messages
    shared_queues.chat_to_bridge.put_nowait(
        ChatSignal(signal="ready", timestamp=time.time())
    )

    try:
        # Only handle inbound messages from the user
        await handle_inbound_user(websocket, user_id, shared_queues)
    except WebSocketDisconnect:
        # The client disconnected
        pass
    except Exception:
        pass
    finally:
        # Remove user from connected clients when disconnected
        if user_id in connected_clients:
            del connected_clients[user_id]


async def handle_inbound_user(
    websocket: WebSocket, user_id: str, shared_queues: SharedQueues
):
    """Handle inbound messages from the user."""
    try:
        while True:
            try:
                data = await websocket.receive_text()

                # Verify user is still connected before processing the message
                if user_id in connected_clients:
                    new_entry = ChatMessage(
                        sender="user",
                        text=data,
                        timestamp=time.time(),
                        timestamp_put_in_queue=time.time(),
                    )
                    shared_queues.chat_to_bridge.put_nowait(new_entry)
                else:
                    # If user is not in connected_clients, send an error message
                    await websocket.send_json(
                        {
                            "sender": "system",
                            "text": "You must be connected to send messages.",
                            "timestamp": time.time(),
                            "error": True,
                        }
                    )
            except WebSocketDisconnect:
                if user_id in connected_clients:
                    del connected_clients[user_id]
                break
            except Exception:
                # Don't re-raise the exception to keep the loop running
                pass
    except Exception:
        if user_id in connected_clients:
            del connected_clients[user_id]


# Add a new endpoint to check if a user is connected
@router.get("/is-connected/{user_id}")
async def check_connection_status(user_id: str):
    """Check if a user is currently connected via WebSocket."""
    return {"connected": user_id in connected_clients}
