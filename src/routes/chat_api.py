from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse
import os
import time
import asyncio
from src.shared_queues import SharedQueues, ChatMessage, ChatSignal
from typing import Set
from src.middleware.auth import get_current_user


router = APIRouter()

# Track connected clients by user ID
connected_clients: Set[str] = set()


@router.get("/", dependencies=[Depends(get_current_user)])
def serve_react_app():
    """
    Serves the React frontend index.html from the pre-built dist folder.
    """
    # Adjust path as needed:
    frontend_build_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "frontend", "dist"
    )
    index_path = os.path.join(frontend_build_path, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket, user_id: str = None):
    """
    WebSocket endpoint for exchanging chat messages with the frontend.
    - Receives text messages from the client
    - Sends them back immediately
    - Forwards them to agent_websocket_bridge

    The user_id is expected to be passed as a query parameter.
    """
    await websocket.accept()

    # Get user_id from query parameters
    if not user_id:
        query_params = dict(websocket.query_params)
        user_id = query_params.get("user_id", "anonymous")

    # Add user to connected clients
    connected_clients.add(user_id)
    print(f"Client connected: {user_id}. Total connected: {len(connected_clients)}")

    # Retrieve shared queues from the application's state
    shared_queues: SharedQueues = websocket.app.state.SHARED_QUEUES

    # Upon connection, signal to the bridge that we're ready to receive messages
    shared_queues.chat_to_bridge.put_nowait(
        ChatSignal(signal="ready", timestamp=time.time())
    )

    # Task A: handle inbound messages from user -> push to chat_to_bridge
    async def handle_inbound_user():
        while True:
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

    # Task B: handle outbound messages from chat_from_bridge -> send to WebSocket
    async def handle_outbound_agent():
        while True:
            # Sometimes messages in the queue will get lost
            msg = await asyncio.get_event_loop().run_in_executor(
                None, shared_queues.chat_from_bridge.get
            )
            await websocket.send_json(
                {
                    "sender": msg.sender,
                    "text": msg.text,
                    "timestamp": msg.timestamp,
                }
            )

    try:
        # Run both tasks concurrently until WebSocket disconnect or error
        tasks = [
            asyncio.create_task(handle_inbound_user()),
            asyncio.create_task(handle_outbound_agent()),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except WebSocketDisconnect:
        # The client disconnected
        pass
    finally:
        # Remove user from connected clients when disconnected
        if user_id in connected_clients:
            connected_clients.remove(user_id)
            print(
                f"Client disconnected: {user_id}. "
                f"Total connected: {len(connected_clients)}"
            )
        for t in tasks:
            t.cancel()


# Add a new endpoint to check if a user is connected
@router.get("/is-connected/{user_id}", dependencies=[Depends(get_current_user)])
async def check_connection_status(user_id: str):
    """
    Check if a user is currently connected via WebSocket.
    Returns True if connected, False otherwise.
    """
    return {"connected": user_id in connected_clients}
