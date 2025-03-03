from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse
import os
import time
import asyncio
from src.shared_queues import SharedQueues, ChatMessage, ChatSignal
from typing import Set, Dict, Any
from src.middleware.auth import get_current_user
from src.middleware.authorized_users import is_authorized


router = APIRouter()

# Track connected clients by user ID
connected_clients: Set[str] = set()
# Store user email by user ID
user_emails: Dict[str, str] = {}


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


# Add an endpoint to store user email
@router.get("/auth/user-info")
async def get_user_info(user_info: Dict[str, Any] = Depends(get_current_user)):
    """
    Get the authenticated user's information and store their email
    """
    user_id = user_info.get("user_id", "")
    email = user_info.get("email", "")

    print(f"Storing user info: user_id={user_id}, email={email}")

    # Store the email for later use
    if user_id and email:
        user_emails[user_id] = email
        print(f"Stored email {email} for user {user_id}")

    return {
        "user_id": user_id,
        "email": email,
        "is_authorized": is_authorized({"email": email}) if email else False,
    }


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

    # Get email from query parameters
    query_params = dict(websocket.query_params)
    email = query_params.get("email", "")

    # Store the email for later use
    if user_id and email:
        user_emails[user_id] = email
        print(f"Stored email {email} for user {user_id} " f"from WebSocket connection")

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
            try:
                print(f"Waiting for message from user {user_id}...")
                data = await websocket.receive_text()
                print(f"Received message from user {user_id}: {data}")

                # Verify user is still connected before processing the message
                if user_id in connected_clients:
                    # Get the user's email from our stored mapping
                    user_email = get_user_email_from_id(user_id)
                    print(f"User email: {user_email}")

                    # If we don't have an email stored, use the one from the query parameters
                    if not user_email and email:
                        user_email = email
                        user_emails[user_id] = email
                        print(f"Using email from query parameters: {email}")

                    if not is_authorized({"email": user_email}):
                        print(
                            f"User {user_id} with email {user_email} "
                            f"is not authorized"
                        )
                        await websocket.send_json(
                            {
                                "sender": "system",
                                "text": "You are not authorized to send messages. "
                                "Please subscribe or contact axel@innate.bot for access.",
                                "timestamp": time.time(),
                                "error": True,
                            }
                        )
                        continue

                    print(
                        f"User {user_id} with email {user_email} is authorized, "
                        f"forwarding message"
                    )
                    new_entry = ChatMessage(
                        sender="user",
                        text=data,
                        timestamp=time.time(),
                        timestamp_put_in_queue=time.time(),
                    )
                    shared_queues.chat_to_bridge.put_nowait(new_entry)

                    # Echo the message back to the user for testing
                    echo_msg = ChatMessage(
                        sender="robot",
                        text=f"Echo: {data}",
                        timestamp=time.time(),
                    )
                    await websocket.send_json(
                        {
                            "sender": echo_msg.sender,
                            "text": echo_msg.text,
                            "timestamp": echo_msg.timestamp,
                        }
                    )
                    print(f"Sent echo message to user {user_id}")

                else:
                    # If user is not in connected_clients, send an error message
                    print(f"User {user_id} is not in connected_clients")
                    await websocket.send_json(
                        {
                            "sender": "system",
                            "text": "You must be connected to send messages.",
                            "timestamp": time.time(),
                            "error": True,
                        }
                    )
            except Exception as e:
                print(f"Error in handle_inbound_user: {e}")
                # Don't re-raise the exception to keep the loop running

    # Task B: handle outbound messages from chat_from_bridge -> send to WebSocket
    async def handle_outbound_agent():
        while True:
            try:
                print(f"Waiting for message from bridge for user {user_id}...")
                # Sometimes messages in the queue will get lost
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, shared_queues.chat_from_bridge.get
                )
                print(f"Received message from bridge: {msg.sender}: {msg.text[:50]}...")

                await websocket.send_json(
                    {
                        "sender": msg.sender,
                        "text": msg.text,
                        "timestamp": msg.timestamp,
                    }
                )
                print(f"Sent message to user {user_id}")
            except Exception as e:
                print(f"Error in handle_outbound_agent: {e}")
                # Don't re-raise the exception to keep the loop running

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


# Helper function to get user email from user ID
def get_user_email_from_id(user_id: str) -> str:
    """
    Get the user's email from their user ID.
    Retrieves the email from the stored mapping of user IDs to emails.
    """
    # Return the stored email if available
    if user_id in user_emails:
        email = user_emails.get(user_id, "")
        print(f"Found email {email} for user {user_id} in stored mapping")
        return email

    # For anonymous users or if email not found
    if user_id == "anonymous":
        print("Anonymous user, no email")
        return ""

    # If we don't have the email stored yet, return empty string
    # The frontend should call /auth/user-info to store the email
    print(f"No email found for user {user_id} in stored mapping")
    return ""
