# SPDX-License-Identifier: Apache-2.0
import json
import urllib.request
from pathlib import Path

from innate import Skill, SkillReturn

from custom_skills.doordash.record_order import ROSTER, load_orders

ORDERS = Path.home() / "doordash_orders.json"
ORDER_RUNNER_URL = "http://192.168.0.32:8123/order"  # Dhruv's Mac on office LAN


class PlaceDoorDashOrder(Skill):
    """Place the group DoorDash order. Refuses to run until every roster
    person has an order or a decline recorded — it will tell you who is
    missing. Pass force=true only if a human explicitly told you to order
    without the missing people."""

    def execute(self, force: bool = False) -> SkillReturn:
        orders = load_orders()
        done = {o["name"] for o in orders}
        missing = [n for n in ROSTER if n not in done]
        if missing and not force:
            self.fail(
                f"Not everyone is in yet — still missing: {', '.join(missing)}. "
                "Go find them first (ask people where they are, use go_direction). "
                "Only pass force=true if a human told you to order without them."
            )
        real = [o for o in orders if o["order"].lower() != "declined"]
        if not real:
            self.fail("Everyone declined — nothing to order.")

        summary = "; ".join(f"{o['name']}: {o['order']}" for o in real)
        try:
            req = urllib.request.Request(
                ORDER_RUNNER_URL, data=json.dumps(real).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read())
            sent = "Order sent to the runner"
        except Exception:
            sent = "Order runner unreachable — orders saved locally for a human to place"

        ORDERS.rename(ORDERS.with_suffix(".sent.json"))
        return f"{sent}. Final list for {len(real)} people: {summary}"
