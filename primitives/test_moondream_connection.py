#!/usr/bin/env python3
"""
Standalone test script for MoondreamInferenceBeta Cloud Run connection.
Tests the WebSocket connection without needing the full ROS2 stack.

Usage:
  # With GCP_AUTH_TOKEN env var (recommended for robot):
  export GCP_AUTH_TOKEN="your-token-here"
  python3 test_moondream_connection.py

  # With gcloud CLI (on local dev machine):
  python3 test_moondream_connection.py

  # Test local server:
  python3 test_moondream_connection.py --local 192.168.1.100 8780
"""

import asyncio
import os
import subprocess
import sys


def get_gcp_identity_token():
    """Get GCP identity token for Cloud Run authentication."""
    # 1. Check environment variable first
    env_token = os.environ.get('GCP_AUTH_TOKEN')
    if env_token:
        print("✅ Using token from GCP_AUTH_TOKEN environment variable")
        return env_token
    
    # 2. Try gcloud CLI
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            text=True,
            stderr=subprocess.DEVNULL
        ).strip()
        print("✅ Using token from gcloud CLI")
        return token
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to get GCP identity token: {e}")
        return None
    except FileNotFoundError:
        print("❌ No token available!")
        print("   Options:")
        print("   1. Set GCP_AUTH_TOKEN environment variable:")
        print("      export GCP_AUTH_TOKEN=\"$(gcloud auth print-identity-token)\"")
        print("   2. Install gcloud CLI")
        return None


async def test_cloud_run_connection(server_address: str = "waypoint-inference-163997526502.us-east4.run.app"):
    """Test WebSocket connection to Cloud Run server."""
    try:
        import websockets
    except ImportError:
        print("❌ websockets library not installed. Run: pip install websockets")
        return False

    print(f"🔐 Getting GCP identity token...")
    token = get_gcp_identity_token()
    if not token:
        return False
    print(f"   Token length: {len(token)} chars")

    server_url = f"wss://{server_address}/ws"
    print(f"🌐 Connecting to {server_url}...")

    try:
        async with websockets.connect(
            server_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=10 * 1024 * 1024
        ) as ws:
            print("✅ Connected!")

            # Wait for initial message
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"📨 Received: {message}")
            except asyncio.TimeoutError:
                print("⏱️  No initial message received (this may be normal)")

            # Send a test status message
            import json
            test_msg = json.dumps({
                "type": "status",
                "source": "robot",
                "state": "test",
                "message": "Connection test from robot"
            })
            await ws.send(test_msg)
            print(f"📤 Sent test message")

            # Wait for response
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"📨 Response: {response}")
            except asyncio.TimeoutError:
                print("⏱️  No response received")

            print("\n✅ Cloud Run connection test PASSED!")
            return True

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


async def test_local_connection(server_address: str, server_port: int = 8780):
    """Test WebSocket connection to local server."""
    try:
        import websockets
    except ImportError:
        print("❌ websockets library not installed. Run: pip install websockets")
        return False

    server_url = f"ws://{server_address}:{server_port}"
    print(f"🌐 Connecting to {server_url}...")

    try:
        async with websockets.connect(server_url, max_size=10 * 1024 * 1024) as ws:
            print("✅ Connected!")

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"📨 Received: {message}")
            except asyncio.TimeoutError:
                print("⏱️  No initial message received")

            print("\n✅ Local connection test PASSED!")
            return True

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


def main():
    print("=" * 60)
    print("MoondreamInferenceBeta Connection Tester")
    print("=" * 60)

    if len(sys.argv) > 1:
        if sys.argv[1] == "--local":
            # Test local server
            address = sys.argv[2] if len(sys.argv) > 2 else "localhost"
            port = int(sys.argv[3]) if len(sys.argv) > 3 else 8780
            print(f"\n🔧 Testing LOCAL server at {address}:{port}\n")
            asyncio.run(test_local_connection(address, port))
        else:
            # Test custom Cloud Run URL
            print(f"\n☁️  Testing Cloud Run server: {sys.argv[1]}\n")
            asyncio.run(test_cloud_run_connection(sys.argv[1]))
    else:
        # Test default Cloud Run
        print("\n☁️  Testing Cloud Run (default)\n")
        asyncio.run(test_cloud_run_connection())

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()

