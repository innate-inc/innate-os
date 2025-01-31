from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import os
import time

# A simple in-memory store for chat messages (in production, store these in a DB)
CHAT_HISTORY = [
    {"sender": "robot", "text": "Hello! How can I assist you today?"},
    {"sender": "user", "text": "Can you look for the teapot?"},
]

router = APIRouter()


@router.get("/")
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
async def chat_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for exchanging chat messages with the frontend.
    - Receives text messages from the client
    - Broadcasts them back to all connected clients
    - Sends the current chat log on new connection
    """
    await websocket.accept()

    # 1) Upon connection, send the existing history
    for msg in CHAT_HISTORY:
        await websocket.send_json({"sender": msg["sender"], "text": msg["text"]})

    # 2) Now start listening for newly received messages
    try:
        while True:
            data = await websocket.receive_text()
            # For demonstration, treat all inbound client messages as from "user"
            new_entry = {"sender": "user", "text": data, "timestamp": time.time()}
            CHAT_HISTORY.append(new_entry)
            # Echo it back to the same client (or broadcast it to many if needed)
            # Here it's just an echo, but you'd typically track all connected clients
            await websocket.send_json({"sender": "user", "text": data})
    except WebSocketDisconnect:
        # The client has disconnected
        pass
