"""Put a running simulator back at the start of the Nowhere story.

    cd sim && uv run sandbox/reset_nowhere.py [--port-base 8650]

Aborts the current challenge, switches the world to Nowhere and idles the
brain; the next Agent page load auto-starts a fresh attempt. To see the story
again in a browser that pressed Skip, also clear localStorage key
`innate.nowhere.skip.v1` on the simulator origin.
"""

import argparse
import json
import time

from websockets.sync.client import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-base", type=int, default=8650, help="INNATE_SIM_PORT_BASE of the checkout")
    base = parser.parse_args().port_base
    with connect(f"ws://127.0.0.1:{base + 2}", open_timeout=5) as ros:
        ros.send(json.dumps({"op": "call_service", "service": "/brain/set_brain_active", "args": {"data": False}}))
    with connect(f"ws://127.0.0.1:{base + 6}", open_timeout=5) as world:
        world.send(json.dumps({"op": "abort_challenge"}))
        world.send(json.dumps({"op": "switch_environment", "id": "void"}))
        deadline = time.time() + 90
        environment = challenge = None
        while time.time() < deadline:
            frame = json.loads(world.recv())
            environment = frame.get("environment", environment)
            challenge = frame.get("challenge", challenge)
            settled = (environment or {}).get("id") == "void" and not frame.get("switch")
            if settled and challenge is not None and not challenge.get("active"):
                break
    print("Nowhere, nothing running, brain idle. Reload the Agent page.")


if __name__ == "__main__":
    main()
