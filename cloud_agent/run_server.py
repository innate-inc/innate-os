import asyncio
import json
import time
import websockets
from websockets.exceptions import ConnectionClosed

# A simple environment variable or hardcoded token
VALID_TOKEN = "MY_HARDCODED_TOKEN"


async def echo_handler(websocket, path):
    # Check for a 'token' query parameter in the handshake
    query = websocket.path  # e.g. "/?token=..."
    # If path is "/?token=MYTOKEN", we can parse it:
    token_param = ""
    if "token=" in query:
        token_param = query.split("token=")[-1]

    if token_param != VALID_TOKEN:
        print("Invalid or missing token. Closing connection.")
        await websocket.close(code=4000, reason="Invalid token")
        return

    print("Client connected with valid token.")
    last_cmd_time = time.time()
    cmd_interval = 3.0  # every 3s we send a new command

    try:
        while True:
            now = time.time()
            if now - last_cmd_time > cmd_interval:
                last_cmd_time = now

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

            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                data = json.loads(message)

                if data.get("type") == "image":
                    print("Received an image (base64).")
                    # optional: decode or process
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.01)
    except ConnectionClosed:
        print("Client disconnected.")


async def main():
    # 0.0.0.0 is recommended so it listens on the container's external interface
    server = await websockets.serve(echo_handler, "0.0.0.0", 8765)
    print("Server started at ws://0.0.0.0:8765")
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
