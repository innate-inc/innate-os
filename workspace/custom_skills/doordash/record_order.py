# SPDX-License-Identifier: Apache-2.0
import difflib
import json
from pathlib import Path

from innate import Skill, SkillReturn

ORDERS = Path.home() / "doordash_orders.json"
ROSTER = ["Dhruv", "Ayen", "Axel", "Theo", "Nick"]


def load_orders() -> list[dict]:
    return json.loads(ORDERS.read_text()) if ORDERS.exists() else []


def mission_status(orders: list[dict]) -> str:
    done = {o["name"] for o in orders}
    missing = [n for n in ROSTER if n not in done]
    lines = "; ".join(
        f"{o['name']}: {'DECLINED' if o['order'].lower() == 'declined' else o['order']}" for o in orders
    ) or "nobody yet"
    if missing:
        return (f"Status — recorded: {lines}. STILL MISSING: {', '.join(missing)}. "
                f"Ask the person in front of you where {missing[0]} is, then use go_direction.")
    return f"Status — recorded: {lines}. ALL FIVE DONE — announce the list and place the order."


class RecordOrder(Skill):
    """Record one roster person's DoorDash order the moment they tell you it —
    no confirmation question needed first. If they correct you afterwards,
    just call this again with the fix; it updates in place. Use
    order='declined' for someone who doesn't want anything. Only the five
    roster people exist: Dhruv, Ayen, Axel, Theo, Nick — close-sounding names
    are matched automatically."""

    def execute(self, person_name: str, order: str, notes: str = "") -> SkillReturn:
        heard = person_name.strip()
        match = difflib.get_close_matches(
            heard.capitalize(), ROSTER, n=1, cutoff=0.5
        )
        if not match:
            self.fail(
                f"'{heard}' matches nobody on the roster ({', '.join(ROSTER)}). "
                "Ask them to repeat their name — do not guess."
            )
        name = match[0]

        orders = load_orders()
        entry = {"name": name, "order": order.strip(), "notes": notes}
        for i, o in enumerate(orders):
            if o["name"] == name:
                orders[i] = entry
                ORDERS.write_text(json.dumps(orders, indent=2))
                return (f"UPDATED {name}'s order to: {order}. "
                        f"(You had already talked to {name}.) " + mission_status(orders))
        orders.append(entry)
        ORDERS.write_text(json.dumps(orders, indent=2))
        note = f" (heard '{heard}', matched to roster name {name})" if heard.capitalize() != name else ""
        return (f"Recorded {name}: {order}.{note} Say thanks, then call remember_face. "
                + mission_status(orders))
