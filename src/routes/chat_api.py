from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import os
import time
import asyncio
from src.shared_queues import SharedQueues, ChatMessage, ChatSignal
from src.agent import agent_websocket_bridge  # to access bridge_chat_history


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
    - Sends them back immediately
    - Forwards them to agent_websocket_bridge
    """
    await websocket.accept()

    # Retrieve shared queues from the application's state
    shared_queues: SharedQueues = websocket.app.state.SHARED_QUEUES

    # 1) Upon connection, signal to the bridge that we're ready to receive messages history
    shared_queues.chat_to_bridge.put_nowait(
        ChatSignal(signal="ready", timestamp=time.time())
    )

    # Task A: handle inbound messages from user -> push to chat_to_bridge
    async def handle_inbound_user():
        while True:
            data = await websocket.receive_text()
            new_entry = ChatMessage(sender="user", text=data, timestamp=time.time())
            shared_queues.chat_to_bridge.put_nowait(new_entry)

    # Task B: handle outbound messages from chat_from_bridge -> send to WebSocket
    async def handle_outbound_agent():
        while True:
            # BUG: sometimes one of these messages in the queue will get lost and i can't figure why
            msg = await asyncio.get_event_loop().run_in_executor(
                None, shared_queues.chat_from_bridge.get
            )
            print(f"[ChatAPI] Sending message: {msg}")
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
        for t in tasks:
            t.cancel()
