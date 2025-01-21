import asyncio
import websockets


async def test_websocket():
    # Replace with your actual Cloud Run URL (no port needed).
    # Use wss:// for a secure WebSocket connection.
    uri = "wss://innate-agent-websocket-service-533276562345.us-central1.run.app"

    print(f"Connecting to {uri}...")

    try:
        # 1) Connect to the WebSocket server
        async with websockets.connect(uri) as websocket:
            print("Connected!")

            # 2) Immediately send your hard-coded token
            token = "MY_HARDCODED_TOKEN"
            print(f"Sending token: {token}")
            await websocket.send(token)

            # 3) Listen for messages from the server
            #    We'll just do a simple loop reading whatever the server sends
            while True:
                message = await websocket.recv()
                print("Received from server:", message)
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"Connection closed with error: {e}")
    except Exception as e:
        print(f"Failed to connect or other error: {e}")
        # Give the full error message
        print(f"Full error message: {e}")


asyncio.run(test_websocket())
