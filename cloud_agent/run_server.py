import asyncio
import json
import time

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode


# Example hard-coded token check
def get_user_from_token(token: str):
    """
    Return user identifier if token is valid; return None if invalid.
    Replace this with real logic (DB lookup, etc.) if needed.
    """
    if token == "MY_HARDCODED_TOKEN":
        return "user123"
    return None


async def echo_handler(websocket):
    """
    This handler:
    1) Receives the FIRST MESSAGE from the client (as the token).
    2) Authenticates or rejects the connection.
    3) If successful, proceeds with the normal velocity command logic.
    """

    # --- AUTHENTICATION STEP ---
    try:
        # Wait for the FIRST message: it should be the token
        token = await websocket.recv()
        print(f"Received first message (token): {token}")
    except ConnectionClosed:
        print("Connection closed before a token was received.")
        return

    user_id = get_user_from_token(token)
    if user_id is None:
        print("Invalid token. Closing connection.")
        await websocket.close(
            code=CloseCode.INTERNAL_ERROR, reason="authentication failed"
        )
        return

    print(f"Client authenticated as: {user_id}")

    # --- NORMAL HANDLER LOGIC ---
    last_cmd_time = time.time()
    cmd_interval = 3.0  # every 3s, send a new command

    try:
        while True:
            # 1) Possibly send a velocity command
            now = time.time()
            if now - last_cmd_time > cmd_interval:
                last_cmd_time = now

                # Alternate left vs right
                turn_left = int(time.time() / cmd_interval) % 2 == 0
                if turn_left:
                    vel_left = 10.0
                    vel_right = 0.0
                else:
                    vel_left = 0.0
                    vel_right = 10.0

                msg = {"cmd": "set_velocity", "values": [vel_left, vel_right]}
                await websocket.send(json.dumps(msg))
                print(f"Sent velocity command: {msg['values']}")

            # 2) Listen for incoming messages
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                data = json.loads(message)

                if data.get("type") == "image":
                    print("Received an image (base64).")
                    # optional: decode or process image data here
            except asyncio.TimeoutError:
                pass  # no new messages

            await asyncio.sleep(0.01)

    except ConnectionClosed:
        print("Client disconnected.")


async def main():
    # Listen on port 8765
    server = await websockets.serve(echo_handler, "0.0.0.0", 8765)
    print("Server started at ws://0.0.0.0:8765")
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
