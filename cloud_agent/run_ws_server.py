# run_server.py
import asyncio
import json
import time
import websockets


async def echo_handler(websocket):
    print("Client connected.")
    last_cmd_time = time.time()
    cmd_interval = 3.0  # every 3s we send a new command

    try:
        while True:
            # 1) Check if we need to send a new random velocity command
            now = time.time()
            if now - last_cmd_time > cmd_interval:
                last_cmd_time = now

                # Use time to alternate between left and right
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

            # 2) Listen for incoming messages (images, etc.)
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                data = json.loads(message)

                if data.get("type") == "image":
                    print("Received an image from the agent (base64).")
                    # You could decode & process it if you want
                    # b64_data = data["image_b64"]
                    # ...
            except asyncio.TimeoutError:
                pass  # no new messages arrived

            # Sleep briefly
            await asyncio.sleep(0.01)
    finally:
        print("Client disconnected.")


async def main():
    server = await websockets.serve(echo_handler, "localhost", 8765)
    print("Server started at ws://localhost:8765")
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
