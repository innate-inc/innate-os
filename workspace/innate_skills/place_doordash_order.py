# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Skill, SkillReturn


class PlaceDoordashOrder(Skill):
    """Submit the three confirmed household orders to the simulated DoorDash
    checkout. Provide one complete order for Alex, Blake, and Casey. This demo
    skill records the checkout without contacting DoorDash or spending money."""

    def execute(self, alex_order: str, blake_order: str, casey_order: str) -> SkillReturn:
        orders = {
            "Alex": alex_order.strip(),
            "Blake": blake_order.strip(),
            "Casey": casey_order.strip(),
        }
        missing = [name for name, order in orders.items() if not order]
        if missing:
            self.fail(f"Missing confirmed order for: {', '.join(missing)}")

        summary = "\n".join(f"- {name}: {order}" for name, order in orders.items())
        self.logger.info(f"[PlaceDoordashOrder] Simulated DoorDash checkout:\n{summary}")
        return "All three orders were submitted to the simulated DoorDash checkout."
